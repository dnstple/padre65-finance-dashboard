from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from common import SERIES, conn, editor, gbp, heading, ledger, metric, pct, period_filter, style_fig, table
from glossary import GLOSSARY
from tracker import db

MONEY = lambda label: st.column_config.NumberColumn(label, format="£%.2f")  # noqa: E731
LEVELS = ["Grouped", "Product", "Variant"]


def _per_unit_chart(pc, by, name_cols):
    """Each bar = average price actually paid per unit, split into what the unit cost and what you kept."""
    d = pc[pc["avg_price_paid"].notna() & pc["unit_cost"].notna()].head(20).iloc[::-1]
    if d.empty:
        return
    if by == "Variant":
        labels = d["product_title"] + " · " + d["variant_title"].fillna("")
    else:
        labels = d[name_cols[0]]
    heading("What each unit sells for, and what you keep")
    fig = go.Figure()
    fig.add_bar(y=labels, x=d["unit_cost"], name="Unit cost", orientation="h", marker_color=SERIES[1],
                marker_line_color="rgba(255,255,255,1)", marker_line_width=2,
                hovertemplate="%{x:£,.2f}<extra>Unit cost</extra>")
    fig.add_bar(y=labels, x=d["cm1_per_unit"], name="Gross profit per unit", orientation="h", marker_color=SERIES[0],
                marker_line_color="rgba(255,255,255,1)", marker_line_width=2,
                text=[f"{gbp(v, 2)} · {p:.0%}" for v, p in zip(d["cm1_per_unit"], d["cm1_pct"].fillna(0))],
                textposition="outside", cliponaxis=False,
                hovertemplate="%{x:£,.2f}<extra>Gross profit per unit</extra>")
    fig = style_fig(fig, height=max(280, 32 * len(d) + 80))
    fig.update_layout(barmode="stack", hovermode="y unified", margin=dict(r=110))
    fig.update_yaxes(tickprefix="", gridcolor="rgba(0,0,0,0)")
    fig.update_xaxes(tickprefix="£", tickformat=",.0f")
    st.plotly_chart(fig, width="stretch")
    st.caption("Bar length = average price actually paid per unit. Labels show gross profit per unit and as a % of the price.")


def render():
    heading("Products & contribution", "title")
    start, end = period_filter()
    L = ledger()
    if st.session_state.get("prod_level") not in (None, *LEVELS):
        st.session_state.pop("prod_level")
    by = st.segmented_control("Level", LEVELS, default="Grouped", key="prod_level",
                              help="Grouped = products that are the same thing commercially (same type, unit cost and "
                                   "price) joined together, e.g. every Club Long Sleeve colour or the £50 caps. "
                                   "Product = each Shopify product. Variant = each size.") or "Grouped"
    pc = L.grouped_contribution(start, end) if by == "Grouped" else L.product_contribution(start, end, by=by.lower())
    if pc.empty:
        st.info("No sales in this period.")
        return

    missing = pc[pc["missing_cost"]]
    if len(missing):
        noun = {"Grouped": "group", "Product": "product", "Variant": "variant"}[by] + ("s have" if len(missing) != 1 else " has")
        st.warning(f"{len(missing)} {noun} no unit cost, so COGS is £0 and margin is overstated. "
                   "Add costs on **Data & sync**.")

    name_cols = {"Grouped": ["group", "products", "includes"], "Product": ["product_title"],
                 "Variant": ["product_title", "variant_title", "sku"]}[by]
    names = {"group": "Product group", "products": "Products", "includes": "Includes", "product_title": "Product",
             "variant_title": "Variant", "sku": "SKU"}

    heading("Margin per unit")
    st.caption("What one unit sells for and earns. **At full price** uses today's price; **actual** uses what customers "
               "really paid on average, after discounts and returns.")
    table(pc[name_cols + ["net_units", "full_price", "unit_cost", "full_price_margin", "avg_price_paid", "cm1_per_unit",
                          "cm1_pct", "cm2_per_unit", "cm3_per_unit", "missing_cost"]],
          hide_index=True, width="stretch", column_config={
              **names, "net_units": "Units sold (net)", "full_price": MONEY("Full price"), "unit_cost": MONEY("Unit cost"),
              "full_price_margin": MONEY("Margin per unit at full price"), "avg_price_paid": MONEY("Avg price paid"),
              "cm1_per_unit": MONEY("Gross profit per unit"), "cm1_pct": st.column_config.NumberColumn("CM1 %", format="percent"),
              "cm2_per_unit": MONEY("CM2 per unit"), "cm3_per_unit": MONEY("CM3 per unit"),
              "missing_cost": st.column_config.CheckboxColumn("No cost")})

    _per_unit_chart(pc, by, name_cols)

    heading("Contribution totals")
    table(pc[name_cols + ["units", "units_returned", "net_revenue", "cogs", "cm1", "cm1_pct", "alloc_fulfilment_fees",
                          "cm2", "cm2_pct", "alloc_marketing", "cm3", "cm3_pct"]],
          hide_index=True, width="stretch", column_config={
              **names, "units": "Units", "units_returned": "Returned",
              "net_revenue": st.column_config.NumberColumn("Net revenue", format="£%.2f", help=GLOSSARY["Net revenue (product)"]),
              "cogs": MONEY("COGS"), "cm1": MONEY("Gross profit (CM1)"),
              "cm1_pct": st.column_config.NumberColumn("CM1 %", format="percent"),
              "alloc_fulfilment_fees": MONEY("Fees & fulfilment (alloc.)"), "cm2": MONEY("CM2"),
              "alloc_marketing": MONEY("Marketing (alloc.)"), "cm3": MONEY("CM3"),
              "cm2_pct": st.column_config.NumberColumn("CM2 %", format="percent"),
              "cm3_pct": st.column_config.NumberColumn("CM3 %", format="percent")})
    st.caption("CM1 is exact. Fees, fulfilment and marketing are shared costs, so they're allocated to products "
               "by share of net revenue for the period.")

    st.divider()
    _product_detail(L)


