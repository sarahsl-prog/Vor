"""
One-time migration: seed blast_radius_table with the 5 entries that used
to be hardcoded in vor_agents/blast_radius.py's BLAST_RADIUS_TABLE dict,
before that table moved to Firestore (see
docs/superpowers/specs/2026-08-24-blast-radius-firestore-design.md).

Run once, before first production deploy against a fresh Firestore
project -- see docs/DEPLOY.md. Idempotent: re-running just overwrites the
same 5 entries with the same values (merge=True in _commit_indicators),
so it's safe to run more than once.

Usage:
    .venv/bin/python scripts/seed_blast_radius_table.py
"""

import sys
from pathlib import Path

# Running this file directly puts scripts/ on sys.path, not the repo root,
# so `import vor_agents` would fail for the documented invocation
# (`python scripts/<name>.py`). Tests import it fine via pytest's
# pythonpath=".", which is exactly why this was easy to miss.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google.cloud import firestore

from vor_agents.blast_radius import _commit_indicators

# (indicator, score) -- matches the original BLAST_RADIUS_TABLE dict
# exactly, tier constants inlined as their point values since the
# original dict stored scores, not tier labels.
SEED_ENTRIES: list[tuple[str, float]] = [
    ("parent_image=lsass.exe", 0.95),  # CRITICAL
    ("endpoint_family=ToolPane_admin", 0.95),  # CRITICAL (CVE-2026-56164 model)
    ("parent_image=w3wp.exe", 0.75),  # HIGH
    ("parent_image=svchost.exe", 0.45),  # MEDIUM
    ("parent_image=explorer.exe", 0.15),  # LOW
]


def main() -> None:
    client = firestore.Client()
    for indicator, score in SEED_ENTRIES:
        _commit_indicators([indicator], score, client)
    print(f"Seeded {len(SEED_ENTRIES)} blast-radius table entries into Firestore.")


if __name__ == "__main__":
    main()
