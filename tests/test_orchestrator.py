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
