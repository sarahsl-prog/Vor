"""
Vör — tests for vor_agents/event_stream.py.

The properties worth pinning here are the ones a caller silently depends
on: that a seed replays exactly, that generated alerts are actually
classifiable by the real identity/diff code (not just plausible-looking
dicts), that the canonical cases really do get injected on schedule, and
that the answer-key labels never leak into the published payload.
"""

from datetime import UTC, datetime

import pytest

from vor_agents.datasets import DatasetCase, generate_case
from vor_agents.event_stream import (
    BACKGROUND_PATTERNS,
    EventStreamConfigError,
    generate_events,
    validate_alert,
)
from vor_agents.identity import (
    DIFFABLE_FIELDS,
    IDENTITY_KEY_FIELDS,
    build_structural_template,
    pattern_identity_key,
)

FIXED_NOW = datetime(2026, 8, 29, 12, 0, 0, tzinfo=UTC)


def _now() -> datetime:
    return FIXED_NOW


def _events(count, **kwargs):
    kwargs.setdefault("now_fn", _now)
    return list(generate_events(count, **kwargs))


class TestShape:
    def test_every_alert_carries_every_required_field(self):
        for event in _events(200):
            assert validate_alert(event.alert) == []

    def test_alerts_are_accepted_by_the_real_identity_key_builder(self):
        # The point of using pattern_identity_key rather than checking
        # field names by hand: if identity.py's requirements change, this
        # test fails instead of the generator quietly emitting alerts the
        # service will 422.
        for event in _events(100):
            key = pattern_identity_key(event.alert)
            assert len(key) == len(IDENTITY_KEY_FIELDS)

    def test_alerts_carry_context_for_diversity_scoring(self):
        for event in _events(50):
            assert event.alert["host"]
            assert event.alert["user"]
            assert event.alert["timestamp"].endswith("Z")

    def test_instance_ids_are_unique(self):
        ids = [event.alert["instance_id"] for event in _events(300)]
        assert len(set(ids)) == len(ids)

    def test_labels_never_leak_into_the_published_alert(self):
        # A synthetic answer key inside the payload would be visible to
        # enrichment, the prompt and Firestore — see GeneratedEvent's
        # docstring. Nothing outside the real alert schema may appear.
        allowed = (
            set(IDENTITY_KEY_FIELDS)
            | set(DIFFABLE_FIELDS)
            | {
                "host",
                "user",
                "timestamp",
                "instance_id",
            }
        )
        for event in _events(200):
            assert set(event.alert) <= allowed


class TestDeterminism:
    def test_same_seed_replays_identically(self):
        assert _events(120, seed=7) == _events(120, seed=7)

    def test_different_seeds_diverge(self):
        assert _events(120, seed=1) != _events(120, seed=2)

    def test_timestamps_come_from_the_injected_clock(self):
        stamp = _events(5)[0].alert["timestamp"]
        assert stamp == "2026-08-29T12:00:00Z"

    def test_default_clock_is_wall_time(self):
        # now_fn omitted deliberately: the live path must stamp real time,
        # not the fixed dataset epoch.
        before = datetime.now(UTC)
        event = next(generate_events(1))
        stamped = datetime.fromisoformat(event.alert["timestamp"])
        assert before <= stamped <= datetime.now(UTC)


class TestCanonicalInjection:
    def test_cases_are_injected_on_the_interval(self):
        events = _events(60, case_interval=10)
        canonical = [i for i, e in enumerate(events) if e.kind == "canonical"]
        # 1-based positions 10, 20, ... => 0-based indices 9, 19, ...
        assert canonical == [9, 19, 29, 39, 49, 59]

    def test_cases_cycle_in_order(self):
        events = _events(60, case_interval=10)
        injected = [e.case for e in events if e.kind == "canonical"]
        assert injected == [case.value for case in DatasetCase]

    def test_all_six_cases_appear_over_a_full_cycle(self):
        events = _events(60, case_interval=10)
        assert {e.case for e in events if e.kind == "canonical"} == {
            case.value for case in DatasetCase
        }

    def test_injected_probe_preserves_the_case_semantics(self):
        # Only timestamp/instance_id may be re-stamped; changing an
        # identity or diffable field would silently turn case #6 into a
        # different case entirely.
        events = _events(10, case_interval=10, seed=3)
        injected = events[9]
        original = generate_case(DatasetCase.SEEDED_CONFIRMED, seed=3)["probe_alert"]
        for name in list(IDENTITY_KEY_FIELDS) + DIFFABLE_FIELDS:
            assert injected.alert[name] == original[name]

    def test_injected_probe_is_restamped_not_epoch_dated(self):
        injected = _events(10, case_interval=10)[9]
        assert injected.alert["timestamp"] == "2026-08-29T12:00:00Z"
        assert injected.alert["instance_id"].endswith(DatasetCase.SEEDED_CONFIRMED.value)

    def test_no_cases_yields_pure_noise(self):
        events = _events(100, inject_cases=False)
        assert all(event.kind != "canonical" for event in events)

    def test_field_deviation_probe_deviates_from_the_baseline_template(self):
        # End-to-end through the real diff logic: case #6 injected into a
        # stream must still register as a deviation against a template
        # built from case #1's history, which is the whole reason to
        # inject it.
        history = generate_case(DatasetCase.SEEDED_CONFIRMED, seed=0)["instances"]
        template = build_structural_template(history)
        events = _events(60, case_interval=10, seed=0)
        probe = next(e for e in events if e.case == DatasetCase.FIELD_DEVIATION.value)
        deviating = [
            name for name in DIFFABLE_FIELDS if probe.alert[name] != template["fields"][name]
        ]
        assert len(deviating) == len(DIFFABLE_FIELDS)


