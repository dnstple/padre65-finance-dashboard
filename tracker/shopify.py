"""Pull orders, refunds, products and Shopify Payments data via the Admin GraphQL API."""
import time

import requests

from . import config, db

ORDERS_QUERY = """
query($cursor: String, $q: String) {
  orders(first: 50, after: $cursor, query: $q, sortKey: CREATED_AT) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id name createdAt processedAt cancelledAt test displayFinancialStatus sourceName
      paymentGatewayNames currencyCode
      subtotalPriceSet { shopMoney { amount } }
      totalDiscountsSet { shopMoney { amount } }
      totalShippingPriceSet { shopMoney { amount } }
      totalTaxSet { shopMoney { amount } }
      totalPriceSet { shopMoney { amount } }
      totalRefundedSet { shopMoney { amount } }
      lineItems(first: 100) {
        nodes {
          id sku title variantTitle quantity
          product { id title }
          variant { id }
          originalUnitPriceSet { shopMoney { amount } }
          originalTotalSet { shopMoney { amount } }
          discountAllocations { allocatedAmountSet { shopMoney { amount } } }
          taxLines { priceSet { shopMoney { amount } } }
        }
      }
      refunds {
        id createdAt
        totalRefundedSet { shopMoney { amount } }
        refundLineItems(first: 100) {
          nodes {
            id quantity restockType
            lineItem { id }
            subtotalSet { shopMoney { amount } }
            totalTaxSet { shopMoney { amount } }
          }
        }
        refundShippingLines(first: 10) { nodes { subtotalAmountSet { shopMoney { amount } } } }
      }
    }
  }
}
"""

VARIANTS_QUERY = """
query($cursor: String) {
  productVariants(first: 100, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id sku title price inventoryQuantity
      product { id title handle status collections(first: 5) { nodes { title } } }
    }
  }
}
"""

BALANCE_QUERY = """
query($cursor: String) {
  shopifyPaymentsAccount {
    balanceTransactions(first: 100, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        id type transactionDate test sourceType
        amount { amount currencyCode } fee { amount } net { amount }
        associatedOrder { id }
        associatedPayout { id status }
      }
    }
  }
}
"""

PAYOUTS_QUERY = """
query($cursor: String) {
  shopifyPaymentsAccount {
    payouts(first: 100, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes { id issuedAt status net { amount currencyCode } }
    }
  }
}
"""


class ShopifyError(Exception):
    pass


class ShopifyClient:
    def __init__(self):
        if not config.SHOPIFY_STORE:
            raise ShopifyError("SHOPIFY_STORE is not set in .env")
        self.url = f"https://{config.SHOPIFY_STORE}/admin/api/{config.SHOPIFY_API_VERSION}/graphql.json"
        self.session = requests.Session()
        self.session.headers["X-Shopify-Access-Token"] = self._token()
        self.session.headers["Content-Type"] = "application/json"

    def _token(self):
        if config.SHOPIFY_ACCESS_TOKEN:
            return config.SHOPIFY_ACCESS_TOKEN
        if config.SHOPIFY_CLIENT_ID and config.SHOPIFY_CLIENT_SECRET:
            # Client credentials grant for apps created in the Shopify Dev Dashboard
            resp = requests.post(
                f"https://{config.SHOPIFY_STORE}/admin/oauth/access_token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": config.SHOPIFY_CLIENT_ID,
                    "client_secret": config.SHOPIFY_CLIENT_SECRET,
                },
                timeout=30,
            )
            if not resp.ok:
                raise ShopifyError(f"Could not get Shopify token ({resp.status_code}): {resp.text[:300]}")
            return resp.json()["access_token"]
        raise ShopifyError("Set SHOPIFY_ACCESS_TOKEN or SHOPIFY_CLIENT_ID/SHOPIFY_CLIENT_SECRET in .env")

    def query(self, query, variables=None):
        for attempt in range(6):
            resp = self.session.post(self.url, json={"query": query, "variables": variables or {}}, timeout=60)
            if resp.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            if not resp.ok:
                raise ShopifyError(f"Shopify API error {resp.status_code}: {resp.text[:300]}")
            body = resp.json()
            errors = body.get("errors") or []
            if any((e.get("extensions") or {}).get("code") == "THROTTLED" for e in errors):
                time.sleep(2 ** attempt)
                continue
            if errors:
                raise ShopifyError("; ".join(e.get("message", str(e)) for e in errors))
            return body["data"]
        raise ShopifyError("Shopify API kept throttling; try again later")

    def paginate(self, query, path, variables=None):
        cursor = None
        while True:
            data = self.query(query, {**(variables or {}), "cursor": cursor})
            for key in path:
                data = data[key] if data else None
            if not data:
                return
            yield from data["nodes"]
            if not data["pageInfo"]["hasNextPage"]:
                return
            cursor = data["pageInfo"]["endCursor"]


def _money(obj):
    try:
        return float(obj["shopMoney"]["amount"])
    except (TypeError, KeyError):
        return 0.0


def _gid(value):
    return value.rsplit("/", 1)[-1] if value else None


