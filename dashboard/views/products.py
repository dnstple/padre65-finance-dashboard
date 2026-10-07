import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from common import SERIES, gbp, heading, ledger, period_filter, style_fig, table
from glossary import GLOSSARY

MONEY = lambda label: st.column_config.NumberColumn(label, format="£%.2f")  # noqa: E731
PCT = lambda label: st.column_config.NumberColumn(label, format="percent")  # noqa: E731
LEVELS = ["Grouped", "Product", "Variant"]
SPLIT_VIEWS = ["£ total", "£ per unit", "% of revenue"]
# where each pound of revenue goes, in stacking order (colour follows the part, not its size)
PARTS = [
    ("cogs", "Product cost", SERIES[1]),
    ("fees", "Fees & fulfilment", SERIES[3]),
    ("marketing", "Marketing", SERIES[2]),
    ("cm3", "Profit after marketing (CM3)", SERIES[0]),
]


def render():
    heading("Products & contribution", "title")
    start, end = period_filter()
    L = ledger()
    groups = L.grouped_contribution(start, end)
    if groups.empty:
        st.info("No sales in this period.")
        return
    if groups["missing_cost"].any():
        names = ", ".join(groups.loc[groups["missing_cost"], "group"])
        st.warning(f"No unit cost for: {names}. Their COGS is £0, so margins are overstated. Add costs on **Data & sync**.")

    _summary(groups)
    st.divider()
    _detail(L, start, end)
    st.divider()
    _revenue_split(groups)


def _summary(groups):
    heading("By product type")
    st.caption("Products that are the same thing (same type, unit cost and price) are joined, e.g. all Club Long "
               "Sleeve colours, or the £50 caps that cost the same to make. Sorted by revenue.")
    g = groups.copy()
    total = {
        "group": "All products", "net_units": int(g["net_units"].sum()), "net_revenue": g["net_revenue"].sum(),
        "unit_cost": None, "avg_price_paid": g["net_revenue"].sum() / max(g["net_units"].sum(), 1),
        "cm1_per_unit": g["cm1"].sum() / max(g["net_units"].sum(), 1),
        "cm1_pct": g["cm1"].sum() / g["net_revenue"].sum() if g["net_revenue"].sum() else None,
        "cm2_pct": g["cm2"].sum() / g["net_revenue"].sum() if g["net_revenue"].sum() else None,
        "cm3_pct": g["cm3"].sum() / g["net_revenue"].sum() if g["net_revenue"].sum() else None,
    }
    cols = ["group", "net_units", "net_revenue", "unit_cost", "avg_price_paid", "cm1_per_unit", "cm1_pct", "cm2_pct", "cm3_pct"]
    shown = pd.concat([g[cols], pd.DataFrame([total])], ignore_index=True)
    small = lambda col: {**col, "width": "small"}  # noqa: E731
    table(shown, hide_index=True, width="stretch", height=35 * (len(shown) + 1) + 3, column_config={
        "group": st.column_config.Column("Product group", width="medium"),
        "net_units": st.column_config.NumberColumn("Units sold (net)", format="%d"),
        "net_revenue": small(st.column_config.NumberColumn("Net revenue", format="£%.0f", help=GLOSSARY["Net revenue (product)"])),
        "unit_cost": small(MONEY("Unit cost")), "avg_price_paid": MONEY("Avg price paid"),
        "cm1_per_unit": MONEY("Gross profit per unit"), "cm1_pct": small(PCT("CM1 %")),
        "cm2_pct": small(PCT("CM2 %")), "cm3_pct": small(PCT("CM3 %"))})


