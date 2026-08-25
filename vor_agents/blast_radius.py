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

import hashlib
import json
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from google.cloud.firestore import Client
from loguru import logger

from .env_config import env_int

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
BLAST_RADIUS_PROPOSALS_COLLECTION = "blast_radius_proposals"

_TABLE_CACHE: dict[tuple[str, str], float] = {}
_TABLE_CACHE_LOADED_AT: float | None = None

DEFAULT_BLAST_RADIUS_CACHE_TTL_SECONDS = 300
BLAST_RADIUS_CACHE_TTL_ENV_VAR = "BLAST_RADIUS_CACHE_TTL_SECONDS"
# Per-process, TTL'd cache -- _fetch_all_confirmed_patterns() calls
# estimate_blast_radius() once per confirmed instance per sweep; a
# Firestore read per call would turn one sweep into O(instances) reads
# for a table that changes rarely. 5 minutes is an unvalidated starting
# point, same posture as GRADUATION_THRESHOLD elsewhere in this design --
# which is exactly why it's worth being able to retune it from a deploy
# flag rather than a code change.
#
# minimum=0 rather than 1: 0 means "never serve from cache", a legitimate
# setting for debugging a stale table or for a deployment that would
# rather pay the Firestore reads. Negative is meaningless.


def _blast_radius_cache_ttl_seconds() -> int:
    """TTL for the in-process blast-radius table cache, from
    $BLAST_RADIUS_CACHE_TTL_SECONDS. Read per call, not bound at import."""
    return env_int(
        BLAST_RADIUS_CACHE_TTL_ENV_VAR, DEFAULT_BLAST_RADIUS_CACHE_TTL_SECONDS, minimum=0
    )


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
        and (now - _TABLE_CACHE_LOADED_AT) < _blast_radius_cache_ttl_seconds()
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


def _table_doc_id(indicator_type: str, value: str) -> str:
    """Content hash, not a raw f-string join -- same collision-avoidance
    reasoning as enrichment._doc_id() and task_queue._task_name()."""
    encoded = json.dumps([indicator_type, value], separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _parse_cited_indicator(indicator: str) -> tuple[str, str]:
    """
    cited_indicators entries are "indicator_type=value" strings (e.g.
    "parent_image=lsass.exe") -- the format propose_blast_radius()'s
    callers (human or an extended auditor LLM step) are expected to use.
    Raises ValueError on anything else, same "fail loud on a malformed
    proposal rather than silently write a wrong table entry" posture as
    the tier/score validation already in this function.
    """
    if "=" not in indicator:
        raise ValueError(
            f"Malformed cited_indicator {indicator!r}; expected 'indicator_type=value'"
        )
    indicator_type, value = indicator.split("=", 1)
    return indicator_type.strip(), value.strip()


def _commit_indicators(cited_indicators: list[str], score: float, firestore_client: Client) -> None:
    """Writes each cited indicator into blast_radius_table at the given
    score, then invalidates the read cache so the next
    estimate_blast_radius() call sees it without waiting out the TTL."""
    for indicator in cited_indicators:
        indicator_type, value = _parse_cited_indicator(indicator)
        doc_id = _table_doc_id(indicator_type, value)
        firestore_client.collection(BLAST_RADIUS_TABLE_COLLECTION).document(doc_id).set(
            {
                "indicator_type": indicator_type,
                "value": value,
                "score": score,
                "committed_at": datetime.now(UTC).isoformat(),
            },
            merge=True,
        )
    _invalidate_table_cache()


class ProposalNotFoundError(Exception):
    """Raised when POST /blast-radius/commit references a proposal_id
    that doesn't exist in blast_radius_proposals."""


class ProposalAlreadyResolvedError(Exception):
    """Raised when a commit is attempted on a proposal whose status isn't
    pending_human_review -- no double-commit, whether it was already
    manually committed or was auto-committed at proposal time (CRITICAL/
    HIGH)."""


def propose_blast_radius(
    identity_key: tuple[str, ...],
    proposed_tier: str,
    proposed_score: float,
    cited_indicators: list[str],
    rationale: str,
    firestore_client: Client,
) -> dict[str, Any]:
    """
    Validates tier/score exactly as before (raises ValueError for an
    unknown tier or an out-of-range score -- unchanged). New in this
    revision: persists the proposal to blast_radius_proposals instead of
    just returning an inert dict, and CRITICAL/HIGH proposals commit
    directly into blast_radius_table in the same call (the conservative
    direction -- matches BLAST_RADIUS_PLAYBOOK.md's "may be added
    directly" language). MEDIUM/LOW proposals are written with
    status="pending_human_review" and NOT committed -- see
    commit_blast_radius_proposal() for the human-gated commit path.
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

    requires_review = proposed_tier in ("MEDIUM", "LOW")
    proposal: dict[str, Any] = {
        "proposal_id": str(uuid.uuid4()),
        "identity_key": list(identity_key),
        "proposed_tier": proposed_tier,
        "proposed_score": proposed_score,
        "cited_indicators": cited_indicators,
        "rationale": rationale,
        "proposed_at": datetime.now(UTC).isoformat(),
        "status": "pending_human_review",
        "requires_review": requires_review,
    }

    if not requires_review:
        _commit_indicators(cited_indicators, proposed_score, firestore_client)
        proposal["status"] = "committed"

    firestore_client.collection(BLAST_RADIUS_PROPOSALS_COLLECTION).document(
        proposal["proposal_id"]
    ).set(proposal)
    return proposal


def commit_blast_radius_proposal(proposal_id: str, firestore_client: Client) -> dict[str, Any]:
    """
    Human-triggered commit for a pending MEDIUM/LOW proposal -- see
    main.py's POST /blast-radius/commit, the only caller. Writes the
    proposal's cited indicators into blast_radius_table at its
    proposed_score, marks the proposal committed, returns the updated
    proposal dict.
    """
    doc_ref = firestore_client.collection(BLAST_RADIUS_PROPOSALS_COLLECTION).document(proposal_id)
    doc = doc_ref.get()
    if not doc.exists:
        raise ProposalNotFoundError(f"No blast-radius proposal with id {proposal_id!r}")

    data = doc.to_dict() or {}
    if data.get("status") != "pending_human_review":
        raise ProposalAlreadyResolvedError(
            f"Proposal {proposal_id!r} already has status {data.get('status')!r}, "
            "not pending_human_review"
        )

    _commit_indicators(data["cited_indicators"], data["proposed_score"], firestore_client)
    committed_at = datetime.now(UTC).isoformat()
    doc_ref.update({"status": "committed", "committed_at": committed_at})
    data["status"] = "committed"
    data["committed_at"] = committed_at
    return data
