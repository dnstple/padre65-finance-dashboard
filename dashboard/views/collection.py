"""Collection planner: the line-up, demand from past pop-ups and online, the buy plan, supplier timeline and scenarios."""
import io
from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from common import SERIES, conn, editor, gbp, heading, ledger, metric, pct, report_table, style_fig, table
from tracker import collection as col

MONEY = lambda label: st.column_config.NumberColumn(label, format="£%.0f")  # noqa: E731
MONEY2 = lambda label: st.column_config.NumberColumn(label, format="£%.2f")  # noqa: E731
PCT = lambda label: st.column_config.NumberColumn(label, format="percent")  # noqa: E731
UNITS = lambda label: st.column_config.NumberColumn(label, format="%d")  # noqa: E731
CONF_ICON = {"High": "🟢 High", "Medium": "🟡 Medium", "Low": "🟠 Low", "Your estimate": "🔵 Your estimate",
             "No evidence": "🔴 No evidence"}
STATUS_ICON = {"Late": "🔴 Late", "Order within 2 weeks": "🟠 Order within 2 weeks", "On track": "🟢 On track",
               "Add lead time": "⚪ Add lead time", "Add pop-up dates": "⚪ Add pop-up dates"}
PLAIN = dict(delta_color="off", delta_arrow="off", border=True)


def render():
    heading("Collection planner", "title")
    st.caption("Plan what to buy for the next collection: the line-up and supplier terms, demand from past pop-ups and "
               "online sales, quantities by size within your budget or target, and when each supplier needs the order. "
               "Planning only: it never places orders or changes stock.")
    c = conn()
    cid = _collection_picker(c)
    if cid is None:
        return
    coll = col.get_collection(c, cid)
    settings = dict(coll["settings"])
    items = col.load_items(c, cid)

    summary = st.container()
    tabs = st.tabs(["① Line-up", "② Demand & pop-ups", "③ Buy plan", "④ Timeline", "⑤ Scenarios"])
    with tabs[0]:
        _lineup_editor(c, cid, items)
    if items.empty or items["name"].dropna().empty:
        with tabs[0]:
            st.info("Add the products you're considering, one row per colourway.")
        return
    ev = col.evidence(c, ledger(), soldout_uplift=settings.get("soldout_uplift", 0.25))
    with tabs[1]:
        settings = _popups_editor(cid, settings)
        settings = _type_demand_editor(cid, coll, ev, items, settings)
    with tabs[2]:
        settings = _plan_settings(cid, settings)
    if settings != coll["settings"]:
        col.update_collection(c, cid, settings=settings)
        coll["settings"] = settings

    plan = col.build(ev, items, coll)
    if plan["items"].empty:
        st.info("All products are marked Dropped.")
        return
    tl = col.timeline(plan, settings)
    with summary:
        _kpis(plan, settings, tl)
    with tabs[0]:
        _lineup_summary(plan)
    with tabs[1]:
        _demand_view(ev, plan)
    with tabs[2]:
        _plan_view(c, cid, coll, plan)
    with tabs[3]:
        _timeline_view(tl, settings)
    with tabs[4]:
        _scenarios_tab(c, cid, coll, ev, items)


# --- collection picker / details -------------------------------------------------------

def _collection_picker(c):
    cols = col.list_collections(c)
    if cols.empty:
        st.info("Create a collection to start planning.")
    labels = {int(r.id): f"{r.name}" + (f" · {r.season}" if r.season else "") + ("" if r.n_items else " (empty)")
              for r in cols.itertuples()}
    cid = None
    if labels:
        if st.session_state.get("plan_cid") not in labels:
            st.session_state["plan_cid"] = next(iter(labels))
        a, b = st.columns([3, 2])
        cid = a.selectbox("Collection", list(labels), format_func=labels.get, key="plan_cid",
                          help="Each collection has its own line-up, plan and saved scenarios.")
        coll = col.get_collection(c, cid)
        period = (pd.Timestamp(coll["end_date"]) - pd.Timestamp(coll["start_date"])).days + 1
        b.caption(f"Selling {pd.Timestamp(coll['start_date']):%d %b %Y} – {pd.Timestamp(coll['end_date']):%d %b %Y} "
                  f"({period} days)" + (f"  \n{coll['notes']}" if coll.get("notes") else ""))
        with st.expander("Collection details"):
            _details_form(c, cid, coll)
    with st.expander("➕ New collection", expanded=cols.empty):
        with st.form("new_collection", clear_on_submit=True):
            a, b = st.columns([2, 1])
            name = a.text_input("Collection name", placeholder="e.g. Summer 2027")
            season = b.text_input("Season", placeholder="e.g. SS27")
            d, e = st.columns(2)
            start = d.date_input("Selling from", date.today() + timedelta(days=30), format="DD/MM/YYYY")
            end = e.date_input("Selling until", date.today() + timedelta(days=150), format="DD/MM/YYYY")
            if st.form_submit_button("Create collection", type="primary"):
                if not name.strip():
                    st.error("Give the collection a name.")
                elif end <= start:
                    st.error("The selling period must end after it starts.")
                else:
                    st.session_state["plan_cid"] = col.create_collection(c, name.strip(), season, start, end)
                    st.rerun()
    return cid


