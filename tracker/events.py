"""Events (pop-ups, markets): tag orders and costs to an event and build its sales report."""
import re

import pandas as pd

from . import db

SIZES = ["XXS", "XS", "S", "M", "L", "XL", "XXL"]
POS_SOURCES = ("pos", "quick_sale")

# Checked in order - first match wins
CATEGORY_RULES = [
    ("long sleeve", "Long Sleeve"), ("tracksuit", "Tracksuit"), ("shorts", "Shorts"), ("hoodie", "Hoodie"),
    (r"crew|sweater|sweatshirt|jumper", "Sweatshirt"), ("jacket", "Jacket"), ("jeans", "Jeans"),
    (r"\bcap\b", "Cap"), ("foulard", "Foulard"), (r"\btee\b|t-shirt", "T-Shirt"),
]
COLOUR_RULES = [
    (r"white|cream", "White / Cream"), ("olive", "Olive"), ("navy", "Navy"), ("black", "Black"),
    (r"denim|indigo", "Denim / Indigo"), (r"grey|gray", "Grey"), (r"brown|mocha|siena", "Brown"),
    (r"green|forest|orzola", "Green"), ("camo", "Camo"), ("blue", "Blue"), ("sand", "Sand"), ("yellow", "Yellow"),
]


def split_title(title):
    """'Club Long Sleeve - Vintage White' -> ('Club Long Sleeve', 'Vintage White')."""
    parts = re.split(r"\s+[-–—]\s+", str(title or ""), maxsplit=1)
    return parts[0].strip(), (parts[1].strip() if len(parts) > 1 else "")


def category_of(title):
    t = str(title or "").lower()
    if not t or "custom" in t:
        return "Custom sale"
    for pattern, cat in CATEGORY_RULES:
        if re.search(pattern, t):
            return cat
    return "Other"


def colour_of(title, category):
    style, colour = split_title(title)
    text = (colour or style).lower()
    for pattern, family in COLOUR_RULES:
        if re.search(pattern, text):
            return family
    if category == "Foulard":
        return "Print (Foulard)"
    return "—"


def size_of(variant_title):
    v = str(variant_title or "").strip().upper()
    if v in SIZES or v.isdigit():
        return v
    return "One size"


def enrich(lines):
    lines = lines.copy()
    lines["product_title"] = lines["product_title"].fillna("Custom amount (no product)")
    lines["category"] = lines["product_title"].map(category_of)
    lines["style"] = lines["product_title"].map(lambda t: split_title(t)[0])
    lines["colour_family"] = [colour_of(t, c) for t, c in zip(lines["product_title"], lines["category"])]
    lines["size"] = lines["variant_title"].map(size_of)
    return lines


# --- event management -------------------------------------------------------

def list_events(conn):
    return db.read_df(conn, """
        SELECT e.*, (SELECT COUNT(*) FROM event_orders o WHERE o.event_id = e.id) AS orders
        FROM events e ORDER BY start_date DESC""")


