"""
Tests for vor_agents.blast_radius — table matching, the UNSCORED_DEFAULT
safety net, and the MEDIUM/LOW human-review gate.
"""

import pytest

from vor_agents.blast_radius import (
    BLAST_RADIUS_PROPOSALS_COLLECTION,
    BLAST_RADIUS_TABLE_COLLECTION,
    CRITICAL,
    HIGH,
    LOW,
    MEDIUM,
    UNSCORED_DEFAULT,
    estimate_blast_radius,
    propose_blast_radius,
    reset_table_cache,
)


class TestEstimateBlastRadius:
    def setup_method(self):
        reset_table_cache()

    def _seed_entry(self, fake_firestore, indicator_type, value, score):
        fake_firestore.collection(BLAST_RADIUS_TABLE_COLLECTION).document(
            f"{indicator_type}:{value}"
        ).set({"indicator_type": indicator_type, "value": value, "score": score})

    def test_known_critical_indicator(self, fake_firestore):
        self._seed_entry(fake_firestore, "parent_image", "lsass.exe", CRITICAL)
        alert = {"parent_image": "lsass.exe"}
        assert estimate_blast_radius(alert, fake_firestore) == CRITICAL

    def test_known_high_indicator(self, fake_firestore):
        self._seed_entry(fake_firestore, "parent_image", "w3wp.exe", HIGH)
        alert = {"parent_image": "w3wp.exe"}
        assert estimate_blast_radius(alert, fake_firestore) == HIGH

    def test_unmatched_alert_gets_unscored_default_not_low(self, fake_firestore):
        """The whole point of UNSCORED_DEFAULT: an unassessed pattern
        must never be silently treated as safe. Explicitly asserting it's
        HIGH, not LOW and not zero."""
        self._seed_entry(fake_firestore, "parent_image", "lsass.exe", CRITICAL)
        alert = {"parent_image": "totally_unknown_process.exe"}
        result = estimate_blast_radius(alert, fake_firestore)
        assert result == UNSCORED_DEFAULT
        assert result == HIGH
        assert result != LOW
        assert result != 0.0

    def test_multiple_matches_take_the_max(self, fake_firestore):
        """Worst-case-wins: if an alert matches both a HIGH indicator and
        a CRITICAL one, CRITICAL should win, not an average."""
        self._seed_entry(fake_firestore, "parent_image", "lsass.exe", CRITICAL)
        self._seed_entry(fake_firestore, "endpoint_family", "ToolPane_admin", CRITICAL)
        alert = {"parent_image": "lsass.exe", "endpoint_family": "ToolPane_admin"}
        assert estimate_blast_radius(alert, fake_firestore) == CRITICAL


