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
from vor_agents.model_config import (
    DEFAULT_GEMINI_MODEL,
    GEMINI_MODEL_ENV_VAR,
    resolve_model,
)
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


class TestModelResolution:
    """
    REGRESSION: the model default was originally written as
    `def build(model=os.environ.get("GEMINI_MODEL", ...))`, which evaluates
    the lookup once at import and binds it for the process lifetime. That
    silently ignored any value set after import and made the setting
    impossible to monkeypatch -- i.e. the configurability was untestable,
    and these tests could not have been written against it.
    """

    def test_default_when_env_var_unset(self, monkeypatch):
        monkeypatch.delenv(GEMINI_MODEL_ENV_VAR, raising=False)

        assert resolve_model() == DEFAULT_GEMINI_MODEL

    def test_env_var_is_read_at_call_time(self, monkeypatch):
        """The whole point: set it after import, and it still takes."""
        monkeypatch.setenv(GEMINI_MODEL_ENV_VAR, "gemini-2.0-flash")

        assert resolve_model() == "gemini-2.0-flash"

    def test_env_var_change_takes_effect_between_calls(self, monkeypatch):
        monkeypatch.setenv(GEMINI_MODEL_ENV_VAR, "model-a")
        first = resolve_model()
        monkeypatch.setenv(GEMINI_MODEL_ENV_VAR, "model-b")
        second = resolve_model()

        assert (first, second) == ("model-a", "model-b")

    def test_explicit_argument_beats_the_env_var(self, monkeypatch):
        """An operator or a test naming a model explicitly always wins."""
        monkeypatch.setenv(GEMINI_MODEL_ENV_VAR, "from-env")

        assert resolve_model("explicit") == "explicit"

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_blank_env_var_falls_back_to_the_default(self, monkeypatch, blank):
        """An unset var in a deploy script usually arrives as "". Passing
        that through would fail at model-call time with an empty model
        name, which is far harder to diagnose than a fallback."""
        monkeypatch.setenv(GEMINI_MODEL_ENV_VAR, blank)

        assert resolve_model() == DEFAULT_GEMINI_MODEL

    @pytest.mark.parametrize("build", [build_classifier_agent, build_auditor_agent])
    def test_builders_honor_the_env_var(self, monkeypatch, build):
        monkeypatch.setenv(GEMINI_MODEL_ENV_VAR, "gemini-2.0-flash")

        assert build().model == "gemini-2.0-flash"

    @pytest.mark.parametrize("build", [build_classifier_agent, build_auditor_agent])
    def test_builders_default_when_env_var_unset(self, monkeypatch, build):
        monkeypatch.delenv(GEMINI_MODEL_ENV_VAR, raising=False)

        assert build().model == DEFAULT_GEMINI_MODEL

    def test_default_model_is_a_real_flash_model_id(self):
        """Regression for the Aug25 code review: DEFAULT_GEMINI_MODEL was
        `gemini-3.5-flash`, which does not exist as a Google model. This
        pins the default to the well-known-valid family/version pattern
        so a future typo'd default fails a fast unit test instead of
        the first real Vertex AI call in production."""
        assert DEFAULT_GEMINI_MODEL == "gemini-2.0-flash"
