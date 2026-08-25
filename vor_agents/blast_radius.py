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

import time
from typing import Any

from google.cloud.firestore import Client
from loguru import logger

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

BLAST_RADIUS_TABLE_COLLECTION = "blast_radius_table"

_TABLE_CACHE: dict[tuple[str, str], float] = {}
_TABLE_CACHE_LOADED_AT: float | None = None
_TABLE_CACHE_TTL_SECONDS = 300
# Per-process, TTL'd cache -- _fetch_all_confirmed_patterns() calls
# estimate_blast_radius() once per confirmed instance per sweep; a
# Firestore read per call would turn one sweep into O(instances) reads
# for a table that changes rarely. 5 minutes is an unvalidated starting
# point, same posture as GRADUATION_THRESHOLD elsewhere in this design.


def reset_table_cache() -> None:
    """Test-only reset hook -- module-level cache state persists across
    tests in the same process otherwise. Not called anywhere in
    production code."""
    global _TABLE_CACHE, _TABLE_CACHE_LOADED_AT
    _TABLE_CACHE = {}
    _TABLE_CACHE_LOADED_AT = None


def _invalidate_table_cache() -> None:
    """Called after a commit writes new entries, so the next read sees
    them without waiting out the full TTL."""
    global _TABLE_CACHE_LOADED_AT
    _TABLE_CACHE_LOADED_AT = None


def _load_table(firestore_client: Client) -> dict[tuple[str, str], float]:
    """
    Returns the cached (indicator_type, value) -> score table, refreshing
    from Firestore if the cache is missing or past its TTL. On a refresh
    failure: serves the previous cache if one exists (a stale table is a
    much safer failure mode than an unhandled exception breaking
    estimate_blast_radius() and, transitively, the whole sweep); falls
    back to an empty table (every lookup then returns UNSCORED_DEFAULT,
    same "unassessed defaults to HIGH, never silently trusted" principle
    this whole module already runs on) if the cache has never been
    populated at all.
    """
    global _TABLE_CACHE, _TABLE_CACHE_LOADED_AT
    now = time.monotonic()
    if (
        _TABLE_CACHE_LOADED_AT is not None
        and (now - _TABLE_CACHE_LOADED_AT) < _TABLE_CACHE_TTL_SECONDS
    ):
        return _TABLE_CACHE

    try:
        fresh: dict[tuple[str, str], float] = {}
        for doc in firestore_client.collection(BLAST_RADIUS_TABLE_COLLECTION).stream():
            data = doc.to_dict() or {}
            indicator_type = data.get("indicator_type")
            value = data.get("value")
            score = data.get("score")
            if indicator_type is None or value is None or score is None:
                logger.bind(doc_id=doc.id).warning(
                    "blast_radius_table doc missing indicator_type/value/score, skipping"
                )
                continue
            fresh[(indicator_type, value)] = score
        _TABLE_CACHE = fresh
        _TABLE_CACHE_LOADED_AT = now
        return _TABLE_CACHE
    except Exception as exc:  # noqa: BLE001 — deliberate catch-all: any
        # Firestore failure here degrades to stale-or-empty, never raises.
        if _TABLE_CACHE_LOADED_AT is not None:
            logger.bind(error=str(exc)).warning(
                "Failed to refresh blast_radius_table cache, serving stale cache"
            )
            return _TABLE_CACHE
        logger.bind(error=str(exc)).warning(
            "Failed to load blast_radius_table cache and no prior cache exists; "
            "every lookup will fall back to UNSCORED_DEFAULT"
        )
        return {}


def estimate_blast_radius(alert: dict[str, Any], firestore_client: Client) -> float:
    """
    Checks every indicator present on the alert against the cached
    blast_radius_table (Firestore-backed, see _load_table), returns the
    MAX matching score -- blast radius is a worst-case estimate, not an
    average. Falls back to UNSCORED_DEFAULT (HIGH, deliberately not LOW
    or zero) when nothing matches or the table is unavailable, so an
    unassessed pattern gets prioritized for audit attention rather than
    silently trusted by omission.
    """
    table = _load_table(firestore_client)
    matches = [
        score
        for (indicator_type, value), score in table.items()
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
