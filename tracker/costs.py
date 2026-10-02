"""Product unit costs: master sheet template/import, the restock log, and cost lookup for COGS."""
import pandas as pd

from . import config, db

TEMPLATE_PATH = config.ROOT / "templates" / "master_costs.xlsx"


def write_template(conn, path=TEMPLATE_PATH, log=print):
    """Create a master cost sheet pre-filled with every Shopify product, ready for unit costs."""
    variants = db.read_df(conn, "SELECT * FROM shopify_variants")
    if variants.empty:
        raise RuntimeError("No products yet - run `python sync.py shopify` first.")
    existing = db.read_df(conn, "SELECT * FROM product_costs")
    prod_cost = existing[existing["key"].str.startswith("product:")].set_index("product_id")
    var_cost = existing[existing["key"].str.startswith("variant:")].set_index("variant_id")

    products = (
        variants.groupby(["product_id", "product_title"], as_index=False)
        .agg(collections=("collections", "first"), status=("product_status", "first"),
             variants=("variant_id", "count"), price=("price", "max"),
             skus=("sku", lambda s: ", ".join(x for x in s.dropna()[:6])))
        .sort_values("product_title")
    )
    products["collection"] = products["product_id"].map(prod_cost["collection"]) if not prod_cost.empty else ""
    products["unit_cost"] = products["product_id"].map(prod_cost["unit_cost"]) if not prod_cost.empty else None
    products["notes"] = products["product_id"].map(prod_cost["notes"]) if not prod_cost.empty else ""
    products = products[["product_id", "product_title", "status", "collections", "collection", "variants",
                         "skus", "price", "unit_cost", "notes"]]

    overrides = variants.sort_values(["product_title", "variant_title"])[
        ["variant_id", "product_id", "product_title", "variant_title", "sku", "price"]
    ].copy()
    overrides["unit_cost"] = overrides["variant_id"].map(var_cost["unit_cost"]) if not var_cost.empty else None

    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as xl:
        pd.DataFrame({"How to fill this in": [
            "Products tab: enter the landed unit cost (GBP, incl. freight/duty per unit) for each product.",
            "'collection' is optional - the collection/drop the product was bought for.",
            "Variant overrides tab: ONLY fill unit_cost where a size/colour costs differently from its product.",
            "Leave product_id / variant_id untouched - they link rows to Shopify.",
            "When costs change on a restock, log it in the dashboard (Products page) rather than editing this sheet.",
            "Import with: python sync.py import-costs templates/master_costs.xlsx  (or upload on the Data page)",
        ]}).to_excel(xl, sheet_name="Instructions", index=False)
        products.to_excel(xl, sheet_name="Products", index=False)
        overrides.to_excel(xl, sheet_name="Variant overrides", index=False)
        for ws in xl.book.worksheets:
            for col in ws.columns:
                width = max(len(str(c.value or "")) for c in col[:200])
                ws.column_dimensions[col[0].column_letter].width = min(max(width + 2, 10), 60)
    log(f"Wrote {path} ({len(products)} products, {len(overrides)} variants)")
    return path


COST_COLUMNS = ("total_cost", "landed_cost", "unit_cost", "cost", "cost_price", "unit_cost_gbp", "cogs")
TITLE_COLUMNS = ("product_title", "product", "item", "name", "title")


def _norm_text(v):
    """Lower-case and unify dashes/whitespace so 'Tee – Black' matches 'Tee - Black'."""
    v = str(v or "").lower()
    for dash in ("–", "—", "‒", "−"):
        v = v.replace(dash, "-")
    return " ".join(v.split())


def _norm(s):
    return s.fillna("").map(_norm_text)


def _clean_columns(df):
    df.columns = [_norm_text(c).replace(" ", "_").replace("(", "").replace(")", "").replace("/", "_") for c in df.columns]
    return df


def _find_header(raw):
    """Locate the header row in sheets that have titles or blank rows above the table."""
    for i in range(min(len(raw), 15)):
        cells = {_norm_text(c).replace(" ", "_") for c in raw.iloc[i] if isinstance(c, str)}
        if cells & set(COST_COLUMNS) and (cells & set(TITLE_COLUMNS) or cells & {"sku", "variant_id", "handle", "product_id"}):
            return i
    return None


def _read_tables(path):
    path = str(path)
    if path.lower().endswith(".csv"):
        raws = {"Sheet1": pd.read_csv(path, header=None)}
    else:
        raws = pd.read_excel(path, sheet_name=None, header=None)
    tables = {}
    for name, raw in raws.items():
        h = _find_header(raw) if not raw.empty else None
        if h is None:
            continue
        df = raw.iloc[h + 1:].copy()
        df.columns = raw.iloc[h].tolist()
        df = df.loc[:, [c for c in df.columns if isinstance(c, str)]]
        tables[name] = _clean_columns(df)
    return tables


def _num(v):
    v = pd.to_numeric(v, errors="coerce")
    return None if pd.isna(v) else float(v)


