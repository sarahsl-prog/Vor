"""
Vör — under_review flag lifecycle. Closes the burst-replay race condition.

Deliberately NOT an LLM decision — synchronous Firestore boolean writes,
same design vocabulary as the NO_HISTORY / tier checks in the classifier
prompt: when in doubt, force a deterministic degrade rather than trusting
a model to notice the ambiguity itself.
"""

from datetime import UTC, datetime

from .enrichment import CONFIDENCE_COLLECTION, _doc_id, invalidate_instances


def mark_under_review(pattern_identity_key: tuple, firestore_client) -> None:
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
    pattern_identity_key: tuple, firestore_client, auditor_decision: dict
) -> None:
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

    update = {
        "under_review": False,
        "last_reviewed_at": datetime.now(UTC).isoformat(),
    }
    if auditor_decision["action"] == "DOWNGRADE":
        rebuild = invalidate_instances(
            pattern_identity_key,
            auditor_decision.get("invalidated_instance_ids", []),
            firestore_client,
        )
        update.update(rebuild)

    doc_ref.update(update)
