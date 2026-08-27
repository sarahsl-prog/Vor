"""Vör Dashboard — Streamlit entry point."""

import sys
from pathlib import Path

# Allow imports from dashboard/ and project root
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))

import streamlit as st

st.set_page_config(
    page_title="Vör Dashboard",
    page_icon="🔮",
    layout="wide",
    initial_sidebar_state="expanded",
)

home = st.Page("pages/home.py", title="Home", icon="🏠", default=True)
patterns = st.Page("pages/patterns.py", title="Patterns", icon="📋")
detail = st.Page("pages/detail.py", title="Detail", icon="🔍")
pipeline = st.Page("pages/pipeline.py", title="Pipeline", icon="🔄")
traces = st.Page("pages/traces.py", title="Traces", icon="📜")

pg = st.navigation([home, patterns, detail, pipeline, traces])
pg.run()
