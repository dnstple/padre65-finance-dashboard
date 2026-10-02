"""Convert non-GBP amounts to the base currency using daily ECB rates (frankfurter.app, free, no key)."""
import requests

from . import config


def rate(conn, currency, day):
    if currency == config.BASE_CURRENCY:
        return 1.0
    row = conn.execute("SELECT rate FROM fx_rates WHERE date = ? AND currency = ?", (day, currency)).fetchone()
    if row:
        return row[0]
    try:
        resp = requests.get(
            f"https://api.frankfurter.app/{day}",
            params={"from": currency, "to": config.BASE_CURRENCY},
            timeout=20,
        )
        resp.raise_for_status()
        value = float(resp.json()["rates"][config.BASE_CURRENCY])
    except Exception:
        return None
    conn.execute("INSERT OR REPLACE INTO fx_rates (date, currency, rate) VALUES (?, ?, ?)", (day, currency, value))
    conn.commit()
    return value


def fill_base_amounts(conn, txns):
    for t in txns:
        r = rate(conn, t["currency"], t["date"][:10])
        t["amount_base"] = round(t["amount"] * r, 2) if r is not None else None
