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

from typing import Any

CRITICAL = 0.95
HIGH = 0.75
MEDIUM = 0.45
LOW = 0.15
UNSCORED_DEFAULT = HIGH  # never silently treat an unassessed pattern as safe

# Score ranges per BLAST_RADIUS_PLAYBOOK.md's Tiers section — kept here
# rather than re-derived, since propose_blast_radius() needs the actual
# bounds, not just the representative point value each tier constant
# above holds. Inclusive on both ends, matching the playbook's "0.90-1.0"
# style ranges exactly.
TIER_RANGES: dict[str, tuple[float, float]] = {
    "CRITICAL": (0.90, 1.0),
    "HIGH": (0.60, 0.89),
    "MEDIUM": (0.30, 0.59),
    "LOW": (0.0, 0.29),
}

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


def estimate_blast_radius(alert: dict[str, Any]) -> float:
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
    identity_key: tuple[str, ...],
    proposed_tier: str,
    proposed_score: float,
    cited_indicators: list[str],
    rationale: str,
) -> dict[str, Any]:
    """
    NOT auto-applied to BLAST_RADIUS_TABLE under any circumstances. Callable
    by a human directly, or by an LLM step (e.g. an extended auditor pass)
    proposing a new pattern be scored — either way this returns an inert
    record. Promoting it into BLAST_RADIUS_TABLE is a manual code change
    made after review against BLAST_RADIUS_PLAYBOOK.md, particularly for
    any MEDIUM/LOW proposal, which is the direction that reduces scrutiny.

    Raises ValueError for an unknown proposed_tier or a proposed_score
    outside that tier's documented range (TIER_RANGES, from
    BLAST_RADIUS_PLAYBOOK.md's Tiers section) — previously an unknown
    tier silently fell through requires_review's `in ("MEDIUM", "LOW")`
    check as False, and a tier/score mismatch (e.g. LOW with a CRITICAL-
    range score) was accepted without complaint. Both are weak-API-
    contract bugs a human reviewer downstream could be misled by, not
    just cosmetic — the whole point of TIER_RANGES existing is that a
    tier and its score are supposed to agree.
    """
    if proposed_tier not in TIER_RANGES:
        raise ValueError(
            f"Unknown blast-radius tier {proposed_tier!r}; must be one of " f"{sorted(TIER_RANGES)}"
        )
    low, high = TIER_RANGES[proposed_tier]
    if not (low <= proposed_score <= high):
        raise ValueError(
            f"proposed_score {proposed_score} is outside {proposed_tier}'s "
            f"documented range [{low}, {high}] (see BLAST_RADIUS_PLAYBOOK.md)"
        )

    return {
        "identity_key": identity_key,
        "proposed_tier": proposed_tier,
        "proposed_score": proposed_score,
        "cited_indicators": cited_indicators,
        "rationale": rationale,
        "status": "pending_human_review",
        "requires_review": proposed_tier in ("MEDIUM", "LOW"),
    }
