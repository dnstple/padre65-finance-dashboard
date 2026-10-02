import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from common import SERIES, conn, gbp, ledger, period_filter, style_fig, table
from tracker import db


def render():
    st.title("Cash & Wise")
    start, end = period_filter()
    L = ledger()
    c = conn()

    balances = db.read_df(c, "SELECT currency, name, type, amount, updated_at FROM wise_balances ORDER BY amount DESC")
    if not balances.empty:
        cols = st.columns(min(len(balances), 5))
        for i, b in enumerate(balances.head(5).to_dict("records")):
            label = f"{b['currency']} balance" + (f" · {b['name']}" if b["type"] == "SAVINGS" else "")
            cols[i].metric(label, f"{b['amount']:,.2f} {b['currency']}")
        st.caption(f"Balances as of last sync: {balances['updated_at'].max()[:16].replace('T', ' ')} UTC")

    hist = L.wise_balance_history()
    if not hist.empty:
        st.subheader("Wise balance over time")
        fig = go.Figure()
        for i, (cur, h) in enumerate(hist.groupby("currency")):
            fig.add_scatter(x=h["date"], y=h["running_balance"], name=cur, mode="lines", line=dict(color=SERIES[i % 8], width=2),
                            line_shape="hv", hovertemplate="%{y:,.2f} " + cur + "<extra></extra>")
        fig = style_fig(fig, height=300)
        if hist["currency"].nunique() == 1:
            fig.update_layout(showlegend=False)
        st.plotly_chart(fig, width="stretch")

    cf = L.cash_flow(start, end)
    if not cf.empty:
        st.subheader("Cash in and out by month")
        tx = L.cash
        if start:
            tx = tx[(tx["date"] >= pd.Timestamp(start)) & (tx["date"] < pd.Timestamp(end) + pd.Timedelta(days=1))]
        tx = tx.assign(month=tx["date"].dt.to_period("M").astype(str))
        inflow = tx[tx["amount_base"] > 0].groupby("month")["amount_base"].sum()
        outflow = -tx[tx["amount_base"] < 0].groupby("month")["amount_base"].sum()
        frame = pd.DataFrame({"In": inflow, "Out": outflow}).fillna(0)
        frame["Net"] = frame["In"] - frame["Out"]
        frame = frame.reset_index(names="month")
        fig = go.Figure()
        fig.add_bar(x=frame["month"], y=frame["In"], name="Money in", marker_color=SERIES[0], hovertemplate="%{y:£,.0f}<extra>In</extra>")
        fig.add_bar(x=frame["month"], y=frame["Out"], name="Money out", marker_color=SERIES[1], hovertemplate="%{y:£,.0f}<extra>Out</extra>")
        fig.add_scatter(x=frame["month"], y=frame["Net"], name="Net cash flow", mode="lines+markers",
                        line=dict(color=SERIES[2], width=2), marker=dict(size=8),
                        hovertemplate="%{y:£,.0f}<extra>Net</extra>")
        st.plotly_chart(style_fig(fig), width="stretch")
        st.caption("Includes everything that moved cash, including stock purchases, owner money and Shopify payouts. "
                   "Manual entries are included, so this isn't only Wise.")

    st.divider()
    st.subheader("Shopify payouts → Wise reconciliation")
    payouts, unmatched = L.payout_reconciliation()
    if payouts is None:
        st.info("Shopify Payments payout data isn't available yet. Add the `read_shopify_payments_payouts` scope "
                "to the Shopify app to check every payout arrived in Wise.")
        if not unmatched.empty:
            st.caption(f"Shopify deposits seen in Wise: {len(unmatched)} totalling {gbp(unmatched['amount_base'].sum(), 2)}")
    else:
        missing = payouts[payouts["status_check"] != "Matched"]
        (st.success if missing.empty else st.warning)(
            f"{len(payouts) - len(missing)} of {len(payouts)} payouts matched to a Wise deposit." +
            (f" {len(missing)} not found ({gbp(missing['net'].sum(), 2)})." if len(missing) else ""))
        table(payouts[["date", "net", "status", "matched_wise_date", "status_check"]], hide_index=True, width="stretch",
                     column_config={"date": st.column_config.DatetimeColumn("Payout date", format="D MMM YYYY"),
                                    "net": st.column_config.NumberColumn("Amount", format="£%.2f"),
                                    "matched_wise_date": st.column_config.DatetimeColumn("Arrived in Wise", format="D MMM YYYY"),
                                    "status": "Shopify status", "status_check": "Check"})