def _detail(L, start, end):
    heading("Detailed breakdown")
    if st.session_state.get("prod_level") not in (None, *LEVELS):
        st.session_state.pop("prod_level")
    by = st.segmented_control("Level", LEVELS, default="Grouped", key="prod_level",
                              help="Grouped = products that are the same thing commercially (same type, unit cost and "
                                   "price) joined together. Product = each Shopify product. Variant = each size.") or "Grouped"
    pc = L.grouped_contribution(start, end) if by == "Grouped" else L.product_contribution(start, end, by=by.lower())
    if pc.empty:
        return
    name_cols = {"Grouped": ["group", "includes"], "Product": ["product_title"],
                 "Variant": ["product_title", "variant_title", "sku"]}[by]
    cols = name_cols + ["net_units", "units_returned", "full_price", "avg_price_paid", "unit_cost", "full_price_margin",
                        "cm1_per_unit", "net_revenue", "cogs", "cm1", "cm1_pct", "alloc_fulfilment_fees", "cm2", "cm2_pct",
                        "alloc_marketing", "cm3", "cm3_pct", "cm3_per_unit", "missing_cost"]
    table(pc[cols], hide_index=True, width="stretch", column_config={
        "group": "Product group", "includes": "Includes", "product_title": "Product", "variant_title": "Variant",
        "sku": "SKU", "net_units": "Units sold (net)", "units_returned": "Returned",
        "full_price": MONEY("Full price"), "avg_price_paid": MONEY("Avg price paid"), "unit_cost": MONEY("Unit cost"),
        "full_price_margin": MONEY("Margin per unit at full price"), "cm1_per_unit": MONEY("Gross profit per unit"),
        "net_revenue": st.column_config.NumberColumn("Net revenue", format="£%.2f", help=GLOSSARY["Net revenue (product)"]),
        "cogs": MONEY("COGS"), "cm1": MONEY("Gross profit (CM1)"), "cm1_pct": PCT("CM1 %"),
        "alloc_fulfilment_fees": MONEY("Fees & fulfilment (alloc.)"), "cm2": MONEY("CM2"), "cm2_pct": PCT("CM2 %"),
        "alloc_marketing": MONEY("Marketing (alloc.)"), "cm3": MONEY("CM3"), "cm3_pct": PCT("CM3 %"),
        "cm3_per_unit": MONEY("CM3 per unit"), "missing_cost": st.column_config.CheckboxColumn("No cost")})
    st.caption("CM1 is exact. Fees, fulfilment and marketing are shared costs, so they're allocated to products "
               "by share of net revenue for the period.")


def _revenue_split(groups):
    heading("Where the revenue goes")
    view = st.segmented_control("Show as", SPLIT_VIEWS, default="£ total", key="split_view",
                                help="£ total = the whole period. £ per unit = for one unit sold. "
                                     "% of revenue = each part as a share of the group's revenue, so groups of "
                                     "different sizes can be compared.") or "£ total"
    d = groups[groups["net_units"] > 0].copy()
    d["fees"] = -d["alloc_fulfilment_fees"]
    d["marketing"] = -d["alloc_marketing"]
    if view == "£ per unit":
        for col, _, _ in PARTS:
            d[col] = d[col] / d["net_units"]
    elif view == "% of revenue":
        for col, _, _ in PARTS:
            d[col] = (d[col] / d["net_revenue"]).where(d["net_revenue"] != 0)
    d = d.iloc[::-1]  # largest revenue at the top of the chart

    is_pct = view == "% of revenue"
    fig = go.Figure()
    for col, label, colour in PARTS:
        fmt = "{:.0%}" if is_pct else "£{:,.0f}" if view == "£ total" else "£{:,.2f}"
        fig.add_bar(y=d["group"], x=d[col], name=label, orientation="h", marker_color=colour,
                    marker_line_color="rgba(255,255,255,1)", marker_line_width=2,
                    customdata=[fmt.format(v) if pd.notna(v) else "–" for v in d[col]],
                    hovertemplate="%{customdata}<extra>" + label + "</extra>")
    fig = style_fig(fig, height=max(320, 34 * len(d) + 100), money=not is_pct)
    fig.update_layout(barmode="relative", hovermode="y unified")
    fig.update_yaxes(tickprefix="", gridcolor="rgba(0,0,0,0)")
    if is_pct:
        fig.update_xaxes(tickprefix="", tickformat=".0%")
    else:
        fig.update_xaxes(tickprefix="£", tickformat=",.0f")
    st.plotly_chart(fig, width="stretch")
    st.caption("Each bar is a product group's net revenue split into product cost, its share of fees & fulfilment and "
               "marketing, and the profit left after marketing (CM3). A CM3 segment to the left of zero means that "
               "group lost money after its share of costs.")
