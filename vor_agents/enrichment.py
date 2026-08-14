"""
Vör — Enrichment: pure Firestore reads + aggregation, no LLM calls.

Runs BEFORE the classifier agent is ever invoked. Produces the payload the
classifier prompt describes as "enrichment context" — the agent never
fetches this itself.
"""

import uuid

from .identity import build_structural_template, pattern_identity_key

CONFIDENCE_COLLECTION = "confidence_docs"


def _doc_id(identity_key: tuple) -> str:
    return "_".join(identity_key)


def enrich(alert: dict, firestore_client) -> dict:
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
        }

    This dict is what gets serialized into the classifier's prompt context
    — the agent reasons over exactly this, nothing more, nothing fetched
    on its own. diversity_score here is informational context for the
    classifier's reasoning (not a gate — graduation already enforced
    MIN_DIVERSITY before this pattern could reach "confirmed" tier at
    all); it is NOT used to re-decide tier at classification time.
    """
    identity_key = pattern_identity_key(alert)
    doc_ref = firestore_client.collection(CONFIDENCE_COLLECTION).document(
        _doc_id(identity_key)
    )
    doc = doc_ref.get()

    if not doc.exists:
        return {"status": "NO_HISTORY", "pattern_identity_key": identity_key}

    data = doc.to_dict()
    return {
        "status": "TEMPLATE",
        "pattern_identity_key": identity_key,
        "fields": data.get("fields", {}),
        "tier": data.get("tier", "provisional"),
        "provenance": data.get("provenance", "live"),
        "under_review": data.get("under_review", False),
        "days_since_last_review": data.get("days_since_last_review", 0),
        "diversity_score": data.get("diversity_score", 0.0),
    }


def record_confirmed_negative(
    alert: dict, firestore_client, human_confirmed: bool = True
) -> dict:
    """
    Called when a human (or a sufficiently-sized seed batch) confirms an
    alert as a true negative. Appends to the pattern's confirmed-instance
    history and rebuilds the template — this is the graduation path from
    NO_HISTORY / provisional toward confirmed.

    Deliberately separate from enrich() — reads and writes to the same
    collection shouldn't share a function, so the "does this alert change
    trust" question is never accidentally answered by the same code path
    that's just checking current trust.
    """
    identity_key = pattern_identity_key(alert)
    doc_ref = firestore_client.collection(CONFIDENCE_COLLECTION).document(
        _doc_id(identity_key)
    )
    doc = doc_ref.get()
    instances = doc.to_dict().get("confirmed_instances", []) if doc.exists else []
    # Every stored instance gets a stable ID so the auditor can later
    # point at specific instances to invalidate rather than only being
    # able to distrust the pattern as a whole (see invalidate_instances()).
    # Preserve an existing instance_id if the alert already has one
    # (matches seed_template()'s behavior) rather than always minting a
    # new one — found via testing: this function previously always
    # overwrote instance_id, which silently discarded IDs a caller had
    # already assigned.
    instances.append({**alert, "instance_id": alert.get("instance_id", str(uuid.uuid4()))})

    template = build_structural_template(instances, provenance="live")
    doc_ref.set(
        {
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
    identity_key: tuple, confirmed_negative_instances: list[dict], firestore_client
) -> dict:
    """
    Bulk-import path for synthetic/historical data (dataset case #1).
    Can enter directly at "confirmed" tier if the seed batch already meets
    GRADUATION_THRESHOLD — that's the entire point of pre-seeding instead
    of waiting on live graduation one alert at a time.
    """
    seeded_instances = [
        {**instance, "instance_id": instance.get("instance_id", str(uuid.uuid4()))}
        for instance in confirmed_negative_instances
    ]
    template = build_structural_template(seeded_instances, provenance="seeded")
    doc_ref = firestore_client.collection(CONFIDENCE_COLLECTION).document(
        _doc_id(identity_key)
    )
    doc_ref.set(
        {
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
    identity_key: tuple, instance_ids_to_remove: list[str], firestore_client
) -> dict:
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
    doc_ref = firestore_client.collection(CONFIDENCE_COLLECTION).document(
        _doc_id(identity_key)
    )
    doc = doc_ref.get()
    instances = doc.to_dict().get("confirmed_instances", []) if doc.exists else []
    remaining = [
        instance for instance in instances
        if instance.get("instance_id") not in instance_ids_to_remove
    ]

    provenance = doc.to_dict().get("provenance", "live") if doc.exists else "live"
    template = build_structural_template(remaining, provenance=provenance)
    return {
        "confirmed_instances": remaining,
        "fields": template["fields"],
        "tier": template["tier"],
        "instance_count": template["instance_count"],
        "diversity_score": template["diversity_score"],
    }
