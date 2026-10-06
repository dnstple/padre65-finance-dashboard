"""Revenue explorer: any metric, broken down by any dimension, over any time scale.

Facts are one row per sold line (on the order date) and one row per refunded line (on the refund date,
with negative values), carrying every dimension. Shared costs (fees, fulfilment, marketing) are allocated
to rows by their share of net revenue within each month, the same way as the product contribution report.
"""
import pandas as pd

from . import db, events

CHANNELS = {"web": "Online", "pos": "In person", "quick_sale": "In person", "shopify_draft_order": "Draft / manual"}

DIMENSIONS = {
    "Total": None, "Product": "product_title", "Style": "style", "Category": "category", "Size": "size",
    "Colour": "colour_family", "Variant": "variant_label", "Channel": "channel", "Event": "event",
    "Collection": "collection", "Full price vs discounted": "pricing",
}

# name -> (numerator column, denominator column or None, additive?)
METRICS = {
    "Net revenue": ("net", None, True),
    "Gross sales": ("gross", None, True),
    "Discounts": ("discount", None, True),
    "Refunds": ("refunds", None, True),
    "Units sold": ("units", None, True),
    "Orders": ("orders", None, True),
    "Average order value": ("net", "orders", False),
    "COGS": ("cogs", None, True),
    "Gross profit (CM1)": ("cm1", None, True),
    "CM1 %": ("cm1", "net", False),
    "CM2 (allocated)": ("cm2", None, True),
    "CM2 %": ("cm2", "net", False),
    "CM3 (allocated)": ("cm3", None, True),
    "CM3 %": ("cm3", "net", False),
    "Discount rate": ("discount", "gross", False),
    "Net profit": ("net_profit", None, True),
}
MONEY_METRICS = {"Net revenue", "Gross sales", "Discounts", "Refunds", "Average order value", "COGS",
                 "Gross profit (CM1)", "CM2 (allocated)", "CM3 (allocated)", "Net profit"}
PERCENT_METRICS = {"CM1 %", "CM2 %", "CM3 %", "Discount rate"}
TOTAL_ONLY = {"Net profit"}

TIMESCALES = {
    "Daily": ("D", None), "Daily, 7-day rolling average": ("D", 7), "Daily, 30-day rolling average": ("D", 30),
    "Weekly": ("W", None), "Monthly": ("M", None), "Quarterly": ("Q", None), "Yearly": ("Y", None),
}


def facts(conn, ledger):
    lines = events.enrich(ledger.lines)
    orders = ledger.orders.set_index("id")
    lines["channel"] = lines["order_id"].map(orders["source_name"]).map(lambda s: CHANNELS.get(s, str(s or "Other").title()))
    ev = db.read_df(conn, "SELECT o.order_id, e.name FROM event_orders o JOIN events e ON e.id = o.event_id")
    lines["event"] = lines["order_id"].map(ev.set_index("order_id")["name"]).fillna("No event")
    variants = db.read_df(conn, "SELECT variant_id, collections FROM shopify_variants")
    coll = variants.set_index("variant_id")["collections"].fillna("").map(lambda s: s.split(",")[0].strip() or "No collection")
    lines["collection"] = lines["variant_id"].map(coll).fillna("No collection")
    lines["variant_label"] = lines["product_title"] + " · " + lines["variant_title"].fillna("One size")
    lines["pricing"] = (lines["discount"] > 0).map({True: "Discounted", False: "Full price"})

    dims = [c for c in DIMENSIONS.values() if c]
    sales = lines.assign(
        kind="sale", units=lines["quantity"], refunds=0.0, cogs=lines["cogs"].fillna(0),
        net=lines["net"], orders_id=lines["order_id"],
    )[["date", "kind", "order_id", "orders_id", "gross", "discount", "refunds", "net", "units", "cogs"] + dims]

    rl = ledger.refund_lines
    if not rl.empty:
        dim_by_line = lines.set_index("id")[dims]
        refunds = rl.join(dim_by_line, on="line_item_id", rsuffix="_line")
        for d in dims:
            if f"{d}_line" in refunds:
                refunds[d] = refunds[f"{d}_line"]
        refunds = refunds.assign(
            kind="refund", gross=0.0, discount=0.0, refunds=refunds["net"], net=-refunds["net"],
            units=-refunds["quantity"], cogs=-refunds["cogs_reversal"].fillna(0), orders_id=None,
        )[["date", "kind", "order_id", "orders_id", "gross", "discount", "refunds", "net", "units", "cogs"] + dims]
        data = pd.concat([sales, refunds], ignore_index=True)
    else:
        data = sales
    data["cm1"] = data["net"] - data["cogs"]
    return _allocate(data, ledger)