def _details_form(c, cid, coll):
    s = coll["settings"]
    with st.form(f"details_{cid}"):
        a, b = st.columns([2, 1])
        name = a.text_input("Collection name", coll["name"])
        season = b.text_input("Season", coll.get("season") or "")
        d, e, f, g = st.columns(4)
        start = d.date_input("Selling from", pd.Timestamp(coll["start_date"]).date(), format="DD/MM/YYYY")
        end = e.date_input("Selling until", pd.Timestamp(coll["end_date"]).date(), format="DD/MM/YYYY")
        eur = f.number_input("€1 = £", 0.5, 1.5, float(s["fx"].get("EUR", 0.8418)), 0.0001, format="%.4f",
                             help="Exchange rate for euro prices. Update it when you pay or when the rate moves.")
        buffer = g.number_input("Customising time (weeks)", 0.0, 12.0, float(s.get("custom_buffer_weeks") or 0), 0.5,
                                help="Weeks after stock arrives for embroidery, labels or relabelling before it can be sold. "
                                     "Added to each supplier's lead time when working out order-by dates.")
        notes = st.text_area("Context", coll.get("notes") or "", height=70)
        x, y = st.columns([1, 3])
        if x.form_submit_button("Save details", type="primary"):
            col.update_collection(c, cid, name=name, season=season, start_date=str(start), end_date=str(end), notes=notes,
                                  settings={**s, "fx": {**s["fx"], "EUR": eur}, "custom_buffer_weeks": buffer})
            st.rerun()
    confirm = st.checkbox("Delete this collection (line-up and scenarios too)", key=f"cdelc_{cid}")
    if confirm and st.button("Delete collection", key=f"cdel_{cid}"):
        col.delete_collection(c, cid)
        st.session_state.pop("plan_cid", None)
        st.rerun()


# --- summary --------------------------------------------------------------------------------

def _kpis(plan, settings, tl):
    t = plan["totals"]
    budget = settings.get("budget")
    target = settings.get("target_revenue")
    row = st.columns(3)
    metric(row[0], "Cash needed", gbp(t["cash"]),
           (f"{gbp(budget - t['cash'])} {'left' if budget >= t['cash'] else 'over'} of {gbp(budget)}" if budget else
            f"incl. {gbp(t['sample_cost'])} samples"), **PLAIN)
    metric(row[1], "Units to buy", f"{t['buy']:,.0f}", f"vs {t['expected_demand']:,.0f} expected to sell", **PLAIN)
    metric(row[2], "Expected sales", gbp(t["revenue_mid"]),
           f"{gbp(t['revenue_low'])} – {gbp(t['revenue_high'])}" + (f" · target {gbp(target)}" if target else ""), **PLAIN)
    row = st.columns(3)
    metric(row[0], "Expected gross profit", gbp(t["gross_profit_mid"]), pct(t["gross_margin_mid"]) + " margin", **PLAIN)
    metric(row[1], "Left over if sales are weak", gbp(t["unsold_low_cost"]), f"expected {gbp(t['unsold_mid_cost'])}", **PLAIN)
    nxt = tl.dropna(subset=["order_by"]) if not tl.empty else tl
    if len(nxt):
        first = nxt.iloc[0]
        metric(row[2], "First order by", f"{first['order_by']:%d %b}", first["supplier"], **PLAIN)
    else:
        metric(row[2], "First order by", "–", "add pop-up dates and lead times", **PLAIN)
    for note in plan["notes"]:
        st.warning(note)


# --- ① line-up ------------------------------------------------------------------------------

LINEUP_CONFIG = {
    "id": None,
    "name": st.column_config.TextColumn("Product", required=True, width="medium"),
    "colour": st.column_config.TextColumn("Colourway"),
    "product_type": st.column_config.SelectboxColumn("Product type", options=col.PRODUCT_TYPES, required=True),
    "supplier": st.column_config.TextColumn("Supplier"),
    "sizes": st.column_config.TextColumn("Sizes"),
    "currency": st.column_config.SelectboxColumn("Currency", options=col.CURRENCIES, default="GBP"),
    "unit_cost": st.column_config.NumberColumn("Unit price", min_value=0, format="%.2f"),
    "tiers": st.column_config.TextColumn("Price breaks"),
    "moq": st.column_config.NumberColumn("Minimum order", min_value=0, step=1, format="%d"),
    "sample_cost": st.column_config.NumberColumn("Samples to pay (£)", min_value=0, format="£%.0f"),
    "custom_cost": st.column_config.NumberColumn("Customising (£/unit)", min_value=0, format="£%.2f"),
    "lead_weeks": st.column_config.NumberColumn("Lead time (weeks)", min_value=0, step=0.5, format="%.1f"),
    "price": st.column_config.NumberColumn("Retail price (£)", min_value=0, format="£%.0f"),
    "draft_units": st.column_config.NumberColumn("Your quantity", min_value=0, step=1, format="%d"),
    "emphasis": st.column_config.NumberColumn("Colour weight", min_value=0.0, max_value=5.0, step=0.25, format="×%.2f", default=1.0),
    "status": st.column_config.SelectboxColumn("Plan status", options=col.STATUSES, default="Considering"),
    "notes": st.column_config.TextColumn("Notes"),
}


