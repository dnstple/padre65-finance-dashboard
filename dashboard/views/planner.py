import io

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from common import SERIES, conn, editor, gbp, heading, ledger, metric, pct, report_table, style_fig, table
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

    plan_rows = planner.restock_plan(c, in_plan)
    final_units, final_cost = int(plan_rows["final_buy"].sum()), float(plan_rows["final_cost"].sum())
    changed = bool((~plan_rows["include"]).any() or plan_rows["buy_override"].notna().any())
    plain = dict(delta_color="off", delta_arrow="off", border=True)
    row = st.columns(2)
    metric(row[0], "Stock readiness", pct(s["readiness"]), f"{s['covered_units']} of {s['target_units']} target units in stock", **plain)
    metric(row[1], "Units to buy", f"{final_units}",
           f"suggested {s['to_buy_units']}" if changed else f"across {int((plan_rows['final_buy'] > 0).sum())} product sizes",
           **plain)
    row = st.columns(2)
    metric(row[0], "Cost to restock", gbp(final_cost),
           f"suggested {gbp(s['buy_cost'])}" if changed else "at landed unit cost", **plain)
    metric(row[1], "Expected pop-up sales", gbp(s["expected_sales"]),
           f"≈ {gbp(s['expected_gross_profit'])} gross profit", **plain)
    if s["uncosted_to_buy"]:
        st.caption(f"{s['uncosted_to_buy']} units to buy have no unit cost in the cost sheet, so they're not in the cost total.")

    heading("Stock vs target by product")
    st.caption("🔴 Restock: none in stock · 🟠 Top up: some sizes short · 🟢 Ready: enough stock · 🔵 Plenty: at least 2× the target, so push these.")
    mine = plan_rows.groupby("product_id")[["final_buy", "final_cost"]].sum()
    shown = products.assign(status=products["status"].map(STATUS_ICON)).join(mine, on="product_id")
    table(shown[["status", "product_title", "category", "sold_last", "on_hand", "expected", "target", "to_buy",
                 "final_buy", "final_cost"]],
          hide_index=True, width="stretch", column_config={
              "status": "Status", "product_title": "Product", "category": "Category",
              "sold_last": "Sold last time", "on_hand": "In stock",
              "expected": st.column_config.NumberColumn("Expected sales", format="%.1f"),
              "target": "Target stock", "to_buy": "Suggested buy", "final_buy": "Final buy",
              "final_cost": MONEY("Cost")})

    left, right = st.columns([3, 2])
    with left:
        heading("Sizes: in stock / target")
        grid = planner.size_grid(in_plan)
        if not grid.empty:
            st.caption("Each cell is units in stock / target. ⚠️ = short for that size.")
            st.dataframe(grid, width="stretch")
    with right:
        heading("Where the restock money goes")
        spend = plan_rows[plan_rows["final_cost"] > 0].groupby("category")["final_cost"].sum().sort_values()
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

    _plan_editor(c, plan_rows, products)
    _costs_section(c, s, final_cost)

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


PLAN_COLUMNS = ["rank", "include", "product_title", "size", "sold_all_time", "on_hand", "suggested", "buy_override",
                "final_buy", "final_cost", "target", "product_sold_all_time", "sold_last"]


def _plan_editor(c, plan_rows, products):
    heading("Restock plan by size")
    st.caption("Ranked by how much each product has sold overall (online + in person), most in demand first. "
               "Untick **Include** to leave a size out of the totals. Type a number in **Your buy** to override the "
               "suggestion, or clear it to go back. Changes save automatically and are shared with everyone using the dashboard.")
    a, b = st.columns([3, 1])
    only_needed = a.toggle("Only show sizes with something to buy", value=False, key="plan_only_needed",
                           help="Hide sizes where both the suggestion and your override are zero.")
    if b.button("Reset to suggestions", help="Tick every row again and clear all your overrides."):
        planner.reset_choices(c)
        st.session_state["plan_ver"] = st.session_state.get("plan_ver", 0) + 1
        st.rerun()

    rows = plan_rows
    if only_needed:
        rows = rows[(rows["suggested"] > 0) | (rows["buy_override"].fillna(0) > 0)]
    rows = rows.reset_index(drop=True)
    key = f"plan_editor_{st.session_state.get('plan_ver', 0)}"
    ids = rows["variant_id"].tolist()
    base_include = rows["include"].tolist()
    base_override = rows["buy_override"].tolist()

    def save():
        """Persist ticks/overrides, then rebuild the table so final buy and cost update."""
        changes = []
        for idx, change in st.session_state.get(key, {}).get("edited_rows", {}).items():
            i = int(idx)
            include = change.get("include", base_include[i])
            override = change.get("buy_override", base_override[i])
            override = None if override is None or pd.isna(override) else int(override)
            changes.append((ids[i], include, override))
        if changes:
            planner.save_choices(conn(), changes)
        st.session_state["plan_ver"] = st.session_state.get("plan_ver", 0) + 1

    editor(rows[PLAN_COLUMNS], key=key, on_change=save, hide_index=True, width="stretch",
           height=min(38 * (len(rows) + 1) + 4, 620),
           disabled=[col for col in PLAN_COLUMNS if col not in ("include", "buy_override")],
           column_config={
               "rank": st.column_config.NumberColumn("#", format="%d"),
               "include": st.column_config.CheckboxColumn("Include"),
               "product_title": "Product", "size": "Size",
               "product_sold_all_time": "Product sold (all time)", "sold_all_time": "Size sold (all time)",
               "sold_last": "Sold last time", "on_hand": "In stock", "target": "Target stock",
               "suggested": "Suggested buy",
               "buy_override": st.column_config.NumberColumn("Your buy", min_value=0, step=1, format="%d"),
               "final_buy": "Final buy", "final_cost": MONEY("Cost")})

    final = plan_rows[plan_rows["final_buy"] > 0]
    heading("Buy list")
    if final.empty:
        st.success("Nothing to buy with the current plan.")
        return
    buy = final[["product_title", "size", "sku", "on_hand", "suggested", "final_buy", "unit_cost", "final_cost"]]
    st.caption(f"{int(final['final_buy'].sum())} units · {gbp(final['final_cost'].sum())}. "
               "Only included sizes, using your overrides.")
    table(buy, hide_index=True, width="stretch", column_config={
        "product_title": "Product", "size": "Size", "sku": "SKU", "on_hand": "In stock",
        "suggested": "Suggested buy", "final_buy": "Final buy", "unit_cost": MONEY("Unit cost"),
        "final_cost": MONEY("Cost")})
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        buy.to_excel(xl, sheet_name="Buy list", index=False)
        plan_rows[PLAN_COLUMNS + ["sku", "unit_cost"]].to_excel(xl, sheet_name="Full plan by size", index=False)
        products.to_excel(xl, sheet_name="By product", index=False)
    st.download_button("Download buy list (Excel)", buf.getvalue(), file_name="popup_buy_list.xlsx")


