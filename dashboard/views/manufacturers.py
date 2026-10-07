from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from common import SERIES, conn, editor, gbp, heading, metric, style_fig, table
from tracker import sourcing

MONEY = lambda label: st.column_config.NumberColumn(label, format="£%.2f")  # noqa: E731
PCT = lambda label: st.column_config.NumberColumn(label, format="percent")  # noqa: E731


def render():
    heading("Manufacturers", "title")
    st.caption("Record quotes from manufacturers and compare what each would really cost you, once currency, freight, "
               "duty, import VAT and one-off setup and sample costs are included, against what you pay today.")
    c = conn()

    heading("Proposals")
    st.caption("One row per quote. Add rows at the bottom and delete with the row selector. Unit, setup and sample "
               "costs are in the quote's currency; freight per unit is in £. Click **Save proposals** to keep changes "
               "(shared with everyone using the dashboard).")
    saved = sourcing.load(c)
    edited = editor(
        saved[sourcing.FIELDS], key=f"proposals_{st.session_state.get('proposals_ver', 0)}", num_rows="dynamic",
        hide_index=True, width="stretch", column_config={
            "quote_date": st.column_config.DateColumn("Quote date", format="DD/MM/YYYY", default=date.today()),
            "manufacturer": st.column_config.TextColumn("Manufacturer", required=True),
            "country": st.column_config.TextColumn("Country"),
            "contact": st.column_config.TextColumn("Contact"),
            "product_type": st.column_config.SelectboxColumn("Product type", options=sourcing.PRODUCT_TYPES),
            "product": st.column_config.TextColumn("Product / spec"),
            "currency": st.column_config.SelectboxColumn("Currency", options=sourcing.CURRENCIES, default="GBP"),
            "unit_cost": st.column_config.NumberColumn("Unit price", min_value=0, format="%.2f"),
            "moq": st.column_config.NumberColumn("MOQ", min_value=1, step=1, format="%d"),
            "setup_cost": st.column_config.NumberColumn("Setup / tooling", min_value=0, format="%.2f"),
            "sample_cost": st.column_config.NumberColumn("Samples", min_value=0, format="%.2f"),
            "freight_per_unit": st.column_config.NumberColumn("Freight per unit (£)", min_value=0, format="£%.2f"),
            "duty_pct": st.column_config.NumberColumn("Duty %", min_value=0, max_value=100, format="%.1f%%"),
            "lead_time_weeks": st.column_config.NumberColumn("Lead time (weeks)", min_value=0, format="%.0f"),
            "retail_price": st.column_config.NumberColumn("Planned retail price (£)", min_value=0, format="£%.2f"),
            "quality": st.column_config.NumberColumn("Quality (1-5)", min_value=1, max_value=5, step=1, format="%d"),
            "status": st.column_config.SelectboxColumn("Status", options=sourcing.STATUSES, default="Quote received"),
            "notes": st.column_config.TextColumn("Notes"),
        })
    unsaved = not _same(edited, saved)
    a, b = st.columns([1, 4])
    if a.button("Save proposals", type="primary", disabled=not unsaved):
        sourcing.save(c, edited)
        st.session_state["proposals_ver"] = st.session_state.get("proposals_ver", 0) + 1
        st.rerun()
    if unsaved:
        b.caption("⚠️ Unsaved changes. The comparison below already includes them.")

    proposals = edited[edited["manufacturer"].fillna("").astype(str).str.strip() != ""]
    if proposals.empty:
        st.info("Add a quote above to start comparing. Required: manufacturer, unit price, currency and MOQ. "
                "Add a planned retail price to see margins.")
        return

    f1, f2, f3 = st.columns([2, 1, 1])
    types = sorted(proposals["product_type"].dropna().unique())
    pick = f1.multiselect("Product types", types, key="src_types",
                          help="Compare like with like: pick one type (e.g. T-Shirt) to see only those quotes.")
    hide_rejected = f2.toggle("Hide rejected", value=True, key="src_hide_rej", help="Leave out quotes marked Rejected.")
    with_vat = f3.toggle("Add 20% import VAT", value=True, key="src_vat",
                         help="Overseas suppliers: import VAT is charged at the border. Padre65 isn't VAT registered, "
                              "so it can't be reclaimed and is a real cost. Suppliers with no country or a UK country are treated as UK.")

    ev = sourcing.evaluate(c, proposals.reset_index(drop=True), include_import_vat=with_vat)
    if pick:
        ev = ev[ev["product_type"].isin(pick)]
    if hide_rejected:
        ev = ev[ev["status"] != "Rejected"]
    if ev.empty:
        st.info("No quotes match these filters.")
        return
    missing_fx = ev[ev["fx_rate"].isna()]
    if len(missing_fx):
        st.warning(f"Couldn't get an exchange rate for {', '.join(missing_fx['currency'].unique())}, so those quotes can't be converted to £.")

    _headline(ev)
    _chart(ev)

    heading("Comparison")
    ev = ev.assign(label=ev["manufacturer"] + " · " + ev["product"].fillna(ev["product_type"].fillna("")))
    table(ev[["manufacturer", "product_type", "product", "status", "country", "currency", "unit_cost", "unit_cost_gbp",
              "landed_cost", "moq", "upfront_cost", "cost_at_moq", "current_cost", "vs_current", "retail_price",
              "margin", "markup", "payback_units", "payback_share", "lead_time_weeks", "quality"]]
          .sort_values(["product_type", "cost_at_moq"]),
          hide_index=True, width="stretch", column_config={
              "manufacturer": "Manufacturer", "product_type": "Product type", "product": "Product / spec",
              "status": "Status", "country": "Country", "currency": "Currency",
              "unit_cost": st.column_config.NumberColumn("Unit price", format="%.2f"),
              "unit_cost_gbp": MONEY("Unit price (£)"), "landed_cost": MONEY("Landed cost"),
              "moq": st.column_config.NumberColumn("MOQ", format="%d"), "upfront_cost": MONEY("Minimum order cost"),
              "cost_at_moq": MONEY("Cost per unit at MOQ"), "current_cost": MONEY("Current cost (same type)"),
              "vs_current": PCT("vs current"), "retail_price": MONEY("Planned retail price (£)"),
              "margin": PCT("Margin at retail"), "markup": st.column_config.NumberColumn("Markup", format="%.1f×"),
              "payback_units": st.column_config.NumberColumn("Units to pay back", format="%d"),
              "payback_share": PCT("Payback as % of MOQ"),
              "lead_time_weeks": st.column_config.NumberColumn("Lead time (weeks)", format="%.0f"),
              "quality": st.column_config.NumberColumn("Quality (1-5)", format="%d")})

    best = sourcing.best_by_type(ev)
    if not best.empty:
        heading("Best option by product type")
        table(best, hide_index=True, width="stretch", column_config={
            "product_type": "Product type", "quotes": "Quotes", "cheapest": "Cheapest per unit (£)",
            "best_margin": "Best margin", "lowest_upfront": "Lowest minimum order (£)"})

    with st.expander("How the figures are worked out"):
        st.markdown("""
- **Unit price (£)** = the quoted unit price × today's exchange rate (European Central Bank rates).
- **Landed cost** = (unit price in £ + freight per unit) × (1 + duty %) × (1 + 20% import VAT if the supplier is
  overseas and the toggle is on).
- **Minimum order cost** = MOQ × landed cost + setup/tooling + samples (converted to £). The cash you'd need to place the first order.
- **Cost per unit at MOQ** = minimum order cost ÷ MOQ, so one-off costs are included. Use this to compare quotes.
- **vs current** compares cost per unit at MOQ with the average landed cost of the products you sell today in the same
  product type (from the cost sheet). Negative = cheaper than now.
- **Margin at retail** = (planned retail price − cost per unit at MOQ) ÷ retail price, before discounts, card fees and postage.
- **Units to pay back** = how many you'd need to sell at the retail price to recover the minimum order cost.
""")


