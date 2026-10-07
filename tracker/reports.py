"""Financial reports built from the local database.

Conventions
- Sales are recognised on the order's processed date, refunds on the refund date (UK time).
- Revenue comes from Shopify only. Shopify payouts landing in Wise are excluded (cash movement).
- COGS = units sold x unit cost (master sheet / restock log). Stock purchases are cash, not P&L.
- Refunded units that were restocked reverse their COGS; units refunded without restock keep it.
"""
import pandas as pd

from . import categorise, config, costs, db

EXCLUDED_STATUSES = ("VOIDED", "EXPIRED")
RESTOCKED = ("RETURN", "CANCEL", "LEGACY_RESTOCK")


def _local(series):
    return pd.to_datetime(series, utc=True, format="mixed").dt.tz_convert(config.TIMEZONE).dt.tz_localize(None)


def excluded_order_ids(conn):
    """Orders whose every line is an excluded product (e.g. internal £1 TEST purchases)."""
    if not config.EXCLUDE_PRODUCTS:
        return set()
    lines = db.read_df(conn, "SELECT order_id, product_title FROM shopify_order_lines")
    lines["excluded"] = lines["product_title"].fillna("").str.strip().str.lower().isin(config.EXCLUDE_PRODUCTS)
    flags = lines.groupby("order_id")["excluded"].all()
    return set(flags[flags].index)


def orders(conn):
    df = db.read_df(conn, "SELECT * FROM shopify_orders WHERE test = 0")
    df = df[~df["financial_status"].isin(EXCLUDED_STATUSES) & ~df["id"].isin(excluded_order_ids(conn))].copy()
    df["date"] = _local(df["processed_at"])
    return df


def sales_lines(conn):
    lines = db.read_df(conn, """
        SELECT l.*, o.processed_at, o.name AS order_name
        FROM shopify_order_lines l JOIN shopify_orders o ON o.id = l.order_id
        WHERE o.test = 0 AND COALESCE(o.financial_status, '') NOT IN ('VOIDED', 'EXPIRED')
    """)
    lines = lines[~lines["order_id"].isin(excluded_order_ids(conn))].copy()
    lines["date"] = _local(lines["processed_at"])
    lines["net"] = lines["gross"] - lines["discount"] - lines["tax"]
    lines = costs.unit_costs_for_lines(conn, lines)
    lines["cogs"] = lines["quantity"] * lines["unit_cost"]
    return lines


def refund_lines(conn, sold):
    r = db.read_df(conn, "SELECT * FROM shopify_refund_lines")
    r = r[r["order_id"].isin(sold["order_id"])].copy()
    if r.empty:
        return r.assign(date=pd.Series(dtype="datetime64[ns]"), net=[], cogs_reversal=[], product_id=[], product_title=[], variant_id=[])
    r["date"] = _local(r["created_at"])
    info = sold.set_index("id")[["product_id", "variant_id", "product_title", "variant_title", "sku", "unit_cost"]]
    r = r.join(info, on="line_item_id")
    r["net"] = r["subtotal"] - r["tax"]
    restocked = r["restock_type"].isin(RESTOCKED)
    r["cogs_reversal"] = (r["quantity"] * r["unit_cost"]).where(restocked, 0.0)
    return r


def payment_fees(conn, ords):
    """Actual Shopify Payments fees if synced, otherwise an estimate from the configured rate."""
    bt = db.read_df(conn, "SELECT transaction_date, fee FROM shopify_balance_txns")
    if not bt.empty:
        bt["date"] = _local(bt["transaction_date"])
        return bt[["date", "fee"]].assign(estimated=False)
    paid = ords[(ords["gateways"].fillna("").str.contains("shopify_payments")) & (ords["total"] > 0)]
    fee = paid["total"] * config.SHOPIFY_FEE_PCT + config.SHOPIFY_FEE_FIXED
    return pd.DataFrame({"date": paid["date"], "fee": fee, "estimated": True})


def _in_range(df, start, end):
    if start is not None:
        df = df[df["date"] >= pd.Timestamp(start)]
    if end is not None:
        df = df[df["date"] < pd.Timestamp(end) + pd.Timedelta(days=1)]
    return df


