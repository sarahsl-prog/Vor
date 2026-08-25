"""Tests for vor_agents.review_flag — the under_review race-condition fix
and last_reviewed_at stamping."""

from vor_agents.enrichment import (
    CONFIDENCE_COLLECTION,
    _doc_id,
    record_confirmed_negative,
)
from vor_agents.review_flag import (
    NEEDS_ATTENTION_COLLECTION,
    clear_under_review,
    mark_under_review,
    record_needs_attention,
    resolve_needs_attention,
)


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


class TestFailureCountTracking:
    def test_audit_failed_increments_failure_count(self, fake_firestore):
        identity_key = ("rule", "w3wp.exe", "csc.exe", "family")
        mark_under_review(identity_key, fake_firestore)

        new_count = clear_under_review(
            identity_key, fake_firestore, {"action": "NO_ACTION"}, audit_failed=True
        )

        assert new_count == 1
        doc = fake_firestore.collection(CONFIDENCE_COLLECTION).document(_doc_id(identity_key)).get()
        assert doc.to_dict()["failure_count"] == 1

    def test_audit_succeeded_resets_failure_count(self, fake_firestore):
        identity_key = ("rule", "w3wp.exe", "csc.exe", "family")
        mark_under_review(identity_key, fake_firestore)
        clear_under_review(identity_key, fake_firestore, {"action": "NO_ACTION"}, audit_failed=True)
        clear_under_review(identity_key, fake_firestore, {"action": "NO_ACTION"}, audit_failed=True)

        new_count = clear_under_review(
            identity_key, fake_firestore, {"action": "NO_ACTION"}, audit_failed=False
        )

        assert new_count == 0
        doc = fake_firestore.collection(CONFIDENCE_COLLECTION).document(_doc_id(identity_key)).get()
        assert doc.to_dict()["failure_count"] == 0

    def test_consecutive_failures_accumulate(self, fake_firestore):
        identity_key = ("rule", "w3wp.exe", "csc.exe", "family")
        mark_under_review(identity_key, fake_firestore)

        counts = [
            clear_under_review(
                identity_key, fake_firestore, {"action": "NO_ACTION"}, audit_failed=True
            )
            for _ in range(3)
        ]

        assert counts == [1, 2, 3]

    def test_missing_failure_count_treated_as_zero_before_incrementing(self, fake_firestore):
        """A doc that predates this feature (or is brand new) has no
        failure_count field at all -- must not KeyError, starts from 0."""
        identity_key = ("rule", "w3wp.exe", "csc.exe", "family")
        mark_under_review(identity_key, fake_firestore)

        new_count = clear_under_review(
            identity_key, fake_firestore, {"action": "NO_ACTION"}, audit_failed=True
        )

        assert new_count == 1

    def test_failed_audit_does_not_stamp_last_reviewed_at(self, fake_firestore):
        """A failed audit is not a genuine review (see review finding #1
        of the final whole-branch review) -- skipping the stamp lets
        select_audit_targets() keep re-prioritizing a repeatedly-failing
        pattern as it grows increasingly stale, instead of every failure
        burying it at the bottom of sweep priority."""
        identity_key = ("rule", "w3wp.exe", "csc.exe", "family")
        mark_under_review(identity_key, fake_firestore)

        clear_under_review(identity_key, fake_firestore, {"action": "NO_ACTION"}, audit_failed=True)

        doc = fake_firestore.collection(CONFIDENCE_COLLECTION).document(_doc_id(identity_key)).get()
        assert "last_reviewed_at" not in doc.to_dict()

    def test_successful_audit_still_stamps_last_reviewed_at(self, fake_firestore):
        """The counterpart to the test above -- a genuinely successful
        audit (audit_failed=False) must still stamp last_reviewed_at,
        same as before this change."""
        identity_key = ("rule", "w3wp.exe", "csc.exe", "family")
        mark_under_review(identity_key, fake_firestore)

        clear_under_review(
            identity_key, fake_firestore, {"action": "NO_ACTION"}, audit_failed=False
        )

        doc = fake_firestore.collection(CONFIDENCE_COLLECTION).document(_doc_id(identity_key)).get()
        assert "last_reviewed_at" in doc.to_dict()


