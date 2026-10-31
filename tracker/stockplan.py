"""Stock planner: the shared buying plan lives in the database; the sales history behind it is worked out live.

The plan (line-up, quantities, prices, demand settings, notes) is one JSON document in `stock_plans`, edited by the
Stock planner page and saved with a version number. Product photos are kept separately in `stock_plan_images`.
Nothing about the line-up is in the code: load it once with `python sync.py stock-plan-import`.

History per demand group (products customers choose between share one pool, e.g. tees + long sleeves):
- lastPopup: units sold at past pop-ups; perPopup: the same with a 25% allowance for sizes that sold out there
- online30: online units per 30 days since the type first sold
- histProducts: designs that have sold; old: designs still in stock (name, units)
"""
import json

import pandas as pd

from . import collection, db, events

PLAN_ID = "winter-2027"
DEFAULT_ID = "winter-2027-default"
# planner group -> the Shopify product types (events.category_of) that make up its history
GROUPS = {
    "Tees": ["T-Shirt", "Long Sleeve"],
    "Hoodies & knitwear": ["Hoodie"],
    "Jackets": ["Jacket"],
    "Jeans": ["Jeans"],
    "Caps": ["Cap"],
}


def history(conn, ledger):
    """Everything the planner needs from past sales, worked out from the current data."""
    ev = collection.evidence(conn, ledger, soldout_uplift=0.25)
    v, f = ev["variants"], ev["facts"]
    lines = events.enrich(ledger.lines)
    event_orders = set(db.read_df(conn, "SELECT order_id FROM event_orders")["order_id"])
    popup = lines[lines["order_id"].isin(event_orders)]
    online = lines[~lines["order_id"].isin(event_orders)]
    today = pd.Timestamp(f["today"])

    def per_type(t):
        pool = v[v["category"] == t]
        prods = pool.groupby("product_title").agg(units=("units", "sum"), stock=("on_hand", "sum"))
        tl = lines[lines["category"] == t]
        days = max((today - tl["date"].min().normalize()).days, 30) if not tl.empty else 30
        return {
            "lastPopup": int(popup.loc[popup["category"] == t, "quantity"].sum()),
            "perPopup": float(pool["event_per_event"].sum()),
            "online30": float(online.loc[online["category"] == t, "quantity"].sum()) / days * 30 if not tl.empty else 0.0,
            "histProducts": int((prods["units"] > 0).sum()),
            "old": [{"name": n, "units": int(r.stock)} for n, r in prods.iterrows() if r.stock > 0],
        }

    groups = {}
    for group, types in GROUPS.items():
        parts = {t: per_type(t) for t in types}
        label = " + ".join(f"{p['lastPopup']} {t.lower()}{'s' if p['lastPopup'] != 1 else ''}" for t, p in parts.items())
        groups[group] = {
            "label": f"last pop-up: {label}",
            "lastPopup": sum(p["lastPopup"] for p in parts.values()),
            "perPopup": round(sum(p["perPopup"] for p in parts.values()), 2),
            "online30": round(sum(p["online30"] for p in parts.values()), 2),
            "histProducts": sum(p["histProducts"] for p in parts.values()),
            "old": [o for p in parts.values() for o in p["old"]],
        }
    first, last = f["first_date"], f["last_date"]
    return {
        "history": groups,
        "sizeHistory": size_history(v),
        "popupUnits": int(popup["quantity"].sum()),
        "popupOrders": int(popup["order_id"].nunique()),
        "popupNet": round(float(popup["net"].sum()), 2),
        "discount": round(float(f["discount_rate"]), 4),
        "coverage": (f"Expected sales from Padre65 sales {first.day} {first:%b %Y} – {last.day} {last:%b %Y} "
                     f"({len(ledger.orders)} orders, {f['online_units'] + f['event_units']} units, {f['event_units']} at pop-ups), "
                     "projected to the pop-ups + online, Dec 2026 – Mar 2027. Updated with every sync.")
        if pd.notna(first) else "No sales history yet.",
    }


