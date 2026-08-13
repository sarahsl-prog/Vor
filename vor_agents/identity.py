"""
Vör — Pattern identity + structural template logic.

Everything here is deterministic aggregation/lookup. Zero LLM calls. This
is intentional: the classifier should never be asked to judge what counts
as "the same pattern" or "an invariant field" — that judgment call belongs
in code that's testable and auditable on its own, not embedded in a prompt.
"""

DIFFABLE_FIELDS = [
    "auth_method_present",
    "session_cookie_present",
    "integrity_level",
    "file_access_mode",       # read vs write
    "egress_follows_access",  # bool
]

GRADUATION_THRESHOLD = 3  # confirmed instances needed to leave "provisional" tier


def pattern_identity_key(alert: dict) -> tuple:
    """
    (detection_rule_id, parent_image, child_image, endpoint_family)

    Deliberately excludes every field in DIFFABLE_FIELDS. If auth-presence
    were part of identity, an attacker repeating a technique would just
    spawn "new, unmatched patterns" forever instead of ever tripping the
    deviation check against the legitimate one.
    """
    return (
        alert["detection_rule_id"],
        alert["parent_image"],
        alert["child_image"],
        alert["endpoint_family"],
    )


def build_structural_template(
    confirmed_negative_instances: list[dict], provenance: str = "live"
) -> dict:
    """
    Returns:
        {
            "fields": {field: invariant_value, ...},  # only 100%-consistent
            "tier": "provisional" | "confirmed",
            "provenance": "live" | "seeded",
            "instance_count": int,
        }

    provenance "seeded" = bulk-imported (e.g. dataset case #1) rather than
    earned one live human confirmation at a time. Structurally identical
    once built, but worth tracking so a bad seeded assumption can be traced
    later rather than poisoning decisions invisibly.
    """
    fields = {}
    for field in DIFFABLE_FIELDS:
        values = {instance[field] for instance in confirmed_negative_instances}
        if len(values) == 1:
            fields[field] = values.pop()

    count = len(confirmed_negative_instances)
    return {
        "fields": fields,
        "tier": "confirmed" if count >= GRADUATION_THRESHOLD else "provisional",
        "provenance": provenance,
        "instance_count": count,
    }


def diff_alert_against_template(alert: dict, template_fields: dict) -> list[str]:
    """
    Exhaustive diff — every field checked, never short-circuits on first
    mismatch. Returns human-readable deviation strings, empty list if none.
    """
    deviations = []
    for field, expected in template_fields.items():
        observed = alert.get(field)
        if observed != expected:
            deviations.append(f"{field}: template={expected!r}, observed={observed!r}")
    return deviations
