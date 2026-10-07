from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from common import SERIES, conn, editor, gbp, heading, ledger, metric, pct, style_fig, table
from tracker import db, ideas, sourcing

MONEY = lambda label: st.column_config.NumberColumn(label, format="£%.2f")  # noqa: E731
PCT = lambda label: st.column_config.NumberColumn(label, format="percent")  # noqa: E731
CURRENT_COLOUR = SERIES[0]
IDEA_COLOUR = SERIES[1]


def render():
    heading("Product log", "title")
    tab_ideas, tab_existing = st.tabs(["💡 New product ideas", "🗂️ Existing products"])
    with tab_ideas:
        _ideas()
    with tab_existing:
        _existing()


# --- new product ideas -----------------------------------------------------------

def _ideas():
    c = conn()
    L = ledger()
    rates = ideas.business_rates(L)
    current = ideas.current_range(c, L, rates)

    st.caption("Log products you're thinking of making and see where they'd sit in your range on price and "
               "contribution, before you commit. Ideas are saved and shared with everyone using the dashboard.")
    _add_idea_form(c)

    saved = ideas.load(c)
    if saved.empty:
        st.info("No ideas yet. Add one above to compare it with your current products.")
        _range_chart(current, pd.DataFrame(), list(ideas.METRICS)[0])
        return

    heading("Your ideas")
    edited = editor(saved[ideas.FIELDS], key=f"ideas_{st.session_state.get('ideas_ver', 0)}", num_rows="dynamic",
                    hide_index=True, width="stretch", column_config={
                        "name": st.column_config.TextColumn("Idea", required=True),
                        "product_type": st.column_config.SelectboxColumn("Product type", options=ideas.PRODUCT_TYPES),
                        "price": st.column_config.NumberColumn("Planned price (£)", min_value=0, format="£%.2f"),
                        "unit_cost": st.column_config.NumberColumn("Expected unit cost (£)", min_value=0, format="£%.2f"),
                        "cost_source": st.column_config.TextColumn("Cost from"),
                        "first_order_units": st.column_config.NumberColumn("First order (units)", min_value=0, step=1, format="%d"),
                        "status": st.column_config.SelectboxColumn("Idea status", options=ideas.STATUSES, default="Idea"),
                        "notes": st.column_config.TextColumn("Notes")})
    unsaved = not _same(edited, saved)
    a, b = st.columns([1, 4])
    if a.button("Save changes", type="primary", disabled=not unsaved, key="save_ideas"):
        ideas.save(c, edited)
        st.session_state["ideas_ver"] = st.session_state.get("ideas_ver", 0) + 1
        st.rerun()
    if unsaved:
        b.caption("⚠️ Unsaved changes. The charts below already include them.")

    f1, f2 = st.columns([3, 2])
    show_dropped = f2.toggle("Include dropped ideas", value=False, key="ideas_dropped",
                             help="Ideas with status Dropped are hidden from the comparison unless this is on.")
    live = edited if show_dropped else edited[edited["status"] != "Dropped"]
    ev = ideas.evaluate_ideas(live, rates)
    ev = ev[ev["price"].notna() & ev["unit_cost"].notna()]
    names = ev["name"].tolist()
    # newly added ideas join the comparison automatically; ones you've removed stay removed
    known = st.session_state.get("ideas_known", [])
    selected = [n for n in st.session_state.get("ideas_chosen", names) if n in names] + [n for n in names if n not in known]
    st.session_state["ideas_known"] = names
    st.session_state["ideas_chosen"] = list(dict.fromkeys(selected))
    chosen = f1.multiselect("Ideas to compare", names, key="ideas_chosen",
                            help="Pick which ideas appear in the charts below. New ideas are added automatically.")
    ev = ev[ev["name"].isin(chosen)]
    if ev.empty:
        st.info("Add a planned price and expected unit cost to an idea to compare it.")
        _range_chart(current, ev, list(ideas.METRICS)[0])
        return

    _idea_tiles(ev, current)
    metric_name = st.segmented_control("Compare on", list(ideas.METRICS), default=list(ideas.METRICS)[0],
                                       key="ideas_metric",
                                       help="Margin at full price = price − unit cost, before any discount. Expected "
                                            "figures assume ideas sell like the current range: the same average discount, "
                                            "and the same share of revenue going on fees, delivery and marketing.") \
        or list(ideas.METRICS)[0]
    _price_map(current, ev, metric_name)
    _range_chart(current, ev, metric_name)
    _stack_up_table(ev, current)

    with st.expander("How ideas are estimated"):
        st.markdown(f"""
- **Margin per unit at full price** = planned price − expected unit cost. Exact, no assumptions.
- **Expected price paid** = planned price × (1 − {rates['discount_rate']:.1%}), your average discount and returns so far.
- **Gross profit per unit (expected)** = expected price paid − unit cost.
- **CM3 per unit (expected)** = gross profit per unit − {rates['fulfil_rate']:.1%} of the price paid for fees and delivery
  − {rates['marketing_rate']:.1%} for marketing. These are the shares of revenue they take today.
- **Current products** use their **actual** results where they've sold, and the same estimates where they haven't sold yet.
- **Units to pay back** = first order cost ÷ expected gross profit per unit.
""")


