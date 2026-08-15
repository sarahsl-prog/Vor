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
  GET  /healthz  — Cloud Run health check

See DEPLOY.md for how this actually gets deployed and secured.
"""

import os

from fastapi import FastAPI, HTTPException, Request
from google.cloud import firestore, tasks_v2
from loguru import logger

from vor_agents.identity import MalformedAlertError
from vor_agents.orchestrator import audit_pattern, classify_alert, run_scheduled_sweep
from vor_agents.schemas import AuditRequest, ClassifierRequest
from vor_agents.task_queue import AuditEnqueueError, enqueue_audit

app = FastAPI(title="Vör")
_firestore_client = None
_tasks_client = None


def get_firestore_client():
    # Lazy singleton — avoids paying Firestore client init cost on every
    # cold start path that doesn't need it (e.g. /healthz).
    global _firestore_client
    if _firestore_client is None:
        _firestore_client = firestore.Client()
    return _firestore_client


def get_tasks_client():
    # Lazy singleton, same shape as get_firestore_client().
    global _tasks_client
    if _tasks_client is None:
        _tasks_client = tasks_v2.CloudTasksClient()
    return _tasks_client


def _queue_path() -> str:
    return get_tasks_client().queue_path(
        os.environ["GCP_PROJECT"], os.environ["TASKS_LOCATION"], os.environ["TASKS_QUEUE"]
    )


def _audit_url() -> str:
    return f"{os.environ['SERVICE_URL']}/audit"


def _enqueue(identity_key: tuple, pattern_data: dict) -> bool:
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
async def healthz():
    return {"status": "ok"}


@app.post("/classify")
async def classify(payload: ClassifierRequest):
    """
    payload is validated by FastAPI/pydantic before this body runs, same
    pattern as /audit below — a missing identity field (previously a raw
    KeyError-turned-500 out of pattern_identity_key()) or an invalid JSON
    body (previously an unhandled JSONDecodeError) both return 422 instead.
    """
    alert = payload.model_dump()
    client = get_firestore_client()
    result, identity_key = await classify_alert(alert, client)

    if result.decision == "SUPPRESS":
        _enqueue(identity_key, {"triggered_by": "classify_suppress"})

    return result.model_dump()


@app.post("/sweep")
async def sweep(request: Request):
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
async def audit(payload: AuditRequest):
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
    rather than raising — but mark_under_review() (before that try block)
    and the invalidate_instances() rebuild inside clear_under_review()
    (in its finally block) are NOT covered by it, so a Firestore write
    failure or malformed stored evidence can still raise out of this call.
    Split those on the same "retryable vs permanent" line Cloud Tasks
    itself cares about: a transient Firestore/network error is exactly
    what its retry budget exists for (500, let it retry); malformed data
    already in Firestore will fail identically on every retry (422,
    correctness bug for a human to fix, not something retrying helps).
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
