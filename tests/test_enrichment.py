"""Tests for vor_agents.enrichment — reads, graduation writes, seeding,
and targeted evidence invalidation."""

from vor_agents.enrichment import (
    enrich,
    invalidate_instances,
    record_confirmed_negative,
    seed_template,
)


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
            {"detection_rule_id": "rule", "parent_image": "w3wp.exe",
             "child_image": "csc.exe", "endpoint_family": "family",
             "auth_method_present": True, "session_cookie_present": True,
             "integrity_level": "Medium", "file_access_mode": "read",
             "egress_follows_access": False, "host": f"h{i}", "user": f"u{i}",
             "timestamp": f"2026-08-0{i}T09:00:00Z"}
            for i in range(1, 6)
        ]
        seed_template(key, instances_without_ids, fake_firestore)

        from vor_agents.enrichment import CONFIDENCE_COLLECTION, _doc_id
        doc = fake_firestore.collection(CONFIDENCE_COLLECTION).document(_doc_id(key)).get()
        stored = doc.to_dict()["confirmed_instances"]
        assert all("instance_id" in inst and inst["instance_id"] for inst in stored)


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