def _add_idea_form(c):
    with st.expander("➕ Add a product idea", expanded=False):
        a, b = st.columns([2, 1])
        name = a.text_input("Name", key="idea_name", placeholder="e.g. Club Rugby Shirt - Navy")
        ptype = b.selectbox("Product type", ideas.PRODUCT_TYPES, key="idea_type")
        d, e, f = st.columns(3)
        price = d.number_input("Planned price (£)", min_value=0.0, step=5.0, format="%.2f", key="idea_price")
        source = e.radio("Unit cost from", ["Enter my own", "A manufacturer quote"], horizontal=True, key="idea_src",
                         help="Use a quote from the Manufacturers tab to bring in its cost per unit at MOQ (incl. freight, duty, import VAT and setup).")
        quote_label, unit_cost = None, None
        if source == "A manufacturer quote":
            quotes = sourcing.evaluate(c, sourcing.load(c))
            quotes = quotes[quotes["cost_at_moq"].notna()] if not quotes.empty else quotes
            if quotes.empty:
                f.caption("No quotes with a cost yet. Add them on the Manufacturers tab.")
            else:
                labels = {i: f"{r.manufacturer} · {r.product or r.product_type} · {gbp(r.cost_at_moq, 2)}/unit (MOQ {int(r.moq)})"
                          for i, r in quotes.iterrows()}
                pick = f.selectbox("Quote", list(labels), format_func=labels.get, key="idea_quote")
                q = quotes.loc[pick]
                unit_cost, quote_label = float(q["cost_at_moq"]), f"Quote: {q['manufacturer']}"
                f.caption(f"Unit cost: **{gbp(unit_cost, 2)}**")
        else:
            unit_cost = f.number_input("Expected unit cost (£, landed)", min_value=0.0, step=0.5, format="%.2f", key="idea_cost")
            quote_label = "My estimate"
        g, h, i = st.columns([1, 1, 2])
        units = g.number_input("First order (units)", min_value=0, step=10, key="idea_units",
                               help="Optional: how many you'd order first, to show the cash needed and payback.")
        status = h.selectbox("Status", ideas.STATUSES, key="idea_status")
        notes = i.text_input("Notes", key="idea_notes")
        if st.button("Add idea", type="primary", key="idea_add"):
            if not name.strip():
                st.error("Give the idea a name.")
            elif not price or not unit_cost:
                st.error("Enter a planned price and a unit cost.")
            else:
                ideas.add(c, name=name.strip(), product_type=ptype, price=price, unit_cost=unit_cost,
                          cost_source=quote_label, first_order_units=units or None, status=status, notes=notes)
                for k in ("idea_name", "idea_price", "idea_cost", "idea_units", "idea_notes"):
                    st.session_state.pop(k, None)
                st.session_state["ideas_ver"] = st.session_state.get("ideas_ver", 0) + 1
                st.rerun()


def _same(a, b):
    norm = lambda df: df[ideas.FIELDS].reset_index(drop=True).astype(str).replace({"None": "", "nan": "", "<NA>": ""})  # noqa: E731
    return len(a) == len(b) and norm(a).equals(norm(b))


def _idea_tiles(ev, current):
    plain = dict(delta_color="off", delta_arrow="off", border=True)
    best = ev.loc[ev["full_margin"].idxmax()]
    cur_avg_pct = (current["full_margin"].sum() / current["price"].sum()) if current["price"].sum() else None
    idea_pct = ev["full_margin"].sum() / ev["price"].sum() if ev["price"].sum() else None
    row = st.columns(3)
    metric(row[0], "Ideas compared", f"{len(ev)}", f"against {len(current)} current product groups", **plain)
    metric(row[1], "Best idea by margin per unit", gbp(best["full_margin"], 2), best["name"], **plain)
    metric(row[2], "Ideas' margin % at full price", pct(idea_pct), f"current range: {pct(cur_avg_pct)}", **plain)


