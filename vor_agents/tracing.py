"""
Vör — MLflow tracing for the two LLM-calling entry points
(classify_alert(), audit_pattern()). Best-effort: an MLflow-logging
failure never fails the caller's own request. On a failure, the run data
is queued to a durable Firestore fallback (pending_traces) instead of
being dropped, and replayed later by replay_pending_traces() -- see
docs/superpowers/specs/2026-08-24-mlflow-tracing-design.md.
"""

import os
import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

import mlflow
from google.cloud.firestore import Client
from loguru import logger

from .env_config import env_int

if TYPE_CHECKING:
    from .schemas import AuditorOutput, ClassifierOutput

PENDING_TRACES_COLLECTION = "pending_traces"

DEFAULT_TRACE_REPLAY_BATCH_SIZE = 1000
TRACE_REPLAY_BATCH_SIZE_ENV_VAR = "TRACE_REPLAY_BATCH_SIZE"
# Caps how many pending_traces docs one replay run reads into memory.
# Previously unbounded -- see docs/Code-review-Aug25.md 2.2 -- an extended
# MLflow outage grows this collection without limit, and materializing
# all of it every 15 minutes (see docs/DEPLOY.md's replay schedule) is an
# avoidable OOM risk. 1000 is an unvalidated starting point, same posture
# as every other unvalidated interval/threshold in this project.


class TracingError(Exception):
    """
    Raised internally when even the Firestore fallback write fails --
    distinguishes "MLflow unreachable, fell back to Firestore" (expected,
    logged at WARNING, not this exception) from "the fallback itself is
    broken" (unexpected, logged at ERROR). Never raised to
    classify_alert()/audit_pattern() -- _log_run() catches it internally,
    per this module's "never fail the caller's own request" rule.
    """


def _write_pending_trace(run_type: str, run_data: dict[str, Any], firestore_client: Client) -> None:
    doc_id = str(uuid.uuid4())
    try:
        firestore_client.collection(PENDING_TRACES_COLLECTION).document(doc_id).set(
            {
                "run_type": run_type,
                "run_data": run_data,
                "queued_at": datetime.now(UTC).isoformat(),
            }
        )
    except Exception as exc:
        raise TracingError(f"Failed to queue pending trace: {exc}") from exc


DEFAULT_EXPERIMENT_NAME = "Default"
EXPERIMENT_NAME_ENV_VAR = "MLFLOW_EXPERIMENT_NAME"

# Scalar run_data fields promoted onto the MLflow run as params. Params
# are returned by search_runs(); everything else lives only inside the
# run_data.json artifact, which costs one download per run to read. That
# difference decides what a reader can do: aggregating decisions across
# every run (what the dashboard's pipeline view does) is one query over
# params, or N artifact downloads over the artifact. Only small, bounded,
# filterable values belong here -- the artifact stays the full record.
SUMMARY_PARAM_FIELDS = ("decision", "action", "uncertain_reason", "audit_failed")

# reasoning is free text and can be long, so it goes in a tag (8000 chars)
# rather than a param (6000), and is truncated rather than dropped -- a
# reader wanting the untruncated text reads the artifact.
REASONING_TAG = "reasoning"
IDENTITY_KEY_SEPARATOR = " → "
_MAX_TAG_LENGTH = 8000
_MAX_PARAM_LENGTH = 6000


def mlflow_experiment_name() -> str:
    """
    The experiment both the writer and any reader must agree on.

    MLflow honours $MLFLOW_EXPERIMENT_NAME implicitly, so _log_run() used
    to rely on that and never named an experiment itself. That works, but
    it leaves the reader (dashboard/shared.py) guessing at the same
    default from the other side of the system with nothing tying the two
    together. Resolving it here, and having the writer set it explicitly,
    makes the agreement checkable instead of coincidental.
    """
    return os.environ.get(EXPERIMENT_NAME_ENV_VAR) or DEFAULT_EXPERIMENT_NAME