def _allocate(data, ledger):
    """Spread each month's fees/fulfilment and marketing over rows by share of that month's net revenue."""
    p = ledger.pnl(freq="M")
    data = data.copy()
    data["month"] = data["date"].dt.to_period("M").astype(str)
    if p.empty:
        data["cm2"] = data["cm3"] = data["cm1"]
        return data

    def row(label):
        return p.loc[label].drop("Total") if label in p.index else pd.Series(0.0, index=p.columns.drop("Total"))

    fulfil = (row("Contribution after fulfilment (CM2)") - row("Gross profit (CM1)") + row("Shipping charged")
              + row("Shipping refunded") + row("Other refunds & adjustments"))
    marketing = row("Contribution after marketing (CM3)") - row("Contribution after fulfilment (CM2)")
    month_net = data.groupby("month")["net"].transform("sum")
    share = (data["net"] / month_net).where(month_net != 0, 0.0)
    data["cm2"] = data["cm1"] + share * data["month"].map(fulfil).fillna(0)
    data["cm3"] = data["cm2"] + share * data["month"].map(marketing).fillna(0)

    # Costs in months with no sales can't be spread over products - keep them visible so totals reconcile
    sales_months = set(data.loc[data.groupby("month")["net"].transform("sum") != 0, "month"])
    orphan = [m for m in fulfil.index if m not in sales_months and abs(fulfil[m]) + abs(marketing[m]) > 0.005]
    if orphan:
        label = "No sales that month"
        extra = pd.DataFrame({
            "date": [pd.Period(m, "M").start_time for m in orphan], "kind": "cost", "order_id": None, "orders_id": None,
            "gross": 0.0, "discount": 0.0, "refunds": 0.0, "net": 0.0, "units": 0, "cogs": 0.0, "cm1": 0.0,
            "cm2": [fulfil[m] for m in orphan], "cm3": [fulfil[m] + marketing[m] for m in orphan], "month": orphan,
        })
        for col in DIMENSIONS.values():
            if col:
                extra[col] = label
        data = pd.concat([data, extra], ignore_index=True)
    return data


def _bucket(dates, freq):
    return dates.dt.to_period(freq).dt.start_time


def series(conn, ledger, data, metric, dimension, timescale, start=None, end=None, only=None, cumulative=False):
    """Long table: period, group, value (plus the numerator/denominator behind ratios)."""
    num, den, additive = METRICS[metric]
    freq, window = TIMESCALES[timescale]
    dim = DIMENSIONS[dimension]

    if num == "net_profit":
        p = ledger.pnl(start, end, freq="D" if freq == "D" else freq)
        if p.empty or "Net profit" not in p.index:
            return pd.DataFrame(columns=["period", "group", "value"])
        s = p.loc["Net profit"].drop("Total")
        idx = pd.PeriodIndex(s.index, freq=freq if freq != "D" else "D").start_time
        out = pd.DataFrame({"period": idx, "group": "Total", "num": s.values, "den": 1.0})
    else:
        d = data
        if start is not None:
            d = d[d["date"] >= pd.Timestamp(start)]
        if end is not None:
            d = d[d["date"] < pd.Timestamp(end) + pd.Timedelta(days=1)]
        if dim and only:
            d = d[d[dim].isin(only)]
        d = d.assign(period=_bucket(d["date"], freq), group=d[dim] if dim else "Total")

        def agg(col):
            if col == "orders":
                return d[d["kind"] == "sale"].groupby(["period", "group"])["orders_id"].nunique()
            return d.groupby(["period", "group"])[col].sum()

        out = agg(num).rename("num").to_frame()
        out["den"] = agg(den) if den else 1.0
        out = out.reset_index()

    if out.empty:
        return out.assign(value=[])
    # complete grid of periods so lines/rolling averages see zero days
    lo, hi = out["period"].min(), out["period"].max()
    if start is not None:
        lo = min(lo, pd.Timestamp(start).to_period(freq).start_time)
    if end is not None:
        hi = max(hi, pd.Timestamp(end).to_period(freq).start_time)
    full = pd.period_range(lo, hi, freq=freq).start_time
    groups = out["group"].unique()
    grid = pd.MultiIndex.from_product([full, groups], names=["period", "group"])
    out = out.set_index(["period", "group"]).reindex(grid).fillna({"num": 0.0, "den": 0.0 if den else 1.0}).reset_index()
    out = out.sort_values(["group", "period"])
    if cumulative and additive:
        out["num"] = out.groupby("group")["num"].cumsum()
    if window:
        roll = out.groupby("group")[["num", "den"]].rolling(window, min_periods=1).sum().reset_index(level=0, drop=True)
        out[["num", "den"]] = roll
        if additive and not cumulative:
            out["num"] = out["num"] / window
            out["den"] = 1.0
    out["value"] = out["num"] / out["den"] if den else out["num"]
    if den:
        out.loc[out["den"] == 0, "value"] = float("nan")
    return out


def totals_by_group(out, metric):
    """One figure per group over the whole range (ratios recomputed from sums, not averaged)."""
    num, den, additive = METRICS[metric]
    if out.empty:
        return pd.Series(dtype=float)
    g = out.groupby("group")[["num", "den"]].sum() if (den or additive) else None
    if den:
        return (g["num"] / g["den"]).where(g["den"] != 0)
    return g["num"]
