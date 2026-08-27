"""
Tests for vor_agents.orchestrator — specifically the asymmetric
reconciliation between deterministic diffing and the model's own reported
deviations. This is the single most safety-critical piece of logic in the
whole system: it's the last line of defense against a model silently
missing a real deviation and autonomously suppressing something it
shouldn't.

_run_agent (the actual LLM call) is mocked throughout — these are unit
tests for the reconciliation logic, not integration tests against a real
model. Real-model behavior belongs in a separate, explicitly-marked
integration suite, not here.
"""

from unittest.mock import AsyncMock, patch

import pytest

from vor_agents.enrichment import (
    CONFIDENCE_COLLECTION,
    _doc_id,
    record_confirmed_negative,
)
from vor_agents.identity import pattern_identity_key
from vor_agents.orchestrator import (
    DEFAULT_SWEEP_MAX_TARGETS,
    SWEEP_MAX_TARGETS_ENV_VAR,
    AgentOutputError,
    _deviation_field_names,
    _merge_deviations,
    _run_agent,
    audit_pattern,
    classify_alert,
    run_scheduled_sweep,
)
from vor_agents.review_flag import NEEDS_ATTENTION_COLLECTION, mark_under_review


async def _graduate_baseline_pattern(fake_firestore, diverse_confirmed_instances):
    for instance in diverse_confirmed_instances:
        record_confirmed_negative(instance, fake_firestore)


@pytest.mark.asyncio
class TestReconciliation:
    async def test_model_correctly_suppresses_no_override(
        self, fake_firestore, baseline_alert, diverse_confirmed_instances
    ):
        """Model says SUPPRESS, zero deviations, ground truth agrees
        (baseline_alert matches the template exactly) — no override."""
        await _graduate_baseline_pattern(fake_firestore, diverse_confirmed_instances)

        fake_model_response = {
            "decision": "SUPPRESS",
            "matched_pattern_id": "test",
            "uncertain_reason": "not_applicable",
            "structural_deviations_found": [],
            "reasoning": "No deviations found, template matches.",
        }
        with patch(
            "vor_agents.orchestrator._run_agent",
            new=AsyncMock(return_value=fake_model_response),
        ):
            result, _identity_key = await classify_alert(baseline_alert, fake_firestore)

        assert result.decision == "SUPPRESS"
        assert "override" not in result.reasoning.lower()

    async def test_model_misses_real_deviation_gets_overridden(
        self, fake_firestore, field_level_drift_alert, diverse_confirmed_instances
    ):
        """THE critical test. field_level_drift_alert has 5 real
        deviations from the graduated baseline template. The model
        hallucinates/misses all of them and says SUPPRESS anyway — this
        must be overridden to ESCALATE, automatically, in code."""
        await _graduate_baseline_pattern(fake_firestore, diverse_confirmed_instances)

        fake_model_response = {
            "decision": "SUPPRESS",  # model wrongly missed the deviations
            "matched_pattern_id": "test",
            "uncertain_reason": "not_applicable",
            "structural_deviations_found": [],  # model reported nothing
            "reasoning": "Looks fine to me.",
        }
        with patch(
            "vor_agents.orchestrator._run_agent",
            new=AsyncMock(return_value=fake_model_response),
        ):
            result, _identity_key = await classify_alert(field_level_drift_alert, fake_firestore)

        assert result.decision == "ESCALATE"
        assert "override" in result.reasoning.lower()
        assert len(result.structural_deviations_found) >= 5

    async def test_model_more_cautious_than_ground_truth_not_touched(
        self, fake_firestore, baseline_alert, diverse_confirmed_instances
    ):
        """Model reports ESCALATE with a deviation ground truth didn't
        find — the SAFE direction. Must NOT be overridden; the model's
        (more cautious) decision stands as-is."""
        await _graduate_baseline_pattern(fake_firestore, diverse_confirmed_instances)

        fake_model_response = {
            "decision": "ESCALATE",
            "matched_pattern_id": "test",
            "uncertain_reason": "not_applicable",
            "structural_deviations_found": [
                {"field": "some_field", "template": "X", "observed": "Y"}
            ],
            "reasoning": "This looks suspicious for reasons beyond the diffed fields.",
        }
        with patch(
            "vor_agents.orchestrator._run_agent",
            new=AsyncMock(return_value=fake_model_response),
        ):
            result, _identity_key = await classify_alert(baseline_alert, fake_firestore)

        assert result.decision == "ESCALATE"
        assert "override" not in result.reasoning.lower()

    async def test_no_history_skips_reconciliation_entirely(self, fake_firestore, baseline_alert):
        """No confirmed template exists yet -> precomputed_deviations is
        empty -> reconciliation block should not run at all (guarded by
        'if precomputed_deviations:'). Model's UNCERTAIN stands untouched."""
        fake_model_response = {
            "decision": "UNCERTAIN",
            "matched_pattern_id": None,
            "uncertain_reason": "no_history",
            "structural_deviations_found": [],
            "reasoning": "No prior history for this pattern.",
        }
        with patch(
            "vor_agents.orchestrator._run_agent",
            new=AsyncMock(return_value=fake_model_response),
        ):
            result, _identity_key = await classify_alert(baseline_alert, fake_firestore)

        assert result.decision == "UNCERTAIN"
        assert result.uncertain_reason == "no_history"


