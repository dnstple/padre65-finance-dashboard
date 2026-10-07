"""Product ideas: potential new products, compared on price and contribution with the current range.

Current range = every priced, costed Shopify product, grouped like the contribution report (same type, unit cost
and price joined). Products that have sold use their actual results; ideas (and unsold products) use estimates
from the business's averages so far:
- price customers actually pay = full price x (1 - average discount & returns rate)
- fees & fulfilment and marketing = the same share of revenue they take today.
"""
import math

import pandas as pd

from . import config, costs, db, events
from .reports import _group_name

FIELDS = ["name", "product_type", "price", "unit_cost", "cost_source", "first_order_units", "status", "notes"]
STATUSES = ["Idea", "Sampling", "Approved", "Launched", "Dropped"]
PRODUCT_TYPES = ["T-Shirt", "Long Sleeve", "Hoodie", "Sweatshirt", "Tracksuit", "Shorts", "Jacket", "Jeans", "Cap",
                 "Foulard", "Other"]
METRICS = {
    "Margin per unit at full price": ("full_margin", "money"),
    "Margin % at full price": ("full_margin_pct", "pct"),
    "Gross profit per unit (expected)": ("cm1_per_unit", "money"),
    "Profit per unit after fees, delivery & marketing (CM3)": ("cm3_per_unit", "money"),
}


# --- storage ------------------------------------------------------------------

