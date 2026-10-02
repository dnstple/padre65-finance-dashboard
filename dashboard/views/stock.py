from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from common import SERIES, conn, editor, gbp, ledger, metric, style_fig, table
from tracker import db

MONEY = lambda label: st.column_config.NumberColumn(label, format="£%.2f")  # noqa: E731


def render():
    st.title("Stock")
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

    fig = go.Figure()
    parts = [("Sold (COGS)", k["sold"], SERIES[0]), ("Usable stock", k["usable"], SERIES[2]),
             ("Written off", k["written_off"], SERIES[1]), ("Unaccounted", max(k["gap"], 0), "rgba(128,128,128,0.45)")]
    for label, value, colour in parts:
        if value > 0.5:
            fig.add_bar(y=["Stock bought"], x=[value], name=label, orientation="h", marker_color=colour,
                        marker_line_color="rgba(255,255,255,1)", marker_line_width=2,
                        text=[f"{label} {gbp(value)}"], textposition="inside", insidetextanchor="middle",
                        hovertemplate=f"{label}: %{{x:£,.0f}}<extra></extra>")
    fig = style_fig(fig, height=150)
    fig.update_layout(barmode="stack", hovermode="closest", showlegend=False, margin=dict(t=10, b=10))
    fig.update_yaxes(tickprefix="", showticklabels=False)
    fig.update_xaxes(tickprefix="£", tickformat=",.0f")
    st.plotly_chart(fig, width="stretch")
    st.caption("Everything you've paid for stock (at cost) should be either sold, still usable in Shopify, or written off. "
               "**Unaccounted** is what's left: usually unusable stock that hasn't been written off yet, samples or gifts, "
               "or unit costs in the cost sheet that differ from what was actually paid.")
    if k["uncosted_units"]:
        st.caption(f"{k['uncosted_units']} unit(s) in Shopify have no unit cost, so they're valued at £0 here.")

    st.subheader("Record a write-off")
    st.markdown("Write off stock that can't be sold (damaged, faulty, samples, gifted). It becomes a cost in the P&L "
                "on the date you choose. Cash isn't affected, because that money left when you paid the supplier.")
    with st.form("writeoff", clear_on_submit=True):
        a, b, d = st.columns([1, 1, 2])
        when = a.date_input("Date", date.today())
        amount = b.number_input("Amount £ (at cost)", min_value=0.0, value=float(max(round(k["gap"], 2), 0)), step=10.0,
                                format="%.2f", help="Pre-filled with the unaccounted amount. Change it if only part is unusable.")
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
        st.markdown("**Write-offs**")
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

    st.subheader("Usable stock by product")
    table(k["on_hand"][["product_title", "units", "unit_cost", "value"]], hide_index=True, width="stretch", column_config={
        "product_title": "Product", "units": "Units", "unit_cost": MONEY("Unit cost"),
        "value": MONEY("Stock value")})
