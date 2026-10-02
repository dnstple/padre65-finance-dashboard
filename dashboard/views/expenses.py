import re

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from common import TYPE_COLORS, TYPE_LABELS, conn, gbp, ledger, period_filter, style_fig
from tracker import categorise


def render():
    st.title("Expenses")
    start, end = period_filter()
    L = ledger()
    cash = L.cash
    if start:
        cash = cash[(cash["date"] >= pd.Timestamp(start)) & (cash["date"] < pd.Timestamp(end) + pd.Timedelta(days=1))]
    if cash.empty:
        st.info("No Wise or manual transactions yet.")
        return

    spend = cash[(cash["type"].isin(["marketing", "fulfilment", "opex", "inventory"])) ]
    spend = spend.assign(cost=-spend["amount_base"], month=spend["date"].dt.to_period("M").astype(str))

    k = st.columns(4)
    for i, t in enumerate(["marketing", "fulfilment", "opex", "inventory"]):
        k[i].metric(TYPE_LABELS[t], gbp(spend.loc[spend["type"] == t, "cost"].sum()))

    st.subheader("Spend by month")
    show_stock = st.toggle("Include stock purchases", value=False,
                           help="Stock purchases are cash out, not P&L costs. Their cost reaches the P&L as COGS when items sell.")
    types = ["marketing", "fulfilment", "opex"] + (["inventory"] if show_stock else [])
    monthly = spend[spend["type"].isin(types)].pivot_table(index="month", columns="type", values="cost", aggfunc="sum", fill_value=0)
    fig = go.Figure()
    for t in types:
        if t in monthly:
            fig.add_bar(x=monthly.index, y=monthly[t], name=TYPE_LABELS[t], marker_color=TYPE_COLORS[t],
                        marker_line_color="rgba(255,255,255,1)", marker_line_width=2,
                        hovertemplate="%{y:£,.0f}<extra>" + TYPE_LABELS[t] + "</extra>")
    fig = style_fig(fig)
    fig.update_layout(barmode="stack")
    st.plotly_chart(fig, width="stretch")

    left, right = st.columns(2)
    with left:
        st.subheader("By category")
        by_cat = (spend.groupby(["category", "type"])["cost"].sum().reset_index().sort_values("cost", ascending=False))
        by_cat["type"] = by_cat["type"].map(TYPE_LABELS)
        st.dataframe(by_cat, hide_index=True, width="stretch", column_config={
            "category": "Category", "type": "Group", "cost": st.column_config.NumberColumn("Spend", format="£%.2f")})
    with right:
        st.subheader("Top payees")
        payees = spend.groupby("counterparty")["cost"].agg(["sum", "count"]).sort_values("sum", ascending=False).head(15).reset_index()
        st.dataframe(payees, hide_index=True, width="stretch", column_config={
            "counterparty": "Payee", "sum": st.column_config.NumberColumn("Spend", format="£%.2f"), "count": "Payments"})

    st.divider()
    _review(L)


def _review(L):
    st.header("Review & categorise transactions")
    c = conn()
    cats = categorise.categories(c)["category"].tolist()
    f1, f2, f3 = st.columns([2, 2, 1])
    cat_filter = f1.multiselect("Category", cats, default=["Uncategorised", "Uncategorised Income"], key="rev_cat")
    search = f2.text_input("Search payee / description", key="rev_search")
    source = f3.selectbox("Source", ["All", "Wise", "Manual"], key="rev_source")

    tx = L.cash.copy()
    if cat_filter:
        tx = tx[tx["category"].isin(cat_filter)]
    if search:
        mask = tx["counterparty"].fillna("").str.contains(search, case=False, regex=False) | \
            tx["description"].fillna("").str.contains(search, case=False, regex=False)
        tx = tx[mask]
    if source != "All":
        tx = tx[tx["source"].str.startswith(source)]
    tx = tx.sort_values("date", ascending=False)[["id", "date", "counterparty", "description", "amount_base", "source", "category"]]
    st.caption(f"{len(tx)} transactions")

    edited = st.data_editor(tx, hide_index=True, width="stretch", key="rev_editor",
                            disabled=["id", "date", "counterparty", "description", "amount_base", "source"],
                            column_config={
                                "id": None, "date": st.column_config.DatetimeColumn("Date", format="D MMM YYYY"),
                                "counterparty": "Payee / payer", "description": "Description",
                                "amount_base": st.column_config.NumberColumn("Amount", format="£%.2f"),
                                "source": "Source",
                                "category": st.column_config.SelectboxColumn("Category", options=cats, required=True),
                            })
    changed = edited[edited["category"] != tx["category"]]
    if len(changed) and st.button(f"Save {len(changed)} category change(s)", type="primary"):
        for r in changed.to_dict("records"):
            if r["id"].startswith("manual:"):
                c.execute("UPDATE manual_transactions SET category = ? WHERE id = ?", (r["category"], int(r["id"].split(":")[1])))
            else:
                c.execute("INSERT OR REPLACE INTO category_overrides (txn_id, category) VALUES (?, ?)", (r["id"], r["category"]))
        c.commit()
        st.success("Saved")
        st.rerun()

    with st.expander("Add an auto-categorisation rule"):
        st.caption("Rules match text in the payee, merchant, description or reference (case-insensitive) and apply "
                   "to all Wise transactions, past and future. Manual category changes always win over rules.")
        with st.form("new_rule", clear_on_submit=True):
            r1, r2, r3 = st.columns([3, 2, 1])
            pattern = r1.text_input("Text to match (e.g. `factory ltd`, or a regex like `uber|bolt`)")
            category = r2.selectbox("Category", cats)
            direction = r3.selectbox("Direction", ["out", "in", "any"])
            if st.form_submit_button("Add rule"):
                try:
                    re.compile(pattern)
                except re.error as e:
                    st.error(f"Invalid pattern: {e}")
                else:
                    if pattern.strip():
                        c.execute("INSERT INTO category_rules (priority, pattern, category, direction, field) VALUES (15, ?, ?, ?, 'text')",
                                  (pattern.strip(), category, direction))
                        c.commit()
                        st.success("Rule added")
                        st.rerun()
