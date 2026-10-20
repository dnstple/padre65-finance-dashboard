"""Collection & stock planning: what to buy for an upcoming collection, how many of each size, and what it implies.

Two separate things are kept apart throughout:
- the EXPECTATION: what history suggests the collection will sell (evidence x your emphasis), with a low/high range;
- the PLAN: how much stock to buy, which can be scaled up to support a revenue target or down to fit a budget.
Buying for a target does not mean it will sell: outcomes are always measured against the expectation.

Evidence model (per Shopify variant = product colourway x size)
- Online demand: units sold outside events / days the variant was in stock. A size that sold out stops the clock
  at its last sale, so selling out quickly shows up as a higher daily rate (stock-out adjustment).
- Event demand: units sold at pop-ups / number of events. Sizes that sold out at an event get an uplift, because
  they might have sold more.
- Expected units for the selling period = online rate x period days + event demand x planned events.
- Returns are netted off; discounts come from each product's history.
"""
import json
import math
from datetime import date

import pandas as pd

from . import config, costs, db, events

STATUSES = ["Considering", "Likely", "Confirmed", "Dropped"]
PRODUCT_TYPES = ["T-Shirt", "Long Sleeve", "Hoodie", "Sweatshirt", "Tracksuit", "Shorts", "Jacket", "Jeans", "Cap",
                 "Foulard", "Other"]
