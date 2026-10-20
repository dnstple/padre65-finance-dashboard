"""Collection & stock planning: build a collection, plan quantities around history, a target or a budget, compare scenarios."""
import io
from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from common import SERIES, conn, editor, gbp, heading, ledger, metric, pct, report_table, style_fig, table
from tracker import collection as col
from tracker import events

MONEY = lambda label: st.column_config.NumberColumn(label, format="£%.0f")  # noqa: E731
MONEY2 = lambda label: st.column_config.NumberColumn(label, format="£%.2f")  # noqa: E731
PCT = lambda label: st.column_config.NumberColumn(label, format="percent")  # noqa: E731
CONF_ICON = {"High": "🟢 High", "Medium": "🟡 Medium", "Low": "🟠 Low", "Your estimate": "🔵 Your estimate",
             "No evidence": "🔴 No evidence"}
PLENTY = dict(delta_color="off", delta_arrow="off", border=True)


def render():
    heading("Collection planner", "title")
    st.caption("Decide what to buy for an upcoming collection: which products, how many of each size, the cash it "
               "needs, the sales and margin it could support, and where the risks are. A planning tool only: it never "
               "places orders or changes stock.")
    c = conn()
    cid = _collection_picker(c)
    if cid is None:
        return
    coll = col.get_collection(c, cid)
    ev = col.evidence(c, ledger(), soldout_uplift=coll["settings"].get("soldout_uplift", 0.25))

    with st.expander("⚠️ What the history can and can't tell us", expanded=False):
        for note in col.limitations(ev):
            st.markdown(f"- {note}")

    t1, t2, t3, t4 = st.tabs(["① Products", "② Quantities & money", "③ Scenarios", "Evidence"])
    with t1:
        _products_tab(c, cid, coll, ev)
    items = col.load_items(c, cid)
    with t2:
        _plan_tab(c, cid, coll, ev, items)
    with t3:
        _scenarios_tab(c, cid, col.get_collection(c, cid), ev, items)
    with t4:
        _evidence_tab(ev, coll)


# --- collection picker / details -------------------------------------------------------

def _collection_picker(c):
    cols = col.list_collections(c)
    with st.expander("➕ New collection", expanded=cols.empty):
        with st.form("new_collection", clear_on_submit=True):
            a, b = st.columns([2, 1])
            name = a.text_input("Collection name", placeholder="e.g. Winter 26")
            season = b.text_input("Season", placeholder="e.g. AW26")
            d, e, f = st.columns(3)
            start = d.date_input("Selling from", date.today() + timedelta(days=30), format="DD/MM/YYYY")
            end = e.date_input("Selling until", date.today() + timedelta(days=150), format="DD/MM/YYYY")
            n_events = f.number_input("Pop-ups / events planned", min_value=0, max_value=20, value=1, step=1,
                                      help="Most past sales came from a pop-up, so the number of events in the selling period has a big effect on expected sales.")
            notes = st.text_area("Context (optional)", height=70, placeholder="Seasonal focus, marketing plans, what's new…")
            if st.form_submit_button("Create collection", type="primary"):
                if not name.strip():
                    st.error("Give the collection a name.")
                elif end <= start:
                    st.error("The selling period must end after it starts.")
                else:
                    new_id = col.create_collection(c, name.strip(), season, start, end, n_events, notes)
                    st.session_state["plan_cid"] = new_id
                    st.rerun()
    if cols.empty:
        st.info("Create a collection to start planning.")
        return None
    labels = {int(r.id): f"{r.name}" + (f" · {r.season}" if r.season else "") for r in cols.itertuples()}
    if st.session_state.get("plan_cid") not in labels:
        st.session_state["plan_cid"] = next(iter(labels))
    cid = st.selectbox("Collection", list(labels), format_func=labels.get, key="plan_cid",
                       help="Each collection has its own products, working plan and saved scenarios.")
    coll = col.get_collection(c, cid)
    period = (pd.Timestamp(coll["end_date"]) - pd.Timestamp(coll["start_date"])).days + 1
    st.caption(f"Selling {pd.Timestamp(coll['start_date']):%d %b %Y} – {pd.Timestamp(coll['end_date']):%d %b %Y} "
               f"({period} days) · {coll['planned_events'] or 0} pop-up/event(s) planned"
               + (f" · {coll['notes']}" if coll.get("notes") else ""))
    with st.expander("Edit collection details"):
        a, b = st.columns([2, 1])
        name = a.text_input("Collection name", coll["name"], key=f"cname_{cid}")
        season = b.text_input("Season", coll.get("season") or "", key=f"cseason_{cid}")
        d, e, f = st.columns(3)
        start = d.date_input("Selling from", pd.Timestamp(coll["start_date"]).date(), format="DD/MM/YYYY", key=f"cstart_{cid}")
        end = e.date_input("Selling until", pd.Timestamp(coll["end_date"]).date(), format="DD/MM/YYYY", key=f"cend_{cid}")
        n_events = f.number_input("Pop-ups / events planned", 0, 20, int(coll.get("planned_events") or 0), key=f"cevents_{cid}")
        notes = st.text_area("Context", coll.get("notes") or "", height=70, key=f"cnotes_{cid}")
        g, h = st.columns([1, 3])
        if g.button("Save details", key=f"csave_{cid}"):
            col.update_collection(c, cid, name=name, season=season, start_date=str(start), end_date=str(end),
                                  planned_events=int(n_events), notes=notes)
            st.rerun()
        confirm = h.checkbox("Delete this collection (products and scenarios too)", key=f"cdelc_{cid}")
        if confirm and h.button("Delete collection", key=f"cdel_{cid}"):
            col.delete_collection(c, cid)
            st.session_state.pop("plan_cid", None)
            st.rerun()
    return cid


