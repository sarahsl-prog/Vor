"""Tests for vor_agents.evidence_diversity."""

from vor_agents.evidence_diversity import evidence_diversity_score


def test_empty_list_returns_zero():
    assert evidence_diversity_score([]) == 0.0


def test_diverse_instances_score_high(diverse_confirmed_instances):
    score = evidence_diversity_score(diverse_confirmed_instances)
    assert score >= 0.5


def test_low_diversity_instances_score_low(low_diversity_confirmed_instances):
    """Same host, same user, same hour, 3 times — should score near 0,
    this is the exact 'weak evidence dressed up as strong' failure mode
    the auditor prompt and the graduation gate both exist to catch."""
    score = evidence_diversity_score(low_diversity_confirmed_instances)
    assert score < 0.5


def test_missing_dimensions_degrades_gracefully():
    """Instances with no host/user/timestamp fields at all shouldn't
    crash — score should just be based on whatever dimensions ARE
    present, or 0.0 if none are."""
    instances = [{"foo": "bar"}, {"foo": "baz"}]
    score = evidence_diversity_score(instances)
    assert score == 0.0


def test_single_instance_has_max_diversity_per_instance():
    """One instance, one host, one user -> distinct/n ratio is 1/1 = 1.0
    per dimension. This isn't really 'diverse' in a meaningful sense, but
    the function is honest about what it measures (ratio, not absolute
    count) — graduation's count requirement is what stops a single
    instance from graduating, not this function pretending otherwise."""
    instances = [{"host": "SRV-01", "user": "jsmith", "timestamp": "2026-08-01T09:00:00Z"}]
    score = evidence_diversity_score(instances)
    assert score == 1.0


def test_malformed_timestamp_not_counted_as_a_distinct_hour():
    """Regression coverage: the function used to slice timestamp[11:13]
    on any string >= 13 chars regardless of format, so
    "2026-08-01T99:00:00Z" counted "99" as a real, distinct hour —
    nonsensical but silently inflating diversity. Must now be ignored via
    real ISO-8601 parsing, same as any other unparseable timestamp."""
    instances = [
        {"host": "SRV-01", "user": "jsmith", "timestamp": "2026-08-01T99:00:00Z"},
        {"host": "SRV-01", "user": "jsmith", "timestamp": "2026-08-01T99:00:00Z"},
        {"host": "SRV-01", "user": "jsmith", "timestamp": "2026-08-01T99:00:00Z"},
    ]
    score = evidence_diversity_score(instances)
    # host/user both fully repeated (1 distinct value / 3 instances =
    # 1/3 each); the malformed timestamp contributes nothing (no valid
    # hours parsed at all, so the "hours" dimension is dropped entirely
    # rather than averaged in as a bogus extra distinct value).
    assert score == 1 / 3


def test_valid_and_malformed_timestamps_mixed_only_valid_counted():
    instances = [
        {"host": f"h{i}", "user": f"u{i}", "timestamp": ts}
        for i, ts in enumerate(
            [
                "2026-08-01T09:00:00Z",
                "2026-08-01T14:00:00Z",
                "not-a-timestamp-at-all",
            ]
        )
    ]
    score = evidence_diversity_score(instances)
    # host/user both fully diverse (3/3 = 1.0 each); hours dimension sees
    # only the 2 valid timestamps, both distinct -> 2/3 ratio, not 3/3
    # (which would happen if the malformed one silently produced its own
    # bogus "hour").
    assert score == (1.0 + 1.0 + 2 / 3) / 3


def test_non_hashable_host_value_is_skipped_not_a_crash():
    """Regression for Code-review-Aug25 3.2: a bad ingestion pipeline
    storing a list/dict in `host` or `user` used to raise TypeError
    from the set comprehension -- this function is otherwise designed
    to degrade gracefully (see module docstring), so a malformed
    dimension should be skipped, not fatal."""
    instances = [
        {"host": ["not", "hashable"], "user": "jsmith", "timestamp": "2026-08-01T09:00:00Z"},
        {"host": "SRV-01", "user": "mjones", "timestamp": "2026-08-02T10:00:00Z"},
    ]

    score = evidence_diversity_score(instances)

    assert isinstance(score, float)
    assert 0.0 <= score <= 1.0