@pytest.mark.asyncio
class TestSelfConsistency:
    """
    The classifier's own prompt (rule 4 in CLASSIFIER_SYSTEM_PROMPT) says
    ANY non-empty structural_deviations_found must force ESCALATE. Nothing
    previously enforced that internally -- the reconciliation block only
    cross-checked the model's reported deviations against the
    deterministic diff, and was skipped entirely when the deterministic
    diff found nothing (the 'if precomputed_deviations:' guard). A model
    that reports a deviation not present in the deterministic set (e.g.
    hallucinated, or on a non-diffable field) but still emits SUPPRESS
    sailed through untouched. This must be caught independent of what the
    deterministic diff found.
    """

    async def test_supress_with_self_reported_deviation_gets_overridden(
        self, fake_firestore, baseline_alert, diverse_confirmed_instances
    ):
        """baseline_alert matches the graduated template exactly, so the
        deterministic diff finds NOTHING (precomputed_deviations == []),
        meaning the ground-truth reconciliation block wouldn't even run.
        The model self-contradicts anyway: SUPPRESS decision, but a
        non-empty structural_deviations_found -- must still be overridden
        to ESCALATE."""
        await _graduate_baseline_pattern(fake_firestore, diverse_confirmed_instances)

        fake_model_response = {
            "decision": "SUPPRESS",
            "matched_pattern_id": "test",
            "uncertain_reason": "not_applicable",
            "structural_deviations_found": [
                {"field": "integrity_level", "template": "Medium", "observed": "High"}
            ],
            "reasoning": "Reported a deviation but suppressed anyway.",
        }
        with patch(
            "vor_agents.orchestrator._run_agent",
            new=AsyncMock(return_value=fake_model_response),
        ):
            result, _identity_key = await classify_alert(baseline_alert, fake_firestore)

        assert result.decision == "ESCALATE"
        assert "override" in result.reasoning.lower()

    async def test_escalate_with_self_reported_deviation_not_touched(
        self, fake_firestore, baseline_alert, diverse_confirmed_instances
    ):
        """Sanity check: a self-consistent ESCALATE (deviation reported,
        decision already ESCALATE) must not be needlessly re-flagged."""
        await _graduate_baseline_pattern(fake_firestore, diverse_confirmed_instances)

        fake_model_response = {
            "decision": "ESCALATE",
            "matched_pattern_id": "test",
            "uncertain_reason": "not_applicable",
            "structural_deviations_found": [
                {"field": "integrity_level", "template": "Medium", "observed": "High"}
            ],
            "reasoning": "Deviation found, escalating as instructed.",
        }
        with patch(
            "vor_agents.orchestrator._run_agent",
            new=AsyncMock(return_value=fake_model_response),
        ):
            result, _identity_key = await classify_alert(baseline_alert, fake_firestore)

        assert result.decision == "ESCALATE"
        assert "override" not in result.reasoning.lower()


class TestDeviationFieldNames:
    """
    Regression coverage: a deviation object missing its "field" key (the
    model not following its own output schema) used to be impossible to
    represent under the old free-string format's failure mode (a
    colon-less string) -- now the equivalent malformed case is a dict
    with no "field" key. Must be skipped (and logged), not guessed at.
    """

    def test_well_formed_objects_extract_field_name(self):
        result = _deviation_field_names(
            [
                {"field": "integrity_level", "template": "Medium", "observed": "High"},
                {"field": "file_access_mode", "template": "read", "observed": "write"},
            ]
        )
        assert result == {"integrity_level", "file_access_mode"}

    def test_object_missing_field_key_is_skipped_not_treated_as_a_field_name(self):
        result = _deviation_field_names(
            [
                {"template": "Medium", "observed": "High"},
            ]
        )
        assert result == set()

    def test_mix_of_well_formed_and_malformed_keeps_only_well_formed(self):
        result = _deviation_field_names(
            [
                {"field": "integrity_level", "template": "Medium", "observed": "High"},
                {"template": "no field key here"},
                {},
            ]
        )
        assert result == {"integrity_level"}


class TestMergeDeviations:
    """
    Direct unit coverage for _merge_deviations' de-duplication logic. It's
    only reached from classify_alert()'s ground-truth-missed override
    (precomputed_deviations non-empty AND missed_by_model non-empty AND
    decision==SUPPRESS), and the existing reconciliation test that reaches
    it always passes an empty list as one of the two groups -- so the
    actual dedup/merge branch never previously executed under test.
    """

    def test_identical_deviations_from_different_groups_collapse_to_one(self):
        dup = {"field": "f", "template": "a", "observed": "b"}
        result = _merge_deviations([dup], [dict(dup)])
        assert result == [dup]

    def test_same_field_different_observed_value_both_survive(self):
        """The dangerous case to get wrong: two deviations sharing a field
        name but differing in what was actually observed are two distinct,
        real deviations -- collapsing them into one would silently drop
        evidence."""
        first = {"field": "f", "template": "a", "observed": "b"}
        second = {"field": "f", "template": "a", "observed": "c"}
        result = _merge_deviations([first], [second])
        assert len(result) == 2
        assert first in result
        assert second in result

    def test_output_ordering_is_deterministic_regardless_of_group_order(self):
        first = {"field": "f", "template": "a", "observed": "b"}
        second = {"field": "f", "template": "a", "observed": "c"}
        result_first_then_second = _merge_deviations([first], [second])
        result_second_then_first = _merge_deviations([second], [first])
        assert result_first_then_second == result_second_then_first == [first, second]