def download_sheet(url, dest=config.ROOT / "data" / "costs_sheet.xlsx"):
    """Download a Google Sheet (shared by link) as xlsx."""
    import re

    import requests
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", url)
    if not m:
        raise ValueError("COSTS_SHEET_URL doesn't look like a Google Sheets link")
    resp = requests.get(f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=xlsx", timeout=60)
    if not resp.ok or "spreadsheetml" not in resp.headers.get("content-type", ""):
        raise ValueError("Couldn't download the cost sheet - check it's shared as 'Anyone with the link can view'")
    dest.write_bytes(resp.content)
    return dest


def import_costs(conn, path, log=print):
    """Import unit costs from a cost sheet.

    Matches rows to Shopify by product_id / variant_id, then handle, then SKU, then product title.
    Uses 'Total cost' (landed) if present, otherwise 'Unit cost'.
    """
    variants = db.read_df(conn, "SELECT * FROM shopify_variants")
    if variants.empty:
        raise RuntimeError("No Shopify products yet - run the Shopify sync first.")
    variants["_title"] = _norm(variants["product_title"])
    variants["_handle"] = _norm(variants["handle"]) if "handle" in variants else ""
    variants["_sku"] = _norm(variants["sku"])
    rows, unmatched, skipped = [], [], 0

    for name, df in _read_tables(path).items():
        cost_col = next(c for c in COST_COLUMNS if c in df.columns)
        title_col = next((c for c in TITLE_COLUMNS if c in df.columns), None)
        is_variant_sheet = "variant_id" in df.columns or ("sku" in df.columns and not title_col and "handle" not in df.columns)
        parts = [c for c in df.columns if c not in (cost_col,) and c in
                 ("unit_cost", "shipping", "embroidery_screen_printing", "printing", "freight", "duty", "packaging")]

        for r in df.to_dict("records"):
            cost = _num(r.get(cost_col))
            title = r.get(title_col) if title_col else None
            if cost is None:
                if isinstance(title, str) and title.strip():
                    skipped += 1
                continue
            collection = r.get("collection") if isinstance(r.get("collection"), str) and r.get("collection") not in ("�", "-") else ""
            breakdown = ", ".join(f"{c.replace('_', ' ')} {_num(r.get(c)):.2f}" for c in parts if _num(r.get(c)) is not None)
            notes = r.get("notes") if isinstance(r.get("notes"), str) else breakdown

            if is_variant_sheet:
                match = variants[variants["variant_id"].astype(str) == str(r.get("variant_id", "")).split(".")[0]]
                if match.empty and isinstance(r.get("sku"), str):
                    match = variants[variants["_sku"] == _norm_text(r["sku"])]
                if match.empty:
                    unmatched.append(r.get("sku") or r.get("variant_id"))
                    continue
                for v in match.to_dict("records"):
                    rows.append({"key": f"variant:{v['variant_id']}", "product_id": v["product_id"],
                                 "variant_id": v["variant_id"], "product_title": v["product_title"],
                                 "variant_title": v["variant_title"], "sku": v["sku"], "collection": collection,
                                 "unit_cost": cost, "notes": notes, "updated_at": db.now_iso()})
                continue

            match = variants[variants["product_id"].astype(str) == str(r.get("product_id", "")).split(".")[0]]
            if match.empty and isinstance(r.get("handle"), str):
                match = variants[variants["_handle"] == _norm_text(r["handle"])]
            if match.empty and isinstance(r.get("sku"), str):
                match = variants[variants["_sku"] == _norm_text(r["sku"])]
            if match.empty and isinstance(title, str):
                match = variants[variants["_title"] == _norm_text(title)]
            if match.empty:
                unmatched.append(title or r.get("handle"))
                continue
            p = match.iloc[0]
            rows.append({"key": f"product:{p['product_id']}", "product_id": p["product_id"], "variant_id": None,
                         "product_title": p["product_title"], "variant_title": None, "sku": None,
                         "collection": collection, "unit_cost": cost, "notes": notes, "updated_at": db.now_iso()})

    db.upsert(conn, "product_costs", rows, key="key")
    msg = f"Imported {len(rows)} unit costs"
    if skipped:
        msg += f"; {skipped} rows had no cost yet"
    if unmatched:
        msg += f"; {len(unmatched)} not matched to Shopify: {unmatched[:10]}"
    log(msg)
    return len(rows), unmatched


def sync_sheet(conn, log=print):
    if not config.COSTS_SHEET_URL:
        return
    path = download_sheet(config.COSTS_SHEET_URL)
    import_costs(conn, path, log=log)
    db.log_sync(conn, "costs_sheet", "ok", "imported from Google Sheet")


def unit_costs_for_lines(conn, lines):
    """Add a unit_cost column to order lines (needs product_id, variant_id, date).

    Order of precedence: latest restock/cost-change log entry on or before the sale date,
    then variant override, then product cost from the master sheet. Missing -> NaN.
    """
    lines = lines.copy()
    costs = db.read_df(conn, "SELECT * FROM product_costs")
    var_cost = costs[costs["key"].str.startswith("variant:")].set_index("variant_id")["unit_cost"]
    prod_cost = costs[costs["key"].str.startswith("product:")].set_index("product_id")["unit_cost"]
    base = lines["variant_id"].map(var_cost) if not var_cost.empty else pd.Series(float("nan"), index=lines.index)
    if not prod_cost.empty:
        base = base.fillna(lines["product_id"].map(prod_cost))
    lines["unit_cost"] = base.astype(float)

    log = db.read_df(conn, """
        SELECT date, product_id, unit_cost FROM product_log
        WHERE unit_cost IS NOT NULL AND event IN ('restock', 'cost_change') AND product_id IS NOT NULL
    """)
    if not log.empty and not lines.empty:
        log["date"] = pd.to_datetime(log["date"])
        left = lines.reset_index().rename(columns={"index": "_row"}).sort_values("date")
        left["date"] = pd.to_datetime(left["date"])
        merged = pd.merge_asof(left, log.sort_values("date").rename(columns={"unit_cost": "log_cost"}),
                               on="date", by="product_id", direction="backward")
        log_cost = merged.set_index("_row")["log_cost"]
        lines["unit_cost"] = log_cost.reindex(lines.index).fillna(lines["unit_cost"])
    return lines
