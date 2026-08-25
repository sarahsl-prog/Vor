"""
Vör — Enrichment: pure Firestore reads + aggregation, no LLM calls.

Runs BEFORE the classifier agent is ever invoked. Produces the payload the
classifier prompt describes as "enrichment context" — the agent never
fetches this itself.
"""

import hashlib
import json
import uuid
from typing import Any

from google.cloud.firestore import Client

from .identity import build_structural_template, pattern_identity_key

CONFIDENCE_COLLECTION = "confidence_docs"


def _doc_id(identity_key: tuple[str, ...]) -> str:
    """
    Doc ID is a content hash of the identity_key tuple, not a "_"-joined
    string. The join-based scheme was ambiguous and lossy: identity_key
    components (rule IDs, process names) aren't guaranteed to be
    underscore-free, so ("a", "b_c"), ("a_b", "c"), and ("a", "b", "c")
    all joined to the same "a_b_c" doc ID, and splitting it back apart
    couldn't tell which was which.

    The doc ID no longer needs to be reversible — every write path now
    also stores the identity_key as a first-class array field on the doc
    itself (see record_confirmed_negative / seed_template), and readers
    that need the tuple back (e.g. _fetch_all_confirmed_patterns) read
    that field instead of parsing the ID. json.dumps with sorted
    separators guarantees the same tuple always hashes to the same ID
    regardless of any incidental formatting differences.
    """
    encoded = json.dumps(list(identity_key), separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def enrich(alert: dict[str, Any], firestore_client: Client) -> dict[str, Any]:
    """
    Returns either:
        {"status": "NO_HISTORY"}
    or:
        {
            "status": "TEMPLATE",
            "pattern_identity_key": tuple,
            "fields": {...},
            "tier": "provisional" | "confirmed",
            "provenance": "live" | "seeded",
            "under_review": bool,
            "days_since_last_review": int,
            "diversity_score": float,
            "failure_count": int,
        }

    This dict is what gets serialized into the classifier's prompt context
    — the agent reasons over exactly this, nothing more, nothing fetched
    on its own. diversity_score here is informational context for the
    classifier's reasoning (not a gate — graduation already enforced
    MIN_DIVERSITY before this pattern could reach "confirmed" tier at
    all); it is NOT used to re-decide tier at classification time.
    """
    identity_key = pattern_identity_key(alert)
    doc_ref = firestore_client.collection(CONFIDENCE_COLLECTION).document(_doc_id(identity_key))
    doc = doc_ref.get()

    if not doc.exists:
        return {"status": "NO_HISTORY", "pattern_identity_key": identity_key}

    # doc.to_dict() is None exactly when the doc doesn't exist — already
    # ruled out above — but mypy can't correlate that with doc.exists
    # (a separate attribute), so `or {}` makes the non-None guarantee
    # explicit for the type checker too, not just at runtime.
    data = doc.to_dict() or {}
    return {
        "status": "TEMPLATE",
        "pattern_identity_key": identity_key,
        "fields": data.get("fields", {}),
        "tier": data.get("tier", "provisional"),
        "provenance": data.get("provenance", "live"),
        "under_review": data.get("under_review", False),
        "days_since_last_review": data.get("days_since_last_review", 0),
        "diversity_score": data.get("diversity_score", 0.0),
        "failure_count": data.get("failure_count", 0),
    }


def record_confirmed_negative(
    alert: dict[str, Any], firestore_client: Client, human_confirmed: bool = True
) -> dict[str, Any]:
    """
    Called when a human (or a sufficiently-sized seed batch) confirms an
    alert as a true negative. Appends to the pattern's confirmed-instance
    history and rebuilds the template — this is the graduation path from
    NO_HISTORY / provisional toward confirmed.

    Deliberately separate from enrich() — reads and writes to the same
    collection shouldn't share a function, so the "does this alert change
    trust" question is never accidentally answered by the same code path
    that's just checking current trust.

    human_confirmed distinguishes a real per-alert human sign-off from a
    caller confirming on the pattern's behalf without one (e.g. a bulk
    "confirm everything matching this template" tool). Previously accepted
    but silently discarded — every instance looked identically trusted no
    matter how it was confirmed, which is exactly the "absence of
    complaint is not confirmation" gap the auditor prompt already warns
    about for unverified instances. Tagged per-instance (verified_by, not
    a doc-level field) because confirmed_instances accumulates across many
    calls to this function — a doc-level field would only ever reflect the
    most recent call and silently mislabel every earlier instance.
    """
    identity_key = pattern_identity_key(alert)
    doc_ref = firestore_client.collection(CONFIDENCE_COLLECTION).document(_doc_id(identity_key))
    doc = doc_ref.get()
    # doc.to_dict() is None exactly when the doc doesn't exist, so `or {}`
    # collapses what used to be an explicit `if doc.exists else []` —
    # same result, and makes the non-None case explicit for mypy too.
    instances = (doc.to_dict() or {}).get("confirmed_instances", [])
    # Every stored instance gets a stable ID so the auditor can later
    # point at specific instances to invalidate rather than only being
    # able to distrust the pattern as a whole (see invalidate_instances()).
    # Preserve an existing instance_id if the alert already has one
    # (matches seed_template()'s behavior) rather than always minting a
    # new one — found via testing: this function previously always
    # overwrote instance_id, which silently discarded IDs a caller had
    # already assigned.
    instances.append(
        {
            **alert,
            "instance_id": alert.get("instance_id", str(uuid.uuid4())),
            "verified_by": "human" if human_confirmed else "bulk",
        }
    )

    template = build_structural_template(instances, provenance="live")
    doc_ref.set(
        {
            # Stored as a first-class field, not just encoded into the doc
            # ID — see _doc_id()'s docstring. list(), not the tuple itself:
            # Firestore has no tuple type and would silently coerce it to
            # a list anyway; storing it explicitly as a list keeps the
            # round-trip (list -> tuple(...) on read) obvious rather than
            # implicit.
            "identity_key": list(identity_key),
            "confirmed_instances": instances,
            "fields": template["fields"],
            "tier": template["tier"],
            "provenance": template["provenance"],
            "instance_count": template["instance_count"],
            "diversity_score": template["diversity_score"],
            "under_review": False,
        },
        merge=True,
    )
    return template


def seed_template(
    identity_key: tuple[str, ...],
    confirmed_negative_instances: list[dict[str, Any]],
    firestore_client: Client,
) -> dict[str, Any]:
    """
    Bulk-import path for synthetic/historical data (dataset case #1).
    Can enter directly at "confirmed" tier if the seed batch already meets
    GRADUATION_THRESHOLD — that's the entire point of pre-seeding instead
    of waiting on live graduation one alert at a time.

    verified_by is "bulk" for every instance here, same as
    record_confirmed_negative(human_confirmed=False) — no per-alert human
    signed off on any of these individually, regardless of how trustworthy
    the source dataset is.
    """
    seeded_instances = [
        {
            **instance,
            "instance_id": instance.get("instance_id", str(uuid.uuid4())),
            "verified_by": "bulk",
        }
        for instance in confirmed_negative_instances
    ]
    template = build_structural_template(seeded_instances, provenance="seeded")
    doc_ref = firestore_client.collection(CONFIDENCE_COLLECTION).document(_doc_id(identity_key))
    doc_ref.set(
        {
            "identity_key": list(identity_key),
            "confirmed_instances": seeded_instances,
            "fields": template["fields"],
            "tier": template["tier"],
            "provenance": template["provenance"],
            "instance_count": template["instance_count"],
            "diversity_score": template["diversity_score"],
            "under_review": False,
        },
        merge=True,
    )
    return template


def invalidate_instances(
    identity_key: tuple[str, ...], instance_ids_to_remove: list[str], firestore_client: Client
) -> dict[str, Any]:
    """
    Targeted evidence invalidation — the auditor's DOWNGRADE mechanism.
    Removes only the cited instance_ids from the pool and rebuilds the
    template from whatever remains. Tier is a CONSEQUENCE of this, not a
    separate value the caller sets: if enough clean instances remain, the
    pattern can stay "confirmed" with a corrected template; if not, it
    naturally falls back to "provisional" via build_structural_template()'s
    own threshold check. No blanket punishment for the whole pattern over
    evidence that was fine.

    Does NOT write under_review here — caller (review_flag.clear_under_review)
    is responsible for combining this result with clearing that flag in one
    write, so there's no gap between confidence data landing and the flag
    clearing.
    """
    doc_ref = firestore_client.collection(CONFIDENCE_COLLECTION).document(_doc_id(identity_key))
    doc = doc_ref.get()
    # See record_confirmed_negative() above for why `or {}` (not the
    # `if doc.exists else` ternary this replaced) is both simpler and
    # what makes the non-None case explicit for mypy.
    data = doc.to_dict() or {}
    instances = data.get("confirmed_instances", [])
    remaining = [
        instance
        for instance in instances
        if instance.get("instance_id") not in instance_ids_to_remove
    ]

    provenance = data.get("provenance", "live")
    template = build_structural_template(remaining, provenance=provenance)
    return {
        "confirmed_instances": remaining,
        "fields": template["fields"],
        "tier": template["tier"],
        "instance_count": template["instance_count"],
        "diversity_score": template["diversity_score"],
    }
