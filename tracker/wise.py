"""Pull balances and statements from Wise Business, with SCA signing for UK/EEA accounts.

Also imports Wise CSV statement exports, for backfilling history or if the API isn't set up.
"""
import base64
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import requests

from . import config, db, fx

MAX_INTERVAL_DAYS = 365  # Wise allows up to 469 days per statement request


class WiseError(Exception):
    pass


class WiseClient:
    def __init__(self):
        if not config.WISE_API_TOKEN:
            raise WiseError("WISE_API_TOKEN is not set in .env")
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {config.WISE_API_TOKEN}"
        self._key = None

    def _sign(self, one_time_token):
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        if self._key is None and config.WISE_PRIVATE_KEY:
            self._key = serialization.load_pem_private_key(config.WISE_PRIVATE_KEY.encode(), password=None)
        if self._key is None:
            if not config.WISE_PRIVATE_KEY_PATH.exists():
                raise WiseError(
                    f"Wise needs a signed SCA challenge but no private key was found at {config.WISE_PRIVATE_KEY_PATH}. "
                    "Run `python sync.py wise-keys` and upload the public key to Wise (see README)."
                )
            self._key = serialization.load_pem_private_key(config.WISE_PRIVATE_KEY_PATH.read_bytes(), password=None)
        sig = self._key.sign(one_time_token.encode(), padding.PKCS1v15(), hashes.SHA256())
        return base64.b64encode(sig).decode()

    def get(self, path, params=None):
        url = f"{config.WISE_API_BASE}{path}"
        resp = self.session.get(url, params=params, timeout=60)
        if resp.status_code == 403 and resp.headers.get("x-2fa-approval"):
            ott = resp.headers["x-2fa-approval"]
            resp = self.session.get(
                url, params=params, timeout=60,
                headers={"x-2fa-approval": ott, "X-Signature": self._sign(ott)},
            )
        if resp.status_code == 403 and resp.headers.get("x-2fa-approval-result") == "REJECTED":
            raise StatementsBlocked(f"Wise rejected the SCA signature on {path}")
        if resp.status_code == 401:
            raise WiseError("Wise rejected the API token (401). Check WISE_API_TOKEN.")
        if not resp.ok:
            raise WiseError(f"Wise API error {resp.status_code} on {path}: {resp.text[:300]}")
        return resp.json()

    def business_profile_id(self):
        if config.WISE_PROFILE_ID:
            return config.WISE_PROFILE_ID
        profiles = self.get("/v2/profiles")
        business = [p for p in profiles if str(p.get("type", "")).upper() == "BUSINESS"]
        if not business:
            raise WiseError("No Wise business profile found for this token")
        return str(business[0]["id"])

    def balances(self, profile_id):
        return self.get(f"/v4/profiles/{profile_id}/balances", {"types": "STANDARD,SAVINGS"})

    def statement(self, profile_id, balance_id, currency, start, end):
        return self.get(
            f"/v1/profiles/{profile_id}/balance-statements/{balance_id}/statement.json",
            {
                "currency": currency,
                "intervalStart": start.strftime("%Y-%m-%dT00:00:00.000Z"),
                "intervalEnd": end.strftime("%Y-%m-%dT23:59:59.999Z"),
                "type": "COMPACT",
            },
        )


def _counterparty(details):
    merchant = details.get("merchant") or {}
    recipient = details.get("recipient") or {}
    return (
        merchant.get("name")
        or details.get("senderName")
        or recipient.get("name")
        or ""
    )


def parse_statement_txn(t, balance_id, currency):
    details = t.get("details") or {}
    amount = float(t["amount"]["value"])
    ref = t.get("referenceNumber") or f"{t['date']}:{amount}"
    return {
        "id": f"{currency}:{ref}:{t.get('type')}",
        "balance_id": str(balance_id),
        "currency": currency,
        "date": t["date"],
        "amount": amount,
        "fee": float((t.get("totalFees") or {}).get("value") or 0),
        "direction": "in" if amount > 0 else "out",
        "detail_type": details.get("type"),
        "description": details.get("description") or "",
        "merchant": (details.get("merchant") or {}).get("name") or "",
        "counterparty": _counterparty(details),
        "reference": details.get("paymentReference") or ref,
        "running_balance": float((t.get("runningBalance") or {}).get("value") or 0),
        "source": "api",
    }


class StatementsBlocked(WiseError):
    pass