# --- ① products ------------------------------------------------------------------------------

def _products_tab(c, cid, coll, ev):
    heading("Products in this collection")
    st.caption("Add what you're considering. Anything you leave blank (sizes, cost, price) is filled from the closest "
               "product you've sold and marked as assumed. Refine it as you learn more.")
    v = ev["variants"]
    range_ = (v.groupby(["product_title", "category"]).agg(units=("units", "sum"), price=("price", "max"),
                                                          unit_cost=("unit_cost", "max"),
                                                          sizes=("size", lambda s: ", ".join(sorted(set(s), key=col.size_rank))))
              .reset_index().sort_values("units", ascending=False))
    a, b = st.columns([4, 1])
    picks = a.multiselect("Quick add from your current range", range_["product_title"].tolist(), key=f"addrange_{cid}",
                          help="Restocks or repeats: brings in the product's type, sizes, cost and price, and uses its own sales as evidence.")
    if b.button("Add selected", key=f"addbtn_{cid}", disabled=not picks):
        for title in picks:
            r = range_[range_["product_title"] == title].iloc[0]
            col.add_item(c, cid, name=title, product_type=r["category"],
                         colour=events.split_title(title)[1],
                         sizes=r["sizes"], unit_cost=r["unit_cost"], price=r["price"], comparable=f"Product: {title}",
                         status="Likely")
        st.session_state.pop(f"addrange_{cid}", None)
        st.session_state[f"items_ver_{cid}"] = st.session_state.get(f"items_ver_{cid}", 0) + 1
        st.rerun()

    saved = col.load_items(c, cid)
    options = col.comparable_options(ev)
    edited = editor(saved[["id"] + col.ITEM_FIELDS], key=f"items_{cid}_{st.session_state.get(f'items_ver_{cid}', 0)}",
                    num_rows="dynamic", hide_index=True, width="stretch", column_config={
                        "id": None,
                        "name": st.column_config.TextColumn("Product", required=True),
                        "product_type": st.column_config.SelectboxColumn("Product type", options=col.PRODUCT_TYPES),
                        "colour": st.column_config.TextColumn("Colour"),
                        "sizes": st.column_config.TextColumn("Sizes", help="Comma separated, e.g. XS, S, M, L, XL or One size. Blank = use the comparable's sizes."),
                        "unit_cost": st.column_config.NumberColumn("Est. unit cost (£)", min_value=0, format="£%.2f"),
                        "price": st.column_config.NumberColumn("Selling price (£)", min_value=0, format="£%.2f"),
                        "comparable": st.column_config.SelectboxColumn("Compare with", options=options, default="Auto"),
                        "manual_units": st.column_config.NumberColumn("Your expected units", min_value=0, format="%.0f"),
                        "emphasis": st.column_config.NumberColumn("Emphasis", min_value=0.0, max_value=5.0, step=0.25, format="×%.2f", default=1.0),
                        "moq": st.column_config.NumberColumn("MOQ", min_value=0, step=1, format="%d"),
                        "status": st.column_config.SelectboxColumn("Plan status", options=col.STATUSES, default="Considering"),
                        "notes": st.column_config.TextColumn("Context / why"),
                    })
    unsaved = not _same_items(edited, saved)
    x, y = st.columns([1, 4])
    if x.button("Save products", type="primary", disabled=not unsaved, key=f"saveitems_{cid}"):
        col.save_items(c, cid, edited)
        st.session_state[f"items_ver_{cid}"] = st.session_state.get(f"items_ver_{cid}", 0) + 1
        st.rerun()
    if unsaved:
        y.caption("⚠️ Unsaved changes. Save to use them in the plan.")

    if saved.empty:
        return
    plan = col.build(ev, saved, coll)
    it = plan["items"]
    if it.empty:
        st.info("All products are marked Dropped.")
        return
    heading("How each product will be estimated")
    shown = it.assign(confidence=it["confidence"].map(CONF_ICON),
                      assumed=it["assumed"].map(lambda a: ", ".join(a) if a else ""))
    table(shown[["name", "basis", "confidence", "expected_demand", "sizes", "unit_cost", "price", "margin_pct", "assumed"]],
          hide_index=True, width="stretch", column_config={
              "name": "Product", "basis": "Evidence used", "confidence": "Confidence",
              "expected_demand": st.column_config.NumberColumn("Expected units", format="%.1f"), "sizes": "Sizes",
              "unit_cost": MONEY2("Est. unit cost (£)"), "price": MONEY2("Selling price (£)"),
              "margin_pct": PCT("Margin % at full price"), "assumed": "Filled in for you"})


