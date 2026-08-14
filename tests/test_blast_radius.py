"""
Tests for vor_agents.blast_radius — table matching, the UNSCORED_DEFAULT
safety net, and the MEDIUM/LOW human-review gate.
"""

from vor_agents.blast_radius import (
    CRITICAL,
    HIGH,
    LOW,
    MEDIUM,
    UNSCORED_DEFAULT,
    estimate_blast_radius,
    propose_blast_radius,
)


class TestEstimateBlastRadius:
    def test_known_critical_indicator(self):
        alert = {"parent_image": "lsass.exe"}
        assert estimate_blast_radius(alert) == CRITICAL

    def test_known_high_indicator(self):
        alert = {"parent_image": "w3wp.exe"}
        assert estimate_blast_radius(alert) == HIGH

    def test_unmatched_alert_gets_unscored_default_not_low(self):
        """The whole point of UNSCORED_DEFAULT: an unassessed pattern
        must never be silently treated as safe. Explicitly asserting it's
        HIGH, not LOW and not zero."""
        alert = {"parent_image": "totally_unknown_process.exe"}
        result = estimate_blast_radius(alert)
        assert result == UNSCORED_DEFAULT
        assert result == HIGH
        assert result != LOW
        assert result != 0.0

    def test_multiple_matches_take_the_max(self):
        """Worst-case-wins: if an alert matches both a HIGH indicator and
        a CRITICAL one, CRITICAL should win, not an average."""
        alert = {"parent_image": "lsass.exe", "endpoint_family": "ToolPane_admin"}
        assert estimate_blast_radius(alert) == CRITICAL


class TestProposeBlastRadius:
    def test_critical_proposal_does_not_require_review(self):
        proposal = propose_blast_radius(
            identity_key=("rule", "proc.exe", "child.exe", "family"),
            proposed_tier="CRITICAL",
            proposed_score=CRITICAL,
            cited_indicators=["credential access"],
            rationale="test",
        )
        assert proposal["requires_review"] is False
        assert proposal["status"] == "pending_human_review"

    def test_medium_proposal_requires_review(self):
        """The safety-critical assertion: a MEDIUM proposal (the
        direction that REDUCES scrutiny) must always be flagged for
        human review, matching the DOWNGRADE/RECOMMEND_UPGRADE asymmetry
        used everywhere else in this system."""
        proposal = propose_blast_radius(
            identity_key=("rule", "proc.exe", "child.exe", "family"),
            proposed_tier="MEDIUM",
            proposed_score=MEDIUM,
            cited_indicators=["internal only"],
            rationale="test",
        )
        assert proposal["requires_review"] is True

    def test_low_proposal_requires_review(self):
        proposal = propose_blast_radius(
            identity_key=("rule", "proc.exe", "child.exe", "family"),
            proposed_tier="LOW",
            proposed_score=LOW,
            cited_indicators=["standard user context"],
            rationale="test",
        )
        assert proposal["requires_review"] is True

    def test_proposal_never_writes_to_the_table(self):
        """A proposal must be inert — it returns a dict, it does not
        mutate BLAST_RADIUS_TABLE. Verify the table is unaffected."""
        from vor_agents.blast_radius import BLAST_RADIUS_TABLE
        before = dict(BLAST_RADIUS_TABLE)
        propose_blast_radius(
            identity_key=("rule", "brand_new_process.exe", "child.exe", "family"),
            proposed_tier="LOW",
            proposed_score=LOW,
            cited_indicators=["test"],
            rationale="test",
        )
        assert BLAST_RADIUS_TABLE == before
