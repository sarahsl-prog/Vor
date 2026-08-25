"""
Tests for orchestrator._run_agent -- the ADK Runner plumbing that is the
only place this codebase actually calls a model.

Every other orchestrator test monkeypatches `_run_agent` away, which means
the function itself had no coverage at all: the code between "we have a
prompt" and "we have a dict" was exercised only by real Gemini calls in
production. These tests close that gap by driving the REAL Runner with a
fake `BaseLlm`, so the event-stream handling is tested without network,
credentials, or spend.

The fake returns whatever parts a test asks for, which is the point --
the failure modes here are all about what the response stream contains,
not about what the model decided.
"""

import json
from collections.abc import AsyncGenerator
from typing import ClassVar

import pytest
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai.types import Content, Part
from pydantic import ValidationError

from vor_agents.classifier_agent import build_classifier_agent
from vor_agents.orchestrator import (
    AgentOutputError,
    _run_agent,
    classify_alert,
    session_service,
)

pytestmark = pytest.mark.asyncio

VALID_OUTPUT = {
    "decision": "SUPPRESS",
    "matched_pattern_id": "pattern-1",
    "uncertain_reason": "not_applicable",
    "structural_deviations_found": [],
    "reasoning": "matches template",
}


class _FakeLlm(BaseLlm):
    """Minimal BaseLlm returning a scripted response. Class-level `parts`
    so a test can script the stream without fighting pydantic's field
    validation on a BaseLlm subclass."""

    model: str = "fake-model"
    parts: ClassVar[list[Part]] = []

    @classmethod
    def supported_models(cls) -> list[str]:
        return [r"fake-model"]

    async def generate_content_async(
        self, llm_request: object, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        yield LlmResponse(content=Content(role="model", parts=list(type(self).parts)))


async def _run_with(parts: list[Part], session_id: str) -> dict:
    _FakeLlm.parts = parts
    agent = build_classifier_agent(model=_FakeLlm())
    return await _run_agent(agent, "prompt", session_id=session_id)


class TestResponseParsing:
    async def test_plain_json_response_parses(self):
        result = await _run_with([Part(text=json.dumps(VALID_OUTPUT))], "s-plain")
        assert result == VALID_OUTPUT

    async def test_json_split_across_parts_is_reassembled(self):
        """Gemini may split one JSON payload across multiple text parts;
        concatenating them is the whole reason this loop accumulates."""
        encoded = json.dumps(VALID_OUTPUT)
        result = await _run_with([Part(text=encoded[:25]), Part(text=encoded[25:])], "s-split")
        assert result == VALID_OUTPUT

    async def test_thought_parts_are_ignored(self):
        """REGRESSION: thought summaries arrive as ordinary text parts
        flagged thought=True. Concatenating them made the payload
        unparseable, which degraded every classification to UNCERTAIN and
        marked every audit failed -- a total, silent loss of autonomous
        suppression that reads as conservative behavior. Not reachable on
        gemini-2.0-flash, but build_classifier_agent() documents
        escalating to a Pro model, and thinking models emit these."""
        result = await _run_with(
            [
                Part(text="First I will consider the parent process.", thought=True),
                Part(text="Then the integrity level.", thought=True),
                Part(text=json.dumps(VALID_OUTPUT)),
            ],
            "s-thought",
        )
        assert result == VALID_OUTPUT

    async def test_thought_only_response_is_an_output_error(self):
        """A response that is nothing but reasoning carries no decision.
        Failing loudly is right -- classify_alert() turns this into
        UNCERTAIN rather than inventing an answer."""
        with pytest.raises(AgentOutputError):
            await _run_with([Part(text="Thinking...", thought=True)], "s-thought-only")

    async def test_empty_response_is_an_output_error(self):
        with pytest.raises(AgentOutputError, match="length=0"):
            await _run_with([Part(text="")], "s-empty")

    @pytest.mark.parametrize(
        "text",
        [
            '```json\n{"decision": "SUPPRESS"}\n```',
            "I think this one is fine, suppress it.",
            '{"foo": "bar"}',
        ],
        ids=["markdown_fenced", "prose", "wrong_schema"],
    )
    async def test_malformed_output_is_normalized_to_agent_output_error(self, text):
        """REGRESSION: both agents set output_schema, so ADK validates the
        response inside the Runner and raises pydantic ValidationError --
        NOT the JSONDecodeError this function was written around. That
        error bypassed classify_alert()'s `except AgentOutputError`
        entirely and escaped /classify as a 500, which a Pub/Sub push
        subscription then retries against the same poisoned alert. Every
        malformed shape must surface as AgentOutputError so the
        degrade-to-UNCERTAIN path actually runs."""
        with pytest.raises(AgentOutputError):
            await _run_with([Part(text=text)], f"s-malformed-{abs(hash(text))}")

    async def test_error_never_surfaces_a_raw_pydantic_error(self):
        """Project standard: never surface a raw exception to the caller.
        The original is preserved as __cause__ for debugging, but the type
        crossing the boundary is this project's own."""
        with pytest.raises(AgentOutputError) as exc_info:
            await _run_with([Part(text="not json at all")], "s-raw")

        assert not isinstance(exc_info.value, ValidationError)
        assert exc_info.value.__cause__ is not None


class TestClassifyAlertDegradesInsteadOfRaising:
    """The failure this protects against, at the layer that matters:
    /classify's caller. A raw exception escaping classify_alert() becomes
    a 500, and a Pub/Sub push subscription redelivers the same alert until
    it ages out of the retention window."""

    @pytest.mark.parametrize(
        "text",
        [
            "I think this one is fine, suppress it.",
            '```json\n{"decision": "SUPPRESS"}\n```',
            '{"foo": "bar"}',
            "",
        ],
        ids=["prose", "markdown_fenced", "wrong_schema", "empty"],
    )
    async def test_malformed_model_output_degrades_to_uncertain(
        self, text, fake_firestore, baseline_alert, monkeypatch
    ):
        _FakeLlm.parts = [Part(text=text)]
        monkeypatch.setattr(
            "vor_agents.orchestrator.build_classifier_agent",
            lambda *args, **kwargs: build_classifier_agent(model=_FakeLlm()),
        )

        result, _ = await classify_alert(baseline_alert, fake_firestore)

        # Never SUPPRESS off output the system could not even parse.
        assert result.decision == "UNCERTAIN"


class TestSessionCleanup:
    """REGRESSION: auto_create_session registers a session per call under a
    uuid4-suffixed ID that is never reused, and Runner holds per-call
    resources. Without explicit cleanup a long-lived Cloud Run instance
    accumulates one of each per alert, forever, until it OOMs."""

    async def test_session_is_deleted_after_a_successful_call(self):
        await _run_with([Part(text=json.dumps(VALID_OUTPUT))], "s-cleanup")

        assert not await _session_for("s-cleanup")

    async def test_session_is_deleted_even_when_the_call_fails(self):
        """The failure path is the one that matters: a model returning junk
        must not also leak a session on every retry."""
        with pytest.raises(AgentOutputError):
            await _run_with([Part(text="junk")], "s-cleanup-fail")

        assert not await _session_for("s-cleanup-fail")

    async def test_many_calls_do_not_accumulate_sessions(self):
        for index in range(10):
            await _run_with([Part(text=json.dumps(VALID_OUTPUT))], f"s-bulk-{index}")

        for index in range(10):
            assert not await _session_for(f"s-bulk-{index}")


async def _session_for(session_id: str) -> object:
    return await session_service.get_session(
        app_name="vor", user_id="vor-system", session_id=session_id
    )
