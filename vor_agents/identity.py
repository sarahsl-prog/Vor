"""
Vör — Pattern identity + structural template logic.

Everything here is deterministic aggregation/lookup. Zero LLM calls. This
is intentional: the classifier should never be asked to judge what counts
as "the same pattern" or "an invariant field" — that judgment call belongs
in code that's testable and auditable on its own, not embedded in a prompt.
"""

from typing import Any

from .evidence_diversity import evidence_diversity_score

DIFFABLE_FIELDS = [
    "auth_method_present",
    "session_cookie_present",
    "integrity_level",
    "file_access_mode",  # read vs write
    "egress_follows_access",  # bool
]

# Graduation is a two-part gate, not a raw instance count. Raw count alone
# is statistically weak: if a field is genuinely variable (say truly 80/20
# split) rather than invariant, the odds of 3 random draws all landing on
# the same value are ~51% — a coin flip that a "confirmed" template locks
# in a field as trusted when it isn't. Requiring diversity alongside count
# closes this at the graduation gate itself, rather than relying on the
# auditor to catch it later after it's already been trusted.
#
# Both thresholds are starting points, not validated against real review
# volume — no production Hayabusa/EVTX history exists yet to calibrate
# against (same open gap noted throughout this design). Recalibrate once
# real traffic is available; MIN_DIVERSITY in particular is a guess.
GRADUATION_THRESHOLD = 3  # minimum confirmed instances
MIN_DIVERSITY = 0.5  # minimum evidence_diversity_score, see evidence_diversity.py


class MalformedAlertError(ValueError):
    """
    Raised when a confirmed instance is missing one or more DIFFABLE_FIELDS.
    These fields are required structural data, not optional context (unlike
    evidence_diversity_score's host/user/timestamp, which degrade
    gracefully when absent) — a missing one means the alert/ingestion
    pipeline produced bad data, not that the field "doesn't apply here".
    Raised instead of letting a bare KeyError propagate out of
    build_structural_template, per this project's error-handling standard:
    external/user input is validated, never surfaced as a raw exception.
    """


def _validate_diffable_fields(instance: dict[str, Any]) -> None:
    missing = [field for field in DIFFABLE_FIELDS if field not in instance]
    if missing:
        raise MalformedAlertError(
            f"Confirmed instance is missing required diffable field(s): {missing}"
        )


IDENTITY_KEY_FIELDS = ("detection_rule_id", "parent_image", "child_image", "endpoint_family")


def pattern_identity_key(alert: dict[str, Any]) -> tuple[str, ...]:
    """
    (detection_rule_id, parent_image, child_image, endpoint_family)

    Deliberately excludes every field in DIFFABLE_FIELDS. If auth-presence
    were part of identity, an attacker repeating a technique would just
    spawn "new, unmatched patterns" forever instead of ever tripping the
    deviation check against the legitimate one.

    Raises MalformedAlertError (not a raw KeyError) if any identity field
    is missing -- HTTP callers already get this via ClassifierRequest's
    validation, but internal callers (scripts/seed_firestore.py,
    scripts/backfill_identity_key.py, enrichment.record_confirmed_negative)
    pass plain dicts straight through, and a raw KeyError there violated
    this project's "never surface raw exceptions" standard. See
    docs/Code-review-Aug25.md 3.1.
    """
    missing = [field for field in IDENTITY_KEY_FIELDS if field not in alert]
    if missing:
        raise MalformedAlertError(f"Alert is missing required identity field(s): {missing}")
    return tuple(alert[field] for field in IDENTITY_KEY_FIELDS)


def build_structural_template(
    confirmed_negative_instances: list[dict[str, Any]], provenance: str = "live"
) -> dict[str, Any]:
    """
    Returns:
        {
            "fields": {field: invariant_value, ...},  # only 100%-consistent
            "tier": "provisional" | "confirmed",
            "provenance": "live" | "seeded",
            "instance_count": int,
            "diversity_score": float,
        }

    Raises MalformedAlertError if any instance is missing a DIFFABLE_FIELDS
    key — this is the single choke point every caller (record_confirmed_
    negative, seed_template, invalidate_instances' rebuild) goes through,
    so validation happens exactly once regardless of entry point.

    provenance "seeded" = bulk-imported (e.g. dataset case #1) rather than
    earned one live human confirmation at a time. Structurally identical
    once built, but worth tracking so a bad seeded assumption can be traced
    later rather than poisoning decisions invisibly.

    Graduating to "confirmed" requires BOTH instance_count >=
    GRADUATION_THRESHOLD AND diversity_score >= MIN_DIVERSITY. Count alone
    can pass on repetition (the same host/user/hour logged three times);
    diversity alone with too few instances is just noise. Neither is
    sufficient by itself — see the module-level comment above these
    constants for why count-only graduation is statistically weak.
    """
    for instance in confirmed_negative_instances:
        _validate_diffable_fields(instance)

    fields = {}
    for field in DIFFABLE_FIELDS:
        values = {instance[field] for instance in confirmed_negative_instances}
        if len(values) == 1:
            fields[field] = values.pop()

    count = len(confirmed_negative_instances)
    diversity = evidence_diversity_score(confirmed_negative_instances)
    is_confirmed = count >= GRADUATION_THRESHOLD and diversity >= MIN_DIVERSITY

    return {
        "fields": fields,
        "tier": "confirmed" if is_confirmed else "provisional",
        "provenance": provenance,
        "instance_count": count,
        "diversity_score": diversity,
    }


def diff_alert_against_template(
    alert: dict[str, Any], template_fields: dict[str, Any]
) -> list[dict[str, Any]]:
    """
    Exhaustive diff — every field checked, never short-circuits on first
    mismatch. Returns structured deviation objects, empty list if none:
    [{"field": str, "template": <expected value>, "observed": <alert's
    value>}, ...]. Structured rather than a formatted string (see
    docs/Code-review-Aug25.md 3.3/3.4/decision 4) so orchestrator.py's
    reconciliation compares by field name without parsing free text, and
    a caller inspecting a real mismatch's values doesn't have to un-repr
    them out of a sentence.
    """
    deviations = []
    for field, expected in template_fields.items():
        observed = alert.get(field)
        if observed != expected:
            deviations.append({"field": field, "template": expected, "observed": observed})
    return deviations