APPAREL_SIZES = ["XS", "S", "M", "L", "XL"]
DEFAULT_SIZES = {t: APPAREL_SIZES for t in ["T-Shirt", "Long Sleeve", "Hoodie", "Sweatshirt", "Tracksuit", "Shorts", "Jacket"]}
DEFAULT_SIZES.update({"Jeans": ["28", "30", "32", "34", "36"], "Cap": ["One size"], "Foulard": ["One size"], "Other": ["One size"]})
# +/- range around the expectation, by how much evidence there is
BANDS = {"High": 0.25, "Medium": 0.40, "Low": 0.60, "Your estimate": 0.50, "No evidence": 0.75}
SIZE_SHRINK = 6  # weight of the category size curve vs an item's own sales when splitting by size
MODES = {"evidence": "History × ambition", "target": "Hit a revenue target", "budget": "Fit a stock budget"}
DEFAULT_SETTINGS = {
    "mode": "evidence", "ambition": 1.0, "target_revenue": None, "budget": None, "cover": 1.2,
    "min_per_size": 1, "moq_round": True, "soldout_uplift": 0.25, "category_emphasis": {},
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


def create_collection(conn, name, season, start_date, end_date, planned_events, notes=""):
    cur = conn.execute("""INSERT INTO plan_collections (name, season, start_date, end_date, planned_events, notes,
                          settings, overrides, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                       (name, season, str(start_date), str(end_date), int(planned_events or 0), notes,
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


ITEM_FIELDS = ["name", "product_type", "colour", "sizes", "unit_cost", "price", "comparable", "manual_units",
               "emphasis", "moq", "status", "notes"]


def load_items(conn, cid):
    df = db.read_df(conn, f"SELECT id, {', '.join(ITEM_FIELDS)} FROM plan_items WHERE collection_id = ? ORDER BY id", (cid,))
    if df.empty:
        df = pd.DataFrame(columns=["id"] + ITEM_FIELDS)
    for col in ("unit_cost", "price", "manual_units", "emphasis", "moq"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in ("name", "product_type", "colour", "sizes", "comparable", "status", "notes"):
        df[col] = df[col].astype("object").where(df[col].notna(), None)
    return df


def _clean(v):
    if v is None or (not isinstance(v, str) and pd.isna(v)):
        return None
    return v.item() if hasattr(v, "item") else v


def add_item(conn, cid, **values):
    values = {"emphasis": 1.0, "status": "Considering", **values}
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
    v["style"] = v["product_title"].map(lambda t: events.split_title(t)[0])
    v["colour"] = v["product_title"].map(lambda t: events.split_title(t)[1])
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
    v["window_days"] = window
    # selling out early raises the daily rate, but never more than doubles it (small numbers are noisy)
    v["in_stock_days"] = pd.concat([in_stock, window / 2], axis=1).max(axis=1).clip(lower=14)
    v["stockout_factor"] = (v["window_days"] / v["in_stock_days"]).where(v["sold_out"], 1.0).fillna(1.0)
    v["online_rate"] = (v["online_units"] / v["in_stock_days"]).fillna(0)  # units per day while in stock
    uplift = 1 + soldout_uplift * (v["sold_out"] & (v["last_channel"] == "event"))
    v["event_per_event"] = (v["event_units"] / n_events * uplift) if n_events else 0.0
    v["soldout_at_event"] = v["sold_out"] & (v["last_channel"] == "event")

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
        share = f["event_units"] / total
        notes.append(f"{share:.0%} of units came from {f['n_events']} pop-up event{'s' if f['n_events'] != 1 else ''}. "
                     "Event demand is estimated per event, so the plan depends heavily on how many events you run.")
    if f["variants_sold_out"]:
        notes.append(f"{f['variants_sold_out']} of {f['variants_sold']} sizes that sold are now sold out, so their true "
                     "demand was probably higher. Online rates are adjusted for time out of stock, and sizes that sold out "
                     "at an event get the sold-out uplift.")
    notes.append("Only one season of sales exists, so seasonality (e.g. winter vs summer demand) can't be measured. "
                 "Use emphasis to adjust for it.")
    if not f["returns_units"]:
        notes.append("No returns recorded yet, so no allowance is made for them.")
    return notes


def products_evidence(ev, period_days=90, planned_events=1):
    """Per-product summary of what history shows (used for the Evidence tab and to pick comparables)."""
    v = ev["variants"]
    g = v.groupby(["product_id", "product_title", "style", "category", "colour_family"]).agg(
        units=("units", "sum"), online_units=("online_units", "sum"), event_units=("event_units", "sum"),
        online_rate=("online_rate", "sum"), event_per_event=("event_per_event", "sum"),
        sizes=("size", lambda s: ", ".join(sorted(set(s), key=size_rank))),
        sold_out_sizes=("size", lambda s: ""), price=("price", "max"), unit_cost=("unit_cost", "max"),
        on_hand=("on_hand", "sum"), gross=("gross", "sum"), discount=("discount", "sum"),
        first_sale=("first_sale", "min"), status=("product_status", "first")).reset_index()
    so = v[v["sold_out"]].groupby("product_id")["size"].apply(lambda s: ", ".join(sorted(s, key=size_rank)))
    g["sold_out_sizes"] = g["product_id"].map(so).fillna("")
    g["online_per_30"] = g["online_rate"] * 30
    g["expected_period"] = g["online_rate"] * period_days + g["event_per_event"] * planned_events
    g["discount_rate"] = (g["discount"] / g["gross"]).where(g["gross"] > 0)
    g["confidence"] = g["units"].map(confidence_for)
    return g.sort_values("units", ascending=False).reset_index(drop=True)


def confidence_for(units):
    if units >= 15:
        return "High"
    if units >= 5:
        return "Medium"
    if units > 0:
        return "Low"
    return "No evidence"


def comparable_options(ev):
    v = ev["variants"]
    sold = v[v["units"] > 0]
    prods = sold.groupby("product_title")["units"].sum().sort_values(ascending=False).index.tolist()
    styles = sold.groupby("style")["units"].sum().sort_values(ascending=False).index.tolist()
    cats = sold.groupby("category")["units"].sum().sort_values(ascending=False).index.tolist()
    return (["Auto"] + [f"Product: {p}" for p in prods] + [f"Style: {s}" for s in styles] +
            [f"Type: {c}" for c in cats] + ["All products average", "My estimate only"])


def auto_comparable(name, product_type, ev):
    """Best available evidence for an item: the same product, the same design, the same type, or everything."""
    v = ev["variants"]
    sold = v[v["units"] > 0]
    title = str(name or "").strip().lower()
    if title and title in set(sold["product_title"].str.lower()):
        return "Product: " + sold.loc[sold["product_title"].str.lower() == title, "product_title"].iloc[0]
    style = events.split_title(name)[0].strip().lower() if name else ""
    if style and style in set(sold["style"].str.lower()):
        return "Style: " + sold.loc[sold["style"].str.lower() == style, "style"].iloc[0]
    if product_type and product_type in set(sold["category"]):
        return f"Type: {product_type}"
    return "All products average"


def comparable_demand(ev, comparable, period_days, planned_events, category=None):
    """Expected units for ONE product over the period from the chosen comparable, plus its size mix and context."""
    v = ev["variants"].copy()
    v["period_demand"] = v["online_rate"] * period_days + v["event_per_event"] * planned_events
    kind, _, key = comparable.partition(": ")
    if comparable == "All products average":
        pool, per = v[v["units"] > 0], None
    elif kind == "Product":
        pool, per = v[v["product_title"] == key], 1
    elif kind == "Style":
        pool, per = v[(v["style"] == key) & (v["units"] > 0)], None
    elif kind == "Type":
        pool, per = v[(v["category"] == key) & (v["units"] > 0)], None
    else:
        pool, per = v.iloc[0:0], None
    n_products = per or max(pool["product_id"].nunique(), 1)
    units = pool["period_demand"].sum() / n_products
    size_units = pool.groupby("size")["period_demand"].sum() / n_products
    hist_units = pool["units"].sum() / n_products
    gross, disc = pool["gross"].sum(), pool["discount"].sum()
    if kind == "Product":
        conf = confidence_for(pool["units"].sum())
    elif kind == "Style":
        conf = "Medium" if hist_units >= 5 else "Low" if hist_units > 0 else "No evidence"
    else:
        conf = "Low" if len(pool) else "No evidence"
    return {
        "units": float(units), "size_units": size_units, "hist_units": float(hist_units),
        "products": int(n_products), "discount_rate": disc / gross if gross else None,
        "price": float(pool["price"].max()) if len(pool) and pool["price"].notna().any() else None,
        "unit_cost": float(pool["unit_cost"].max()) if len(pool) and pool["unit_cost"].notna().any() else None,
        "sizes": sorted(set(pool["size"]), key=size_rank) if len(pool) else [],
        "sold_out_sizes": sorted(set(pool.loc[pool["sold_out"], "size"]), key=size_rank) if len(pool) else [],
        "confidence": conf, "kind": kind if kind in ("Product", "Style", "Type") else comparable,
        "online_part": float((pool["online_rate"] * period_days).sum() / n_products),
        "event_part": float((pool["event_per_event"] * planned_events).sum() / n_products),
    }


def size_weights(ev, sizes, own_size_units, category, period_days, planned_events):
    """Split an item's units across its sizes: its own history blended with its category's size curve."""
    if sizes == ["One size"] or len(sizes) <= 1:
        return pd.Series(1.0, index=sizes or ["One size"])
    v = ev["variants"]
    demand = v["online_rate"] * period_days + v["event_per_event"] * planned_events
    sized = v["size"] != "One size"
    overall = demand[sized].groupby(v.loc[sized, "size"]).sum().reindex(sizes).fillna(0)
    overall = overall / overall.sum() if overall.sum() > 0 else pd.Series(1.0 / len(sizes), index=sizes)
    cat = demand[v["category"] == category].groupby(v["size"]).sum().reindex(sizes).fillna(0)
    # the type's own curve, softened with the overall curve so a size that hasn't sold yet still gets a share
    cat = 0.7 * cat / cat.sum() + 0.3 * overall if cat.sum() > 0 else overall
    cat = cat.where(cat > 0, 1.0 / (len(sizes) * 20))
    cat = cat / cat.sum()
    own = own_size_units.reindex(sizes).fillna(0) if own_size_units is not None else pd.Series(0.0, index=sizes)
    w = (own + SIZE_SHRINK * cat) / (own.sum() + SIZE_SHRINK)
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


def build(ev, items, collection, settings=None, overrides=None):
    """Recommended and final quantities per item and size, with outcomes, risks and reasoning."""
    settings = {**DEFAULT_SETTINGS, **(settings or collection["settings"])}
    overrides = overrides if overrides is not None else collection["overrides"]
    start, end = pd.Timestamp(collection["start_date"]), pd.Timestamp(collection["end_date"])
    period_days = max((end - start).days + 1, 1)
    planned_events = int(collection.get("planned_events") or 0)
    base_discount = ev["facts"]["discount_rate"]
    cat_emph = settings.get("category_emphasis") or {}

    rows = []
    for it in items.to_dict("records"):
        if not str(it.get("name") or "").strip() or it.get("status") == "Dropped":
            continue
        ptype = it.get("product_type") or "Other"
        comp_choice = it.get("comparable") or "Auto"
        comp_name = auto_comparable(it["name"], ptype, ev) if comp_choice == "Auto" else comp_choice
        comp = comparable_demand(ev, comp_name, period_days, planned_events, ptype)
        assumed = []
        sizes = parse_sizes(it.get("sizes")) or comp["sizes"] or DEFAULT_SIZES.get(ptype, ["One size"])
        if not parse_sizes(it.get("sizes")):
            assumed.append("sizes")
        cost = it.get("unit_cost")
        if cost is None or pd.isna(cost):
            cost = comp["unit_cost"]
            assumed.append("unit cost")
        price = it.get("price")
        if price is None or pd.isna(price):
            price = comp["price"]
            assumed.append("price")
        manual = it.get("manual_units")
        if comp_name == "My estimate only" or (manual is not None and not pd.isna(manual)):
            base_units = float(manual) if manual is not None and not pd.isna(manual) else 0.0
            confidence, basis = ("Your estimate" if base_units else "No evidence"), "your estimate"
        else:
            base_units, confidence = comp["units"], comp["confidence"]
            basis = comp_name
        emphasis = float(it.get("emphasis") if it.get("emphasis") is not None and not pd.isna(it.get("emphasis")) else 1.0)
        emphasis *= float(cat_emph.get(ptype, 1.0))
        expected = base_units * emphasis
        disc = comp["discount_rate"] if comp["discount_rate"] is not None else base_discount
        net_price = (price or 0) * (1 - disc)
        weights = size_weights(ev, sizes, comp["size_units"] if comp_name.startswith(("Product", "Style")) else None,
                               ptype, period_days, planned_events)
        rows.append({"id": int(it["id"]) if it.get("id") is not None and not pd.isna(it.get("id")) else None,
                     "name": it["name"], "product_type": ptype, "colour": it.get("colour"), "sizes": sizes,
                     "unit_cost": cost, "price": price, "net_price": net_price, "discount_rate": disc,
                     "comparable": comp_name, "basis": basis, "confidence": confidence, "base_units": base_units,
                     "emphasis": emphasis, "expected": expected, "weights": weights, "moq": it.get("moq"),
                     "assumed": assumed, "comp": comp, "status": it.get("status"), "notes": it.get("notes")})

    expected_value = sum(r["expected"] * r["net_price"] for r in rows)
    mode = settings.get("mode", "evidence")
    notes = []
    if mode == "target" and settings.get("target_revenue"):
        scale = settings["target_revenue"] / expected_value if expected_value else 0.0
        if not expected_value and rows:
            notes.append("None of these products has sales history or an expected-units estimate to scale up to the "
                         "target. Add 'Your expected units' or a comparable on the Products tab.")
    elif mode == "budget" and settings.get("budget"):
        budget = float(settings["budget"])
        scale, fits = _fit_budget(rows, settings, overrides, budget)
        committed = _plan_cost(rows, 1e-6, settings, overrides)  # MOQs, size minimums and your own quantities
        if committed > budget * 0.25:
            moqs = [r["name"] for r in rows if r["moq"] and not pd.isna(r["moq"]) and settings.get("moq_round")]
            notes.append(f"£{committed:,.0f} of the £{budget:,.0f} budget is taken by minimum orders"
                         + (f" ({', '.join(moqs)})" if moqs else "") + ", minimum sizes and quantities you've set yourself, "
                         "before anything is scaled to demand.")
        if not fits:
            notes.append("Even the smallest plan (MOQs, minimum per size and your own quantities) costs more than "
                         "the budget. Lower MOQs, drop items or turn off MOQ rounding.")
        elif scale < 0.05:
            big = [f"{r['name']} (MOQ {int(r['moq'])} = {r['moq'] * (r['unit_cost'] or 0):,.0f})" for r in rows
                   if r["moq"] and not pd.isna(r["moq"]) and settings.get("moq_round") and r["moq"] * (r["unit_cost"] or 0) > budget * 0.5]
            notes.append("The budget is too small to buy much once minimum orders are included"
                         + (f": {', '.join(big)}" if big else "") + ". Raise the budget, set those items to 0, or turn off MOQ rounding.")
    else:
        mode, scale = "evidence", float(settings.get("ambition") or 1.0)

    size_rows, item_rows = [], []
    for r in rows:
        planned = r["expected"] * scale  # units the plan is buying to support
        rec, below_moq = _recommend(r, planned, settings)
        band = BANDS.get(r["confidence"], 0.6)
        item_sizes = []
        for size in r["sizes"]:
            key = f"{r['id']}|{size}"
            final = int(overrides[key]) if key in overrides and overrides[key] is not None else int(rec.get(size, 0))
            d = r["expected"] * r["weights"].get(size, 0)
            item_sizes.append({
                "item_id": r["id"], "name": r["name"], "size": size, "weight": r["weights"].get(size, 0),
                "expected_demand": d, "recommended": int(rec.get(size, 0)), "final": final,
                "overridden": key in overrides, "sold_low": min(final, d * (1 - band)), "sold_mid": min(final, d),
                "sold_high": min(final, d * (1 + band)),
                "sold_out_before": size in r["comp"]["sold_out_sizes"] and r["basis"] != "your estimate",
            })
        size_rows.extend(item_sizes)
        s = pd.DataFrame(item_sizes)
        buy = int(s["final"].sum()) if len(s) else 0
        cost = r["unit_cost"] or 0
        res = {
            "id": r["id"], "name": r["name"], "product_type": r["product_type"], "colour": r["colour"],
            "sizes": ", ".join(r["sizes"]), "confidence": r["confidence"], "basis": r["basis"],
            "unit_cost": r["unit_cost"], "price": r["price"], "net_price": r["net_price"],
            "expected_demand": r["expected"], "planned_units": planned, "recommended": int(s["recommended"].sum()) if len(s) else 0,
            "buy": buy, "cash": buy * cost,
            "sold_low": s["sold_low"].sum() if len(s) else 0, "sold_mid": s["sold_mid"].sum() if len(s) else 0,
            "sold_high": s["sold_high"].sum() if len(s) else 0,
            "potential_revenue": buy * r["net_price"], "full_price_value": buy * (r["price"] or 0),
            "margin_pct": ((r["price"] - cost) / r["price"]) if r["price"] else None,
            "below_moq": below_moq, "moq": r["moq"], "assumed": r["assumed"], "emphasis": r["emphasis"],
            "overridden": bool(len(s) and s["overridden"].any()),
        }
        for case in ("low", "mid", "high"):
            res[f"revenue_{case}"] = res[f"sold_{case}"] * r["net_price"]
            res[f"unsold_{case}_cost"] = (buy - res[f"sold_{case}"]) * cost
        res["gross_profit_mid"] = res["revenue_mid"] - res["sold_mid"] * cost
        res["sell_through_mid"] = res["sold_mid"] / buy if buy else None
        res["break_even_sell_through"] = (cost / r["net_price"]) if r["net_price"] else None
        res["flags"] = _flags(r, res, s)
        res["reasoning"] = _reasoning(r, res, scale, settings, period_days, planned_events)
        item_rows.append(res)

    items_out = pd.DataFrame(item_rows)
    sizes_out = pd.DataFrame(size_rows)
    totals = _totals(items_out, expected_value, scale, mode, settings)
    return {"items": items_out, "sizes": sizes_out, "totals": totals, "scale": scale, "mode": mode,
            "period_days": period_days, "planned_events": planned_events, "notes": notes}


def _recommend(r, planned, settings):
    """Recommended units per size for an item planned to sell `planned` units."""
    rec = _round_alloc(r["weights"], round(planned * settings["cover"]))
    if settings.get("min_per_size") and planned > 0 and len(r["sizes"]) > 1:
        rec = rec.clip(lower=int(settings["min_per_size"]))
    below_moq = False
    if r["moq"] and not pd.isna(r["moq"]) and 0 < rec.sum() < r["moq"]:
        below_moq = True
        if settings.get("moq_round"):
            rec = _round_alloc(r["weights"], int(r["moq"]))
            if settings.get("min_per_size") and len(r["sizes"]) > 1:
                rec = rec.clip(lower=int(settings["min_per_size"]))
    return rec, below_moq


def _plan_cost(rows, scale, settings, overrides):
    total = 0.0
    for r in rows:
        rec, _ = _recommend(r, r["expected"] * scale, settings)
        for size in r["sizes"]:
            key = f"{r['id']}|{size}"
            qty = overrides[key] if key in overrides and overrides[key] is not None else rec.get(size, 0)
            total += float(qty) * (r["unit_cost"] or 0)
    return total


def _fit_budget(rows, settings, overrides, budget):
    """Largest scale whose actual order (after rounding, minimums, MOQs and your overrides) fits the budget."""
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
    flags = []
    if r["confidence"] in ("Low", "No evidence"):
        flags.append("little evidence")
    if res["buy"] and res["sell_through_mid"] is not None and res["sell_through_mid"] < 0.6:
        flags.append(f"expect only {res['sell_through_mid']:.0%} to sell")
    if res["below_moq"]:
        flags.append(f"below MOQ ({int(res['moq'])})")
    if r["assumed"]:
        flags.append("assumed " + ", ".join(r["assumed"]))
    if len(s) and (s["final"] < s["expected_demand"] * 0.8).any():
        short = ", ".join(s.loc[s["final"] < s["expected_demand"] * 0.8, "size"])
        flags.append(f"may sell out: {short}")
    if not r["unit_cost"]:
        flags.append("no unit cost")
    return flags


def _reasoning(r, res, scale, settings, period_days, planned_events):
    comp = r["comp"]
    parts = []
    if r["basis"] == "your estimate":
        parts.append(f"Uses your estimate of {r['base_units']:.0f} units for the period.")
    elif comp["kind"] == "Product":
        parts.append(f"Based on its own sales: {comp['hist_units']:.0f} units so far.")
    elif comp["kind"] == "Style":
        parts.append(f"New colourway/variant, so based on the {r['comparable'].split(': ', 1)[1]} design: "
                     f"{comp['hist_units']:.1f} units per colourway on average across {comp['products']}.")
    elif comp["kind"] == "Type":
        parts.append(f"No direct history, so based on the average {r['product_type']} "
                     f"({comp['hist_units']:.1f} units per product across {comp['products']}).")
    else:
        parts.append("No comparable product, so based on the average product across the whole range.")
    if r["basis"] != "your estimate":
        parts.append(f"Over {period_days} days that's ≈{comp['online_part']:.1f} online + "
                     f"{comp['event_part']:.1f} at {planned_events} event{'s' if planned_events != 1 else ''} "
                     f"= {r['base_units']:.1f} expected.")
        if comp["sold_out_sizes"]:
            who = "It" if comp["kind"] == "Product" else "Similar products"
            parts.append(f"{who} sold out in {', '.join(comp['sold_out_sizes'])}, so demand there is adjusted up "
                         "(capped at double).")
    if abs(r["emphasis"] - 1) > 1e-9:
        parts.append(f"Your emphasis ×{r['emphasis']:.2f} → expect {r['expected']:.1f}.")
    if abs(scale - 1) > 1e-9:
        parts.append(f"Plan buys for ×{scale:.2f} that ({res['planned_units']:.1f} units)"
                     + (" to support the target." if settings.get("mode") == "target" else
                        " to fit the budget." if settings.get("mode") == "budget" else " (ambition)."))
    parts.append(f"Plus {settings['cover'] - 1:.0%} safety stock, split by size → {res['recommended']} recommended.")
    if res["overridden"]:
        parts.append(f"You've changed some sizes: final order {res['buy']}.")
    if r["notes"]:
        parts.append(f"Your note: {r['notes']}")
    return " ".join(parts)


def _totals(items, expected_value, scale, mode, settings):
    if items.empty:
        return {}
    t = {k: float(items[k].sum()) for k in ("buy", "cash", "sold_low", "sold_mid", "sold_high", "revenue_low",
                                             "revenue_mid", "revenue_high", "potential_revenue", "full_price_value",
                                             "gross_profit_mid", "unsold_low_cost", "unsold_mid_cost", "expected_demand")}
    t["sell_through_mid"] = t["sold_mid"] / t["buy"] if t["buy"] else None
    t["break_even_sell_through"] = t["cash"] / t["potential_revenue"] if t["potential_revenue"] else None
    t["gross_margin_mid"] = t["gross_profit_mid"] / t["revenue_mid"] if t["revenue_mid"] else None
    t["expected_value_unconstrained"] = expected_value
    t["scale"], t["mode"] = scale, mode
    t["target_revenue"] = settings.get("target_revenue")
    t["budget"] = settings.get("budget")
    t["low_confidence_cash"] = float(items.loc[items["confidence"].isin(["Low", "No evidence"]), "cash"].sum())
    return t
