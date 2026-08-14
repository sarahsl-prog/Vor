"""Tests for vor_agents.audit_targets — deterministic priority scoring."""

from vor_agents.audit_targets import select_audit_targets


def _pattern(days=0, diversity=1.0, blast_radius=0.0, key="p"):
    return {
        "identity_key": key,
        "days_since_last_review": days,
        "evidence_diversity_score": diversity,
        "blast_radius_estimate": blast_radius,
    }


def test_higher_blast_radius_prioritized():
    """blast_radius has the heaviest per-unit weight (x3.0) in the
    priority formula. Holding days_since_last_review constant to isolate
    that effect — days_since_last_review is unbounded and can otherwise
    dominate the score regardless of blast radius, which is a real
    property of the current weighting, not something to paper over."""
    patterns = [
        _pattern(days=10, diversity=1.0, blast_radius=0.1, key="low_risk"),
        _pattern(days=10, diversity=1.0, blast_radius=0.95, key="high_risk"),
    ]
    result = select_audit_targets(patterns)
    assert result[0]["identity_key"] == "high_risk"


def test_thin_evidence_prioritized_over_thick():
    patterns = [
        _pattern(days=10, diversity=0.9, blast_radius=0.5, key="thick_evidence"),
        _pattern(days=10, diversity=0.1, blast_radius=0.5, key="thin_evidence"),
    ]
    result = select_audit_targets(patterns)
    assert result[0]["identity_key"] == "thin_evidence"


def test_max_targets_respected():
    patterns = [_pattern(key=f"p{i}") for i in range(20)]
    result = select_audit_targets(patterns, max_targets=5)
    assert len(result) == 5


def test_empty_input_returns_empty():
    assert select_audit_targets([]) == []