@pytest.mark.asyncio
class TestUnderReviewBlocksSuppress:
    """
    Regression coverage for the "model non-compliance during an active
    audit" gap: the classifier prompt tells the model to treat
    under_review=True as provisional, but nothing previously checked that
    in code. A non-compliant/hallucinating model returning SUPPRESS for a
    pattern currently under audit sailed through untouched — exactly the
    burst-replay race under_review exists to close, just closed by
    convention instead of code. classify_alert() must now force UNCERTAIN
    regardless of what the model says.
    """

    async def test_suppress_overridden_to_uncertain_when_under_review(
        self, fake_firestore, baseline_alert, diverse_confirmed_instances
    ):
        await _graduate_baseline_pattern(fake_firestore, diverse_confirmed_instances)
        identity_key = ("SharePoint_ToolPane_Rule", "w3wp.exe", "csc.exe", "ToolPane_admin")
        mark_under_review(identity_key, fake_firestore)

        fake_model_response = {
            "decision": "SUPPRESS",  # non-compliant: model ignored under_review
            "matched_pattern_id": "test",
            "uncertain_reason": "not_applicable",
            "structural_deviations_found": [],
            "reasoning": "Matches template.",
        }
        with patch(
            "vor_agents.orchestrator._run_agent",
            new=AsyncMock(return_value=fake_model_response),
        ):
            result, _identity_key = await classify_alert(baseline_alert, fake_firestore)

        assert result.decision == "UNCERTAIN"
        assert result.uncertain_reason == "under_review"
        assert "under active audit" in result.reasoning.lower()

    async def test_escalate_while_under_review_left_untouched(
        self, fake_firestore, baseline_alert, diverse_confirmed_instances
    ):
        """under_review only blocks SUPPRESS — it's not a blanket override
        of every decision. A model that already says ESCALATE needs no
        correction."""
        await _graduate_baseline_pattern(fake_firestore, diverse_confirmed_instances)
        identity_key = ("SharePoint_ToolPane_Rule", "w3wp.exe", "csc.exe", "ToolPane_admin")
        mark_under_review(identity_key, fake_firestore)

        fake_model_response = {
            "decision": "ESCALATE",
            "matched_pattern_id": "test",
            "uncertain_reason": "not_applicable",
            "structural_deviations_found": [],
            "reasoning": "Escalating out of caution.",
        }
        with patch(
            "vor_agents.orchestrator._run_agent",
            new=AsyncMock(return_value=fake_model_response),
        ):
            result, _identity_key = await classify_alert(baseline_alert, fake_firestore)

        assert result.decision == "ESCALATE"
        assert "under_review" not in result.reasoning.lower()


@pytest.mark.asyncio
class TestProvisionalTierBlocksSuppress:
    """
    Same shape as TestUnderReviewBlocksSuppress, for the analogous gap:
    CLASSIFIER_SYSTEM_PROMPT rule 6 already tells the model a
    provisional-tier pattern hasn't earned autonomous suppression yet, but
    nothing previously checked that in code. A non-compliant/hallucinating
    model returning SUPPRESS for a pattern that hasn't graduated (fewer
    than GRADUATION_THRESHOLD confirmed instances, or below MIN_DIVERSITY
    — identity.py's two-part gate) sailed through untouched.
    classify_alert() must now force UNCERTAIN regardless of what the model
    says.
    """

    async def test_suppress_overridden_to_uncertain_when_provisional(
        self, fake_firestore, baseline_alert, low_diversity_confirmed_instances
    ):
        # 3 instances meets the raw count threshold but fails
        # MIN_DIVERSITY (same host/user/hour repeated) — stays
        # provisional, per low_diversity_confirmed_instances' own
        # docstring.
        for instance in low_diversity_confirmed_instances:
            record_confirmed_negative(instance, fake_firestore)

        fake_model_response = {
            "decision": "SUPPRESS",  # non-compliant: model ignored provisional tier
            "matched_pattern_id": "test",
            "uncertain_reason": "not_applicable",
            "structural_deviations_found": [],
            "reasoning": "Matches template.",
        }
        with patch(
            "vor_agents.orchestrator._run_agent",
            new=AsyncMock(return_value=fake_model_response),
        ):
            result, _identity_key = await classify_alert(baseline_alert, fake_firestore)

        assert result.decision == "UNCERTAIN"
        assert result.uncertain_reason == "graduation_pending"
        assert "provisional" in result.reasoning.lower()

    async def test_escalate_while_provisional_left_untouched(
        self, fake_firestore, baseline_alert, low_diversity_confirmed_instances
    ):
        """provisional tier only blocks SUPPRESS — it's not a blanket
        override of every decision. A model that already says ESCALATE
        needs no correction."""
        for instance in low_diversity_confirmed_instances:
            record_confirmed_negative(instance, fake_firestore)

        fake_model_response = {
            "decision": "ESCALATE",
            "matched_pattern_id": "test",
            "uncertain_reason": "not_applicable",
            "structural_deviations_found": [],
            "reasoning": "Escalating out of caution.",
        }
        with patch(
            "vor_agents.orchestrator._run_agent",
            new=AsyncMock(return_value=fake_model_response),
        ):
            result, _identity_key = await classify_alert(baseline_alert, fake_firestore)

        assert result.decision == "ESCALATE"
        assert "graduation_pending" not in result.reasoning.lower()

    async def test_suppress_not_overridden_once_graduated(
        self, fake_firestore, baseline_alert, diverse_confirmed_instances
    ):
        """Confirms the override is scoped to provisional tier only — a
        graduated (confirmed-tier) pattern's SUPPRESS must NOT be touched
        by this check (TestReconciliation's
        test_model_correctly_suppresses_no_override already covers this
        end-to-end; this test isolates the provisional-tier check
        specifically so a future regression here fails close to the
        cause)."""
        await _graduate_baseline_pattern(fake_firestore, diverse_confirmed_instances)

        fake_model_response = {
            "decision": "SUPPRESS",
            "matched_pattern_id": "test",
            "uncertain_reason": "not_applicable",
            "structural_deviations_found": [],
            "reasoning": "No deviations found, template matches.",
        }
        with patch(
            "vor_agents.orchestrator._run_agent",
            new=AsyncMock(return_value=fake_model_response),
        ):
            result, _identity_key = await classify_alert(baseline_alert, fake_firestore)

        assert result.decision == "SUPPRESS"
        assert "graduation_pending" not in result.reasoning.lower()