class TestTrafficMix:
    def test_zero_rates_produce_only_clean_background(self):
        events = _events(200, deviation_rate=0.0, novel_rate=0.0, inject_cases=False)
        assert {event.kind for event in events} == {"background"}

    def test_deviation_rate_of_one_deviates_everything(self):
        events = _events(100, deviation_rate=1.0, novel_rate=0.0, inject_cases=False)
        assert all(event.kind == "deviation" for event in events)
        assert all(event.deviated_fields for event in events)

    def test_novel_rate_of_one_makes_every_key_unseen(self):
        events = _events(100, deviation_rate=0.0, novel_rate=1.0, inject_cases=False)
        known = {tuple(pattern.identity().values()) for pattern in BACKGROUND_PATTERNS}
        assert all(event.kind == "novel" for event in events)
        assert all(pattern_identity_key(event.alert) not in known for event in events)

    def test_deviations_break_at_most_two_fields(self):
        # One or two, not all five — see _deviate()'s docstring. All five
        # is case #6's job, and a stream that always maxed out would never
        # test the harder single-field detection.
        events = _events(300, deviation_rate=1.0, novel_rate=0.0, inject_cases=False)
        assert {len(event.deviated_fields) for event in events} <= {1, 2}

    def test_deviated_fields_actually_differ_from_the_pattern_template(self):
        by_key = {tuple(p.identity().values()): p for p in BACKGROUND_PATTERNS}
        for event in _events(200, deviation_rate=1.0, novel_rate=0.0, inject_cases=False):
            pattern = by_key[pattern_identity_key(event.alert)]
            for name in event.deviated_fields:
                assert event.alert[name] != pattern.structure[name]

    def test_undeviated_fields_are_left_alone(self):
        by_key = {tuple(p.identity().values()): p for p in BACKGROUND_PATTERNS}
        for event in _events(200, deviation_rate=1.0, novel_rate=0.0, inject_cases=False):
            pattern = by_key[pattern_identity_key(event.alert)]
            for name in DIFFABLE_FIELDS:
                if name not in event.deviated_fields:
                    assert event.alert[name] == pattern.structure[name]

    def test_background_traffic_spans_multiple_patterns(self):
        keys = {pattern_identity_key(e.alert) for e in _events(300, inject_cases=False)}
        assert len(keys) > 1

    def test_background_events_use_their_own_patterns_hosts_and_users(self):
        by_key = {tuple(p.identity().values()): p for p in BACKGROUND_PATTERNS}
        for event in _events(300, deviation_rate=0.0, novel_rate=0.0, inject_cases=False):
            pattern = by_key[pattern_identity_key(event.alert)]
            assert event.alert["host"] in pattern.hosts
            assert event.alert["user"] in pattern.users


class TestConfigValidation:
    @pytest.mark.parametrize("rate", [-0.1, 1.1, 5.0])
    def test_out_of_range_deviation_rate_is_rejected(self, rate):
        with pytest.raises(EventStreamConfigError, match="deviation_rate"):
            _events(1, deviation_rate=rate)

    @pytest.mark.parametrize("rate", [-0.1, 1.1])
    def test_out_of_range_novel_rate_is_rejected(self, rate):
        with pytest.raises(EventStreamConfigError, match="novel_rate"):
            _events(1, novel_rate=rate)

    def test_rates_summing_above_one_are_rejected(self):
        with pytest.raises(EventStreamConfigError, match="must not exceed"):
            _events(1, deviation_rate=0.7, novel_rate=0.5)

    @pytest.mark.parametrize("interval", [0, -1])
    def test_non_positive_case_interval_is_rejected(self, interval):
        with pytest.raises(EventStreamConfigError, match="case_interval"):
            _events(1, case_interval=interval)

    def test_validation_happens_before_the_first_event(self):
        # generate_events is a generator, so a naive implementation would
        # defer validation until first iteration and let a bad config
        # survive an unconsumed call. Constructing it must raise.
        with pytest.raises(EventStreamConfigError):
            list(generate_events(0, deviation_rate=2.0))

    def test_zero_count_yields_nothing(self):
        assert _events(0) == []


class TestValidateAlert:
    def test_complete_alert_has_no_missing_fields(self, baseline_alert):
        assert validate_alert(baseline_alert) == []

    def test_reports_every_missing_field_not_just_the_first(self):
        missing = validate_alert({"detection_rule_id": "R"})
        assert "parent_image" in missing
        assert "egress_follows_access" in missing
        assert len(missing) == len(IDENTITY_KEY_FIELDS) + len(DIFFABLE_FIELDS) - 1

    def test_extra_fields_are_not_flagged(self, baseline_alert):
        assert validate_alert({**baseline_alert, "event_id": 4688}) == []
