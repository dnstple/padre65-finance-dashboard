from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from common import BG, SERIES, conn, editor, gbp, heading, ledger, metric, pct, style_fig, table
from tracker import db

MONEY = lambda label: st.column_config.NumberColumn(label, format="£%.2f")  # noqa: E731


def render():
    heading("Stock", "title")
    tab_check, tab_history = st.tabs(["📦 Stock check", "🗂️ Restocks & cost changes"])
    with tab_check:
        _stock_check()
    with tab_history:
        _product_history()


def _stock_check():
    c = conn()
    L = ledger()
    k = L.stock_check()

    plain = dict(delta_color="off", delta_arrow="off", border=True)
    row = st.columns(3)
    metric(row[0], "Stock bought", gbp(k["bought"]), "paid to suppliers, at cost", **plain)
    metric(row[1], "Stock sold", gbp(k["sold"]), "COGS so far", **plain)
    metric(row[2], "Usable stock", gbp(k["usable"]), f"{k['units']} units in Shopify", **plain)
    row = st.columns(3)
    metric(row[0], "Written off", gbp(k["written_off"]), "unusable stock recorded", **plain)
    metric(row[1], "Unaccounted", gbp(k["gap"]), "not yet explained", **plain)
    metric(row[2], "Usable stock at full price", gbp(k["retail"]),
           f"≈ {gbp(k['retail'] - k['usable'])} gross profit if sold at full price", **plain)

    fig = go.Figure()
    parts = [("Sold (COGS)", k["sold"], SERIES[0]), ("Usable stock", k["usable"], SERIES[2]),
             ("Written off", k["written_off"], SERIES[1]), ("Unaccounted", max(k["gap"], 0), "rgba(128,128,128,0.45)")]
    for label, value, colour in parts:
        if value > 0.5:
            fig.add_bar(y=["Stock bought"], x=[value], name=label, orientation="h", marker_color=colour,
                        marker_line_color=BG, marker_line_width=2,
                        text=[f"{label} {gbp(value)}"], textposition="inside", insidetextanchor="middle",
                        hovertemplate=f"{label}: %{{x:£,.0f}}<extra></extra>")
    fig = style_fig(fig, height=150)
    fig.update_layout(barmode="stack", hovermode="closest", showlegend=False, margin=dict(t=10, b=10))
    fig.update_yaxes(tickprefix="", showticklabels=False)
    fig.update_xaxes(tickprefix="£", tickformat=",.0f")
    st.plotly_chart(fig, width="stretch")
    st.caption("Everything you've paid for stock (at cost) should be either sold, still usable in Shopify, or written off. "
               "**Unaccounted** is what's left. It can be unusable stock not yet written off, stock payments that were really "
               "samples or development, or unit costs in the cost sheet that are lower than what was actually paid.")
    if k["uncosted_units"]:
        st.caption(f"{k['uncosted_units']} unit(s) in Shopify have no unit cost, so they're valued at £0 here.")

    heading("Record a write-off", "subheader")
    st.markdown("Write off stock that can't be sold (damaged, faulty, samples, gifted). It becomes a cost in the P&L "
                "on the date you choose. Cash isn't affected, because that money left when you paid the supplier.")
    with st.form("writeoff", clear_on_submit=True):
        a, b, d = st.columns([1, 1, 2])
        when = a.date_input("Date", date.today())
        amount = b.number_input("Amount £ (at cost)", min_value=0.0, value=0.0, step=10.0, format="%.2f",
                                help="What the unusable stock cost you. Only write off stock you know can't be sold.")
        reason = d.text_input("Reason", placeholder="e.g. Unusable stock: misprints and faulty items")
        products = ["(whole stock / several products)"] + sorted(k["on_hand"]["product_title"].tolist())
        e, f = st.columns([3, 1])
        product = e.selectbox("Product (optional)", products)
        qty = f.number_input("Units (optional)", min_value=0, step=1)
        if st.form_submit_button("Record write-off", type="primary"):
            if amount <= 0:
                st.error("Enter an amount above zero.")
            else:
                pid = None
                if product != products[0]:
                    pid = k["on_hand"].loc[k["on_hand"]["product_title"] == product, "product_id"].iloc[0]
                c.execute("""INSERT INTO stock_writeoffs (date, amount, quantity, product_id, product_title, reason, created_at)
                             VALUES (?, ?, ?, ?, ?, ?, ?)""",
                          (when.isoformat(), amount, qty or None, pid, None if pid is None else product, reason, db.now_iso()))
                c.commit()
                st.success(f"Recorded a {gbp(amount, 2)} write-off")
                st.rerun()

    wo = db.read_df(c, "SELECT id, date, amount, quantity, product_title, reason FROM stock_writeoffs ORDER BY date DESC")
    if not wo.empty:
        heading("Write-offs", "label")
        wo["date"] = pd.to_datetime(wo["date"]).dt.date
        edited = editor(wo, hide_index=True, width="stretch", num_rows="dynamic", key="wo_editor", column_config={
            "id": None, "date": st.column_config.DateColumn("Date", format="D MMM YYYY", required=True),
            "amount": MONEY("Amount"), "quantity": "Units", "product_title": "Product", "reason": "Reason"})
        if st.button("Save changes to write-offs"):
            kept = edited.dropna(subset=["id"])
            for rid in set(wo["id"]) - set(kept["id"]):
                c.execute("DELETE FROM stock_writeoffs WHERE id = ?", (int(rid),))
            for r in kept.to_dict("records"):
                c.execute("UPDATE stock_writeoffs SET date=?, amount=?, quantity=?, reason=? WHERE id=?",
                          (str(r["date"])[:10], float(r["amount"] or 0), None if pd.isna(r["quantity"]) else int(r["quantity"]),
                           r["reason"], int(r["id"])))
            c.commit()
            st.rerun()

    heading("Usable stock by product", "subheader")
    table(k["on_hand"][["product_title", "units", "unit_cost", "value", "retail"]], hide_index=True, width="stretch", column_config={
        "product_title": "Product", "units": "Units", "unit_cost": MONEY("Unit cost"),
        "value": MONEY("Stock value"), "retail": MONEY("Full-price value")})


