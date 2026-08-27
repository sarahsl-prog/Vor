"""
Tests for scripts/seed_firestore.py -- the CLI entrypoint for
enrichment.seed_template().

Weighted toward input validation, because this script is the one place
operator-supplied data enters Firestore in bulk. A malformed file that
gets halfway through writing is far more expensive than one that is
refused outright.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.seed_firestore import (
    SeedInputError,
    group_by_identity,
    load_instances_from_file,
    seed,
)
from vor_agents.datasets import DatasetCase, generate_case
from vor_agents.enrichment import CONFIDENCE_COLLECTION, _doc_id


def _write_json(tmp_path, payload):
    path = tmp_path / "history.json"
    path.write_text(json.dumps(payload))
    return path


class TestLoadInstancesFromFile:
    def test_loads_a_valid_file(self, tmp_path):
        instances = generate_case(DatasetCase.SEEDED_CONFIRMED)["instances"]
        assert load_instances_from_file(_write_json(tmp_path, instances)) == instances

    def test_missing_file_is_a_clear_error(self, tmp_path):
        with pytest.raises(SeedInputError, match="Cannot read"):
            load_instances_from_file(tmp_path / "nope.json")

    def test_invalid_json_is_a_clear_error(self, tmp_path):
        path = tmp_path / "history.json"
        path.write_text("{not json")
        with pytest.raises(SeedInputError, match="not valid JSON"):
            load_instances_from_file(path)

    def test_non_list_is_rejected(self, tmp_path):
        with pytest.raises(SeedInputError, match="must contain a JSON list"):
            load_instances_from_file(_write_json(tmp_path, {"detection_rule_id": "r"}))

    def test_empty_list_is_rejected(self, tmp_path):
        with pytest.raises(SeedInputError, match="no instances"):
            load_instances_from_file(_write_json(tmp_path, []))

    def test_non_object_entry_is_rejected(self, tmp_path):
        with pytest.raises(SeedInputError, match=r"\[1\] is not a JSON object"):
            load_instances_from_file(
                _write_json(
                    tmp_path, [generate_case(DatasetCase.SEEDED_CONFIRMED)["instances"][0], "x"]
                )
            )

    def test_instance_missing_identity_field_is_rejected(self, tmp_path):
        instances = generate_case(DatasetCase.SEEDED_CONFIRMED)["instances"]
        del instances[2]["child_image"]

        with pytest.raises(SeedInputError, match=r"\[2\] is malformed"):
            load_instances_from_file(_write_json(tmp_path, instances))

    def test_validation_happens_before_any_write(self, tmp_path, fake_firestore):
        """The whole reason validation is a separate up-front pass: a bad
        record late in the file must not leave earlier patterns seeded."""
        instances = generate_case(DatasetCase.SEEDED_CONFIRMED)["instances"]
        del instances[-1]["parent_image"]
        path = _write_json(tmp_path, instances)

        with pytest.raises(SeedInputError):
            load_instances_from_file(path)

        assert fake_firestore._collections == {}


class TestGroupByIdentity:
    def test_one_pattern_groups_together(self):
        instances = generate_case(DatasetCase.SEEDED_CONFIRMED)["instances"]
        grouped = group_by_identity(instances)

        assert len(grouped) == 1
        assert len(next(iter(grouped.values()))) == len(instances)

    def test_distinct_patterns_are_separated(self):
        baseline = generate_case(DatasetCase.SEEDED_CONFIRMED)["instances"]
        other = [{**instance, "child_image": "cmd.exe"} for instance in baseline]

        assert len(group_by_identity(baseline + other)) == 2


class TestSeed:
    def test_seeds_a_confirmed_pattern(self, fake_firestore):
        case = generate_case(DatasetCase.SEEDED_CONFIRMED)

        summary = seed(case["instances"], fake_firestore)

        assert summary["patterns"] == 1
        assert summary["instances"] == len(case["instances"])
        assert list(summary["tiers"].values()) == ["confirmed"]

        doc = (
            fake_firestore.collection(CONFIDENCE_COLLECTION)
            .document(_doc_id(case["identity_key"]))
            .get()
        )
        assert doc.exists
        assert doc.to_dict()["tier"] == "confirmed"
        assert doc.to_dict()["provenance"] == "seeded"

    def test_every_seeded_instance_is_marked_bulk(self, fake_firestore):
        """Provenance labelling is a trust claim, not decoration -- no
        human signed off on these individually and the auditor must be
        able to tell."""
        case = generate_case(DatasetCase.SEEDED_CONFIRMED)

        seed(case["instances"], fake_firestore)

        doc = (
            fake_firestore.collection(CONFIDENCE_COLLECTION)
            .document(_doc_id(case["identity_key"]))
            .get()
        )
        assert all(i["verified_by"] == "bulk" for i in doc.to_dict()["confirmed_instances"])

    def test_below_threshold_batch_reports_provisional(self, fake_firestore):
        """A too-small batch landing at provisional is the graduation gate
        working -- but the operator must see it, not assume confirmed."""
        case = generate_case(DatasetCase.INSUFFICIENT_HISTORY)

        summary = seed(case["instances"], fake_firestore)

        assert list(summary["tiers"].values()) == ["provisional"]

    def test_low_diversity_batch_reports_provisional(self, fake_firestore):
        case = generate_case(DatasetCase.LOW_DIVERSITY)

        summary = seed(case["instances"], fake_firestore)

        assert list(summary["tiers"].values()) == ["provisional"]

    def test_dry_run_writes_nothing_but_reports_the_tier(self, fake_firestore):
        case = generate_case(DatasetCase.SEEDED_CONFIRMED)

        summary = seed(case["instances"], fake_firestore, dry_run=True)

        assert list(summary["tiers"].values()) == ["confirmed"]
        assert fake_firestore._collections == {}

    def test_seeds_multiple_patterns_in_one_run(self, fake_firestore):
        baseline = generate_case(DatasetCase.SEEDED_CONFIRMED)["instances"]
        other = [{**instance, "child_image": "cmd.exe"} for instance in baseline]

        summary = seed(baseline + other, fake_firestore)

        assert summary["patterns"] == 2
        assert len(summary["tiers"]) == 2