@pytest.mark.asyncio
class TestSessionUniqueness:
    """
    Regression coverage for the session-reuse gap: session_id was
    previously derived only from identity_key, so every classify_alert()
    call for the same recurring pattern reused the exact same ADK
    session — accumulating conversation history across calls instead of
    each call getting an independent judgment, contradicting the
    stateless-per-call design stated in enrichment.py's docstring.
    """

    async def test_repeated_calls_for_same_pattern_get_distinct_session_ids(
        self, fake_firestore, baseline_alert
    ):
        fake_model_response = {
            "decision": "UNCERTAIN",
            "matched_pattern_id": None,
            "uncertain_reason": "no_history",
            "structural_deviations_found": [],
            "reasoning": "No prior history for this pattern.",
        }
        mock_run_agent = AsyncMock(return_value=fake_model_response)
        with patch("vor_agents.orchestrator._run_agent", new=mock_run_agent):
            await classify_alert(baseline_alert, fake_firestore)
            await classify_alert(baseline_alert, fake_firestore)

        first_session_id = mock_run_agent.call_args_list[0].kwargs["session_id"]
        second_session_id = mock_run_agent.call_args_list[1].kwargs["session_id"]
        assert first_session_id != second_session_id


@pytest.mark.asyncio
class TestAuditPatternFailureHandling:
    """
    Regression coverage for the stuck-under_review bug: a failed audit
    (model exception, malformed output, Firestore hiccup) previously left
    under_review=True forever, since clear_under_review() only ran after
    a successful model call. audit_pattern() must now clear the flag on
    ANY outcome, success or failure.
    """

    async def test_run_agent_exception_clears_under_review_but_does_not_stamp_review_time(
        self, fake_firestore, diverse_confirmed_instances
    ):
        """A failed audit clears under_review (the original stuck-flag
        fix) but must NOT stamp last_reviewed_at -- a failed audit is not
        a genuine review, and stamping it would make select_audit_targets()
        rank this pattern as freshly-reviewed instead of increasingly
        stale (see final review finding #1 / clear_under_review()'s
        docstring)."""
        identity_key = ("TestRule", "parent.exe", "child.exe", "testfamily")
        for instance in diverse_confirmed_instances:
            record_confirmed_negative(instance, fake_firestore)

        with patch(
            "vor_agents.orchestrator._run_agent",
            new=AsyncMock(side_effect=RuntimeError("model unavailable")),
        ):
            decision = await audit_pattern(identity_key, {}, fake_firestore)

        assert decision.action == "NO_ACTION"
        assert "model unavailable" in decision.reasoning

        doc = fake_firestore.collection(CONFIDENCE_COLLECTION).document(_doc_id(identity_key)).get()
        data = doc.to_dict()
        assert data["under_review"] is False
        assert "last_reviewed_at" not in data

    async def test_invalid_model_output_clears_under_review(
        self, fake_firestore, diverse_confirmed_instances
    ):
        identity_key = ("TestRule", "parent.exe", "child.exe", "testfamily")
        for instance in diverse_confirmed_instances:
            record_confirmed_negative(instance, fake_firestore)

        with patch(
            "vor_agents.orchestrator._run_agent",
            new=AsyncMock(return_value={"action": "NOT_A_REAL_ACTION"}),
        ):
            decision = await audit_pattern(identity_key, {}, fake_firestore)

        assert decision.action == "NO_ACTION"
        assert "NOT_A_REAL_ACTION" in decision.reasoning or "action" in decision.reasoning

        doc = fake_firestore.collection(CONFIDENCE_COLLECTION).document(_doc_id(identity_key)).get()
        assert doc.to_dict()["under_review"] is False


