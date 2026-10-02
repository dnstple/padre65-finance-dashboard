"""Plain-English definitions shown when hovering over terms in the dashboard.

Keys are the labels as they appear on screen. Expense categories also pick up their
description from config/categories.csv, so they don't need repeating here.
"""

GLOSSARY = {
    # --- P&L lines -------------------------------------------------------------
    "Gross sales": "Full-price value of everything sold, before discounts and refunds.",
    "Discounts": "Money taken off orders by discount codes or manual price reductions.",
    "Refunds": "Money given back to customers for returned or refunded items, on the date of the refund.",
    "Other refunds & adjustments": "Refunds not tied to a specific item, e.g. a goodwill partial refund.",
    "VAT": "VAT included in sale prices. Zero while the business isn't VAT registered.",
    "Shipping charged": "Delivery fees customers paid at checkout.",
    "Shipping refunded": "Delivery fees given back to customers.",
    "Net revenue": "What customers actually paid for products and delivery: gross sales minus discounts, refunds and VAT.",
    "COGS": "Cost of goods sold: units sold × landed unit cost (product + inbound shipping + embroidery/printing), from the cost sheet.",
    "COGS reversed (returns)": "Cost added back when a returned item goes back into stock, so it can be sold again.",
    "Gross profit (CM1)": "Contribution margin 1: net revenue minus COGS. What's left after paying for the product itself.",
    "Payment fees": "Shopify Payments card processing fees, taken from each payout.",
    "Payment fees (estimated)": "Card fees estimated from the Shopify rate because actual fee data isn't available.",
    "Packaging (per-order estimate)": "A flat packaging cost per order, from the PACKAGING_COST_PER_ORDER setting.",
    "Contribution after fulfilment (CM2)": "Contribution margin 2: CM1 minus card fees, postage and packaging. What each sale earns once it has been delivered.",
    "Contribution after marketing (CM3)": "Contribution margin 3: CM2 minus marketing (Meta ads etc.). What sales earn after paying to acquire them; this has to cover overheads.",
    "Net profit": "CM3 minus overheads (shoots, samples, software, admin…) plus other income. The bottom line.",
    # --- margins -----------------------------------------------------------------
    "Gross margin (CM1 %)": "Gross profit as a % of net revenue. Of every £100 sold, how much is left after product cost.",
    "CM1 %": "Gross profit as a % of net revenue. Of every £100 sold, how much is left after product cost.",
    "CM2 %": "CM2 as a % of net revenue: margin left after product cost, card fees, postage and packaging.",
    "CM3 %": "CM3 as a % of net revenue: margin left after product, fulfilment and marketing costs.",
    "Net margin %": "Net profit as a % of net revenue.",
    "Margin": "Gross profit as a % of net sales (after discounts), using the landed unit cost.",
    # --- product / contribution table ---------------------------------------------
    "Net revenue (product)": "Product sales after discounts and refunds, excluding VAT and delivery fees.",
    "Unit cost": "Landed cost of one unit: product + inbound shipping + embroidery/printing. From the cost sheet, or the latest restock log entry.",
    "Units": "Number of items sold.",
    "Returned": "Units refunded to customers.",
    "CM2": "CM1 minus this product's share of card fees, postage and packaging (allocated by share of revenue).",
    "CM3": "CM2 minus this product's share of marketing spend (allocated by share of revenue).",
    "Fees & fulfilment (alloc.)": "This product's share of card fees, postage, packaging and delivery income, split by share of net revenue. Shared costs can't be traced to one product exactly.",
    "Marketing (alloc.)": "This product's share of marketing spend, split by share of net revenue.",
    "No cost": "Ticked if this product has no unit cost in the cost sheet, so its COGS shows £0 and its margin is overstated.",
    "In stock": "Units currently available in Shopify inventory.",
    # --- sales / events ------------------------------------------------------------
    "Orders": "Number of separate checkouts.",
    "AOV": "Average order value: net sales ÷ number of orders.",
    "Net sales": "What customers paid for products after discounts (excluding VAT and delivery).",
    "Gross": "Full-price value before discounts.",
    "% of net": "Share of total net sales.",
    "% of orders": "Share of the total number of orders.",
    "Avg net price": "Average price actually paid per unit, after discounts.",
    "Stock cost": "Units sold × landed unit cost (the COGS for these items).",
    "Gross profit": "Net sales minus the stock cost of the items sold.",
    "Trading hrs*": "Hours between the first and last sale recorded that day. Not the actual opening hours.",
    "Net / hr": "Net sales per trading hour.",
    "First sale": "Time of the first order that day (UK time).",
    "Last sale": "Time of the last order that day (UK time).",
    "Basket": "Number of items in the order.",
    "Pricing": "Whether the order had any discount applied.",
    "Avg unit price": "Average price actually paid per unit, after discounts.",
    "Units sold": "Number of items sold.",
    "Card fees": "Shopify Payments processing fees on these orders.",
    "Event costs": "Costs you've tagged to this event (stall fee, travel, kit…) under Manage this event.",
    "Event profit": "Net sales minus stock cost, card fees and tagged event costs.",
    "Style (all colourways)": "Product line with all its colours combined, e.g. Club Long Sleeve.",
    "Colour family": "Colours grouped into families, e.g. Vintage White and Cream are both White / Cream.",
    # --- expenses / cash -------------------------------------------------------------
    "Group": "Marketing and Fulfilment count towards contribution (CM2/CM3); Overheads come off after; Stock purchases are cash only and reach the P&L as COGS when items sell.",
    "Spend": "Total paid out in this category for the selected period.",
    "Payments": "Number of transactions.",
    "Payee": "Who the money was paid to (merchant or recipient).",
    "Payee / payer": "Who the money was paid to, or received from.",
    "Amount": "In GBP. Money out is negative; money in is positive.",
    "Source": "Where the transaction came from: Wise, or a manual entry (with how it was paid).",
    "Category": "How the transaction is classified in the reports. Change it here, or add a rule to do it automatically.",
    "Paid with": "How a manual payment was made, e.g. a personal card. Useful for knowing who to reimburse.",
    "Direction": "out = money paid; in = money received.",
    "Shopify status": "Shopify's status for the payout.",
    "Check": "Whether a matching deposit was found in Wise within 7 days.",
    "Arrived in Wise": "Date the matching Shopify deposit landed in Wise.",
    "Payout date": "Date Shopify sent the payout.",
    "Marketing": "Ads and promotion (Meta, Google, events ads). Counts towards CM3.",
    "Fulfilment": "Getting orders to customers: postage, packaging and non-Shopify payment fees. Counts towards CM2.",
    "Overheads": "Running costs not tied to individual sales: shoots, samples, software, admin, travel. Taken off after CM3.",
    "Stock purchases": "Payments to suppliers for stock, inbound freight and import VAT. Cash only: it reaches the P&L as COGS when items sell.",
    "Units sold (all time)": "Number of items of this product sold since the store opened.",
    "Current unit cost": "Landed cost of one unit today: the latest restock/cost-change entry, or the cost sheet.",
    "Gross margin": "Gross profit as a % of net sales for this product (after discounts, using landed unit cost).",
    "In stock now": "Units of this product currently in Shopify inventory, all sizes combined.",
    # --- headline tiles ------------------------------------------------------------
    "Contribution after marketing (CM3) tile": "CM2 minus marketing. If this is positive, sales pay for their own product, delivery and ads.",
    "Cash in Wise (GBP)": "Combined balance of the GBP accounts in Wise at the last sync.",
}


def define(label, categories=None):
    """Definition for a label, falling back to expense category descriptions."""
    if label in GLOSSARY:
        return GLOSSARY[label]
    if categories is not None and label in categories:
        return categories[label]
    return None