def _period(df, freq):
    return df["date"].dt.to_period(freq).astype(str)


def _per_unit(g):
    """Per-unit economics: what each unit actually sold for and earned, after discounts and returns."""
    g = g.copy()
    g["cm1_pct"] = (g["cm1"] / g["net_revenue"]).where(g["net_revenue"] != 0)
    g["cm2_pct"] = (g["cm2"] / g["net_revenue"]).where(g["net_revenue"] != 0)
    g["cm3_pct"] = (g["cm3"] / g["net_revenue"]).where(g["net_revenue"] != 0)
    net_units = (g["units"] - g["units_returned"]).where(lambda u: u > 0)
    g["net_units"] = net_units.fillna(0).astype(int)
    g["avg_price_paid"] = g["net_revenue"] / net_units
    g["cm1_per_unit"] = g["cm1"] / net_units
    g["cm2_per_unit"] = g["cm2"] / net_units
    g["cm3_per_unit"] = g["cm3"] / net_units
    g["full_price_margin"] = g["full_price"] - g["unit_cost"]
    return g


def _group_name(styles, category, price):
    """Readable name for a group of products: the design name if they're all one design, otherwise the words
    they share (e.g. 'Patchwork Club Cap'), otherwise the type and price."""
    if len(styles) == 1:
        return styles[0]
    words = [s.split() for s in styles]
    common = []
    for parts in zip(*[w[::-1] for w in words]):
        if len({p.lower() for p in parts}) == 1:
            common.insert(0, parts[0])
        else:
            break
    base = " ".join(common) if common else f"{category} · £{price:,.0f}" if pd.notna(price) else category
    return f"{base} ({len(styles)} designs)"


