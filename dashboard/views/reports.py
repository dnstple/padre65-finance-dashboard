import streamlit as st

from common import heading
from views import events, planner

REPORTS = {"Event report": events.render, "Pop-up stock planner": planner.render}


def render():
    heading("Reports", "title")
    choice = st.segmented_control("Report", list(REPORTS), default="Event report", key="report_choice",
                                  help="Event report: how a past pop-up performed. Pop-up stock planner: what stock "
                                       "you'd need to buy for another pop-up.") or "Event report"
    st.divider()
    REPORTS[choice]()
