"""
Vör — Cloud Run entrypoint.

Exposes the two trigger paths from the hybrid cadence decision:
  POST /classify — event-triggered primary path. A SUPPRESS decision means
                   this pattern's identity key just matched an incoming
                   alert again — exactly the trigger condition the auditor
                   was designed around. Fires the audit as a background
                   task so the classify response isn't held up waiting on
                   a second LLM call.
  POST /sweep    — scheduled safety-net path, invoked by Cloud Scheduler
                   for the quiet, low-volume patterns event-triggering
                   would otherwise never revisit.
  GET  /healthz  — Cloud Run health check

See DEPLOY.md for how this actually gets deployed and secured.
"""

from fastapi import BackgroundTasks, FastAPI, Request
from google.cloud import firestore

from vor_agents.enrichment import CONFIDENCE_COLLECTION, _doc_id
from vor_agents.orchestrator import audit_pattern, classify_alert, run_scheduled_sweep

app = FastAPI(title="Vör")
_firestore_client = None


def get_firestore_client():
    # Lazy singleton — avoids paying Firestore client init cost on every
    # cold start path that doesn't need it (e.g. /healthz).
    global _firestore_client
    if _firestore_client is None:
        _firestore_client = firestore.Client()
    return _firestore_client


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.post("/classify")
async def classify(request: Request, background_tasks: BackgroundTasks):
    alert = await request.json()
    client = get_firestore_client()
    result, identity_key = await classify_alert(alert, client)

    if result.decision == "SUPPRESS":
        # Guard against firing a second concurrent audit on a pattern
        # that's already mid-review — a burst of the same SUPPRESS-eligible
        # pattern arriving faster than one audit completes would otherwise
        # schedule duplicate auditor LLM calls for the same identity_key.
        # Cheap read, no LLM involved, same "check before acting" shape as
        # everything else deterministic in this design.
        doc = client.collection(CONFIDENCE_COLLECTION).document(
            _doc_id(identity_key)
        ).get()
        already_under_review = doc.exists and doc.to_dict().get("under_review", False)

        if not already_under_review:
            background_tasks.add_task(
                audit_pattern,
                identity_key,
                {"triggered_by": "classify_suppress"},
                client,
            )

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
    results = await run_scheduled_sweep(client)
    return {"audited_count": len(results)}