def _lineup_editor(c, cid, items):
    heading("Line-up")
    st.caption("One row per colourway. Unit price is in the supplier's currency; price breaks are `quantity: price` pairs, "
               "e.g. `7: 11.25; 26: 10.85`. Leave retail price blank to use your usual price for that type.")
    ver = st.session_state.get(f"items_ver_{cid}", 0)
    shown = items[["id"] + col.ITEM_FIELDS].sort_values("product_type", key=lambda s: s.map(
        lambda t: col.PRODUCT_TYPES.index(t) if t in col.PRODUCT_TYPES else 99), kind="stable").reset_index(drop=True)
    edited = editor(shown, key=f"items_{cid}_{ver}", num_rows="dynamic", hide_index=True, width="stretch",
                    column_config=LINEUP_CONFIG)
    unsaved = not _same_items(edited, shown)
    x, y = st.columns([1, 4])
    if x.button("Save line-up", type="primary", disabled=not unsaved, key=f"saveitems_{cid}"):
        col.save_items(c, cid, edited)
        st.session_state[f"items_ver_{cid}"] = ver + 1
        st.rerun()
    if unsaved:
        y.caption("⚠️ Unsaved changes. Save to use them in the plan.")


def _same_items(a, b):
    cols = col.ITEM_FIELDS
    norm = lambda df: df[cols].reset_index(drop=True).astype(str).replace({"None": "", "nan": "", "<NA>": ""})  # noqa: E731
    return len(a) == len(b) and norm(a).equals(norm(b))


def _lineup_summary(plan):
    it = plan["items"]
    heading("Cost and margin per colourway")
    shown = it.assign(price_note=it["assumed"].map(lambda a: "assumed" if "price" in a else ""),
                      flags=it["flags"].map("; ".join))
    table(shown[["label", "supplier", "buy", "unit_cost", "price", "price_note", "margin_pct", "stock_cost", "sample_cost", "flags"]],
          hide_index=True, width="stretch", column_config={
              "label": st.column_config.Column("Product", width="medium"), "supplier": "Supplier",
              "buy": UNITS("Your order"), "unit_cost": MONEY2("Landed cost"), "price": MONEY("Retail price (£)"),
              "price_note": "Price", "margin_pct": PCT("Margin % at full price"), "stock_cost": MONEY("Stock cost"),
              "sample_cost": MONEY("Samples to pay (£)"), "flags": "Watch out for"})
    st.caption("Landed cost = the supplier's price at your order quantity (after price breaks), converted to £, plus "
               "customising. Blank retail prices use your usual price for the type, or cost × markup when that would "
               "leave a thin margin.")


# --- ② demand & pop-ups ---------------------------------------------------------------------

def _popups_editor(cid, settings):
    heading("Pop-ups this season")
    st.caption("Each pop-up's size compared with the last one (×1 = the same footfall and sales). Dates drive the "
               "order-by dates on the Timeline.")
    pops = pd.DataFrame(settings.get("popups") or [], columns=["name", "date", "scale"])
    pops["date"] = pd.to_datetime(pops["date"], errors="coerce").dt.date
    key = f"popups_{cid}_{st.session_state.get(f'popups_ver_{cid}', 0)}"
    edited = editor(pops, key=key, num_rows="dynamic", hide_index=True, width="stretch", column_config={
        "name": st.column_config.TextColumn("Pop-up", required=True),
        "date": st.column_config.DateColumn("Date", format="D MMM YYYY"),
        "scale": st.column_config.NumberColumn("Size vs last pop-up", min_value=0.0, max_value=5.0, step=0.25,
                                               format="×%.2f", default=1.0)})
    rows = [{"name": r["name"], "date": None if pd.isna(r["date"]) else str(r["date"])[:10],
             "scale": 1.0 if pd.isna(r["scale"]) else float(r["scale"])}
            for r in edited.to_dict("records") if str(r.get("name") or "").strip()]
    return {**settings, "popups": rows}