def create_event(conn, name, start_date, end_date, location="", notes="", tag_pos_orders=True):
    cur = conn.execute("INSERT INTO events (name, start_date, end_date, location, notes, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                       (name, str(start_date), str(end_date), location, notes, db.now_iso()))
    event_id = cur.lastrowid
    if tag_pos_orders:
        tag_orders_in_range(conn, event_id, start_date, end_date)
    conn.commit()
    return event_id


def tag_orders_in_range(conn, event_id, start_date, end_date, sources=POS_SOURCES):
    """Tag in-person (POS) orders placed between the dates (UK time) to the event."""
    from .reports import orders
    o = orders(conn)
    sel = o[(o["date"] >= pd.Timestamp(start_date)) & (o["date"] < pd.Timestamp(end_date) + pd.Timedelta(days=1))
            & o["source_name"].isin(sources)]
    for oid in sel["id"]:
        conn.execute("INSERT OR REPLACE INTO event_orders (order_id, event_id) VALUES (?, ?)", (oid, event_id))
    conn.commit()
    return len(sel)


def set_orders(conn, event_id, order_ids):
    conn.execute("DELETE FROM event_orders WHERE event_id = ?", (event_id,))
    for oid in order_ids:
        conn.execute("INSERT OR REPLACE INTO event_orders (order_id, event_id) VALUES (?, ?)", (oid, event_id))
    conn.commit()


def set_costs(conn, event_id, txn_ids):
    conn.execute("DELETE FROM event_costs WHERE event_id = ?", (event_id,))
    for tid in txn_ids:
        conn.execute("INSERT OR REPLACE INTO event_costs (txn_id, event_id) VALUES (?, ?)", (tid, event_id))
    conn.commit()


# --- report -----------------------------------------------------------------

class EventReport:
    def __init__(self, conn, ledger, event_id):
        self.event = dict(conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone())
        order_ids = {r[0] for r in conn.execute("SELECT order_id FROM event_orders WHERE event_id = ?", (event_id,))}
        self.orders = ledger.orders[ledger.orders["id"].isin(order_ids)].copy()
        self.lines = enrich(ledger.lines[ledger.lines["order_id"].isin(order_ids)])
        self.lines = self.lines.merge(self.orders[["id", "date"]].rename(columns={"id": "order_id", "date": "order_date"}),
                                      on="order_id", how="left")
        self.lines["day"] = self.lines["order_date"].dt.strftime("%a %d %b")
        self.orders["day"] = self.orders["date"].dt.strftime("%a %d %b")
        self.orders["hour"] = self.orders["date"].dt.hour
        net = self.lines.groupby("order_id")["net"].sum()
        units = self.lines.groupby("order_id")["quantity"].sum()
        self.orders["net"] = self.orders["id"].map(net).fillna(0)
        self.orders["units"] = self.orders["id"].map(units).fillna(0).astype(int)
        self.orders["discounted"] = self.orders["discounts"] > 0

        fees = db.read_df(conn, "SELECT order_id, fee FROM shopify_balance_txns")
        self.fees = float(fees[fees["order_id"].isin(order_ids)]["fee"].sum()) if not fees.empty else 0.0
        self.fees_estimated = fees.empty
        if self.fees_estimated:
            from . import config
            paid = self.orders[self.orders["total"] > 0]
            self.fees = float((paid["total"] * config.SHOPIFY_FEE_PCT + config.SHOPIFY_FEE_FIXED).sum())

        cost_ids = {r[0] for r in conn.execute("SELECT txn_id FROM event_costs WHERE event_id = ?", (event_id,))}
        self.costs = ledger.cash[ledger.cash["id"].isin(cost_ids)].copy()

    # headline
    def kpis(self):
        o, l = self.orders, self.lines
        gross, disc, net = l["gross"].sum(), l["discount"].sum(), l["net"].sum()
        cogs = l["cogs"].fillna(0).sum()
        event_costs = -self.costs["amount_base"].sum() if not self.costs.empty else 0.0
        return {
            "orders": len(o), "units": int(l["quantity"].sum()), "gross": gross, "discounts": disc, "net": net,
            "aov": net / len(o) if len(o) else 0, "units_per_order": l["quantity"].sum() / len(o) if len(o) else 0,
            "avg_unit_price": net / l["quantity"].sum() if l["quantity"].sum() else 0,
            "discount_rate": disc / gross if gross else 0, "discounted_orders": int(o["discounted"].sum()),
            "multi_item_orders": int((o["units"] > 1).sum()), "distinct_products": l["product_title"].nunique(),
            "cogs": cogs, "gross_profit": net - cogs, "card_fees": self.fees, "event_costs": event_costs,
            "event_profit": net - cogs - self.fees - event_costs,
            "missing_cost_units": int(l.loc[l["unit_cost"].isna() & (l["gross"] > 0), "quantity"].sum()),
        }

    def by_day(self):
        o, l = self.orders, self.lines
        g = l.groupby("day").agg(units=("quantity", "sum"), gross=("gross", "sum"), discounts=("discount", "sum"),
                                 net=("net", "sum"), cogs=("cogs", "sum"))
        od = o.groupby("day").agg(orders=("id", "count"), first=("date", "min"), last=("date", "max"))
        g = od.join(g)
        g["aov"] = g["net"] / g["orders"]
        g["share"] = g["net"] / g["net"].sum()
        g["trading_hrs"] = (g["last"] - g["first"]).dt.total_seconds() / 3600
        g["net_per_hr"] = (g["net"] / g["trading_hrs"]).where(g["trading_hrs"] > 0.5)
        g["gross_profit"] = g["net"] - g["cogs"].fillna(0)
        return g.sort_values("first").reset_index()

    def by(self, key):
        l = self.lines
        g = l.groupby(key).agg(units=("quantity", "sum"), orders=("order_id", "nunique"), gross=("gross", "sum"),
                               discounts=("discount", "sum"), net=("net", "sum"), cogs=("cogs", "sum"),
                               missing_cost=("unit_cost", lambda s: bool(s.isna().any())))
        g["share"] = g["net"] / g["net"].sum()
        g["avg_price"] = g["net"] / g["units"]
        g["gross_profit"] = g["net"] - g["cogs"].fillna(0)
        g["margin"] = (g["gross_profit"] / g["net"]).where(g["net"] > 0)
        return g.sort_values("net", ascending=False).reset_index()

    def size_curve(self):
        l = self.lines[self.lines["size"] != "One size"]
        order = {s: i for i, s in enumerate(SIZES)}
        g = l.groupby("size").agg(units=("quantity", "sum"), net=("net", "sum")).reset_index()
        g["share"] = g["units"] / g["units"].sum()
        g["_o"] = g["size"].map(lambda s: order.get(s, 100 + (int(s) if s.isdigit() else 0)))
        return g.sort_values("_o").drop(columns="_o")

    def style_by_size(self):
        l = self.lines[self.lines["size"] != "One size"]
        t = l.pivot_table(index="style", columns="size", values="quantity", aggfunc="sum", fill_value=0)
        cols = [s for s in SIZES if s in t.columns] + sorted(c for c in t.columns if c not in SIZES)
        t = t[cols]
        t["Total"] = t.sum(axis=1)
        return t.sort_values("Total", ascending=False)

    def by_hour(self):
        return self.orders.pivot_table(index="hour", columns="day", values="net", aggfunc="sum", fill_value=0)

    def baskets(self):
        o = self.orders.assign(basket=self.orders["units"].map(lambda n: "1 item" if n <= 1 else ("2 items" if n == 2 else "3+ items")))
        g = o.groupby("basket").agg(orders=("id", "count"), units=("units", "sum"), net=("net", "sum"))
        g["share_orders"] = g["orders"] / g["orders"].sum()
        g["aov"] = g["net"] / g["orders"]
        return g.reset_index()

    def pricing(self):
        o = self.orders.assign(pricing=self.orders["discounted"].map({True: "Discounted", False: "Full price"}))
        g = o.groupby("pricing").agg(orders=("id", "count"), net=("net", "sum"), discounts=("discounts", "sum"))
        g["aov"] = g["net"] / g["orders"]
        g["share_orders"] = g["orders"] / g["orders"].sum()
        return g.reset_index()

    def findings(self):
        k, days = self.kpis(), self.by_day()
        out = []
        if len(days) > 1:
            top = days.sort_values("net", ascending=False).iloc[0]
            out.append(f"**{top['day']} carried the event**: {int(top['orders'])} orders, £{top['net']:,.0f} ({top['share']:.0%} of net).")
        prods = self.by("product_title")
        if not prods.empty:
            p = prods.iloc[0]
            out.append(f"**Hero product: {p['product_title']}**: {int(p['units'])} units, £{p['net']:,.0f} ({p['share']:.0%} of net).")
        cats = self.by("category")
        if not cats.empty:
            by_units = cats.sort_values("units", ascending=False).iloc[0]
            top_rev = cats.iloc[0]
            if by_units["category"] == top_rev["category"]:
                out.append(f"**{top_rev['category']}** led on both units ({int(top_rev['units'])}) and revenue (£{top_rev['net']:,.0f}).")
            else:
                out.append(f"**{by_units['category']}** led on units ({int(by_units['units'])}), "
                           f"**{top_rev['category']}** led on revenue (£{top_rev['net']:,.0f}).")
            low = cats[(cats["margin"].notna()) & (cats["units"] >= 2)].sort_values("margin")
            if not low.empty and low.iloc[0]["margin"] < 0.4:
                out.append(f"**{low.iloc[0]['category']} had the thinnest margin** ({low.iloc[0]['margin']:.0%}) "
                           "after discounts and unit cost.")
        sizes = self.size_curve()
        if not sizes.empty:
            s = sizes.sort_values("units", ascending=False).iloc[0]
            out.append(f"**{s['size']}** was the most-sold size ({int(s['units'])} units, {s['share']:.0%} of sized units).")
        if k["orders"]:
            single = (self.orders["units"] <= 1).mean()
            out.append(f"**{single:.0%} of orders were a single item**: bundles or add-ons could lift the £{k['aov']:,.0f} AOV.")
        hours = self.orders.groupby(["day", "hour"])["net"].sum()
        if not hours.empty:
            (d, h), v = hours.idxmax(), hours.max()
            out.append(f"**Peak hour: {d} {h:02d}:00** (£{v:,.0f}).")
        if k["net"]:
            out.append(f"**Gross margin {k['gross_profit'] / k['net']:.0%}** after unit costs; event profit "
                       f"£{k['event_profit']:,.0f} after card fees and £{k['event_costs']:,.0f} of tagged event costs.")
        return out