def _param_value(value: Any, limit: int = _MAX_PARAM_LENGTH, separator: str = ", ") -> str:
    """
    Renders one run_data value as an MLflow param/tag string.

    Enums are unwrapped to `.value` deliberately: Decision and
    AuditorAction subclass str, but since Python 3.11 `str(Decision
    .SUPPRESS)` is "Decision.SUPPRESS", not "SUPPRESS". Writing that
    would make every reader's decision lookup miss -- and it would only
    show up as decisions quietly falling through to a default, never as
    an error. json.dumps (which the artifact goes through) unwraps them
    by value, so the param and the artifact would also have silently
    disagreed about the same field.

    Lists are joined rather than repr'd for the same reason: `str(["a",
    "b"])` writes a Python repr into a data field that a reader then has
    to eval back. `separator` only picks how a joined list reads -- the
    canonical machine-readable form of every field stays the artifact.
    """
    if isinstance(value, Enum):
        text = str(value.value)
    elif isinstance(value, (list, tuple)):
        text = separator.join(_param_value(item, limit) for item in value)
    else:
        text = str(value)
    return text[:limit]


def _summary_params(run_type: str, run_data: dict[str, Any]) -> dict[str, str]:
    """Params for one run: the two that were always written, plus
    whichever SUMMARY_PARAM_FIELDS this run_type actually carries
    (classification has decision, audit has action). Absent fields are
    omitted rather than written empty, so a reader can tell "this run
    type has no such field" from "the field was empty"."""
    params = {
        "run_type": run_type,
        # Joined with the same arrow the dashboard renders identity keys
        # with, so MLflow's own UI and every dashboard page show one
        # pattern the same way and no reader has to re-separate it.
        "identity_key": _param_value(
            run_data.get("identity_key"), separator=IDENTITY_KEY_SEPARATOR
        ),
    }
    for name in SUMMARY_PARAM_FIELDS:
        if name in run_data:
            params[name] = _param_value(run_data[name])
    if "overrides_fired" in run_data:
        # Always written, empty string included: "no override fired" is a
        # meaningful, queryable answer, not missing data.
        params["overrides_fired"] = _param_value(run_data["overrides_fired"])
    return params


def _log_run(run_type: str, run_data: dict[str, Any], firestore_client: Client) -> None:
    """
    Shared best-effort logging path for both public functions below.
    Tries MLflow directly first; on ANY failure, falls back to the
    durable Firestore queue instead of dropping the trace. Never raises
    to its caller -- if even the fallback write fails, that's logged at
    ERROR with the run data serialized into the log line itself, so it's
    at least recoverable from log storage by hand in the worst case.
    """
    try:
        mlflow.set_experiment(mlflow_experiment_name())
        with mlflow.start_run(run_name=f"{run_type}_{run_data.get('identity_key')}"):
            mlflow.log_params(_summary_params(run_type, run_data))
            if "reasoning" in run_data:
                mlflow.set_tag(REASONING_TAG, _param_value(run_data["reasoning"], _MAX_TAG_LENGTH))
            mlflow.log_dict(run_data, "run_data.json")
        return
    except Exception as exc:  # noqa: BLE001 — deliberate: any MLflow
        # failure (auth, connection, timeout) falls back, never raises.
        logger.bind(run_type=run_type).warning(
            "MLflow logging failed, falling back to pending_traces: {}", exc
        )

    try:
        _write_pending_trace(run_type, run_data, firestore_client)
    except TracingError as exc:
        logger.bind(run_type=run_type, run_data=run_data).error(
            "MLflow AND the pending_traces fallback both failed; trace data "
            "is only in this log line: {}",
            exc,
        )


