"""
Vör Dashboard — shared helpers: theme, Firestore data, demo fallback, state.
"""

from __future__ import annotations

import html
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import streamlit as st

# Ensure vor_agents is importable when running from dashboard/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from vor_agents.firestore_config import firestore_database

# ------------------------------------------------------------------
# Theme
# ------------------------------------------------------------------

DARK_CSS = """
<style>
/* Global dark background */
.stApp {
    background-color: #0a0a0a;
    color: #f0f0f0;
}

/* Sidebar */
section[data-testid="stSidebar"] {
    background-color: #111111;
}
section[data-testid="stSidebar"] * {
    color: #f0f0f0 !important;
}

/* Headers */
h1, h2, h3, .stMarkdown h1, .stMarkdown h2, .stMarkdown h3 {
    color: #f0f0f0 !important;
}

/* Metric labels + values */
[data-testid="stMetricLabel"],
[data-testid="stMetricValue"] {
    color: #f0f0f0 !important;
}

/* Purple accent bar */
.purple-bar {
    background: linear-gradient(90deg, #7b2d8e, #3a0a4e);
    padding: 0.8rem 1.2rem;
    border-radius: 8px;
    border: 1px solid #00e5ff;
    margin-bottom: 1rem;
}
.purple-bar h1 {
    color: #f0f0f0 !important;
    margin: 0;
    font-size: 1.6rem;
}
.purple-bar span {
    color: #f0f0f0;
    font-size: 0.9rem;
}

/* Panel containers */
.panel {
    background-color: #111111;
    border: 1px solid #333;
    border-radius: 8px;
    padding: 1rem;
    margin-bottom: 0.8rem;
}
.panel-header {
    background: linear-gradient(90deg, #7b2d8e, #3a0a4e);
    color: #f0f0f0;
    padding: 0.5rem 0.8rem;
    border-radius: 6px 6px 0 0;
    margin: -1rem -1rem 0.8rem -1rem;
    font-weight: 600;
    font-size: 0.95rem;
    border-bottom: 2px solid #00e5ff;
}

/* Source / tier badges */
.badge-confirmed {
    background-color: #00ff9f;
    color: #0a0a0a;
    padding: 0.15rem 0.5rem;
    border-radius: 4px;
    font-size: 0.75rem;
    font-weight: 600;
}
.badge-provisional {
    background-color: #ffaa00;
    color: #0a0a0a;
    padding: 0.15rem 0.5rem;
    border-radius: 4px;
    font-size: 0.75rem;
    font-weight: 600;
}
.badge-review {
    background-color: #ff6b8a;
    color: #0a0a0a;
    padding: 0.15rem 0.5rem;
    border-radius: 4px;
    font-size: 0.75rem;
    font-weight: 600;
}
.badge-escalate {
    background-color: #ff4444;
    color: #f0f0f0;
    padding: 0.15rem 0.5rem;
    border-radius: 4px;
    font-size: 0.75rem;
    font-weight: 600;
}
.badge-suppress {
    background-color: #00e5ff;
    color: #0a0a0a;
    padding: 0.15rem 0.5rem;
    border-radius: 4px;
    font-size: 0.75rem;
    font-weight: 600;
}
.badge-uncertain {
    background-color: #ffaa00;
    color: #0a0a0a;
    padding: 0.15rem 0.5rem;
    border-radius: 4px;
    font-size: 0.75rem;
    font-weight: 600;
}

/* Triage / activity cards */
.triage-card {
    background-color: #1a1a1a;
    border-left: 3px solid #00e5ff;
    padding: 0.5rem 0.7rem;
    margin-bottom: 0.4rem;
    border-radius: 0 4px 4px 0;
    font-size: 0.85rem;
}

/* Timeline entries */
.timeline-entry {
    padding: 0.5rem 0.8rem;
    border-left: 3px solid #7b2d8e;
    margin-bottom: 0.5rem;
    color: #f0f0f0;
    font-size: 0.85rem;
}
.timeline-improved {
    border-left-color: #00ff9f;
}
.timeline-flagged {
    border-left-color: #ff6b8a;
}

/* Table / dataframe text color override */
.stDataFrame, .stTable {
    color: #f0f0f0;
}

/* Button styling override for dark theme */
button[kind="secondary"] {
    background-color: #1a1a1a !important;
    color: #f0f0f0 !important;
    border: 1px solid #333 !important;
}
button[kind="secondary"]:hover {
    border-color: #00e5ff !important;
}
</style>
"""