def _product_history():
    L = ledger()
    c = conn()
    variants = db.read_df(c, "SELECT * FROM shopify_variants")
    if variants.empty:
        st.info("Sync Shopify to see products.")
        return
    st.caption("Sales, stock and the history of each product. Log restocks, cost changes and notes here. A restock "
               "or cost change with a new unit cost updates COGS for sales from that date.")
    products = variants.groupby(["product_id", "product_title"], as_index=False).agg(
        stock=("inventory_quantity", "sum"), price=("price", "max"))
    sold_ids = L.lines.groupby("product_id")["quantity"].sum()
    products["sold"] = products["product_id"].map(sold_ids).fillna(0)
    products = products.sort_values(["sold", "product_title"], ascending=[False, True])
    choice = st.selectbox("Product", products["product_title"].tolist(), key="history_product",
                          help="Pick a product to see its sales, stock and its log of restocks, cost changes and notes.")
    prod = products[products["product_title"] == choice].iloc[0]
    pid = prod["product_id"]

    lines = L.lines[L.lines["product_id"] == pid]
    cost_row = c.execute("SELECT unit_cost FROM product_costs WHERE key = ?", (f"product:{pid}",)).fetchone()
    unit_cost = cost_row["unit_cost"] if cost_row else None
    plain = dict(border=True)
    k = st.columns(3)
    metric(k[0], "Units sold (all time)", int(lines["quantity"].sum()), **plain)
    metric(k[1], "Net revenue", gbp(lines["net"].sum()), **plain)
    metric(k[2], "In stock now", int(prod["stock"] or 0), **plain)
    k = st.columns(3)
    metric(k[0], "Full price", gbp(prod["price"], 2), **plain)
    metric(k[1], "Current unit cost", gbp(unit_cost, 2) if unit_cost is not None else "Not set", **plain)
    metric(k[2], "Margin per unit at full price",
           gbp(prod["price"] - unit_cost, 2) if unit_cost is not None else "–",
           (pct((prod["price"] - unit_cost) / prod["price"]) + " of price") if unit_cost is not None and prod["price"] else "",
           delta_color="off", delta_arrow="off", **plain)

    left, right = st.columns([3, 2])
    with left:
        if not lines.empty:
            m = lines.assign(month=lines["date"].dt.to_period("M").dt.start_time).groupby("month")["quantity"].sum().reset_index()
            heading("Units sold by month")
            fig = go.Figure(go.Bar(x=m["month"], y=m["quantity"], marker_color=SERIES[0], name="Units",
                                   hovertemplate="%{x|%b %Y}: %{y} units<extra></extra>"))
            fig = style_fig(fig, height=240, money=False)
            fig.update_layout(hovermode="closest")
            st.plotly_chart(fig, width="stretch")
        heading("Stock by size")
        stock = variants[variants["product_id"] == pid][["variant_title", "sku", "price", "inventory_quantity"]]
        table(stock, hide_index=True, width="stretch", column_config={
            "variant_title": "Variant", "sku": "SKU", "price": MONEY("Price"), "inventory_quantity": "In stock"})

    with right:
        with st.form("log_entry", clear_on_submit=True):
            heading("Add log entry", "label")
            event = st.selectbox("Type", ["restock", "cost_change", "note"],
                                 format_func={"restock": "Restock", "cost_change": "Cost change", "note": "Note"}.get)
            d = st.date_input("Date", date.today(), format="DD/MM/YYYY")
            qty = st.number_input("Quantity (restock)", min_value=0, step=1)
            new_cost = st.number_input("Unit cost £ (landed). Leave at 0 to keep the current cost", min_value=0.0,
                                       step=0.5, format="%.2f")
            supplier = st.text_input("Supplier")
            notes = st.text_area("Notes", height=80)
            if st.form_submit_button("Save entry", type="primary"):
                uc = new_cost or None
                c.execute("""INSERT INTO product_log (date, product_id, product_title, event, quantity, unit_cost,
                             total_cost, supplier, notes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                          (d.isoformat(), pid, choice, event, qty or None, uc, (qty * uc) if (qty and uc) else None,
                           supplier, notes, db.now_iso()))
                c.commit()
                st.success("Saved")
                st.rerun()

    log = db.read_df(c, "SELECT id, date, event, quantity, unit_cost, total_cost, supplier, notes FROM product_log "
                        "WHERE product_id = ? ORDER BY date DESC", (pid,))
    heading("History", "label")
    if log.empty:
        st.caption("No entries yet. Log restocks, cost changes and notes here.")
        return
    edited = editor(log, hide_index=True, width="stretch", num_rows="dynamic", key=f"log_{pid}", disabled=["id"],
                    column_config={"id": None,
                                   "event": st.column_config.SelectboxColumn("Type", options=["restock", "cost_change", "note"]),
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
