"""
One-time migration: backfill the `identity_key` field onto confidence docs
written before the doc-ID scheme changed, and re-key those docs under the
new content-hash doc ID.

Background (docs/TODO-Aug15.md Task 3, README "Known gaps"): confidence
docs used to be stored under a `"_".join(identity_key)` doc ID and carried
no `identity_key` field. That join was ambiguous and lossy -- ("a", "b_c")
and ("a_b", "c") both joined to "a_b_c" -- so `_doc_id()` now hashes the
tuple instead, and every write path stores `identity_key` as a first-class
array field. Readers (`_fetch_all_confirmed_patterns()`) read that field
and *skip* any doc missing it, logging a warning. That means legacy docs
are silently invisible to the sweep rather than crashing it -- which is
safe, but means a pre-existing pattern stops being audited entirely until
this migration runs.

How the identity_key is recovered: NOT by splitting the legacy doc ID,
which is exactly the ambiguity this migration exists to escape. It is
rebuilt from the doc's own `confirmed_instances`, each of which carries
the four identity fields verbatim (detection_rule_id, parent_image,
child_image, endpoint_family) -- an unambiguous, lossless source.

Run once, before first production deploy against pre-existing data -- see
docs/DEPLOY.md. Idempotent: docs that already have an `identity_key` field
are counted and skipped, so re-running is safe and does nothing.

Usage:
    .venv/bin/python scripts/backfill_identity_key.py --dry-run
    .venv/bin/python scripts/backfill_identity_key.py
"""

import argparse
import sys
from typing import Any

from google.cloud import firestore
from google.cloud.firestore import Client
from loguru import logger

from vor_agents.enrichment import CONFIDENCE_COLLECTION, _doc_id
from vor_agents.identity import pattern_identity_key


class BackfillError(Exception):
    """
    Raised when a legacy doc cannot be migrated -- almost always because
    its confirmed_instances are missing or lack the identity fields, so
    there is no unambiguous way to recover its identity_key. Surfaced as
    a per-doc skip with a reason rather than aborting the whole run: one
    unrecoverable legacy doc must not block migrating the rest.
    """


def _recover_identity_key(data: dict[str, Any]) -> tuple[str, ...]:
    """
    Rebuilds the identity_key from the doc's stored confirmed_instances.

    Every instance in a given doc belongs to the same pattern by
    definition, so the first usable one is authoritative -- but they are
    all checked for agreement anyway: a doc whose instances disagree on
    their identity fields is corrupt in a way this migration must not
    paper over by silently picking the first.
    """
    instances = data.get("confirmed_instances", [])
    if not instances:
        raise BackfillError("no confirmed_instances to recover the identity_key from")

    recovered: set[tuple[str, ...]] = set()
    for instance in instances:
        try:
            recovered.add(pattern_identity_key(instance))
        except KeyError as exc:
            raise BackfillError(f"confirmed_instance is missing identity field {exc}") from exc

    if len(recovered) > 1:
        raise BackfillError(
            f"confirmed_instances disagree on their identity_key: {sorted(recovered)}"
        )
    return recovered.pop()


def backfill(firestore_client: Client, dry_run: bool = False) -> dict[str, int]:
    """
    Walks every confidence doc, migrating any that predate the
    identity_key field. Returns a counts summary
    {"migrated", "already_current", "skipped"} so the caller can report
    it and a re-run can be seen to be a no-op.

    A migrated doc is written under its NEW hashed doc ID and the legacy
    doc is deleted, because the doc ID itself is part of what changed --
    leaving the old ID in place would strand a duplicate that readers
    still can't address. Write-then-delete, in that order: an interrupted
    run leaves a readable duplicate (recoverable, and the re-run cleans
    it up) rather than deleting data whose replacement never landed.
    """
    counts = {"migrated": 0, "already_current": 0, "skipped": 0}

    # Materialized before any write, deliberately: this migration adds and
    # deletes docs in the very collection it is scanning, and mutating a
    # collection mid-stream is unsafe both against a live Firestore query
    # and against the in-memory test double.
    scanned = [
        (doc.id, doc.to_dict() or {})
        for doc in firestore_client.collection(CONFIDENCE_COLLECTION).stream()
    ]

    for doc_id, data in scanned:

        if data.get("identity_key"):
            counts["already_current"] += 1
            continue

        try:
            identity_key = _recover_identity_key(data)
        except BackfillError as exc:
            logger.bind(doc_id=doc_id).error("Cannot migrate doc, skipping: {}", exc)
            counts["skipped"] += 1
            continue

        new_doc_id = _doc_id(identity_key)
        logger.bind(doc_id=doc_id, new_doc_id=new_doc_id, identity_key=identity_key).info(
            "Migrating legacy confidence doc"
        )

        if dry_run:
            counts["migrated"] += 1
            continue

        collection = firestore_client.collection(CONFIDENCE_COLLECTION)
        collection.document(new_doc_id).set(
            {**data, "identity_key": list(identity_key)}, merge=True
        )
        if new_doc_id != doc_id:
            collection.document(doc_id).delete()
        counts["migrated"] += 1

    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be migrated without writing or deleting anything.",
    )
    args = parser.parse_args()

    counts = backfill(firestore.Client(), dry_run=args.dry_run)

    prefix = "[dry-run] would migrate" if args.dry_run else "Migrated"
    print(
        f"{prefix} {counts['migrated']} doc(s); "
        f"{counts['already_current']} already current; "
        f"{counts['skipped']} skipped (see logged reasons)."
    )
    # Non-zero exit on skips: a skipped doc is a pattern that stays
    # invisible to the sweep, which is exactly the condition this
    # migration exists to clear -- it should not read as a clean run.
    return 1 if counts["skipped"] else 0


if __name__ == "__main__":
    sys.exit(main())