@pytest.mark.asyncio
class TestFailureEscalation:
    """
    audit_pattern() failing repeatedly for the same pattern must, at the
    threshold, write a needs_attention doc -- not just clear the flag and
    log silently forever. Mirrors TestProvisionalTierBlocksSuppress's
    structure (Task 21 in docs/TODO-Aug15.md).
    """

    async def _fail_audit_once(self, identity_key, fake_firestore, monkeypatch):
        async def _boom(*args, **kwargs):
            raise RuntimeError("model call failed")

        monkeypatch.setattr("vor_agents.orchestrator._run_agent", _boom)
        return await audit_pattern(identity_key, {"triggered_by": "test"}, fake_firestore)

    async def test_third_consecutive_failure_writes_needs_attention(
        self, fake_firestore, monkeypatch
    ):
        identity_key = ("rule", "w3wp.exe", "csc.exe", "family")

        for _ in range(3):
            await self._fail_audit_once(identity_key, fake_firestore, monkeypatch)

        doc = (
            fake_firestore.collection(NEEDS_ATTENTION_COLLECTION)
            .document(_doc_id(identity_key))
            .get()
        )
        assert doc.exists
        assert doc.to_dict()["failure_count"] == 3

    async def test_two_consecutive_failures_do_not_escalate(self, fake_firestore, monkeypatch):
        identity_key = ("rule", "w3wp.exe", "csc.exe", "family")

        for _ in range(2):
            await self._fail_audit_once(identity_key, fake_firestore, monkeypatch)

        doc = (
            fake_firestore.collection(NEEDS_ATTENTION_COLLECTION)
            .document(_doc_id(identity_key))
            .get()
        )
        assert not doc.exists

    async def test_success_after_failures_resets_and_prevents_escalation(
        self, fake_firestore, monkeypatch
    ):
        identity_key = ("rule", "w3wp.exe", "csc.exe", "family")

        for _ in range(2):
            await self._fail_audit_once(identity_key, fake_firestore, monkeypatch)

        async def _ok(*args, **kwargs):
            return {
                "action": "NO_ACTION",
                "invalidated_instance_ids": [],
                "concerns_found": [],
                "reasoning": "clean",
            }

        monkeypatch.setattr("vor_agents.orchestrator._run_agent", _ok)
        await audit_pattern(identity_key, {"triggered_by": "test"}, fake_firestore)

        # One more failure after the reset -- count should be 1, not 3.
        await self._fail_audit_once(identity_key, fake_firestore, monkeypatch)

        doc = (
            fake_firestore.collection(NEEDS_ATTENTION_COLLECTION)
            .document(_doc_id(identity_key))
            .get()
        )
        assert not doc.exists

    async def test_success_after_escalation_resolves_needs_attention_doc(
        self, fake_firestore, monkeypatch
    ):
        """The counterpart to escalation: once a needs_attention doc has
        been written (3 consecutive failures), a subsequent successful
        audit must mark it resolved -- see final review finding #2. The
        doc must persist (a human still needs to be able to see it
        happened) but carry a resolved_at stamp and a zeroed
        failure_count so a live escalation can be told apart from a
        resolved one."""
        identity_key = ("rule", "w3wp.exe", "csc.exe", "family")

        for _ in range(3):
            await self._fail_audit_once(identity_key, fake_firestore, monkeypatch)

        doc_ref = fake_firestore.collection(NEEDS_ATTENTION_COLLECTION).document(
            _doc_id(identity_key)
        )
        assert doc_ref.get().exists
        assert "resolved_at" not in doc_ref.get().to_dict()

        async def _ok(*args, **kwargs):
            return {
                "action": "NO_ACTION",
                "invalidated_instance_ids": [],
                "concerns_found": [],
                "reasoning": "clean",
            }

        monkeypatch.setattr("vor_agents.orchestrator._run_agent", _ok)
        await audit_pattern(identity_key, {"triggered_by": "test"}, fake_firestore)

        resolved = doc_ref.get().to_dict()
        assert "resolved_at" in resolved
        assert resolved["failure_count"] == 0

    async def test_needs_attention_write_failure_never_propagates_or_blocks_clear(
        self, fake_firestore, monkeypatch
    ):
        """Directly tests the plan's headline safety constraint (final
        review finding #4): a failure to write the needs_attention doc on
        the 3rd consecutive failure must never raise out of
        audit_pattern(), and must never prevent clear_under_review()'s own
        write -- the confidence doc must still land with
        under_review=False / failure_count=3."""
        identity_key = ("rule", "w3wp.exe", "csc.exe", "family")

        def _boom_record(*args, **kwargs):
            raise RuntimeError("firestore write failed")

        monkeypatch.setattr("vor_agents.orchestrator.record_needs_attention", _boom_record)

        for _ in range(2):
            await self._fail_audit_once(identity_key, fake_firestore, monkeypatch)
        await self._fail_audit_once(identity_key, fake_firestore, monkeypatch)  # 3rd -- no raise

        doc = fake_firestore.collection(CONFIDENCE_COLLECTION).document(_doc_id(identity_key)).get()
        data = doc.to_dict()
        assert data["under_review"] is False
        assert data["failure_count"] == 3


@pytest.mark.asyncio
class TestFailureCountBlocksSuppress:
    """Same shape as TestProvisionalTierBlocksSuppress -- SUPPRESS is
    deterministically overridden once failure_count crosses the
    escalation threshold, mirroring the under_review/provisional
    overrides already in classify_alert()."""

    async def test_suppress_overridden_when_failure_count_at_threshold(
        self, fake_firestore, baseline_alert, diverse_confirmed_instances, monkeypatch
    ):
        for instance in diverse_confirmed_instances:
            record_confirmed_negative(instance, fake_firestore)
        identity_key = pattern_identity_key(baseline_alert)
        doc_ref = fake_firestore.collection(CONFIDENCE_COLLECTION).document(_doc_id(identity_key))
        doc_ref.set({"failure_count": 3}, merge=True)

        async def _fake_run_agent(*args, **kwargs):
            return {
                "decision": "SUPPRESS",
                "matched_pattern_id": "test",
                "uncertain_reason": "not_applicable",
                "structural_deviations_found": [],
                "reasoning": "matches template",
            }

        monkeypatch.setattr("vor_agents.orchestrator._run_agent", _fake_run_agent)
        result, _ = await classify_alert(baseline_alert, fake_firestore)

        assert result.decision == "UNCERTAIN"
        assert result.uncertain_reason == "audit_failing"

    async def test_failure_count_below_threshold_does_not_override(
        self, fake_firestore, baseline_alert, diverse_confirmed_instances, monkeypatch
    ):
        for instance in diverse_confirmed_instances:
            record_confirmed_negative(instance, fake_firestore)
        identity_key = pattern_identity_key(baseline_alert)
        doc_ref = fake_firestore.collection(CONFIDENCE_COLLECTION).document(_doc_id(identity_key))
        doc_ref.set({"failure_count": 2}, merge=True)

        async def _fake_run_agent(*args, **kwargs):
            return {
                "decision": "SUPPRESS",
                "matched_pattern_id": "test",
                "uncertain_reason": "not_applicable",
                "structural_deviations_found": [],
                "reasoning": "matches template",
            }

        monkeypatch.setattr("vor_agents.orchestrator._run_agent", _fake_run_agent)
        result, _ = await classify_alert(baseline_alert, fake_firestore)

        assert result.decision == "SUPPRESS"


class _FakePart:
    def __init__(self, text, thought=None):
        self.text = text
        # Thought summaries are skipped by _run_agent; modeled here so the
        # double matches the real Part interface it stands in for.
        self.thought = thought


class _FakeContent:
    def __init__(self, parts):
        self.parts = parts


class _FakeEvent:
    def __init__(self, text, partial=None):
        self.content = _FakeContent([_FakePart(text)])
        # _run_agent skips partial (streaming) events so their text isn't
        # counted twice alongside the aggregated event that follows.
        self.partial = partial


