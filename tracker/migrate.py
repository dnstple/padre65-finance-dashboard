"""One-off copy of the local SQLite database into Postgres (Supabase)."""
from . import config, db
from .pg import PRIMARY_KEYS, SERIAL_TABLES


def sqlite_to_postgres(force=False, log=print):
    if not config.DATABASE_URL:
        raise RuntimeError("Set DATABASE_URL in .env first")
    src = db.connect(path=config.DB_PATH)
    dst = db.connect(url=config.DATABASE_URL)

    existing = dst.execute("SELECT COUNT(*) FROM shopify_orders").fetchone()[0]
    if existing and not force:
        raise RuntimeError(f"Postgres already has {existing} orders. Re-run with --force to overwrite it with the local data.")

    tables = [r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type = 'table'") if r[0] in PRIMARY_KEYS]
    for table in tables:
        rows = src.execute(f"SELECT * FROM {table}").fetchall()
        dst.execute(f"DELETE FROM {table}")  # exact mirror of the local data (incl. seeded categories/rules)
        if rows:
            cols = list(rows[0].keys())
            dst.executemany(
                f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                [tuple(r) for r in rows],
            )
        log(f"  {table}: {len(rows)} rows")
    for table in SERIAL_TABLES:
        dst.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                    f"COALESCE((SELECT MAX(id) FROM {table}), 0) + 1, false)")
    log("Done. The local SQLite file is left untouched as a backup.")
