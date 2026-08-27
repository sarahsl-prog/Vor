"""Tests for vor_agents.enrichment — reads, graduation writes, seeding,
and targeted evidence invalidation."""

from vor_agents.enrichment import (
    enrich,
    invalidate_instances,
    record_confirmed_negative,
    seed_template,
)
from vor_agents.identity import pattern_identity_key


class TestEnrich:
    def test_no_history_returns_status(self, fake_firestore, baseline_alert):
        result = enrich(baseline_alert, fake_firestore)
        assert result["status"] == "NO_HISTORY"

    def test_existing_template_returns_diversity_score_not_zero(
        self, fake_firestore, baseline_alert, diverse_confirmed_instances
    ):
        """Regression test for the exact bug found and fixed while
        closing gap #1: enrich() was reading 'evidence_diversity_score'
        but nothing ever wrote a field by that name — it always silently
        returned 0.0. This must now return the REAL score."""
        for instance in diverse_confirmed_instances:
            record_confirmed_negative(instance, fake_firestore)

        result = enrich(baseline_alert, fake_firestore)
        assert result["status"] == "TEMPLATE"
        assert result["diversity_score"] > 0.0

    def test_never_audited_pattern_reports_the_stale_sentinel_not_zero(
        self, fake_firestore, diverse_confirmed_instances
    ):
        """Regression for Code-review-Aug25 2.1: a freshly-graduated
        pattern with no last_reviewed_at field yet used to report
        days_since_last_review=0 ('reviewed today'), contradicting the
        sweep's own 9999 ('never audited') sentinel for the exact same
        condition -- see orchestrator._fetch_all_confirmed_patterns."""
        for instance in diverse_confirmed_instances:
            record_confirmed_negative(instance, fake_firestore)

        result = enrich(diverse_confirmed_instances[0], fake_firestore)

        assert result["days_since_last_review"] == 9999


class TestRecordConfirmedNegative:
    def test_graduates_after_enough_diverse_instances(
        self, fake_firestore, diverse_confirmed_instances
    ):
        template = None
        for instance in diverse_confirmed_instances:
            template = record_confirmed_negative(instance, fake_firestore)
        assert template["tier"] == "confirmed"

    def test_stays_provisional_with_low_diversity(
        self, fake_firestore, low_diversity_confirmed_instances
    ):
        template = None
        for instance in low_diversity_confirmed_instances:
            template = record_confirmed_negative(instance, fake_firestore)
        assert template["tier"] == "provisional"

    def test_assigns_stable_instance_id(self, fake_firestore, baseline_alert):
        record_confirmed_negative(baseline_alert, fake_firestore)
        # instance_id isn't in the template return, but it should be on
        # the stored doc — verify via a second enrichment call context.
        from vor_agents.enrichment import CONFIDENCE_COLLECTION, _doc_id
        from vor_agents.identity import pattern_identity_key

        key = pattern_identity_key(baseline_alert)
        doc = fake_firestore.collection(CONFIDENCE_COLLECTION).document(_doc_id(key)).get()
        instances = doc.to_dict()["confirmed_instances"]
        assert "instance_id" in instances[0]
        assert instances[0]["instance_id"] is not None

    def test_human_confirmed_true_tags_instance_verified_by_human(
        self, fake_firestore, baseline_alert
    ):
        """human_confirmed was previously accepted but silently ignored —
        every instance looked identically trusted regardless of how it was
        confirmed. Default (True) must now tag the instance accordingly."""
        record_confirmed_negative(baseline_alert, fake_firestore)

        from vor_agents.enrichment import CONFIDENCE_COLLECTION, _doc_id
        from vor_agents.identity import pattern_identity_key

        key = pattern_identity_key(baseline_alert)
        doc = fake_firestore.collection(CONFIDENCE_COLLECTION).document(_doc_id(key)).get()
        instances = doc.to_dict()["confirmed_instances"]
        assert instances[0]["verified_by"] == "human"

    def test_human_confirmed_false_tags_instance_verified_by_bulk(
        self, fake_firestore, baseline_alert
    ):
        record_confirmed_negative(baseline_alert, fake_firestore, human_confirmed=False)

        from vor_agents.enrichment import CONFIDENCE_COLLECTION, _doc_id
        from vor_agents.identity import pattern_identity_key

        key = pattern_identity_key(baseline_alert)
        doc = fake_firestore.collection(CONFIDENCE_COLLECTION).document(_doc_id(key)).get()
        instances = doc.to_dict()["confirmed_instances"]
        assert instances[0]["verified_by"] == "bulk"

    def test_verified_by_is_per_instance_not_clobbered_by_later_calls(
        self, fake_firestore, diverse_confirmed_instances
    ):
        """The whole reason verified_by is tagged per-instance rather than
        as a doc-level field: confirmed_instances accumulates across many
        calls, and a doc-level field would silently mislabel every earlier
        instance with whichever call happened last."""
        first, *rest = diverse_confirmed_instances
        record_confirmed_negative(first, fake_firestore, human_confirmed=True)
        for instance in rest:
            record_confirmed_negative(instance, fake_firestore, human_confirmed=False)

        from vor_agents.enrichment import CONFIDENCE_COLLECTION, _doc_id
        from vor_agents.identity import pattern_identity_key

        key = pattern_identity_key(first)
        doc = fake_firestore.collection(CONFIDENCE_COLLECTION).document(_doc_id(key)).get()
        instances = doc.to_dict()["confirmed_instances"]
        assert instances[0]["verified_by"] == "human"
        assert all(inst["verified_by"] == "bulk" for inst in instances[1:])


