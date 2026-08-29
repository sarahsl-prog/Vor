"""Vör Dashboard — Home / Overview page."""

import streamlit as st
from shared import (
    h,
    inject_auto_refresh,
    inject_theme,
    load_needs_attention,
    load_patterns,
    load_pending_traces,
)

inject_theme()
inject_auto_refresh(15)

st.markdown(
    '<div class="purple-bar"><h1>🔮 Vör Dashboard</h1><span>'
    "Self-tuning confidence layer for alert triage</span></div>",
    unsafe_allow_html=True,
)

# ------------------------------------------------------------------
# Load data
# ------------------------------------------------------------------
patterns = load_patterns()
traces = load_pending_traces()
needs_attention = load_needs_attention()

# ------------------------------------------------------------------
# Summary metrics
# ------------------------------------------------------------------
col1, col2, col3, col4, col5 = st.columns(5)

total_patterns = len(patterns)
confirmed = len(patterns[patterns["tier"] == "confirmed"]) if not patterns.empty else 0
provisional = len(patterns[patterns["tier"] == "provisional"]) if not patterns.empty else 0
under_review = len(patterns[patterns["under_review"]]) if not patterns.empty else 0
needs_attn = (
    len(needs_attention[needs_attention["resolved_at"] == ""]) if not needs_attention.empty else 0
)

with col1:
    st.metric("Patterns", total_patterns)
with col2:
    st.metric("Confirmed", confirmed)
with col3:
    st.metric("Provisional", provisional)
with col4:
    st.metric("Under Review", under_review)
with col5:
    st.metric(
        "Needs Attention",
        needs_attn,
        delta=f"{needs_attn} open" if needs_attn else None,
        delta_color="inverse",
    )

st.divider()

# ------------------------------------------------------------------
# Two-column layout: recent traces + needs attention
# ------------------------------------------------------------------
left, right = st.columns([3, 2])

with left:
    st.subheader("📈 Recent Agent Runs")
    if traces.empty:
        st.info("No traces yet.")
    else:
        for _, row in traces.head(10).iterrows():
            badge_class = (
                "badge-suppress"
                if row["decision"] == "SUPPRESS"
                else (
                    "badge-escalate"
                    if row["decision"] == "ESCALATE"
                    else (
                        "badge-uncertain" if row["decision"] == "UNCERTAIN" else "badge-provisional"
                    )
                )
            )
            decision_text = row["decision"] if row["decision"] != "—" else row["action"]
            overrides = (
                f"<br/><em>Overrides: {h(row['overrides_fired'])}</em>"
                if row["overrides_fired"]
                else ""
            )
            st.markdown(
                f'<div class="timeline-entry">'
                f'<strong>{h(str(row["run_type"]).upper())}</strong> — '
                f'<span class="{badge_class}">{h(decision_text)}</span><br/>'
                f'{h(row["identity_key"], 80)}{overrides}</div>',
                unsafe_allow_html=True,
            )

with right:
    st.subheader("🚨 Needs Attention")
    if needs_attention.empty:
        st.info("All clear.")
    else:
        open_items = needs_attention[needs_attention["resolved_at"] == ""]
        if open_items.empty:
            st.success("All escalations resolved.")
        else:
            for _, row in open_items.iterrows():
                st.markdown(
                    f'<div class="timeline-entry timeline-flagged">'
                    f'<strong>{h(row["identity_key"], 60)}</strong><br/>'
                    f'Failures: {h(row["failure_count"])} | '
                    f'Last: {h(str(row["last_failed_at"])[:16])}<br/>'
                    f'<code>{h(row["last_error"], 120)}</code></div>',
                    unsafe_allow_html=True,
                )

st.divider()

# ------------------------------------------------------------------
# Pattern overview table (top of feed)
# ------------------------------------------------------------------
st.subheader("📋 Patterns Overview")
if patterns.empty:
    st.info("No patterns in store.")
else:
    display = patterns[
        [
            "identity_key",
            "tier",
            "under_review",
            "instance_count",
            "diversity_score",
            "days_since_last_review",
            "failure_count",
        ]
    ].copy()
    display.columns = [
        "Pattern",
        "Tier",
        "Review?",
        "Instances",
        "Diversity",
        "Days Since Review",
        "Failures",
    ]
    st.dataframe(display, width="stretch", hide_index=True)
