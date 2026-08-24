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

from .enrichment import CONFIDENCE_COLLECTION, _doc_id, invalidate_instances


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


NEEDS_ATTENTION_COLLECTION = "needs_attention"


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

    Also stamps last_reviewed_at on every call, not just DOWNGRADE — an
    audit that found nothing wrong (NO_ACTION) is still evidence the
    pattern was looked at, which is exactly what select_audit_targets()
    needs to know to stop re-prioritizing it every sweep.

    audit_failed tracks failure_count: incremented on a failed audit,
    reset to 0 on a successful one. Read-then-write, not
    firestore.Increment — see this plan/spec's rationale; audits for the
    same pattern are already serialized in the common case, so the small
    race window is accepted. Returns the resulting failure_count so
    callers (audit_pattern) can decide whether to escalate without a
    second Firestore read.

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

    current = doc_ref.get().to_dict() or {}
    previous_failure_count = current.get("failure_count", 0)
    new_failure_count = previous_failure_count + 1 if audit_failed else 0

    update: dict[str, Any] = {
        "under_review": False,
        "last_reviewed_at": datetime.now(UTC).isoformat(),
        "failure_count": new_failure_count,
    }
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
