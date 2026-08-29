"""Vör Dashboard — Pipeline / Agent Flow page."""

import streamlit as st
from shared import h, inject_theme, load_patterns, load_pending_traces

inject_theme()

st.markdown(
    '<div class="purple-bar"><h1>🔄 Agent Pipeline</h1><span>'
    "How alerts flow through classify → audit → clear</span></div>",
    unsafe_allow_html=True,
)

patterns = load_patterns()
traces = load_pending_traces()

# ------------------------------------------------------------------
# Visual pipeline stages
# ------------------------------------------------------------------
st.subheader("Pipeline Stages")

stage_labels = [
    "🚨 Incoming Alert",
    "🔍 Classify",
    "📤 Enqueue Audit",
    "🛡️ Audit Review",
    "✅ Clear / Downgrade",
]
stage_desc = [
    "Pub/Sub pushes alert to POST /classify",
    "Deterministic enrichment + LLM classifier",
    "If SUPPRESS, queue audit via Cloud Tasks",
    "Auditor reviews pattern evidence",
    "Update confidence doc + clear under_review",
]

# Stages and connectors get their own interleaved columns
# (stage, arrow, stage, arrow, …) so each arrow renders *between* two
# cards. A previous version laid out 5 columns and wrote stage i's arrow
# into column i+1, which Streamlit appends before that column's own card
# is written on the next iteration -- so every arrow rendered stacked
# above the following card rather than beside it.
stage_widths = [4, 1] * (len(stage_labels) - 1) + [4]
cols = st.columns(stage_widths)

for index, (label, desc) in enumerate(zip(stage_labels, stage_desc)):
    icon, name = label.split(" ", 1)
    with cols[index * 2]:
        st.markdown(
            f'<div class="panel" style="text-align:center;">'
            f'<div style="font-size:2rem;margin-bottom:0.3rem;">{icon}</div>'
            f"<strong>{name}</strong><br/>"
            f'<span style="font-size:0.8rem;color:#aaa;">{desc}</span></div>',
            unsafe_allow_html=True,
        )
    if index < len(stage_labels) - 1:
        with cols[index * 2 + 1]:
            st.markdown(
                '<div style="text-align:center;margin-top:2.5rem;font-size:1.5rem;">➜</div>',
                unsafe_allow_html=True,
            )

st.divider()

# ------------------------------------------------------------------
# Decision distribution
# ------------------------------------------------------------------
left, right = st.columns(2)

with left:
    st.subheader("📊 Classification Decisions (Last Traces)")
    if traces.empty:
        st.info("No trace data.")
    else:
        clf = traces[traces["run_type"] == "classification"]
        counts = clf["decision"].value_counts().to_dict()
        for decision, count in counts.items():
            badge = {
                "SUPPRESS": "badge-suppress",
                "ESCALATE": "badge-escalate",
                "UNCERTAIN": "badge-uncertain",
            }.get(str(decision), "badge-provisional")
            st.markdown(
                f'<div class="triage-card">'
                f'<span class="{badge}">{h(decision)}</span> '
                f"<strong>{h(count)}</strong> runs</div>",
                unsafe_allow_html=True,
            )

with right:
    st.subheader("📊 Audit Actions (Last Traces)")
    if traces.empty:
        st.info("No trace data.")
    else:
        aud = traces[traces["run_type"] == "audit"]
        counts = aud["action"].value_counts().to_dict()
        for action, count in counts.items():
            badge = {
                "NO_ACTION": "badge-suppress",
                "DOWNGRADE": "badge-escalate",
                "RECOMMEND_UPGRADE_FOR_HUMAN_REVIEW": "badge-uncertain",
            }.get(str(action), "badge-provisional")
            st.markdown(
                f'<div class="triage-card">'
                f'<span class="{badge}">{h(action)}</span> '
                f"<strong>{h(count)}</strong> runs</div>",
                unsafe_allow_html=True,
            )

st.divider()

# ------------------------------------------------------------------
# Override breakdown
# ------------------------------------------------------------------
st.subheader("⚡ Override Breakdown")
if traces.empty:
    st.info("No trace data.")
else:
    clf = traces[traces["run_type"] == "classification"]
    overrides = clf[clf["overrides_fired"] != ""]["overrides_fired"].value_counts().to_dict()
    if not overrides:
        st.success("No overrides fired in recent traces — model decisions stood.")
    else:
        for override, count in overrides.items():
            st.markdown(
                f'<div class="timeline-entry timeline-flagged">'
                f"<strong>{h(override)}</strong> — fired {h(count)} time(s)</div>",
                unsafe_allow_html=True,
            )

# ------------------------------------------------------------------
# Patterns currently in each stage
# ------------------------------------------------------------------
st.divider()
st.subheader("🗺️ Patterns by Pipeline Stage")

if patterns.empty:
    st.info("No patterns.")
else:
    tabs = st.tabs(
        [
            "No History",
            "Provisional (not under review)",
            "Under Review",
            "Confirmed",
            "Needs Attention",
        ]
    )

    # No history is not directly in patterns, but we can show provisional with 0 instances
    with tabs[0]:
        st.markdown(
            "_Patterns with `status: NO_HISTORY` are not stored in confidence_docs — they appear on first classification._"
        )
        st.info("Check the Traces page for recent NO_HISTORY classifications.")

    with tabs[1]:
        prov = patterns[(patterns["tier"] == "provisional") & (~patterns["under_review"])]
        st.write(f"{len(prov)} provisional patterns not under review")
        if not prov.empty:
            st.dataframe(
                prov[
                    ["identity_key", "instance_count", "diversity_score", "days_since_last_review"]
                ],
                width="stretch",
                hide_index=True,
            )

    with tabs[2]:
        rev = patterns[patterns["under_review"]]
        st.write(f"{len(rev)} patterns currently under review")
        if not rev.empty:
            st.dataframe(
                rev[["identity_key", "tier", "instance_count", "days_since_last_review"]],
                width="stretch",
                hide_index=True,
            )

    with tabs[3]:
        conf = patterns[patterns["tier"] == "confirmed"]
        st.write(f"{len(conf)} confirmed patterns")
        if not conf.empty:
            st.dataframe(
                conf[
                    ["identity_key", "instance_count", "diversity_score", "days_since_last_review"]
                ],
                width="stretch",
                hide_index=True,
            )

    with tabs[4]:
        st.markdown(
            "_Patterns in needs_attention are those with repeated audit failures — see Home page for details._"
        )
        attn = patterns[patterns["failure_count"] >= 3]
        st.write(f"{len(attn)} patterns with failure_count ≥ 3")
        if not attn.empty:
            st.dataframe(
                attn[["identity_key", "tier", "failure_count", "instance_count"]],
                width="stretch",
                hide_index=True,
            )
