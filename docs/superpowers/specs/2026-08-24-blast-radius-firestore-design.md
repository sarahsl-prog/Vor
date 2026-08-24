# Blast-radius table: Firestore-backed + commit workflow — design

**Status:** approved for implementation (spec review pending)
**Date:** 2026-08-24
**Classification:** architectural

## Problem

`propose_blast_radius()` (`vor_agents/blast_radius.py`) has always
returned an inert `dict` — never written anywhere. Promoting a proposal
into `BLAST_RADIUS_TABLE` today means a human manually editing the Python
literal in `blast_radius.py` and shipping a code deploy. That's the
"intentionally manual forever" reading of TODO-Aug15's outstanding
decision #6 — now resolved the other way: build an actual review/commit
step so a human can promote a MEDIUM/LOW proposal without a code change.

## Goal

- `BLAST_RADIUS_TABLE` moves from an in-code Python dict to a
  Firestore-backed collection — adding or changing an entry no longer
  requires a deploy.
- `propose_blast_radius()`'s output becomes something a human can actually
  act on: a pending-proposal doc in Firestore, plus a commit endpoint
  (gated the same way `/audit` is) that writes an approved proposal into
  the live table.
- Every existing read site (`estimate_blast_radius()`,
  `_fetch_all_confirmed_patterns()`'s call into it) keeps working, reading
  from Firestore instead of the module-level dict — same asymmetric trust
  rule preserved: CRITICAL/HIGH proposals commit directly (safe
  direction), MEDIUM/LOW require the explicit human-reviewed commit step
  (risky direction, must stay gated).

## Non-goals

- Changing the scoring criteria themselves (`TIER_RANGES`, what counts as
  CRITICAL vs HIGH) — `BLAST_RADIUS_PLAYBOOK.md`'s tiers are unchanged,
  this is a storage/workflow change only.
- A UI for reviewing proposals. The commit step is an authenticated
  endpoint (or CLI script hitting it) — no web form.
- Removing `UNSCORED_DEFAULT`'s fallback behavior — an alert matching no
  table entry still defaults to HIGH, never LOW, regardless of where the
  table lives.

## Architecture

```
estimate_blast_radius(alert)
        |
   read BLAST_RADIUS_TABLE (Firestore collection, cached)
        |
   max(matching scores) or UNSCORED_DEFAULT

propose_blast_radius(...) --(writes)--> blast_radius_proposals (Firestore, status=pending)
                                                  |
                                    (human reviews via commit endpoint)
                                                  v
                                    POST /blast-radius/commit
                                                  |
                                   writes into blast_radius_table (Firestore)
                                   marks proposal status=committed
```

## Components

### New Firestore collections

- `blast_radius_table` — replaces the in-code dict. One doc per entry,
  keyed by a deterministic doc ID derived from
  `(indicator_type, value)` (same hashing approach as `_doc_id()` in
  `enrichment.py`, for consistency — avoids doc-ID collisions the same
  way that fix addressed for confidence docs). Fields: `indicator_type`,
  `value`, `score`, `tier`, `committed_at`, `committed_by` (if/when auth
  identity is available — see open items).
- `blast_radius_proposals` — one doc per `propose_blast_radius()` call
  that isn't auto-committed (see below). Fields: everything
  `propose_blast_radius()`'s return dict already has
  (`identity_key`, `proposed_tier`, `proposed_score`, `cited_indicators`,
  `rationale`, `status`), plus `proposed_at`. `status` transitions
  `pending_human_review` → `committed` or `rejected`.

### `vor_agents/blast_radius.py` changes

- `estimate_blast_radius(alert, firestore_client)` — signature gains a
  required `firestore_client` param (breaking change, all call sites
  updated: `orchestrator.py`'s `_fetch_all_confirmed_patterns()`, any
  test fakes). Reads from `blast_radius_table` instead of the module dict.
- **Caching, not a live read per call**: a per-process, TTL'd
  (e.g. 5 minutes) in-memory cache of the full table, refreshed lazily —
  `_fetch_all_confirmed_patterns()` calls `estimate_blast_radius()` once
  per confirmed instance per sweep, and a Firestore read per call would
  turn one sweep into O(instances) reads for a table that changes rarely.
  Same lazy-singleton shape as `get_firestore_client()`/`get_tasks_client()`
  in `main.py`, but with a TTL instead of living forever — a table update
  should become visible within one cache TTL window, not require a
  process restart.
- `propose_blast_radius()` — unchanged validation logic (unknown
  tier/out-of-range score still raises `ValueError`, same as today). New
  behavior on top: takes an added `firestore_client` param, writes the
  returned dict to `blast_radius_proposals` before returning it, so it's
  not just inert in the caller's hand anymore. **CRITICAL/HIGH proposals
  are auto-committed** — written directly to `blast_radius_table` in the
  same call, `status="committed"` on the proposal doc immediately — this
  is the conservative direction, matches the playbook's existing "may be
  added directly" language, and doesn't need the human gate. **MEDIUM/LOW
  proposals stay `pending_human_review`**, not written to
  `blast_radius_table` until the commit endpoint is called.

### New `POST /blast-radius/commit` endpoint (`main.py`)

- Body: `{proposal_id: str}` (a new `BlastRadiusCommitRequest` Pydantic
  model in `schemas.py`).
- Reads the proposal doc, verifies `status == "pending_human_review"`
  (returns 409 if already committed/rejected — no double-commit), writes
  its `(indicator_type per cited_indicators, score)` pairs into
  `blast_radius_table`, sets `status="committed"` on the proposal doc.
- Gated the same way `/audit` is — Cloud Run IAM, OIDC-authenticated
  caller, never `--allow-unauthenticated`. Unlike `/audit`, this is meant
  to be called by a human (via `gcloud run services proxy` + curl, or a
  small authenticated CLI script) rather than a machine dispatcher — no
  Cloud Tasks/Scheduler wiring needed, just the same IAM gate.
- A `cited_indicators` entry is a list of strings today
  (`propose_blast_radius()`'s existing param) — needs a decision at
  implementation time on how a string like `"parent_image=lsass.exe"`
  maps to a concrete `(indicator_type, value)` table key. Flagged as an
  implementation-time parsing detail, not re-litigated here since it
  doesn't change the architecture.

## Data flow

1. A human or an extended auditor LLM step calls `propose_blast_radius()`
   with a candidate tier/score/rationale.
2. CRITICAL/HIGH: committed immediately, visible in `blast_radius_table`
   (and to the next cache refresh) within one TTL window.
3. MEDIUM/LOW: sits in `blast_radius_proposals` as `pending_human_review`.
4. A human reviews it against `BLAST_RADIUS_PLAYBOOK.md`'s criteria
   (unchanged — this workflow doesn't relax that review, it just gives it
   a real commit mechanism instead of "edit code by hand").
5. Human calls `POST /blast-radius/commit` with the proposal ID → entry
   lands in `blast_radius_table`, proposal marked `committed`.
6. Every subsequent `estimate_blast_radius()` call sees the new entry
   once its process's cache refreshes.

## Error handling & retry safety

- `estimate_blast_radius()`'s Firestore read failing (transient
  unavailability): the cache serves its last-known-good value past its
  TTL rather than raising — a stale blast-radius table is a much safer
  failure mode than an unhandled exception propagating into
  `_fetch_all_confirmed_patterns()` and breaking the whole sweep. If the
  cache has never been populated yet (cold start, first call ever fails):
  fall back to `UNSCORED_DEFAULT` for every lookup rather than crashing —
  same "unassessed defaults to HIGH, never silently trusted" principle
  the whole module already runs on.
- Commit-endpoint double-submission: guarded by the `status` check above
  (409 on a non-pending proposal), not by Firestore-level transaction —
  acceptable since this is a low-frequency, human-triggered action, not a
  high-concurrency path like Cloud Tasks dedup.

## Logging

`loguru`. Log at `INFO` on every table read that falls back to a stale
cache or `UNSCORED_DEFAULT` due to a Firestore failure (distinguishable
from a genuine "nothing matched" `UNSCORED_DEFAULT` case, which isn't
worth logging on every call). Log at `WARNING` on every commit (who/what
committed which entry) — this is a trust-table change, worth a durable
log trail beyond just the Firestore write itself.

## Testing

- `tests/test_blast_radius.py`: extend `FakeFirestoreClient`-backed tests
  — `estimate_blast_radius()` reads from the fake Firestore collection
  instead of the module dict; cache-hit vs cache-miss behavior; stale-cache
  fallback on a simulated Firestore failure; cold-cache fallback to
  `UNSCORED_DEFAULT`.
- `propose_blast_radius()`: CRITICAL/HIGH auto-commits (assert
  `blast_radius_table` gets the entry, proposal doc status is
  `committed`); MEDIUM/LOW does NOT auto-commit (table unchanged,
  proposal stays `pending_human_review`); existing tier/score validation
  tests (unknown tier, out-of-range score) unchanged.
- `tests/test_main.py`: new tests for `POST /blast-radius/commit` —
  commits a pending MEDIUM proposal successfully; 409 on an
  already-committed proposal; 404 on an unknown proposal ID.

## Deploy (`DEPLOY.md` additions)

- No new GCP resources beyond two new Firestore collections (same
  project, same credentials already in use — Firestore is schemaless, no
  migration step to create a collection).
- `/blast-radius/commit` added to the "never `--allow-unauthenticated`"
  list alongside `/classify`, `/sweep`, `/audit`.
- A one-time seed step: `BLAST_RADIUS_TABLE`'s current 5 hardcoded entries
  (`lsass.exe`, `ToolPane_admin`, `w3wp.exe`, `svchost.exe`,
  `explorer.exe`) need writing into `blast_radius_table` before first
  deploy, or `estimate_blast_radius()` falls back to `UNSCORED_DEFAULT`
  for everything until someone re-proposes and commits each one by hand.
  A small one-time migration script, same shape as the identity-key
  backfill script flagged in TODO-Aug15 Task 3 / TODO-Aug24 Task 5.

## Open items carried forward, not resolved here

- `committed_by` — no caller-identity plumbing exists yet (the commit
  endpoint is IAM-gated but doesn't currently extract *which* authenticated
  identity called it). Left as an unpopulated field for now; revisit if
  audit-trail-of-who-committed-what becomes a real requirement.
- The one-time seed-the-5-existing-entries migration script (see Deploy
  section) — not written as part of this spec, tracked as its own task.
- `cited_indicators` string → `(indicator_type, value)` parsing at commit
  time — implementation-level detail, flagged above, not resolved here.
