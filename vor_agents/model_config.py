"""
Vör -- Gemini model selection.

One place for the model default and the env var that overrides it, so the
two agent builders can't drift apart on either. Both call resolve_model().

Why this isn't just a default argument: `def build(model=os.environ.get(...))`
evaluates the lookup ONCE, when the module is first imported, and binds the
result for the life of the process. That silently ignores anything set
after import, makes the setting impossible to monkeypatch in a test, and
turns "configurable" into "configurable only if the import order happens to
cooperate". Reading it per call costs nothing measurable next to a model
round-trip and behaves the way an operator would expect.
"""

import os

from google.adk.models.base_llm import BaseLlm

# Flash is the right default for both agents -- see build_classifier_agent's
# docstring. Overridable per-deployment with GEMINI_MODEL (docs/DEPLOY.md).
DEFAULT_GEMINI_MODEL = "gemini-2.0-flash"

GEMINI_MODEL_ENV_VAR = "GEMINI_MODEL"


def resolve_model(model: str | BaseLlm | None = None) -> str | BaseLlm:
    """
    Resolves which model an agent should use, in precedence order:

      1. An explicit `model` argument -- an operator or a test asking for
         something specific always wins.
      2. $GEMINI_MODEL, read at call time.
      3. DEFAULT_GEMINI_MODEL.

    Accepts a BaseLlm instance as well as a model-name string because
    ADK's Agent(model=...) does: passing an instance is how tests drive
    the agents without a network call. An empty or whitespace-only
    GEMINI_MODEL is treated as unset rather than passed through -- an
    unset env var in a deploy script usually arrives as "", and failing at
    model-call time with an empty model name is far harder to diagnose
    than just falling back to the default.
    """
    if model is not None:
        return model

    configured = os.environ.get(GEMINI_MODEL_ENV_VAR, "")
    return configured.strip() or DEFAULT_GEMINI_MODEL
