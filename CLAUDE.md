# Padre65 Finance Tracker

Internal finance dashboard for Padre65 (UK clothing brand, Shopify store, Wise Business account, GBP, not VAT registered).
Owned by Daniel and Santiago. Pulls Shopify + Wise into a database, adds product costs and manual entries,
and shows P&L, product contribution, expenses, cash and pop-up event reports in a Streamlit app.

## Layout

- `tracker/` – data and logic, no UI
  - `shopify.py`, `wise.py` – API syncs (Wise uses the Activities feed; statements need SCA signing, which falls back automatically)
  - `costs.py` – product unit costs from the Google Sheet (`COSTS_SHEET_URL`), matched to Shopify by handle then title
  - `categorise.py` – rule + override categorisation of Wise and manual transactions
  - `reports.py` – `Ledger`: P&L, product contribution, cash flow, payout reconciliation
  - `events.py` – pop-up/event tagging and the event report
  - `db.py` – schema + connection; `pg.py` adapts SQLite-style SQL to Postgres
- `dashboard/` – Streamlit app (`app.py` + one module per page in `views/`)
- `config/categories.csv`, `config/category_rules.csv` – starting categories/rules, seeded into the DB (new rows are added on connect; deleted ones are not re-added)
- `sync.py` – command line (run `python sync.py help` for commands)
- `.github/workflows/sync.yml` – daily sync into the hosted database

## Running locally

```bash
pip install -r requirements.txt
python sync.py demo                                   # sample data in data/demo.db
set DB_PATH=data/demo.db && python -m streamlit run dashboard/app.py
```

Without `DATABASE_URL` the app uses SQLite (`DB_PATH`). With `DATABASE_URL` set (in `.env`) it uses the shared
**production** Postgres database on Supabase — the same data the live dashboard shows.

## Accounting rules — do not break these

- **Revenue comes from Shopify only.** Shopify payouts into Wise are category `Shopify Payout` (type `excluded`); counting them would double count revenue.
- **COGS = units sold × unit cost** (cost sheet, overridden from a date by `restock`/`cost_change` entries in `product_log`). Returned items that are restocked reverse COGS.
- **Stock purchases are cash, not P&L.** Categories of type `inventory` (supplier payments, inbound freight/import VAT) never appear in the P&L — their cost arrives via COGS. Unit costs in the sheet already include shipping/embroidery.
- **Excluded types** (`excluded`): payouts, internal transfers, owner investment/drawings/reimbursement, tax, loans, uncategorised money *in*.
- **Uncategorised money out counts as an expense** (so nothing is hidden); uncategorised money in is excluded until categorised.
- **Card refunds** take the category of the purchase they reverse.
- Contribution: CM1 = net revenue − COGS; CM2 = CM1 − payment fees − `fulfilment`; CM3 = CM2 − `marketing`; Net = CM3 − `opex` + `other_income`.
- Orders containing only `EXCLUDE_PRODUCTS` (default `TEST`) are ignored.
- Dates are reported in UK time (`Europe/London`).

## Working conventions

- Work on a branch and open a pull request; `main` deploys straight to the live dashboard.
- Keep SQL SQLite-style (`?` placeholders, `INSERT OR REPLACE`) — `tracker/pg.py` translates it for Postgres. New tables need an entry in `pg.PRIMARY_KEYS` (and `SERIAL_TABLES` if they use an autoincrement `id`).
- Charts: Plotly with the fixed palette in `dashboard/common.py` (`SERIES`), one £ axis per chart, `style_fig()` for layout.
- Money is GBP floats; display with `common.gbp()`.
- Use `common.table()` / `common.editor()` / `common.metric()` instead of `st.dataframe` / `st.data_editor` / `st.metric` so terms get hover definitions. Add a plain-English definition to `dashboard/glossary.py` for any new column, tile or P&L line.
- Test against demo data (`python sync.py demo`) before pointing at production.

## Safety

- **Never commit secrets.** `.env`, `keys/` and `data/` are gitignored. Secrets live in Streamlit Cloud secrets and GitHub Actions secrets.
- The production database holds real financial data and the owners' manual entries/categorisations, which can't be re-synced from Shopify or Wise. Do not run `DELETE`, `DROP`, `TRUNCATE` or `migrate-to-postgres --force` against `DATABASE_URL` without explicit confirmation from the owners. Prefer read-only queries when investigating.
- Wise and Shopify credentials are read-only; keep them that way.