def _same_items(a, b):
    cols = col.ITEM_FIELDS
    norm = lambda df: df[cols].reset_index(drop=True).astype(str).replace({"None": "", "nan": "", "<NA>": ""})  # noqa: E731
    return len(a) == len(b) and norm(a).equals(norm(b))


# --- ② quantities & money ---------------------------------------------------------------------

def _plan_tab(c, cid, coll, ev, items):
    if items.empty or items["name"].dropna().empty:
        st.info("Add products on the Products tab first.")
        return
    settings = dict(coll["settings"])
    heading("How do you want to plan?")
    mode_labels = list(col.MODES.values())
    mode_keys = list(col.MODES)
    choice = st.segmented_control("Plan by", mode_labels, default=col.MODES.get(settings["mode"], mode_labels[0]),
                                  key=f"mode_{cid}", help="History × ambition: buy around what past sales suggest, scaled up or down. "
                                  "Revenue target: buy enough to support a sales target. Stock budget: the biggest balanced order that fits the cash you have.") \
        or mode_labels[0]
    settings["mode"] = mode_keys[mode_labels.index(choice)]
    a, b = st.columns([2, 3])
    if settings["mode"] == "evidence":
        settings["ambition"] = a.slider("Ambition", 25, 300, int(round((settings.get("ambition") or 1) * 100)), 5,
                                        format="%d%%", key=f"amb_{cid}",
                                        help="100% = buy for what history suggests. Higher if you expect to grow (more marketing, more events), lower to be cautious.") / 100
    elif settings["mode"] == "target":
        settings["target_revenue"] = a.number_input("Revenue target for the selling period (£)", min_value=0.0, step=500.0,
                                                    value=float(settings.get("target_revenue") or 10000), key=f"target_{cid}",
                                                    help="Net sales you're aiming for. The plan buys enough stock to support it, but expected sales still come from the evidence.")
    else:
        settings["budget"] = a.number_input("Stock budget (£)", min_value=0.0, step=250.0,
                                            value=float(settings.get("budget") or 3000), key=f"budget_{cid}",
                                            help="Cash available for stock. The plan finds the largest balanced order that fits, after minimum orders and your own quantities.")

    with st.expander("Assumptions and emphasis"):
        e1, e2, e3, e4 = st.columns(4)
        settings["cover"] = 1 + e1.slider("Safety stock", 0, 100, int(round((settings["cover"] - 1) * 100)), 5, format="%d%%",
                                          key=f"cover_{cid}", help="Extra stock on top of planned sales so popular sizes don't sell out immediately.") / 100
        settings["min_per_size"] = int(e2.number_input("Minimum per size", 0, 5, int(settings.get("min_per_size") or 0),
                                                       key=f"minsize_{cid}", help="At least this many of every size offered, so the size run is complete."))
        settings["moq_round"] = e3.toggle("Round up to MOQ", value=bool(settings.get("moq_round")), key=f"moq_{cid}",
                                          help="If an item's quantity is below its minimum order, raise it to the MOQ.")
        settings["soldout_uplift"] = e4.slider("Sold-out uplift", 0, 100, int(round(settings.get("soldout_uplift", 0.25) * 100)), 5,
                                               format="%d%%", key=f"uplift_{cid}",
                                               help="Extra demand assumed for sizes that sold out at a pop-up, as they might have sold more.") / 100
        types = sorted(set(items.loc[items["status"] != "Dropped", "product_type"].dropna()))
        if types:
            st.markdown("**Emphasis by product type** (×1 = as history suggests)")
            cols = st.columns(min(len(types), 6))
            emph = dict(settings.get("category_emphasis") or {})
            for i, t in enumerate(types):
                emph[t] = cols[i % len(cols)].number_input(t, 0.0, 5.0, float(emph.get(t, 1.0)), 0.25, key=f"emph_{cid}_{t}",
                                                          help=f"Scale expected demand for every {t} up or down, e.g. ×1.5 for a winter focus on knitwear.")
            settings["category_emphasis"] = emph
    if settings != coll["settings"]:
        if settings["soldout_uplift"] != coll["settings"].get("soldout_uplift"):
            ev = col.evidence(c, ledger(), soldout_uplift=settings["soldout_uplift"])  # uplift changes the evidence itself
        col.update_collection(c, cid, settings=settings)
        coll["settings"] = settings

    plan = col.build(ev, items, coll)
    it, t = plan["items"], plan["totals"]
    if it.empty:
        st.info("All products are marked Dropped.")
        return
    for note in plan["notes"]:
        st.warning(note)

    heading("What it means")
    row = st.columns(4)
    metric(row[0], "Units to buy", f"{t['buy']:,.0f}", f"{len(it)} products", **PLENTY)
    metric(row[1], "Cash required", gbp(t["cash"]), "at estimated unit costs", **PLENTY)
    metric(row[2], "Expected sales", gbp(t["revenue_mid"]), f"range {gbp(t['revenue_low'])} – {gbp(t['revenue_high'])}", **PLENTY)
    metric(row[3], "If everything sells", gbp(t["potential_revenue"]), "at last season's average discount", **PLENTY)
    row = st.columns(4)
    metric(row[0], "Expected gross profit", gbp(t["gross_profit_mid"]), pct(t["gross_margin_mid"]) + " margin", **PLENTY)
    metric(row[1], "Stock left over (at cost)", gbp(t["unsold_mid_cost"]), f"cautious case {gbp(t['unsold_low_cost'])}", **PLENTY)
    metric(row[2], "Expected sell-through", pct(t["sell_through_mid"]), "share of units bought that sell", **PLENTY)
    metric(row[3], "Break-even sell-through", pct(t["break_even_sell_through"]), "share you must sell to get the cash back", **PLENTY)

    _target_callout(plan)
    _cash_chart(it)
    _order_grid(c, cid, coll, plan)
    _item_table(it)
    _risk_chart(it, t)

    with st.expander("Why these quantities: the reasoning for each product"):
        for r in it.itertuples():
            st.markdown(f"**{r.name}** · {CONF_ICON.get(r.confidence, r.confidence)}  \n{r.reasoning}"
                        + (f"  \n⚠️ {'; '.join(r.flags)}" if r.flags else ""))
    _download(plan, coll)


