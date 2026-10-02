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
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        target = str(path)
    if target not in _initialised:  # schema + seeding once per process
        conn.executescript(SCHEMA)
        _migrate(conn)
        _seed(conn)
        _initialised.add(target)
    return conn


def is_postgres(conn):
    return not isinstance(conn, sqlite3.Connection)


def _migrate(conn):
    if is_postgres(conn):
        conn.execute("ALTER TABLE shopify_variants ADD COLUMN IF NOT EXISTS handle TEXT")
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(shopify_variants)")}
    if "handle" not in cols:
        conn.execute("ALTER TABLE shopify_variants ADD COLUMN handle TEXT")


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