def size_history(v):
    """Per size, for each product type and each past product: [units sold, sold out now (0/1), units in stock].
    The planner turns this into the chance that a sale is each size."""
    def sizes(pool):
        g = pool.groupby("size").agg(units=("units", "sum"), out=("sold_out", "any"), stock=("on_hand", "sum"))
        return {s: [int(r.units), int(bool(r.out)), int(r.stock)] for s, r in g.iterrows() if r.units > 0 or r.stock > 0}

    v = v[v["size"] != "One size"]
    products = {}
    for title, pool in v.groupby("product_title"):
        s = sizes(pool)
        if s:
            products[title] = {"type": pool["category"].iloc[0], "sizes": s}
    return {"types": {t: sizes(pool) for t, pool in v.groupby("category")}, "products": products}


def store_history(conn, ledger):
    """Save the sales history for the web planner (it reads it from the database; refreshed by every sync)."""
    conn.execute("INSERT OR REPLACE INTO stock_plan_history (id, data, updated_at) VALUES (?, ?, ?)",
                 (PLAN_ID, json.dumps(history(conn, ledger), default=lambda o: o.item() if hasattr(o, "item") else str(o)), db.now_iso()))
    conn.commit()


def load(conn, plan_id=PLAN_ID):
    """The saved plan: {plan, version, updated_at, updated_by}, or None if there isn't one yet."""
    row = conn.execute("SELECT plan, version, updated_at, updated_by FROM stock_plans WHERE id = ?", (plan_id,)).fetchone()
    if not row:
        return None
    return {"plan": json.loads(row["plan"]), "version": int(row["version"] or 0),
            "updated_at": row["updated_at"], "updated_by": row["updated_by"]}


def images(conn):
    """Product photos by line id (data URLs)."""
    return {str(r["id"]): r["data"] for r in conn.execute("SELECT id, data FROM stock_plan_images").fetchall()}


def save(conn, plan, base_version, by, plan_id=PLAN_ID):
    """Save the plan as the next version. If someone else saved since `base_version`, their notes are kept too."""
    current = load(conn, plan_id)
    if current and current["version"] != base_version:
        logs = plan.setdefault("logs", {})
        for product, entries in (current["plan"].get("logs") or {}).items():
            mine = logs.setdefault(product, [])
            ids = {e.get("id") for e in mine}
            mine.extend(e for e in entries if e.get("id") not in ids)
    for line in plan.get("lines", []):
        line.pop("img", None)  # photos are stored separately
    version = (current["version"] if current else 0) + 1
    conn.execute("INSERT OR REPLACE INTO stock_plans (id, plan, version, updated_at, updated_by) VALUES (?, ?, ?, ?, ?)",
                 (plan_id, json.dumps(plan), version, db.now_iso(), by))
    conn.commit()
    return version


def import_plan(conn, plan, by="import", replace_live=False):
    """Load a plan (a saved planner file or the starting line-up). Sets the starting copy; the live plan only if
    there isn't one yet, or replace_live is set. Photos found on the lines are stored separately."""
    plan = json.loads(json.dumps(plan))
    pics = {str(line["id"]): line.pop("img") for line in plan.get("lines", []) if line.get("img")}
    for line_id, data in pics.items():
        conn.execute("INSERT OR REPLACE INTO stock_plan_images (id, data) VALUES (?, ?)", (line_id, data))
    for key in ("shareId", "sharedAt", "history", "popupUnits", "popupOrders", "popupNet", "coverage", "selected", "sizeHistory"):
        plan.pop(key, None)
    save(conn, json.loads(json.dumps(plan)), None, by, DEFAULT_ID)
    if replace_live or load(conn) is None:
        save(conn, plan, None, by)
    conn.commit()
    return len(plan.get("lines", [])), len(pics)