def _price_map(current, ev, metric_name):
    col, kind = ideas.METRICS[metric_name]
    heading("Price vs " + metric_name.lower(),
            help="Each dot is a current product group (size = units sold); diamonds are your ideas. Further right = "
                 "higher price; higher up = more " + ("margin" if "argin" in metric_name else "profit") +
                 " per unit. Dashed lines show 50% and 75% margin at full price.")
    fig = go.Figure()
    cur = current[current[col].notna()]
    size = (cur["net_units"].clip(lower=0) ** 0.5 * 5 + 9).clip(upper=34)
    hover_val = "%{y:.0%}" if kind == "pct" else "%{y:£,.2f}"
    fig.add_scatter(x=cur["price"], y=cur[col], mode="markers", name="Current range",
                    marker=dict(size=size, color=CURRENT_COLOUR, opacity=0.75, line=dict(width=2, color="rgba(255,255,255,1)")),
                    customdata=cur[["name", "net_units", "basis"]].values,
                    hovertemplate="<b>%{customdata[0]}</b><br>Price %{x:£,.0f} · " + hover_val +
                                  "<br>%{customdata[1]:.0f} sold · %{customdata[2]}<extra></extra>")
    fig.add_scatter(x=ev["price"], y=ev[col], mode="markers+text", name="Ideas", text=ev["name"],
                    textposition="top center", textfont=dict(size=12),
                    marker=dict(size=18, symbol="diamond", color=IDEA_COLOUR, line=dict(width=2, color="rgba(255,255,255,1)")),
                    customdata=ev[["name", "unit_cost"]].values,
                    hovertemplate="<b>%{customdata[0]}</b> (idea)<br>Price %{x:£,.0f} · " + hover_val +
                                  "<br>Unit cost %{customdata[1]:£,.2f}<extra></extra>")
    if col == "full_margin":
        x_max = max(cur["price"].max(), ev["price"].max()) * 1.08
        for share, label in ((0.5, "50% margin"), (0.75, "75% margin")):
            fig.add_scatter(x=[0, x_max], y=[0, x_max * share], mode="lines", showlegend=False, hoverinfo="skip",
                            line=dict(color="rgba(128,128,128,0.5)", width=1, dash="dash"))
            fig.add_annotation(x=x_max, y=x_max * share, text=label, showarrow=False, xanchor="right", yanchor="bottom",
                               font=dict(size=11, color="rgba(128,128,128,0.9)"))
    fig = style_fig(fig, height=460, money=kind != "pct")
    fig.update_layout(hovermode="closest")
    fig.update_xaxes(title="Full price", tickprefix="£", tickformat=",.0f", showgrid=True, gridcolor="rgba(128,128,128,0.12)")
    if kind == "pct":
        fig.update_yaxes(tickprefix="", tickformat=".0%")
    st.plotly_chart(fig, width="stretch")


def _range_chart(current, ev, metric_name):
    col, kind = ideas.METRICS[metric_name]
    heading(f"Where ideas rank: {metric_name.lower()}",
            help="Every current product group and idea ranked on the chosen measure. Ideas are highlighted.")
    rows = [current[["name", col]].assign(kind="Current range")]
    if not ev.empty:
        rows.append(ev[["name", col]].assign(kind="Idea", name=ev["name"] + " (idea)"))
    d = pd.concat(rows, ignore_index=True).dropna(subset=[col]).sort_values(col)
    fmt = (lambda v: f"{v:.0%}") if kind == "pct" else (lambda v: gbp(v, 2))
    fig = go.Figure()
    for kind_name, colour in (("Current range", CURRENT_COLOUR), ("Idea", IDEA_COLOUR)):
        sub = d[d["kind"] == kind_name]
        if sub.empty:
            continue
        fig.add_bar(y=sub["name"], x=sub[col], orientation="h", name=kind_name, marker_color=colour,
                    text=[fmt(v) for v in sub[col]], textposition="outside", cliponaxis=False,
                    hovertemplate="%{y}: " + ("%{x:.0%}" if kind == "pct" else "%{x:£,.2f}") + "<extra></extra>")
    fig = style_fig(fig, height=max(320, 28 * len(d) + 90), money=kind != "pct")
    fig.update_layout(hovermode="closest", margin=dict(r=80), barmode="overlay")
    fig.update_yaxes(tickprefix="", gridcolor="rgba(0,0,0,0)", categoryorder="array", categoryarray=d["name"].tolist())
    if kind == "pct":
        fig.update_xaxes(tickprefix="", tickformat=".0%")
    else:
        fig.update_xaxes(tickprefix="£", tickformat=",.0f")
    st.plotly_chart(fig, width="stretch")


def _stack_up_table(ev, current):
    heading("How each idea stacks up")
    s = ideas.stack_up(ev, current)
    table(s, hide_index=True, width="stretch", column_config={
        "name": st.column_config.Column("Idea", width="medium"), "product_type": "Product type",
        "status": "Idea status", "price": MONEY("Planned price (£)"),
        "unit_cost": MONEY("Expected unit cost (£)"), "full_margin": MONEY("Margin per unit at full price"),
        "full_margin_pct": PCT("Margin % at full price"), "cm3_per_unit": MONEY("CM3 per unit (expected)"),
        "rank_margin": "Rank by margin per unit", "rank_pct": "Rank by margin %",
        "type_avg_price": MONEY("Same-type average price"), "type_avg_margin": MONEY("Same-type average margin"),
        "vs_type_margin": MONEY("vs same-type margin"), "first_order_cost": MONEY("First order cost"),
        "payback_units": st.column_config.NumberColumn("Units to pay back", format="%d")})


# --- existing product log -----------------------------------------------------------

def _existing():
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
    choice = st.selectbox("Product", products["product_title"].tolist(), key="log_product",
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