def _target_callout(plan):
    t = plan["totals"]
    mode, scale = plan["mode"], plan["scale"]
    expected = t["expected_value_unconstrained"]
    if mode == "target" and t.get("target_revenue"):
        msg = (f"**Target vs evidence.** Selling {gbp(t['target_revenue'])} would take about **{scale:.1f}×** what history "
               f"suggests ({gbp(expected)} if stock never ran out). The plan buys enough stock to support the target, but "
               f"**expected sales are still {gbp(t['revenue_mid'])}** ({gbp(t['revenue_low'])}–{gbp(t['revenue_high'])}). "
               f"If sales follow history, about **{gbp(t['unsold_mid_cost'])} of stock (at cost) would be left over**.")
        (st.warning if scale > 1.5 else st.info)(msg)
    elif mode == "budget" and t.get("budget"):
        st.info(f"**Budget plan.** The largest balanced order that fits {gbp(t['budget'])} buys for **{scale:.0%}** of the "
                f"demand history suggests. " + ("Some demand would go unmet; consider whether more cash is worth it."
                                                 if scale < 0.9 else "That covers expected demand with room to spare."))
    elif abs(scale - 1) > 0.01:
        st.info(f"**Ambition {scale:.0%}.** You're buying for {scale:.0%} of what history suggests. Expected sales stay at "
                f"{gbp(t['revenue_mid'])}, so the extra stock only sells if demand really grows.")