def inject_theme() -> None:
    st.markdown(DARK_CSS, unsafe_allow_html=True)


# ------------------------------------------------------------------
# Render helpers
# ------------------------------------------------------------------


def h(value: Any, limit: int | None = None) -> str:
    """Escape a dynamic value for interpolation into ``unsafe_allow_html`` markup.

    Firestore/alert-derived strings reach these pages unfiltered — identity keys
    are built from Windows process ancestry and rule names, ``last_error`` is raw
    exception/model text. Without escaping, a value like ``<img src=x onerror=…>``
    stored in any of those fields executes in the analyst's browser (stored XSS);
    ``unsafe_allow_html=True`` performs no sanitisation of its own.

    When ``limit`` is given the text is truncated (with an ellipsis) *before*
    escaping, so the ellipsis is only ever appended when the value is actually
    longer than ``limit``.
    """
    text = str(value)
    if limit is not None and len(text) > limit:
        text = text[:limit] + "…"
    return html.escape(text)


def _as_int(value: Any, default: int = 0) -> int:
    """Coerce a Firestore field to ``int``. A field stored as an explicit ``null``
    survives ``dict.get(key, default)`` (which only covers a *missing* key) and
    would otherwise crash the page inside ``st.metric`` / arithmetic."""
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    """Coerce a Firestore field to ``float`` — see :func:`_as_int`. Guards
    ``round(diversity_score, 2)`` against a stored ``null``."""
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _join_identity(value: Any) -> str:
    """Render an identity key as ``a → b → c``.

    Normally a list of path segments, but a doc that stored it as a plain string
    would otherwise be joined character-by-character (``" → ".join("rule")``);
    anything else degrades to ``str()``.
    """
    if isinstance(value, (list, tuple)):
        return " → ".join(str(part) for part in value)
    return "" if value is None else str(value)


# ------------------------------------------------------------------
# Auto-refresh (live ticker)
# ------------------------------------------------------------------


def inject_auto_refresh(seconds: int = 15) -> None:
    """Rerun the whole app every ``seconds`` so live Firestore data refreshes.

    A prior version injected ``<script>window.location.reload()</script>`` via
    ``st.markdown`` — scripts inserted through markup never execute, so it
    silently did nothing. A ``run_every`` fragment is the supported mechanism.
    """

    @st.fragment(run_every=seconds)
    def _tick() -> None:
        # The fragment body runs once during the normal page render and then
        # again every `seconds`. Only the timed re-executions should trigger a
        # full-app rerun; the first pass is already part of the page render.
        st.session_state["_last_refresh"] = datetime.now(UTC).strftime("%H:%M:%S UTC")
        if st.session_state.get("_auto_refresh_primed"):
            st.rerun(scope="app")
        st.session_state["_auto_refresh_primed"] = True

    _tick()


# ------------------------------------------------------------------
# Firestore connection
# ------------------------------------------------------------------


@st.cache_resource
def _get_firestore_client() -> Any:
    try:
        from google.cloud import firestore

        return firestore.Client(database=firestore_database())
    except Exception as exc:  # noqa: BLE001 - degrade to demo data on any client/auth/network error
        st.warning(f"Firestore unavailable: {exc}")
        return None


def firestore_available() -> bool:
    return _get_firestore_client() is not None


# ------------------------------------------------------------------
# Data loading — confidence_docs
# ------------------------------------------------------------------


@st.cache_data(ttl=15)
def load_patterns() -> pd.DataFrame:
    client = _get_firestore_client()
    if client is None:
        return _demo_patterns()

    rows: list[dict[str, Any]] = []
    try:
        for doc in client.collection("confidence_docs").stream():
            data = doc.to_dict() or {}
            rows.append(
                {
                    "doc_id": doc.id,
                    "identity_key": _join_identity(data.get("identity_key")),
                    "tier": data.get("tier", "provisional"),
                    "provenance": data.get("provenance", "live"),
                    "under_review": bool(data.get("under_review", False)),
                    "failure_count": _as_int(data.get("failure_count")),
                    "instance_count": _as_int(data.get("instance_count")),
                    "diversity_score": round(_as_float(data.get("diversity_score")), 2),
                    "days_since_last_review": _as_int(data.get("days_since_last_review")),
                    "last_reviewed_at": data.get("last_reviewed_at", ""),
                    "confirmed_instances": data.get("confirmed_instances", []),
                    "fields": data.get("fields", {}),
                }
            )
    except Exception as exc:  # noqa: BLE001 - dashboard must not crash on a bad doc/query
        st.warning(f"Firestore query failed, using demo data: {exc}")
        return _demo_patterns()
    return pd.DataFrame(rows)


