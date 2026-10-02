# Padre65 Finance Tracker

Pulls Shopify orders and Wise transactions into a local database, combines them with your product costs and manual entries, and shows P&L, contribution, expenses and cash in a dashboard.

## Run it

```bash
pip install -r requirements.txt
python -m streamlit run dashboard/app.py
```

To try it with sample data first:

```bash
python sync.py demo
set DB_PATH=data/demo.db && python -m streamlit run dashboard/app.py
```

## 1. Wise setup (UK business account)

1. **Create a read-only API token.** In Wise, go to **Settings → Integrations and tools → API tokens → Add new token**. Choose **Read only**.
2. **Copy `.env.example` to `.env`** and set `WISE_API_TOKEN=`.
3. **Set up the signing key.** UK accounts must sign a challenge to read statements (Strong Customer Authentication). A key pair has already been created in `keys/`. If it's missing, run `python sync.py wise-keys`.
4. **Upload the public key.** In Wise, go to **API tokens → Manage public keys → Add new key** and upload `keys/wise_public.pem`. Keep `wise_private.pem` private and never share it.
5. **Run the sync.** Run `python sync.py wise` or click **Sync Wise** on the **Data & sync** page.
   - The first sync pulls everything since `WISE_START_DATE`.

**No API access?** You can download statement CSVs from Wise instead (**Balance → Statements → CSV**) and import them on **Data & sync**. If you later connect the API, rows imported from CSV are de-duplicated.

## 2. Shopify setup

1. **Create an app.** In the Shopify Dev Dashboard (dev.shopify.com), create an app for the store and install it.
2. **Give it these Admin API scopes:**
   - `read_orders`
   - `read_all_orders`: needed for orders older than 60 days.
   - `read_products`
   - `read_inventory`
   - `read_shopify_payments_payouts`: gives real card fees and payout reconciliation. Without it, fees are estimated using `SHOPIFY_FEE_PCT` and `SHOPIFY_FEE_FIXED`.
3. **Add credentials to `.env`.** Use either `SHOPIFY_CLIENT_ID` and `SHOPIFY_CLIENT_SECRET`, or `SHOPIFY_ACCESS_TOKEN`.
4. **Run the sync.** Run `python sync.py shopify`. Add `--full` to re-pull all history.

## 3. Product costs (master sheet)

**Google Sheet (current setup):** set `COSTS_SHEET_URL` in `.env` to your sheet, shared as "anyone with the link can view".
- It's re-imported on every `python sync.py`, or use `python sync.py costs` or the button on **Data & sync**.
- Rows are matched to Shopify by **Handle**, then by product title.
- **Total Cost** is used as the landed unit cost.

**Alternatively, use an Excel template:**

1. **Create the sheet.** On **Data & sync**, click **Create master cost sheet from Shopify products**, or run `python sync.py cost-template`.
   - It lists every product, so you only need to fill in `unit_cost`, the landed cost per unit including freight and duty.
2. **Add variant costs only where needed.** On the **Variant overrides** tab, fill in a cost only where a size or colour costs something different.
3. **Upload the sheet** on **Data & sync**, or run `python sync.py import-costs templates/master_costs.xlsx`.
4. **Log restocks and cost changes** on **Products & contribution → Product log**.
   - A logged unit cost applies to sales from that date onwards.
   - Earlier sales keep the old cost.

## 4. Manual transactions

Add payments made outside Wise on **Manual transactions**: personal cards, cash or other accounts. You can enter them one at a time or bulk import from a CSV using `templates/manual_transactions_template.csv`.

## How the numbers work

- **Net revenue:** Shopify sales minus discounts, refunds and VAT, plus shipping charged. Sales count on the order date and refunds on the refund date.
- **CM1 (gross profit):** net revenue minus COGS. COGS is units sold × unit cost. Returns that go back into stock reverse their COGS.
- **CM2:** CM1 minus payment fees, postage and packaging.
- **CM3:** CM2 minus marketing. Meta ads are picked up automatically from Wise card spend.
- **Net profit:** CM3 minus overheads, plus other income.
- **Left out of the P&L:**
  - Stock purchases: these are cash. Their cost reaches the P&L through COGS as items sell.
  - Shopify payouts into Wise: these are the same money as the Shopify revenue.
  - Transfers between your own accounts, owner money, tax payments and loans.
- **Categorising:** Wise transactions are categorised by rules. You can edit the rules or re-categorise transactions on **Expenses**.
  - Uncategorised money *out* counts as an expense.
  - Uncategorised money *in* is left out of profit until you categorise it.

## Files

| Path | What |
|---|---|
| `sync.py` | Command line for syncs and imports |
| `tracker/` | API clients, categorisation, cost logic and reports |
| `dashboard/` | Streamlit dashboard |
| `config/categories.csv`, `config/category_rules.csv` | Starting categories and rules (copied into the database the first time it runs) |
| `data/finance.db` | Your data (SQLite). Back this file up. |
| `.env`, `keys/` | Secrets. Never commit or share them. |
