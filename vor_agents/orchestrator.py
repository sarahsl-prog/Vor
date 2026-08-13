"""
Vör — Orchestrator. This is the only file that actually calls the ADK
Runner. Everything else is either deterministic (identity.py, enrichment.py,
review_flag.py) or a pure agent definition (classifier_agent.py,
auditor_agent.py) with no orchestration logic of its own.
"""

import asyncio
import json
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai.types import Content, Part

from .classifier_agent import build_classifier_agent
from .auditor_agent import build_auditor_agent
from .enrichment import enrich, CONFIDENCE_COLLECTION, _doc_id
from .identity import diff_alert_against_template
from .review_flag import mark_under_review, clear_under_review
from .audit_targets import select_audit_targets
from .schemas import ClassifierOutput, AuditorOutput

session_service = InMemorySessionService()  # swap for a persistent
                                             # SessionService in production;
                                             # fine for a hackathon demo


async def _run_agent(agent, prompt_text: str, session_id: str) -> dict:
    """Shared runner plumbing — both agents call through this."""
    runner = Runner(
        agent=agent,
        session_service=session_service,
        app_name="vor",
        auto_create_session=True,
    )
    msg = Content(role="user", parts=[Part(text=prompt_text)])
    result_text = ""
    async for event in runner.run_async(
        user_id="vor-system", session_id=session_id, new_message=msg
    ):
        if event.content and event.content.parts:
            for part in event.content.parts:
                if getattr(part, "text", None):
                    result_text += part.text
    return json.loads(result_text)


async def classify_alert(alert: dict, firestore_client) -> ClassifierOutput:
    """
    Full classification path for one incoming alert:
      1. Deterministic enrichment (Firestore read + aggregation, no LLM)
      2. Deterministic diff pre-computation — NOTE: this is a deliberate
         belt-and-suspenders choice. We diff in Python here AND ask the
         model to diff in its reasoning. The model's output is what's
         authoritative (it decides SUPPRESS/ESCALATE/UNCERTAIN), but
         having the Python-computed deviation list available to compare
         against the model's own list is a cheap correctness check worth
         adding once this moves past hackathon-demo stage — flagging the
         hook here rather than building the comparison logic itself yet.
      3. Classifier agent call with enrichment serialized into the prompt
    """
    enrichment = enrich(alert, firestore_client)

    precomputed_deviations = []
    if enrichment["status"] == "TEMPLATE":
        precomputed_deviations = diff_alert_against_template(
            alert, enrichment["fields"]
        )

    prompt = (
        f"Alert:\n{json.dumps(alert, indent=2)}\n\n"
        f"Enrichment context:\n{json.dumps(enrichment, indent=2)}\n\n"
        "Classify this alert per your instructions."
    )

    classifier = build_classifier_agent()
    identity_key = enrichment.get("pattern_identity_key", ("unknown",))
    result = await _run_agent(
        classifier, prompt, session_id=f"classify_{'_'.join(identity_key)}"
    )
    return ClassifierOutput.model_validate(result)


async def audit_pattern(
    identity_key: tuple, pattern_data: dict, firestore_client
) -> AuditorOutput:
    """
    Full audit path for one flagged pattern:
      1. mark_under_review() — synchronous, BEFORE any LLM call, closes
         the burst-replay race window immediately
      2. Auditor agent call, separate context from the classifier
      3. clear_under_review() atomically with the recorded decision
    """
    mark_under_review(identity_key, firestore_client)

    # Fetch the full confirmed_instances list directly rather than trusting
    # pattern_data to already contain it — callers like run_scheduled_sweep
    # only pass summary fields (days_since_last_review, blast_radius, etc.)
    # from select_audit_targets(), and the auditor prompt now depends on
    # seeing every instance_id to be able to cite one.
    doc = firestore_client.collection(CONFIDENCE_COLLECTION).document(
        _doc_id(identity_key)
    ).get()
    confirmed_instances = doc.to_dict().get("confirmed_instances", []) if doc.exists else []

    prompt = (
        f"Pattern under review:\n{json.dumps(pattern_data, indent=2)}\n\n"
        f"Confirmed instances (cite instance_id values from this list only "
        f"if downgrading):\n{json.dumps(confirmed_instances, indent=2)}\n\n"
        "Review this suppression decision per your instructions."
    )
    auditor = build_auditor_agent()
    result = await _run_agent(
        auditor, prompt, session_id=f"audit_{'_'.join(identity_key)}"
    )
    decision = AuditorOutput.model_validate(result)

    clear_under_review(identity_key, firestore_client, decision.model_dump())
    return decision


async def run_scheduled_sweep(firestore_client, max_targets: int = 10) -> list:
    """
    Safety-net path — invoked on a timer (e.g. weekly Cloud Scheduler job
    hitting a Cloud Run endpoint that calls this function). Reuses the
    same select_audit_targets() priority scoring the event-triggered path
    would use if it fired for these patterns, which it may never do for
    quiet, low-volume ones — that's the coverage gap this sweep exists to
    close.
    """
    all_suppressed = _fetch_all_suppressed_patterns(firestore_client)
    targets = select_audit_targets(all_suppressed, max_targets=max_targets)

    results = []
    for pattern in targets:
        decision = await audit_pattern(
            pattern["identity_key"], pattern, firestore_client
        )
        results.append((pattern["identity_key"], decision))
    return results


def _fetch_all_suppressed_patterns(firestore_client) -> list[dict]:
    """
    Placeholder — real implementation queries CONFIDENCE_COLLECTION for
    tier == "confirmed" docs and shapes them into the dict format
    select_audit_targets() expects (days_since_last_review,
    evidence_diversity_score, blast_radius_estimate, identity_key).
    Left unimplemented here since it's a straight Firestore query, not a
    design decision — fill in against your actual schema once Firestore
    is provisioned.
    """
    raise NotImplementedError(
        "Wire this to a real Firestore query once the confidence_docs "
        "collection has data — see CONFIDENCE_COLLECTION in enrichment.py"
    )
