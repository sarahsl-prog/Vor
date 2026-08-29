"""Vör Dashboard — Pattern detail drill-down page."""

import json
from typing import Any

import streamlit as st
from shared import (
    get_selected_pattern_id,
    h,
    inject_theme,
    load_patterns,
    set_selected_pattern_id,
)

inject_theme()

st.markdown(
    '<div class="purple-bar"><h1>🔍 Pattern Detail</h1><span>'
    "Inspect a single confidence doc in depth</span></div>",
    unsafe_allow_html=True,
)

if st.button("← Back to Patterns"):
    set_selected_pattern_id("")
    st.switch_page("pages/patterns.py")

selected_id = get_selected_pattern_id()
if not selected_id:
    st.warning("No pattern selected. Go back and select one from the Patterns page.")
    st.stop()

patterns = load_patterns()
# Separate names for the filtered frame and the single row it yields.
# Rebinding one name from DataFrame to Series hid the type from mypy and
# made every row["field"] below look like a Series rather than a value.
matches = patterns[patterns["doc_id"] == selected_id]
if matches.empty:
    st.error(f"Pattern {selected_id} not found.")
    st.stop()

row = matches.iloc[0]

# ------------------------------------------------------------------
# Header
# ------------------------------------------------------------------
tier_badge = "badge-confirmed" if row["tier"] == "confirmed" else "badge-provisional"
review_badge: str = '<span class="badge-review">UNDER REVIEW</span>' if row["under_review"] else ""

st.markdown(
    f'<div class="panel">'
    f'<span class="{tier_badge}">{h(str(row["tier"]).upper())}</span> {review_badge}<br/>'
    f'<strong style="font-size:1.1rem;">{h(row["identity_key"])}</strong></div>',
    unsafe_allow_html=True,
)

# ------------------------------------------------------------------
# Key stats
# ------------------------------------------------------------------
cols = st.columns(5)
cols[0].metric("Instances", row["instance_count"])
cols[1].metric("Diversity Score", round(row["diversity_score"], 2))
cols[2].metric("Days Since Review", row["days_since_last_review"])
cols[3].metric("Failure Count", row["failure_count"])
cols[4].metric("Provenance", row["provenance"])

st.divider()

# ------------------------------------------------------------------
# Structural template fields
# ------------------------------------------------------------------
st.subheader("🧱 Structural Template Fields")
fields: dict[str, Any] = row.get("fields", {})
if not fields:
    st.info("No invariant fields computed yet.")
else:
    for field, value in fields.items():
        st.markdown(
            f'<div class="triage-card"><strong>{h(field)}</strong> = {h(json.dumps(value))}</div>',
            unsafe_allow_html=True,
        )

st.divider()

# ------------------------------------------------------------------
# Confirmed instances
# ------------------------------------------------------------------
st.subheader("📚 Confirmed Instances")
instances: list[dict[str, Any]] = row.get("confirmed_instances", [])
if not instances:
    st.info("No confirmed instances stored.")
else:
    for inst in instances:
        verified_badge = (
            "badge-confirmed" if inst.get("verified_by") == "human" else "badge-provisional"
        )
        st.markdown(
            f'<div class="timeline-entry">'
            f'<span class="{verified_badge}">{h(inst.get("verified_by", "unknown"))}</span> '
            f'<strong>{h(inst.get("instance_id", "—"))}</strong> | '
            f'{h(inst.get("host", "?"))} | {h(inst.get("user", "?"))} | '
            f'{h(str(inst.get("timestamp", ""))[:16])}</div>',
            unsafe_allow_html=True,
        )

st.divider()

# ------------------------------------------------------------------
# Raw JSON (collapsible)
# ------------------------------------------------------------------
with st.expander("🗂️ Raw Document JSON"):
    st.json(row.to_dict())