def _same(a, b):
    cols = sourcing.FIELDS
    norm = lambda df: df[cols].reset_index(drop=True).astype(str).replace({"None": "", "nan": "", "NaT": "", "<NA>": ""})  # noqa: E731
    return len(a) == len(b) and norm(a).equals(norm(b))


def _headline(ev):
    plain = dict(delta_color="off", delta_arrow="off", border=True)
    row = st.columns(3)
    cheapest = ev.loc[ev["cost_at_moq"].idxmin()] if ev["cost_at_moq"].notna().any() else None
    metric(row[0], "Quotes compared", f"{len(ev)}", f"from {ev['manufacturer'].nunique()} manufacturers", **plain)
    metric(row[1], "Lowest cost per unit", gbp(cheapest["cost_at_moq"], 2) if cheapest is not None else "–",
           f"{cheapest['manufacturer']} · {cheapest['product_type'] or ''}" if cheapest is not None else "", **plain)
    best_margin = ev.loc[ev["margin"].idxmax()] if ev["margin"].notna().any() else None
    metric(row[2], "Best margin at retail", f"{best_margin['margin']:.0%}" if best_margin is not None else "–",
           best_margin["manufacturer"] if best_margin is not None else "add retail prices to see margins", **plain)


def _chart(ev):
    heading("Cost per unit at MOQ vs what you pay now")
    d = ev[ev["cost_at_moq"].notna()].copy()
    if d.empty:
        return
    d["label"] = d["manufacturer"] + " · " + d["product"].fillna(d["product_type"].fillna("")).astype(str)
    d = d.sort_values(["product_type", "cost_at_moq"], ascending=[True, False])
    fig = go.Figure()
    fig.add_bar(y=d["label"], x=d["cost_at_moq"], orientation="h", name="Quote: cost per unit at MOQ",
                marker_color=SERIES[0], text=[gbp(v, 2) for v in d["cost_at_moq"]], textposition="outside",
                cliponaxis=False, customdata=d[["landed_cost", "moq", "product_type"]].values,
                hovertemplate="%{y}<br>Cost per unit at MOQ %{x:£,.2f}<br>Landed %{customdata[0]:£,.2f} · MOQ %{customdata[1]}<extra></extra>")
    if d["current_cost"].notna().any():
        fig.add_bar(y=d["label"], x=d["current_cost"], orientation="h", name="You pay now (same product type)",
                    marker_color="rgba(128,128,128,0.55)",
                    hovertemplate="%{y}<br>Current average for this type %{x:£,.2f}<extra></extra>")
    fig = style_fig(fig, height=max(260, 46 * len(d) + 80))
    fig.update_layout(barmode="group", hovermode="closest", margin=dict(r=80))
    fig.update_yaxes(tickprefix="", showgrid=False)
    fig.update_xaxes(tickprefix="£", tickformat=",.0f")
    st.plotly_chart(fig, width="stretch")
