"""Pop-up stock planner: current stock vs what a repeat of a past pop-up would need.

Demand model
- Each product's expected units = units it sold at the base event × scale.
- Units are split across sizes using a blend of that product's own size mix at the event and the event's
  overall size curve (shrinkage), so a product that sold 2 units doesn't get its whole forecast in one size.
- Target stock = expected × (1 + buffer), with a minimum per size for sized products that sold.
- To buy = max(target − Shopify stock, 0); cost = to buy × landed unit cost.
"""
import math

import pandas as pd

from . import config, costs, db, events

SHRINK = 5  # weight of the overall size curve vs a product's own size mix


def _size_weights(own_counts, curve, sizes):
    """Blend a product's own size counts with the overall curve, restricted to sizes it comes in."""
    curve = curve.reindex(sizes).fillna(0)
    if curve.sum() == 0:
        curve = pd.Series(1.0, index=sizes)
    curve = curve / curve.sum()
    own = own_counts.reindex(sizes).fillna(0)
    w = (own + SHRINK * curve) / (own.sum() + SHRINK)
    return w / w.sum()


def plan(conn, ledger, event_id, scale=1.0, buffer=0.5, min_per_size=1):
    report = events.EventReport(conn, ledger, event_id)
    sold = report.lines[report.lines["variant_id"].notna()]
    sold_by_variant = sold.groupby("variant_id")["quantity"].sum()
    sold_by_product = sold.groupby("product_id")["quantity"].sum()
    last = report.kpis()
    discount_rate = last["discount_rate"]
    fee_rate = last["card_fees"] / last["net"] if last["net"] else 0.0

    v = db.read_df(conn, "SELECT * FROM shopify_variants")
    v = v[~v["product_title"].fillna("").str.strip().str.lower().isin(config.EXCLUDE_PRODUCTS)].copy()
    v["size"] = v["variant_title"].map(events.size_of)
    v["category"] = v["product_title"].map(events.category_of)
    v["on_hand"] = v["inventory_quantity"].fillna(0).clip(lower=0).astype(int)
    v["sold_last"] = v["variant_id"].map(sold_by_variant).fillna(0).astype(int)
    v["product_sold_last"] = v["product_id"].map(sold_by_product).fillna(0).astype(int)
    v = costs.unit_costs_for_lines(conn, v.assign(date=pd.Timestamp.now().normalize()))

    # Demand shown in all sales to date (online + in person), net of refunds - used to rank importance
    all_sold = ledger.lines.groupby("variant_id")["quantity"].sum()
    if not ledger.refund_lines.empty:
        all_sold = all_sold.sub(ledger.refund_lines.groupby("variant_id")["quantity"].sum(), fill_value=0)
    v["sold_all_time"] = v["variant_id"].map(all_sold).fillna(0).clip(lower=0).astype(int)
    v["product_sold_all_time"] = v.groupby("product_id")["sold_all_time"].transform("sum")

    # overall size curve from the event (sized garments only)
    curve = sold[sold["size"] != "One size"].groupby("size")["quantity"].sum()

    v["expected"] = 0.0
    v["target"] = 0
    for pid, grp in v[v["product_sold_last"] > 0].groupby("product_id"):
        demand = grp["product_sold_last"].iloc[0] * scale
        sized = grp[grp["size"] != "One size"]
        if sized.empty:
            weights = pd.Series(1.0 / len(grp), index=grp.index)
        else:
            own = sized.groupby("size")["sold_last"].sum()
            w = _size_weights(own, curve, list(sized["size"]))
            weights = pd.Series(sized["size"].map(w).values, index=sized.index)
        v.loc[weights.index, "expected"] = weights * demand
        # Round once per product, then share the whole units out across sizes (largest remainder),
        # so a product that sold 1 unit isn't given a unit in every size by rounding alone.
        product_target = math.ceil(round(demand * (1 + buffer), 6))
        raw = weights * product_target
        alloc = raw.apply(math.floor)
        for idx in (raw - alloc).sort_values(ascending=False).index[: product_target - int(alloc.sum())]:
            alloc[idx] += 1
        v.loc[alloc.index, "target"] = alloc.astype(int)

    sized_and_sold = (v["size"] != "One size") & (v["product_sold_last"] > 0)
    v.loc[sized_and_sold, "target"] = v.loc[sized_and_sold, "target"].clip(lower=min_per_size)
    v["target"] = v["target"].astype(int)
    v["to_buy"] = (v["target"] - v["on_hand"]).clip(lower=0)
    v["buy_cost"] = v["to_buy"] * v["unit_cost"]
    v["covered_units"] = v[["on_hand", "target"]].min(axis=1)
    v["status"] = [
        "Restock" if t > 0 and o == 0 else ("Top up" if b > 0 else ("Plenty" if t > 0 and o >= 2 * t else "Ready"))
        for t, o, b in zip(v["target"], v["on_hand"], v["to_buy"])
    ]

    in_plan = v[v["product_sold_last"] > 0]
    expected_units = in_plan["expected"].sum()
    expected_sales = (in_plan["expected"] * in_plan["price"]).sum() * (1 - discount_rate)
    expected_cogs = (in_plan["expected"] * in_plan["unit_cost"].fillna(0)).sum()

    products = (in_plan.groupby(["product_id", "product_title", "category"])
                .agg(sold_last=("sold_last", "sum"), on_hand=("on_hand", "sum"), expected=("expected", "sum"),
                     target=("target", "sum"), to_buy=("to_buy", "sum"), buy_cost=("buy_cost", "sum"),
                     unit_cost=("unit_cost", "max"), price=("price", "max"),
                     sizes_short=("to_buy", lambda s: int((s > 0).sum())))
                .reset_index())
    products["status"] = [
        "Restock" if o == 0 else ("Top up" if b > 0 else ("Plenty" if o >= 2 * t else "Ready"))
        for o, b, t in zip(products["on_hand"], products["to_buy"], products["target"])
    ]
    products = products.sort_values(["buy_cost", "expected"], ascending=False)

    not_sold = (v[(v["product_sold_last"] == 0) & (v["on_hand"] > 0)]
                .groupby(["product_title", "category"])
                .agg(on_hand=("on_hand", "sum"), unit_cost=("unit_cost", "max"), price=("price", "max"))
                .reset_index().sort_values("on_hand", ascending=False))

    summary = {
        "event": report.event, "expected_units": expected_units, "expected_sales": expected_sales,
        "expected_gross_profit": expected_sales - expected_cogs, "target_units": int(in_plan["target"].sum()),
        "covered_units": int(in_plan["covered_units"].sum()), "to_buy_units": int(in_plan["to_buy"].sum()),
        "buy_cost": float(in_plan["buy_cost"].sum()),
        "uncosted_to_buy": int(in_plan.loc[in_plan["unit_cost"].isna(), "to_buy"].sum()),
        "discount_rate": discount_rate, "fee_rate": fee_rate, "last_units": int(sold["quantity"].sum()),
        "last_event_costs": last["event_costs"],
        "last_orders": len(report.orders),
    }
    summary["readiness"] = summary["covered_units"] / summary["target_units"] if summary["target_units"] else 1.0
    buy_list = in_plan[in_plan["to_buy"] > 0][["product_title", "size", "sku", "on_hand", "expected", "target",
                                                "to_buy", "unit_cost", "buy_cost"]]
    buy_list = buy_list.sort_values(["product_title", "size"])
    return summary, products, in_plan, buy_list, not_sold