def _product_detail(L):
    heading("Product log", "header")
    c = conn()
    variants = db.read_df(c, "SELECT * FROM shopify_variants")
    if variants.empty:
        st.info("Sync Shopify to see products.")
        return
    products = variants.groupby(["product_id", "product_title"], as_index=False).agg(stock=("inventory_quantity", "sum"))
    sold_ids = L.lines.groupby("product_id")["quantity"].sum()
    products["sold"] = products["product_id"].map(sold_ids).fillna(0)
    products = products.sort_values(["sold", "product_title"], ascending=[False, True])
    choice = st.selectbox("Product", products["product_title"].tolist(), key="log_product",
                          help="Pick a product to see its sales, stock and its log of restocks, cost changes and notes.")
    prod = products[products["product_title"] == choice].iloc[0]
    pid = prod["product_id"]

    lines = L.lines[L.lines["product_id"] == pid]
    cost_row = c.execute("SELECT unit_cost, collection, notes FROM product_costs WHERE key = ?", (f"product:{pid}",)).fetchone()
    k = st.columns(5)
    metric(k[0], "Units sold (all time)", int(lines["quantity"].sum()))
    metric(k[1], "Net revenue", gbp(lines["net"].sum()))
    metric(k[2], "Current unit cost", gbp(cost_row["unit_cost"], 2) if cost_row else "Not set")
    metric(k[3], "Gross margin", pct((lines["net"].sum() - lines["cogs"].sum()) / lines["net"].sum()) if lines["net"].sum() else "–")
    metric(k[4], "In stock now", int(prod["stock"] or 0))

    left, right = st.columns([3, 2])
    with left:
        if not lines.empty:
            m = lines.assign(month=lines["date"].dt.to_period("M").astype(str)).groupby("month")["quantity"].sum().reset_index()
            fig = go.Figure(go.Bar(x=m["month"], y=m["quantity"], marker_color=SERIES[0], name="Units",
                                   hovertemplate="%{x}: %{y} units<extra></extra>"))
            fig = style_fig(fig, height=260, money=False)
            fig.update_layout(title=dict(text="Units sold by month", font=dict(size=14)))
            st.plotly_chart(fig, width="stretch")
        stock = variants[variants["product_id"] == pid][["variant_title", "sku", "price", "inventory_quantity"]]
        table(stock, hide_index=True, width="stretch", column_config={
            "variant_title": "Variant", "sku": "SKU", "price": MONEY("Price"), "inventory_quantity": "In stock"})

    with right:
        with st.form("log_entry", clear_on_submit=True):
            heading("Add log entry", "label")
            event = st.selectbox("Type", ["restock", "cost_change", "note"],
                                 format_func={"restock": "Restock", "cost_change": "Cost change", "note": "Note"}.get)
            d = st.date_input("Date", date.today())
            qty = st.number_input("Quantity (restock)", min_value=0, step=1)
            unit_cost = st.number_input("Unit cost £ (landed). Leave at 0 to keep the current cost", min_value=0.0, step=0.5, format="%.2f")
            supplier = st.text_input("Supplier")
            notes = st.text_area("Notes", height=80)
            if st.form_submit_button("Save entry", type="primary"):
                uc = unit_cost or None
                c.execute("""INSERT INTO product_log (date, product_id, product_title, event, quantity, unit_cost,
                             total_cost, supplier, notes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                          (d.isoformat(), pid, choice, event, qty or None, uc, (qty * uc) if (qty and uc) else None,
                           supplier, notes, db.now_iso()))
                c.commit()
                st.success("Saved")
                st.rerun()

    log = db.read_df(c, "SELECT id, date, event, quantity, unit_cost, total_cost, supplier, notes FROM product_log WHERE product_id = ? ORDER BY date DESC", (pid,))
    heading("History", "label")
    if log.empty:
        st.caption("No entries yet. Log restocks, cost changes and notes here.")
    else:
        edited = editor(log, hide_index=True, width="stretch", num_rows="dynamic", key=f"log_{pid}",
                                disabled=["id"], column_config={
                                    "id": None, "event": st.column_config.SelectboxColumn("Type", options=["restock", "cost_change", "note"]),
                                    "unit_cost": MONEY("Unit cost"), "total_cost": MONEY("Total cost")})
        if st.button("Save changes to history"):
            removed = set(log["id"]) - set(edited["id"].dropna())
            for rid in removed:
                c.execute("DELETE FROM product_log WHERE id = ?", (int(rid),))
            for r in edited.dropna(subset=["id"]).to_dict("records"):
                c.execute("""UPDATE product_log SET date=?, event=?, quantity=?, unit_cost=?, total_cost=?, supplier=?, notes=?
                             WHERE id=?""", (str(r["date"])[:10], r["event"], _n(r["quantity"]), _n(r["unit_cost"]),
                                             _n(r["total_cost"]), r["supplier"], r["notes"], int(r["id"])))
            c.commit()
            st.success("History updated")
            st.rerun()


def _n(v):
    return None if v is None or pd.isna(v) else float(v)
