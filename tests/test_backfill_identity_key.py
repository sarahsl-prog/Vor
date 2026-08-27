"""
Tests for scripts/backfill_identity_key.py -- the one-time migration that
recovers the identity_key field on confidence docs predating the
content-hash doc ID (docs/TODO-Aug15.md Task 3).

The interesting cases here are all failure/edge cases: this script runs
exactly once, against real production data, with no second chance -- so
"what does it do with a doc it can't recover" matters as much as the
happy path.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.backfill_identity_key import (
    BackfillError,
    _recover_identity_key,
    backfill,
)
from vor_agents.enrichment import CONFIDENCE_COLLECTION, _doc_id

IDENTITY_KEY = ("SharePoint_ToolPane_Rule", "w3wp.exe", "csc.exe", "ToolPane_admin")
LEGACY_DOC_ID = "_".join(IDENTITY_KEY)


def _instance(instance_id="i1", **overrides):
    base = {
        "detection_rule_id": "SharePoint_ToolPane_Rule",
        "parent_image": "w3wp.exe",
        "child_image": "csc.exe",
        "endpoint_family": "ToolPane_admin",
        "instance_id": instance_id,
    }
    base.update(overrides)
    return base


def _write_legacy_doc(fake_firestore, doc_id=LEGACY_DOC_ID, instances=None):
    """A doc as it existed before the migration: join-based ID, no
    identity_key field."""
    fake_firestore.collection(CONFIDENCE_COLLECTION).document(doc_id).set(
        {
            "confirmed_instances": instances if instances is not None else [_instance()],
            "tier": "confirmed",
            "under_review": False,
        }
    )


class TestRecoverIdentityKey:
    def test_recovers_from_confirmed_instances(self):
        assert _recover_identity_key({"confirmed_instances": [_instance()]}) == IDENTITY_KEY

    def test_no_instances_is_unrecoverable(self):
        with pytest.raises(BackfillError, match="no confirmed_instances"):
            _recover_identity_key({"confirmed_instances": []})

    def test_instance_missing_identity_field_is_unrecoverable(self):
        broken = _instance()
        del broken["child_image"]
        with pytest.raises(BackfillError, match="is malformed"):
            _recover_identity_key({"confirmed_instances": [broken]})

    def test_disagreeing_instances_are_unrecoverable(self):
        """A doc whose instances don't agree on their identity is corrupt.
        Picking the first silently would write a confidently-wrong
        identity_key -- exactly the kind of quiet data damage a one-shot
        migration must not do."""
        with pytest.raises(BackfillError, match="disagree"):
            _recover_identity_key(
                {
                    "confirmed_instances": [
                        _instance("i1"),
                        _instance("i2", child_image="cmd.exe"),
                    ]
                }
            )

    def test_underscore_containing_components_recover_exactly(self):
        """The whole point of the migration: a legacy doc ID could not be
        split back apart unambiguously, but the instances carry the parts
        verbatim."""
        instance = _instance(detection_rule_id="rule_with_underscores")
        recovered = _recover_identity_key({"confirmed_instances": [instance]})
        assert recovered[0] == "rule_with_underscores"


class TestBackfill:
    def test_migrates_legacy_doc_to_hashed_id(self, fake_firestore):
        _write_legacy_doc(fake_firestore)

        counts = backfill(fake_firestore)

        assert counts == {"migrated": 1, "already_current": 0, "skipped": 0}
        migrated = (
            fake_firestore.collection(CONFIDENCE_COLLECTION).document(_doc_id(IDENTITY_KEY)).get()
        )
        assert migrated.exists
        assert migrated.to_dict()["identity_key"] == list(IDENTITY_KEY)

    def test_deletes_the_legacy_doc(self, fake_firestore):
        """Leaving the old ID behind would strand a duplicate no reader
        can address."""
        _write_legacy_doc(fake_firestore)

        backfill(fake_firestore)

        assert (
            not fake_firestore.collection(CONFIDENCE_COLLECTION)
            .document(LEGACY_DOC_ID)
            .get()
            .exists
        )

    def test_preserves_the_rest_of_the_doc(self, fake_firestore):
        _write_legacy_doc(fake_firestore)

        backfill(fake_firestore)

        data = (
            fake_firestore.collection(CONFIDENCE_COLLECTION)
            .document(_doc_id(IDENTITY_KEY))
            .get()
            .to_dict()
        )
        assert data["tier"] == "confirmed"
        assert data["confirmed_instances"] == [_instance()]

    def test_already_current_doc_is_left_alone(self, fake_firestore):
        fake_firestore.collection(CONFIDENCE_COLLECTION).document(_doc_id(IDENTITY_KEY)).set(
            {
                "identity_key": list(IDENTITY_KEY),
                "confirmed_instances": [_instance()],
                "tier": "confirmed",
            }
        )

        counts = backfill(fake_firestore)

        assert counts == {"migrated": 0, "already_current": 1, "skipped": 0}

    def test_rerun_is_a_no_op(self, fake_firestore):
        """Idempotency matters: a migration that half-failed will be run
        again, and the second run must not duplicate or damage anything."""
        _write_legacy_doc(fake_firestore)

        backfill(fake_firestore)
        second_run = backfill(fake_firestore)

        assert second_run == {"migrated": 0, "already_current": 1, "skipped": 0}

    def test_unrecoverable_doc_is_skipped_not_fatal(self, fake_firestore):
        """One corrupt legacy doc must not block migrating the others."""
        _write_legacy_doc(fake_firestore, doc_id="broken", instances=[])
        _write_legacy_doc(fake_firestore)

        counts = backfill(fake_firestore)

        assert counts == {"migrated": 1, "already_current": 0, "skipped": 1}
        # The unrecoverable doc is left exactly as it was, not deleted.
        assert fake_firestore.collection(CONFIDENCE_COLLECTION).document("broken").get().exists

    def test_dry_run_writes_nothing(self, fake_firestore):
        _write_legacy_doc(fake_firestore)

        counts = backfill(fake_firestore, dry_run=True)

        assert counts == {"migrated": 1, "already_current": 0, "skipped": 0}
        assert fake_firestore.collection(CONFIDENCE_COLLECTION).document(LEGACY_DOC_ID).get().exists
        assert (
            not fake_firestore.collection(CONFIDENCE_COLLECTION)
            .document(_doc_id(IDENTITY_KEY))
            .get()
            .exists
        )

    def test_migrates_every_legacy_doc_in_one_pass(self, fake_firestore):
        _write_legacy_doc(fake_firestore, doc_id="a_b", instances=[_instance(child_image="a.exe")])
        _write_legacy_doc(fake_firestore, doc_id="c_d", instances=[_instance(child_image="b.exe")])
        _write_legacy_doc(fake_firestore)

        counts = backfill(fake_firestore)

        assert counts["migrated"] == 3
