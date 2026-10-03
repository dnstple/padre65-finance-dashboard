"""Postgres connection that behaves like the sqlite3 connection the rest of the code uses.

The app's SQL is written SQLite-style (`?` placeholders, INSERT OR REPLACE / INSERT OR IGNORE,
AUTOINCREMENT, REAL). This adapter translates it so the same code runs on Postgres (e.g. Supabase).
"""
import math
import re
import threading
from datetime import date, datetime

import pandas as pd
import psycopg

# Primary keys, needed to turn INSERT OR REPLACE into ON CONFLICT ... DO UPDATE
PRIMARY_KEYS = {
    "shopify_orders": ["id"], "shopify_order_lines": ["id"], "shopify_refunds": ["id"],
    "shopify_refund_lines": ["id"], "shopify_balance_txns": ["id"], "shopify_payouts": ["id"],
    "shopify_variants": ["variant_id"], "wise_balances": ["id"], "wise_transactions": ["id"],
    "manual_transactions": ["id"], "product_costs": ["key"], "product_log": ["id"],
    "categories": ["category"], "category_rules": ["id"], "category_overrides": ["txn_id"],
    "fx_rates": ["date", "currency"], "sync_log": ["source"], "events": ["id"],
    "event_orders": ["order_id"], "event_costs": ["txn_id"], "stock_writeoffs": ["id"], "planner_overrides": ["variant_id"], "popup_costs": ["id"], "seeded_rules": ["pattern", "category", "direction"],
}
SERIAL_TABLES = {"manual_transactions", "product_log", "category_rules", "events", "stock_writeoffs", "popup_costs"}

_REPLACE = re.compile(r"^\s*INSERT\s+OR\s+REPLACE\s+INTO\s+(\w+)\s*\(([^)]*)\)", re.I | re.S)
_IGNORE = re.compile(r"^\s*INSERT\s+OR\s+IGNORE\s+INTO\s+", re.I)
_INSERT = re.compile(r"^\s*INSERT\s+INTO\s+(\w+)", re.I)


def translate(sql):
    """SQLite-flavoured SQL -> Postgres SQL. Returns (sql, serial_table_or_None)."""
    sql = sql.strip().rstrip(";")
    suffix = ""
    m = _REPLACE.match(sql)
    if m:
        table, cols = m.group(1), [c.strip() for c in m.group(2).split(",")]
        keys = PRIMARY_KEYS[table]
        updates = [f"{c} = EXCLUDED.{c}" for c in cols if c not in keys]
        sql = re.sub(r"^\s*INSERT\s+OR\s+REPLACE\s+INTO", "INSERT INTO", sql, flags=re.I)
        suffix = f" ON CONFLICT ({', '.join(keys)}) " + (f"DO UPDATE SET {', '.join(updates)}" if updates else "DO NOTHING")
    elif _IGNORE.match(sql):
        sql = _IGNORE.sub("INSERT INTO ", sql)
        suffix = " ON CONFLICT DO NOTHING"
    sql = sql.replace("%", "%%").replace("?", "%s") + suffix
    serial = None
    m = _INSERT.match(sql)
    if m and m.group(1).lower() in SERIAL_TABLES and "RETURNING" not in sql.upper():
        sql += " RETURNING id"
        serial = m.group(1)
    return sql, serial


def translate_schema(schema):
    schema = re.sub(r"INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY", schema, flags=re.I)
    return re.sub(r"\bREAL\b", "DOUBLE PRECISION", schema)


def _clean(v):
    """Make values from pandas/numpy safe for psycopg."""
    if v is None:
        return None
    if hasattr(v, "item") and type(v).__module__ == "numpy":
        v = v.item()
    if isinstance(v, float) and math.isnan(v):
        return None
    if v is pd.NaT:
        return None
    if isinstance(v, pd.Timestamp):
        return v.isoformat()
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


def _params(params):
    return tuple(_clean(v) for v in (params or ()))


class Row(dict):
    """Row accessible by column name (row['x']), by position (row[0]) and via dict(row)."""

    def __init__(self, cols, values):
        super().__init__(zip(cols, values))
        self._values = tuple(values)

    def __getitem__(self, k):
        return self._values[k] if isinstance(k, int) else super().__getitem__(k)


def _row_factory(cursor):
    cols = [d.name for d in cursor.description] if cursor.description else []
    return lambda values: Row(cols, values)


class Cursor:
    def __init__(self, cur, lastrowid=None):
        self._cur = cur
        self.lastrowid = lastrowid
        self._buffer = None

    @property
    def description(self):
        return self._cur.description

    def fetchone(self):
        if self._buffer is not None:
            return self._buffer.pop(0) if self._buffer else None
        return self._cur.fetchone() if self._cur.description else None

    def fetchall(self):
        if self._buffer is not None:
            rows, self._buffer = self._buffer, []
            return rows
        return self._cur.fetchall() if self._cur.description else []

    def __iter__(self):
        return iter(self.fetchall())


class PgConnection:
    def __init__(self, url):
        self.url = url
        self._lock = threading.RLock()
        self._conn = None
        self._open()

    def _open(self):
        # prepare_threshold=None keeps it compatible with Supabase's pooler (pgbouncer)
        self._conn = psycopg.connect(self.url, autocommit=True, row_factory=_row_factory, prepare_threshold=None)

    def _ensure(self):
        if self._conn is None or self._conn.closed or self._conn.broken:
            self._open()

    def execute(self, sql, params=()):
        sql, serial = translate(sql)
        with self._lock:
            self._ensure()
            try:
                cur = self._conn.execute(sql, _params(params))
            except psycopg.OperationalError:
                self._open()  # dropped connection - retry once
                cur = self._conn.execute(sql, _params(params))
            if serial:
                row = cur.fetchone()
                return Cursor(cur, lastrowid=row[0] if row else None)
            return Cursor(cur)

    def executemany(self, sql, seq):
        sql, _ = translate(sql)
        sql = sql.replace(" RETURNING id", "")
        rows = [_params(p) for p in seq]
        if not rows:
            return
        with self._lock:
            self._ensure()
            with self._conn.cursor() as cur:
                cur.executemany(sql, rows)

    def executescript(self, script):
        with self._lock:
            self._ensure()
            for stmt in translate_schema(script).split(";"):
                if stmt.strip():
                    self._conn.execute(stmt)

    def read_df(self, sql, params=()):
        cur = self.execute(sql, params)
        cols = [d.name for d in cur.description] if cur.description else []
        rows = [r._values for r in cur.fetchall()]
        return pd.DataFrame(rows, columns=cols)

    def commit(self):
        pass  # autocommit

    def close(self):
        if self._conn is not None:
            self._conn.close()


_shared = {}
_shared_lock = threading.Lock()


def connect_pg(url):
    """One shared connection per process (thread-safe), reconnecting automatically if dropped."""
    with _shared_lock:
        conn = _shared.get(url)
        if conn is None:
            conn = _shared[url] = PgConnection(url)
        return conn