def size_rank(size):
    if size in events.SIZES:
        return events.SIZES.index(size)
    return 100 + (int(size) if str(size).isdigit() else 0)


def restock_plan(conn, in_plan):
    """Per-size restock rows ordered by demand, with saved include/override choices applied."""
    rows = in_plan.copy()
    rows = rows[(rows["target"] > 0) | (rows["on_hand"] > 0) | (rows["sold_all_time"] > 0)]
    saved = db.read_df(conn, "SELECT variant_id, include, buy_override FROM planner_overrides")
    rows = rows.merge(saved, on="variant_id", how="left")
    rows["include"] = rows["include"].fillna(1).astype(bool)
    rows["suggested"] = rows["to_buy"].astype(int)
    rows["buy_override"] = pd.to_numeric(rows["buy_override"], errors="coerce")
    rows["final_buy"] = rows["buy_override"].fillna(rows["suggested"]).where(rows["include"], 0).astype(int)
    rows["final_cost"] = rows["final_buy"] * rows["unit_cost"]
    rows["_size"] = rows["size"].map(size_rank)
    rows = rows.sort_values(["product_sold_all_time", "product_title", "_size"], ascending=[False, True, True])
    ranks = rows.drop_duplicates("product_id")[["product_id"]].reset_index(drop=True)
    ranks["rank"] = ranks.index + 1
    rows = rows.merge(ranks, on="product_id", how="left")
    return rows.drop(columns="_size").reset_index(drop=True)