def _cash_chart(it):
    heading("Cash in vs expected sales, by product")
    d = it.sort_values("cash")
    fig = go.Figure()
    fig.add_bar(y=d["name"], x=d["cash"], name="Cash required", orientation="h", marker_color=SERIES[1],
                hovertemplate="%{y}<br>Cash required %{x:£,.0f}<extra></extra>")
    fig.add_bar(y=d["name"], x=d["revenue_mid"], name="Expected sales (range)", orientation="h", marker_color=SERIES[0],
                error_x=dict(type="data", symmetric=False, array=(d["revenue_high"] - d["revenue_mid"]).tolist(),
                             arrayminus=(d["revenue_mid"] - d["revenue_low"]).tolist(), color="rgba(128,128,128,0.8)", thickness=1.5),
                customdata=d[["revenue_low", "revenue_high"]].values,
                hovertemplate="%{y}<br>Expected sales %{x:£,.0f} (%{customdata[0]:£,.0f}–%{customdata[1]:£,.0f})<extra></extra>")
    fig = style_fig(fig, height=max(260, 50 * len(d) + 90))
    fig.update_layout(barmode="group", hovermode="closest")
    fig.update_yaxes(tickprefix="", gridcolor="rgba(0,0,0,0)")
    fig.update_xaxes(tickprefix="£", tickformat=",.0f")
    st.plotly_chart(fig, width="stretch")
    st.caption("Where the blue bar (with its range) is shorter than the orange one, that product is unlikely to earn back "
               "its stock cost in the selling period on current evidence.")


def _order_grid(c, cid, coll, plan):
    heading("Your order by size")
    st.caption("Recommended quantities are pre-filled. Type over any cell to use your own number; it's kept until you "
               "reset it. Blank cells are sizes that product isn't offered in (change sizes on the Products tab).")
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

    editor(grid, key=key, on_change=save, hide_index=True, width="stretch",
           disabled=["item_id", "name", "Total"],
           column_config={"item_id": None, "name": st.column_config.Column("Product", width="medium"),
                          "Total": st.column_config.NumberColumn("Total", format="%d"),
                          **{s: st.column_config.NumberColumn(s, min_value=0, step=1, format="%d") for s in order}})
    n_over = len(overrides)
    a, b = st.columns([1, 3])
    if a.button("Reset to recommended", disabled=not n_over, key=f"reset_{cid}",
                help="Clear all your own quantities and go back to the recommendation."):
        col.update_collection(c, cid, overrides={})
        st.session_state[f"grid_ver_{cid}"] = ver + 1
        st.rerun()
    if n_over:
        b.caption(f"{n_over} size quantit{'ies' if n_over != 1 else 'y'} set by you.")


