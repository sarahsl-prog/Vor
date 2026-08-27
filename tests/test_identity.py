"""
Tests for vor_agents.identity — pattern identity, structural templates,
the two-part graduation gate, and exhaustive field diffing.
"""

import pytest

from vor_agents.identity import (
    GRADUATION_THRESHOLD,
    MIN_DIVERSITY,
    MalformedAlertError,
    build_structural_template,
    diff_alert_against_template,
    pattern_identity_key,
)


class TestPatternIdentityKey:
    def test_returns_four_tuple(self, baseline_alert):
        key = pattern_identity_key(baseline_alert)
        assert key == (
            "SharePoint_ToolPane_Rule",
            "w3wp.exe",
            "csc.exe",
            "ToolPane_admin",
        )

    def test_excludes_diffable_fields(self, baseline_alert):
        """Identity must not change even if diffable fields do — that's
        the whole point of keeping them separate (see identity.py
        module docstring: an attacker repeating a technique shouldn't
        escape into a 'new pattern' by varying auth_method_present)."""
        key1 = pattern_identity_key(baseline_alert)
        drifted = {**baseline_alert, "auth_method_present": False, "integrity_level": "High"}
        key2 = pattern_identity_key(drifted)
        assert key1 == key2

    def test_different_child_image_is_different_key(self, baseline_alert, drift_alert_cve_model):
        """This is what makes the CVE-2026-56164 drift case get rejected
        at the identity layer before diffing even runs."""
        assert pattern_identity_key(baseline_alert) != pattern_identity_key(drift_alert_cve_model)


class TestPatternIdentityKeyValidation:
    def test_missing_identity_field_raises_malformed_alert_error(self, baseline_alert):
        """Regression for Code-review-Aug25 3.1: pattern_identity_key
        indexed the alert dict directly, so a missing identity field
        raised a raw KeyError -- inconsistent with this project's 'never
        surface raw exceptions' standard, which every OTHER validation
        path (ClassifierRequest, build_structural_template) already
        follows."""
        broken = dict(baseline_alert)
        del broken["parent_image"]

        with pytest.raises(MalformedAlertError, match="parent_image"):
            pattern_identity_key(broken)


class TestBuildStructuralTemplate:
    def test_empty_instances_returns_provisional(self):
        result = build_structural_template([])
        assert result["tier"] == "provisional"
        assert result["fields"] == {}
        assert result["instance_count"] == 0

    def test_invariant_fields_captured(self, diverse_confirmed_instances):
        """All 5 diverse instances share the same field values — every
        field should be captured as invariant even though host/user/
        timestamp differ."""
        result = build_structural_template(diverse_confirmed_instances)
        assert result["fields"]["auth_method_present"] is True
        assert result["fields"]["integrity_level"] == "Medium"
        assert result["fields"]["file_access_mode"] == "read"

    def test_diverse_instances_graduate_to_confirmed(self, diverse_confirmed_instances):
        """The two-part gate's positive case: enough count AND enough
        diversity -> confirmed."""
        result = build_structural_template(diverse_confirmed_instances)
        assert len(diverse_confirmed_instances) >= GRADUATION_THRESHOLD
        assert result["diversity_score"] >= MIN_DIVERSITY
        assert result["tier"] == "confirmed"

    def test_low_diversity_stays_provisional_despite_meeting_count(
        self, low_diversity_confirmed_instances
    ):
        """THE core regression test for gap #1's fix. 3 instances meets
        GRADUATION_THRESHOLD by count alone, but they're all the same
        host/user/hour — must NOT graduate. This is exactly the
        statistical weakness (coin-flip-odds invariant field) the
        two-part gate exists to close."""
        result = build_structural_template(low_diversity_confirmed_instances)
        assert len(low_diversity_confirmed_instances) >= GRADUATION_THRESHOLD
        assert result["diversity_score"] < MIN_DIVERSITY
        assert result["tier"] == "provisional"

    def test_variable_field_excluded_from_template(self):
        """A field that varies across confirmed instances must NOT appear
        in the template — it carries no diffing signal (see
        DIFFABLE_FIELDS handling in build_structural_template)."""
        instances = [
            {
                "detection_rule_id": "r",
                "parent_image": "p",
                "child_image": "c",
                "endpoint_family": "e",
                "auth_method_present": True,
                "session_cookie_present": True,
                "integrity_level": "Medium",
                "file_access_mode": "read",
                "egress_follows_access": False,
            },
            {
                "detection_rule_id": "r",
                "parent_image": "p",
                "child_image": "c",
                "endpoint_family": "e",
                "auth_method_present": True,
                "session_cookie_present": True,
                "integrity_level": "High",
                "file_access_mode": "read",
                "egress_follows_access": False,
            },
        ]
        result = build_structural_template(instances)
        assert "integrity_level" not in result["fields"]
        assert result["fields"]["auth_method_present"] is True

    def test_provenance_passed_through(self, diverse_confirmed_instances):
        result = build_structural_template(diverse_confirmed_instances, provenance="seeded")
        assert result["provenance"] == "seeded"

    def test_instance_missing_diffable_field_raises_malformed_alert_error(
        self, diverse_confirmed_instances
    ):
        """A confirmed instance missing a required DIFFABLE_FIELDS key
        must fail loudly and clearly, not with a raw KeyError. Structural
        fields are required (unlike evidence_diversity_score's optional
        host/user/timestamp dimensions), so a missing one is a
        data-quality problem, not a 'field just varies here' signal."""
        malformed = [dict(diverse_confirmed_instances[0])]
        del malformed[0]["integrity_level"]

        with pytest.raises(MalformedAlertError, match="integrity_level"):
            build_structural_template(malformed)


class TestDiffAlertAgainstTemplate:
    def test_no_deviations_on_matching_alert(self, baseline_alert, confirmed_template_fields):
        deviations = diff_alert_against_template(baseline_alert, confirmed_template_fields)
        assert deviations == []

    def test_exhaustive_not_first_match_only(
        self, field_level_drift_alert, confirmed_template_fields
    ):
        """Dataset case #6: ALL 5 fields deviate simultaneously. Must
        report every one, not short-circuit on the first mismatch — this
        was an explicit design decision (see classifier_agent.py's
        prompt rule 3: 'do not stop at the first mismatch')."""
        deviations = diff_alert_against_template(field_level_drift_alert, confirmed_template_fields)
        deviated_fields = {d["field"] for d in deviations}
        assert deviated_fields == {
            "auth_method_present",
            "session_cookie_present",
            "integrity_level",
            "file_access_mode",
            "egress_follows_access",
        }

    def test_single_field_deviation_detected(self, baseline_alert, confirmed_template_fields):
        drifted = {**baseline_alert, "integrity_level": "High"}
        deviations = diff_alert_against_template(drifted, confirmed_template_fields)
        assert len(deviations) == 1
        assert deviations[0]["field"] == "integrity_level"
        assert deviations[0]["template"] == "Medium"
        assert deviations[0]["observed"] == "High"

    def test_missing_field_on_alert_is_a_deviation(self, confirmed_template_fields):
        """If the alert is missing a templated field entirely, .get()
        returns None, which won't match the template's expected value —
        this should show up as a deviation, not be silently skipped."""
        sparse_alert = {"auth_method_present": True}  # missing everything else
        deviations = diff_alert_against_template(sparse_alert, confirmed_template_fields)
        assert len(deviations) == 4  # every field except auth_method_present