def sync(conn, log=print):
    client = WiseClient()
    profile_id = client.business_profile_id()
    balances = client.balances(profile_id)

    rows = [{
        "id": str(b["id"]),
        "currency": b["currency"],
        "type": b.get("type"),
        "name": b.get("name") or b["currency"],
        "amount": float(b["amount"]["value"]),
        "updated_at": db.now_iso(),
    } for b in balances]
    db.upsert(conn, "wise_balances", rows)
    log(f"Wise: {len(rows)} balances ({', '.join(r['currency'] for r in rows)})")

    try:
        _sync_statements(conn, client, profile_id, rows, log)
    except StatementsBlocked:
        log("Wise: statements need SCA approval that isn't working yet - using the Activities feed instead")
        _sync_activities(conn, client, profile_id, log)


def _sync_statements(conn, client, profile_id, rows, log):
    start_default = date.fromisoformat(config.WISE_START_DATE)
    today = datetime.now(timezone.utc).date()
    total = 0
    for b in rows:
        last = conn.execute(
            "SELECT MAX(date) FROM wise_transactions WHERE balance_id = ? AND source = 'api'", (b["id"],)
        ).fetchone()[0]
        start = (date.fromisoformat(last[:10]) - timedelta(days=7)) if last else start_default
        while start <= today:
            end = min(start + timedelta(days=MAX_INTERVAL_DAYS - 1), today)
            stmt = client.statement(profile_id, b["id"], b["currency"], start, end)
            txns = [parse_statement_txn(t, b["id"], b["currency"]) for t in stmt.get("transactions", [])]
            fx.fill_base_amounts(conn, txns)
            _drop_csv_duplicates(conn, txns)
            db.upsert(conn, "wise_transactions", txns)
            total += len(txns)
            start = end + timedelta(days=1)
    # Statements are the better source: drop any rows that came from the Activities fallback
    conn.execute("DELETE FROM wise_transactions WHERE source = 'activities'")
    conn.commit()
    log(f"Wise: {total} statement transactions synced")
    db.log_sync(conn, "wise", "ok", f"{total} transactions (statements)")


SKIP_ACTIVITY_TYPES = {"CARD_CHECK"}
SKIP_DESCRIPTIONS = {"Cancelled"}  # "Refunded" rows are refund credits - keep them


def _parse_amount(text):
    """'<positive>+ 1,400 GBP</positive>' -> (1400.0, 'GBP'); '3.83 GBP' -> (-3.83, 'GBP')."""
    import re
    if not text:
        return None, None
    positive = "<positive>" in text
    clean = re.sub(r"<[^>]+>", "", text).replace("+", "").replace(",", "").strip()
    m = re.match(r"^(-?[\d.]+)\s+([A-Z]{3})$", clean)
    if not m:
        return None, None
    value = float(m.group(1))
    return (value if positive else -value), m.group(2)


def parse_activity(a):
    import re
    if a.get("status") != "COMPLETED" or a.get("type") in SKIP_ACTIVITY_TYPES or a.get("description") in SKIP_DESCRIPTIONS:
        return None
    amount, currency = _parse_amount(a.get("primaryAmount"))
    if not amount:
        return None
    title = re.sub(r"<[^>]+>", "", a.get("title") or "").strip()
    resource = a.get("resource") or {}
    row = {
        "id": f"act:{resource.get('type')}:{resource.get('id')}",
        "balance_id": None,
        "currency": currency,
        "date": a["createdOn"],
        "amount": amount,
        "fee": 0.0,
        "direction": "in" if amount > 0 else "out",
        "detail_type": a.get("type"),
        "description": " ".join(x for x in (a.get("description"), title) if x),
        "merchant": title if a.get("type") == "CARD_PAYMENT" else "",
        "counterparty": title,
        "reference": str(resource.get("id")),
        "running_balance": None,
        "source": "activities",
    }
    # Foreign-currency transfers show the GBP equivalent as the secondary amount
    sec_amount, sec_currency = _parse_amount(a.get("secondaryAmount"))
    if sec_amount and sec_currency == config.BASE_CURRENCY and currency != config.BASE_CURRENCY:
        row["amount_base"] = abs(sec_amount) * (1 if amount > 0 else -1)
    return row


def _sync_activities(conn, client, profile_id, log):
    rows, cursor = [], None
    while True:
        params = {"size": 100}
        if cursor:
            params["nextCursor"] = cursor
        page = client.get(f"/v1/profiles/{profile_id}/activities", params)
        acts = page.get("activities") or []
        rows += [r for r in (parse_activity(a) for a in acts) if r]
        cursor = page.get("cursor")
        if not cursor or not acts:
            break
    need_fx = [r for r in rows if "amount_base" not in r]
    fx.fill_base_amounts(conn, need_fx)
    db.upsert(conn, "wise_transactions", rows)
    log(f"Wise: {len(rows)} transactions synced from Activities (since {min(r['date'] for r in rows)[:10]})" if rows else "Wise: no activities found")
    db.log_sync(conn, "wise", "ok", f"{len(rows)} transactions (activities)")