def _item_table(it):
    heading("By product")
    shown = it.assign(confidence=it["confidence"].map(CONF_ICON), flags=it["flags"].map(lambda f: "; ".join(f)))
    table(shown[["name", "confidence", "expected_demand", "recommended", "buy", "cash", "sold_mid", "sell_through_mid",
                 "revenue_mid", "gross_profit_mid", "margin_pct", "unsold_low_cost", "flags"]],
          hide_index=True, width="stretch", column_config={
              "name": st.column_config.Column("Product", width="medium"), "confidence": "Confidence",
              "expected_demand": st.column_config.NumberColumn("Expected units", format="%.1f"),
              "recommended": st.column_config.NumberColumn("Recommended", format="%d"),
              "buy": st.column_config.NumberColumn("Your order", format="%d"), "cash": MONEY("Cash required"),
              "sold_mid": st.column_config.NumberColumn("Expected to sell", format="%.1f"),
              "sell_through_mid": PCT("Expected sell-through"), "revenue_mid": MONEY("Expected sales"),
              "gross_profit_mid": MONEY("Expected gross profit"), "margin_pct": PCT("Margin % at full price"),
              "unsold_low_cost": MONEY("Left over if sales are weak"), "flags": "Watch out for"})


def _risk_chart(it, t):
    heading("Where the biggest risks are")
    d = it[it["unsold_low_cost"] > 0.5].sort_values("unsold_low_cost")
    if d.empty:
        st.success("Even in the cautious case, nothing is left over.")
        return
    fig = go.Figure(go.Bar(y=d["name"], x=d["unsold_low_cost"], orientation="h", marker_color=SERIES[1],
                           text=[f"{gbp(v)} · {CONF_ICON.get(cf, cf)}" for v, cf in zip(d["unsold_low_cost"], d["confidence"])],
                           textposition="outside", cliponaxis=False,
                           hovertemplate="%{y}<br>Stock left at cost if sales are weak: %{x:£,.0f}<extra></extra>"))
    fig = style_fig(fig, height=max(220, 40 * len(d) + 80))
    fig.update_layout(hovermode="closest", margin=dict(r=170))
    fig.update_yaxes(tickprefix="", gridcolor="rgba(0,0,0,0)")
    fig.update_xaxes(tickprefix="£", tickformat=",.0f")
    st.plotly_chart(fig, width="stretch")
    st.caption(f"Stock (at cost) that would still be unsold if sales come in at the low end of the range. "
               f"{gbp(t['low_confidence_cash'])} of the order is in products with little or no evidence.")


def _download(plan, coll):
    sizes = plan["sizes"][["name", "size", "recommended", "final", "expected_demand"]].rename(columns={
        "name": "Product", "size": "Size", "recommended": "Recommended", "final": "Your order", "expected_demand": "Expected demand"})
    items = plan["items"].drop(columns=["flags", "assumed"]).assign(flags=plan["items"]["flags"].map("; ".join))
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        sizes[sizes["Your order"] > 0].to_excel(xl, sheet_name="Buy plan by size", index=False)
        items.to_excel(xl, sheet_name="By product", index=False)
        pd.DataFrame([plan["totals"]]).T.rename(columns={0: "value"}).to_excel(xl, sheet_name="Totals")
    st.download_button("Download plan (Excel)", buf.getvalue(), file_name=f"{coll['name']} plan.xlsx")


# --- ③ scenarios ------------------------------------------------------------------------------

PRESETS = {"Cautious": {"mode": "evidence", "ambition": 0.75, "cover": 1.0},
           "Balanced": {"mode": "evidence", "ambition": 1.0, "cover": 1.2},
           "Ambitious": {"mode": "evidence", "ambition": 1.5, "cover": 1.35}}