def _type_demand_editor(cid, coll, ev, items, settings):
    heading("Demand by product type")
    period = (pd.Timestamp(coll["end_date"]) - pd.Timestamp(coll["start_date"])).days + 1
    live = items[items["status"].ne("Dropped") & items["name"].notna()]
    types = list(dict.fromkeys(live["product_type"].fillna("Other")))
    scale = col.popup_scale(settings)
    hist = col.type_demand(ev, types, period, scale, {**settings, "type_demand": {}, "type_emphasis": {}}).set_index("product_type")
    cur = col.type_demand(ev, types, period, scale, settings).set_index("product_type")
    st.caption(f"Expected units for the whole selling period ({period} days online + {len(settings.get('popups') or [])} "
               f"pop-up(s), ×{scale:.2f} the last one). Edit **Per pop-up**, **Online per 30 days** or **Emphasis** to use "
               "your own judgement, e.g. more knitwear in winter. Each type's demand is then split across its colourways.")
    grid = cur[["source", "hist_popup_units", "per_popup", "online_per_30", "emphasis", "expected", "on_hand"]].reset_index()
    grid["confidence"] = cur["confidence"].map(CONF_ICON).values
    key = f"typedem_{cid}_{st.session_state.get(f'typedem_ver_{cid}', 0)}"
    edited = editor(grid, key=key, hide_index=True, width="stretch",
                    disabled=["product_type", "source", "hist_popup_units", "expected", "on_hand", "confidence"],
                    column_config={
                        "product_type": "Product type", "source": "Based on",
                        "hist_popup_units": UNITS("Sold at last pop-up"),
                        "per_popup": st.column_config.NumberColumn("Per pop-up", min_value=0.0, format="%.1f"),
                        "online_per_30": st.column_config.NumberColumn("Online per 30 days", min_value=0.0, format="%.1f"),
                        "emphasis": st.column_config.NumberColumn("Emphasis", min_value=0.0, max_value=5.0, step=0.25, format="×%.2f"),
                        "expected": st.column_config.NumberColumn("Expected units", format="%.0f"),
                        "on_hand": UNITS("Already in stock"), "confidence": "Confidence"})
    demand, emph = {}, {}
    for r in edited.to_dict("records"):
        t = r["product_type"]
        own = {}
        if abs(float(r["per_popup"] or 0) - hist.loc[t, "per_popup"]) > 0.05:
            own["per_popup"] = float(r["per_popup"] or 0)
        if abs(float(r["online_per_30"] or 0) - hist.loc[t, "online_per_30"]) > 0.05:
            own["online_per_30"] = float(r["online_per_30"] or 0)
        if own:
            demand[t] = own
        if abs(float(r["emphasis"] or 0) - 1.0) > 1e-9:
            emph[t] = float(r["emphasis"] or 0)
    if (demand or emph) and st.button("Reset to history", key=f"typedem_reset_{cid}"):
        demand, emph = {}, {}
        st.session_state[f"typedem_ver_{cid}"] = st.session_state.get(f"typedem_ver_{cid}", 0) + 1
    stocked = cur[(cur["on_hand"] > 0) & (cur["on_hand"] >= cur["expected"])]
    for t, r in stocked.iterrows():
        st.info(f"**{t}:** you already hold {int(r['on_hand'])} in stock, against {r['expected']:.0f} expected to sell "
                f"this season. New {t.lower()} stock will compete with it.")
    return {**settings, "type_demand": demand, "type_emphasis": emph}


def _demand_view(ev, plan):
    with st.expander("⚠️ What the history can and can't tell us"):
        for note in col.limitations(ev):
            st.markdown(f"- {note}")
    v = ev["variants"]
    heading("What sold at the last pop-up")
    sold = v[v["event_units"] > 0]
    if sold.empty:
        st.caption("No pop-up sales tagged yet. Tag orders to an event under Reports → Event report.")
    else:
        sized = sold[sold["size"] != "One size"]
        if not sized.empty:
            mix = sized.pivot_table(index="category", columns="size", values="event_units", aggfunc="sum", fill_value=0)
            mix = mix.reindex(columns=sorted(mix.columns, key=col.size_rank))
            mix.insert(0, "Total", mix.sum(axis=1))
            st.markdown("**Units by size**")
            st.dataframe(mix.astype(int), width="stretch")
        colours = sold.pivot_table(index="category", columns="colour_family", values="event_units", aggfunc="sum", fill_value=0)
        st.markdown("**Units by colour**")
        st.dataframe(colours.astype(int), width="stretch")
        st.caption("Use these to set colour weights on the line-up: the colourways that sold best get a bigger share of "
                   "their type's demand.")


# --- ③ buy plan ------------------------------------------------------------------------------