class TestEstimateBlastRadiusFromFirestore:
    def setup_method(self):
        reset_table_cache()

    def _seed_entry(self, fake_firestore, indicator_type, value, score):
        fake_firestore.collection(BLAST_RADIUS_TABLE_COLLECTION).document(
            f"{indicator_type}:{value}"
        ).set({"indicator_type": indicator_type, "value": value, "score": score})

    def test_matches_seeded_entry(self, fake_firestore):
        self._seed_entry(fake_firestore, "parent_image", "lsass.exe", 0.95)

        result = estimate_blast_radius({"parent_image": "lsass.exe"}, fake_firestore)

        assert result == 0.95

    def test_no_match_falls_back_to_unscored_default(self, fake_firestore):
        self._seed_entry(fake_firestore, "parent_image", "lsass.exe", 0.95)

        result = estimate_blast_radius({"parent_image": "notepad.exe"}, fake_firestore)

        assert result == UNSCORED_DEFAULT

    def test_worst_case_wins_on_multiple_matches(self, fake_firestore):
        self._seed_entry(fake_firestore, "parent_image", "w3wp.exe", 0.75)
        self._seed_entry(fake_firestore, "endpoint_family", "ToolPane_admin", 0.95)

        result = estimate_blast_radius(
            {"parent_image": "w3wp.exe", "endpoint_family": "ToolPane_admin"}, fake_firestore
        )

        assert result == 0.95

    def test_cache_serves_repeated_calls_without_rereading(self, fake_firestore):
        self._seed_entry(fake_firestore, "parent_image", "lsass.exe", 0.95)
        estimate_blast_radius({"parent_image": "lsass.exe"}, fake_firestore)

        # Mutate the underlying store directly (bypassing the table's own
        # write path) -- if the cache is honored, this change is invisible
        # until the cache expires/is invalidated.
        fake_firestore.collection(BLAST_RADIUS_TABLE_COLLECTION).document(
            "parent_image:lsass.exe"
        ).set({"indicator_type": "parent_image", "value": "lsass.exe", "score": 0.10})

        result = estimate_blast_radius({"parent_image": "lsass.exe"}, fake_firestore)

        assert result == 0.95  # still the cached value, not the mutated one

    def test_stale_cache_served_on_refresh_failure(self, fake_firestore, monkeypatch):
        self._seed_entry(fake_firestore, "parent_image", "lsass.exe", 0.95)
        estimate_blast_radius({"parent_image": "lsass.exe"}, fake_firestore)  # populates cache

        monkeypatch.setattr("vor_agents.blast_radius._TABLE_CACHE_TTL_SECONDS", 0)

        class _BoomCollection:
            def stream(self):
                raise RuntimeError("Firestore unavailable")

        class _BoomClient:
            def collection(self, name):
                return _BoomCollection()

        result = estimate_blast_radius({"parent_image": "lsass.exe"}, _BoomClient())

        assert result == 0.95  # stale cache, not a raised exception

    def test_cold_cache_failure_falls_back_to_unscored_default(self):
        reset_table_cache()

        class _BoomCollection:
            def stream(self):
                raise RuntimeError("Firestore unavailable")

        class _BoomClient:
            def collection(self, name):
                return _BoomCollection()

        result = estimate_blast_radius({"parent_image": "lsass.exe"}, _BoomClient())

        assert result == UNSCORED_DEFAULT


class TestProposeBlastRadius:
    def setup_method(self):
        reset_table_cache()

    def test_critical_proposal_does_not_require_review(self, fake_firestore):
        proposal = propose_blast_radius(
            identity_key=("rule", "proc.exe", "child.exe", "family"),
            proposed_tier="CRITICAL",
            proposed_score=CRITICAL,
            cited_indicators=["parent_image=proc.exe"],
            rationale="test",
            firestore_client=fake_firestore,
        )
        assert proposal["requires_review"] is False
        assert proposal["status"] == "committed"

    def test_medium_proposal_requires_review(self, fake_firestore):
        """The safety-critical assertion: a MEDIUM proposal (the
        direction that REDUCES scrutiny) must always be flagged for
        human review, matching the DOWNGRADE/RECOMMEND_UPGRADE asymmetry
        used everywhere else in this system."""
        proposal = propose_blast_radius(
            identity_key=("rule", "proc.exe", "child.exe", "family"),
            proposed_tier="MEDIUM",
            proposed_score=MEDIUM,
            cited_indicators=["parent_image=proc.exe"],
            rationale="test",
            firestore_client=fake_firestore,
        )
        assert proposal["requires_review"] is True

    def test_low_proposal_requires_review(self, fake_firestore):
        proposal = propose_blast_radius(
            identity_key=("rule", "proc.exe", "child.exe", "family"),
            proposed_tier="LOW",
            proposed_score=LOW,
            cited_indicators=["parent_image=proc.exe"],
            rationale="test",
            firestore_client=fake_firestore,
        )
        assert proposal["requires_review"] is True

    # test_proposal_never_writes_to_the_table removed here: it asserted
    # against the module-level BLAST_RADIUS_TABLE dict, which Task 2 of
    # docs/superpowers/plans/2026-08-24-blast-radius-firestore.md removes
    # entirely (replaced by the Firestore-backed table). Its intent --
    # proposing doesn't silently commit -- is re-covered, more precisely,
    # by Task 3's TestProposeBlastRadiusStorage
    # .test_medium/low_proposal_does_not_auto_commit, which assert via
    # estimate_blast_radius() against the real fake_firestore table.

    def test_unknown_tier_rejected(self, fake_firestore):
        """Regression coverage: an unknown proposed_tier used to fall
        through requires_review's `in ("MEDIUM", "LOW")` check as False,
        silently treating a typo'd or made-up tier as not needing human
        review — the dangerous direction. Must now raise."""
        with pytest.raises(ValueError, match="Unknown blast-radius tier"):
            propose_blast_radius(
                identity_key=("rule", "proc.exe", "child.exe", "family"),
                proposed_tier="SEVERE",
                proposed_score=0.99,
                cited_indicators=["test=x"],
                rationale="test",
                firestore_client=fake_firestore,
            )

    def test_score_outside_tier_range_rejected(self, fake_firestore):
        """A tier/score mismatch (e.g. LOW tier with a CRITICAL-range
        score) used to be silently accepted, which could mislead a human
        reviewer relying on requires_review's tier-only gate while the
        score itself says something entirely different."""
        with pytest.raises(ValueError, match="outside LOW's documented range"):
            propose_blast_radius(
                identity_key=("rule", "proc.exe", "child.exe", "family"),
                proposed_tier="LOW",
                proposed_score=CRITICAL,
                cited_indicators=["test=x"],
                rationale="test",
                firestore_client=fake_firestore,
            )

    def test_score_at_tier_boundary_accepted(self, fake_firestore):
        """Boundary values are inclusive per BLAST_RADIUS_PLAYBOOK.md's
        "0.90-1.0" style ranges — 0.90 is a valid CRITICAL score, not a
        rejected edge case."""
        proposal = propose_blast_radius(
            identity_key=("rule", "proc.exe", "child.exe", "family"),
            proposed_tier="CRITICAL",
            proposed_score=0.90,
            cited_indicators=["parent_image=proc.exe"],
            rationale="test",
            firestore_client=fake_firestore,
        )
        assert proposal["proposed_score"] == 0.90