def _scenarios_tab(c, cid, coll, ev, items):
    if items.empty:
        st.info("Add products first.")
        return
    heading("Save and compare options")
    st.caption("A scenario is a snapshot of your planning settings and your own quantities. Save a few (cautious, "
               "balanced, ambitious, or different mixes), compare them side by side, and load one back to keep editing.")
    a, b, d = st.columns([2, 2, 1])
    name = a.text_input("Scenario name", key=f"scname_{cid}", placeholder="e.g. Balanced + heavier knitwear")
    notes = b.text_input("Notes", key=f"scnotes_{cid}")
    if d.button("Save current plan", type="primary", key=f"scsave_{cid}", disabled=not name.strip()):
        col.save_scenario(c, cid, name.strip(), coll["settings"], coll["overrides"], notes)
        st.rerun()
    if st.button("Quick start: add Cautious, Balanced and Ambitious", key=f"presets_{cid}",
                 help="Three scenarios from history: 75% with no safety stock, 100% with 20%, 150% with 35%. Your other settings and quantities are kept."):
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
        approach = {"evidence": f"History × {p['scale']:.0%}", "target": f"Target {gbp(t.get('target_revenue') or 0)}",
                    "budget": f"Budget {gbp(t.get('budget') or 0)}"}[p["mode"]]
        rows.append({"id": s.id, "Scenario": s.name, "Approach": approach, "Units": t["buy"], "Cash required": t["cash"],
                     "Expected sales": t["revenue_mid"], "Sales if weak": t["revenue_low"], "Sales if strong": t["revenue_high"],
                     "If everything sells": t["potential_revenue"], "Expected gross profit": t["gross_profit_mid"],
                     "Left over (expected)": t["unsold_mid_cost"], "Left over if weak": t["unsold_low_cost"],
                     "Expected sell-through": t["sell_through_mid"], "Break-even sell-through": t["break_even_sell_through"],
                     "In low-evidence products": t["low_confidence_cash"], "Notes": s.notes})
    cmp_ = pd.DataFrame(rows)
    # one column per scenario, one row per measure: easiest way to compare options side by side
    money_rows = ["Cash required", "Expected sales", "Sales if weak", "Sales if strong", "If everything sells",
                  "Expected gross profit", "Left over (expected)", "Left over if weak", "In low-evidence products"]
    pct_rows = ["Expected sell-through", "Break-even sell-through"]
    side = cmp_.set_index("Scenario")[["Approach", "Units"] + money_rows + pct_rows].T
    shown = side.copy().astype(object)
    for label in side.index:
        for sc_name in side.columns:
            v = side.loc[label, sc_name]
            shown.loc[label, sc_name] = (v if label == "Approach" else f"{v:,.0f}" if label == "Units" else
                                         pct(v) if label in pct_rows else gbp(v))
    report_table(shown, subtotals=["Cash required", "Expected sales", "Left over if weak"], fmt=str)

    heading("Cash, sales and risk by scenario")
    fig = go.Figure()
    for label, col_, colour in (("Cash required", "Cash required", SERIES[1]), ("Expected sales", "Expected sales", SERIES[0]),
                                ("Left over if weak (at cost)", "Left over if weak", SERIES[3])):
        fig.add_bar(x=cmp_["Scenario"], y=cmp_[col_], name=label, marker_color=colour,
                    hovertemplate="%{x}<br>" + label + " %{y:£,.0f}<extra></extra>")
    fig = style_fig(fig, height=360)
    fig.update_layout(barmode="group")
    st.plotly_chart(fig, width="stretch")
    st.caption("More stock raises sales only up to what demand supports. Beyond that, it mostly adds to what's left over.")

    e, f, g = st.columns([2, 1, 1])
    pick = e.selectbox("Scenario", cmp_["id"].tolist(), format_func=dict(zip(cmp_["id"], cmp_["Scenario"])).get, key=f"scpick_{cid}")
    if f.button("Load into plan", key=f"scload_{cid}", help="Replace the working plan's settings and quantities with this scenario's."):
        s = sc[sc["id"] == pick].iloc[0]
        col.update_collection(c, cid, settings=s["settings"], overrides=s["overrides"])
        widget_keys = ("mode", "amb", "target", "budget", "cover", "minsize", "moq", "uplift")
        for k in [k for k in st.session_state if (k.endswith(f"_{cid}") and k.split("_")[0] in widget_keys)
                  or k.startswith(f"emph_{cid}_")]:
            st.session_state.pop(k)
        st.session_state[f"grid_ver_{cid}"] = st.session_state.get(f"grid_ver_{cid}", 0) + 1
        st.rerun()
    confirm = g.checkbox("Confirm delete", key=f"scdelc_{cid}")
    if g.button("Delete", key=f"scdel_{cid}", disabled=not confirm):
        col.delete_scenario(c, pick)
        st.rerun()


