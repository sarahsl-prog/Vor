"""
Vör — Orchestrator. This is the only file that actually calls the ADK
Runner. Everything else is either deterministic (identity.py, enrichment.py,
review_flag.py) or a pure agent definition (classifier_agent.py,
auditor_agent.py) with no orchestration logic of its own.
"""

import json
import uuid
from datetime import datetime, timezone

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai.types import Content, Part
from loguru import logger

from .audit_targets import select_audit_targets
from .auditor_agent import build_auditor_agent
from .blast_radius import estimate_blast_radius
from .classifier_agent import build_classifier_agent
from .enrichment import CONFIDENCE_COLLECTION, _doc_id, enrich
from .evidence_diversity import evidence_diversity_score
from .identity import diff_alert_against_template
from .review_flag import clear_under_review, mark_under_review
from .schemas import AuditorOutput, ClassifierOutput, Decision, UncertainReason

session_service = InMemorySessionService()  # swap for a persistent
                                             # SessionService in production;
                                             # fine for a hackathon demo


class AgentOutputError(Exception):
    """
    Raised when _run_agent() can't turn a model response into a dict —
    empty output, Markdown code fences, truncated JSON, or any other
    non-JSON text. Wraps the underlying json.JSONDecodeError so callers
    (classify_alert, audit_pattern) never see a raw stdlib exception, same
    "never surface raw exceptions" standard as MalformedAlertError in
    identity.py and AuditEnqueueError in task_queue.py.
    """


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
                # getattr (not direct attribute access) so mypy sees an
                # Any here rather than the declared str | None on
                # part.text — the truthiness check already guarantees
                # non-None/non-empty, but mypy can't narrow a separate
                # attribute access through that guard.
                text = getattr(part, "text", None)
                if text:
                    result_text += text
    try:
        return json.loads(result_text)
    except json.JSONDecodeError as exc:
        raise AgentOutputError(
            f"Model did not return valid JSON (length={len(result_text)}): {exc}"
        ) from exc


def _deviation_field_names(deviation_strings: list[str]) -> set[str]:
    """
    Extracts just the field name from each deviation string (format:
    "field_name: template=X, observed=Y" — see diff_alert_against_template()
    and the schema description for structural_deviations_found). Comparing
    by field name rather than exact string match is deliberate: the model
    is asked to follow this format but isn't guaranteed to phrase the
    template/observed values identically to the Python-computed version
    (repr formatting, quoting, etc.) — field name is the part that actually
    matters for reconciliation, not incidental text differences.
    """
    return {d.split(":", 1)[0].strip() for d in deviation_strings if d}


