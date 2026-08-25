"""
Construction smoke tests for classifier_agent.py / auditor_agent.py.

TESTING_PLAN.md deliberately excludes these two modules from logic
coverage -- they are prompt strings plus an `Agent()` call, with no
branching to exercise -- but names a construction smoke test as worth
having. This is that test.

What it actually protects: `Agent()` validates its own arguments at
construction time (name format, output_schema shape, instruction type).
A rename, a bad output_key, or a schema that stops being a usable ADK
output schema fails HERE, in a test that costs nothing, instead of on the
first real model call in production. No network, no credentials, no model
invocation -- constructing an Agent does not contact Vertex AI.
"""

import pytest

from vor_agents.auditor_agent import AUDITOR_SYSTEM_PROMPT, build_auditor_agent
from vor_agents.classifier_agent import CLASSIFIER_SYSTEM_PROMPT, build_classifier_agent
from vor_agents.schemas import AuditorOutput, ClassifierOutput


class TestAgentsConstruct:
    def test_classifier_agent_constructs(self):
        agent = build_classifier_agent()

        assert agent.name == "vor_classifier"
        assert agent.output_schema is ClassifierOutput
        assert agent.output_key == "classifier_result"

    def test_auditor_agent_constructs(self):
        agent = build_auditor_agent()

        assert agent.name == "vor_auditor"
        assert agent.output_schema is AuditorOutput
        assert agent.output_key == "auditor_result"

    @pytest.mark.parametrize("build", [build_classifier_agent, build_auditor_agent])
    def test_model_is_overridable(self, build):
        """Both builders default to Flash but take a model override --
        classifier_agent.py's docstring explicitly anticipates escalating
        to a Pro model if Flash misses subtle deviations, so the override
        is a documented seam, not incidental."""
        agent = build(model="gemini-2.5-pro")

        assert agent.model == "gemini-2.5-pro"

    @pytest.mark.parametrize("build", [build_classifier_agent, build_auditor_agent])
    def test_agents_carry_no_tools(self, build):
        """Enrichment is fetched by the orchestrator and passed in, never
        pulled by the agent itself (see classifier_agent.py's module
        docstring). A tool appearing here would mean the agent had gained
        its own data-access path, which is the design boundary this whole
        system's auditability rests on."""
        agent = build()

        assert not agent.tools


class TestSystemPrompts:
    @pytest.mark.parametrize(
        "prompt", [CLASSIFIER_SYSTEM_PROMPT, AUDITOR_SYSTEM_PROMPT], ids=["classifier", "auditor"]
    )
    def test_prompt_is_non_empty(self, prompt):
        """An empty or accidentally-blanked instruction would leave the
        model with no rules at all while still constructing and running
        perfectly happily -- the one failure mode of a prompt-only module
        that silence would hide."""
        assert prompt.strip()
