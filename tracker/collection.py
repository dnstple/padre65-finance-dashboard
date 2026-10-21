"""Collection planning: how much of each product, colourway and size to buy for a collection.

Two things are kept apart throughout:
- the EXPECTATION: what history suggests the collection will sell (by product type), with a low/high range;
- the PLAN: how much stock to buy - your own quantities, what demand supports, a revenue target, or a budget.
Buying more does not mean selling more: outcomes are always measured against the expectation.

Demand model (per product TYPE, then split down)
- Pop-up demand: units of that type sold at past pop-ups / number of pop-ups (sizes that sold out at a pop-up get an
  uplift), times each planned pop-up's size relative to the last one.
- Online demand: the type's online sales per day while in stock, times the days in the selling period.
- Types with no history borrow from the closest type (e.g. knitwear from crews, then hoodies); you can type your own.
- The type's demand is split across its colourways by colour weight, then across sizes by the type's size mix.

Costs per colourway come from the supplier's price (with price breaks), converted to £, plus customising per unit.
Samples still to pay come out of the budget but aren't stock.
"""
import json
import math
from datetime import date

import pandas as pd

from . import config, costs, db, events

STATUSES = ["Considering", "Likely", "Confirmed", "Dropped"]
PRODUCT_TYPES = ["Jacket", "Knitwear", "Sweatshirt", "Hoodie", "Long Sleeve", "T-Shirt", "Jeans", "Tracksuit", "Shorts",
                 "Cap", "Foulard", "Other"]
# Which sales history stands in for a type (first one with sales wins)
HISTORY_FOR = {"Knitwear": ["Sweatshirt", "Hoodie"], "Sweatshirt": ["Sweatshirt", "Hoodie"], "Shorts": ["Shorts", "Tracksuit"],
               "Tracksuit": ["Tracksuit", "Hoodie"], "Jacket": ["Jacket", "Jeans"]}
CURRENCIES = ["GBP", "EUR", "USD"]
APPAREL_SIZES = ["S", "M", "L", "XL"]
DEFAULT_SIZES = {t: APPAREL_SIZES for t in PRODUCT_TYPES}
DEFAULT_SIZES.update({"Jeans": ["28", "30", "32", "34", "36"], "Cap": ["One size"], "Foulard": ["One size"], "Other": ["One size"]})
# +/- range around the expectation, by how much evidence there is
BANDS = {"High": 0.25, "Medium": 0.40, "Low": 0.60, "Your estimate": 0.50, "No evidence": 0.75}
MODES = {"draft": "Your quantities", "evidence": "What demand supports", "target": "Hit a revenue target",
         "budget": "Fit the budget"}
DEFAULT_SETTINGS = {
    "mode": "draft", "ambition": 1.0, "target_revenue": None, "budget": None, "cover": 1.15, "min_per_size": 1,
    "moq_round": True, "soldout_uplift": 0.25, "type_emphasis": {}, "type_demand": {}, "fx": {"EUR": 0.8418, "USD": 0.79},
    "custom_buffer_weeks": 2, "markup": 2.5, "popups": [{"name": "Pop-up 1", "date": None, "scale": 1.0},
                                                         {"name": "Pop-up 2", "date": None, "scale": 1.0}],
}


# --- storage -----------------------------------------------------------------------

def _loads(s, default):
    try:
        return json.loads(s) if s else default
    except (TypeError, ValueError):
        return default


def list_collections(conn):
    return db.read_df(conn, "SELECT * FROM plan_collections ORDER BY id DESC")


def get_collection(conn, cid):
    row = conn.execute("SELECT * FROM plan_collections WHERE id = ?", (cid,)).fetchone()
    if not row:
        return None
    col = dict(row)
    col["settings"] = {**DEFAULT_SETTINGS, **_loads(col.get("settings"), {})}
    col["overrides"] = _loads(col.get("overrides"), {})
    return col


