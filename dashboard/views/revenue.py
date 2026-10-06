import io

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from common import SERIES, conn, gbp, heading, ledger, metric, period_filter, style_fig
from tracker import explorer

TOP_N = 8
OTHER = "All others"
OTHER_COLOUR = "rgba(128,128,128,0.55)"


def _fmt(metric_name, v):
    if v is None or pd.isna(v):
        return "–"
    if metric_name in explorer.PERCENT_METRICS:
        return f"{v:.1%}"
    if metric_name in explorer.MONEY_METRICS:
        return gbp(v, 2 if abs(v) < 100 else 0)
    return f"{v:,.1f}" if v % 1 else f"{v:,.0f}"


def _period_label(ts, freq):
    ts = pd.Timestamp(ts)
    return {"D": ts.strftime("%d %b %y"), "W": "w/c " + ts.strftime("%d %b %y"), "M": ts.strftime("%b %Y"),
            "Q": f"Q{ts.quarter} {ts.year}", "Y": str(ts.year)}[freq]


def render():
    heading("Revenue", "title")
    start, end = period_filter()
    c = conn()
    L = ledger()
    data = explorer.facts(c, L)

    a, b, d = st.columns(3)
    metric_name = a.selectbox("Metric", list(explorer.METRICS), key="rev_metric",
                              help="What to measure. Ratios (%, average order value) are recalculated for each "
                                   "period and group, not averaged.")
    dimension = b.selectbox("Break down by", list(explorer.DIMENSIONS), key="rev_dim",
                            help="Split the metric into groups, e.g. one line per product or per size. Total = the whole business.")
    timescale = d.selectbox("Time scale", list(explorer.TIMESCALES), index=4, key="rev_time",
                            help="How to group dates. Rolling averages smooth out daily spikes by averaging the last 7 or 30 days.")
    if metric_name in explorer.TOTAL_ONLY and dimension != "Total":
        st.info("Net profit includes overheads (shoots, samples, software…) that can't be fairly split by "
                "product or size, so it's shown for the whole business. Use CM3 to compare groups.")
        dimension = "Total"
    num, den, additive = explorer.METRICS[metric_name]
    dim_col = explorer.DIMENSIONS[dimension]

    e, f, g = st.columns([2, 1, 1])
    only = []
    if dim_col:
        options = (data.groupby(dim_col)["net"].sum().sort_values(ascending=False).index.tolist())
        only = e.multiselect(f"Show only these ({dimension.lower()})", options, key=f"rev_only_{dim_col}",
                             help="Leave empty to include everything. The chart shows the top 8 and groups the rest as 'All others'.")
    style = f.segmented_control("Chart", ["Lines", "Stacked bars"] if additive else ["Lines"], default="Lines",
                                key="rev_style", help="Stacked bars show how groups add up to the total. Only for amounts and counts, not ratios.") or "Lines"
    cumulative = g.toggle("Running total", value=False, key="rev_cum", disabled=not additive,
                          help="Show the cumulative total to date instead of each period on its own.")

    out = explorer.series(c, L, data, metric_name, dimension, timescale, start, end, only, cumulative and additive)
    if out.empty:
        st.info("No data for this selection.")
        return
    freq, window = explorer.TIMESCALES[timescale]
    totals = explorer.totals_by_group(out if not (cumulative or window) else
                                      explorer.series(c, L, data, metric_name, dimension,
                                                      {"D": "Daily", "W": "Weekly", "M": "Monthly", "Q": "Quarterly", "Y": "Yearly"}[freq],
                                                      start, end, only), metric_name)
    ranked = totals.reindex(totals.abs().sort_values(ascending=False).index)

    # headline tiles
    overall = explorer.series(c, L, data, metric_name, "Total",
                              {"D": "Daily", "W": "Weekly", "M": "Monthly", "Q": "Quarterly", "Y": "Yearly"}[freq],
                              start, end, None if not dim_col else None)
    if dim_col and only:
        overall = explorer.series(c, L, data, metric_name, dimension,
                                  {"D": "Daily", "W": "Weekly", "M": "Monthly", "Q": "Quarterly", "Y": "Yearly"}[freq],
                                  start, end, only)
        overall = overall.groupby("period")[["num", "den"]].sum().reset_index()
        overall["value"] = overall["num"] / overall["den"] if den else overall["num"]
    total_value = (overall["num"].sum() / overall["den"].sum()) if den else overall["num"].sum()
    best = overall.loc[overall["value"].idxmax()] if overall["value"].notna().any() else None
    per_label = {"D": "day", "W": "week", "M": "month", "Q": "quarter", "Y": "year"}[freq]
    plain = dict(delta_color="off", delta_arrow="off", border=True)
    row = st.columns(3)
    metric(row[0], "Total for the period", _fmt(metric_name, total_value), metric_name, **plain)
    if additive:
        metric(row[1], "Average per period", _fmt(metric_name, overall["value"].mean()), f"per {per_label}", **plain)
    else:
        metric(row[1], "Average per period", _fmt(metric_name, overall["value"].mean()), f"simple average per {per_label}", **plain)
    metric(row[2], "Best period", _fmt(metric_name, best["value"]) if best is not None else "–",
           _period_label(best["period"], freq) if best is not None else "", **plain)

    # chart: top groups + 'All others'
    heading(f"{metric_name} over time" + (f" by {dimension.lower()}" if dim_col else ""),
            help="Each line (or bar segment) is one group. Hover to see every group's value for that period.")
    top = list(ranked.index[:TOP_N])
    chart = out.copy()
    if dim_col and len(ranked) > TOP_N:
        rest = chart[~chart["group"].isin(top)].groupby("period")[["num", "den"]].sum().reset_index()
        rest["value"] = rest["num"] / rest["den"] if den else rest["num"]
        if den:
            rest.loc[rest["den"] == 0, "value"] = float("nan")
        rest["group"] = OTHER
        chart = pd.concat([chart[chart["group"].isin(top)], rest], ignore_index=True)
        top = top + [OTHER]
    fig = go.Figure()
    money = metric_name in explorer.MONEY_METRICS
    for i, grp in enumerate(top):
        sub = chart[chart["group"] == grp].sort_values("period")
        colour = OTHER_COLOUR if grp == OTHER else SERIES[i % len(SERIES)]
        hover = "%{y:£,.0f}" if money else ("%{y:.1%}" if metric_name in explorer.PERCENT_METRICS else "%{y:,.1f}")
        if style == "Stacked bars":
            fig.add_bar(x=sub["period"], y=sub["value"], name=str(grp), marker_color=colour,
                        marker_line_color="rgba(255,255,255,1)", marker_line_width=1,
                        hovertemplate=hover + f"<extra>{grp}</extra>")
        else:
            fig.add_scatter(x=sub["period"], y=sub["value"], name=str(grp), mode="lines+markers" if len(sub) <= 40 else "lines",
                            line=dict(color=colour, width=2), marker=dict(size=7),
                            connectgaps=False, hovertemplate=hover + f"<extra>{grp}</extra>")
    fig = style_fig(fig, height=420, money=money)
    if style == "Stacked bars":
        fig.update_layout(barmode="relative")
    if metric_name in explorer.PERCENT_METRICS:
        fig.update_yaxes(tickprefix="", tickformat=".0%")
    elif not money:
        fig.update_yaxes(tickprefix="", tickformat=",.0f")
    if len(top) == 1:
        fig.update_layout(showlegend=False)
    st.plotly_chart(fig, width="stretch")
    if num in ("cm2", "cm3"):
        st.caption("CM2/CM3 split shared costs (fees, delivery, marketing) across sales by their share of that month's "
                   "revenue. A product that was the only sale in a busy-spending month carries all of that month's costs. "
                   "Costs in months with no sales appear as 'No sales that month'.")

    # table: groups x periods
    heading("Table", help="The same numbers as the chart, for every group (not just the top 8). The Total column covers the whole range.")
    pivot = out.pivot_table(index="group", columns="period", values="value", aggfunc="first")
    pivot = pivot.reindex(columns=sorted(pivot.columns))
    cols = [_period_label(p, freq) for p in pivot.columns]
    pivot.columns = cols
    if not (cumulative or window):
        pivot["Total"] = totals.reindex(pivot.index)
    pivot = pivot.reindex(ranked.index.intersection(pivot.index).tolist() + [i for i in pivot.index if i not in ranked.index])
    pivot.index.name = dimension if dim_col else ""
    if metric_name in explorer.PERCENT_METRICS:
        fmt = "percent"
    elif money:
        fmt = "£%.2f" if metric_name == "Average order value" else "£%.0f"
    else:
        fmt = "%.1f" if window else "%d"
    st.dataframe(pivot, width="stretch",
                 column_config={col: st.column_config.NumberColumn(col, format=fmt) for col in pivot.columns})

    long = out[["period", "group", "value"]].rename(columns={"group": dimension, "value": metric_name})
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        pivot.to_excel(xl, sheet_name="Table")
        long.to_excel(xl, sheet_name="Data (long)", index=False)
    st.download_button("Download (Excel)", buf.getvalue(), file_name=f"{metric_name} by {dimension} {timescale}.xlsx".replace("/", "-"))
