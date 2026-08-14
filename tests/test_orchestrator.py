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

from vor_agents.enrichment import record_confirmed_negative
from vor_agents.orchestrator import classify_alert


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
            "confidence_used": None,
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
            "confidence_used": 0.9,
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
            "structural_deviations_found": ["some_field: template=X, observed=Y"],
            "reasoning": "This looks suspicious for reasons beyond the diffed fields.",
            "confidence_used": None,
        }
        with patch(
            "vor_agents.orchestrator._run_agent",
            new=AsyncMock(return_value=fake_model_response),
        ):
            result, _identity_key = await classify_alert(baseline_alert, fake_firestore)

        assert result.decision == "ESCALATE"
        assert "override" not in result.reasoning.lower()

    async def test_no_history_skips_reconciliation_entirely(
        self, fake_firestore, baseline_alert
    ):
        """No confirmed template exists yet -> precomputed_deviations is
        empty -> reconciliation block should not run at all (guarded by
        'if precomputed_deviations:'). Model's UNCERTAIN stands untouched."""
        fake_model_response = {
            "decision": "UNCERTAIN",
            "matched_pattern_id": None,
            "uncertain_reason": "no_history",
            "structural_deviations_found": [],
            "reasoning": "No prior history for this pattern.",
            "confidence_used": None,
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
            "structural_deviations_found": ["integrity_level: template=Medium, observed=High"],
            "reasoning": "Reported a deviation but suppressed anyway.",
            "confidence_used": 0.9,
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
            "structural_deviations_found": ["integrity_level: template=Medium, observed=High"],
            "reasoning": "Deviation found, escalating as instructed.",
            "confidence_used": None,
        }
        with patch(
            "vor_agents.orchestrator._run_agent",
            new=AsyncMock(return_value=fake_model_response),
        ):
            result, _identity_key = await classify_alert(baseline_alert, fake_firestore)

        assert result.decision == "ESCALATE"
        assert "override" not in result.reasoning.lower()


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
            "confidence_used": None,
        }
        mock_run_agent = AsyncMock(return_value=fake_model_response)
        with patch("vor_agents.orchestrator._run_agent", new=mock_run_agent):
            await classify_alert(baseline_alert, fake_firestore)
            await classify_alert(baseline_alert, fake_firestore)

        first_session_id = mock_run_agent.call_args_list[0].kwargs["session_id"]
        second_session_id = mock_run_agent.call_args_list[1].kwargs["session_id"]
        assert first_session_id != second_session_id
