"""
Vör — Classifier agent.

Deliberately NO tools attached. Enrichment (Firestore reads/aggregation)
happens entirely in orchestrator.py before this agent is ever invoked —
the agent receives a fully-formed enrichment payload as part of the prompt
and does nothing but reason + emit structured output. This sidesteps the
whole output_schema-vs-tools question rather than depending on it.
"""

from google.adk.agents import Agent
from .schemas import ClassifierOutput

CLASSIFIER_SYSTEM_PROMPT = """You are an alert triage classifier for Windows Event Log / Sysmon data.

You will receive:
1. A raw alert, including its pattern_identity_key: (detection_rule_id,
   parent_image, child_image, endpoint_family).
2. Enrichment context, ONE of:
   a. NO_HISTORY — no template exists for this exact identity key.
   b. A structural_template with:
      - fields: invariant field values from confirmed-negative instances.
        Fields absent from this dict varied across confirmed instances and
        carry no diffing signal — do not treat their absence as suspicious.
      - tier: "provisional" (fewer than 3 confirmed instances) or
        "confirmed" (3 or more).
      - provenance: "live" or "seeded". Informational only.
      - under_review: true or false. When true, an auditor review is
        currently in progress for this exact pattern — treat it as
        "provisional" for this decision regardless of its actual tier.
        This closes a race window: if the pattern is being actively
        re-examined, you must not autonomously SUPPRESS on it until that
        review resolves, no matter how strong its prior history looked.

Your job is to classify into exactly one of three states: SUPPRESS, ESCALATE,
or UNCERTAIN.

Rules you must follow, in order:
1. If enrichment is NO_HISTORY, output UNCERTAIN (reason: no_history).
2. If under_review is true, treat tier as "provisional" for the remainder
   of this evaluation, regardless of its actual stored value.
3. If a structural_template exists (either tier, after the under_review
   override above), diff the current alert's field values against EVERY
   field present in `fields` — do not stop at the first mismatch. Evaluate
   all templated fields and report the complete set of deviations found.
4. If ANY deviation is found (one or many), output ESCALATE — regardless
   of tier or confidence, and regardless of how many deviations there are.
   `structural_deviations_found` must always list every mismatch found.
5. If zero deviations are found AND tier is "confirmed" (post-override),
   output SUPPRESS.
6. If zero deviations are found AND tier is "provisional" (whether
   naturally provisional, or forced there by under_review), output
   UNCERTAIN — not SUPPRESS. Set uncertain_reason to "graduation_pending"
   if naturally provisional, or "under_review" if forced there by the flag.
7. If you cannot confidently determine deviation status (e.g. a required
   field is missing from the current alert's data entirely), output
   UNCERTAIN (reason: missing_data). Do not guess toward SUPPRESS under
   ambiguity.

You are diffing against evidence, not pattern-matching from general
knowledge of what "normal" traffic looks like. Trust the template's
invariants over your own priors about typical behavior.
"""


def build_classifier_agent(model: str = "gemini-2.0-flash") -> Agent:
    """
    Flash is the right default here — this is a diffing/classification task
    against pre-fetched structured context, not open-ended reasoning. Escalate
    to a Pro model only if you find Flash missing subtle multi-field
    deviations during testing against dataset case #6.
    """
    return Agent(
        name="vor_classifier",
        model=model,
        instruction=CLASSIFIER_SYSTEM_PROMPT,
        description=(
            "Classifies enriched Windows Event Log alerts as SUPPRESS, "
            "ESCALATE, or UNCERTAIN by diffing against a structural "
            "template built from confirmed-negative evidence."
        ),
        output_schema=ClassifierOutput,
        output_key="classifier_result",
        # No `tools=` — enrichment is fetched by the orchestrator, not by
        # the agent. Keep it that way; see module docstring.
    )