def parse_order(o):
    order_id = _gid(o["id"])
    order = {
        "id": order_id,
        "name": o["name"],
        "created_at": o["createdAt"],
        "processed_at": o.get("processedAt") or o["createdAt"],
        "cancelled_at": o.get("cancelledAt"),
        "financial_status": o.get("displayFinancialStatus"),
        "test": int(bool(o.get("test"))),
        "source_name": o.get("sourceName"),
        "gateways": ",".join(o.get("paymentGatewayNames") or []),
        "currency": o.get("currencyCode"),
        "subtotal": _money(o.get("subtotalPriceSet")),
        "discounts": _money(o.get("totalDiscountsSet")),
        "shipping": _money(o.get("totalShippingPriceSet")),
        "tax": _money(o.get("totalTaxSet")),
        "total": _money(o.get("totalPriceSet")),
        "refunded": _money(o.get("totalRefundedSet")),
    }
    lines = []
    for li in o["lineItems"]["nodes"]:
        lines.append({
            "id": _gid(li["id"]),
            "order_id": order_id,
            "product_id": _gid((li.get("product") or {}).get("id")),
            "variant_id": _gid((li.get("variant") or {}).get("id")),
            "sku": li.get("sku") or None,
            "product_title": (li.get("product") or {}).get("title") or li.get("title"),
            "variant_title": li.get("variantTitle"),
            "quantity": li["quantity"],
            "unit_price": _money(li.get("originalUnitPriceSet")),
            "gross": _money(li.get("originalTotalSet")),
            "discount": sum(_money(d["allocatedAmountSet"]) for d in li.get("discountAllocations") or []),
            "tax": sum(_money(t["priceSet"]) for t in li.get("taxLines") or []),
        })
    refunds, refund_lines = [], []
    for r in o.get("refunds") or []:
        refund_id = _gid(r["id"])
        shipping_nodes = ((r.get("refundShippingLines") or {}).get("nodes")) or []
        refunds.append({
            "id": refund_id,
            "order_id": order_id,
            "created_at": r["createdAt"],
            "total": _money(r.get("totalRefundedSet")),
            "shipping": sum(_money(s["subtotalAmountSet"]) for s in shipping_nodes),
        })
        for rl in r["refundLineItems"]["nodes"]:
            refund_lines.append({
                "id": _gid(rl["id"]),
                "refund_id": refund_id,
                "order_id": order_id,
                "created_at": r["createdAt"],
                "line_item_id": _gid((rl.get("lineItem") or {}).get("id")),
                "quantity": rl["quantity"],
                "subtotal": _money(rl.get("subtotalSet")),
                "tax": _money(rl.get("totalTaxSet")),
                "restock_type": rl.get("restockType"),
            })
    return order, lines, refunds, refund_lines


def sync(conn, full=False, log=print):
    client = ShopifyClient()

    # Orders: full history first time, then re-pull the last 60 days to catch refunds/edits
    q = None
    if not full:
        last = conn.execute("SELECT MAX(created_at) FROM shopify_orders").fetchone()[0]
        if last:
            q = f"updated_at:>='{_days_before(last, 60)}'"
    n_orders = 0
    for node in client.paginate(ORDERS_QUERY, ["orders"], {"q": q}):
        order, lines, refunds, refund_lines = parse_order(node)
        db.upsert(conn, "shopify_orders", [order])
        db.upsert(conn, "shopify_order_lines", lines)
        db.upsert(conn, "shopify_refunds", refunds)
        db.upsert(conn, "shopify_refund_lines", refund_lines)
        n_orders += 1
    log(f"Shopify: {n_orders} orders synced")

    variants = []
    for v in client.paginate(VARIANTS_QUERY, ["productVariants"]):
        p = v["product"]
        variants.append({
            "variant_id": _gid(v["id"]),
            "product_id": _gid(p["id"]),
            "product_title": p["title"],
            "variant_title": v.get("title"),
            "sku": v.get("sku") or None,
            "price": float(v.get("price") or 0),
            "inventory_quantity": v.get("inventoryQuantity"),
            "product_status": p.get("status"),
            "collections": ", ".join(c["title"] for c in p["collections"]["nodes"]),
            "handle": p.get("handle"),
        })
    db.upsert(conn, "shopify_variants", variants, key="variant_id")
    log(f"Shopify: {len(variants)} product variants synced")

    try:
        txns = []
        for t in client.paginate(BALANCE_QUERY, ["shopifyPaymentsAccount", "balanceTransactions"]):
            if t.get("test"):
                continue
            txns.append({
                "id": _gid(t["id"]),
                "type": t["type"],
                "transaction_date": t["transactionDate"],
                "amount": float(t["amount"]["amount"]),
                "fee": float(t["fee"]["amount"]),
                "net": float(t["net"]["amount"]),
                "currency": t["amount"]["currencyCode"],
                "source_type": t.get("sourceType"),
                "order_id": _gid((t.get("associatedOrder") or {}).get("id")),
                "payout_id": _gid((t.get("associatedPayout") or {}).get("id")),
                "payout_status": (t.get("associatedPayout") or {}).get("status"),
            })
        db.upsert(conn, "shopify_balance_txns", txns)
        payouts = [{
            "id": _gid(p["id"]),
            "issued_at": p["issuedAt"],
            "status": p["status"],
            "net": float(p["net"]["amount"]),
            "currency": p["net"]["currencyCode"],
        } for p in client.paginate(PAYOUTS_QUERY, ["shopifyPaymentsAccount", "payouts"])]
        db.upsert(conn, "shopify_payouts", payouts)
        log(f"Shopify Payments: {len(txns)} balance transactions, {len(payouts)} payouts synced")
    except ShopifyError as e:
        log(f"Shopify Payments data skipped (card fees will be estimated): {e}")

    db.log_sync(conn, "shopify", "ok", f"{n_orders} orders")


def _days_before(iso, days):
    from datetime import datetime, timedelta
    d = datetime.fromisoformat(iso.replace("Z", "+00:00")) - timedelta(days=days)
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")
