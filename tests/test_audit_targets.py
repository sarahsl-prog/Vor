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


def test_negative_days_since_review_clamped_not_penalized():
    """Regression coverage: a negative days_since_last_review (clock
    skew — last_reviewed_at in the future relative to whatever computed
    it) must not rank a pattern BELOW one that's never been audited at
    all. Clamped to 0, not left negative, so the skewed pattern is at
    worst treated as freshly-reviewed, never as lower-priority than
    genuinely-stale evidence."""
    patterns = [
        _pattern(days=-5, diversity=1.0, blast_radius=0.5, key="clock_skewed"),
        _pattern(days=0, diversity=1.0, blast_radius=0.5, key="freshly_reviewed"),
    ]
    result = select_audit_targets(patterns)
    # Both clamp to days_since=0 with identical diversity/blast_radius —
    # same priority score, tie-broken deterministically below, not a
    # ranking assertion here.
    assert {p["identity_key"] for p in result} == {"clock_skewed", "freshly_reviewed"}


def test_tied_priority_breaks_deterministically_on_identity_key():
    """Two patterns with identical days/diversity/blast_radius must
    still sort in a stable, reproducible order across repeated calls —
    not left to whatever order they happened to arrive in (Firestore
    query result order for equal-priority docs isn't guaranteed)."""
    patterns = [
        _pattern(days=5, diversity=0.5, blast_radius=0.5, key="zzz_pattern"),
        _pattern(days=5, diversity=0.5, blast_radius=0.5, key="aaa_pattern"),
    ]
    first_call = [p["identity_key"] for p in select_audit_targets(patterns)]
    second_call = [p["identity_key"] for p in select_audit_targets(list(reversed(patterns)))]
    assert first_call == second_call
