import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from common import SERIES, bar_line_chart, conn, gbp, ledger, pct, period_filter, style_fig


def _row(p, label):
    return float(p.loc[label, "Total"]) if label in p.index else 0.0


def render():
    st.title("Overview")
    start, end = period_filter()
    L = ledger()
    p = L.pnl(start, end)
    if p.empty:
        st.info("No data yet. Go to **Data & sync** to connect Shopify and Wise, or run `python sync.py demo` to try sample data.")
        return

    net_rev = _row(p, "Net revenue")
    cm1 = _row(p, "Gross profit (CM1)")
    cm3 = _row(p, "Contribution after marketing (CM3)")
    net = _row(p, "Net profit")
    ords = L.orders
    if start:
        ords = ords[(ords["date"] >= pd.Timestamp(start)) & (ords["date"] < pd.Timestamp(end) + pd.Timedelta(days=1))]
    paid = ords[ords["total"] > 0]
    balances = conn().execute("SELECT COALESCE(SUM(amount), 0) FROM wise_balances WHERE currency = 'GBP'").fetchone()[0]

    monthly = L.pnl(start, end, freq="M").drop(columns="Total").T.reset_index(names="month")
    spark = dict(chart_type="bar", border=True)
    plain_delta = dict(delta_color="off", delta_arrow="off")
    plain = dict(plain_delta, border=True)

    def margin(v):
        return pct(v / net_rev if net_rev else None) + " margin"

    c = st.columns(3)
    c[0].metric("Net revenue", gbp(net_rev), "by month", chart_data=monthly["Net revenue"].round(0).tolist(), **spark, **plain_delta)
    c[1].metric("Gross profit (CM1)", gbp(cm1), margin(cm1), chart_data=monthly["Gross profit (CM1)"].round(0).tolist(), **spark, **plain_delta)
    c[2].metric("Net profit", gbp(net), margin(net), chart_data=monthly["Net profit"].round(0).tolist(), **spark, **plain_delta)
    c = st.columns(3)
    c[0].metric("Contribution after marketing (CM3)", gbp(cm3), margin(cm3), **plain)
    c[1].metric("Orders", f"{len(paid)}", gbp(paid["total"].mean() if len(paid) else None, 2) + " average order", **plain)
    c[2].metric("Cash in Wise (GBP)", gbp(balances), "as of last sync", **plain)

    _attention(L)

    left, right = st.columns([3, 2])
    with left:
        st.subheader("Revenue and profit by month")
        st.plotly_chart(bar_line_chart(monthly, "month",
                                       [("Net revenue", "Net revenue"), ("Gross profit (CM1)", "Gross profit")],
                                       ("Net profit", "Net profit")), width="stretch")
    with right:
        st.subheader("Where the money went")
        cost_rows = p[(p["Total"] < 0) & ~p.index.isin(p.attrs.get("subtotals", []))
                      & ~p.index.isin(["Discounts", "Refunds", "VAT", "Shipping refunded", "Other refunds & adjustments"])]
        costs = (-cost_rows["Total"]).sort_values()
        fig = go.Figure(go.Bar(x=costs.values, y=costs.index, orientation="h", marker_color=SERIES[0],
                               text=[gbp(v) for v in costs.values], textposition="outside", cliponaxis=False,
                               hovertemplate="%{y}: %{x:£,.0f}<extra></extra>"))
        fig = style_fig(fig, height=max(240, 34 * len(costs) + 60))
        fig.update_layout(hovermode="closest", margin=dict(r=60))
        fig.update_xaxes(showgrid=False, showticklabels=False, zeroline=False)
        fig.update_yaxes(tickprefix="", showgrid=False)
        st.plotly_chart(fig, width="stretch")

    st.subheader("Top products")
    pc = L.product_contribution(start, end).head(8)
    if not pc.empty:
        st.dataframe(
            pc[["product_title", "units", "net_revenue", "cogs", "cm1", "cm1_pct", "cm3"]],
            hide_index=True, width="stretch",
            column_config={
                "product_title": "Product", "units": "Units",
                "net_revenue": st.column_config.NumberColumn("Net revenue", format="£%.0f"),
                "cogs": st.column_config.NumberColumn("COGS", format="£%.0f"),
                "cm1": st.column_config.NumberColumn("Gross profit", format="£%.0f"),
                "cm1_pct": st.column_config.ProgressColumn("Margin", format="percent", min_value=0, max_value=1),
                "cm3": st.column_config.NumberColumn("CM3 (allocated)", format="£%.0f"),
            },
        )


def _attention(L):
    notes = []
    missing = L.missing_cost_lines
    if len(missing):
        n = missing["product_title"].nunique()
        notes.append(f"**{n} product{'s have' if n != 1 else ' has'} no unit cost**, so COGS shows as £0 and margins are overstated. "
                     "Add costs on **Data & sync**.")
    unc = L.cash[L.cash["category"].isin(["Uncategorised", "Uncategorised Income"])]
    if len(unc):
        notes.append(f"**{len(unc)} transactions are uncategorised** ({gbp(unc['amount_base'].abs().sum())}). "
                     "Review them on **Expenses**.")
    if L.fees_estimated:
        notes.append("Card fees are **estimated**. Add the Shopify Payments scope to use actual fees.")
    if notes:
        with st.container(border=True):
            st.markdown("⚠️ **Needs attention**")
            for n in notes:
                st.markdown(f"- {n}")
