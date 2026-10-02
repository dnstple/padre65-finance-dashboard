"""Manual transactions: payments and income that didn't go through Wise."""
import pandas as pd

from . import db

PAYMENT_METHODS = ["Personal card", "Personal bank transfer", "Cash", "Other business card", "PayPal", "Other"]
TEMPLATE_COLUMNS = ["date", "description", "counterparty", "amount", "direction", "category", "payment_method", "notes"]


def add(conn, date, description, counterparty, amount, direction, category, payment_method, notes=""):
    conn.execute(
        """INSERT INTO manual_transactions (date, description, counterparty, amount, direction, category,
           payment_method, notes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (str(date)[:10], description, counterparty, abs(float(amount)), direction, category, payment_method,
         notes, db.now_iso()),
    )
    conn.commit()


def import_csv(conn, path_or_buffer):
    df = pd.read_csv(path_or_buffer)
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
    missing = {"date", "amount"} - set(df.columns)
    if missing:
        raise ValueError(f"CSV is missing columns: {', '.join(sorted(missing))}")
    df["date"] = pd.to_datetime(df["date"], format="mixed", dayfirst=True, errors="coerce")
    df = df[df["date"].notna() & pd.to_numeric(df["amount"], errors="coerce").notna()]
    for r in df.to_dict("records"):
        amount = float(r["amount"])
        direction = (_s(r.get("direction")) or "out").lower()  # no direction given -> treat as a payment out
        add(conn, r["date"].date().isoformat(), _s(r.get("description")), _s(r.get("counterparty")), amount,
            "in" if direction.startswith("in") else "out", _s(r.get("category")) or "Uncategorised",
            _s(r.get("payment_method")) or "Other", _s(r.get("notes")))
    return len(df)


def _s(v):
    return "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v).strip()