def load(conn):
    df = db.read_df(conn, f"SELECT id, {', '.join(FIELDS)} FROM product_ideas ORDER BY id")
    if df.empty:
        df = pd.DataFrame(columns=["id"] + FIELDS)
    for col in ("price", "unit_cost", "first_order_units"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in ("name", "product_type", "cost_source", "status", "notes"):
        df[col] = df[col].astype("object").where(df[col].notna(), None)
    return df


def add(conn, **values):
    conn.execute(f"INSERT INTO product_ideas ({', '.join(FIELDS)}, created_at, updated_at) "
                 f"VALUES ({', '.join('?' for _ in FIELDS)}, ?, ?)",
                 (*[_clean(values.get(f)) for f in FIELDS], db.now_iso(), db.now_iso()))
    conn.commit()


def save(conn, df):
    """Replace saved ideas with the edited table (rows without a name are dropped)."""
    conn.execute("DELETE FROM product_ideas")
    for r in df.to_dict("records"):
        if not str(r.get("name") or "").strip():
            continue
        add(conn, **r)


def _clean(v):
    if v is None or (not isinstance(v, str) and pd.isna(v)):
        return None
    if hasattr(v, "item"):
        v = v.item()
    return v


# --- comparison ---------------------------------------------------------------

def business_rates(ledger):
    """Averages from everything sold so far, used to estimate ideas and unsold products."""
    g = ledger.grouped_contribution()
    gross, net = (g["gross"].sum(), g["net_revenue"].sum()) if not g.empty else (0, 0)
    return {
        "discount_rate": 1 - net / gross if gross else 0.0,
        "fulfil_rate": -g["alloc_fulfilment_fees"].sum() / net if net else 0.0,
        "marketing_rate": -g["alloc_marketing"].sum() / net if net else 0.0,
    }


def _estimate(df, rates):
    """Full-price and expected per-unit economics from price and unit cost."""
    df = df.copy()
    df["full_margin"] = df["price"] - df["unit_cost"]
    df["full_margin_pct"] = (df["full_margin"] / df["price"]).where(df["price"] > 0)
    df["expected_paid"] = df["price"] * (1 - rates["discount_rate"])
    df["est_cm1"] = df["expected_paid"] - df["unit_cost"]
    df["est_cm3"] = df["est_cm1"] - df["expected_paid"] * (rates["fulfil_rate"] + rates["marketing_rate"])
    return df


def current_range(conn, ledger, rates):
    """Every priced, costed product grouped like the contribution report, with actual results where it has sold."""
    v = db.read_df(conn, "SELECT variant_id, product_id, product_title, price, product_status FROM shopify_variants")
    v = v[~v["product_title"].fillna("").str.strip().str.lower().isin(config.EXCLUDE_PRODUCTS)]
    v = costs.unit_costs_for_lines(conn, v.assign(date=pd.Timestamp.now().normalize()))
    p = (v.groupby(["product_id", "product_title"])
         .agg(price=("price", "max"), unit_cost=("unit_cost", "max"), status=("product_status", "first"))
         .reset_index())
    p = p[(p["price"] > 0) & p["unit_cost"].notna()]
    p["category"] = p["product_title"].map(events.category_of)
    p["style"] = p["product_title"].map(lambda t: events.split_title(t)[0])
    p["key"] = list(zip(p["category"], p["unit_cost"].round(2), p["price"].round(2)))

    groups = (p.groupby("key").agg(category=("category", "first"), unit_cost=("unit_cost", "max"),
                                   price=("price", "max"), products=("product_title", "nunique"),
                                   includes=("product_title", lambda s: ", ".join(sorted(s))),
                                   styles=("style", lambda s: sorted(set(s))),
                                   active=("status", lambda s: bool((s == "ACTIVE").any())))
              .reset_index())
    groups["name"] = [_group_name(st, cat, pr) for st, cat, pr in zip(groups["styles"], groups["category"], groups["price"])]
    dupes = groups["name"].duplicated(keep=False)
    groups.loc[dupes, "name"] = [f"{n} (cost £{c:,.2f})" for n, c in zip(groups.loc[dupes, "name"], groups.loc[dupes, "unit_cost"])]

    sold = ledger.grouped_contribution()
    if not sold.empty:
        sold = sold[sold["unit_cost"].notna() & sold["full_price"].notna()].copy()
        sold["key"] = list(zip(sold["category"], sold["unit_cost"].round(2), sold["full_price"].round(2)))
        groups = groups.merge(sold[["key", "net_units", "net_revenue", "cm1_per_unit", "cm1_pct", "cm3_per_unit"]],
                              on="key", how="left")
    for col in ("net_units", "net_revenue"):
        groups[col] = groups.get(col, 0)
        groups[col] = groups[col].fillna(0)
    groups = _estimate(groups, rates)
    has_sales = groups["net_units"] > 0
    # actual results where the products have sold, estimates otherwise
    groups["cm1_per_unit"] = groups.get("cm1_per_unit", pd.Series(dtype=float)).where(has_sales, groups["est_cm1"])
    groups["cm3_per_unit"] = groups.get("cm3_per_unit", pd.Series(dtype=float)).where(has_sales, groups["est_cm3"])
    groups["basis"] = has_sales.map({True: "Actual sales", False: "Estimate (not sold yet)"})
    groups["kind"] = "Current range"
    return groups.drop(columns=["key", "styles"]).sort_values("net_revenue", ascending=False).reset_index(drop=True)


def evaluate_ideas(ideas, rates):
    d = ideas.copy()
    d = d[d["name"].fillna("").astype(str).str.strip() != ""]
    if d.empty:
        return d
    d["category"] = d["product_type"].fillna("Other")
    d = _estimate(d, rates)
    d["cm1_per_unit"], d["cm3_per_unit"] = d["est_cm1"], d["est_cm3"]
    d["first_order_cost"] = d["first_order_units"] * d["unit_cost"]
    d["payback_units"] = [math.ceil(c / m) if pd.notna(c) and pd.notna(m) and m > 0 else None
                          for c, m in zip(d["first_order_cost"], d["cm1_per_unit"])]
    d["kind"] = "Idea"
    return d


def stack_up(ideas_eval, current):
    """How each idea compares: rank among current products and against the average for its product type."""
    rows = []
    n = len(current)
    for r in ideas_eval.to_dict("records"):
        same_type = current[current["category"] == r["category"]]
        rank_margin = int((current["full_margin"] > r["full_margin"]).sum()) + 1 if pd.notna(r["full_margin"]) else None
        rank_pct = int((current["full_margin_pct"] > r["full_margin_pct"]).sum()) + 1 if pd.notna(r["full_margin_pct"]) else None
        rows.append({
            "name": r["name"], "product_type": r["category"], "status": r.get("status"), "price": r["price"],
            "unit_cost": r["unit_cost"], "full_margin": r["full_margin"], "full_margin_pct": r["full_margin_pct"],
            "cm3_per_unit": r["cm3_per_unit"],
            "rank_margin": f"{rank_margin} of {n + 1}" if rank_margin else "–",
            "rank_pct": f"{rank_pct} of {n + 1}" if rank_pct else "–",
            "type_avg_price": same_type["price"].mean() if len(same_type) else None,
            "type_avg_margin": same_type["full_margin"].mean() if len(same_type) else None,
            "vs_type_margin": (r["full_margin"] - same_type["full_margin"].mean()) if len(same_type) and pd.notna(r["full_margin"]) else None,
            "first_order_cost": r.get("first_order_cost"), "payback_units": r.get("payback_units"),
        })
    return pd.DataFrame(rows)
