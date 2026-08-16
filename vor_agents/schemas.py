"""
Vör — Pydantic schemas for ADK structured output.

These mirror CLASSIFIER_OUTPUT_SCHEMA / AUDITOR_OUTPUT_SCHEMA from
agent_prompts.py as Pydantic models, which is what ADK's output_schema
parameter actually expects. Keep this file in sync with agent_prompts.py
by hand — they're deliberately two different representations of the same
contract (JSON schema for documentation/reference, Pydantic for runtime).
"""

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


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


class ClassifierRequest(BaseModel):
    """Body shape for POST /classify. Only the four identity_key
    components (see identity.pattern_identity_key) are required —
    pattern_identity_key() indexes an alert dict directly (alert["field"])
    rather than using .get(), so a missing one previously raised a raw
    KeyError that surfaced as an unhandled 500. Validating here turns that
    into a clear 422 instead, same "never surface raw exceptions" standard
    as AuditRequest below.

    Every other field (DIFFABLE_FIELDS, host/user/timestamp, instance_id)
    is optional and read with .get() throughout the codebase, so nothing
    breaks if they're absent — extra="allow" lets an alert schema carry
    additional context fields this model doesn't know about by name
    without stripping them, since classify_alert() and everything
    downstream operate on a plain dict, not this model's fields."""

    model_config = ConfigDict(extra="allow")

    detection_rule_id: str
    parent_image: str
    child_image: str
    endpoint_family: str


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
