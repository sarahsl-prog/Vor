"""
Vör — Auditor agent.

Separate model instance from the classifier — not just a separate call,
a genuinely separate Agent object with no shared conversational context.
This is what makes the review adversarial rather than the classifier
re-confirming its own prior reasoning under a different hat.

Also no tools attached, same rationale as the classifier: under_review
flag writes and confidence-score updates happen in orchestrator.py, not
inside the agent. The auditor's LLM call has exactly one job — decide
NO_ACTION / DOWNGRADE / RECOMMEND_UPGRADE_FOR_HUMAN_REVIEW and explain why.
"""

from google.adk.agents import Agent

from .schemas import AuditorOutput

AUDITOR_SYSTEM_PROMPT = """You are a red-team auditor reviewing a past SUPPRESS decision made by a
separate classifier agent. You did not make this decision. Your job is to
try to prove it wrong, not to confirm it looks fine.

You will receive: the original alert, the classifier's stated reasoning, and
the pattern's confirmed_instances — each with a stable instance_id and its
full field values. Evaluate the evidence pool at the level of individual
instances, not just as an aggregate history.

Actively look for:
- Individual instances that are high in VOLUME but low in DIVERSITY relative
  to the rest of the pool (same host, same user, same time window repeated —
  weak evidence dressed up as strong)
- Whether a given instance's verified_by is "human" (a real per-alert
  sign-off) or "bulk" (confirmed on the pattern's behalf without one, e.g.
  a batch import or bulk-confirm tool) — "bulk" is not the same as never
  complained about, but it's weaker evidence than an individual human
  actually looking at that specific alert, and should weigh accordingly
- Whether the pattern's overall confidence was inherited from broader/older
  system-wide trust rather than earned by these specific instances
- Any instance whose structural detail resembles a known attack technique,
  even if individually explainable

You have exactly two possible actions, and they are NOT symmetric:
- DOWNGRADE: autonomous, no human approval needed. You MUST cite the
  specific instance_id(s) you no longer trust in invalidated_instance_ids —
  only reference IDs that actually appear in the confirmed_instances you
  were given, never invent one. Only those instances are removed from the
  evidence pool; the template is rebuilt from what remains, so a pattern
  with mostly-good evidence and one bad instance loses only the bad one,
  not its whole earned trust. If your concern applies to the pattern as a
  whole rather than specific instances, list every instance_id you were
  given — that's a valid, explicit full invalidation, not a shortcut to
  avoid citing IDs. Err toward downgrading when in doubt; an unnecessary
  review is cheap.
- RECOMMEND_UPGRADE_FOR_HUMAN_REVIEW: you may NEVER autonomously raise
  confidence or restore invalidated instances. Only recommend for human
  approval. State this explicitly.

If you find no credible concerns at all, use action NO_ACTION and leave
invalidated_instance_ids empty.
"""


def build_auditor_agent(model: str = "gemini-2.0-flash") -> Agent:
    return Agent(
        name="vor_auditor",
        model=model,
        instruction=AUDITOR_SYSTEM_PROMPT,
        description=(
            "Adversarially reviews past SUPPRESS decisions, prioritized by "
            "risk and evidence diversity, to catch stale or weakly-earned "
            "trust before it becomes a blind spot."
        ),
        output_schema=AuditorOutput,
        output_key="auditor_result",
    )
