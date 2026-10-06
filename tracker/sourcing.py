"""Manufacturer proposals: record supplier quotes and compare their true cost.

Landed cost per unit (GBP) = (unit price converted to GBP + freight per unit) x (1 + duty %)
                              x (1 + 20% import VAT when the supplier is overseas and the toggle is on).
Import VAT is a real cost because the business isn't VAT registered, so it can't be reclaimed.
Cost per unit at MOQ spreads the one-off setup and sample costs over the minimum order.
"""
import math
from datetime import date

import pandas as pd

from . import costs as costs_mod
from . import db, events, fx

FIELDS = ["quote_date", "manufacturer", "country", "contact", "product_type", "product", "currency", "unit_cost",
          "moq", "setup_cost", "sample_cost", "freight_per_unit", "duty_pct", "lead_time_weeks", "retail_price",
          "quality", "status", "notes"]
STATUSES = ["Quote received", "Sampling", "Sample approved", "Ordered", "Rejected"]
CURRENCIES = ["GBP", "EUR", "USD", "CNY", "TRY", "INR"]
PRODUCT_TYPES = ["T-Shirt", "Long Sleeve", "Hoodie", "Sweatshirt", "Tracksuit", "Shorts", "Jacket", "Jeans", "Cap",
                 "Foulard", "Other"]
UK_NAMES = {"uk", "united kingdom", "gb", "great britain", "england", "scotland", "wales", "northern ireland"}
IMPORT_VAT = 0.20


def load(conn):
    df = db.read_df(conn, f"SELECT {', '.join(FIELDS)} FROM manufacturer_proposals ORDER BY id")
    if df.empty:
        df = pd.DataFrame(columns=FIELDS)
    df["quote_date"] = pd.to_datetime(df["quote_date"], errors="coerce").dt.date
    for col in ("unit_cost", "moq", "setup_cost", "sample_cost", "freight_per_unit", "duty_pct", "lead_time_weeks",
                "retail_price", "quality"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in ("manufacturer", "country", "contact", "product_type", "product", "currency", "status", "notes"):
        df[col] = df[col].astype("object").where(df[col].notna(), None)
    return df


def save(conn, df):
    """Replace the saved proposals with the edited table (rows without a manufacturer are skipped)."""
    conn.execute("DELETE FROM manufacturer_proposals")
    for r in df.to_dict("records"):
        if not str(r.get("manufacturer") or "").strip():
            continue
        values = []
        for f in FIELDS:
            v = r.get(f)
            if v is not None and not isinstance(v, str) and pd.isna(v):
                v = None
            if f == "quote_date" and v is not None:
                v = str(v)[:10]
            values.append(v)
        conn.execute(f"INSERT INTO manufacturer_proposals ({', '.join(FIELDS)}, updated_at) "
                     f"VALUES ({', '.join('?' for _ in FIELDS)}, ?)", (*values, db.now_iso()))
    conn.commit()


def benchmarks(conn):
    """What you pay and charge today per product type: average landed unit cost and price of current products."""
    variants = db.read_df(conn, "SELECT product_id, product_title, price FROM shopify_variants")
    if variants.empty:
        return pd.DataFrame(columns=["current_cost", "current_price", "current_products"])
    v = costs_mod.unit_costs_for_lines(conn, variants.assign(variant_id=None, date=pd.Timestamp.now().normalize()))
    per_product = v.groupby(["product_id", "product_title"]).agg(cost=("unit_cost", "max"), price=("price", "max")).reset_index()
    per_product = per_product[per_product["cost"].notna()]
    per_product["product_type"] = per_product["product_title"].map(events.category_of)
    return per_product.groupby("product_type").agg(current_cost=("cost", "mean"), current_price=("price", "mean"),
                                                   current_products=("product_id", "count"))


def evaluate(conn, df, include_import_vat=True):
    """Add GBP and comparison columns to the proposals table."""
    out = df.copy()
    if out.empty:
        return out
    today = date.today().isoformat()
    rates = {cur: fx.rate(conn, cur, today) for cur in out["currency"].fillna("GBP").unique()}
    out["fx_rate"] = out["currency"].fillna("GBP").map(rates)
    num = lambda col: pd.to_numeric(out[col], errors="coerce")  # noqa: E731
    out["unit_cost_gbp"] = num("unit_cost") * out["fx_rate"]
    out["setup_gbp"] = num("setup_cost").fillna(0) * out["fx_rate"]
    out["sample_gbp"] = num("sample_cost").fillna(0) * out["fx_rate"]
    country = out["country"].fillna("").astype(str).str.strip().str.lower()
    out["imported"] = (country != "") & ~country.isin(UK_NAMES)
    vat = 1 + IMPORT_VAT * (out["imported"] & include_import_vat)
    out["landed_cost"] = (out["unit_cost_gbp"] + num("freight_per_unit").fillna(0)) * (1 + num("duty_pct").fillna(0) / 100) * vat
    moq = num("moq")
    out["upfront_cost"] = moq * out["landed_cost"] + out["setup_gbp"] + out["sample_gbp"]
    out["cost_at_moq"] = out["upfront_cost"] / moq
    price = num("retail_price")
    out["margin"] = ((price - out["cost_at_moq"]) / price).where(price > 0)
    out["markup"] = (price / out["cost_at_moq"]).where(out["cost_at_moq"] > 0)
    out["payback_units"] = [math.ceil(u / p) if p and p > 0 and pd.notna(u) else None
                            for u, p in zip(out["upfront_cost"], price)]
    out["payback_share"] = (pd.to_numeric(out["payback_units"], errors="coerce") / moq).where(moq > 0)

    bench = benchmarks(conn)
    out = out.join(bench, on="product_type")
    out["vs_current"] = (out["cost_at_moq"] / out["current_cost"] - 1).where(out["current_cost"] > 0)
    return out


def best_by_type(evaluated):
    """For each product type: cheapest per unit, best margin and smallest upfront spend (ignoring rejected quotes)."""
    live = evaluated[(evaluated["status"] != "Rejected") & evaluated["cost_at_moq"].notna()]
    rows = []
    for ptype, g in live.groupby(live["product_type"].fillna("Other")):
        def pick(col, largest=False):
            g2 = g[g[col].notna()]
            if g2.empty:
                return None
            return g2.loc[g2[col].idxmax() if largest else g2[col].idxmin()]
        cheapest, margin, upfront = pick("cost_at_moq"), pick("margin", largest=True), pick("upfront_cost")
        rows.append({
            "product_type": ptype, "quotes": len(g),
            "cheapest": f"{cheapest['manufacturer']} ({cheapest['cost_at_moq']:.2f})" if cheapest is not None else "–",
            "best_margin": f"{margin['manufacturer']} ({margin['margin']:.0%})" if margin is not None else "–",
            "lowest_upfront": f"{upfront['manufacturer']} ({upfront['upfront_cost']:,.0f})" if upfront is not None else "–",
        })
    return pd.DataFrame(rows)
