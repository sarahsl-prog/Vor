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
from vor_agents.orchestrator import (
    AgentOutputError,
    _deviation_field_names,
    _run_agent,
    audit_pattern,
    classify_alert,
    run_scheduled_sweep,
)
from vor_agents.review_flag import mark_under_review


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
            "structural_deviations_found": ["some_field: template=X, observed=Y"],
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
            "structural_deviations_found": ["integrity_level: template=Medium, observed=High"],
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
            "structural_deviations_found": ["integrity_level: template=Medium, observed=High"],
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
    Regression coverage: a deviation string not following the
    "field: template=X, observed=Y" format (the model not following its
    own output rules, or phrasing it differently) used to be treated as a
    whole-string field name, which can never match a real template field
    name on either side of the reconciliation diff. Must now be skipped
    (and logged) instead of silently corrupting the field-name set.
    """

    def test_well_formed_strings_extract_field_name(self):
        result = _deviation_field_names(
            [
                "integrity_level: template=Medium, observed=High",
                "file_access_mode: template=read, observed=write",
            ]
        )
        assert result == {"integrity_level", "file_access_mode"}

    def test_colon_less_string_is_skipped_not_treated_as_field_name(self):
        result = _deviation_field_names(
            [
                "integrity_level observed High instead of Medium",
            ]
        )
        assert result == set()

    def test_mix_of_well_formed_and_malformed_keeps_only_well_formed(self):
        result = _deviation_field_names(
            [
                "integrity_level: template=Medium, observed=High",
                "malformed deviation with no colon at all",
                "",
                "  ",
            ]
        )
        assert result == {"integrity_level"}


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

    async def test_run_agent_exception_clears_under_review_and_stamps_review_time(
        self, fake_firestore, diverse_confirmed_instances
    ):
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
        assert data["last_reviewed_at"] is not None

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


class _FakePart:
    def __init__(self, text):
        self.text = text


class _FakeContent:
    def __init__(self, parts):
        self.parts = parts


class _FakeEvent:
    def __init__(self, text):
        self.content = _FakeContent([_FakePart(text)])


class _FakeRunner:
    """Stand-in for google.adk.runners.Runner — yields one event carrying
    whatever raw text the test wants _run_agent to try to json.loads()."""

    def __init__(self, *, response_text, **kwargs):
        self._response_text = response_text

    async def run_async(self, *, user_id, session_id, new_message):
        yield _FakeEvent(self._response_text)


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