class _FakeRunner:
    """Stand-in for google.adk.runners.Runner — yields one event carrying
    whatever raw text the test wants _run_agent to try to json.loads().

    close() is part of the real Runner's interface and _run_agent now
    calls it in a finally block to release per-call resources; a double
    that omits it would pass here while the real path leaked."""

    def __init__(self, *, response_text, **kwargs):
        self._response_text = response_text
        self.closed = False

    async def run_async(self, *, user_id, session_id, new_message):
        yield _FakeEvent(self._response_text)

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
class TestRunAgentJSONParsing:
    """
    Regression coverage for _run_agent()'s unwrapped json.loads(): a model
    returning empty output, Markdown-fenced JSON, or truncated text
    previously raised a bare json.JSONDecodeError straight out of
    _run_agent, propagating to a 500 on /classify or (before the Task 1
    fix) a permanently stuck under_review flag on /audit.
    """

    async def test_run_agent_bad_json_raises_agent_output_error(self):
        with (
            patch(
                "vor_agents.orchestrator.Runner",
                side_effect=lambda **kwargs: _FakeRunner(response_text="not json at all", **kwargs),
            ),
            pytest.raises(AgentOutputError),
        ):
            await _run_agent(agent=object(), prompt_text="prompt", session_id="s1")

    async def test_classify_alert_degrades_to_uncertain_on_unparseable_output(
        self, fake_firestore, baseline_alert
    ):
        with patch(
            "vor_agents.orchestrator._run_agent",
            new=AsyncMock(side_effect=AgentOutputError("Model did not return valid JSON")),
        ):
            result, identity_key = await classify_alert(baseline_alert, fake_firestore)

        assert result.decision == "UNCERTAIN"
        assert result.uncertain_reason == "missing_data"
        assert "unparseable" in result.reasoning.lower()
        assert identity_key  # still returns a real identity_key, not lost


class TestRunScheduledSweep:
    """
    run_scheduled_sweep() no longer awaits audit_pattern() directly --
    it selects targets (unchanged select_audit_targets() logic) and
    enqueues one task per target via a dependency-injected callable,
    same shape as classify_alert() receiving firestore_client rather
    than constructing one. Not async: enqueueing is a synchronous Cloud
    Tasks client call, same as the existing synchronous Firestore calls
    already used inside async FastAPI handlers elsewhere in this repo.
    """

    def test_enqueues_each_selected_target_and_returns_their_identity_keys(self, fake_firestore):
        # detection_rule_id deliberately contains underscores — this used
        # to break _fetch_all_confirmed_patterns()'s doc.id.split("_")
        # reconstruction (see test_known_gaps.py, now fixed by storing
        # identity_key as a Firestore field instead of parsing the doc
        # ID). Left in place on purpose so this test would catch a
        # regression back to the old scheme.
        # Vary host, user, and timestamp to meet evidence_diversity_score threshold.
        instances = [
            {
                "detection_rule_id": "Test_Rule_With_Underscores",
                "parent_image": "parentexe",
                "child_image": "childexe",
                "endpoint_family": "testfamily",
                "auth_method_present": True,
                "session_cookie_present": True,
                "integrity_level": "Medium",
                "file_access_mode": "read",
                "egress_follows_access": False,
                "host": "host1",
                "user": "user1",
                "timestamp": "2026-08-01T09:00:00Z",
                "instance_id": "i1",
            },
            {
                "detection_rule_id": "Test_Rule_With_Underscores",
                "parent_image": "parentexe",
                "child_image": "childexe",
                "endpoint_family": "testfamily",
                "auth_method_present": True,
                "session_cookie_present": True,
                "integrity_level": "Medium",
                "file_access_mode": "read",
                "egress_follows_access": False,
                "host": "host2",
                "user": "user2",
                "timestamp": "2026-08-03T14:00:00Z",
                "instance_id": "i2",
            },
            {
                "detection_rule_id": "Test_Rule_With_Underscores",
                "parent_image": "parentexe",
                "child_image": "childexe",
                "endpoint_family": "testfamily",
                "auth_method_present": True,
                "session_cookie_present": True,
                "integrity_level": "Medium",
                "file_access_mode": "read",
                "egress_follows_access": False,
                "host": "host3",
                "user": "user3",
                "timestamp": "2026-08-05T22:00:00Z",
                "instance_id": "i3",
            },
        ]
        for instance in instances:
            record_confirmed_negative(instance, fake_firestore)

        enqueued_calls = []

        def fake_enqueue(identity_key, pattern_data):
            enqueued_calls.append(identity_key)
            return True

        result = run_scheduled_sweep(fake_firestore, fake_enqueue)

        expected_key = ("Test_Rule_With_Underscores", "parentexe", "childexe", "testfamily")
        assert result == [expected_key]
        assert enqueued_calls == [expected_key]

    def test_dedup_hits_are_not_counted_in_the_returned_list(
        self, fake_firestore, diverse_confirmed_instances
    ):
        for instance in diverse_confirmed_instances:
            record_confirmed_negative(instance, fake_firestore)

        result = run_scheduled_sweep(fake_firestore, lambda identity_key, pattern_data: False)

        assert result == []

    def test_no_confirmed_patterns_enqueues_nothing(self, fake_firestore):
        result = run_scheduled_sweep(fake_firestore, lambda identity_key, pattern_data: True)
        assert result == []

    def test_doc_missing_identity_key_field_is_skipped_not_crashed(
        self, fake_firestore, diverse_confirmed_instances
    ):
        """
        A confirmed-tier doc written before the identity_key-field fix
        (docs/TODO-Aug15.md Task 3) has no identity_key field at all. This
        must be skipped defensively, not crash the whole sweep — same
        "unassessed/malformed defaults to skip, log, keep going" pattern
        already used for confirmed-tier docs with an empty
        confirmed_instances list a few lines above in the real code.
        """
        for instance in diverse_confirmed_instances:
            record_confirmed_negative(instance, fake_firestore)

        # Simulate a pre-migration doc by deleting the identity_key field
        # Firestore-side, directly through the fake's underlying store.
        collection = fake_firestore._collections["confidence_docs"]
        for doc_id in collection:
            del collection[doc_id]["identity_key"]

        result = run_scheduled_sweep(fake_firestore, lambda identity_key, pattern_data: True)

        assert result == []


