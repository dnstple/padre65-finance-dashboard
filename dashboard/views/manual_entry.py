from datetime import date

import pandas as pd
import streamlit as st

from common import conn, editor, gbp, heading
from tracker import categorise, config, db, manual


def render():
    heading("Manual transactions", "title")
    st.caption("Log anything that didn't go through Wise, such as personal cards, cash or another account. "
               "These feed straight into the P&L and expense reports.")
    c = conn()
    cats = categorise.categories(c)["category"].tolist()

    with st.form("manual", clear_on_submit=True):
        a, b, d = st.columns([1, 2, 2])
        when = a.date_input("Date", date.today())
        description = b.text_input("Description")
        counterparty = d.text_input("Paid to / received from")
        e, f, g, h = st.columns([1, 1, 2, 2])
        amount = e.number_input("Amount £", min_value=0.0, step=1.0, format="%.2f")
        direction = f.selectbox("Direction", ["out", "in"], format_func={"out": "Money out", "in": "Money in"}.get)
        category = g.selectbox("Category", cats, index=cats.index("Other Expense") if "Other Expense" in cats else 0)
        method = h.selectbox("Paid with", manual.PAYMENT_METHODS)
        notes = st.text_input("Notes (optional)")
        if st.form_submit_button("Add transaction", type="primary"):
            if amount <= 0:
                st.error("Enter an amount above zero.")
            else:
                manual.add(c, when.isoformat(), description, counterparty, amount, direction, category, method, notes)
                st.success(f"Added {gbp(amount, 2)} · {category}")

    df = db.read_df(c, "SELECT id, date, description, counterparty, amount, direction, category, payment_method, notes FROM manual_transactions ORDER BY date DESC, id DESC")
    st.subheader(f"All manual transactions ({len(df)})")
    if not df.empty:
        total_out = df.loc[df["direction"] == "out", "amount"].sum()
        total_in = df.loc[df["direction"] == "in", "amount"].sum()
        st.caption(f"Out: {gbp(total_out, 2)} · In: {gbp(total_in, 2)}. Edit cells or select rows and delete, then save.")
        df["date"] = pd.to_datetime(df["date"]).dt.date
        edited = editor(df, hide_index=True, width="stretch", num_rows="dynamic", key="manual_editor",
                                column_config={
                                    "id": None,
                                    "date": st.column_config.DateColumn("Date", format="D MMM YYYY", required=True),
                                    "description": "Description", "counterparty": "Payee / payer",
                                    "amount": st.column_config.NumberColumn("Amount", format="£%.2f", min_value=0, required=True),
                                    "direction": st.column_config.SelectboxColumn("Direction", options=["out", "in"], required=True),
                                    "category": st.column_config.SelectboxColumn("Category", options=cats, required=True),
                                    "payment_method": st.column_config.SelectboxColumn("Paid with", options=manual.PAYMENT_METHODS),
                                    "notes": "Notes",
                                })
        if st.button("Save changes"):
            kept = edited.dropna(subset=["id"])
            for rid in set(df["id"]) - set(kept["id"]):
                c.execute("DELETE FROM manual_transactions WHERE id = ?", (int(rid),))
            for r in kept.to_dict("records"):
                c.execute("""UPDATE manual_transactions SET date=?, description=?, counterparty=?, amount=?, direction=?,
                             category=?, payment_method=?, notes=? WHERE id=?""",
                          (str(r["date"])[:10], r["description"], r["counterparty"], abs(float(r["amount"])),
                           r["direction"], r["category"], r["payment_method"], r["notes"], int(r["id"])))
            for r in edited[edited["id"].isna()].to_dict("records"):
                if r.get("date") and r.get("amount"):
                    manual.add(c, str(r["date"])[:10], r.get("description") or "", r.get("counterparty") or "",
                               r["amount"], r.get("direction") or "out", r.get("category") or "Uncategorised",
                               r.get("payment_method") or "Other", r.get("notes") or "")
            c.commit()
            st.success("Saved")
            st.rerun()

    heading("Bulk import from CSV", "subheader")
    template = (config.ROOT / "templates" / "manual_transactions_template.csv").read_bytes()
    st.download_button("Download CSV template", template, file_name="manual_transactions_template.csv")
    st.caption("Columns: date, description, counterparty, amount, direction (out/in), category, payment_method, notes. "
               "Categories must match the names in the dropdown above.")
    up = st.file_uploader("Upload CSV", type=["csv"], key="manual_upload")
    if up is not None and st.button("Import file"):
        try:
            n = manual.import_csv(c, up)
            st.success(f"Imported {n} transactions")
        except ValueError as e:
            st.error(str(e))
