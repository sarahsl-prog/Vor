"""
Vör Dashboard — shared helpers: theme, Firestore data, demo fallback, state.
"""

from __future__ import annotations

import html
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import streamlit as st
from loguru import logger

# Ensure vor_agents is importable when running from dashboard/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mlflow
import pandas as pd

from vor_agents.enrichment import days_since_last_review
from vor_agents.firestore_config import firestore_database
from vor_agents.tracing import mlflow_experiment_name

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


_AUTO_REFRESH_ARMED = "_auto_refresh_armed"


def inject_auto_refresh(seconds: int = 15) -> None:
    """Rerun the whole app every ``seconds`` so live Firestore data refreshes.

    A prior version injected ``<script>window.location.reload()</script>`` via
    ``st.markdown`` — scripts inserted through markup never execute, so it
    silently did nothing. A ``run_every`` fragment is the supported mechanism.

    ``_tick`` is reached two different ways and must behave differently in each:
    directly from this function on every script run (part of drawing the page —
    must NOT rerun), and on its own ``run_every`` timer without any script run
    (the refresh — must rerun the app so the cached loaders re-read Firestore).

    The flag is therefore *disarmed here, on every script run*, and armed only
    once ``_tick`` has completed a render pass. An earlier version armed it and
    never disarmed it, so every subsequent script run — the timed refresh, but
    also any widget interaction or navigation — re-entered ``_tick`` already
    armed and called ``st.rerun`` while rendering, which reran the script, which
    re-entered ``_tick``: an unbreakable rerun loop that hung the Home and
    Traces pages on first interaction. Disarming is what breaks it, so keep the
    assignment below *outside* the fragment: a fragment-scoped rerun does not
    re-execute this function, which is exactly the distinction being drawn.
    """

    @st.fragment(run_every=seconds)
    def _tick() -> None:
        st.session_state["_last_refresh"] = datetime.now(UTC).strftime("%H:%M:%S UTC")
        if st.session_state.get(_AUTO_REFRESH_ARMED):
            st.rerun(scope="app")
        st.session_state[_AUTO_REFRESH_ARMED] = True

    st.session_state[_AUTO_REFRESH_ARMED] = False
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
                    # Computed from last_reviewed_at, never read as a stored
                    # field: no confidence_doc writes days_since_last_review,
                    # so .get() returned None for every real pattern and
                    # coerced to 0 -- "audited today" shown for a pattern
                    # that has never been audited at all (true value 9999),
                    # the most reassuring possible reading of the staleness
                    # signal in exactly the case that should alarm. Demo data
                    # supplies the field directly, which is why the table
                    # looked right without Firestore. enrichment's helper is
                    # the same one enrich() and the sweep use, so the
                    # dashboard cannot disagree with them about staleness --
                    # its docstring records this bug being found in enrich()
                    # first (Code-review-Aug25 C-3).
                    "days_since_last_review": days_since_last_review(data, doc_id=doc.id),
                    "last_reviewed_at": data.get("last_reviewed_at", ""),
                    "confirmed_instances": data.get("confirmed_instances", []),
                    "fields": data.get("fields", {}),
                }
            )
    except Exception as exc:  # noqa: BLE001 - dashboard must not crash on a bad doc/query
        st.warning(f"Firestore query failed, using demo data: {exc}")
        return _demo_patterns()
    return pd.DataFrame(rows)


# How many runs one traces query pulls back, newest first. The pages show
# far fewer than this; the headroom is for the Pipeline page, which
# aggregates decision and override counts over whatever it is given, so a
# too-small cap would quietly narrow those distributions rather than fail.
TRACE_QUERY_LIMIT = 500

TRACE_COLUMNS = [
    "run_id",
    "run_type",
    "decision",
    "action",
    "identity_key",
    "reasoning",
    "overrides_fired",
    "queued_at",
]


def _empty_traces() -> pd.DataFrame:
    """An empty frame that still has the columns callers index.

    `pd.DataFrame([])` has no columns at all, so a page that guards with
    `if traces.empty` but then filters on `traces["run_type"]` raises a
    KeyError instead of showing "no traces" -- an easy thing to hit here,
    because a healthy, quiet deployment legitimately has zero runs.
    """
    return pd.DataFrame({name: pd.Series(dtype="object") for name in TRACE_COLUMNS})


def _mlflow_configured() -> bool:
    return bool(os.environ.get("MLFLOW_TRACKING_URI"))


