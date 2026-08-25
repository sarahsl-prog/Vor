"""
Vör — Cloud Run entrypoint.

Exposes the trigger paths from the hybrid cadence decision:
  POST /classify — event-triggered primary path. A SUPPRESS decision means
                   this pattern's identity key just matched an incoming
                   alert again — exactly the trigger condition the auditor
                   was designed around. Enqueues the audit onto Cloud
                   Tasks so it runs in its own fully-CPU-allocated request
                   rather than as in-process background work.
  POST /sweep    — scheduled safety-net path, invoked by Cloud Scheduler
                   for the quiet, low-volume patterns event-triggering
                   would otherwise never revisit. Enqueues one audit task
                   per selected target and returns immediately.
  POST /audit    — the only place audit_pattern() actually runs. Reached
                   exclusively via Cloud Tasks (OIDC-authenticated), never
                   called directly by /classify or /sweep.
  POST /replay-traces — scheduled path, invoked by Cloud Scheduler to
                   drain vor_agents.tracing's pending_traces fallback
                   queue back into MLflow.
  POST /blast-radius/commit — human-triggered path. Commits a pending
                   MEDIUM/LOW blast-radius proposal into the live table
                   (see vor_agents/blast_radius.py's
                   commit_blast_radius_proposal()).
  GET  /healthz  — Cloud Run health check

See DEPLOY.md for how this actually gets deployed and secured.
"""

import base64
import binascii
import json
import os
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from google.cloud import firestore, tasks_v2
from loguru import logger
from pydantic import ValidationError as PydanticValidationError

from vor_agents.blast_radius import (
    ProposalAlreadyResolvedError,
    ProposalNotFoundError,
    commit_blast_radius_proposal,
)
from vor_agents.identity import MalformedAlertError
from vor_agents.orchestrator import audit_pattern, classify_alert, run_scheduled_sweep
from vor_agents.schemas import (
    AuditRequest,
    BlastRadiusCommitRequest,
    ClassifierRequest,
    PubSubPushEnvelope,
)
from vor_agents.task_queue import AuditEnqueueError, enqueue_audit
from vor_agents.tracing import replay_pending_traces

app = FastAPI(title="Vör")
_firestore_client = None
_tasks_client = None


def get_firestore_client() -> firestore.Client:
    # Lazy singleton — avoids paying Firestore client init cost on every
    # cold start path that doesn't need it (e.g. /healthz).
    global _firestore_client
    if _firestore_client is None:
        _firestore_client = firestore.Client()
    return _firestore_client


def get_tasks_client() -> tasks_v2.CloudTasksClient:
    # Lazy singleton, same shape as get_firestore_client().
    global _tasks_client
    if _tasks_client is None:
        _tasks_client = tasks_v2.CloudTasksClient()
    return _tasks_client


def _queue_path() -> str:
    return str(
        get_tasks_client().queue_path(
            os.environ["GCP_PROJECT"], os.environ["TASKS_LOCATION"], os.environ["TASKS_QUEUE"]
        )
    )


def _audit_url() -> str:
    return f"{os.environ['SERVICE_URL']}/audit"


def _enqueue(identity_key: tuple[str, ...], pattern_data: dict[str, Any]) -> bool:
    """
    Shared enqueue path for both /classify and /sweep (passed into
    run_scheduled_sweep as its enqueue_audit_fn). Absorbs AuditEnqueueError
    and KeyError (a missing TASK_ENV var) so a bad enqueue or a deploy
    misconfiguration never fails the caller's own response; it's logged
    and treated as "not enqueued" (False) so callers can still react to
    that if they care (today, neither does).

    Not an absolute guarantee: credential errors from get_tasks_client()
    (e.g. no ADC in the runtime environment) are not caught here, since
    they signal a broken deployment rather than a per-request condition
    worth silently swallowing.
    """
    try:
        return enqueue_audit(
            identity_key,
            pattern_data,
            get_tasks_client(),
            _queue_path(),
            _audit_url(),
            os.environ["TASKS_OIDC_SA_EMAIL"],
        )
    except (AuditEnqueueError, KeyError) as exc:
        logger.bind(identity_key=identity_key).error(
            "Audit enqueue failed ({}); caller's response is unaffected", repr(exc)
        )
        return False


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


