"""Vör Dashboard — Patterns detail page."""

import streamlit as st
from shared import inject_theme, load_patterns, set_selected_pattern_id

inject_theme()

st.markdown(
    '<div class="purple-bar"><h1>📋 Patterns</h1><span>'
    "Browse, filter, and inspect pattern confidence docs</span></div>",
    unsafe_allow_html=True,
)

patterns = load_patterns()

if patterns.empty:
    st.info("No patterns in store.")
    st.stop()

# ------------------------------------------------------------------
# Filters
# ------------------------------------------------------------------
left, mid, right = st.columns(3)
with left:
    tier_filter = st.multiselect(
        "Tier", ["confirmed", "provisional"], default=["confirmed", "provisional"]
    )
with mid:
    review_filter = st.selectbox("Under Review", ["All", "Yes", "No"])
with right:
    search = st.text_input("Search identity key", "")

filtered = patterns[patterns["tier"].isin(tier_filter)]
if review_filter == "Yes":
    filtered = filtered[filtered["under_review"]]
elif review_filter == "No":
    filtered = filtered[~filtered["under_review"]]

if search:
    filtered = filtered[filtered["identity_key"].str.contains(search, case=False, na=False)]

st.write(f"Showing {len(filtered)} of {len(patterns)} patterns")

# ------------------------------------------------------------------
# Table with selection
# ------------------------------------------------------------------
display_cols = [
    "identity_key",
    "tier",
    "under_review",
    "instance_count",
    "diversity_score",
    "days_since_last_review",
    "failure_count",
]
display = filtered[display_cols].copy()
display.columns = [
    "Pattern",
    "Tier",
    "Review?",
    "Instances",
    "Diversity",
    "Days Since Review",
    "Failures",
]

selected = st.dataframe(
    display,
    use_container_width=True,
    hide_index=True,
    on_select="rerun",
    selection_mode="single-row",
    height=420,
)

sel_rows = selected.get("selection", {}).get("rows", [])
if sel_rows:
    row_idx = sel_rows[0]
    selected_doc_id = filtered.iloc[row_idx]["doc_id"]
    set_selected_pattern_id(selected_doc_id)
    st.button("🔍 Inspect Pattern", on_click=lambda: st.switch_page("pages/detail.py"))

st.divider()

# ------------------------------------------------------------------
# Quick stats
# ------------------------------------------------------------------
st.subheader("📊 Pattern Stats")
col1, col2, col3, col4 = st.columns(4)
with col1:
    st.metric("Confirmed", len(patterns[patterns["tier"] == "confirmed"]))
with col2:
    st.metric("Provisional", len(patterns[patterns["tier"] == "provisional"]))
with col3:
    st.metric("Under Review", len(patterns[patterns["under_review"]]))
with col4:
    st.metric(
        "Avg Diversity", round(patterns["diversity_score"].mean(), 2) if not patterns.empty else 0.0
    )

st.divider()

# ------------------------------------------------------------------
# Evidence diversity distribution
# ------------------------------------------------------------------
st.subheader("🎨 Diversity Score Distribution")
st.bar_chart(patterns.set_index("identity_key")["diversity_score"], use_container_width=True)