@st.cache_data(ttl=15)
def load_traces(limit: int = TRACE_QUERY_LIMIT) -> pd.DataFrame:
    """
    Recent classification and audit runs, read from MLflow.

    MLflow is where traces actually live. These pages previously read the
    `pending_traces` Firestore collection, which is *not* the trace store:
    tracing.py writes there only when MLflow logging raises, and
    replay_pending_traces() deletes each doc once it has been replayed. So
    the trace views were empty whenever the system was healthy, and when
    they weren't, they showed a sample biased to whatever failed to log,
    which then disappeared on the next replay run. pending_traces is now
    surfaced as what it is -- an outage backlog -- by
    count_pending_traces() below.

    Reads only run params and tags (one query), never the run_data.json
    artifact (one download per run). That is why tracing.py promotes the
    scalar fields onto the run; the artifact remains the full record for
    anyone who needs the alert, the enrichment or the untruncated
    reasoning.
    """
    if not _mlflow_configured():
        # No tracking server configured: MLFLOW_TRACKING_URI unset means
        # the service isn't logging to a durable store either, so there is
        # nothing to read rather than something that failed to be read.
        st.warning("MLFLOW_TRACKING_URI is not set — showing demo traces, not live data.")
        return _demo_traces()

    try:
        # output_format is explicit rather than left to default: it is what
        # decides the return type (a DataFrame vs a list[Run]), so naming
        # it is also what makes the cast below honest rather than a guess.
        result = mlflow.search_runs(
            experiment_names=[mlflow_experiment_name()],
            max_results=limit,
            order_by=["attributes.start_time DESC"],
            output_format="pandas",
        )
        runs = cast("pd.DataFrame", result)
    except Exception as exc:  # noqa: BLE001 - dashboard must not crash on a bad query
        st.warning(f"MLflow query failed, using demo data: {exc}")
        return _demo_traces()

    if runs.empty:
        return _empty_traces()

    def _column(name: str, default: str = "") -> pd.Series:
        # A param absent on every run in the result means MLflow omits the
        # column entirely; present on only some runs, it is NaN for the
        # rest. Both mean "this run type doesn't carry that field", and
        # both must read as the placeholder rather than blow up or render
        # "nan" to an analyst.
        if name not in runs:
            return pd.Series([default] * len(runs), index=runs.index)
        return runs[name].fillna(default)

    return pd.DataFrame(
        {
            "run_id": _column("run_id"),
            "run_type": _column("params.run_type", "unknown"),
            "decision": _column("params.decision", "—"),
            "action": _column("params.action", "—"),
            "identity_key": _column("params.identity_key"),
            "reasoning": _column("tags.reasoning").str.slice(0, 200),
            "overrides_fired": _column("params.overrides_fired"),
            "queued_at": _column("start_time").astype(str),
        }
    ).reset_index(drop=True)


@st.cache_data(ttl=15)
def count_pending_traces() -> int:
    """
    How many traces are sitting in the Firestore fallback queue waiting to
    be replayed into MLflow.

    A count, not a feed: a non-zero value is an operational signal that
    MLflow logging is failing (or was), which the pages surface as a
    health indicator. Reading it does not tell you what the agents
    decided -- load_traces() does -- and 0 is both the normal and the
    healthy answer.
    """
    client = _get_firestore_client()
    if client is None:
        return 0
    try:
        return sum(1 for _ in client.collection("pending_traces").stream())
    except Exception as exc:  # noqa: BLE001 - a health indicator must not take the page down
        logger.warning("Could not count pending_traces: {}", repr(exc))
        return 0


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


def _demo_traces() -> pd.DataFrame:
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


def _selected(key: str) -> str | None:
    """Read a selection out of session state as ``str | None``.

    ``st.session_state.get()`` is untyped, so returning it directly gave
    mypy an ``Any`` where ``str | None`` was declared -- the annotation
    claimed a guarantee nothing checked. Coercing here makes it true, and
    keeps a value written as a non-string (a numpy int from a dataframe
    selection, say) from reaching the callers that compare it to a doc_id.
    """
    value = st.session_state.get(key)
    return None if value is None else str(value)


def get_selected_pattern_id() -> str | None:
    return _selected("selected_pattern_id")


def set_selected_pattern_id(val: str) -> None:
    st.session_state.selected_pattern_id = val


def get_selected_trace_id() -> str | None:
    return _selected("selected_trace_id")


def set_selected_trace_id(val: str) -> None:
    st.session_state.selected_trace_id = val
