import tempfile
from pathlib import Path

import streamlit as st

from common import conn, ledger
from tracker import categorise, config, costs, db


def render():
    st.title("Data & sync")
    c = conn()

    st.subheader("Sync")
    a, b = st.columns(2)
    with a:
        st.markdown("**Shopify**: orders, refunds, products and payouts")
        if not (config.SHOPIFY_ACCESS_TOKEN or config.SHOPIFY_CLIENT_ID):
            st.caption("Not connected. Add Shopify credentials to `.env` (see README).")
        full = st.checkbox("Re-pull full history", key="full_shopify")
        if st.button("Sync Shopify", type="primary", disabled=not (config.SHOPIFY_ACCESS_TOKEN or config.SHOPIFY_CLIENT_ID)):
            _run("shopify", lambda log: __import__("tracker.shopify", fromlist=["sync"]).sync(c, full=full, log=log))
    with b:
        st.markdown("**Wise**: balances and statements")
        if not config.WISE_API_TOKEN:
            st.caption("Not connected. Add `WISE_API_TOKEN` to `.env`, or import CSV statements below.")
        if st.button("Sync Wise", type="primary", disabled=not config.WISE_API_TOKEN):
            _run("wise", lambda log: __import__("tracker.wise", fromlist=["sync"]).sync(c, log=log))
        wise_csv = st.file_uploader("Import Wise statement CSV", type=["csv"], key="wise_csv")
        if wise_csv is not None and st.button("Import Wise CSV"):
            from tracker import wise
            _run("wise_csv", lambda log: wise.import_csv(c, _tmp(wise_csv), log=log))

    st.divider()
    st.subheader("Product costs (master sheet)")
    L = ledger()
    missing = L.missing_cost_lines
    if len(missing):
        st.warning(f"{missing['product_title'].nunique()} sold products have no unit cost.")
        st.dataframe(missing.groupby("product_title")["quantity"].sum().sort_values(ascending=False).reset_index(),
                     hide_index=True, column_config={"product_title": "Product", "quantity": "Units sold"})
    if config.COSTS_SHEET_URL:
        st.markdown(f"Costs come from your [Google Sheet]({config.COSTS_SHEET_URL}) and refresh on every full sync.")
        if st.button("Refresh costs from Google Sheet", type="primary"):
            _run("costs_sheet", lambda log: costs.sync_sheet(c, log=log))
    m1, m2 = st.columns(2)
    with m1:
        if st.button("Create master cost sheet from Shopify products"):
            try:
                path = costs.write_template(c)
                st.session_state["template_ready"] = str(path)
            except RuntimeError as e:
                st.error(str(e))
        if st.session_state.get("template_ready") or costs.TEMPLATE_PATH.exists():
            st.download_button("Download master_costs.xlsx", costs.TEMPLATE_PATH.read_bytes(), file_name="master_costs.xlsx")
    with m2:
        up = st.file_uploader("Upload completed master sheet (xlsx or csv)", type=["xlsx", "csv"], key="costs_up")
        if up is not None and st.button("Import costs", type="primary"):
            n, unmatched = costs.import_costs(c, _tmp(up), log=lambda m: None)
            st.success(f"Imported {n} costs")
            if unmatched:
                st.warning(f"Not matched to a Shopify product: {', '.join(map(str, unmatched[:15]))}")

    current = db.read_df(c, "SELECT key, product_title, variant_title, collection, unit_cost, notes, updated_at FROM product_costs ORDER BY product_title")
    if not current.empty:
        st.caption("Current unit costs. Edit here for quick fixes and log restock cost changes on the Products page.")
        edited = st.data_editor(current, hide_index=True, width="stretch", key="cost_editor",
                                disabled=["key", "product_title", "variant_title", "updated_at"],
                                column_config={"key": None, "unit_cost": st.column_config.NumberColumn("Unit cost", format="£%.2f")})
        if st.button("Save cost changes"):
            for r in edited.to_dict("records"):
                c.execute("UPDATE product_costs SET unit_cost=?, collection=?, notes=?, updated_at=? WHERE key=?",
                          (r["unit_cost"], r["collection"], r["notes"], db.now_iso(), r["key"]))
            c.commit()
            st.success("Saved")
            st.rerun()

    st.divider()
    with st.expander("Categories & rules"):
        st.dataframe(categorise.categories(c), hide_index=True, width="stretch")
        rules = db.read_df(c, "SELECT id, priority, pattern, category, direction, field FROM category_rules ORDER BY priority, id")
        edited = st.data_editor(rules, hide_index=True, width="stretch", num_rows="dynamic", key="rules_editor",
                                column_config={"id": None,
                                               "category": st.column_config.SelectboxColumn("Category", options=categorise.categories(c)["category"].tolist()),
                                               "direction": st.column_config.SelectboxColumn("Direction", options=["any", "in", "out"]),
                                               "field": st.column_config.SelectboxColumn("Match on", options=["text", "detail_type"])})
        if st.button("Save rules"):
            kept = edited.dropna(subset=["id"])
            for rid in set(rules["id"]) - set(kept["id"]):
                c.execute("DELETE FROM category_rules WHERE id = ?", (int(rid),))
            for r in kept.to_dict("records"):
                c.execute("UPDATE category_rules SET priority=?, pattern=?, category=?, direction=?, field=? WHERE id=?",
                          (int(r["priority"] or 20), r["pattern"], r["category"], r["direction"] or "any", r["field"] or "text", int(r["id"])))
            for r in edited[edited["id"].isna()].to_dict("records"):
                if r.get("pattern") and r.get("category"):
                    c.execute("INSERT INTO category_rules (priority, pattern, category, direction, field) VALUES (?, ?, ?, ?, ?)",
                              (int(r.get("priority") or 20), r["pattern"], r["category"], r.get("direction") or "any", r.get("field") or "text"))
            c.commit()
            st.success("Rules saved")
            st.rerun()


def _run(name, fn):
    out = []
    with st.status(f"Syncing {name}…", expanded=True) as status:
        try:
            fn(lambda m: (out.append(m), st.write(m)))
            status.update(label=f"{name} sync complete", state="complete")
        except Exception as e:  # show any API/config problem in the UI
            db.log_sync(conn(), name, "error", str(e))
            status.update(label=f"{name} sync failed", state="error")
            st.error(str(e))


def _tmp(upload):
    path = Path(tempfile.gettempdir()) / f"ft_{upload.name}"
    path.write_bytes(upload.getvalue())
    return path