def _plan_settings(cid, settings):
    settings = dict(settings)
    heading("How do you want to plan?")
    labels, keys = list(col.MODES.values()), list(col.MODES)
    choice = st.segmented_control("Plan by", labels, default=col.MODES.get(settings["mode"], labels[0]), key=f"mode_{cid}",
                                  help="Your quantities: the numbers on the line-up, checked against demand. "
                                       "What demand supports: buy for expected sales plus safety stock. "
                                       "Revenue target: buy enough to support a sales target. "
                                       "Budget: the largest balanced order that fits the cash you have.") or labels[0]
    settings["mode"] = keys[labels.index(choice)]
    a, b, d = st.columns(3)
    settings["budget"] = a.number_input("Budget, incl. samples (£)", min_value=0.0, step=250.0,
                                        value=float(settings.get("budget") or 0), key=f"budget_{cid}",
                                        help="All the cash available for this collection: stock and samples still to pay.") or None
    settings["target_revenue"] = b.number_input("Revenue target (£)", min_value=0.0, step=500.0,
                                                value=float(settings.get("target_revenue") or 0), key=f"target_{cid}",
                                                help="Net sales you're aiming for over the selling period.") or None
    if settings["mode"] == "evidence":
        settings["ambition"] = d.slider("Ambition", 25, 300, int(round((settings.get("ambition") or 1) * 100)), 5,
                                        format="%d%%", key=f"amb_{cid}",
                                        help="100% = buy for expected demand. Higher if you expect to grow, lower to be cautious.") / 100
    with st.expander("Assumptions"):
        e1, e2, e3, e4, e5 = st.columns(5)
        settings["cover"] = 1 + e1.slider("Safety stock", 0, 100, int(round((settings["cover"] - 1) * 100)), 5, format="%d%%",
                                          key=f"cover_{cid}", help="Extra stock on top of expected sales so popular sizes don't sell out at the first pop-up.") / 100
        settings["min_per_size"] = int(e2.number_input("Minimum per size", 0, 5, int(settings.get("min_per_size") or 0),
                                                       key=f"minsize_{cid}", help="At least this many of every size offered."))
        settings["moq_round"] = e3.toggle("Round up to minimum order", value=bool(settings.get("moq_round")), key=f"moq_{cid}",
                                          help="If a colourway is below the supplier's minimum order, raise it to the minimum.")
        settings["soldout_uplift"] = e4.slider("Sold-out uplift", 0, 100, int(round(settings.get("soldout_uplift", 0.25) * 100)), 5,
                                               format="%d%%", key=f"uplift_{cid}",
                                               help="Extra demand assumed for sizes that sold out at the pop-up, as they might have sold more.") / 100
        settings["markup"] = e5.number_input("Markup for missing prices (×)", 1.5, 5.0, float(settings.get("markup") or 2.5), 0.1,
                                             format="%.1f", key=f"markup_{cid}",
                                             help="Used for a colourway with no retail price when your usual price for its type would leave a thin margin.")
    return settings


def _plan_view(c, cid, coll, plan):
    _callout(plan, coll["settings"])
    it = plan["items"]
    _cash_chart(it)
    _order_grid(c, cid, coll, plan)
    _item_table(it)
    _risk_chart(it, plan["totals"])
    with st.expander("Why these quantities: the reasoning for each colourway"):
        for r in it.itertuples():
            st.markdown(f"**{r.label}** · {CONF_ICON.get(r.confidence, r.confidence)}  \n{r.reasoning}"
                        + (f"  \n⚠️ {'; '.join(r.flags)}" if r.flags else ""))
    _download(plan, coll)


def _callout(plan, settings):
    t, mode, scale = plan["totals"], plan["mode"], plan["scale"]
    if mode == "draft":
        over = t["buy"] - t["expected_demand"]
        st.info(f"**Your quantities: {t['buy']:,.0f} units** against **{t['expected_demand']:,.0f} expected to sell** "
                f"this season. " + (f"That's {over:,.0f} more than demand suggests, so about {gbp(t['unsold_mid_cost'])} "
                                    "of stock (at cost) would be left over if sales follow history." if over > 0 else
                                    "That's below expected demand, so some sizes may sell out."))
    elif mode == "target" and t.get("target_revenue"):
        msg = (f"**Target vs evidence.** {gbp(t['target_revenue'])} would take about **{scale:.1f}×** expected demand. "
               f"The plan buys enough to support it, but **expected sales stay {gbp(t['revenue_mid'])}** "
               f"({gbp(t['revenue_low'])}–{gbp(t['revenue_high'])}).")
        (st.warning if scale > 1.5 else st.info)(msg)
    elif mode == "budget" and t.get("budget"):
        st.info(f"**Budget plan.** After {gbp(t['sample_cost'])} of samples, the largest balanced order that fits buys for "
                f"**{scale:.0%}** of expected demand (plus safety stock). "
                + ("Some demand would go unmet." if scale < 0.9 else "That covers expected demand."))


