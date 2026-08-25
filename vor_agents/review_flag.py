"""
Vör — under_review flag lifecycle. Closes the burst-replay race condition.

Deliberately NOT an LLM decision — synchronous Firestore boolean writes,
same design vocabulary as the NO_HISTORY / tier checks in the classifier
prompt: when in doubt, force a deterministic degrade rather than trusting
a model to notice the ambiguity itself.
"""

from datetime import UTC, datetime
from typing import Any

from google.cloud.firestore import Client
from loguru import logger

from .enrichment import CONFIDENCE_COLLECTION, _doc_id, invalidate_instances

NEEDS_ATTENTION_COLLECTION = "needs_attention"


def mark_under_review(pattern_identity_key: tuple[str, ...], firestore_client: Client) -> None:
    """
    Called by the orchestrator as the FIRST action when an audit is
    triggered for a pattern — before the auditor agent is invoked at all.
    Cheap, synchronous, blocks autonomous SUPPRESS on this exact pattern
    until cleared.
    """
    doc_ref = firestore_client.collection(CONFIDENCE_COLLECTION).document(
        _doc_id(pattern_identity_key)
    )
    doc_ref.set({"under_review": True}, merge=True)


def clear_under_review(
    pattern_identity_key: tuple[str, ...],
    firestore_client: Client,
    auditor_decision: dict[str, Any],
    audit_failed: bool = False,
) -> int:
    """
    Called as part of the SAME write that records the auditor's decision
    (DOWNGRADE, RECOMMEND_UPGRADE_FOR_HUMAN_REVIEW, or NO_ACTION) —
    under_review is cleared atomically with the outcome, never in a
    separate write, so there's no window where the flag is false but the
    confidence data hasn't landed yet.

    Stamps last_reviewed_at on every call EXCEPT a failed audit
    (audit_failed=True) — a successful audit (including a plain NO_ACTION
    that found nothing wrong) is evidence the pattern was actually looked
    at, which is exactly what select_audit_targets() needs to know to
    stop re-prioritizing it every sweep. A *failed* audit is the opposite:
    the pattern was never actually re-verified, no matter how many times
    the flag got cleared, so stamping it here would make
    select_audit_targets() rank a repeatedly-failing pattern as
    freshly-reviewed and bury it at the bottom of sweep priority — the
    exact opposite of what this feature exists to do. Skipping the stamp
    on failure is a deliberate, intentional behavior change (see
    docs/superpowers/specs/2026-08-24-audit-failure-escalation-design.md):
    every failed audit now actively pushes the pattern's sweep priority
    up as it grows staler, instead of leaving it neutral.

    audit_failed tracks failure_count: incremented on a failed audit,
    reset to 0 on a successful one. Read-then-write, not
    firestore.Increment — see this plan/spec's rationale; audits for the
    same pattern are already serialized in the common case, so the small
    race window is accepted. Returns the resulting failure_count so
    callers (audit_pattern) can decide whether to escalate without a
    second Firestore read.

    The failure_count read (needed to compute the increment) runs on
    EVERY call, including from audit_pattern()'s `finally` block — that
    read failing must never raise out of `finally` and re-strand
    under_review=True, which is precisely the bug the try/finally exists
    to prevent. So the read is isolated in its own try/except: on
    failure, this function returns -1 as a sentinel meaning "count
    unknown, read failed" (there's no other way to signal that through an
    `int` return type) and OMITS failure_count from the update entirely,
    leaving whatever was previously stored untouched rather than losing
    the write altogether. audit_pattern()'s escalation check
    (`new_failure_count >= AUDIT_FAILURE_ESCALATION_THRESHOLD`) naturally
    never fires on -1, which is the safe direction: no escalation record
    gets written on top of a Firestore outage that already prevented
    reading the real count.

    DOWNGRADE resolution — targeted evidence invalidation, decided over
    the blanket "demote tier to provisional" alternative: the auditor
    cites specific confirmed_instance IDs it no longer trusts
    (auditor_decision["invalidated_instance_ids"]). Only those instances
    are removed; the template is rebuilt from whatever remains, and tier
    falls out of that rebuild rather than being force-set. A pattern with
    9 good instances and 1 bad one loses only the bad one, not its whole
    earned trust — same evidence-diversity principle already used
    everywhere else in this design, just applied to corrections too.
    """
    doc_ref = firestore_client.collection(CONFIDENCE_COLLECTION).document(
        _doc_id(pattern_identity_key)
    )

    try:
        current = doc_ref.get().to_dict() or {}
        previous_failure_count = current.get("failure_count", 0)
        read_failed = False
    except Exception as exc:  # noqa: BLE001 — deliberate catch-all: this
        # read now runs unconditionally, including from audit_pattern()'s
        # finally block (see docstring above). Any failure here must
        # degrade to "count unknown" rather than propagate.
        logger.bind(identity_key=pattern_identity_key).warning(
            "clear_under_review: failed to read current failure_count, "
            "leaving stored value untouched: {}",
            exc,
        )
        previous_failure_count = 0  # unused when read_failed is True
        read_failed = True

    update: dict[str, Any] = {"under_review": False}
    if not audit_failed:
        update["last_reviewed_at"] = datetime.now(UTC).isoformat()

    if read_failed:
        new_failure_count = -1  # sentinel: count unknown, read failed
        # failure_count deliberately omitted from `update` — see docstring.
    else:
        new_failure_count = previous_failure_count + 1 if audit_failed else 0
        update["failure_count"] = new_failure_count

    if auditor_decision["action"] == "DOWNGRADE":
        rebuild = invalidate_instances(
            pattern_identity_key,
            auditor_decision.get("invalidated_instance_ids", []),
            firestore_client,
        )
        update.update(rebuild)

    doc_ref.update(update)
    return new_failure_count


