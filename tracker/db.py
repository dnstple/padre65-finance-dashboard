import sqlite3
from datetime import datetime, timezone

import pandas as pd

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS shopify_orders (
    id TEXT PRIMARY KEY, name TEXT, created_at TEXT, processed_at TEXT, cancelled_at TEXT,
    financial_status TEXT, test INTEGER, source_name TEXT, gateways TEXT, currency TEXT,
    subtotal REAL, discounts REAL, shipping REAL, tax REAL, total REAL, refunded REAL
);
CREATE TABLE IF NOT EXISTS shopify_order_lines (
    id TEXT PRIMARY KEY, order_id TEXT, product_id TEXT, variant_id TEXT, sku TEXT,
    product_title TEXT, variant_title TEXT, quantity INTEGER, unit_price REAL,
    gross REAL, discount REAL, tax REAL
);
CREATE TABLE IF NOT EXISTS shopify_refunds (
    id TEXT PRIMARY KEY, order_id TEXT, created_at TEXT, total REAL, shipping REAL
);
CREATE TABLE IF NOT EXISTS shopify_refund_lines (
    id TEXT PRIMARY KEY, refund_id TEXT, order_id TEXT, created_at TEXT, line_item_id TEXT,
    quantity INTEGER, subtotal REAL, tax REAL, restock_type TEXT
);
CREATE TABLE IF NOT EXISTS shopify_balance_txns (
    id TEXT PRIMARY KEY, type TEXT, transaction_date TEXT, amount REAL, fee REAL, net REAL,
    currency TEXT, source_type TEXT, order_id TEXT, payout_id TEXT, payout_status TEXT
);
CREATE TABLE IF NOT EXISTS shopify_payouts (
    id TEXT PRIMARY KEY, issued_at TEXT, status TEXT, net REAL, currency TEXT
);
CREATE TABLE IF NOT EXISTS shopify_variants (
    variant_id TEXT PRIMARY KEY, product_id TEXT, product_title TEXT, variant_title TEXT,
    sku TEXT, price REAL, inventory_quantity INTEGER, product_status TEXT, collections TEXT, handle TEXT
);
CREATE TABLE IF NOT EXISTS wise_balances (
    id TEXT PRIMARY KEY, currency TEXT, type TEXT, name TEXT, amount REAL, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS wise_transactions (
    id TEXT PRIMARY KEY, balance_id TEXT, currency TEXT, date TEXT, amount REAL, fee REAL,
    direction TEXT, detail_type TEXT, description TEXT, merchant TEXT, counterparty TEXT,
    reference TEXT, running_balance REAL, amount_base REAL, source TEXT
);
CREATE TABLE IF NOT EXISTS manual_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, description TEXT,
    counterparty TEXT, amount REAL NOT NULL, direction TEXT NOT NULL DEFAULT 'out',
    category TEXT, payment_method TEXT, notes TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS product_costs (
    key TEXT PRIMARY KEY, product_id TEXT, variant_id TEXT, product_title TEXT,
    variant_title TEXT, sku TEXT, collection TEXT, unit_cost REAL, notes TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS product_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, product_id TEXT,
    product_title TEXT, variant_id TEXT, event TEXT NOT NULL, quantity INTEGER,
    unit_cost REAL, total_cost REAL, supplier TEXT, notes TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS categories (
    category TEXT PRIMARY KEY, type TEXT NOT NULL, notes TEXT
);
CREATE TABLE IF NOT EXISTS category_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT, priority INTEGER, pattern TEXT, category TEXT,
    direction TEXT DEFAULT 'any', field TEXT DEFAULT 'text'
);
CREATE TABLE IF NOT EXISTS category_overrides (
    txn_id TEXT PRIMARY KEY, category TEXT
);
CREATE TABLE IF NOT EXISTS fx_rates (
    date TEXT, currency TEXT, rate REAL, PRIMARY KEY (date, currency)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, start_date TEXT, end_date TEXT,
    location TEXT, notes TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS event_orders (
    order_id TEXT PRIMARY KEY, event_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS event_costs (
    txn_id TEXT PRIMARY KEY, event_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS stock_writeoffs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, amount REAL NOT NULL, quantity INTEGER,
    product_id TEXT, product_title TEXT, reason TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS planner_overrides (
    variant_id TEXT PRIMARY KEY, include INTEGER NOT NULL DEFAULT 1, buy_override INTEGER, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS popup_costs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, item TEXT NOT NULL, category TEXT, amount REAL, notes TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS manufacturer_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, quote_date TEXT, manufacturer TEXT NOT NULL, country TEXT, contact TEXT,
    product_type TEXT, product TEXT, currency TEXT DEFAULT 'GBP', unit_cost REAL, moq INTEGER, setup_cost REAL,
    sample_cost REAL, freight_per_unit REAL, duty_pct REAL, lead_time_weeks REAL, retail_price REAL,
    quality INTEGER, status TEXT, notes TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS product_ideas (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, product_type TEXT, price REAL, unit_cost REAL,
    cost_source TEXT, first_order_units INTEGER, status TEXT, notes TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS plan_collections (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, season TEXT, start_date TEXT, end_date TEXT,
    planned_events INTEGER, notes TEXT, settings TEXT, overrides TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS plan_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT, collection_id INTEGER NOT NULL, name TEXT NOT NULL, product_type TEXT,
    colour TEXT, sizes TEXT, unit_cost REAL, price REAL, comparable TEXT, manual_units REAL, emphasis REAL,
    moq INTEGER, status TEXT, notes TEXT, created_at TEXT, updated_at TEXT, supplier TEXT, currency TEXT, tiers TEXT,
    sample_cost REAL, custom_cost REAL, lead_weeks REAL, draft_units REAL
);
CREATE TABLE IF NOT EXISTS plan_scenarios (
    id INTEGER PRIMARY KEY AUTOINCREMENT, collection_id INTEGER NOT NULL, name TEXT NOT NULL, settings TEXT,
    overrides TEXT, notes TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS stock_plans (
    id TEXT PRIMARY KEY, plan TEXT, version INTEGER, updated_at TEXT, updated_by TEXT
);
CREATE TABLE IF NOT EXISTS stock_plan_images (
    id TEXT PRIMARY KEY, data TEXT
);
CREATE TABLE IF NOT EXISTS sync_log (
    source TEXT PRIMARY KEY, last_run TEXT, status TEXT, message TEXT
);
CREATE TABLE IF NOT EXISTS seeded_rules (
    pattern TEXT, category TEXT, direction TEXT, PRIMARY KEY (pattern, category, direction)
);
"""


_initialised = set()


def connect(path=None, url=None):
    """Open the database: Postgres when DATABASE_URL is set, otherwise the local SQLite file."""
    url = url if url is not None else (None if path else config.DATABASE_URL)
    if url:
        from .pg import connect_pg
        conn = connect_pg(url)
        target = url
    else:
        path = path or config.DB_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False)  # the dashboard caches reports across threads
        conn.row_factory = sqlite3.Row
        target = str(path)
    if target not in _initialised:  # schema + seeding once per process
        conn.executescript(SCHEMA)
        _migrate(conn)
        _seed(conn)
        _initialised.add(target)
    return conn


def is_postgres(conn):
    return not isinstance(getattr(conn, "inner", conn), sqlite3.Connection)


# Columns added after a table was first created: (table, column, type)
ADDED_COLUMNS = [
    ("shopify_variants", "handle", "TEXT"),
    ("plan_items", "supplier", "TEXT"), ("plan_items", "currency", "TEXT"), ("plan_items", "tiers", "TEXT"),
    ("plan_items", "sample_cost", "REAL"), ("plan_items", "custom_cost", "REAL"), ("plan_items", "lead_weeks", "REAL"),
    ("plan_items", "draft_units", "REAL"),
]


def _migrate(conn):
    for table, column, kind in ADDED_COLUMNS:
        if is_postgres(conn):
            kind = "DOUBLE PRECISION" if kind == "REAL" else kind
            conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {kind}")
        elif column not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")


def _seed(conn):
    # New categories in categories.csv are added to existing databases too
    cats = pd.read_csv(config.ROOT / "config" / "categories.csv")
    conn.executemany(
        "INSERT OR IGNORE INTO categories (category, type, notes) VALUES (?, ?, ?)",
        cats[["category", "type", "notes"]].fillna("").itertuples(index=False),
    )
    # Add rules from category_rules.csv that this database hasn't seen before.
    # Seeded rules are remembered, so a rule you delete in the dashboard isn't re-added.
    if conn.execute("SELECT COUNT(*) FROM seeded_rules").fetchone()[0] == 0:
        conn.execute("INSERT OR IGNORE INTO seeded_rules SELECT pattern, category, direction FROM category_rules")
    rules = pd.read_csv(config.ROOT / "config" / "category_rules.csv")
    for r in rules.itertuples(index=False):
        if conn.execute("SELECT 1 FROM seeded_rules WHERE pattern = ? AND category = ? AND direction = ?",
                        (r.pattern, r.category, r.direction)).fetchone():
            continue
        conn.execute("INSERT INTO category_rules (priority, pattern, category, direction, field) VALUES (?, ?, ?, ?, ?)",
                     (int(r.priority), r.pattern, r.category, r.direction, r.field))
        conn.execute("INSERT INTO seeded_rules VALUES (?, ?, ?)", (r.pattern, r.category, r.direction))
    conn.commit()


def upsert(conn, table, rows, key="id"):
    """Insert or replace a list of dicts into a table."""
    if not rows:
        return 0
    cols = list(rows[0].keys())
    sql = f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
    conn.executemany(sql, [tuple(r.get(c) for c in cols) for r in rows])
    conn.commit()
    return len(rows)


def read_df(conn, sql, params=()):
    conn = getattr(conn, "inner", conn)  # the dashboard's write-tracking wrapper
    if is_postgres(conn):
        return conn.read_df(sql, params)
    return pd.read_sql_query(sql, conn, params=params)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log_sync(conn, source, status, message=""):
    conn.execute(
        "INSERT OR REPLACE INTO sync_log (source, last_run, status, message) VALUES (?, ?, ?, ?)",
        (source, now_iso(), status, message),
    )
    conn.commit()
