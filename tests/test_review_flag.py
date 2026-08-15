"""Tests for vor_agents.review_flag — the under_review race-condition fix
and last_reviewed_at stamping."""

from vor_agents.enrichment import (
    CONFIDENCE_COLLECTION,
    _doc_id,
    record_confirmed_negative,
)
from vor_agents.review_flag import clear_under_review, mark_under_review


def _setup_confirmed_pattern(fake_firestore, diverse_confirmed_instances):
    for instance in diverse_confirmed_instances:
        record_confirmed_negative(instance, fake_firestore)
    return ("SharePoint_ToolPane_Rule", "w3wp.exe", "csc.exe", "ToolPane_admin")


def test_mark_under_review_sets_flag(fake_firestore, diverse_confirmed_instances):
    key = _setup_confirmed_pattern(fake_firestore, diverse_confirmed_instances)
    mark_under_review(key, fake_firestore)
    doc = fake_firestore.collection(CONFIDENCE_COLLECTION).document(_doc_id(key)).get()
    assert doc.to_dict()["under_review"] is True


def test_clear_under_review_no_action_stamps_review_time_not_tier(
    fake_firestore, diverse_confirmed_instances
):
    """NO_ACTION should still stamp last_reviewed_at (an audit that found
    nothing wrong is still evidence the pattern was looked at) but must
    NOT touch tier or confirmed_instances."""
    key = _setup_confirmed_pattern(fake_firestore, diverse_confirmed_instances)
    mark_under_review(key, fake_firestore)

    clear_under_review(key, fake_firestore, {"action": "NO_ACTION"})

    doc = fake_firestore.collection(CONFIDENCE_COLLECTION).document(_doc_id(key)).get()
    data = doc.to_dict()
    assert data["under_review"] is False
    assert "last_reviewed_at" in data
    assert data["tier"] == "confirmed"


def test_clear_under_review_downgrade_invalidates_cited_instances(
    fake_firestore, diverse_confirmed_instances
):
    key = _setup_confirmed_pattern(fake_firestore, diverse_confirmed_instances)
    mark_under_review(key, fake_firestore)

    clear_under_review(
        key,
        fake_firestore,
        {
            "action": "DOWNGRADE",
            "invalidated_instance_ids": ["i1"],
        },
    )

    doc = fake_firestore.collection(CONFIDENCE_COLLECTION).document(_doc_id(key)).get()
    data = doc.to_dict()
    assert data["under_review"] is False
    remaining_ids = {inst["instance_id"] for inst in data["confirmed_instances"]}
    assert "i1" not in remaining_ids
    assert len(remaining_ids) == 4


def test_clear_under_review_recommend_upgrade_never_changes_tier(
    fake_firestore, diverse_confirmed_instances
):
    """RECOMMEND_UPGRADE_FOR_HUMAN_REVIEW must be a complete no-op on
    tier/confirmed_instances — the auditor can never autonomously raise
    trust, only clear its own review flag."""
    key = _setup_confirmed_pattern(fake_firestore, diverse_confirmed_instances)
    mark_under_review(key, fake_firestore)
    before = fake_firestore.collection(CONFIDENCE_COLLECTION).document(_doc_id(key)).get().to_dict()

    clear_under_review(key, fake_firestore, {"action": "RECOMMEND_UPGRADE_FOR_HUMAN_REVIEW"})

    after = fake_firestore.collection(CONFIDENCE_COLLECTION).document(_doc_id(key)).get().to_dict()
    assert after["tier"] == before["tier"]
    assert after["confirmed_instances"] == before["confirmed_instances"]
    assert after["under_review"] is False
