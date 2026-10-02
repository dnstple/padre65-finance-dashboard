from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from common import SERIES, conn, gbp, ledger, pct, period_filter, style_fig
from tracker import db

MONEY = lambda label: st.column_config.NumberColumn(label, format="£%.2f")  # noqa: E731


def render():
    st.title("Products & contribution")
    start, end = period_filter()
    L = ledger()
    by = st.segmented_control("Level", ["Product", "Variant"], default="Product", key="prod_level") or "Product"
    pc = L.product_contribution(start, end, by=by.lower())
    if pc.empty:
        st.info("No sales in this period.")
        return

    missing = pc[pc["missing_cost"]]
    if len(missing):
        noun = by.lower() + ("s have" if len(missing) != 1 else " has")
        st.warning(f"{len(missing)} {noun} no unit cost, so COGS is £0 and margin is overstated. "
                   "Add costs on **Data & sync**.")

    name_cols = ["product_title"] if by == "Product" else ["product_title", "variant_title", "sku"]
    table = pc[name_cols + ["units", "units_returned", "net_revenue", "unit_cost", "cogs", "cm1", "cm1_pct",
                            "alloc_fulfilment_fees", "cm2", "alloc_marketing", "cm3", "cm3_pct", "missing_cost"]]
    st.dataframe(table, hide_index=True, width="stretch", column_config={
        "product_title": "Product", "variant_title": "Variant", "sku": "SKU",
        "units": "Units", "units_returned": "Returned",
        "net_revenue": MONEY("Net revenue"), "unit_cost": MONEY("Unit cost"), "cogs": MONEY("COGS"),
        "cm1": MONEY("Gross profit (CM1)"),
        "cm1_pct": st.column_config.NumberColumn("CM1 %", format="percent"),
        "alloc_fulfilment_fees": MONEY("Fees & fulfilment (alloc.)"), "cm2": MONEY("CM2"),
        "alloc_marketing": MONEY("Marketing (alloc.)"), "cm3": MONEY("CM3"),
        "cm3_pct": st.column_config.NumberColumn("CM3 %", format="percent"),
        "missing_cost": st.column_config.CheckboxColumn("No cost"),
    })
    st.caption("CM1 is exact. Fees, fulfilment and marketing are shared costs, so they're allocated to products "
               "by share of net revenue for the period.")

    top = pc.head(15).iloc[::-1]
    labels = top["product_title"] if by == "Product" else top["product_title"] + " · " + top["variant_title"].fillna("")
    fig = go.Figure()
    fig.add_bar(y=labels, x=top["cogs"], name="COGS", orientation="h", marker_color=SERIES[1],
                hovertemplate="%{x:£,.0f}<extra>COGS</extra>")
    fig.add_bar(y=labels, x=top["cm1"], name="Gross profit", orientation="h", marker_color=SERIES[0],
                hovertemplate="%{x:£,.0f}<extra>Gross profit</extra>")
    fig = style_fig(fig, height=max(280, 30 * len(top) + 80))
    fig.update_layout(barmode="stack", hovermode="y unified")
    fig.update_yaxes(tickprefix="", gridcolor="rgba(0,0,0,0)")
    fig.update_xaxes(tickprefix="£", tickformat=",.0f")
    st.subheader("Revenue split: cost vs gross profit")
    st.plotly_chart(fig, width="stretch")

    st.divider()
    _product_detail(L)


def _product_detail(L):
    st.header("Product log")
    c = conn()
    variants = db.read_df(c, "SELECT * FROM shopify_variants")
    if variants.empty:
        st.info("Sync Shopify to see products.")
        return
    products = variants.groupby(["product_id", "product_title"], as_index=False).agg(stock=("inventory_quantity", "sum"))
    sold_ids = L.lines.groupby("product_id")["quantity"].sum()
    products["sold"] = products["product_id"].map(sold_ids).fillna(0)
    products = products.sort_values(["sold", "product_title"], ascending=[False, True])
    choice = st.selectbox("Product", products["product_title"].tolist(), key="log_product")
    prod = products[products["product_title"] == choice].iloc[0]
    pid = prod["product_id"]

    lines = L.lines[L.lines["product_id"] == pid]
    cost_row = c.execute("SELECT unit_cost, collection, notes FROM product_costs WHERE key = ?", (f"product:{pid}",)).fetchone()
    k = st.columns(5)
    k[0].metric("Units sold (all time)", int(lines["quantity"].sum()))
    k[1].metric("Net revenue", gbp(lines["net"].sum()))
    k[2].metric("Current unit cost", gbp(cost_row["unit_cost"], 2) if cost_row else "Not set")
    k[3].metric("Gross margin", pct((lines["net"].sum() - lines["cogs"].sum()) / lines["net"].sum()) if lines["net"].sum() else "–")
    k[4].metric("In stock now", int(prod["stock"] or 0))

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
        st.dataframe(stock, hide_index=True, width="stretch", column_config={
            "variant_title": "Variant", "sku": "SKU", "price": MONEY("Price"), "inventory_quantity": "In stock"})

    with right:
        with st.form("log_entry", clear_on_submit=True):
            st.markdown("**Add log entry**")
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
    st.markdown("**History**")
    if log.empty:
        st.caption("No entries yet. Log restocks, cost changes and notes here.")
    else:
        edited = st.data_editor(log, hide_index=True, width="stretch", num_rows="dynamic", key=f"log_{pid}",
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