def record_needs_attention(
    pattern_identity_key: tuple[str, ...],
    failure_count: int,
    last_error: str,
    firestore_client: Client,
) -> None:
    """
    Writes a visible, queryable record that a pattern has crossed the
    consecutive-audit-failure escalation threshold (see orchestrator.py's
    AUDIT_FAILURE_ESCALATION_THRESHOLD). Deliberately a separate
    collection and a separate call from clear_under_review() — the
    caller (audit_pattern) wraps this call in its own try/except: a
    failure to record this must never re-introduce the stuck-under_review
    bug clear_under_review()'s own try/finally already fixed, so this
    function's failure must never be able to prevent that one's write.
    """
    doc_ref = firestore_client.collection(NEEDS_ATTENTION_COLLECTION).document(
        _doc_id(pattern_identity_key)
    )
    doc_ref.set(
        {
            "identity_key": list(pattern_identity_key),
            "failure_count": failure_count,
            "last_error": last_error,
            "last_failed_at": datetime.now(UTC).isoformat(),
        },
        merge=True,
    )


def resolve_needs_attention(
    pattern_identity_key: tuple[str, ...], firestore_client: Client
) -> None:
    """
    Marks a needs_attention doc resolved once a pattern that previously
    escalated (record_needs_attention() was called for it) has a
    successful audit — see orchestrator.py's audit_pattern(), called on
    the success path only. Without this, a needs_attention doc persists
    forever with stale data after the pattern has already recovered,
    giving a human no way to tell a live escalation from a resolved one.

    Deliberately .update(), not .set(merge=True): most patterns never
    escalate, so most calls here target a doc that was never written, and
    that must be a silent no-op rather than creating a doc out of
    nothing. .update() on a missing doc raises — which this function
    swallows here, same isolation posture as record_needs_attention():
    the caller (audit_pattern) also wraps this call in its own
    try/except, but this function must never raise on its own regardless,
    since "there was nothing to resolve" is the common, expected case,
    not an error.
    """
    doc_ref = firestore_client.collection(NEEDS_ATTENTION_COLLECTION).document(
        _doc_id(pattern_identity_key)
    )
    try:
        doc_ref.update(
            {
                "resolved_at": datetime.now(UTC).isoformat(),
                "failure_count": 0,
            }
        )
    except Exception as exc:  # noqa: BLE001 — deliberate, see docstring:
        # this must never raise, including "no doc to resolve" (the
        # common case for a pattern that never escalated).
        logger.bind(identity_key=pattern_identity_key).debug(
            "resolve_needs_attention: nothing to resolve or write failed: {}", exc
        )
