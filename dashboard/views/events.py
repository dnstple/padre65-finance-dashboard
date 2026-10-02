from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from common import SERIES, conn, gbp, ledger, pct, style_fig
from tracker import events

MONEY = lambda label: st.column_config.NumberColumn(label, format="£%.2f")  # noqa: E731
PCT = lambda label: st.column_config.NumberColumn(label, format="percent")  # noqa: E731


def render():
    st.title("Events")
    c = conn()
    evs = events.list_events(c)
    _new_event_form(c)
    if evs.empty:
        st.info("No events yet. Create one above. In-person (POS) orders in the date range are tagged automatically.")
        return

    labels = {int(r.id): f"{r.name} · {r.start_date}" + (f" to {r.end_date}" if r.end_date != r.start_date else "")
              for r in evs.itertuples()}
    event_id = st.selectbox("Event", list(labels), format_func=labels.get, key="event_id")
    L = ledger()
    R = events.EventReport(c, L, event_id)
    if R.orders.empty:
        st.warning("No orders tagged to this event yet. Add them under **Manage this event** below.")
        _manage(c, L, R, event_id)
        return

    k = R.kpis()
    ev = R.event
    st.caption(" · ".join(x for x in [ev.get("location"), ev.get("notes")] if x))

    row = st.columns(4)
    row[0].metric("Net sales", gbp(k["net"]), f"{gbp(k['gross'])} gross",
                  delta_color="off", delta_arrow="off", border=True)
    row[1].metric("Orders", k["orders"], f"{gbp(k['aov'])} AOV", delta_color="off", delta_arrow="off", border=True)
    row[2].metric("Units sold", k["units"], f"{k['units_per_order']:.2f} per order", delta_color="off", delta_arrow="off", border=True)
    row[3].metric("Avg unit price", gbp(k["avg_unit_price"]), f"{pct(k['discount_rate'])} discounted",
                  delta_color="off", delta_arrow="off", border=True)
    row = st.columns(4)
    row[0].metric("Gross profit", gbp(k["gross_profit"]), pct(k["gross_profit"] / k["net"] if k["net"] else None) + " margin",
                  delta_color="off", delta_arrow="off", border=True)
    row[1].metric("Card fees" + (" (est.)" if R.fees_estimated else ""), gbp(k["card_fees"]), border=True)
    row[2].metric("Event costs", gbp(k["event_costs"]), "tagged below", delta_color="off", delta_arrow="off", border=True)
    row[3].metric("Event profit", gbp(k["event_profit"]), "after all costs", delta_color="off",
                  delta_arrow="off", border=True)
    if k["missing_cost_units"]:
        st.caption(f"{k['missing_cost_units']} unit(s) have no unit cost (e.g. custom-amount sales), so they count as 100% margin.")

    with st.container(border=True):
        st.markdown("**Key findings**")
        for f in R.findings():
            st.markdown(f"- {f}")

    st.subheader("By day")
    days = R.by_day()
    st.dataframe(days[["day", "orders", "units", "gross", "discounts", "net", "share", "aov", "first", "last",
                       "trading_hrs", "net_per_hr", "gross_profit"]], hide_index=True, width="stretch", column_config={
        "day": "Day", "orders": "Orders", "units": "Units", "gross": MONEY("Gross"), "discounts": MONEY("Discounts"),
        "net": MONEY("Net sales"), "share": PCT("% of net"), "aov": MONEY("AOV"),
        "first": st.column_config.TimeColumn("First sale", format="HH:mm"),
        "last": st.column_config.TimeColumn("Last sale", format="HH:mm"),
        "trading_hrs": st.column_config.NumberColumn("Trading hrs*", format="%.1f"),
        "net_per_hr": MONEY("Net / hr"), "gross_profit": MONEY("Gross profit")})
    st.caption("*Trading hours = first to last sale recorded in Shopify.")

    left, right = st.columns(2)
    with left:
        st.subheader("Sales by hour")
        hours = R.by_hour()
        main_days = [d for d in days.sort_values("first")["day"] if days.set_index("day").loc[d, "orders"] > 1][:4]
        fig = go.Figure()
        for i, d in enumerate(main_days):
            fig.add_bar(x=[f"{h:02d}:00" for h in hours.index], y=hours[d], name=d, marker_color=SERIES[i],
                        hovertemplate="%{x}: %{y:£,.0f}<extra>" + d + "</extra>")
        fig = style_fig(fig, height=320)
        fig.update_layout(barmode="group")
        st.plotly_chart(fig, width="stretch")
    with right:
        st.subheader("By category")
        cats = R.by("category").iloc[::-1]
        fig = go.Figure(go.Bar(x=cats["net"], y=cats["category"], orientation="h", marker_color=SERIES[0],
                               text=[f"{gbp(n)} · {u} unit{'s' if u != 1 else ''}" for n, u in zip(cats["net"], cats["units"])],
                               textposition="outside", cliponaxis=False,
                               customdata=cats["margin"],
                               hovertemplate="%{y}: %{x:£,.0f} · margin %{customdata:.0%}<extra></extra>"))
        fig = style_fig(fig, height=320)
        fig.update_layout(hovermode="closest", margin=dict(r=110))
        fig.update_xaxes(showticklabels=False, showgrid=False, zeroline=False)
        fig.update_yaxes(tickprefix="", showgrid=False)
        st.plotly_chart(fig, width="stretch")

    tab_p, tab_s, tab_size, tab_c, tab_b = st.tabs(["Products", "Styles", "Sizes", "Colours", "Baskets & discounts"])
    profit_cols = {"units": "Units", "orders": "Orders", "gross": MONEY("Gross"), "discounts": MONEY("Discounts"),
                   "net": MONEY("Net sales"), "share": PCT("% of net"), "avg_price": MONEY("Avg net price"),
                   "cogs": MONEY("Stock cost"), "gross_profit": MONEY("Gross profit"), "margin": PCT("Margin")}
    with tab_p:
        p = R.by(["product_title", "category"])
        st.dataframe(p[["product_title", "category"] + list(profit_cols)], hide_index=True, width="stretch",
                     column_config={"product_title": "Product", "category": "Category", **profit_cols})
    with tab_s:
        s = R.by("style")
        st.dataframe(s[["style"] + list(profit_cols)], hide_index=True, width="stretch",
                     column_config={"style": "Style (all colourways)", **profit_cols})
    with tab_size:
        a, b = st.columns([2, 3])
        sizes = R.size_curve()
        with a:
            fig = go.Figure(go.Bar(x=sizes["size"], y=sizes["units"], marker_color=SERIES[0],
                                   text=[f"{s:.0%}" for s in sizes["share"]], textposition="outside", cliponaxis=False,
                                   hovertemplate="%{x}: %{y} units<extra></extra>"))
            fig = style_fig(fig, height=300, money=False)
            fig.update_layout(title=dict(text="Size curve (sized garments)", font=dict(size=14)), hovermode="closest")
            st.plotly_chart(fig, width="stretch")
        with b:
            st.markdown("**Units by style × size**")
            st.dataframe(R.style_by_size(), width="stretch")
        st.caption("Numeric sizes are waist sizes (jeans). One-size items (caps, foulards) are excluded from the curve.")
    with tab_c:
        col = R.by("colour_family")
        st.dataframe(col[["colour_family", "units", "net", "share", "gross_profit", "margin"]], hide_index=True, width="stretch",
                     column_config={"colour_family": "Colour family", **profit_cols})
    with tab_b:
        a, b = st.columns(2)
        with a:
            st.markdown("**Basket size**")
            st.dataframe(R.baskets(), hide_index=True, width="stretch", column_config={
                "basket": "Basket", "orders": "Orders", "units": "Units", "net": MONEY("Net sales"),
                "share_orders": PCT("% of orders"), "aov": MONEY("AOV")})
        with b:
            st.markdown("**Full price vs discounted**")
            st.dataframe(R.pricing(), hide_index=True, width="stretch", column_config={
                "pricing": "Pricing", "orders": "Orders", "net": MONEY("Net sales"), "discounts": MONEY("Discounts"),
                "aov": MONEY("AOV"), "share_orders": PCT("% of orders")})

    if not R.costs.empty:
        st.subheader("Event costs")
        st.dataframe(R.costs[["date", "counterparty", "category", "amount_base", "source"]], hide_index=True, width="stretch",
                     column_config={"date": st.column_config.DatetimeColumn("Date", format="D MMM YYYY"),
                                    "counterparty": "Payee", "category": "Category", "amount_base": MONEY("Amount"),
                                    "source": "Source"})

    _manage(c, L, R, event_id)