class TestSweepSurvivesMalformedLastReviewedAt:
    def test_bad_last_reviewed_at_does_not_crash_the_sweep(
        self, fake_firestore, diverse_confirmed_instances
    ):
        """Regression for Code-review-Aug25 1.3: a single corrupted
        last_reviewed_at string used to raise inside
        datetime.fromisoformat() with no handling, crashing the ENTIRE
        weekly sweep over one bad doc."""
        for instance in diverse_confirmed_instances:
            record_confirmed_negative(instance, fake_firestore)
        identity_key = pattern_identity_key(diverse_confirmed_instances[0])
        fake_firestore.collection(CONFIDENCE_COLLECTION).document(_doc_id(identity_key)).update(
            {"last_reviewed_at": "not-a-date"}
        )

        enqueued = run_scheduled_sweep(fake_firestore, enqueue_audit_fn=lambda k, p: True)

        assert identity_key in enqueued  # still surfaced, not dropped

    def test_malformed_timestamp_in_one_pattern_does_not_prevent_sweep_of_others(
        self, fake_firestore, diverse_confirmed_instances
    ):
        """Prove that the per-doc try/except isolation actually holds:
        a malformed last_reviewed_at in ONE pattern must not prevent
        OTHER, unrelated patterns from being processed by the same sweep."""
        # First pattern with malformed timestamp
        for instance in diverse_confirmed_instances:
            record_confirmed_negative(instance, fake_firestore)
        first_key = pattern_identity_key(diverse_confirmed_instances[0])
        fake_firestore.collection(CONFIDENCE_COLLECTION).document(_doc_id(first_key)).update(
            {"last_reviewed_at": "not-a-date"}
        )

        # Second pattern with different identity key and valid timestamp
        # Use the same instances but with different detection_rule_id
        second_instances = [
            dict(
                instance,
                detection_rule_id="Different_Rule",
                timestamp="2026-08-01T09:00:00Z",
            )
            for instance in diverse_confirmed_instances
        ]
        for instance in second_instances:
            record_confirmed_negative(instance, fake_firestore)
        second_key = pattern_identity_key(second_instances[0])
        # Explicitly set a valid timestamp (record_confirmed_negative doesn't set one)
        fake_firestore.collection(CONFIDENCE_COLLECTION).document(_doc_id(second_key)).update(
            {"last_reviewed_at": "2026-08-20T12:00:00Z"}
        )

        enqueued = run_scheduled_sweep(fake_firestore, enqueue_audit_fn=lambda k, p: True)

        # Both patterns must be enqueued despite the first being malformed
        assert first_key in enqueued
        assert second_key in enqueued


@pytest.mark.asyncio
class TestTracingWiring:
    """
    classify_alert()/audit_pattern() call the tracing functions exactly
    once per call, with overrides_fired/audit_failed populated correctly.
    mlflow itself is monkeypatched to the success fake from
    test_tracing.py's pattern -- these tests only check that orchestrator
    calls into tracing.py correctly, not tracing.py's own fallback
    behavior (already covered in tests/test_tracing.py).
    """

    async def test_classify_alert_logs_a_trace_with_no_overrides_on_a_clean_suppress(
        self, fake_firestore, baseline_alert, diverse_confirmed_instances, monkeypatch
    ):
        for instance in diverse_confirmed_instances:
            record_confirmed_negative(instance, fake_firestore)

        async def _fake_run_agent(*args, **kwargs):
            return {
                "decision": "SUPPRESS",
                "matched_pattern_id": "test",
                "uncertain_reason": "not_applicable",
                "structural_deviations_found": [],
                "reasoning": "matches template",
            }

        monkeypatch.setattr("vor_agents.orchestrator._run_agent", _fake_run_agent)
        captured = {}

        def _fake_log_classification_trace(
            alert, enrichment, classifier_output, overrides_fired, client
        ):
            captured["overrides_fired"] = overrides_fired

        monkeypatch.setattr(
            "vor_agents.orchestrator.log_classification_trace", _fake_log_classification_trace
        )

        await classify_alert(baseline_alert, fake_firestore)

        assert captured["overrides_fired"] == []

    async def test_classify_alert_logs_under_review_override(
        self, fake_firestore, baseline_alert, diverse_confirmed_instances, monkeypatch
    ):
        for instance in diverse_confirmed_instances:
            record_confirmed_negative(instance, fake_firestore)
        identity_key = pattern_identity_key(baseline_alert)
        fake_firestore.collection(CONFIDENCE_COLLECTION).document(_doc_id(identity_key)).set(
            {"under_review": True}, merge=True
        )

        async def _fake_run_agent(*args, **kwargs):
            return {
                "decision": "SUPPRESS",
                "matched_pattern_id": "test",
                "uncertain_reason": "not_applicable",
                "structural_deviations_found": [],
                "reasoning": "matches template",
            }

        monkeypatch.setattr("vor_agents.orchestrator._run_agent", _fake_run_agent)
        captured = {}

        def _fake_log_classification_trace(
            alert, enrichment, classifier_output, overrides_fired, client
        ):
            captured["overrides_fired"] = overrides_fired

        monkeypatch.setattr(
            "vor_agents.orchestrator.log_classification_trace", _fake_log_classification_trace
        )

        await classify_alert(baseline_alert, fake_firestore)

        assert captured["overrides_fired"] == ["under_review"]

    async def test_audit_pattern_logs_a_trace(self, fake_firestore, monkeypatch):
        identity_key = ("rule", "w3wp.exe", "csc.exe", "family")

        async def _ok(*args, **kwargs):
            return {
                "action": "NO_ACTION",
                "invalidated_instance_ids": [],
                "concerns_found": [],
                "reasoning": "clean",
            }

        monkeypatch.setattr("vor_agents.orchestrator._run_agent", _ok)
        captured = {}

        def _fake_log_audit_trace(ident_key, pattern_data, auditor_output, audit_failed, client):
            captured["audit_failed"] = audit_failed

        monkeypatch.setattr("vor_agents.orchestrator.log_audit_trace", _fake_log_audit_trace)

        await audit_pattern(identity_key, {"triggered_by": "test"}, fake_firestore)

        assert captured["audit_failed"] is False

    async def test_audit_pattern_logs_audit_failed_true_on_failure(
        self, fake_firestore, monkeypatch
    ):
        identity_key = ("rule", "w3wp.exe", "csc.exe", "family")

        async def _boom(*args, **kwargs):
            raise RuntimeError("model call failed")

        monkeypatch.setattr("vor_agents.orchestrator._run_agent", _boom)
        captured = {}

        def _fake_log_audit_trace(ident_key, pattern_data, auditor_output, audit_failed, client):
            captured["audit_failed"] = audit_failed

        monkeypatch.setattr("vor_agents.orchestrator.log_audit_trace", _fake_log_audit_trace)

        await audit_pattern(identity_key, {"triggered_by": "test"}, fake_firestore)

        assert captured["audit_failed"] is True


