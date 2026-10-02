"""Build data/demo.db with realistic sample data so the dashboard can be tried before connecting accounts."""
import random
from datetime import datetime, timedelta

from . import config, db

PRODUCTS = [
    ("1001", "Core Tee - Mocha Brown", 70, 14.5, ["XS", "S", "M", "L", "XL"]),
    ("1002", "Script Oversized Tee - Black", 80, 17.0, ["S", "M", "L", "XL"]),
    ("1003", "Club Hoodie - Vintage Black", 160, 38.0, ["S", "M", "L", "XL"]),
    ("1004", "Script Cap - Siena Brown", 50, 9.5, ["Default"]),
    ("1005", "Classic Foulard", 120, 22.0, ["Default"]),
    ("1006", "Core Crew", 140, 31.0, ["S", "M", "L", "XL"]),
]


def build():
    path = config.ROOT / "data" / "demo.db"
    if path.exists():
        path.unlink()
    conn = db.connect(path)
    rnd = random.Random(65)

    variants, costs = [], []
    for pid, title, price, cost, sizes in PRODUCTS:
        for i, s in enumerate(sizes):
            variants.append({"variant_id": f"{pid}{i}", "product_id": pid, "product_title": title,
                             "variant_title": s, "sku": f"{pid}-{s}", "price": price,
                             "inventory_quantity": rnd.randint(0, 12), "product_status": "ACTIVE", "collections": "SS26"})
        if pid != "1006":  # leave one product without a cost to show the warning
            costs.append({"key": f"product:{pid}", "product_id": pid, "variant_id": None, "product_title": title,
                          "variant_title": None, "sku": None, "collection": "SS26", "unit_cost": cost,
                          "notes": "", "updated_at": db.now_iso()})
    db.upsert(conn, "shopify_variants", variants)
    db.upsert(conn, "product_costs", costs)
    conn.execute("""INSERT INTO product_log (date, product_id, product_title, event, quantity, unit_cost, total_cost,
                    supplier, notes, created_at) VALUES ('2026-08-01', '1001', 'Core Tee - Mocha Brown', 'restock',
                    60, 15.2, 912, 'Factory Ltd', 'Restock - cotton price up', ?)""", (db.now_iso(),))
    conn.commit()

    orders, lines, refunds, rlines, wise = [], [], [], [], []
    start = datetime(2026, 2, 10)
    n = 0
    for day in range(0, 232):
        d = start + timedelta(days=day)
        rate = 0.15 if d.month < 7 else (0.6 if d.month < 9 else 1.8)
        for _ in range(sum(rnd.random() < rate for _ in range(3))):
            n += 1
            oid = str(5000 + n)
            ts = d + timedelta(hours=rnd.randint(8, 22), minutes=rnd.randint(0, 59))
            picks = rnd.sample(variants, rnd.choice([1, 1, 1, 2, 2, 3]))
            subtotal = disc_total = 0
            for j, v in enumerate(picks):
                qty = 1
                gross = v["price"] * qty
                disc = round(gross * 0.2, 2) if rnd.random() < 0.2 else 0
                subtotal += gross - disc
                disc_total += disc
                lines.append({"id": f"{oid}{j}", "order_id": oid, "product_id": v["product_id"], "variant_id": v["variant_id"],
                              "sku": v["sku"], "product_title": v["product_title"], "variant_title": v["variant_title"],
                              "quantity": qty, "unit_price": v["price"], "gross": gross, "discount": disc, "tax": 0})
            shipping = 4.99 if subtotal < 100 else 0
            total = subtotal + shipping
            orders.append({"id": oid, "name": f"#{1000 + n}", "created_at": ts.isoformat() + "Z",
                           "processed_at": ts.isoformat() + "Z", "cancelled_at": None, "financial_status": "PAID",
                           "test": 0, "source_name": "web", "gateways": "shopify_payments", "currency": "GBP",
                           "subtotal": subtotal, "discounts": disc_total, "shipping": shipping, "tax": 0,
                           "total": total, "refunded": 0})
            if rnd.random() < 0.06:
                l = lines[-1]
                rts = ts + timedelta(days=rnd.randint(3, 12))
                amt = l["gross"] - l["discount"]
                refunds.append({"id": f"r{oid}", "order_id": oid, "created_at": rts.isoformat() + "Z", "total": amt, "shipping": 0})
                rlines.append({"id": f"rl{oid}", "refund_id": f"r{oid}", "order_id": oid, "created_at": rts.isoformat() + "Z",
                               "line_item_id": l["id"], "quantity": 1, "subtotal": amt, "tax": 0, "restock_type": "RETURN"})
    db.upsert(conn, "shopify_orders", orders)
    db.upsert(conn, "shopify_order_lines", lines)
    db.upsert(conn, "shopify_refunds", refunds)
    db.upsert(conn, "shopify_refund_lines", rlines)

    # Wise: weekly Shopify payouts, ads, postage, software, stock payments
    balance = 2000.0
    k = 0

    def txn(d, amount, detail, desc, merchant="", counterparty=""):
        nonlocal balance, k
        k += 1
        balance += amount
        wise.append({"id": f"GBP:DEMO-{k}:{'CREDIT' if amount > 0 else 'DEBIT'}", "balance_id": "1", "currency": "GBP",
                     "date": d.isoformat() + "Z", "amount": round(amount, 2), "fee": 0,
                     "direction": "in" if amount > 0 else "out", "detail_type": detail, "description": desc,
                     "merchant": merchant, "counterparty": counterparty or merchant, "reference": f"DEMO-{k}",
                     "running_balance": round(balance, 2), "amount_base": round(amount, 2), "source": "api"})

    txn(datetime(2026, 2, 1, 9), 5000, "DEPOSIT", "Received money from Owner", counterparty="Owner")
    txn(datetime(2026, 2, 3, 9), -2400, "TRANSFER", "Sent money to Factory Ltd", counterparty="Factory Ltd")
    txn(datetime(2026, 7, 20, 9), -1800, "TRANSFER", "Sent money to Factory Ltd", counterparty="Factory Ltd")
    for week in range(34):
        d = datetime(2026, 2, 9) + timedelta(weeks=week)
        wk_orders = [o for o in orders if d - timedelta(days=7) <= datetime.fromisoformat(o["processed_at"][:-1]) < d]
        gross = sum(o["total"] for o in wk_orders)
        if gross:
            txn(d + timedelta(hours=6), gross - sum(o["total"] * 0.02 + 0.25 for o in wk_orders), "DEPOSIT",
                "Received money from SHOPIFY", counterparty="SHOPIFY INTERNATIONAL")
        if d.month >= 6:
            txn(d + timedelta(days=1), -(60 if d.month < 9 else 220), "CARD", "Card transaction", "FACEBK *ADS")
        if wk_orders:
            txn(d + timedelta(days=2), -3.4 * len(wk_orders), "CARD", "Card transaction", "ROYAL MAIL CLICK & DROP")
        if week % 4 == 0:
            txn(d + timedelta(days=3), -25, "CARD", "Card transaction", "SHOPIFY* BASIC")
            txn(d + timedelta(days=3, hours=1), -11.99, "CARD", "Card transaction", "CANVA")
    txn(datetime(2026, 5, 18, 12), -350, "TRANSFER", "Sent money to Jo Photo", counterparty="Jo Photo")
    txn(datetime(2026, 6, 2, 12), -89, "CARD", "Card transaction", "NOISSUE")
    db.upsert(conn, "wise_transactions", wise)
    db.upsert(conn, "wise_balances", [{"id": "1", "currency": "GBP", "type": "STANDARD", "name": "GBP",
                                       "amount": round(balance, 2), "updated_at": db.now_iso()}])

    from . import manual
    manual.add(conn, "2026-01-20", "Sample development", "Factory Ltd", 600, "out", "Samples & Development", "Personal card")
    manual.add(conn, "2026-04-12", "Pop-up stall", "Spitalfields Market", 120, "out", "Events & Pop-ups", "Cash")
    db.log_sync(conn, "demo", "ok", "demo data")
    print(f"Demo database written to {path}")
