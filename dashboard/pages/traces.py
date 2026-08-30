"""Vör Dashboard — Traces / Audit Log page."""

import streamlit as st
from shared import (
    count_pending_traces,
    h,
    inject_auto_refresh,
    inject_theme,
    load_traces,
)

inject_theme()
inject_auto_refresh(15)

st.markdown(
    '<div class="purple-bar"><h1>📜 Agent Traces</h1><span>'
    "Live feed of classification and audit runs, read from MLflow</span></div>",
    unsafe_allow_html=True,
)

traces = load_traces()

# A non-zero backlog means MLflow logging is failing (or recently was),
# so this feed is behind by that many runs. Surfaced here rather than
# left invisible: these pages used to *be* the backlog, and an analyst
# reading a trace feed needs to know when it is incomplete.
pending = count_pending_traces()
if pending:
    st.warning(
        f"{pending} trace(s) queued in Firestore awaiting replay into MLflow — "
        "this feed is missing them until POST /replay-traces drains the queue."
    )

if traces.empty:
    st.info("No agent runs logged yet.")
    st.stop()

# ------------------------------------------------------------------
# Filters
# ------------------------------------------------------------------
left, right = st.columns(2)
with left:
    type_filter = st.multiselect(
        "Run Type", ["classification", "audit"], default=["classification", "audit"]
    )
with right:
    decision_filter = st.text_input("Filter by decision/action", "")

filtered = traces[traces["run_type"].isin(type_filter)]
if decision_filter:
    filtered = filtered[
        filtered["decision"].str.contains(decision_filter, case=False, na=False)
        | filtered["action"].str.contains(decision_filter, case=False, na=False)
    ]

st.write(f"Showing {len(filtered)} of {len(traces)} traces")

# ------------------------------------------------------------------
# Trace feed
# ------------------------------------------------------------------
for _, row in filtered.iterrows():
    badge_class = {
        "SUPPRESS": "badge-suppress",
        "ESCALATE": "badge-escalate",
        "UNCERTAIN": "badge-uncertain",
        "NO_ACTION": "badge-suppress",
        "DOWNGRADE": "badge-escalate",
        "RECOMMEND_UPGRADE_FOR_HUMAN_REVIEW": "badge-uncertain",
    }.get(row["decision"] if row["decision"] != "—" else row["action"], "badge-provisional")

    decision_text = row["decision"] if row["decision"] != "—" else row["action"]
    overrides = f" | Overrides: {h(row['overrides_fired'])}" if row["overrides_fired"] else ""

    st.markdown(
        f'<div class="timeline-entry">'
        f'<strong>{h(str(row["run_type"]).upper())}</strong> — '
        f'<span class="{badge_class}">{h(decision_text)}</span>'
        f"{overrides}<br/>"
        f'<em>{h(row["identity_key"], 90)}</em><br/>'
        f'<span style="color:#aaa;font-size:0.8rem;">{h(row["reasoning"], 180)}</span></div>',
        unsafe_allow_html=True,
    )

st.divider()
st.caption(f"Last refreshed: {st.session_state.get('_last_refresh', 'now')}")
