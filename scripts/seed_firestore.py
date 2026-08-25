"""
Seed a real Firestore instance with confirmed-negative history.

`enrichment.seed_template()` has always been the bulk-import path (dataset
case #1), but it was only ever reachable from tests -- there was no way to
point it at a real project. This is that entrypoint.

Two sources, because they answer different needs:

  --case <name>   one of the 6 synthetic cases (vor_agents/datasets.py).
                  For demos, a fresh dev project, or sanity-checking the
                  graduation gate against known-shaped evidence.

  --file <path>   a JSON file of real historical confirmed-negative
                  instances. This is the production path -- your own
                  Hayabusa/EVTX history, exported however you like.

Both land in the same place: seed_template(), which stamps every instance
verified_by "bulk" and provenance "seeded". That labelling is deliberate
and must not be worked around -- no human signed off on these individually,
however trustworthy the source, and the auditor is entitled to know that.

Usage:
    .venv/bin/python scripts/seed_firestore.py --case seeded_confirmed --dry-run
    .venv/bin/python scripts/seed_firestore.py --case seeded_confirmed
    .venv/bin/python scripts/seed_firestore.py --file history.json

The --file JSON must be a list of alert objects, each carrying the four
identity fields and all five DIFFABLE_FIELDS. Instances are grouped by
identity key automatically, so one file may contain many patterns.
"""

import sys
from pathlib import Path

# Running this file directly puts scripts/ on sys.path, not the repo root,
# so `import vor_agents` would fail for the documented invocation
# (`python scripts/<name>.py`). Tests import it fine via pytest's
# pythonpath=".", which is exactly why this was easy to miss.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import json
from collections import defaultdict
from typing import Any

from google.cloud import firestore
from google.cloud.firestore import Client
from loguru import logger

from vor_agents.datasets import DatasetCase, UnknownDatasetCaseError, generate_case
from vor_agents.enrichment import seed_template
from vor_agents.identity import (
    MalformedAlertError,
    build_structural_template,
    pattern_identity_key,
)


class SeedInputError(Exception):
    """
    Raised on malformed --file input: not JSON, not a list, or instances
    missing required fields. Validated up front rather than letting
    seed_template() fail partway through -- a half-seeded Firestore is
    much worse to recover from than a refused run.
    """


def load_instances_from_file(path: Path) -> list[dict[str, Any]]:
    """
    Reads and validates a JSON file of confirmed-negative instances.

    Every instance is validated BEFORE anything is written: a file whose
    47th record is malformed must not leave 46 patterns seeded and the
    rest missing.
    """
    try:
        raw = json.loads(path.read_text())
    except OSError as exc:
        raise SeedInputError(f"Cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SeedInputError(f"{path} is not valid JSON: {exc}") from exc

    if not isinstance(raw, list):
        raise SeedInputError(
            f"{path} must contain a JSON list of instances, got {type(raw).__name__}"
        )
    if not raw:
        raise SeedInputError(f"{path} contains no instances")

    for index, instance in enumerate(raw):
        if not isinstance(instance, dict):
            raise SeedInputError(f"{path}[{index}] is not a JSON object")
        try:
            pattern_identity_key(instance)
        except KeyError as exc:
            raise SeedInputError(f"{path}[{index}] is missing identity field {exc}") from exc

    return raw


def group_by_identity(
    instances: list[dict[str, Any]],
) -> dict[tuple[str, ...], list[dict[str, Any]]]:
    """One seed_template() call per distinct pattern, not one per file."""
    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for instance in instances:
        grouped[pattern_identity_key(instance)].append(instance)
    return dict(grouped)


def seed(
    instances: list[dict[str, Any]], firestore_client: Client, dry_run: bool = False
) -> dict[str, Any]:
    """
    Seeds every pattern found in `instances`. Returns a summary
    {"patterns": int, "instances": int, "tiers": {identity_key: tier}} so
    the operator can see which patterns actually reached confirmed tier
    rather than assuming they all did -- a batch too small or too uniform
    lands at provisional, which is the graduation gate working, not a
    failure, but it should never be a surprise.
    """
    grouped = group_by_identity(instances)
    tiers: dict[str, str] = {}

    for identity_key, group in grouped.items():
        if dry_run:
            # Computed without writing, so a dry run still reports the
            # tier each batch would actually land at.
            tiers[str(identity_key)] = build_structural_template(group, provenance="seeded")["tier"]
            continue

        template = seed_template(identity_key, group, firestore_client)
        tiers[str(identity_key)] = template["tier"]
        logger.bind(identity_key=identity_key, tier=template["tier"]).info(
            "Seeded {} instance(s)", len(group)
        )

    return {"patterns": len(grouped), "instances": len(instances), "tiers": tiers}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--case",
        choices=[member.value for member in DatasetCase],
        help="Seed one of the 6 synthetic dataset cases.",
    )
    source.add_argument(
        "--file", type=Path, help="Seed from a JSON file of real confirmed-negative instances."
    )
    parser.add_argument("--seed", type=int, default=0, help="RNG seed for --case (default 0).")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be seeded, and at what tier, without writing.",
    )
    args = parser.parse_args()

    try:
        if args.case:
            instances = generate_case(args.case, seed=args.seed)["instances"]
        else:
            instances = load_instances_from_file(args.file)
    except (SeedInputError, UnknownDatasetCaseError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        summary = seed(instances, firestore.Client(), dry_run=args.dry_run)
    except MalformedAlertError as exc:
        # seed_template -> build_structural_template validates DIFFABLE_FIELDS;
        # surfaced as a clear message rather than a traceback, per this
        # project's "never surface raw exceptions" standard.
        print(f"error: {exc}", file=sys.stderr)
        return 2

    prefix = "[dry-run] would seed" if args.dry_run else "Seeded"
    print(f"{prefix} {summary['instances']} instance(s) across {summary['patterns']} pattern(s):")
    for identity_key, tier in summary["tiers"].items():
        note = "" if tier == "confirmed" else "  <-- below the graduation gate"
        print(f"  {tier:<12} {identity_key}{note}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