def _cash_chart(it):
    heading("Cash in vs expected sales, by colourway")
    d = it[it["buy"] > 0].sort_values("cash")
    fig = go.Figure()
    fig.add_bar(y=d["label"], x=d["cash"], name="Cash needed", orientation="h", marker_color=SERIES[1],
                hovertemplate="%{y}<br>Cash needed %{x:£,.0f}<extra></extra>")
    fig.add_bar(y=d["label"], x=d["revenue_mid"], name="Expected sales (range)", orientation="h", marker_color=SERIES[0],
                error_x=dict(type="data", symmetric=False, array=(d["revenue_high"] - d["revenue_mid"]).tolist(),
                             arrayminus=(d["revenue_mid"] - d["revenue_low"]).tolist(), color="rgba(200,200,200,0.8)", thickness=1.5),
                customdata=d[["revenue_low", "revenue_high"]].values,
                hovertemplate="%{y}<br>Expected sales %{x:£,.0f} (%{customdata[0]:£,.0f}–%{customdata[1]:£,.0f})<extra></extra>")
    fig = style_fig(fig, height=max(280, 34 * len(d) + 90))
    fig.update_layout(barmode="group", hovermode="closest")
    fig.update_yaxes(tickprefix="", gridcolor="rgba(0,0,0,0)")
    fig.update_xaxes(tickprefix="£", tickformat=",.0f")
    st.plotly_chart(fig, width="stretch")
    st.caption("Where the blue bar (with its range) is shorter than the orange one, that colourway is unlikely to earn back "
               "its cost this season on current evidence.")


def _order_grid(c, cid, coll, plan):
    heading("Your order by size")
    st.caption("Pre-filled from the plan. Type over any cell to set your own number; it's kept until you reset it.")
    sizes = plan["sizes"]
    order = sorted(sizes["size"].unique(), key=col.size_rank)
    final = sizes.pivot_table(index=["item_id", "name"], columns="size", values="final", aggfunc="first").reindex(columns=order)
    rec = sizes.pivot_table(index=["item_id", "name"], columns="size", values="recommended", aggfunc="first").reindex(columns=order)
    grid = final.reset_index()
    grid.insert(2, "Total", final.sum(axis=1).astype(int).values)
    ver = st.session_state.get(f"grid_ver_{cid}", 0)
    key = f"grid_{cid}_{ver}"
    overrides = dict(coll["overrides"])

    def save():
        changes = st.session_state.get(key, {}).get("edited_rows", {})
        for idx, cells in changes.items():
            row = grid.iloc[int(idx)]
            for size, val in cells.items():
                if size not in order or pd.isna(rec.loc[(row["item_id"], row["name"]), size]):
                    continue
                k = f"{int(row['item_id'])}|{size}"
                recommended = rec.loc[(row["item_id"], row["name"]), size]
                if val is None or (not pd.isna(val) and int(val) == int(recommended)):
                    overrides.pop(k, None)
                else:
                    overrides[k] = int(val)
        col.update_collection(conn(), cid, overrides=overrides)
        st.session_state[f"grid_ver_{cid}"] = ver + 1

    editor(grid, key=key, on_change=save, hide_index=True, width="stretch", disabled=["item_id", "name", "Total"],
           column_config={"item_id": None, "name": st.column_config.Column("Product", width="medium"),
                          "Total": UNITS("Total"), **{s: st.column_config.NumberColumn(s, min_value=0, step=1, format="%d") for s in order}})
    n_over = len(overrides)
    a, b = st.columns([1, 3])
    if a.button("Reset sizes", disabled=not n_over, key=f"reset_{cid}", help="Clear the sizes you've typed and go back to the plan."):
        col.update_collection(c, cid, overrides={})
        st.session_state[f"grid_ver_{cid}"] = ver + 1
        st.rerun()
    if n_over:
        b.caption(f"{n_over} size quantit{'ies' if n_over != 1 else 'y'} set by you.")


def _item_table(it):
    heading("By colourway")
    shown = it.assign(confidence=it["confidence"].map(CONF_ICON), flags=it["flags"].map("; ".join))
    table(shown[["label", "confidence", "expected_demand", "buy", "unit_cost", "cash", "sold_mid", "sell_through_mid",
                 "revenue_mid", "gross_profit_mid", "unsold_low_cost", "flags"]],
          hide_index=True, width="stretch", column_config={
              "label": st.column_config.Column("Product", width="medium"), "confidence": "Confidence",
              "expected_demand": st.column_config.NumberColumn("Expected units", format="%.1f"),
              "buy": UNITS("Your order"), "unit_cost": MONEY2("Landed cost"), "cash": MONEY("Cash needed"),
              "sold_mid": st.column_config.NumberColumn("Expected to sell", format="%.1f"),
              "sell_through_mid": PCT("Expected sell-through"), "revenue_mid": MONEY("Expected sales"),
              "gross_profit_mid": MONEY("Expected gross profit"), "unsold_low_cost": MONEY("Left over if sales are weak"),
              "flags": "Watch out for"})


