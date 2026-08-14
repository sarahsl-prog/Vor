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
