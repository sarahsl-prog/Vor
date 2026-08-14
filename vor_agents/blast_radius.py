"""
Vör — Blast radius estimation. Hybrid: a curated table (trusted,
authoritative) plus a gated proposal path for new/unscored patterns.

See BLAST_RADIUS_PLAYBOOK.md for the tier criteria every entry and every
proposal must be justified against.

Design principle, consistent with the rest of this system: a LOW score
means "audit this less" — that's the dangerous direction, so it can never
be written autonomously. Same asymmetry as the auditor's DOWNGRADE
(autonomous, safe) vs RECOMMEND_UPGRADE (human-gated, risky): scoring
something HIGH/CRITICAL is the conservative move and can happen freely;
scoring it MEDIUM/LOW requires a human to actually commit it.
"""

CRITICAL = 0.95
HIGH = 0.75
MEDIUM = 0.45
LOW = 0.15
UNSCORED_DEFAULT = HIGH  # never silently treat an unassessed pattern as safe

# Keyed by (indicator_type, value) -> score. indicator_type matches a field
# name that may appear on an alert dict: "parent_image", "endpoint_family",
# or similar structural indicators — not full identity keys, since the same
# indicator (e.g. a given parent process) should carry consistent risk
# across every pattern it appears in, not be re-scored per pattern.
BLAST_RADIUS_TABLE: dict[tuple[str, str], float] = {
    ("parent_image", "lsass.exe"): CRITICAL,
    ("endpoint_family", "ToolPane_admin"): CRITICAL,  # CVE-2026-56164 model
    ("parent_image", "w3wp.exe"): HIGH,
    ("parent_image", "svchost.exe"): MEDIUM,
    ("parent_image", "explorer.exe"): LOW,
    # New entries: follow BLAST_RADIUS_PLAYBOOK.md. CRITICAL/HIGH may be
    # added directly. MEDIUM/LOW must go through propose_blast_radius()
    # and a human review — never write those tiers here without one.
}


def estimate_blast_radius(alert: dict) -> float:
    """
    Checks every indicator present on the alert against
    BLAST_RADIUS_TABLE, returns the MAX matching score — blast radius is
    a worst-case estimate, not an average, so if an alert matches both a
    MEDIUM indicator and a CRITICAL one, CRITICAL wins.

    Falls back to UNSCORED_DEFAULT (HIGH, deliberately not LOW or zero)
    when nothing matches, so an unassessed pattern gets prioritized for
    audit attention rather than silently trusted by omission.
    """
    matches = [
        score
        for (indicator_type, value), score in BLAST_RADIUS_TABLE.items()
        if alert.get(indicator_type) == value
    ]
    return max(matches) if matches else UNSCORED_DEFAULT


def propose_blast_radius(
    identity_key: tuple,
    proposed_tier: str,
    proposed_score: float,
    cited_indicators: list[str],
    rationale: str,
) -> dict:
    """
    NOT auto-applied to BLAST_RADIUS_TABLE under any circumstances. Callable
    by a human directly, or by an LLM step (e.g. an extended auditor pass)
    proposing a new pattern be scored — either way this returns an inert
    record. Promoting it into BLAST_RADIUS_TABLE is a manual code change
    made after review against BLAST_RADIUS_PLAYBOOK.md, particularly for
    any MEDIUM/LOW proposal, which is the direction that reduces scrutiny.
    """
    return {
        "identity_key": identity_key,
        "proposed_tier": proposed_tier,
        "proposed_score": proposed_score,
        "cited_indicators": cited_indicators,
        "rationale": rationale,
        "status": "pending_human_review",
        "requires_review": proposed_tier in ("MEDIUM", "LOW"),
    }