def _decode_classify_body(raw_body: bytes) -> dict[str, Any]:
    """
    Detects whether /classify's raw request body is a Pub/Sub push
    envelope ({"message": {"data": base64}, "subscription": ...}) or a
    raw alert JSON body (direct/test callers). Returns the alert dict
    either way -- NOT yet validated against ClassifierRequest, the
    /classify handler does that next with the same model either path
    took before this change.

    Detection IS validation: a body counts as an envelope only if it
    successfully parses as PubSubPushEnvelope, which requires both
    message.data AND subscription (a field Pub/Sub push always
    includes). Requiring subscription is what stops a legitimate alert
    that happens to carry its own top-level `message: {data: ...}` field
    (e.g. Windows Event Log records commonly do) from being misread as
    an envelope -- anything that doesn't validate as an envelope falls
    straight through to the raw-alert path below.

    Raises ValueError on invalid JSON -- including invalid UTF-8 bytes,
    since json.loads() raises UnicodeDecodeError (a ValueError subclass)
    for those rather than JSONDecodeError -- a non-object body, or a
    malformed envelope (invalid base64, or base64 content that isn't a
    JSON object once decoded). /classify's handler turns this into a
    422, same as every other malformed-input case in this codebase.
    """
    try:
        body = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"Request body is not valid JSON: {exc}") from exc

    try:
        envelope: PubSubPushEnvelope | None = PubSubPushEnvelope.model_validate(body)
    except PydanticValidationError:
        envelope = None

    if envelope is not None:
        try:
            decoded = base64.b64decode(envelope.message.data, validate=True)
            alert = json.loads(decoded)
        except (binascii.Error, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"Malformed Pub/Sub push envelope: {exc}") from exc
        if not isinstance(alert, dict):
            raise ValueError("Decoded Pub/Sub message.data is not a JSON object")
        return alert

    if not isinstance(body, dict):
        # ValueError, not TypeError: the caller catches ValueError
        # alongside PydanticValidationError to turn every malformed-body
        # case into a 422, same as the other raises in this function.
        raise ValueError("Request body must be a JSON object")  # noqa: TRY004
    return body


def _classify_error_context(raw_body: bytes) -> dict[str, Any]:
    """Best-effort context for the WARNING logged when /classify rejects
    a request body. Never raises -- this runs on an already-failing
    path, so it re-parses raw_body defensively rather than reusing
    anything from _decode_classify_body()'s own (already-failed) attempt.
    Reports which shape was detected (envelope-like vs raw vs
    unparseable/non-object) and, for an envelope-like body, messageId /
    subscription when present -- both non-sensitive, unlike alert
    content, which is deliberately not logged here."""
    try:
        body = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"shape": "unparseable"}
    if not isinstance(body, dict):
        return {"shape": "non-object"}
    message = body.get("message")
    if isinstance(message, dict) and "data" in message:
        return {
            "shape": "envelope-like",
            "message_id": message.get("messageId"),
            "subscription": body.get("subscription"),
        }
    return {"shape": "raw-alert"}


@app.post("/classify")
async def classify(request: Request) -> dict[str, Any]:
    """
    Accepts either a raw alert JSON body (direct/test callers) or a
    Pub/Sub push envelope (a push subscription calling this endpoint --
    see docs/superpowers/specs/2026-08-24-pubsub-classify-trigger-design.md).
    Either shape is unwrapped to a plain alert dict by
    _decode_classify_body(), then validated against ClassifierRequest --
    a missing identity field or malformed body returns 422 either way,
    same guarantee the previous typed-body-param version gave, just with
    the validation call made explicitly instead of by FastAPI's own
    body-parsing layer.
    """
    raw_body = await request.body()
    try:
        alert_body = _decode_classify_body(raw_body)
        payload = ClassifierRequest.model_validate(alert_body)
    except (ValueError, PydanticValidationError) as exc:
        logger.bind(**_classify_error_context(raw_body)).warning(
            "Rejected /classify request body: {}", exc
        )
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    alert = payload.model_dump()
    client = get_firestore_client()
    result, identity_key = await classify_alert(alert, client)

    if result.decision == "SUPPRESS":
        _enqueue(identity_key, {"triggered_by": "classify_suppress"})

    return result.model_dump()