def log_classification_trace(
    alert: dict[str, Any],
    enrichment: dict[str, Any],
    classifier_output: "ClassifierOutput",
    overrides_fired: list[str],
    firestore_client: Client,
) -> None:
    """
    Logs one classify_alert() call. overrides_fired lists which of
    classify_alert()'s deterministic overrides actually changed the
    decision (e.g. ["under_review"], ["ground_truth_missed"], or [] if
    the model's own decision stood untouched) -- queryable/filterable in
    MLflow without parsing the reasoning text.
    """
    run_data = {
        "identity_key": list(enrichment.get("pattern_identity_key", ())),
        "alert": alert,
        "enrichment": enrichment,
        "decision": classifier_output.decision,
        "uncertain_reason": classifier_output.uncertain_reason,
        # .model_dump() per item, not the StructuralDeviation objects
        # themselves: both sinks below need plain JSON-compatible values
        # (mlflow.log_dict json-serializes; the pending_traces fallback
        # writes into Firestore, which can't store a pydantic model
        # either). Passing the models straight through fails BOTH sinks
        # and the trace is lost to a log line -- see final-review C-1,
        # which changed this field from list[dict] to
        # list[StructuralDeviation].
        "structural_deviations_found": [
            deviation.model_dump() for deviation in classifier_output.structural_deviations_found
        ],
        "reasoning": classifier_output.reasoning,
        "overrides_fired": overrides_fired,
    }
    _log_run("classification", run_data, firestore_client)


def replay_pending_traces(firestore_client: Client, max_docs: int | None = None) -> int:
    """
    Reads up to `max_docs` docs in pending_traces (default: $TRACE_REPLAY_BATCH_SIZE,
    else DEFAULT_TRACE_REPLAY_BATCH_SIZE), attempts to log each to MLflow;
    on success deletes the doc, on failure leaves it for the next
    scheduled run (see main.py's POST /replay-traces). Returns the count
    successfully replayed. Each doc gets its own try/except -- one bad or
    still-failing doc doesn't block the rest of the batch.

    Bounded rather than draining the whole collection in one call --
    during an extended MLflow outage this collection can grow far larger
    than fits comfortably in memory; the next scheduled run picks up
    whatever this one didn't reach. See docs/Code-review-Aug25.md 2.2.
    """
    if max_docs is None:
        max_docs = env_int(
            TRACE_REPLAY_BATCH_SIZE_ENV_VAR, DEFAULT_TRACE_REPLAY_BATCH_SIZE, minimum=1
        )

    replayed = 0
    still_pending = 0
    docs = list(firestore_client.collection(PENDING_TRACES_COLLECTION).limit(max_docs).stream())
    for doc in docs:
        data = doc.to_dict() or {}
        run_type = data.get("run_type", "unknown")
        run_data = data.get("run_data", {})
        try:
            mlflow.set_experiment(mlflow_experiment_name())
            with mlflow.start_run(run_name=f"{run_type}_{run_data.get('identity_key')}_replayed"):
                # Same params and tag as the live path: a trace that
                # reached MLflow late, after an outage, must be as
                # queryable as one that got there first time. Writing
                # fewer here would make an outage window show up in any
                # reader as runs with blank decisions rather than as
                # runs that arrived late.
                mlflow.log_params(_summary_params(run_type, run_data))
                if "reasoning" in run_data:
                    mlflow.set_tag(
                        REASONING_TAG, _param_value(run_data["reasoning"], _MAX_TAG_LENGTH)
                    )
                mlflow.log_dict(run_data, "run_data.json")
            firestore_client.collection(PENDING_TRACES_COLLECTION).document(doc.id).delete()
            replayed += 1
        except Exception as exc:  # noqa: BLE001 — deliberate: one doc's
            # failure must not stop the rest of the batch from replaying.
            still_pending += 1
            logger.bind(doc_id=doc.id, run_type=run_type).warning(
                "Replay attempt failed, leaving doc for next run: {}", exc
            )
    logger.bind(replayed=replayed, still_pending=still_pending).info("Trace replay run complete")
    return replayed


def log_audit_trace(
    identity_key: tuple[str, ...],
    pattern_data: dict[str, Any],
    auditor_output: "AuditorOutput",
    audit_failed: bool,
    firestore_client: Client,
) -> None:
    """Logs one audit_pattern() call, success or failure alike."""
    run_data = {
        "identity_key": list(identity_key),
        "pattern_data": pattern_data,
        "action": auditor_output.action,
        "invalidated_instance_ids": auditor_output.invalidated_instance_ids,
        "concerns_found": auditor_output.concerns_found,
        "reasoning": auditor_output.reasoning,
        "audit_failed": audit_failed,
    }
    _log_run("audit", run_data, firestore_client)
