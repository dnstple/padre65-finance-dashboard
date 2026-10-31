"""Stock planner: the shared Winter 2027 buying plan, saved in the database so both owners see and edit the same plan."""
import os
import uuid
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

from common import conn, heading, ledger
from tracker import stockplan

_planner = components.declare_component("stock_planner", path=str(Path(__file__).resolve().parent.parent / "components" / "stock_planner"))
PEOPLE = ["Daniel", "Santiago"]
# the same planner as a fast web page (planner/ on Vercel), saving to the same database
PLANNER_URL = os.environ.get("PLANNER_URL", "https://padre65-stock-planner.vercel.app")


@st.cache_resource(max_entries=2, show_spinner=False)
def _history(ledger_id, _ledger):
    """Sales history behind the plan; recalculated whenever the ledger reloads (a save or a sync)."""
    return stockplan.history(conn(), _ledger)


def render():
    heading("Stock planner", "title")
    st.link_button("Open the stock planner ↗", PLANNER_URL, type="primary")
    st.caption("The planner runs as its own fast web page and saves to the same shared plan. Log in with the planner password.")
    if not st.toggle("Use it here instead (slower)", key="planner_here"):
        return
    c = conn()
    a, b = st.columns([3, 2])
    who = a.segmented_control("Who's editing?", PEOPLE, key="planner_who", default=PEOPLE[0],
                              help="Notes you add are labelled with this name, and the plan shows who saved the last change.") or PEOPLE[0]
    b.caption("One shared plan: every change saves automatically and your partner sees it next time they open or refresh this page.")

    live, default = stockplan.load(c), stockplan.load(c, stockplan.DEFAULT_ID)
    if live is None or default is None:
        st.info("There's no stock plan in the database yet. Load one with `python sync.py stock-plan-import <planner file>`.")
        return
    session = st.session_state.setdefault("planner_session", uuid.uuid4().hex)
    L = ledger()
    value = _planner(plan=live["plan"], start=default["plan"], images=stockplan.images(c), history=_history(id(L), L),
                     version=live["version"], updatedAt=live["updated_at"], updatedBy=live["updated_by"],
                     # tells the planner whether the latest version is its own save or your partner's change
                     updatedSession=session if live["version"] in st.session_state.setdefault("planner_my_versions", set()) else None,
                     session=session, user=who,
                     key="stock_planner", default=None)

    # a change from the planner: save it, then show it as the latest version
    if value and value.get("nonce") and value["nonce"] != st.session_state.get("planner_saved_nonce"):
        st.session_state["planner_saved_nonce"] = value["nonce"]
        # saved on the raw connection: the plan isn't financial data, so the cached ledger doesn't need reloading
        version = stockplan.save(c.inner, value["plan"], value.get("base"), who)
        st.session_state["planner_my_versions"].add(version)
        st.rerun()