class Ledger:
    """Everything the reports need, loaded once."""

    def __init__(self, conn):
        self.conn = conn
        self.orders = orders(conn)
        self.lines = sales_lines(conn)
        self.refund_lines = refund_lines(conn, self.lines)
        refunds = db.read_df(conn, "SELECT * FROM shopify_refunds")
        refunds = refunds[~refunds["order_id"].isin(excluded_order_ids(conn))].copy()
        refunds["date"] = _local(refunds["created_at"]) if not refunds.empty else pd.Series(dtype="datetime64[ns]")
        line_totals = self.refund_lines.groupby("refund_id")["subtotal"].sum() if not self.refund_lines.empty else pd.Series(dtype=float)
        if not refunds.empty:
            refunds["other"] = (refunds["total"] - refunds["shipping"] - refunds["id"].map(line_totals).fillna(0)).clip(lower=0)
        self.refunds = refunds
        self.fees = payment_fees(conn, self.orders)
        self.cash = categorise.all_cash_transactions(conn)
        wo = db.read_df(conn, "SELECT * FROM stock_writeoffs")
        wo["date"] = pd.to_datetime(wo["date"]) if not wo.empty else pd.Series(dtype="datetime64[ns]")
        self.writeoffs = wo

    @property
    def fees_estimated(self):
        return bool(len(self.fees)) and bool(self.fees["estimated"].iloc[0])

    @property
    def missing_cost_lines(self):
        return self.lines[self.lines["unit_cost"].isna() & (self.lines["gross"] > 0)]

    def pnl(self, start=None, end=None, freq="M"):
        """P&L with one column per period plus Total. Costs are negative numbers."""
        rows = {}

        def add(label, df, value_col, sign=1):
            df = _in_range(df, start, end)
            if df.empty:
                rows[label] = pd.Series(dtype=float)
                return
            rows[label] = sign * df.groupby(_period(df, freq))[value_col].sum()

        lines, rl, ords = self.lines, self.refund_lines, self.orders
        add("Gross sales", lines.assign(v=lines["gross"]), "v")
        add("Discounts", lines, "discount", -1)
        if not rl.empty:
            add("Refunds", rl, "subtotal", -1)
        if not self.refunds.empty:
            add("Other refunds & adjustments", self.refunds, "other", -1)
        add("VAT", lines, "tax", -1)
        if not rl.empty:
            vat_back = _in_range(rl, start, end)
            if not vat_back.empty:
                rows["VAT"] = rows["VAT"].add(vat_back.groupby(_period(vat_back, freq))["tax"].sum(), fill_value=0)
        add("Shipping charged", ords, "shipping")
        if not self.refunds.empty:
            add("Shipping refunded", self.refunds, "shipping", -1)

        add("COGS", lines.assign(v=lines["cogs"].fillna(0)), "v", -1)
        if not rl.empty:
            add("COGS reversed (returns)", rl.assign(v=rl["cogs_reversal"].fillna(0)), "v")

        add("Payment fees" + (" (estimated)" if self.fees_estimated else ""), self.fees, "fee", -1)
        if config.PACKAGING_COST_PER_ORDER:
            shipped = ords[ords["total"] > 0].assign(v=config.PACKAGING_COST_PER_ORDER)
            add("Packaging (per-order estimate)", shipped, "v", -1)

        if not self.writeoffs.empty:
            add("Stock written off", self.writeoffs, "amount", -1)

        cash = self.cash
        cat_rows = {}
        for ctype in ("fulfilment", "marketing", "opex", "other_income"):
            sub = cash[cash["type"] == ctype]
            for cat in sorted(sub["category"].unique()):
                c = sub[sub["category"] == cat]
                add(cat, c, "amount_base")
                cat_rows.setdefault(ctype, []).append(cat)

        table = pd.DataFrame(rows).T.fillna(0.0)
        if table.empty:
            return table
        table = table.reindex(sorted(table.columns), axis=1)

        def total(labels):
            present = [l for l in labels if l in table.index]
            return table.loc[present].sum() if present else pd.Series(0.0, index=table.columns)

        revenue_labels = ["Gross sales", "Discounts", "Refunds", "Other refunds & adjustments", "VAT",
                          "Shipping charged", "Shipping refunded"]
        fee_labels = [l for l in table.index if l.startswith("Payment fees") or l.startswith("Packaging (")]
        cogs_labels = ["COGS", "COGS reversed (returns)"]

        out = []

        def section(labels, subtotal_label=None, subtotal=None):
            for l in labels:
                if l in table.index:
                    out.append((l, table.loc[l], False))
            if subtotal_label:
                out.append((subtotal_label, subtotal, True))

        net_rev = total(revenue_labels)
        cm1 = net_rev + total(cogs_labels)
        cm2 = cm1 + total(fee_labels) + total(cat_rows.get("fulfilment", []))
        cm3 = cm2 + total(cat_rows.get("marketing", []))
        net = (cm3 + total(["Stock written off"]) + total(cat_rows.get("opex", []))
               + total(cat_rows.get("other_income", [])))

        section(revenue_labels, "Net revenue", net_rev)
        section(cogs_labels, "Gross profit (CM1)", cm1)
        section(fee_labels + cat_rows.get("fulfilment", []), "Contribution after fulfilment (CM2)", cm2)
        section(cat_rows.get("marketing", []), "Contribution after marketing (CM3)", cm3)
        section(["Stock written off"] + cat_rows.get("opex", []) + cat_rows.get("other_income", []), "Net profit", net)

        result = pd.DataFrame([r[1] for r in out], index=[r[0] for r in out])
        result["Total"] = result.sum(axis=1)
        subtotal_rows = [r[0] for r in out if r[2]]
        result = result[(result.abs().sum(axis=1) > 0.005) | result.index.isin(subtotal_rows + ["Gross sales", "COGS"])]
        result.attrs["subtotals"] = [r[0] for r in out if r[2]]
        return result

    def product_contribution(self, start=None, end=None, by="product"):
        """Per-product (or per-variant) revenue, COGS and contribution, with period costs allocated by revenue share."""
        keys = ["product_id", "product_title"] if by == "product" else ["variant_id", "product_title", "variant_title", "sku"]
        lines = _in_range(self.lines, start, end)
        if lines.empty:
            return pd.DataFrame()
        g = lines.groupby(keys, dropna=False).agg(
            units=("quantity", "sum"), gross=("gross", "sum"), discounts=("discount", "sum"),
            vat=("tax", "sum"), net_sales=("net", "sum"), cogs=("cogs", "sum"),
            missing_cost=("unit_cost", lambda s: bool(s.isna().any())),
            unit_cost=("unit_cost", "last"),
        )
        rl = _in_range(self.refund_lines, start, end)
        if not rl.empty:
            r = rl.groupby(keys, dropna=False).agg(units_returned=("quantity", "sum"), refunds=("net", "sum"),
                                                   cogs_reversed=("cogs_reversal", "sum"))
            g = g.join(r, how="left")
        for c in ("units_returned", "refunds", "cogs_reversed"):
            g[c] = g.get(c, 0.0)
        g = g.fillna({"units_returned": 0, "refunds": 0.0, "cogs_reversed": 0.0})
        g["net_revenue"] = g["net_sales"] - g["refunds"]
        g["cogs"] = g["cogs"].fillna(0) - g["cogs_reversed"]
        g["cm1"] = g["net_revenue"] - g["cogs"]

        p = self.pnl(start, end, freq="Y")
        share = g["net_revenue"] / g["net_revenue"].sum() if g["net_revenue"].sum() else 0
        if not p.empty:
            def row(label):
                return p.loc[label, "Total"] if label in p.index else 0.0
            fees_fulfil = row("Contribution after fulfilment (CM2)") - row("Gross profit (CM1)")
            # shipping income/refunds are order level - allocate with fulfilment costs
            fees_fulfil += row("Shipping charged") + row("Shipping refunded") + row("Other refunds & adjustments")
            marketing = row("Contribution after marketing (CM3)") - row("Contribution after fulfilment (CM2)")
        else:
            fees_fulfil = marketing = 0.0
        g["alloc_fulfilment_fees"] = fees_fulfil * share
        g["cm2"] = g["cm1"] + g["alloc_fulfilment_fees"]
        g["alloc_marketing"] = marketing * share
        g["cm3"] = g["cm2"] + g["alloc_marketing"]
        g = g.reset_index()
        # current full (list) price, for the margin you'd make on an undiscounted sale
        variants = db.read_df(self.conn, "SELECT variant_id, product_id, price FROM shopify_variants")
        if by == "product":
            g["full_price"] = g["product_id"].map(variants.groupby("product_id")["price"].max())
        else:
            g["full_price"] = g["variant_id"].map(variants.set_index("variant_id")["price"])
        return _per_unit(g).sort_values("net_revenue", ascending=False)

    def grouped_contribution(self, start=None, end=None):
        """Products joined when they're the same thing commercially: same type, same unit cost and same full price
        (e.g. every Club Long Sleeve colour, or the £50 caps that cost the same to make)."""
        from . import events
        pc = self.product_contribution(start, end, by="product")
        if pc.empty:
            return pc
        pc["category"] = pc["product_title"].map(events.category_of)
        pc["style"] = pc["product_title"].map(lambda t: events.split_title(t)[0])
        pc["cost_key"] = pc["unit_cost"].round(2).astype(object).where(pc["unit_cost"].notna(), "no cost")
        pc["price_key"] = pc["full_price"].round(2).astype(object).where(pc["full_price"].notna(), "no price")
        sums = ["units", "units_returned", "gross", "discounts", "net_revenue", "cogs", "cm1",
                "alloc_fulfilment_fees", "cm2", "alloc_marketing", "cm3"]
        g = (pc.groupby(["category", "cost_key", "price_key"], dropna=False)
             .agg(**{c: (c, "sum") for c in sums},
                  unit_cost=("unit_cost", "max"), full_price=("full_price", "max"),
                  missing_cost=("missing_cost", "any"), products=("product_title", "nunique"),
                  includes=("product_title", lambda s: ", ".join(sorted(s))),
                  styles=("style", lambda s: sorted(set(s))))
             .reset_index())
        g["group"] = [_group_name(styles, cat, price) for styles, cat, price in zip(g["styles"], g["category"], g["full_price"])]
        # same design but a different cost (e.g. two colours made by different suppliers): say which is which
        dupes = g["group"].duplicated(keep=False)
        g.loc[dupes, "group"] = [f"{name} (cost £{cost:,.2f})" if pd.notna(cost) else f"{name} (no cost)"
                                 for name, cost in zip(g.loc[dupes, "group"], g.loc[dupes, "unit_cost"])]
        g = g[g["gross"] != 0]  # free items such as £0 postage add-ons
        g = g.drop(columns=["styles", "cost_key", "price_key"])
        for col, num in (("cm1_pct", "cm1"), ("cm2_pct", "cm2"), ("cm3_pct", "cm3")):
            g[col] = (g[num] / g["net_revenue"]).where(g["net_revenue"] != 0)
        return _per_unit(g).sort_values("net_revenue", ascending=False)

    def stock_check(self):
        """Stock bought vs sold vs written off vs what Shopify says is usable, all at cost."""
        cash = self.cash
        bought = -cash.loc[cash["type"] == "inventory", "amount_base"].sum()
        sold = self.lines["cogs"].fillna(0).sum()
        if not self.refund_lines.empty:
            sold -= self.refund_lines["cogs_reversal"].fillna(0).sum()
        written_off = self.writeoffs["amount"].sum() if not self.writeoffs.empty else 0.0

        variants = db.read_df(self.conn, "SELECT * FROM shopify_variants")
        variants = variants[~variants["product_title"].fillna("").str.strip().str.lower().isin(config.EXCLUDE_PRODUCTS)]
        today = pd.Timestamp.now().normalize()
        v = variants.assign(date=today, quantity=variants["inventory_quantity"].fillna(0).clip(lower=0))
        v = costs.unit_costs_for_lines(self.conn, v)
        v["value"] = v["quantity"] * v["unit_cost"]
        v["retail"] = v["quantity"] * v["price"].fillna(0)
        on_hand = (v.groupby(["product_id", "product_title"])
                   .agg(units=("quantity", "sum"), unit_cost=("unit_cost", "max"), value=("value", "sum"),
                        retail=("retail", "sum"), status=("product_status", "first"))
                   .reset_index())
        on_hand = on_hand[on_hand["units"] > 0].sort_values("value", ascending=False)
        usable = on_hand["value"].sum()
        uncosted_units = int(on_hand.loc[on_hand["unit_cost"].isna(), "units"].sum())
        expected = bought - sold - written_off
        return {
            "bought": bought, "sold": sold, "written_off": written_off, "expected": expected,
            "usable": usable, "retail": on_hand["retail"].sum(), "gap": expected - usable, "units": int(on_hand["units"].sum()),
            "uncosted_units": uncosted_units, "on_hand": on_hand,
        }

    def cash_flow(self, start=None, end=None, freq="M"):
        cash = _in_range(self.cash, start, end)
        if cash.empty:
            return pd.DataFrame()
        cash = cash.assign(period=_period(cash, freq))
        return cash.pivot_table(index="period", columns="type", values="amount_base", aggfunc="sum", fill_value=0)

    def payout_reconciliation(self):
        payouts = db.read_df(self.conn, "SELECT * FROM shopify_payouts WHERE status = 'PAID'")
        deposits = self.cash[(self.cash["category"] == "Shopify Payout") & (self.cash["source"] == "Wise")].copy()
        if payouts.empty:
            return None, deposits
        payouts["date"] = _local(payouts["issued_at"])
        payouts["matched_wise_date"] = pd.NaT
        used = set()
        for i, p in payouts.iterrows():
            cand = deposits[(~deposits["id"].isin(used))
                            & ((deposits["amount_base"] - p["net"]).abs() < 0.01)
                            & ((deposits["date"] - p["date"]).abs() <= pd.Timedelta(days=7))]
            if not cand.empty:
                used.add(cand.iloc[0]["id"])
                payouts.at[i, "matched_wise_date"] = cand.iloc[0]["date"]
        payouts["status_check"] = payouts["matched_wise_date"].notna().map({True: "Matched", False: "Not found in Wise"})
        return payouts.sort_values("date", ascending=False), deposits[~deposits["id"].isin(used)]

    def wise_balance_history(self):
        w = db.read_df(self.conn, "SELECT date, currency, balance_id, running_balance, amount, amount_base FROM wise_transactions WHERE source = 'api'")
        if w.empty:
            return w
        w["date"] = _local(w["date"]).dt.normalize()
        w = w.sort_values("date")
        daily = w.groupby(["currency", "date"])["running_balance"].last().reset_index()
        return daily
