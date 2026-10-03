import io

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from common import SERIES, conn, gbp, heading, ledger, metric, pct, style_fig, table
from tracker import events, planner

MONEY = lambda label: st.column_config.NumberColumn(label, format="£%.2f")  # noqa: E731
STATUS_ICON = {"Restock": "🔴 Restock", "Top up": "🟠 Top up", "Ready": "🟢 Ready", "Plenty": "🔵 Plenty"}


def render():
    heading("Pop-up stock planner", "header")
    c = conn()
    evs = events.list_events(c)
    if evs.empty:
        st.info("Create an event under **Event report** first. The planner uses a past pop-up's sales as its forecast.")
        return

    labels = {int(r.id): f"{r.name} · {r.start_date}" for r in evs.itertuples()}
    a, b, d, e = st.columns(4)
    event_id = a.selectbox("Base the plan on", list(labels), format_func=labels.get, key="plan_event",
                           help="The past pop-up whose sales are used as the forecast for the next one.")
    scale = b.slider("Expected sales vs that pop-up", 50, 250, 100, 10, format="%d%%", key="plan_scale",
                     help="100% = sell the same as last time. Raise it for a longer or busier event, lower it for a smaller one.") / 100
    buffer = d.slider("Safety buffer", 0, 150, 50, 10, format="%d%%", key="plan_buffer",
                      help="Extra stock on top of expected sales, so popular items and sizes don't sell out. 50% = bring 1.5× what you expect to sell.") / 100
    min_size = e.number_input("Minimum per size", 0, 5, 1, key="plan_min",
                              help="For clothing that sold last time, stock at least this many of every size so the full size run is on display. Set 0 to turn off.")

    s, products, in_plan, buy_list, not_sold = planner.plan(c, ledger(), event_id, scale, buffer, int(min_size))
    st.caption(f"Forecast from {s['last_orders']} orders / {s['last_units']} units at **{s['event']['name']}**, scaled to "
               f"{scale:.0%}, plus a {buffer:.0%} buffer. Compared with current Shopify stock.")

    plain = dict(delta_color="off", delta_arrow="off", border=True)
    row = st.columns(2)
    metric(row[0], "Stock readiness", pct(s["readiness"]), f"{s['covered_units']} of {s['target_units']} target units in stock", **plain)
    metric(row[1], "Units to buy", f"{s['to_buy_units']}", f"across {int((buy_list['to_buy'] > 0).sum())} product sizes", **plain)
    row = st.columns(2)
    metric(row[0], "Cost to restock", gbp(s["buy_cost"]), "at landed unit cost", **plain)
    metric(row[1], "Expected pop-up sales", gbp(s["expected_sales"]),
           f"≈ {gbp(s['expected_gross_profit'])} gross profit", **plain)
    if s["uncosted_to_buy"]:
        st.caption(f"{s['uncosted_to_buy']} units to buy have no unit cost in the cost sheet, so they're not in the cost total.")

    heading("Stock vs target by product")
    st.caption("🔴 Restock: none in stock · 🟠 Top up: some sizes short · 🟢 Ready: enough stock · 🔵 Plenty: at least 2× the target, so push these.")
    shown = products.assign(status=products["status"].map(STATUS_ICON))
    table(shown[["status", "product_title", "category", "sold_last", "on_hand", "expected", "target", "to_buy", "buy_cost"]],
          hide_index=True, width="stretch", column_config={
              "status": "Status", "product_title": "Product", "category": "Category",
              "sold_last": "Sold last time", "on_hand": "In stock",
              "expected": st.column_config.NumberColumn("Expected sales", format="%.1f"),
              "target": "Target stock", "to_buy": "To buy", "buy_cost": MONEY("Cost to buy")})

    left, right = st.columns([3, 2])
    with left:
        heading("Sizes: in stock / target")
        grid = planner.size_grid(in_plan)
        if not grid.empty:
            st.caption("Each cell is units in stock / target. ⚠️ = short for that size.")
            st.dataframe(grid, width="stretch")
    with right:
        heading("Where the restock money goes")
        spend = products[products["buy_cost"] > 0].groupby("category")["buy_cost"].sum().sort_values()
        if spend.empty:
            st.success("Nothing to buy: current stock covers the plan.")
        else:
            fig = go.Figure(go.Bar(x=spend.values, y=spend.index, orientation="h", marker_color=SERIES[1],
                                   text=[gbp(v) for v in spend.values], textposition="outside", cliponaxis=False,
                                   hovertemplate="%{y}: %{x:£,.0f}<extra></extra>"))
            fig = style_fig(fig, height=max(220, 40 * len(spend) + 60))
            fig.update_layout(hovermode="closest", margin=dict(r=70))
            fig.update_xaxes(showticklabels=False, showgrid=False, zeroline=False)
            fig.update_yaxes(tickprefix="", showgrid=False)
            st.plotly_chart(fig, width="stretch")

    heading("Buy list")
    if buy_list.empty:
        st.success("Nothing to buy.")
    else:
        table(buy_list, hide_index=True, width="stretch", column_config={
            "product_title": "Product", "size": "Size", "sku": "SKU", "on_hand": "In stock",
            "expected": st.column_config.NumberColumn("Expected sales", format="%.1f"), "target": "Target stock",
            "to_buy": "To buy", "unit_cost": MONEY("Unit cost"), "buy_cost": MONEY("Cost to buy")})
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as xl:
            buy_list.to_excel(xl, sheet_name="Buy list", index=False)
            products.to_excel(xl, sheet_name="By product", index=False)
        st.download_button("Download buy list (Excel)", buf.getvalue(), file_name="popup_buy_list.xlsx")

    if not not_sold.empty:
        heading("In stock but didn't sell last time")
        st.caption("These have stock but no sales at the base pop-up, so there's no forecast for them. "
                   "Bring them if there's space, as they cost nothing extra.")
        table(not_sold, hide_index=True, width="stretch", column_config={
            "product_title": "Product", "category": "Category", "on_hand": "In stock",
            "unit_cost": MONEY("Unit cost"), "price": MONEY("Price")})

    with st.expander("How this plan is worked out, and its limits"):
        st.markdown(f"""
- **Expected sales** per product = units it sold at the base pop-up × the expected-sales slider.
- **Sizes:** each product's expected units are split across its sizes using its own size mix last time, blended with
  the pop-up's overall size curve. One product that sold 2 units doesn't get all its forecast in one size.
- **Target stock** = expected × (1 + safety buffer), rounded up once per product and shared across its sizes,
  then at least the minimum per size for clothing that sold.
- **To buy** = target − current Shopify stock (never below 0). **Cost** uses the landed unit cost from the cost sheet.
- **Expected sales £** = expected units × price, less last time's average discount ({pct(s['discount_rate'])}).
- **Limits:**
  - It's based on one pop-up ({s['last_orders']} orders), so treat it as a starting point.
  - Online sales before the event will use up some stock.
  - Supplier minimum order quantities and lead times aren't included. A size needing 2 units may only be orderable in a batch of 10.
  - Products that didn't sell last time get no forecast.
""")
