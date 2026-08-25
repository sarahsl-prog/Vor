"""
Tests for vor_agents.datasets -- the 6 synthetic cases.

These deliberately assert the cases against the REAL graduation,
diversity and identity code rather than against hardcoded expectations.
A synthetic dataset whose cases don't actually provoke the behavior they
claim to is worse than no dataset: it looks like coverage while testing
nothing. So case #4 is asserted to genuinely fail MIN_DIVERSITY, case #6
to genuinely produce 5 deviations, and so on.
"""

import pytest

from vor_agents.datasets import (
    DatasetCase,
    UnknownDatasetCaseError,
    generate_all,
    generate_case,
)
from vor_agents.evidence_diversity import evidence_diversity_score
from vor_agents.identity import (
    DIFFABLE_FIELDS,
    GRADUATION_THRESHOLD,
    MIN_DIVERSITY,
    build_structural_template,
    diff_alert_against_template,
    pattern_identity_key,
)


class TestGeneration:
    def test_all_six_cases_generate(self):
        cases = generate_all()
        assert len(cases) == 6
        assert set(cases) == {member.value for member in DatasetCase}

    @pytest.mark.parametrize("case", list(DatasetCase))
    def test_every_case_has_the_documented_shape(self, case):
        result = generate_case(case)
        assert set(result) == {
            "case",
            "description",
            "identity_key",
            "instances",
            "probe_alert",
            "expected_outcome",
        }
        assert result["description"].strip()
        assert result["expected_outcome"].strip()

    def test_case_accepts_a_plain_string(self):
        """A CLI passes `--case low_diversity` as a string, not an enum."""
        assert generate_case("low_diversity")["case"] == "low_diversity"

    def test_unknown_case_names_the_valid_options(self):
        with pytest.raises(UnknownDatasetCaseError, match="low_diversity"):
            generate_case("not_a_real_case")

    def test_generation_is_deterministic(self):
        """Same seed must mean byte-identical output, or nothing built on
        this dataset is reproducible."""
        assert generate_all(seed=7) == generate_all(seed=7)

    def test_different_seeds_vary_the_context(self):
        a = generate_case(DatasetCase.SEEDED_CONFIRMED, seed=1)["instances"]
        b = generate_case(DatasetCase.SEEDED_CONFIRMED, seed=2)["instances"]
        assert [i["host"] for i in a] != [i["host"] for i in b] or [i["timestamp"] for i in a] != [
            i["timestamp"] for i in b
        ]

    @pytest.mark.parametrize("case", list(DatasetCase))
    def test_instances_carry_every_diffable_field(self, case):
        """build_structural_template() raises MalformedAlertError on a
        missing diffable field -- a generated instance must never trip
        it."""
        for instance in generate_case(case)["instances"]:
            for field in DIFFABLE_FIELDS:
                assert field in instance


class TestCasesProvokeTheirDesignedBehavior:
    def test_seeded_confirmed_graduates(self):
        case = generate_case(DatasetCase.SEEDED_CONFIRMED)
        template = build_structural_template(case["instances"], provenance="seeded")

        assert template["tier"] == "confirmed"
        assert template["provenance"] == "seeded"

    def test_live_confirmed_graduates_with_live_provenance(self):
        case = generate_case(DatasetCase.LIVE_CONFIRMED)
        template = build_structural_template(case["instances"], provenance="live")

        assert template["tier"] == "confirmed"
        assert template["provenance"] == "live"

    def test_identity_drift_probe_has_a_different_identity_key(self):
        """The whole point of case #3: the probe never reaches field-level
        diffing, because it isn't the same pattern."""
        case = generate_case(DatasetCase.IDENTITY_DRIFT)
        seeded_key = pattern_identity_key(case["instances"][0])

        assert pattern_identity_key(case["probe_alert"]) != seeded_key

    def test_low_diversity_meets_count_but_fails_diversity(self):
        case = generate_case(DatasetCase.LOW_DIVERSITY)
        instances = case["instances"]

        assert len(instances) >= GRADUATION_THRESHOLD
        assert evidence_diversity_score(instances) < MIN_DIVERSITY
        assert build_structural_template(instances)["tier"] == "provisional"

    def test_insufficient_history_fails_on_count(self):
        case = generate_case(DatasetCase.INSUFFICIENT_HISTORY)
        instances = case["instances"]

        assert len(instances) < GRADUATION_THRESHOLD
        assert build_structural_template(instances)["tier"] == "provisional"

    def test_field_deviation_probe_deviates_on_every_diffable_field(self):
        case = generate_case(DatasetCase.FIELD_DEVIATION)
        template = build_structural_template(case["instances"])

        deviations = diff_alert_against_template(case["probe_alert"], template["fields"])

        assert len(deviations) == len(DIFFABLE_FIELDS)

    def test_field_deviation_probe_keeps_the_same_identity_key(self):
        """Distinguishes case #6 from case #3 -- same pattern, behaving
        wrongly, rather than a different pattern entirely."""
        case = generate_case(DatasetCase.FIELD_DEVIATION)

        assert pattern_identity_key(case["probe_alert"]) == pattern_identity_key(
            case["instances"][0]
        )

    def test_trusted_case_probes_produce_no_deviations(self):
        """The negative control: cases #1/#2 must diff clean, otherwise
        the deviation cases prove nothing."""
        for name in (DatasetCase.SEEDED_CONFIRMED, DatasetCase.LIVE_CONFIRMED):
            case = generate_case(name)
            template = build_structural_template(case["instances"])

            assert diff_alert_against_template(case["probe_alert"], template["fields"]) == []
