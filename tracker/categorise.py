"""Assign a category to every Wise and manual transaction using ordered rules plus manual overrides."""
import re

import pandas as pd

from . import db


def load_rules(conn):
    rules = db.read_df(conn, "SELECT * FROM category_rules ORDER BY priority, id")
    compiled = []
    for r in rules.itertuples():
        try:
            compiled.append((re.compile(r.pattern, re.IGNORECASE), r.category, r.direction or "any", r.field or "text"))
        except re.error:
            continue
    return compiled


def is_refund(row):
    return row.get("direction") == "in" and str(row.get("description") or "").startswith("Refunded")


def categorise_row(row, rules):
    text = " ".join(str(row.get(c) or "") for c in ("merchant", "counterparty", "description", "reference"))
    # A card refund is matched like the purchase it reverses, so it reduces that expense category
    row_direction = "out" if is_refund(row) else row["direction"]
    for pattern, category, direction, field in rules:
        if direction != "any" and direction != row_direction:
            continue
        target = str(row.get("detail_type") or "") if field == "detail_type" else text
        if pattern.search(target):
            return category
    return "Uncategorised Income" if row_direction == "in" else "Uncategorised"


def wise_transactions(conn):
    """All Wise transactions with category, category type and GBP amount."""
    df = db.read_df(conn, """
        SELECT w.*, o.category AS override
        FROM wise_transactions w LEFT JOIN category_overrides o ON o.txn_id = w.id
    """)
    if df.empty:
        return df.assign(category=[], type=[])
    rules = load_rules(conn)
    df["category"] = [
        ov if isinstance(ov, str) and ov else categorise_row(r, rules)
        for ov, r in zip(df["override"], df.to_dict("records"))
    ]
    # Refunds without their own override follow the latest manually-categorised purchase from the same payee
    overridden = df[df["override"].notna() & (df["direction"] == "out")].sort_values("date")
    last_override = overridden.groupby("counterparty")["override"].last()
    for i, r in df[df["override"].isna()].iterrows():
        if is_refund(r) and r["counterparty"] in last_override:
            df.at[i, "category"] = last_override[r["counterparty"]]
    df["amount_base"] = df["amount_base"].fillna(df["amount"])
    return df.merge(categories(conn)[["category", "type"]], on="category", how="left").fillna({"type": "opex"})


def manual_transactions(conn):
    df = db.read_df(conn, "SELECT * FROM manual_transactions")
    if df.empty:
        return df.assign(type=[], amount_base=[])
    df["category"] = df["category"].fillna("Uncategorised").replace("", "Uncategorised")
    df["amount_base"] = df["amount"].abs().where(df["direction"] == "in", -df["amount"].abs())
    return df.merge(categories(conn)[["category", "type"]], on="category", how="left").fillna({"type": "opex"})


def categories(conn):
    return db.read_df(conn, "SELECT * FROM categories ORDER BY type, category")


def all_cash_transactions(conn):
    """Wise + manual transactions in one frame: date, amount_base (signed), category, type, source."""
    cols = ["id", "date", "description", "counterparty", "amount_base", "category", "type", "source", "direction"]
    w = wise_transactions(conn)
    m = manual_transactions(conn)
    frames = []
    if not w.empty:
        w = w.assign(source="Wise", counterparty=w["counterparty"].where(w["counterparty"] != "", w["description"]))
        frames.append(w[cols])
    if not m.empty:
        m = m.assign(id="manual:" + m["id"].astype(str), source="Manual: " + m["payment_method"].fillna("Other"))
        frames.append(m[cols])
    if not frames:
        return pd.DataFrame(columns=cols)
    out = pd.concat(frames, ignore_index=True)
    out["date"] = pd.to_datetime(out["date"], utc=True, format="mixed").dt.tz_convert("Europe/London").dt.tz_localize(None)
    return out