# --- evidence ---------------------------------------------------------------------------------

def _evidence_tab(ev, coll):
    period = (pd.Timestamp(coll["end_date"]) - pd.Timestamp(coll["start_date"])).days + 1
    n_events = int(coll.get("planned_events") or 0)
    heading("What history shows")
    st.caption(f"Each product's demand evidence, projected over this collection's {period}-day selling period with "
               f"{n_events} pop-up/event(s).")
    pe = col.products_evidence(ev, period, n_events)
    pe = pe[pe["units"] > 0]
    table(pe.assign(confidence=pe["confidence"].map(CONF_ICON))[
        ["product_title", "category", "units", "online_units", "event_units", "online_per_30", "event_per_event",
         "sold_out_sizes", "discount_rate", "expected_period", "confidence"]],
        hide_index=True, width="stretch", column_config={
            "product_title": st.column_config.Column("Product", width="medium"), "category": "Product type",
            "units": st.column_config.NumberColumn("Units sold (net)", format="%d"),
            "online_units": st.column_config.NumberColumn("Online", format="%d"),
            "event_units": st.column_config.NumberColumn("At events", format="%d"),
            "online_per_30": st.column_config.NumberColumn("Online per 30 days", format="%.1f"),
            "event_per_event": st.column_config.NumberColumn("Per event", format="%.1f"),
            "sold_out_sizes": "Sold-out sizes", "discount_rate": PCT("Discount rate"),
            "expected_period": st.column_config.NumberColumn("Expected in this period", format="%.1f"),
            "confidence": "Confidence"})

    heading("Size mix by product type")
    v = ev["variants"]
    demand = v["online_rate"] * period + v["event_per_event"] * n_events
    sized = v[(v["size"] != "One size") & (demand > 0)]
    if not sized.empty:
        curve = demand[sized.index].groupby([sized["category"], sized["size"]]).sum().unstack(fill_value=0)
        curve = curve.reindex(columns=sorted(curve.columns, key=col.size_rank))
        share = curve.div(curve.sum(axis=1), axis=0)
        st.dataframe(share, width="stretch", column_config={s: st.column_config.NumberColumn(s, format="percent") for s in share.columns})
        st.caption("Share of expected demand by size within each type, after the sold-out adjustments. New products "
                   "use their type's mix, blended with their own sales where they have any.")

    with st.expander("How the recommendations are worked out"):
        st.markdown(f"""
- **Expected units** for each product = online sales rate × {period} days + sales per pop-up × {n_events} event(s),
  then × your emphasis (product and type).
  - *Online rate* = units sold online ÷ days in stock. Sizes that sold out stop the clock at their last sale, so selling
    out quickly counts as higher demand (capped at double).
  - *Per event* = units sold at pop-ups ÷ number of pop-ups so far. Sizes that sold out at a pop-up get the sold-out uplift.
  - Refunded units are taken off. Prices allow for each product's average discount.
- **Evidence used**: the product's own sales if it has sold; otherwise the same design in other colours (average per
  colourway); otherwise the average for its product type; otherwise the average product. You can choose the comparison
  or enter your own expected units.
- **Confidence** reflects how much evidence there is (High ≥ 15 units, Medium 5–14, Low < 5 or borrowed from similar
  products). The sales range widens with lower confidence (±25% High to ±75% No evidence).
- **Recommended order** = expected units × plan scale × (1 + safety stock), split across sizes by the product's own
  size mix blended with its type's mix, then at least the minimum per size, and rounded up to MOQ if you choose.
- **Plan scale**: your ambition %, or whatever is needed to reach the revenue target, or the largest order that fits the budget.
- **Outcomes are always judged against expected demand, not the target**: each size sells the smaller of what you buy
  and what's expected (and the low/high range). Unsold units are valued at cost.
""")