class TestRecordNeedsAttention:
    def test_writes_a_needs_attention_doc(self, fake_firestore):
        identity_key = ("rule", "w3wp.exe", "csc.exe", "family")

        record_needs_attention(identity_key, 3, "RuntimeError('boom')", fake_firestore)

        doc = (
            fake_firestore.collection(NEEDS_ATTENTION_COLLECTION)
            .document(_doc_id(identity_key))
            .get()
        )
        assert doc.exists
        assert doc.to_dict()["failure_count"] == 3
        assert doc.to_dict()["last_error"] == "RuntimeError('boom')"
        assert doc.to_dict()["identity_key"] == list(identity_key)


class TestResolveNeedsAttention:
    def test_resolves_an_existing_doc(self, fake_firestore):
        identity_key = ("rule", "w3wp.exe", "csc.exe", "family")
        record_needs_attention(identity_key, 3, "RuntimeError('boom')", fake_firestore)

        resolve_needs_attention(identity_key, fake_firestore)

        doc = (
            fake_firestore.collection(NEEDS_ATTENTION_COLLECTION)
            .document(_doc_id(identity_key))
            .get()
        )
        data = doc.to_dict()
        assert "resolved_at" in data
        assert data["failure_count"] == 0

    def test_resolving_when_no_doc_exists_does_not_raise(self, fake_firestore):
        """Most patterns never escalate, so most calls to this function
        target a doc that was never written -- must be a silent no-op,
        not an error (same isolation posture as record_needs_attention())."""
        identity_key = ("rule", "w3wp.exe", "csc.exe", "family")

        resolve_needs_attention(identity_key, fake_firestore)  # must not raise

        doc = (
            fake_firestore.collection(NEEDS_ATTENTION_COLLECTION)
            .document(_doc_id(identity_key))
            .get()
        )
        assert not doc.exists


class _RaisingGetDocRef:
    """Stand-in for a Firestore DocumentReference whose .get() always
    fails -- used to verify clear_under_review()'s read failure is
    isolated (see final review finding #3) without needing a real
    Firestore outage."""

    def __init__(self, store, doc_id):
        self._store = store
        self._doc_id = doc_id

    def get(self):
        raise RuntimeError("firestore read failed")

    def update(self, data):
        if self._doc_id not in self._store:
            raise KeyError(f"No document to update: {self._doc_id}")
        self._store[self._doc_id].update(data)


class _RaisingGetCollection:
    def __init__(self, store):
        self._store = store

    def document(self, doc_id):
        return _RaisingGetDocRef(self._store, doc_id)


class _RaisingGetFirestoreClient:
    """Wraps a real fake_firestore's underlying store but makes every
    doc_ref.get() raise, while .update() keeps working normally against
    the same store -- so the write side-effects of a call under test can
    still be inspected via the original fake_firestore fixture."""

    def __init__(self, fake_firestore_client):
        self._fake_firestore_client = fake_firestore_client

    def collection(self, name):
        store = self._fake_firestore_client._collections.setdefault(name, {})
        return _RaisingGetCollection(store)


class TestClearUnderReviewReadFailure:
    def test_read_failure_returns_sentinel_and_write_still_happens(self, fake_firestore):
        identity_key = ("rule", "w3wp.exe", "csc.exe", "family")
        mark_under_review(identity_key, fake_firestore)
        doc_ref = fake_firestore.collection(CONFIDENCE_COLLECTION).document(_doc_id(identity_key))
        doc_ref.set({"failure_count": 2}, merge=True)

        raising_client = _RaisingGetFirestoreClient(fake_firestore)
        new_count = clear_under_review(
            identity_key, raising_client, {"action": "NO_ACTION"}, audit_failed=True
        )

        assert new_count == -1
        data = (
            fake_firestore.collection(CONFIDENCE_COLLECTION).document(_doc_id(identity_key)).get()
        ).to_dict()
        assert data["under_review"] is False
        # failure_count was left out of the update entirely, so the
        # previously-stored value survives untouched.
        assert data["failure_count"] == 2