async def classify_alert(alert: dict, firestore_client) -> tuple[ClassifierOutput, tuple]:
    """
    Full classification path for one incoming alert:
      1. Deterministic enrichment (Firestore read + aggregation, no LLM)
      2. Deterministic diff pre-computation, reconciled against the
         model's own reported deviations AFTER the agent call (see below)
         — this is no longer just a computed-but-unused hook.
      3. Classifier agent call with enrichment serialized into the prompt

    Reconciliation is asymmetric, same shape as every other trust decision
    in this system:
      - If the deterministic diff found a deviation the model did NOT
        report, and the model still said SUPPRESS, that's the dangerous
        direction — the model missed something real. The decision is
        overridden to ESCALATE; this never happens silently, the override
        is recorded in the returned reasoning text.
      - If the model reported a deviation the deterministic diff did NOT
        find, that's the model being more cautious than ground truth —
        the safe direction. No override; the model's decision stands.

    Returns (result, identity_key) — NOT just the ClassifierOutput. The
    identity_key returned here is the one computed deterministically in
    enrich(), not anything parsed from the model's own
    matched_pattern_id text. A caller (e.g. the Cloud Run /classify
    endpoint) that wants to trigger an audit off a SUPPRESS decision needs
    a real identity key to hand to audit_pattern() — reconstructing one by
    parsing LLM-generated text would repeat the same fragile-split problem
    already flagged for _fetch_all_suppressed_patterns(), just one layer
    less reliable since it would also depend on the model formatting that
    string consistently.
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
    # session_id is unique per call (uuid4), not just per identity_key: the
    # classifier is meant to be stateless per-call (see enrich()'s
    # docstring — "reasons over exactly this, nothing more"). Reusing a
    # session_id across repeat calls for the same recurring pattern would
    # let ADK accumulate conversation history into the same session,
    # silently biasing later calls with earlier turns. identity_key is
    # kept as a log-correlation prefix only, not as the dedup key.
    try:
        result = await _run_agent(
            classifier, prompt, session_id=f"classify_{'_'.join(identity_key)}_{uuid.uuid4()}"
        )
        classifier_output = ClassifierOutput.model_validate(result)
    except AgentOutputError as exc:
        # Model returned unparseable output (empty, Markdown-fenced,
        # truncated JSON). This must not surface as a 500 to the /classify
        # caller — degrade to UNCERTAIN, same "force a safe outcome in
        # code" principle as every other trust decision in this system.
        # No reconciliation to run below: there's no classifier_output to
        # reconcile against.
        logger.bind(identity_key=identity_key).error("Classifier output unparseable: {}", exc)
        return (
            ClassifierOutput(
                decision=Decision.UNCERTAIN,
                matched_pattern_id=None,
                uncertain_reason=UncertainReason.MISSING_DATA,
                structural_deviations_found=[],
                reasoning=f"Classifier returned unparseable output: {exc}",
            ),
            identity_key,
        )

    if precomputed_deviations:
        precomputed_fields = _deviation_field_names(precomputed_deviations)
        reported_fields = _deviation_field_names(classifier_output.structural_deviations_found)
        missed_by_model = precomputed_fields - reported_fields

        if missed_by_model and classifier_output.decision == "SUPPRESS":
            # Dangerous direction — ground truth found a real deviation
            # the model didn't report, yet it still said SUPPRESS. Never
            # trust that; override deterministically, don't ask the model
            # to reconsider (same "when in doubt, force the safe outcome
            # in code, not via another LLM call" principle used for
            # NO_HISTORY and under_review elsewhere in this design).
            classifier_output = classifier_output.model_copy(update={
                "decision": "ESCALATE",
                "structural_deviations_found": sorted(
                    set(classifier_output.structural_deviations_found) | set(precomputed_deviations)
                ),
                "reasoning": (
                    classifier_output.reasoning
                    + f" [Vör correctness override: deterministic diff found "
                    f"deviation(s) in {sorted(missed_by_model)} that the model "
                    f"did not report or act on; SUPPRESS overridden to ESCALATE.]"
                ),
            })
        # The reverse case (model reported a deviation ground truth didn't
        # find) is deliberately NOT overridden — the model being more
        # cautious than the deterministic check is the safe direction and
        # doesn't need correction, just isn't flagged as an error here.

    if classifier_output.structural_deviations_found and classifier_output.decision == "SUPPRESS":
        # Self-consistency override, independent of the ground-truth
        # reconciliation above (and of whether it even ran — this fires
        # even when precomputed_deviations is empty). The classifier's own
        # prompt (rule 4) requires any non-empty structural_deviations_found
        # to force ESCALATE; nothing previously enforced that internally,
        # so a model reporting a deviation (real or hallucinated) while
        # still emitting SUPPRESS sailed through untouched whenever that
        # deviation wasn't also in the deterministic diff. Same "force the
        # safe outcome in code" principle as the check above — a model
        # contradicting its own stated rule can't be trusted regardless of
        # what ground truth separately found.
        classifier_output = classifier_output.model_copy(update={
            "decision": "ESCALATE",
            "reasoning": (
                classifier_output.reasoning
                + " [Vör correctness override: model reported structural "
                "deviation(s) but decision was SUPPRESS, contradicting its "
                "own classification rule that any deviation forces "
                "ESCALATE; overridden to ESCALATE.]"
            ),
        })

    return classifier_output, identity_key


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
    # Same unique-session-per-call rationale as classify_alert() above —
    # each audit is a fresh, independent review, not a continuation of a
    # prior one for this pattern.
    result = await _run_agent(
        auditor, prompt, session_id=f"audit_{'_'.join(identity_key)}_{uuid.uuid4()}"
    )
    decision = AuditorOutput.model_validate(result)

    clear_under_review(identity_key, firestore_client, decision.model_dump())
    return decision


def run_scheduled_sweep(
    firestore_client, enqueue_audit_fn, max_targets: int = 10
) -> list[tuple]:
    """
    Safety-net path — invoked on a timer (e.g. weekly Cloud Scheduler job
    hitting a Cloud Run endpoint that calls this function). Reuses the
    same select_audit_targets() priority scoring the event-triggered path
    would use if it fired for these patterns, which it may never do for
    quiet, low-volume ones — that's the coverage gap this sweep exists to
    close.

    No longer runs audits itself — enqueues one Cloud Tasks task per
    selected target via enqueue_audit_fn (identity_key, pattern_data) ->
    bool, and returns the identity_keys that were newly enqueued (a
    dedup hit — the pattern already has an audit in flight — returns
    False from enqueue_audit_fn and is excluded from the result). The
    actual audit runs later, in its own request, when Cloud Tasks
    dispatches to POST /audit — see task_queue.py and main.py.

    enqueue_audit_fn is dependency-injected rather than imported and
    called directly: this function doesn't need to know Cloud Tasks
    config specifics (queue path, audit URL, OIDC service account) any
    more than it needs to know how firestore_client was constructed.
    """
    all_suppressed = _fetch_all_suppressed_patterns(firestore_client)
    targets = select_audit_targets(all_suppressed, max_targets=max_targets)

    enqueued = []
    for pattern in targets:
        was_new = enqueue_audit_fn(pattern["identity_key"], pattern)
        if was_new:
            enqueued.append(pattern["identity_key"])
    return enqueued


def _fetch_all_suppressed_patterns(firestore_client) -> list[dict]:
    """
    Queries CONFIDENCE_COLLECTION for tier == "confirmed" docs and shapes
    them into what select_audit_targets() expects: days_since_last_review,
    evidence_diversity_score, blast_radius_estimate, identity_key.

    days_since_last_review: computed from last_reviewed_at (stamped by
    clear_under_review() on every audit, not just downgrades). A pattern
    that has NEVER been audited gets a large sentinel value (9999) rather
    than 0 — same "unassessed defaults to needs-attention" principle used
    for blast_radius_estimate's UNSCORED_DEFAULT. A freshly-graduated
    pattern that's never once been checked by the auditor should not look
    lower-priority than one reviewed yesterday.

    evidence_diversity_score / blast_radius_estimate: computed fresh from
    stored confirmed_instances on every call rather than cached on the doc
    — both are cheap pure functions, and recomputing avoids the staleness
    risk of a cached score surviving past an invalidate_instances() call
    that changed the underlying evidence.
    """
    docs = firestore_client.collection(CONFIDENCE_COLLECTION).where(
        "tier", "==", "confirmed"
    ).stream()

    patterns = []
    for doc in docs:
        data = doc.to_dict()
        instances = data.get("confirmed_instances", [])
        if not instances:
            continue  # confirmed tier with no instances shouldn't occur, but skip defensively

        last_reviewed_at = data.get("last_reviewed_at")
        if last_reviewed_at:
            reviewed_dt = datetime.fromisoformat(last_reviewed_at)
            days_since = (datetime.now(timezone.utc) - reviewed_dt).days
        else:
            days_since = 9999  # never audited — treat as maximally stale

        patterns.append({
            "identity_key": tuple(doc.id.split("_")),
            "days_since_last_review": days_since,
            "evidence_diversity_score": evidence_diversity_score(instances),
            # Worst case across ALL instances, not just the first —
            # different confirmed instances of the same pattern can carry
            # different indicator values (e.g. different hosts with
            # different privilege contexts), and blast radius is
            # deliberately a worst-case estimate throughout this design.
            "blast_radius_estimate": max(
                estimate_blast_radius(instance) for instance in instances
            ),
        })
    return patterns