def _new_event_form(c):
    with st.expander("➕ New event"):
        with st.form("new_event", clear_on_submit=True):
            name = st.text_input("Name", placeholder="e.g. Shoreditch pop-up")
            a, b = st.columns(2)
            start = a.date_input("Start date", date.today())
            end = b.date_input("End date", date.today())
            location = st.text_input("Location")
            notes = st.text_input("Notes")
            auto = st.checkbox("Tag in-person (POS) orders in this date range", value=True)
            if st.form_submit_button("Create event", type="primary") and name.strip():
                events.create_event(c, name.strip(), start, end, location, notes, tag_pos_orders=auto)
                st.success("Event created")
                st.rerun()


def _manage(c, L, R, event_id):
    with st.expander("Manage this event: orders, costs, details"):
        ev = R.event
        start = pd.Timestamp(ev["start_date"]) - pd.Timedelta(days=14)
        end = pd.Timestamp(ev["end_date"]) + pd.Timedelta(days=14)

        st.markdown("**Orders in this event**")
        o = L.orders[(L.orders["date"] >= start) & (L.orders["date"] <= end)].sort_values("date")
        label = {r.id: f"{r.name} · {r.date:%a %d %b %H:%M} · {gbp(r.total, 2)} · {r.source_name}" for r in o.itertuples()}
        current = [i for i in R.orders["id"] if i in label]
        chosen = st.multiselect("Orders (two weeks either side of the event)", list(label), default=current,
                                format_func=label.get, key=f"ev_orders_{event_id}")

        st.markdown("**Event costs**: stall fee, travel, display kit, staff and so on")
        cash = L.cash[(L.cash["date"] >= start) & (L.cash["date"] <= end) & (L.cash["amount_base"] < 0)].sort_values("date")
        clabel = {r.id: f"{r.date:%d %b} · {r.counterparty} · {gbp(-r.amount_base, 2)} · {r.category}" for r in cash.itertuples()}
        ccurrent = [i for i in R.costs["id"] if i in clabel] if not R.costs.empty else []
        costs_chosen = st.multiselect("Costs (Wise and manual transactions near the event)", list(clabel), default=ccurrent,
                                      format_func=clabel.get, key=f"ev_costs_{event_id}")
        st.caption("Tagging a cost here only links it to the event report. It stays in its normal category in the P&L. "
                   "For costs paid outside Wise, add them on **Manual transactions** first.")

        a, b, d = st.columns(3)
        name = a.text_input("Name", ev["name"], key=f"ev_name_{event_id}")
        location = b.text_input("Location", ev.get("location") or "", key=f"ev_loc_{event_id}")
        notes = d.text_input("Notes", ev.get("notes") or "", key=f"ev_notes_{event_id}")
        s1, s2 = st.columns([1, 5])
        if s1.button("Save", type="primary", key=f"ev_save_{event_id}"):
            events.set_orders(c, event_id, chosen)
            events.set_costs(c, event_id, costs_chosen)
            c.execute("UPDATE events SET name=?, location=?, notes=? WHERE id=?", (name, location, notes, event_id))
            c.commit()
            st.success("Saved")
            st.rerun()
        confirm = s2.checkbox("I want to delete this event", key=f"ev_delconfirm_{event_id}")
        if confirm and s2.button("Delete event", key=f"ev_del_{event_id}"):
            c.execute("DELETE FROM event_orders WHERE event_id=?", (event_id,))
            c.execute("DELETE FROM event_costs WHERE event_id=?", (event_id,))
            c.execute("DELETE FROM events WHERE id=?", (event_id,))
            c.commit()
            st.rerun()