@st.cache_data(ttl=15)
def load_pending_traces() -> pd.DataFrame:
    client = _get_firestore_client()
    if client is None:
        return _demo_pending_traces()

    rows: list[dict[str, Any]] = []
    try:
        for doc in client.collection("pending_traces").stream():
            data = doc.to_dict() or {}
            run_data = data.get("run_data", {})
            rows.append(
                {
                    "doc_id": doc.id,
                    "run_type": data.get("run_type", "unknown"),
                    "decision": run_data.get("decision", "—"),
                    "action": run_data.get("action", "—"),
                    "identity_key": _join_identity(run_data.get("identity_key")),
                    "reasoning": str(run_data.get("reasoning", ""))[:200],
                    "overrides_fired": ", ".join(
                        str(o) for o in run_data.get("overrides_fired", [])
                    ),
                    "queued_at": data.get("queued_at", ""),
                }
            )
    except Exception as exc:  # noqa: BLE001 - dashboard must not crash on a bad doc/query
        st.warning(f"Firestore query failed, using demo data: {exc}")
        return _demo_pending_traces()
    return pd.DataFrame(rows)


@st.cache_data(ttl=15)
def load_needs_attention() -> pd.DataFrame:
    client = _get_firestore_client()
    if client is None:
        return _demo_needs_attention()

    rows: list[dict[str, Any]] = []
    try:
        for doc in client.collection("needs_attention").stream():
            data = doc.to_dict() or {}
            rows.append(
                {
                    "doc_id": doc.id,
                    "identity_key": _join_identity(data.get("identity_key")),
                    "failure_count": _as_int(data.get("failure_count")),
                    "last_error": str(data.get("last_error", ""))[:300],
                    "last_failed_at": data.get("last_failed_at", ""),
                    "resolved_at": data.get("resolved_at", ""),
                }
            )
    except Exception as exc:  # noqa: BLE001 - dashboard must not crash on a bad doc/query
        st.warning(f"Firestore query failed, using demo data: {exc}")
        return _demo_needs_attention()
    return pd.DataFrame(rows)


# ------------------------------------------------------------------
# Demo fallback data (runs when Firestore is unreachable)
# ------------------------------------------------------------------