@pytest.mark.asyncio
class TestAuditFailureReasoningIsBounded:
    async def test_reasoning_uses_the_truncated_error_repr(self, fake_firestore):
        """Regression for Code-review-Aug25 2.4: `reasoning` embedded the
        FULL repr(exc) while last_error_repr (written to needs_attention)
        was truncated to 500 chars -- an inconsistency that let an
        unbounded exception repr (request IDs, URLs, stack context) reach
        MLflow/Firestore via AuditorOutput.reasoning even though the
        exact same string was being deliberately bounded two lines away."""
        identity_key = ("rule", "p.exe", "c.exe", "family")
        long_message = "x" * 2000
        with patch(
            "vor_agents.orchestrator._run_agent",
            new=AsyncMock(side_effect=RuntimeError(long_message)),
        ):
            result = await audit_pattern(identity_key, {"triggered_by": "test"}, fake_firestore)

        assert len(result.reasoning) < 600  # bounded, not ~2000+ chars


class TestSweepMaxTargetsIsConfigurable:
    """
    $SWEEP_MAX_TARGETS caps how many patterns one sweep enqueues audits
    for -- the sweep's cost dial, since every target is a model call.
    These drive the real run_scheduled_sweep() rather than the parsing
    helper, so a wiring mistake between the two is caught here.
    """

    def _seed_confirmed_patterns(self, fake_firestore, count):
        """`count` distinct graduated patterns, each with enough diverse
        evidence to reach confirmed tier and so be sweep-eligible."""
        for index in range(count):
            for instance_index, (host, user, timestamp) in enumerate(
                [
                    ("host1", "user1", "2026-08-01T09:00:00Z"),
                    ("host2", "user2", "2026-08-03T14:00:00Z"),
                    ("host3", "user3", "2026-08-05T22:00:00Z"),
                ]
            ):
                record_confirmed_negative(
                    {
                        "detection_rule_id": f"rule_{index}",
                        "parent_image": "parent.exe",
                        "child_image": "child.exe",
                        "endpoint_family": "family",
                        "auth_method_present": True,
                        "session_cookie_present": True,
                        "integrity_level": "Medium",
                        "file_access_mode": "read",
                        "egress_follows_access": False,
                        "host": host,
                        "user": user,
                        "timestamp": timestamp,
                        "instance_id": f"i{index}_{instance_index}",
                    },
                    fake_firestore,
                )

    def test_env_var_caps_the_number_enqueued(self, fake_firestore, monkeypatch):
        self._seed_confirmed_patterns(fake_firestore, 6)
        monkeypatch.setenv(SWEEP_MAX_TARGETS_ENV_VAR, "2")

        result = run_scheduled_sweep(fake_firestore, lambda identity_key, pattern_data: True)

        assert len(result) == 2

    def test_default_applies_when_unset(self, fake_firestore, monkeypatch):
        self._seed_confirmed_patterns(fake_firestore, 12)
        monkeypatch.delenv(SWEEP_MAX_TARGETS_ENV_VAR, raising=False)

        result = run_scheduled_sweep(fake_firestore, lambda identity_key, pattern_data: True)

        assert len(result) == DEFAULT_SWEEP_MAX_TARGETS

    def test_explicit_argument_beats_the_env_var(self, fake_firestore, monkeypatch):
        """A caller naming a value is never overridden by the environment."""
        self._seed_confirmed_patterns(fake_firestore, 6)
        monkeypatch.setenv(SWEEP_MAX_TARGETS_ENV_VAR, "2")

        result = run_scheduled_sweep(
            fake_firestore, lambda identity_key, pattern_data: True, max_targets=4
        )

        assert len(result) == 4

    def test_garbage_value_falls_back_rather_than_failing_the_sweep(
        self, fake_firestore, monkeypatch
    ):
        """A typo in the deploy flag must not take the sweep down."""
        self._seed_confirmed_patterns(fake_firestore, 12)
        monkeypatch.setenv(SWEEP_MAX_TARGETS_ENV_VAR, "lots")

        result = run_scheduled_sweep(fake_firestore, lambda identity_key, pattern_data: True)

        assert len(result) == DEFAULT_SWEEP_MAX_TARGETS

    def test_zero_does_not_silently_disable_the_sweep(self, fake_firestore, monkeypatch):
        """The dangerous misconfiguration: 0 targets looks exactly like a
        healthy sweep with nothing to audit."""
        self._seed_confirmed_patterns(fake_firestore, 12)
        monkeypatch.setenv(SWEEP_MAX_TARGETS_ENV_VAR, "0")

        result = run_scheduled_sweep(fake_firestore, lambda identity_key, pattern_data: True)

        assert len(result) == DEFAULT_SWEEP_MAX_TARGETS
