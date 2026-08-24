"""
Vör — MLflow tracing for the two LLM-calling entry points
(classify_alert(), audit_pattern()). Best-effort: an MLflow-logging
failure never fails the caller's own request. On a failure, the run data
is queued to a durable Firestore fallback (pending_traces) instead of
being dropped, and replayed later by replay_pending_traces() -- see
docs/superpowers/specs/2026-08-24-mlflow-tracing-design.md.
"""

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import mlflow
from google.cloud.firestore import Client
from loguru import logger

if TYPE_CHECKING:
    from .schemas import AuditorOutput, ClassifierOutput

PENDING_TRACES_COLLECTION = "pending_traces"


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
        with mlflow.start_run(run_name=f"{run_type}_{run_data.get('identity_key')}"):
            mlflow.log_params(
                {"run_type": run_type, "identity_key": str(run_data.get("identity_key"))}
            )
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
        "structural_deviations_found": classifier_output.structural_deviations_found,
        "reasoning": classifier_output.reasoning,
        "overrides_fired": overrides_fired,
    }
    _log_run("classification", run_data, firestore_client)


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