class TestSeedTemplate:
    def test_seed_batch_meeting_threshold_enters_confirmed(
        self, fake_firestore, diverse_confirmed_instances
    ):
        """The whole point of seeding: skip live graduation if the seed
        batch already has enough diverse evidence."""
        key = ("rule", "w3wp.exe", "csc.exe", "family")
        template = seed_template(key, diverse_confirmed_instances, fake_firestore)
        assert template["tier"] == "confirmed"
        assert template["provenance"] == "seeded"

    def test_seeded_instances_get_ids_if_missing(self, fake_firestore):
        key = ("rule", "w3wp.exe", "csc.exe", "family")
        instances_without_ids = [
            {
                "detection_rule_id": "rule",
                "parent_image": "w3wp.exe",
                "child_image": "csc.exe",
                "endpoint_family": "family",
                "auth_method_present": True,
                "session_cookie_present": True,
                "integrity_level": "Medium",
                "file_access_mode": "read",
                "egress_follows_access": False,
                "host": f"h{i}",
                "user": f"u{i}",
                "timestamp": f"2026-08-0{i}T09:00:00Z",
            }
            for i in range(1, 6)
        ]
        seed_template(key, instances_without_ids, fake_firestore)

        from vor_agents.enrichment import CONFIDENCE_COLLECTION, _doc_id

        doc = fake_firestore.collection(CONFIDENCE_COLLECTION).document(_doc_id(key)).get()
        stored = doc.to_dict()["confirmed_instances"]
        assert all("instance_id" in inst and inst["instance_id"] for inst in stored)

    def test_seeded_instances_tagged_verified_by_bulk(
        self, fake_firestore, diverse_confirmed_instances
    ):
        """No per-alert human signed off on a seeded batch, regardless of
        how trustworthy the source dataset is — same "bulk" tag as
        record_confirmed_negative(human_confirmed=False)."""
        key = ("rule", "w3wp.exe", "csc.exe", "family")
        seed_template(key, diverse_confirmed_instances, fake_firestore)

        from vor_agents.enrichment import CONFIDENCE_COLLECTION, _doc_id

        doc = fake_firestore.collection(CONFIDENCE_COLLECTION).document(_doc_id(key)).get()
        stored = doc.to_dict()["confirmed_instances"]
        assert all(inst["verified_by"] == "bulk" for inst in stored)


class TestInvalidateInstances:
    def test_removes_only_cited_instances(self, fake_firestore, diverse_confirmed_instances):
        """The core behavior of targeted evidence invalidation: removing
        ONE bad instance out of 5 should not blow away the other 4."""
        for instance in diverse_confirmed_instances:
            record_confirmed_negative(instance, fake_firestore)

        key = ("SharePoint_ToolPane_Rule", "w3wp.exe", "csc.exe", "ToolPane_admin")
        result = invalidate_instances(key, ["i1"], fake_firestore)

        remaining_ids = {inst["instance_id"] for inst in result["confirmed_instances"]}
        assert "i1" not in remaining_ids
        assert len(remaining_ids) == 4

    def test_removing_enough_instances_demotes_tier(
        self, fake_firestore, diverse_confirmed_instances
    ):
        """If enough instances get invalidated that diversity or count
        drops below threshold, tier should fall back to provisional —
        this is a CONSEQUENCE of the rebuild, never force-set."""
        for instance in diverse_confirmed_instances:
            record_confirmed_negative(instance, fake_firestore)

        key = ("SharePoint_ToolPane_Rule", "w3wp.exe", "csc.exe", "ToolPane_admin")
        # Remove all but one instance
        result = invalidate_instances(key, ["i1", "i2", "i3", "i4"], fake_firestore)
        assert result["tier"] == "provisional"


class TestEnrichFailureCount:
    def test_enrich_surfaces_failure_count(self, fake_firestore, baseline_alert):
        from vor_agents.enrichment import CONFIDENCE_COLLECTION, _doc_id

        record_confirmed_negative(baseline_alert, fake_firestore)
        identity_key = pattern_identity_key(baseline_alert)
        doc_ref = fake_firestore.collection(CONFIDENCE_COLLECTION).document(_doc_id(identity_key))
        doc_ref.set({"failure_count": 2}, merge=True)

        result = enrich(baseline_alert, fake_firestore)

        assert result["failure_count"] == 2

    def test_enrich_defaults_failure_count_to_zero(self, fake_firestore, baseline_alert):
        record_confirmed_negative(baseline_alert, fake_firestore)

        result = enrich(baseline_alert, fake_firestore)

        assert result["failure_count"] == 0
