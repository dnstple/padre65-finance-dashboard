import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _load_streamlit_secrets():
    """On Streamlit Cloud settings live in st.secrets - expose them as env vars before tracker.config reads them."""
    import os
    try:
        for key, value in st.secrets.items():
            if isinstance(value, (str, int, float)) and not os.environ.get(key):
                os.environ[key] = str(value)
    except Exception:  # no secrets file locally - .env is used instead
        pass


_load_streamlit_secrets()

from tracker import config, db, reports  # noqa: E402

# Categorical slots, fixed order (validated palette - see dataviz reference)
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
GOOD, CRITICAL = "#0ca30c", "#d03b3b"
TYPE_COLORS = {"marketing": SERIES[0], "fulfilment": SERIES[1], "opex": SERIES[2], "inventory": SERIES[3]}
TYPE_LABELS = {"marketing": "Marketing", "fulfilment": "Fulfilment", "opex": "Overheads",
               "inventory": "Stock purchases", "other_income": "Other income", "excluded": "Excluded"}


def require_password():
    """Ask for DASHBOARD_PASSWORD (from secrets/.env) once per browser session. No password set = no gate."""
    import hmac
    import os

    expected = os.environ.get("DASHBOARD_PASSWORD", "")
    if not expected or st.session_state.get("authenticated"):
        return
    _, middle, _ = st.columns([1, 2, 1])
    with middle:
        st.title("Padre65 Finance")
        with st.form("login"):
            entered = st.text_input("Password", type="password")
            if st.form_submit_button("Log in", type="primary"):
                if hmac.compare_digest(entered.encode(), expected.encode()):
                    st.session_state["authenticated"] = True
                    st.rerun()
                st.error("Incorrect password")
    st.stop()


def conn():
    return db.connect()


def ledger():
    return reports.Ledger(conn())


def gbp(v, decimals=0):
    if v is None or pd.isna(v):
        return "–"
    s = f"£{abs(v):,.{decimals}f}"
    return f"-{s}" if v < -0.0049 else s


def pct(v):
    return "–" if v is None or pd.isna(v) else f"{v:.0%}"


def period_filter():
    """Sidebar date range. Returns (start, end) dates or (None, None) for all time."""
    today = date.today()
    options = {
        "All time": (None, None),
        "This month": (today.replace(day=1), today),
        "Last month": ((today.replace(day=1) - timedelta(days=1)).replace(day=1), today.replace(day=1) - timedelta(days=1)),
        "Last 3 months": ((pd.Timestamp(today) - pd.DateOffset(months=3)).date().replace(day=1), today),
        "Year to date": (today.replace(month=1, day=1), today),
        "Custom": None,
    }
    choice = st.sidebar.selectbox("Period", list(options), key="period")
    if choice == "Custom":
        rng = st.sidebar.date_input("Date range", (today.replace(day=1), today), key="custom_range")
        return (rng[0], rng[1]) if isinstance(rng, (list, tuple)) and len(rng) == 2 else (rng[0], rng[0])
    return options[choice]


def style_fig(fig, height=340, money=True):
    fig.update_layout(
        height=height, margin=dict(l=8, r=8, t=36, b=8),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0, title=None),
        hovermode="x unified", bargap=0.35, font=dict(size=13),
    )
    fig.update_xaxes(showgrid=False, title=None)
    fig.update_yaxes(gridcolor="rgba(128,128,128,0.18)", zeroline=True, zerolinecolor="rgba(128,128,128,0.45)",
                     title=None, tickprefix="£" if money else "", tickformat=",.0f" if money else None)
    return fig


def bar_line_chart(df, x, bars, line=None):
    """Bars for one or more series plus an optional line; all on one £ axis."""
    fig = go.Figure()
    for i, (col, label) in enumerate(bars):
        fig.add_bar(x=df[x], y=df[col], name=label, marker_color=SERIES[i], marker_line_width=0,
                    hovertemplate="%{y:£,.0f}<extra>" + label + "</extra>")
    if line:
        col, label = line
        fig.add_scatter(x=df[x], y=df[col], name=label, mode="lines+markers", line=dict(color=SERIES[len(bars)], width=2),
                        marker=dict(size=8, line=dict(width=2, color="white")),
                        hovertemplate="%{y:£,.0f}<extra>" + label + "</extra>")
    return style_fig(fig)


def pnl_table(p):
    """Format a P&L frame for display, with subtotal rows highlighted."""
    subtotals = p.attrs.get("subtotals", [])
    shown = p.map(lambda v: gbp(v))

    def highlight(row):
        if row.name in subtotals:
            return ["font-weight: 700; background-color: rgba(42,120,214,0.10)"] * len(row)
        return [""] * len(row)

    return shown.style.apply(highlight, axis=1)


def sidebar_status():
    c = conn()
    rows = c.execute("SELECT source, last_run, status, message FROM sync_log").fetchall()
    with st.sidebar.expander("Data status", expanded=False):
        if config.DATABASE_URL:
            host = config.DATABASE_URL.split("@")[-1].split("/")[0].split(":")[0]
            st.caption(f"🗄️ Database: Postgres ({'Supabase' if 'supabase' in host else host})")
        else:
            st.warning(f"🗄️ Database: local file **{config.DB_PATH.name}**. Set DATABASE_URL to use the shared database.")
        if not rows:
            st.caption("No data synced yet - go to **Data & sync**.")
        for r in rows:
            icon = "✅" if r["status"] == "ok" else "⚠️"
            st.caption(f"{icon} **{r['source']}** · {r['last_run'][:16].replace('T', ' ')} UTC · {r['message'] or ''}")