def _risk_chart(it, t):
    heading("Where the biggest risks are")
    d = it[it["unsold_low_cost"] > 0.5].sort_values("unsold_low_cost")
    if d.empty:
        st.success("Even if sales are weak, nothing is left over.")
        return
    fig = go.Figure(go.Bar(y=d["label"], x=d["unsold_low_cost"], orientation="h", marker_color=SERIES[1],
                           text=[f"{gbp(v)} · {CONF_ICON.get(cf, cf)}" for v, cf in zip(d["unsold_low_cost"], d["confidence"])],
                           textposition="outside", cliponaxis=False,
                           hovertemplate="%{y}<br>Stock left at cost if sales are weak: %{x:£,.0f}<extra></extra>"))
    fig = style_fig(fig, height=max(240, 30 * len(d) + 80))
    fig.update_layout(hovermode="closest", margin=dict(r=170))
    fig.update_yaxes(tickprefix="", gridcolor="rgba(0,0,0,0)")
    fig.update_xaxes(tickprefix="£", tickformat=",.0f")
    st.plotly_chart(fig, width="stretch")
    st.caption(f"Stock (at cost) still unsold if sales come in at the low end of the range. "
               f"{gbp(t['low_confidence_cash'])} of the stock is in types with little or no sales history.")


def _download(plan, coll):
    sizes = plan["sizes"][["name", "size", "final"]]
    sizes = sizes[sizes["final"] > 0].rename(columns={"name": "Product", "size": "Size", "final": "Units"})
    it = plan["items"]
    buy = it[it["buy"] > 0][["supplier", "label", "buy", "currency", "blank_price", "unit_cost", "stock_cost", "sample_cost",
                             "lead_weeks"]].rename(columns={
        "supplier": "Supplier", "label": "Product", "buy": "Units", "currency": "Currency", "blank_price": "Supplier unit price",
        "unit_cost": "Landed cost (£)", "stock_cost": "Stock cost (£)", "sample_cost": "Samples to pay (£)",
        "lead_weeks": "Lead time (weeks)"}).sort_values(["Supplier", "Product"])
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        buy.to_excel(xl, sheet_name="Buy list by supplier", index=False)
        sizes.to_excel(xl, sheet_name="Sizes", index=False)
        it.drop(columns=["flags", "assumed", "next_break", "reasoning"]).assign(
            flags=it["flags"].map("; ".join)).to_excel(xl, sheet_name="Plan detail", index=False)
    st.download_button("Download buy list (Excel)", buf.getvalue(), file_name=f"{coll['name']} buy list.xlsx")


# --- ④ timeline ------------------------------------------------------------------------------

def _timeline_view(tl, settings):
    heading("When to order")
    pops = [p for p in settings.get("popups") or [] if p.get("date")]
    st.caption("Order-by date = first pop-up − supplier lead time − customising time "
               f"({settings.get('custom_buffer_weeks') or 0:g} weeks, set in Collection details).")
    if tl.empty:
        st.info("Nothing to order yet.")
        return
    table(tl.assign(status=tl["status"].map(STATUS_ICON), order_by=pd.to_datetime(tl["order_by"]).dt.date)[
        ["supplier", "status", "order_by", "lead_weeks", "units", "cash", "products"]],
        hide_index=True, width="stretch", column_config={
            "supplier": "Supplier", "status": "Status", "order_by": st.column_config.DateColumn("Order by", format="D MMM YYYY"),
            "lead_weeks": st.column_config.NumberColumn("Lead time (weeks)", format="%.1f"), "units": UNITS("Units"),
            "cash": MONEY("Cash needed"), "products": st.column_config.Column("Products", width="large")})
    dated = tl.dropna(subset=["order_by"])
    if pops and not dated.empty:
        first = min(pd.Timestamp(p["date"]) for p in pops)
        fig = go.Figure()
        for i, r in enumerate(dated.itertuples()):
            fig.add_bar(y=[r.supplier], x=[(first - r.order_by).total_seconds() * 1000], base=[r.order_by], orientation="h",
                        marker_color=SERIES[i % len(SERIES)], name=r.supplier, showlegend=False,
                        hovertemplate=f"{r.supplier}<br>Order by {r.order_by:%d %b}<extra></extra>")
        fig.add_vline(x=pd.Timestamp(date.today()), line_dash="dot", line_color="rgba(200,200,200,0.7)")
        for p in pops:
            fig.add_vline(x=pd.Timestamp(p["date"]), line_color=SERIES[2])
            fig.add_annotation(x=pd.Timestamp(p["date"]), y=1.06, yref="paper", text=p["name"], showarrow=False)
        fig = style_fig(fig, height=max(200, 50 * len(dated) + 90), money=False)
        fig.update_layout(hovermode="closest")
        fig.update_xaxes(type="date", tickformat="%d %b")
        st.plotly_chart(fig, width="stretch")
        st.caption("Each bar runs from the order-by date to the first pop-up. Dotted line = today.")