@app.post("/sweep")
async def sweep(request: Request) -> dict[str, int]:
    """
    Cloud Scheduler hits this on a weekly cadence (see DEPLOY.md). The
    scheduler job is configured with an OIDC token bound to a service
    account with run.invoker on this service — Cloud Run itself rejects
    unauthenticated requests, so no manual auth check is needed here. Do
    NOT deploy this endpoint with --allow-unauthenticated in production.
    """
    client = get_firestore_client()
    enqueued = run_scheduled_sweep(client, _enqueue)
    return {"enqueued": len(enqueued)}


@app.post("/audit")
async def audit(payload: AuditRequest) -> dict[str, Any]:
    """
    Reached exclusively via a Cloud Tasks dispatch — never called
    directly by /classify or /sweep. This is the one place
    audit_pattern() actually runs, inside its own fully-CPU-allocated
    request. Gated by Cloud Run IAM the same way /sweep is (OIDC,
    never --allow-unauthenticated) — no manual token check needed here.

    payload is validated by FastAPI/pydantic before this body runs — a
    malformed or truncated body (Cloud Tasks retrying a stale/corrupted
    task, or a bug in task_queue.py's construction) returns 422 rather
    than an unhandled KeyError-turned-500, so it fails predictably
    instead of burning Cloud Tasks' retry budget on a payload that can
    never succeed.

    audit_pattern() itself already degrades model/parsing failures to a
    logged NO_ACTION decision (see orchestrator.py's try/except/finally)
    rather than raising. clear_under_review()'s own read of the current
    failure_count (in its finally block, on every call) is ALSO handled
    internally now — a read failure there returns a -1 sentinel rather
    than raising, so it can't re-strand under_review=True either. What's
    still NOT covered: mark_under_review() (before the try block) and the
    invalidate_instances() rebuild inside clear_under_review()'s DOWNGRADE
    branch (also in the finally block) can still raise out of this call on
    a Firestore write failure or malformed stored evidence. Split those on
    the same "retryable vs permanent" line Cloud Tasks itself cares about:
    a transient Firestore/network error is exactly what its retry budget
    exists for (500, let it retry); malformed data already in Firestore
    will fail identically on every retry (422, correctness bug for a
    human to fix, not something retrying helps).
    """
    client = get_firestore_client()
    try:
        decision = await audit_pattern(tuple(payload.identity_key), payload.pattern_data, client)
    except MalformedAlertError as exc:
        logger.bind(identity_key=payload.identity_key).error(
            "Audit failed on malformed stored data: {}", exc
        )
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        # Catch-all deliberate: any other failure here (Firestore
        # unavailable, network) is the retryable direction. Logged with
        # full context and re-raised as an explicit 500 rather than left
        # to propagate as FastAPI's generic unhandled-exception response,
        # so it's visible in logs with identity_key attached instead of
        # just a stack trace.
        logger.bind(identity_key=payload.identity_key).exception(
            "Audit endpoint failed unexpectedly"
        )
        raise HTTPException(status_code=500, detail="Audit failed, will be retried") from exc
    return decision.model_dump()


@app.post("/blast-radius/commit")
async def blast_radius_commit(payload: BlastRadiusCommitRequest) -> dict[str, Any]:
    """
    Human-triggered commit for a pending MEDIUM/LOW blast-radius
    proposal (see vor_agents/blast_radius.py's
    commit_blast_radius_proposal()). Gated the same way /audit is --
    Cloud Run IAM, OIDC-authenticated caller, never
    --allow-unauthenticated in production -- but unlike /audit this is
    meant to be called by a human (via `gcloud run services proxy` +
    curl, or a small authenticated script), not a machine dispatcher.
    """
    client = get_firestore_client()
    try:
        proposal = commit_blast_radius_proposal(payload.proposal_id, client)
    except ProposalNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ProposalAlreadyResolvedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return proposal


@app.post("/replay-traces")
async def replay_traces(request: Request) -> dict[str, int]:
    """
    Cloud Scheduler hits this every 15 minutes (see DEPLOY.md) to drain
    vor_agents.tracing's pending_traces fallback queue -- traces that
    failed to log directly to MLflow (server unreachable at the time)
    get retried here. OIDC-gated the same way /sweep is -- Cloud Run
    itself rejects unauthenticated requests, no manual auth check needed.
    """
    client = get_firestore_client()
    replayed = replay_pending_traces(client)
    return {"replayed": replayed}