def _drop_csv_duplicates(conn, txns):
    """If history was first loaded from CSV, remove those rows once the API returns the same transaction."""
    for t in txns:
        ref = t["id"].split(":", 1)[1].rsplit(":", 1)[0]
        conn.execute(
            "DELETE FROM wise_transactions WHERE source = 'csv' AND currency = ? AND reference = ?",
            (t["currency"], ref),
        )


# --- CSV import -------------------------------------------------------------

CSV_COLUMNS = {
    "id": ["TransferWise ID", "ID", "Wise ID"],
    "date": ["Date", "Created on", "Date Time"],
    "amount": ["Amount"],
    "currency": ["Currency"],
    "description": ["Description"],
    "reference": ["Payment Reference", "Reference"],
    "running_balance": ["Running Balance"],
    "payer": ["Payer Name"],
    "payee": ["Payee Name"],
    "merchant": ["Merchant"],
    "fee": ["Total fees", "Total Fees"],
    "exchange_to": ["Exchange To"],
}


def _pick(df, names):
    for n in names:
        if n in df.columns:
            return df[n]
    return pd.Series([None] * len(df), index=df.index)


def import_csv(conn, path, log=print):
    """Import a Wise balance statement CSV export (Statements > download as CSV)."""
    df = pd.read_csv(path)
    ids = _pick(df, CSV_COLUMNS["id"]).astype(str)
    amounts = pd.to_numeric(_pick(df, CSV_COLUMNS["amount"]), errors="coerce").fillna(0)
    dates = pd.to_datetime(_pick(df, CSV_COLUMNS["date"]), dayfirst=True, errors="coerce")
    descriptions = _pick(df, CSV_COLUMNS["description"]).fillna("").astype(str)
    merchants = _pick(df, CSV_COLUMNS["merchant"]).fillna("").astype(str)
    payer = _pick(df, CSV_COLUMNS["payer"]).fillna("").astype(str)
    payee = _pick(df, CSV_COLUMNS["payee"]).fillna("").astype(str)
    currencies = _pick(df, CSV_COLUMNS["currency"]).fillna(config.BASE_CURRENCY).astype(str)
    exchange_to = _pick(df, CSV_COLUMNS["exchange_to"]).fillna("").astype(str)

    txns = []
    for i in df.index:
        if pd.isna(dates[i]):
            continue
        amount = float(amounts[i])
        ref = ids[i]
        currency = currencies[i]
        if ref.upper().startswith("BALANCE") or exchange_to[i]:
            detail_type = "CONVERSION"
        elif ref.upper().startswith("CARD"):
            detail_type = "CARD"
        elif amount > 0:
            detail_type = "DEPOSIT"
        else:
            detail_type = "TRANSFER"
        existing_api = conn.execute(
            "SELECT 1 FROM wise_transactions WHERE source = 'api' AND currency = ? AND id LIKE ?",
            (currency, f"{currency}:{ref}:%"),
        ).fetchone()
        if existing_api:
            continue
        txns.append({
            "id": f"csv:{currency}:{ref}:{'CREDIT' if amount > 0 else 'DEBIT'}",
            "balance_id": None,
            "currency": currency,
            "date": dates[i].strftime("%Y-%m-%dT%H:%M:%SZ"),
            "amount": amount,
            "fee": float(pd.to_numeric(_pick(df, CSV_COLUMNS["fee"])[i], errors="coerce") or 0),
            "direction": "in" if amount > 0 else "out",
            "detail_type": detail_type,
            "description": descriptions[i],
            "merchant": merchants[i],
            "counterparty": merchants[i] or (payer[i] if amount > 0 else payee[i]),
            "reference": ref,
            "running_balance": float(pd.to_numeric(_pick(df, CSV_COLUMNS["running_balance"])[i], errors="coerce") or 0),
            "source": "csv",
        })
    fx.fill_base_amounts(conn, txns)
    db.upsert(conn, "wise_transactions", txns)
    log(f"Wise CSV: imported {len(txns)} transactions from {path}")
    db.log_sync(conn, "wise_csv", "ok", f"{len(txns)} transactions from {path}")


def generate_keys(log=print):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    priv_path = config.WISE_PRIVATE_KEY_PATH
    pub_path = priv_path.with_name("wise_public.pem")
    if priv_path.exists():
        log(f"Key already exists at {priv_path}; not overwriting.")
        return
    priv_path.parent.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
    ))
    pub_path.write_bytes(key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ))
    log(f"Created {priv_path} (keep private) and {pub_path} (upload this one to Wise).")