# --- ⑤ scenarios ------------------------------------------------------------------------------

PRESETS = {"Your quantities": {"mode": "draft"},
           "Demand, cautious": {"mode": "evidence", "ambition": 0.75, "cover": 1.0},
           "Demand, balanced": {"mode": "evidence", "ambition": 1.0, "cover": 1.15},
           "Fit the budget": {"mode": "budget"}}


def _scenarios_tab(c, cid, coll, ev, items):
    heading("Save and compare options")
    st.caption("A scenario is a snapshot of the plan settings and the sizes you've typed. Save a few, compare them side by "
               "side, and load one back to keep editing.")
    a, b, d = st.columns([2, 2, 1])
    name = a.text_input("Scenario name", key=f"scname_{cid}", placeholder="e.g. Fewer jackets, more knitwear")
    notes = b.text_input("Notes", key=f"scnotes_{cid}")
    if d.button("Save current plan", type="primary", key=f"scsave_{cid}", disabled=not name.strip()):
        col.save_scenario(c, cid, name.strip(), coll["settings"], coll["overrides"], notes)
        st.rerun()
    if st.button("Quick start: add the four standard scenarios", key=f"presets_{cid}",
                 help="Your quantities; demand at 75% with no safety stock; demand at 100% with 15%; and the budget. Your other settings are kept."):
        for nm, p in PRESETS.items():
            col.save_scenario(c, cid, nm, {**coll["settings"], **p}, coll["overrides"], "Preset")
        st.rerun()

    sc = col.list_scenarios(c, cid)
    if sc.empty:
        st.info("No scenarios saved yet.")
        return
    rows = []
    for s in sc.itertuples():
        p = col.build(ev, items, coll, settings=s.settings, overrides=s.overrides)
        t = p["totals"]
        if not t:
            continue
        approach = {"draft": "Your quantities", "evidence": f"Demand × {p['scale']:.0%}",
                    "target": f"Target {gbp(t.get('target_revenue') or 0)}", "budget": f"Budget {gbp(t.get('budget') or 0)}"}[p["mode"]]
        rows.append({"id": s.id, "Scenario": s.name, "Approach": approach, "Units": t["buy"], "Cash needed": t["cash"],
                     "Expected sales": t["revenue_mid"], "Sales if weak": t["revenue_low"], "Sales if strong": t["revenue_high"],
                     "Expected gross profit": t["gross_profit_mid"], "Left over (expected)": t["unsold_mid_cost"],
                     "Left over if weak": t["unsold_low_cost"], "Expected sell-through": t["sell_through_mid"],
                     "Break-even sell-through": t["break_even_sell_through"]})
    cmp_ = pd.DataFrame(rows)
    money_rows = ["Cash needed", "Expected sales", "Sales if weak", "Sales if strong", "Expected gross profit",
                  "Left over (expected)", "Left over if weak"]
    pct_rows = ["Expected sell-through", "Break-even sell-through"]
    side = cmp_.set_index("Scenario")[["Approach", "Units"] + money_rows + pct_rows].T
    shown = side.copy().astype(object)
    for label in side.index:
        for sc_name in side.columns:
            v = side.loc[label, sc_name]
            shown.loc[label, sc_name] = (v if label == "Approach" else f"{v:,.0f}" if label == "Units" else
                                         pct(v) if label in pct_rows else gbp(v))
    report_table(shown, subtotals=["Cash needed", "Expected sales", "Left over if weak"], fmt=str)

    e, f, g = st.columns([2, 1, 1])
    pick = e.selectbox("Scenario", cmp_["id"].tolist(), format_func=dict(zip(cmp_["id"], cmp_["Scenario"])).get, key=f"scpick_{cid}")
    if f.button("Load into plan", key=f"scload_{cid}", help="Replace the working plan's settings and sizes with this scenario's."):
        s = sc[sc["id"] == pick].iloc[0]
        col.update_collection(c, cid, settings=s["settings"], overrides=s["overrides"])
        widget_keys = ("mode", "amb", "target", "budget", "cover", "minsize", "moq", "uplift", "markup")
        for k in [k for k in st.session_state if k.endswith(f"_{cid}") and k.split("_")[0] in widget_keys]:
            st.session_state.pop(k)
        for k in ("grid_ver", "typedem_ver", "popups_ver"):
            st.session_state[f"{k}_{cid}"] = st.session_state.get(f"{k}_{cid}", 0) + 1
        st.rerun()
    confirm = g.checkbox("Confirm delete", key=f"scdelc_{cid}")
    if g.button("Delete", key=f"scdel_{cid}", disabled=not confirm):
        col.delete_scenario(c, pick)
        st.rerun()

