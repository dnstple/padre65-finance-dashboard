import streamlit as st

st.set_page_config(page_title="Padre65 Finance", page_icon="📊", layout="wide")

from common import require_password, sidebar_status  # noqa: E402

require_password()

from views import cash, data, expenses, manual_entry, overview, pnl, products, reports, stock  # noqa: E402

pages = {
    "Finance": [
        st.Page(overview.render, title="Overview", icon="📊", url_path="overview", default=True),
        st.Page(pnl.render, title="P&L", icon="🧾", url_path="pnl"),
        st.Page(products.render, title="Products & contribution", icon="👕", url_path="products"),
        st.Page(expenses.render, title="Expenses", icon="💸", url_path="expenses"),
        st.Page(stock.render, title="Stock", icon="📦", url_path="stock"),
        st.Page(cash.render, title="Cash & Wise", icon="🏦", url_path="cash"),
        st.Page(reports.render, title="Reports", icon="📑", url_path="reports"),
    ],
    "Input": [
        st.Page(manual_entry.render, title="Manual transactions", icon="✍️", url_path="manual"),
        st.Page(data.render, title="Data & sync", icon="🔄", url_path="data"),
    ],
}
nav = st.navigation(pages)
sidebar_status()
nav.run()