class TestProposeBlastRadiusStorage:
    def setup_method(self):
        reset_table_cache()

    def test_critical_proposal_auto_commits(self, fake_firestore):
        result = propose_blast_radius(
            ("rule", "p.exe", "c.exe", "family"),
            "CRITICAL",
            0.95,
            ["parent_image=p.exe"],
            "reads credential material",
            fake_firestore,
        )

        assert result["status"] == "committed"
        score = estimate_blast_radius({"parent_image": "p.exe"}, fake_firestore)
        assert score == 0.95

    def test_high_proposal_auto_commits(self, fake_firestore):
        result = propose_blast_radius(
            ("rule", "p.exe", "c.exe", "family"),
            "HIGH",
            0.75,
            ["parent_image=p.exe"],
            "internet-facing service",
            fake_firestore,
        )

        assert result["status"] == "committed"

    def test_medium_proposal_does_not_auto_commit(self, fake_firestore):
        result = propose_blast_radius(
            ("rule", "p.exe", "c.exe", "family"),
            "MEDIUM",
            0.45,
            ["parent_image=p.exe"],
            "internal service account",
            fake_firestore,
        )

        assert result["status"] == "pending_human_review"
        score = estimate_blast_radius({"parent_image": "p.exe"}, fake_firestore)
        assert score == UNSCORED_DEFAULT  # not committed to the table

    def test_low_proposal_does_not_auto_commit(self, fake_firestore):
        result = propose_blast_radius(
            ("rule", "p.exe", "c.exe", "family"),
            "LOW",
            0.15,
            ["parent_image=p.exe"],
            "ordinary user-context app",
            fake_firestore,
        )

        assert result["status"] == "pending_human_review"

    def test_proposal_is_persisted_and_retrievable(self, fake_firestore):
        result = propose_blast_radius(
            ("rule", "p.exe", "c.exe", "family"),
            "MEDIUM",
            0.45,
            ["parent_image=p.exe"],
            "internal service account",
            fake_firestore,
        )

        doc = (
            fake_firestore.collection(BLAST_RADIUS_PROPOSALS_COLLECTION)
            .document(result["proposal_id"])
            .get()
        )
        assert doc.exists
        assert doc.to_dict()["proposed_tier"] == "MEDIUM"

    def test_unknown_tier_still_raises_before_any_write(self, fake_firestore):
        with pytest.raises(ValueError):
            propose_blast_radius(
                ("rule", "p.exe", "c.exe", "family"), "SEVERE", 0.5, [], "x", fake_firestore
            )
        assert list(fake_firestore.collection(BLAST_RADIUS_PROPOSALS_COLLECTION).stream()) == []