def create_collection(conn, name, season, start_date, end_date, notes=""):
    cur = conn.execute("""INSERT INTO plan_collections (name, season, start_date, end_date, planned_events, notes,
                          settings, overrides, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                       (name, season, str(start_date), str(end_date), 2, notes,
                        json.dumps(DEFAULT_SETTINGS), "{}", db.now_iso(), db.now_iso()))
    conn.commit()
    return cur.lastrowid


def update_collection(conn, cid, **fields):
    for k in ("settings", "overrides"):
        if k in fields and not isinstance(fields[k], str):
            fields[k] = json.dumps(fields[k])
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE plan_collections SET {sets}, updated_at = ? WHERE id = ?", (*fields.values(), db.now_iso(), cid))
    conn.commit()


def delete_collection(conn, cid):
    for table in ("plan_items", "plan_scenarios"):
        conn.execute(f"DELETE FROM {table} WHERE collection_id = ?", (cid,))
    conn.execute("DELETE FROM plan_collections WHERE id = ?", (cid,))
    conn.commit()


ITEM_FIELDS = ["name", "colour", "product_type", "supplier", "sizes", "currency", "unit_cost", "tiers", "moq",
               "sample_cost", "custom_cost", "lead_weeks", "price", "draft_units", "emphasis", "status", "notes"]
NUMERIC_FIELDS = ("unit_cost", "moq", "sample_cost", "custom_cost", "lead_weeks", "price", "draft_units", "emphasis")


def load_items(conn, cid):
    df = db.read_df(conn, f"SELECT id, {', '.join(ITEM_FIELDS)} FROM plan_items WHERE collection_id = ? ORDER BY id", (cid,))
    if df.empty:
        df = pd.DataFrame(columns=["id"] + ITEM_FIELDS)
    for c in NUMERIC_FIELDS:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    for c in set(ITEM_FIELDS) - set(NUMERIC_FIELDS):
        df[c] = df[c].astype("object").where(df[c].notna(), None)
    return df


def _clean(v):
    if v is None or (not isinstance(v, str) and pd.isna(v)):
        return None
    return v.item() if hasattr(v, "item") else v


def add_item(conn, cid, **values):
    values = {"emphasis": 1.0, "status": "Considering", "currency": "GBP", **values}
    cur = conn.execute(f"INSERT INTO plan_items (collection_id, {', '.join(ITEM_FIELDS)}, created_at, updated_at) "
                       f"VALUES (?, {', '.join('?' for _ in ITEM_FIELDS)}, ?, ?)",
                       (cid, *[_clean(values.get(f)) for f in ITEM_FIELDS], db.now_iso(), db.now_iso()))
    conn.commit()
    return cur.lastrowid


def save_items(conn, cid, edited):
    """Upsert edited rows by id (ids stay stable so quantity overrides keep pointing at the right item)."""
    existing = {int(r[0]) for r in conn.execute("SELECT id FROM plan_items WHERE collection_id = ?", (cid,))}
    kept = set()
    for r in edited.to_dict("records"):
        if not str(r.get("name") or "").strip():
            continue
        rid = r.get("id")
        if rid is None or pd.isna(rid):
            add_item(conn, cid, **{f: r.get(f) for f in ITEM_FIELDS})
            continue
        rid = int(rid)
        kept.add(rid)
        conn.execute(f"UPDATE plan_items SET {', '.join(f'{f} = ?' for f in ITEM_FIELDS)}, updated_at = ? WHERE id = ?",
                     (*[_clean(r.get(f)) for f in ITEM_FIELDS], db.now_iso(), rid))
    for rid in existing - kept:
        conn.execute("DELETE FROM plan_items WHERE id = ?", (rid,))
    conn.commit()


def list_scenarios(conn, cid):
    df = db.read_df(conn, "SELECT * FROM plan_scenarios WHERE collection_id = ? ORDER BY id", (cid,))
    if not df.empty:
        df["settings"] = df["settings"].map(lambda s: {**DEFAULT_SETTINGS, **_loads(s, {})})
        df["overrides"] = df["overrides"].map(lambda s: _loads(s, {}))
    return df


def save_scenario(conn, cid, name, settings, overrides, notes=""):
    conn.execute("INSERT INTO plan_scenarios (collection_id, name, settings, overrides, notes, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                 (cid, name, json.dumps(settings), json.dumps(overrides), notes, db.now_iso()))
    conn.commit()


def delete_scenario(conn, sid):
    conn.execute("DELETE FROM plan_scenarios WHERE id = ?", (sid,))
    conn.commit()


# --- prices ------------------------------------------------------------------------

def parse_tiers(text):
    """'7: 49.90; 26: 47.90' -> [(7, 49.9), (26, 47.9)] (minimum quantity, unit price), sorted by quantity."""
    if text is None or (not isinstance(text, str) and pd.isna(text)):
        return []
    out = []
    for part in str(text).replace(",", ";").split(";"):
        qty, _, price = part.partition(":")
        try:
            out.append((int(float(qty.strip().lstrip("+"))), float(price.strip().lstrip("£€$"))))
        except ValueError:
            continue
    return sorted(out)


def unit_price_at(base, tiers, qty):
    """Supplier's unit price (in its currency) for an order of qty: the base price unless a price break is reached."""
    price = None if base is None or pd.isna(base) else base
    for min_qty, p in tiers:
        if qty >= min_qty or price is None:  # no base price: the first break is the best guess below it
            price = p
    return price


def next_break(base, tiers, qty):
    """The next price break above qty: (min quantity, unit price) or None."""
    now = unit_price_at(base, tiers, qty)
    for min_qty, p in tiers:
        if qty < min_qty and now is not None and p < now:
            return min_qty, p
    return None


def fx_rate(settings, currency):
    if not currency or currency == "GBP":
        return 1.0
    return float((settings.get("fx") or {}).get(currency) or DEFAULT_SETTINGS["fx"].get(currency, 1.0))


# --- evidence from history -----------------------------------------------------------

def parse_sizes(text):
    if text is None or (not isinstance(text, str) and pd.isna(text)):
        return []
    parts = [p.strip().upper() for p in str(text).replace(";", ",").replace("/", ",").split(",") if p.strip()]
    return ["One size" if p in ("ONE SIZE", "OS", "ONESIZE") else p for p in parts]


def size_rank(size):
    if size in events.SIZES:
        return events.SIZES.index(size)
    if size == "One size":
        return -1  # first, so one-size items (caps, foulards) are easy to see
    return 100 + (int(size) if str(size).isdigit() else 0)


def evidence(conn, ledger, today=None, soldout_uplift=0.25):
    """Per-variant demand evidence plus the business-level facts needed to plan."""
    today = pd.Timestamp(today or date.today()).normalize()
    lines = events.enrich(ledger.lines)
    ev_orders = db.read_df(conn, "SELECT order_id, event_id FROM event_orders")
    event_set = set(ev_orders["order_id"])
    n_events = int(ev_orders["event_id"].nunique()) if not ev_orders.empty else 0
    lines["channel"] = lines["order_id"].isin(event_set).map({True: "event", False: "online"})

    rl = ledger.refund_lines
    returned = pd.DataFrame(columns=["variant_id", "channel", "quantity"])
    if not rl.empty:
        returned = rl.assign(channel=rl["order_id"].isin(event_set).map({True: "event", False: "online"}))

    v = db.read_df(conn, "SELECT variant_id, product_id, product_title, variant_title, sku, price, inventory_quantity, "
                         "product_status FROM shopify_variants")
    v = v[~v["product_title"].fillna("").str.strip().str.lower().isin(config.EXCLUDE_PRODUCTS)].copy()
    v = costs.unit_costs_for_lines(conn, v.assign(date=today))
    v["size"] = v["variant_title"].map(events.size_of)
    v["category"] = v["product_title"].map(events.category_of)
    v["colour_family"] = [events.colour_of(t, c) for t, c in zip(v["product_title"], v["category"])]
    v["on_hand"] = v["inventory_quantity"].fillna(0).clip(lower=0).astype(int)

    def units(channel):
        sold = lines[lines["channel"] == channel].groupby("variant_id")["quantity"].sum()
        back = returned[returned["channel"] == channel].groupby("variant_id")["quantity"].sum() if len(returned) else pd.Series(dtype=float)
        return sold.sub(back, fill_value=0).clip(lower=0)

    v["online_units"] = v["variant_id"].map(units("online")).fillna(0)
    v["event_units"] = v["variant_id"].map(units("event")).fillna(0)
    v["units"] = v["online_units"] + v["event_units"]
    agg = lines.groupby("variant_id").agg(first_sale=("date", "min"), last_sale=("date", "max"),
                                          gross=("gross", "sum"), discount=("discount", "sum"))
    last_channel = lines.sort_values("date").groupby("variant_id")["channel"].last()
    v = v.join(agg, on="variant_id")
    v["last_channel"] = v["variant_id"].map(last_channel)
    launch = v.groupby("product_id")["first_sale"].transform("min")  # product launch ~ its first sale
    v["sold_out"] = (v["on_hand"] == 0) & (v["units"] > 0)

    end = v["last_sale"].where(v["sold_out"], today)
    window = ((today - launch).dt.days + 1).where(launch.notna())
    in_stock = ((end - launch).dt.days + 1).where(launch.notna())
    # selling out early raises the daily rate, but never more than doubles it (small numbers are noisy)
    v["in_stock_days"] = pd.concat([in_stock, window / 2], axis=1).max(axis=1).clip(lower=14)
    v["online_rate"] = (v["online_units"] / v["in_stock_days"]).fillna(0)  # units per day while in stock
    v["soldout_at_event"] = v["sold_out"] & (v["last_channel"] == "event")
    uplift = 1 + soldout_uplift * v["soldout_at_event"]
    v["event_per_event"] = (v["event_units"] / n_events * uplift) if n_events else 0.0

    gross, disc = lines["gross"].sum(), lines["discount"].sum()
    facts = {
        "n_events": n_events, "first_date": lines["date"].min(), "last_date": lines["date"].max(),
        "online_units": int(lines.loc[lines["channel"] == "online", "quantity"].sum()),
        "event_units": int(lines.loc[lines["channel"] == "event", "quantity"].sum()),
        "variants_sold": int((v["units"] > 0).sum()), "variants_sold_out": int(v["sold_out"].sum()),
        "returns_units": int(returned["quantity"].sum()) if len(returned) else 0,
        "discount_rate": disc / gross if gross else 0.0, "today": today,
    }
    return {"variants": v, "facts": facts}


def limitations(ev):
    f = ev["facts"]
    notes = []
    total = f["online_units"] + f["event_units"]
    if pd.notna(f["first_date"]):
        months = max(1, round((f["last_date"] - f["first_date"]).days / 30))
        notes.append(f"Sales history covers about {months} months ({f['first_date']:%b %Y} – {f['last_date']:%b %Y}): "
                     f"{total} units in total. Treat every forecast as a rough guide, not a precise number.")
    if f["event_units"] and total:
        notes.append(f"{f['event_units'] / total:.0%} of units came from {f['n_events']} pop-up"
                     f"{'s' if f['n_events'] != 1 else ''}, so pop-up demand rests on very few data points. "
                     "Set each planned pop-up's size relative to the last one.")
    if f["variants_sold_out"]:
        notes.append(f"{f['variants_sold_out']} of {f['variants_sold']} sizes that sold are now sold out, so their true "
                     "demand was probably higher. Online rates allow for time out of stock, and sizes that sold out at a "
                     "pop-up get the sold-out uplift.")
    notes.append("Only summer/autumn sales exist, so winter demand (knitwear, jackets) can't be measured. Use the "
                 "type emphasis or your own numbers on the Demand tab.")
    return notes


def type_history(ev, product_type):
    """The history category used for a planning type: its own if it has sales, else the closest that has."""
    v = ev["variants"]
    sold = set(v.loc[v["units"] > 0, "category"])
    for cat in HISTORY_FOR.get(product_type, [product_type]):
        if cat in sold:
            return cat
    return None


def type_demand(ev, types, period_days, popup_scale, settings):
    """Expected units per product type for the collection, with where the number came from."""
    v = ev["variants"]
    emph = settings.get("type_emphasis") or {}
    manual = settings.get("type_demand") or {}
    rows = []
    for t in types:
        cat = type_history(ev, t)
        pool = v[v["category"] == cat] if cat else v.iloc[0:0]
        per_popup = float(pool["event_per_event"].sum())
        online_per_30 = float(pool["online_rate"].sum() * 30)
        hist_units = float(pool["units"].sum())
        if cat is None:
            conf, source = "No evidence", "No history: enter your own estimate"
        elif cat != t:
            conf, source = "Low", f"Borrowed from {cat}"
        else:
            conf = "High" if hist_units >= 15 else "Medium" if hist_units >= 5 else "Low"
            source = f"{cat} sales"
        m = manual.get(t) or {}
        if m.get("per_popup") is not None or m.get("online_per_30") is not None:
            per_popup = float(m["per_popup"]) if m.get("per_popup") is not None else per_popup
            online_per_30 = float(m["online_per_30"]) if m.get("online_per_30") is not None else online_per_30
            conf, source = "Your estimate", "Your estimate"
        e = float(emph.get(t, 1.0))
        popups = per_popup * popup_scale * e
        online = online_per_30 / 30 * period_days * e
        sold_out = sorted(set(pool.loc[pool["soldout_at_event"], "size"]), key=size_rank) if len(pool) else []
        rows.append({"product_type": t, "history": cat, "source": source, "confidence": conf,
                     "hist_popup_units": float(pool["event_units"].sum()), "hist_online_units": float(pool["online_units"].sum()),
                     "per_popup": per_popup, "online_per_30": online_per_30, "emphasis": e,
                     "expected_popups": popups, "expected_online": online, "expected": popups + online,
                     "on_hand": int(pool["on_hand"].sum()), "price": _typical_price(pool),
                     "discount_rate": (pool["discount"].sum() / pool["gross"].sum()) if pool["gross"].sum() > 0 else None,
                     "sold_out_sizes": ", ".join(sold_out)})
    return pd.DataFrame(rows)


def _typical_price(pool):
    sold = pool[pool["units"] > 0]
    p = (sold if len(sold) else pool)["price"].dropna()
    return float(p.median()) if len(p) else None


def size_weights(ev, sizes, category, period_days, popup_scale):
    """Split units across sizes: the history type's size curve, softened with the overall curve."""
    if sizes == ["One size"] or len(sizes) <= 1:
        return pd.Series(1.0, index=sizes or ["One size"])
    v = ev["variants"]
    demand = v["online_rate"] * period_days + v["event_per_event"] * popup_scale
    sized = v["size"] != "One size"
    overall = demand[sized].groupby(v.loc[sized, "size"]).sum().reindex(sizes).fillna(0)
    overall = overall / overall.sum() if overall.sum() > 0 else pd.Series(1.0 / len(sizes), index=sizes)
    cat = demand[v["category"] == category].groupby(v["size"]).sum().reindex(sizes).fillna(0) if category else overall * 0
    # the type's own curve, softened with the overall curve so a size that hasn't sold yet still gets a share
    w = 0.6 * cat / cat.sum() + 0.4 * overall if cat.sum() > 0 else overall
    w = w.where(w > 0, 1.0 / (len(sizes) * 20))
    return w / w.sum()


# --- the plan -----------------------------------------------------------------------

def _round_alloc(weights, total):
    """Whole units across sizes that add up to total (largest remainder)."""
    total = int(max(total, 0))
    raw = weights * total
    alloc = raw.apply(math.floor).astype(int)
    for idx in (raw - alloc).sort_values(ascending=False).index[: total - int(alloc.sum())]:
        alloc[idx] += 1
    return alloc


def _num(v, default=None):
    return default if v is None or (not isinstance(v, str) and pd.isna(v)) else float(v)


def popup_scale(settings):
    return sum(float(p.get("scale") or 0) for p in settings.get("popups") or [])


def build(ev, items, collection, settings=None, overrides=None):
    """Quantities per colourway and size, with cost (price breaks, FX, customising), outcomes, risks and reasoning."""
    settings = {**DEFAULT_SETTINGS, **(settings or collection["settings"])}
    overrides = overrides if overrides is not None else collection["overrides"]
    start, end = pd.Timestamp(collection["start_date"]), pd.Timestamp(collection["end_date"])
    period_days = max((end - start).days + 1, 1)
    scale_popups = popup_scale(settings)
    base_discount = ev["facts"]["discount_rate"]

    live = items[items["name"].fillna("").str.strip().ne("") & items["status"].ne("Dropped")].copy()
    live["product_type"] = live["product_type"].fillna("Other")
    types = list(dict.fromkeys(live["product_type"]))
    td = type_demand(ev, types, period_days, scale_popups, settings).set_index("product_type") if types else pd.DataFrame()
    mode = settings.get("mode", "draft")
    # demand is shared by the colourways being bought: with your own quantities, a colourway set to 0 is left out
    live["w"] = live["emphasis"].fillna(1.0)
    if mode == "draft":
        live.loc[live["draft_units"].fillna(1) <= 0, "w"] = 0.0
    weight_sum = live.groupby("product_type")["w"].sum()

    rows = []
    for it in live.to_dict("records"):
        t = it["product_type"]
        d = td.loc[t]
        assumed = []
        sizes = parse_sizes(it.get("sizes"))
        if not sizes:
            sizes = DEFAULT_SIZES.get(t, ["One size"])
            assumed.append("sizes")
        fx = fx_rate(settings, it.get("currency"))
        tiers = parse_tiers(it.get("tiers"))
        base = _num(it.get("unit_cost"))
        if base is None and tiers:
            base = tiers[0][1]
        custom = _num(it.get("custom_cost"), 0.0)
        price = _num(it.get("price"))
        if price is None:
            typical = d["price"] if pd.notna(d["price"]) else None
            landed_guess = (base or 0) * fx + custom
            price = typical if typical and landed_guess < typical * 0.6 else _round5(landed_guess * float(settings["markup"]))
            assumed.append("price")
        share = it["w"] / weight_sum[t] if weight_sum[t] else 0
        disc = d["discount_rate"] if pd.notna(d["discount_rate"]) else base_discount
        rows.append({
            "id": int(it["id"]) if it.get("id") is not None and not pd.isna(it.get("id")) else None,
            "name": it["name"], "colour": it.get("colour") or "", "product_type": t, "supplier": it.get("supplier") or "",
            "sizes": sizes, "currency": it.get("currency") or "GBP", "fx": fx, "base": base, "tiers": tiers, "custom": custom,
            "moq": _num(it.get("moq")), "sample": _num(it.get("sample_cost"), 0.0), "lead_weeks": _num(it.get("lead_weeks")),
            "price": price, "net_price": price * (1 - disc), "discount_rate": disc, "draft": _num(it.get("draft_units")),
            "share": share, "expected": d["expected"] * share, "confidence": d["confidence"], "source": d["source"],
            "weights": size_weights(ev, sizes, d["history"], period_days, scale_popups),
            "assumed": assumed, "notes": it.get("notes"), "status": it.get("status"),
            "sold_out_sizes": d["sold_out_sizes"], "label": _label(it),
        })

    expected_value = sum(r["expected"] * r["net_price"] for r in rows)
    notes = []
    samples = sum(r["sample"] for r in rows)
    if mode == "target" and settings.get("target_revenue"):
        scale = settings["target_revenue"] / expected_value if expected_value else 0.0
        if not expected_value and rows:
            notes.append("No product type has sales history or an estimate to scale to the target. Add estimates on the Demand tab.")
    elif mode == "budget" and settings.get("budget"):
        stock_budget = float(settings["budget"]) - samples
        scale, fits = _fit_budget(rows, settings, overrides, stock_budget)
        if not fits:
            notes.append("Even the smallest order (minimum orders, minimum per size and the sizes you've typed) costs more "
                         "than the budget left after samples. Drop products, lower minimums or turn off minimum-order rounding.")
    elif mode == "evidence":
        scale = float(settings.get("ambition") or 1.0)
    else:
        mode, scale = "draft", 1.0
        missing = [r["label"] for r in rows if r["draft"] is None]
        if missing:
            notes.append(f"No quantity of your own for {', '.join(missing)}, so they use what demand supports.")

    size_rows, item_rows = [], []
    for r in rows:
        rec, below_moq = _recommend(r, scale, settings, mode)
        band = BANDS.get(r["confidence"], 0.6)
        item_sizes = []
        for size in r["sizes"]:
            key = f"{r['id']}|{size}"
            final = int(overrides[key]) if key in overrides and overrides[key] is not None else int(rec.get(size, 0))
            dem = r["expected"] * r["weights"].get(size, 0)
            item_sizes.append({
                "item_id": r["id"], "name": r["label"], "size": size, "expected_demand": dem,
                "recommended": int(rec.get(size, 0)), "final": final, "overridden": key in overrides,
                "sold_low": min(final, dem * (1 - band)), "sold_mid": min(final, dem), "sold_high": min(final, dem * (1 + band)),
            })
        size_rows.extend(item_sizes)
        s = pd.DataFrame(item_sizes)
        buy = int(s["final"].sum()) if len(s) else 0
        blank = unit_price_at(r["base"], r["tiers"], buy)
        unit = (blank or 0) * r["fx"] + r["custom"]
        nb = next_break(r["base"], r["tiers"], buy) if buy else None
        res = {
            "id": r["id"], "label": r["label"], "name": r["name"], "colour": r["colour"], "product_type": r["product_type"],
            "supplier": r["supplier"], "sizes": ", ".join(r["sizes"]), "confidence": r["confidence"], "source": r["source"],
            "currency": r["currency"], "blank_price": blank, "unit_cost": unit, "custom_cost": r["custom"], "price": r["price"],
            "net_price": r["net_price"], "expected_demand": r["expected"], "draft": r["draft"],
            "recommended": int(s["recommended"].sum()) if len(s) else 0, "buy": buy, "stock_cost": buy * unit,
            "sample_cost": r["sample"], "cash": buy * unit + r["sample"], "lead_weeks": r["lead_weeks"],
            "sold_low": s["sold_low"].sum() if len(s) else 0, "sold_mid": s["sold_mid"].sum() if len(s) else 0,
            "sold_high": s["sold_high"].sum() if len(s) else 0, "potential_revenue": buy * r["net_price"],
            "margin_pct": ((r["price"] - unit) / r["price"]) if r["price"] else None, "moq": r["moq"],
            "below_moq": below_moq, "assumed": r["assumed"], "overridden": bool(len(s) and s["overridden"].any()),
            "next_break": nb,
        }
        for case in ("low", "mid", "high"):
            res[f"revenue_{case}"] = res[f"sold_{case}"] * r["net_price"]
            res[f"unsold_{case}_cost"] = (buy - res[f"sold_{case}"]) * unit
        res["gross_profit_mid"] = res["revenue_mid"] - res["sold_mid"] * unit
        res["sell_through_mid"] = res["sold_mid"] / buy if buy else None
        res["break_even_sell_through"] = (res["cash"] / (buy * r["net_price"])) if buy and r["net_price"] else None
        res["flags"] = _flags(r, res, s)
        res["reasoning"] = _reasoning(r, res, scale, settings, mode)
        item_rows.append(res)

    bought = [r for r, res in zip(rows, item_rows) if res["buy"]]
    no_custom = [r for r in bought if not r["custom"]]
    if no_custom:
        notes.append(f"{len(no_custom)} of {len(bought)} colourways have no customising cost (embroidery, labels, "
                     "relabelling), so their landed cost and cash needed are understated. Add it per unit on the line-up.")
    priced = [r["label"] for r in bought if "price" in r["assumed"]]
    if priced:
        notes.append(f"{len(priced)} colourways have no retail price, so one is assumed (your usual price for the type, "
                     "or cost × markup). Set real prices on the line-up for sales and margin to mean much.")
    items_out = pd.DataFrame(item_rows)
    sizes_out = pd.DataFrame(size_rows)
    totals = _totals(items_out, expected_value, scale, mode, settings)
    return {"items": items_out, "sizes": sizes_out, "totals": totals, "scale": scale, "mode": mode, "types": td.reset_index() if len(td) else td,
            "period_days": period_days, "popup_scale": scale_popups, "notes": notes}


def _label(it):
    colour = str(it.get("colour") or "").strip()
    return f"{it['name']} · {colour}" if colour else str(it["name"])


def _round5(v):
    return float(max(5, round(v / 5) * 5))


def _recommend(r, scale, settings, mode):
    """Units per size: your own total (draft mode) or expected demand x plan scale x safety stock."""
    if mode == "draft" and r["draft"] is not None:
        total = int(round(r["draft"]))
    else:
        total = int(round(r["expected"] * scale * settings["cover"]))
    rec = _round_alloc(r["weights"], total)
    if mode != "draft" and settings.get("min_per_size") and total > 0 and len(r["sizes"]) > 1:
        rec = rec.clip(lower=int(settings["min_per_size"]))
    below_moq = False
    if r["moq"] and 0 < rec.sum() < r["moq"]:
        below_moq = True
        if settings.get("moq_round") and mode != "draft":
            rec = _round_alloc(r["weights"], int(r["moq"]))
    return rec, below_moq


def _plan_cost(rows, scale, settings, overrides, mode="budget"):
    total = 0.0
    for r in rows:
        rec, _ = _recommend(r, scale, settings, mode)
        qty = sum(overrides.get(f"{r['id']}|{s}", rec.get(s, 0)) or 0 for s in r["sizes"])
        total += qty * ((unit_price_at(r["base"], r["tiers"], qty) or 0) * r["fx"] + r["custom"])
    return total


def _fit_budget(rows, settings, overrides, budget):
    """Largest scale whose actual order (after rounding, minimums, price breaks and your sizes) fits the budget."""
    if _plan_cost(rows, 0.0, settings, overrides) > budget:
        return 0.0, False
    lo, hi = 0.0, 1.0
    while _plan_cost(rows, hi, settings, overrides) <= budget and hi < 100:
        lo, hi = hi, hi * 2
    for _ in range(40):
        mid = (lo + hi) / 2
        if _plan_cost(rows, mid, settings, overrides) <= budget:
            lo = mid
        else:
            hi = mid
    return lo, True


def _flags(r, res, s):
    """What to look at for one colourway (missing lead times and customising costs are reported once, not per row)."""
    flags = []
    if not res["buy"]:
        return flags
    if r["confidence"] in ("Low", "No evidence"):
        flags.append("little evidence")
    if res["sell_through_mid"] is not None and res["sell_through_mid"] < 0.6:
        flags.append(f"expect only {res['sell_through_mid']:.0%} to sell")
    if res["below_moq"]:
        flags.append(f"below minimum order ({int(res['moq'])})")
    if res["next_break"]:
        q, p = res["next_break"]
        extra = q - res["buy"]
        if extra <= max(3, res["buy"] * 0.5):
            flags.append(f"{extra} more → {_money(p, r['currency'])} each")
    if not r["supplier"] or "not set" in r["supplier"].lower() or "find" in r["supplier"].lower():
        flags.append("no supplier yet")
    if "price" in r["assumed"]:
        flags.append("assumed price")
    if len(s) and (s["final"] < s["expected_demand"] * 0.8).any():
        flags.append("may sell out: " + ", ".join(s.loc[s["final"] < s["expected_demand"] * 0.8, "size"]))
    return flags


def _money(v, currency):
    return {"EUR": "€", "USD": "$"}.get(currency, "£") + f"{v:,.2f}"


def _reasoning(r, res, scale, settings, mode):
    parts = [f"{r['product_type']} demand: {r['source'].lower()}; this colourway gets {r['share']:.0%} of it "
             f"→ {r['expected']:.1f} units expected."]
    if mode == "draft" and r["draft"] is not None:
        parts.append(f"You plan {int(r['draft'])}, split by size.")
    elif mode == "draft":
        parts.append("No quantity of your own, so it buys for expected demand plus safety stock.")
    else:
        if abs(scale - 1) > 1e-9:
            parts.append(f"Plan buys for ×{scale:.2f} of demand"
                         + (" to support the target." if mode == "target" else " to fit the budget." if mode == "budget" else "."))
        parts.append(f"Plus {settings['cover'] - 1:.0%} safety stock → {res['recommended']} recommended.")
    if res["blank_price"] is not None:
        parts.append(f"At {res['buy']} units the supplier price is {_money(res['blank_price'], r['currency'])}"
                     + (f" (×{r['fx']:.4f} to £)" if r["currency"] != "GBP" else "")
                     + (f" + £{r['custom']:.2f} customising" if r["custom"] else "") + f" = £{res['unit_cost']:.2f} landed.")
    if res["overridden"]:
        parts.append(f"You've changed some sizes: final order {res['buy']}.")
    if r["notes"]:
        parts.append(f"Note: {r['notes']}")
    return " ".join(parts)


def _totals(items, expected_value, scale, mode, settings):
    if items.empty:
        return {}
    t = {k: float(items[k].sum()) for k in ("buy", "stock_cost", "sample_cost", "cash", "sold_low", "sold_mid", "sold_high",
                                             "revenue_low", "revenue_mid", "revenue_high", "potential_revenue",
                                             "gross_profit_mid", "unsold_low_cost", "unsold_mid_cost", "expected_demand")}
    t["sell_through_mid"] = t["sold_mid"] / t["buy"] if t["buy"] else None
    t["break_even_sell_through"] = t["cash"] / t["potential_revenue"] if t["potential_revenue"] else None
    t["gross_margin_mid"] = t["gross_profit_mid"] / t["revenue_mid"] if t["revenue_mid"] else None
    t["expected_value_unconstrained"] = expected_value
    t["scale"], t["mode"] = scale, mode
    t["target_revenue"] = settings.get("target_revenue")
    t["budget"] = settings.get("budget")
    t["low_confidence_cash"] = float(items.loc[items["confidence"].isin(["Low", "No evidence"]), "stock_cost"].sum())
    return t


# --- timeline ----------------------------------------------------------------------------

def timeline(plan, settings, today=None):
    """Per supplier: what's being bought, the longest lead time and the date to order by for the first pop-up."""
    today = pd.Timestamp(today or date.today()).normalize()
    it = plan["items"]
    if it.empty:
        return pd.DataFrame()
    dates = [pd.Timestamp(p["date"]) for p in settings.get("popups") or [] if p.get("date")]
    first = min(dates) if dates else None
    buffer = float(settings.get("custom_buffer_weeks") or 0)
    rows = []
    for supplier, g in it[it["buy"] > 0].assign(supplier=it["supplier"].replace("", "Supplier not set")).groupby("supplier"):
        lead = g["lead_weeks"].max() if g["lead_weeks"].notna().any() else None
        order_by = first - pd.Timedelta(weeks=lead + buffer) if first is not None and lead is not None else None
        if order_by is None:
            status = "Add lead time" if lead is None else "Add pop-up dates"
        elif order_by < today:
            status = "Late"
        elif order_by - today <= pd.Timedelta(days=14):
            status = "Order within 2 weeks"
        else:
            status = "On track"
        rows.append({"supplier": supplier, "products": ", ".join(g["label"]), "units": int(g["buy"].sum()),
                     "cash": float(g["cash"].sum()), "lead_weeks": lead, "order_by": order_by, "status": status,
                     "missing_lead": int(g["lead_weeks"].isna().sum())})
    return pd.DataFrame(rows).sort_values("order_by", na_position="last")
