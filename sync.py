"""Command line for syncing and importing data.

  python sync.py                    sync Shopify + Wise
  python sync.py shopify [--full]   sync Shopify only (--full re-pulls all history)
  python sync.py wise               sync Wise only
  python sync.py wise-csv FILE      import a Wise statement CSV export
  python sync.py wise-keys          create the key pair for Wise SCA signing
  python sync.py import-orders FILE import Shopify orders saved as JSON (e.g. older-than-60-day backfill)
  python sync.py costs              re-import product costs from the Google Sheet (COSTS_SHEET_URL)
  python sync.py cost-template     write templates/master_costs.xlsx pre-filled with your products
  python sync.py import-costs FILE  import unit costs from the master sheet (xlsx or csv)
  python sync.py import-manual FILE import manual transactions from a CSV
  python sync.py migrate-to-postgres  copy the local SQLite data into DATABASE_URL (one-off)
  python sync.py demo              build data/demo.db with sample data to try the dashboard
"""
import sys

from tracker import costs, db, manual


def main(argv):
    cmd = argv[0] if argv else "all"
    args = argv[1:]

    if cmd == "wise-keys":
        from tracker import wise
        wise.generate_keys()
        return
    if cmd == "migrate-to-postgres":
        from tracker import migrate
        migrate.sqlite_to_postgres(force="--force" in args)
        return
    if cmd == "demo":
        from tracker import demo
        demo.build()
        return

    conn = db.connect()
    if cmd in ("all", "shopify"):
        from tracker import shopify
        try:
            shopify.sync(conn, full="--full" in args)
        except shopify.ShopifyError as e:
            db.log_sync(conn, "shopify", "error", str(e))
            print(f"Shopify sync failed: {e}")
    if cmd in ("all", "wise"):
        from tracker import wise
        try:
            wise.sync(conn)
        except wise.WiseError as e:
            db.log_sync(conn, "wise", "error", str(e))
            print(f"Wise sync failed: {e}")
    if cmd in ("all", "costs"):
        try:
            costs.sync_sheet(conn)
        except Exception as e:
            db.log_sync(conn, "costs_sheet", "error", str(e))
            print(f"Cost sheet import failed: {e}")
    if cmd == "wise-csv":
        from tracker import wise
        for path in args:
            wise.import_csv(conn, path)
    elif cmd == "import-orders":
        import json
        from tracker import shopify
        nodes = json.load(open(args[0], encoding="utf-8"))
        for node in nodes:
            order, lines, refunds, refund_lines = shopify.parse_order(node)
            db.upsert(conn, "shopify_orders", [order])
            db.upsert(conn, "shopify_order_lines", lines)
            db.upsert(conn, "shopify_refunds", refunds)
            db.upsert(conn, "shopify_refund_lines", refund_lines)
        print(f"Imported {len(nodes)} orders from {args[0]}")
    elif cmd == "cost-template":
        costs.write_template(conn)
    elif cmd == "import-costs":
        costs.import_costs(conn, args[0])
    elif cmd == "import-manual":
        n = manual.import_csv(conn, args[0])
        print(f"Imported {n} manual transactions")
    elif cmd not in ("all", "shopify", "wise", "costs"):
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