def save_choices(conn, changes):
    """changes: list of (variant_id, include, buy_override or None)."""
    for variant_id, include, override in changes:
        conn.execute("INSERT OR REPLACE INTO planner_overrides (variant_id, include, buy_override, updated_at) VALUES (?, ?, ?, ?)",
                     (variant_id, int(bool(include)), None if override is None else int(override), db.now_iso()))
    conn.commit()


def reset_choices(conn):
    conn.execute("DELETE FROM planner_overrides")
    conn.commit()


# --- pop-up running costs and expected profit ---------------------------------------

COST_CATEGORIES = ["Venue / shop rent", "Fit-out & display", "Staff", "Travel & transport", "Marketing & promotion",
                   "Packaging & bags", "Insurance & licences", "Equipment & POS", "Food & sundries", "Other"]
DEFAULT_COSTS = [
    ("Shop rent", "Venue / shop rent"), ("Rails, hangers, mirrors & signage", "Fit-out & display"),
    ("Staff / helpers", "Staff"), ("Van hire, fuel & parking", "Travel & transport"),
    ("Promotion (ads, flyers, influencers)", "Marketing & promotion"), ("Bags & tissue", "Packaging & bags"),
    ("Event insurance", "Insurance & licences"), ("Drinks, food & sundries", "Food & sundries"),
]


def load_costs(conn):
    """Saved pop-up cost lines, or a starter list at £0 if none have been saved yet."""
    df = db.read_df(conn, "SELECT id, item, category, amount, notes FROM popup_costs ORDER BY id")
    if df.empty:
        df = pd.DataFrame({"id": [None] * len(DEFAULT_COSTS), "item": [i for i, _ in DEFAULT_COSTS],
                           "category": [c for _, c in DEFAULT_COSTS], "amount": 0.0, "notes": ""})
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce").fillna(0.0)
    return df


def save_costs(conn, df):
    """Replace the saved cost lines with the edited table."""
    conn.execute("DELETE FROM popup_costs")
    for r in df.to_dict("records"):
        item = str(r.get("item") or "").strip()
        if not item:
            continue
        amount = pd.to_numeric(r.get("amount"), errors="coerce")
        conn.execute("INSERT INTO popup_costs (item, category, amount, notes, updated_at) VALUES (?, ?, ?, ?, ?)",
                     (item, r.get("category") or "Other", 0.0 if pd.isna(amount) else float(amount),
                      r.get("notes") or "", db.now_iso()))
    conn.commit()


def projection(summary, costs_df, sales_factor=1.0):
    """Expected pop-up P&L: sales - stock cost of items sold - card fees - running costs."""
    sales = summary["expected_sales"] * sales_factor
    cogs = (summary["expected_sales"] - summary["expected_gross_profit"]) * sales_factor
    fees = sales * summary["fee_rate"]
    running = float(pd.to_numeric(costs_df["amount"], errors="coerce").fillna(0).sum())
    profit = sales - cogs - fees - running
    contribution_rate = (sales - cogs - fees) / sales if sales else 0
    return {"sales": sales, "cogs": cogs, "gross_profit": sales - cogs, "fees": fees, "running": running,
            "profit": profit, "margin": profit / sales if sales else None,
            "break_even_sales": running / contribution_rate if contribution_rate > 0 else None}


def size_grid(in_plan):
    """Style × size grid of 'on hand / target' for sized products in the plan."""
    sized = in_plan[in_plan["size"] != "One size"].copy()
    if sized.empty:
        return pd.DataFrame()
    order = [s for s in events.SIZES if s in set(sized["size"])] + sorted(s for s in set(sized["size"]) if s not in events.SIZES)
    sized["cell"] = [("⚠️ " if b > 0 else "") + f"{o} / {t}" for o, t, b in zip(sized["on_hand"], sized["target"], sized["to_buy"])]
    grid = sized.pivot_table(index="product_title", columns="size", values="cell", aggfunc="first").reindex(columns=order)
    return grid.fillna("–")
