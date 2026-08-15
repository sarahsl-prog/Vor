"""
Vör — Pydantic schemas for ADK structured output.

These mirror CLASSIFIER_OUTPUT_SCHEMA / AUDITOR_OUTPUT_SCHEMA from
agent_prompts.py as Pydantic models, which is what ADK's output_schema
parameter actually expects. Keep this file in sync with agent_prompts.py
by hand — they're deliberately two different representations of the same
contract (JSON schema for documentation/reference, Pydantic for runtime).
"""

from enum import Enum

from pydantic import BaseModel, Field


class Decision(str, Enum):
    SUPPRESS = "SUPPRESS"
    ESCALATE = "ESCALATE"
    UNCERTAIN = "UNCERTAIN"


class UncertainReason(str, Enum):
    NO_HISTORY = "no_history"
    GRADUATION_PENDING = "graduation_pending"
    UNDER_REVIEW = "under_review"
    MISSING_DATA = "missing_data"
    NOT_APPLICABLE = "not_applicable"


class ClassifierOutput(BaseModel):
    decision: Decision
    matched_pattern_id: str | None = Field(
        default=None,
        description="Identity key string this was compared against, or null",
    )
    uncertain_reason: UncertainReason = Field(
        default=UncertainReason.NOT_APPLICABLE,
        description="Only meaningful when decision is UNCERTAIN",
    )
    structural_deviations_found: list[str] = Field(
        default_factory=list,
        description="EXHAUSTIVE list — every field-level mismatch found, "
        "not just the first. Format: 'field_name: template=X, observed=Y'.",
    )
    reasoning: str
    confidence_used: float | None = None


class AuditRequest(BaseModel):
    """Body shape for POST /audit. Cloud Tasks is the only caller (via
    the queue task body task_queue.py constructs) — validating here
    turns a malformed/truncated payload into a clear 422 instead of an
    unhandled KeyError-turned-500, so it fails visibly and predictably
    rather than burning Cloud Tasks' retry budget on a payload that will
    never succeed."""

    identity_key: list[str]
    pattern_data: dict


class AuditorAction(str, Enum):
    NO_ACTION = "NO_ACTION"
    DOWNGRADE = "DOWNGRADE"
    RECOMMEND_UPGRADE_FOR_HUMAN_REVIEW = "RECOMMEND_UPGRADE_FOR_HUMAN_REVIEW"


class AuditorOutput(BaseModel):
    action: AuditorAction
    invalidated_instance_ids: list[str] = Field(
        default_factory=list,
        description="Only populated if action is DOWNGRADE. Specific "
        "confirmed_instance instance_id values (from the evidence "
        "provided) that this review no longer trusts as evidence. Must "
        "reference real IDs from the provided evidence — never invent "
        "one. The template is rebuilt from whatever instances remain "
        "after these are removed; tier is recomputed, not force-set. If "
        "you distrust the pattern as a whole rather than specific "
        "instances, list every instance_id provided — that's a valid, "
        "explicit full invalidation, not a shortcut around citing IDs.",
    )
    concerns_found: list[str] = Field(default_factory=list)
    reasoning: str
