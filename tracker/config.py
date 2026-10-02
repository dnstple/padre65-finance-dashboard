import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _get(name, default=""):
    return os.environ.get(name, default).strip()


def _path(value):
    p = Path(value)
    return p if p.is_absolute() else ROOT / p


WISE_API_TOKEN = _get("WISE_API_TOKEN")
WISE_PRIVATE_KEY_PATH = _path(_get("WISE_PRIVATE_KEY_PATH", "keys/wise_private.pem"))
# Alternative to the key file for hosted runs: the PEM text itself (newlines may be written as a literal \n)
WISE_PRIVATE_KEY = os.environ.get("WISE_PRIVATE_KEY", "").replace("\\n", "\n").strip()
WISE_PROFILE_ID = _get("WISE_PROFILE_ID")
WISE_START_DATE = _get("WISE_START_DATE", "2025-01-01")
WISE_API_BASE = _get("WISE_API_BASE", "https://api.wise.com")

SHOPIFY_STORE = _get("SHOPIFY_STORE")
SHOPIFY_ACCESS_TOKEN = _get("SHOPIFY_ACCESS_TOKEN")
SHOPIFY_CLIENT_ID = _get("SHOPIFY_CLIENT_ID")
SHOPIFY_CLIENT_SECRET = _get("SHOPIFY_CLIENT_SECRET")
SHOPIFY_API_VERSION = _get("SHOPIFY_API_VERSION", "2026-07")
SHOPIFY_FEE_PCT = float(_get("SHOPIFY_FEE_PCT", "0.02") or 0)
SHOPIFY_FEE_FIXED = float(_get("SHOPIFY_FEE_FIXED", "0.25") or 0)

# Orders containing only these products (comma-separated titles) are left out of reports
EXCLUDE_PRODUCTS = [p.strip().lower() for p in _get("EXCLUDE_PRODUCTS", "TEST").split(",") if p.strip()]

# Google Sheet (shared "anyone with the link can view") holding product costs; re-imported on each sync
COSTS_SHEET_URL = _get("COSTS_SHEET_URL")

# Postgres connection string (e.g. Supabase). When set it's used instead of the local SQLite file.
DATABASE_URL = _get("DATABASE_URL")
DB_PATH = _path(_get("DB_PATH", "data/finance.db"))
BASE_CURRENCY = _get("BASE_CURRENCY", "GBP")
PACKAGING_COST_PER_ORDER = float(_get("PACKAGING_COST_PER_ORDER", "0") or 0)
TIMEZONE = "Europe/London"
