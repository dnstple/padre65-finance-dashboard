import io

import pandas as pd
import streamlit as st

from common import category_notes, ledger, period_filter, report_table

FREQS = {"Monthly": "M", "Quarterly": "Q", "Yearly": "Y"}


def render():
    st.title("Profit & loss")
    start, end = period_filter()
    freq = st.segmented_control("View", list(FREQS), default="Monthly", key="pnl_freq") or "Monthly"
    L = ledger()
    p = L.pnl(start, end, FREQS[freq])
    if p.empty:
        st.info("No data for this period.")
        return

    st.caption("Hover over an underlined line to see what it means.")
    report_table(p, subtotals=p.attrs.get("subtotals", []), definitions=category_notes())

    rev = p.loc["Net revenue"].replace(0, pd.NA)
    margins = pd.DataFrame({
        label: (p.loc[row] / rev) for label, row in [
            ("Gross margin (CM1 %)", "Gross profit (CM1)"),
            ("CM2 %", "Contribution after fulfilment (CM2)"),
            ("CM3 %", "Contribution after marketing (CM3)"),
            ("Net margin %", "Net profit"),
        ]
    }).T
    st.caption("Margins")
    report_table(margins, fmt=lambda v: "–" if pd.isna(v) else f"{v:.1%}")

    with st.expander("How this P&L is built"):
        st.markdown("""
- **Revenue** comes from Shopify orders on the date they were placed. Refunds are recorded on the date they were made.
- **COGS** is units sold × unit cost from the master sheet, or from the restock log once a new cost is logged. Returned items that go back into stock reverse their COGS.
- **Stock purchases** (paying suppliers) are *not* in the P&L because that would double count COGS. You can see them on **Cash & Wise**.
- **Shopify payouts** into Wise are excluded because they're the same money as the Shopify revenue.
- **CM2** takes off payment fees, postage and packaging. **CM3** also takes off marketing. **Net profit** also takes off overheads.
- Costs come from Wise transactions (auto-categorised) plus your manual entries.
""")

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        p.to_excel(xl, sheet_name="P&L")
        margins.to_excel(xl, sheet_name="Margins")
    st.download_button("Download P&L (Excel)", buf.getvalue(), file_name=f"pnl_{freq.lower()}.xlsx")