def _demo_patterns() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "doc_id": "demo-1",
                "identity_key": "SharePoint_ToolPane_Rule → w3wp.exe → csc.exe → ToolPane_admin",
                "tier": "confirmed",
                "provenance": "seeded",
                "under_review": False,
                "failure_count": 0,
                "instance_count": 5,
                "diversity_score": 0.85,
                "days_since_last_review": 2,
                "last_reviewed_at": "2026-08-25T14:00:00Z",
                "confirmed_instances": [
                    {
                        "instance_id": "i1",
                        "host": "SRV-SP-01",
                        "user": "jsmith",
                        "timestamp": "2026-08-01T09:00:00Z",
                        "verified_by": "human",
                    },
                    {
                        "instance_id": "i2",
                        "host": "SRV-SP-02",
                        "user": "mjones",
                        "timestamp": "2026-08-03T14:00:00Z",
                        "verified_by": "human",
                    },
                    {
                        "instance_id": "i3",
                        "host": "SRV-SP-03",
                        "user": "kwhite",
                        "timestamp": "2026-08-05T22:00:00Z",
                        "verified_by": "bulk",
                    },
                ],
                "fields": {
                    "auth_method_present": True,
                    "session_cookie_present": True,
                    "integrity_level": "Medium",
                    "file_access_mode": "read",
                    "egress_follows_access": False,
                },
            },
            {
                "doc_id": "demo-2",
                "identity_key": "Exchange_ProxyShell_Rule → w3wp.exe → powershell.exe → ProxyShell_chain",
                "tier": "provisional",
                "provenance": "live",
                "under_review": True,
                "failure_count": 2,
                "instance_count": 2,
                "diversity_score": 0.35,
                "days_since_last_review": 12,
                "last_reviewed_at": "2026-08-15T10:30:00Z",
                "confirmed_instances": [
                    {
                        "instance_id": "e1",
                        "host": "SRV-EX-01",
                        "user": "admin",
                        "timestamp": "2026-08-10T11:00:00Z",
                        "verified_by": "human",
                    }
                ],
                "fields": {
                    "auth_method_present": True,
                    "integrity_level": "Medium",
                },
            },
            {
                "doc_id": "demo-3",
                "identity_key": "SQL_Server_XP_Rule → sqlservr.exe → xp_cmdshell → MSSQL_admin",
                "tier": "confirmed",
                "provenance": "live",
                "under_review": False,
                "failure_count": 0,
                "instance_count": 7,
                "diversity_score": 0.72,
                "days_since_last_review": 0,
                "last_reviewed_at": "2026-08-27T08:00:00Z",
                "confirmed_instances": [],
                "fields": {},
            },
            {
                "doc_id": "demo-4",
                "identity_key": "IIS_WebShell_Rule → w3wp.exe → cmd.exe → WebShell_drop",
                "tier": "provisional",
                "provenance": "live",
                "under_review": False,
                "failure_count": 3,
                "instance_count": 1,
                "diversity_score": 0.15,
                "days_since_last_review": 5,
                "last_reviewed_at": "",
                "confirmed_instances": [],
                "fields": {},
            },
        ]
    )


def _demo_pending_traces() -> pd.DataFrame:
    now = datetime.now(UTC).isoformat()
    return pd.DataFrame(
        [
            {
                "doc_id": "t1",
                "run_type": "classification",
                "decision": "SUPPRESS",
                "action": "—",
                "identity_key": "SharePoint_ToolPane_Rule → w3wp.exe → csc.exe → ToolPane_admin",
                "reasoning": "Template match with no structural deviations. All invariant fields consistent.",
                "overrides_fired": "",
                "queued_at": now,
            },
            {
                "doc_id": "t2",
                "run_type": "classification",
                "decision": "ESCALATE",
                "action": "—",
                "identity_key": "IIS_WebShell_Rule → w3wp.exe → cmd.exe → WebShell_drop",
                "reasoning": "Deterministic diff found deviation in integrity_level that model did not report.",
                "overrides_fired": "ground_truth_missed",
                "queued_at": now,
            },
            {
                "doc_id": "t3",
                "run_type": "audit",
                "decision": "—",
                "action": "NO_ACTION",
                "identity_key": "Exchange_ProxyShell_Rule → w3wp.exe → powershell.exe → ProxyShell_chain",
                "reasoning": "Pattern evidence looks solid. No concerns found in confirmed instances.",
                "overrides_fired": "",
                "queued_at": now,
            },
            {
                "doc_id": "t4",
                "run_type": "classification",
                "decision": "UNCERTAIN",
                "action": "—",
                "identity_key": "SQL_Server_XP_Rule → sqlservr.exe → xp_cmdshell → MSSQL_admin",
                "reasoning": "Pattern is under active audit; SUPPRESS not allowed until review completes.",
                "overrides_fired": "under_review",
                "queued_at": now,
            },
        ]
    )


def _demo_needs_attention() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "doc_id": "na1",
                "identity_key": "IIS_WebShell_Rule → w3wp.exe → cmd.exe → WebShell_drop",
                "failure_count": 3,
                "last_error": "Model output failed schema validation: matched_pattern_id must be string or null",
                "last_failed_at": "2026-08-26T22:15:00Z",
                "resolved_at": "",
            },
        ]
    )


# ------------------------------------------------------------------
# Session-state helpers
# ------------------------------------------------------------------


def get_selected_pattern_id() -> str | None:
    return st.session_state.get("selected_pattern_id", None)


def set_selected_pattern_id(val: str) -> None:
    st.session_state.selected_pattern_id = val


def get_selected_trace_id() -> str | None:
    return st.session_state.get("selected_trace_id", None)


def set_selected_trace_id(val: str) -> None:
    st.session_state.selected_trace_id = val