def _costs_section(c, s, restock_cost):
    st.divider()
    heading("Pop-up costs & expected profit", "header")
    st.caption("Enter what running the shop will cost: rent, fit-out, staff, travel and so on. Add or delete rows as "
               "needed. The expected profit below updates as you type. Click **Save costs** to keep them (shared with "
               "everyone using the dashboard).")

    saved = planner.load_costs(c)
    edited = editor(saved[["item", "category", "amount", "notes"]], key=f"popup_costs_{st.session_state.get('costs_ver', 0)}",
                    num_rows="dynamic", hide_index=True, width="stretch", column_config={
                        "item": st.column_config.TextColumn("Cost item", required=True),
                        "category": st.column_config.SelectboxColumn("Cost type", options=planner.COST_CATEGORIES,
                                                                     default="Other"),
                        "amount": st.column_config.NumberColumn("Expected cost", format="£%.2f", min_value=0, default=0.0),
                        "notes": "Notes"})
    unsaved = not edited.reset_index(drop=True).fillna("").astype(str).equals(
        saved[["item", "category", "amount", "notes"]].reset_index(drop=True).fillna("").astype(str))
    a, b = st.columns([1, 4])
    if a.button("Save costs", type="primary", disabled=not unsaved):
        planner.save_costs(c, edited)
        st.session_state["costs_ver"] = st.session_state.get("costs_ver", 0) + 1
        st.rerun()
    if unsaved:
        b.caption("⚠️ Unsaved changes. The figures below already include them.")

    p = planner.projection(s, edited)
    plain = dict(delta_color="off", delta_arrow="off", border=True)
    row = st.columns(2)
    n_lines = int((pd.to_numeric(edited["amount"], errors="coerce").fillna(0) > 0).sum())
    metric(row[0], "Running costs", gbp(p["running"]), f"{n_lines} cost line{'s' if n_lines != 1 else ''}", **plain)
    metric(row[1], "Expected pop-up profit", gbp(p["profit"]),
           (pct(p["margin"]) + " of sales") if p["margin"] is not None else "", **plain)
    row = st.columns(2)
    metric(row[0], "Break-even sales", gbp(p["break_even_sales"]) if p["break_even_sales"] is not None else "–",
           (f"{p['break_even_sales'] / p['sales']:.0%} of expected sales" if p["break_even_sales"] and p["sales"] else ""), **plain)
    metric(row[1], "Cash needed up front", gbp(restock_cost + p["running"]),
           f"{gbp(restock_cost)} stock + {gbp(p['running'])} costs", **plain)

    left, right = st.columns([3, 2])
    with left:
        heading("Expected pop-up P&L")
        lines = {"Expected pop-up sales": p["sales"], "Stock cost of items sold": -p["cogs"],
                 "Gross profit": p["gross_profit"], f"Card fees ({s['fee_rate']:.1%})": -p["fees"]}
        by_type = edited.assign(amount=pd.to_numeric(edited["amount"], errors="coerce").fillna(0))
        by_type = by_type[by_type["amount"] > 0].groupby(by_type["category"].fillna("Other"))["amount"].sum()
        for cat, amount in by_type.items():
            lines[cat] = -amount
        lines["Expected pop-up profit"] = p["profit"]
        pnl = pd.DataFrame({"Expected": lines})
        report_table(pnl, subtotals=["Gross profit", "Expected pop-up profit"],
                     definitions={k: v for k, v in [(f"Card fees ({s['fee_rate']:.1%})",
                                                     "Shopify card fees, at the same rate as the base pop-up.")]} |
                                 {c_: "Your expected cost for this cost type, from the table above." for c_ in by_type.index})
    with right:
        heading("If sales come in higher or lower")
        rows = []
        for f in (0.5, 0.75, 1.0, 1.25, 1.5):
            q = planner.projection(s, edited, f)
            rows.append({"Sales vs expected": f"{f:.0%}", "Sales": q["sales"], "Profit": q["profit"]})
        table(pd.DataFrame(rows), hide_index=True, width="stretch", column_config={
            "Sales": st.column_config.NumberColumn("Sales", format="£%.0f"),
            "Profit": st.column_config.NumberColumn("Profit", format="£%.0f")})
        st.caption("Running costs stay the same whatever you sell; stock cost and card fees move with sales.")

    st.caption("The restock spend isn't a cost of the pop-up itself. Only the stock you **sell** counts (stock cost of "
               "items sold). Anything left over stays in stock for future sales. That's why cash needed up front can "
               "be higher than the costs in the P&L.")
