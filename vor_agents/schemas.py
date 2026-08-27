"""
Vör — Pydantic schemas for ADK structured output.

These mirror CLASSIFIER_OUTPUT_SCHEMA / AUDITOR_OUTPUT_SCHEMA from
agent_prompts.py as Pydantic models, which is what ADK's output_schema
parameter actually expects. Keep this file in sync with agent_prompts.py
by hand — they're deliberately two different representations of the same
contract (JSON schema for documentation/reference, Pydantic for runtime).
"""

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Decision(str, Enum):
    SUPPRESS = "SUPPRESS"
    ESCALATE = "ESCALATE"
    UNCERTAIN = "UNCERTAIN"


class UncertainReason(str, Enum):
    NO_HISTORY = "no_history"
    GRADUATION_PENDING = "graduation_pending"
    UNDER_REVIEW = "under_review"
    AUDIT_FAILING = "audit_failing"
    MISSING_DATA = "missing_data"
    NOT_APPLICABLE = "not_applicable"


class StructuralDeviation(BaseModel):
    """
    One field-level mismatch between an alert and its confirmed template.
    Declared as a submodel, NOT a plain dict -- see
    docs/Code-review-Aug25.md's remediation final-review, finding C-1:
    `list[dict[str, Any]]` generates a propertyless OBJECT schema in the
    Gemini/Vertex structured-output contract (no declared "properties"),
    which Vertex rejects as invalid and which is semantically
    indistinguishable from `Any` from the model's side even when accepted.
    A declared submodel forces the schema to carry real named properties.

    template/observed are `Any`, not `str`, because DIFFABLE_FIELDS values
    are heterogeneous (bool fields like egress_follows_access, string
    fields like integrity_level) -- see identity.DIFFABLE_FIELDS.
    """

    field: str
    template: Any = None
    observed: Any = None


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
    structural_deviations_found: list[StructuralDeviation] = Field(
        default_factory=list,
        description="EXHAUSTIVE list — every field-level mismatch found, " "not just the first.",
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
    pattern_data: dict[str, Any]


class BlastRadiusCommitRequest(BaseModel):
    """Body shape for POST /blast-radius/commit -- a human committing a
    pending MEDIUM/LOW blast-radius proposal into the live table."""

    proposal_id: str


class PubSubMessage(BaseModel):
    """The `message` object inside a Pub/Sub push request body. `data` is
    base64-encoded -- Pub/Sub always encodes the published message body
    this way, regardless of what the publisher originally sent. Other
    fields Pub/Sub includes (messageId, publishTime, attributes) aren't
    read by anything here, so they're not modeled -- extra="allow" isn't
    even needed since pydantic ignores unrecognized fields by default."""

    data: str


class PubSubPushEnvelope(BaseModel):
    """Body shape Pub/Sub actually POSTs to a push endpoint:
    {"message": {"data": "<base64>", ...}, "subscription": "..."}. Used
    both to DETECT this shape in /classify (a successful
    model_validate() IS the detection -- see main.py's
    _decode_classify_body()) and to decode it. Requiring `subscription`
    (a field Pub/Sub push always includes) alongside `message.data` is
    what keeps a legitimate alert that happens to carry its own top-level
    `message: {data: ...}` field (e.g. Windows Event Log records commonly
    do) from being misread as an envelope. The alert JSON itself lives
    base64-encoded inside message.data, decoded and re-validated against
    ClassifierRequest separately, not by this model."""

    message: PubSubMessage
    subscription: str


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
