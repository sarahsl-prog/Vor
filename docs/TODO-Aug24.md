# Vör — Gap-analysis To-Do List (docs vs. implementation, 2026-08-24)

**Status (updated 2026-08-25):** Compiled by auditing `docs/` planning docs against current
`vor_agents/`, `main.py`, `tests/`, CI, and `pyproject.toml`. Cloud Tasks audit-queue plan (spec +
implementation plan) is **fully implemented and verified** — not repeated here.
`docs/TODO-Aug15.md`'s 22 tasks are all ✅ DONE, also verified.

**18 of 19 tasks are now closed.** Only Task 8 (re-run the Vertex AI billing smoke test) remains
fully open, and it cannot be closed from a dev environment — it needs a real GCP project with
billing enabled. Two closed tasks carry a narrower follow-up checkbox (Task 2's conftest fixture
collapse, Task 9's not-yet-executed real-API run).

Order below = rough priority (production-blocking → nice-to-have), not strict CLAUDE.md fix
order since these are mostly unrelated features rather than bugs in one flow — pick order per
outstanding-decisions discussion.

---

## Features not implemented

### Task 1 — Wire a real trigger source for `/classify`
- [x] Nothing currently calls `POST /classify`. DEPLOY.md step 4 and README both flag this as
  open; confirmed — no webhook/Pub/Sub/Hayabusa-ingest code anywhere in the repo. **Resolved** —
  see `docs/superpowers/plans/2026-08-24-pubsub-classify-trigger.md`.
- [x] **Decision (2026-08-24): Pub/Sub push subscription.** Ingest source publishes to a topic;
  a push subscription calls `/classify` with OIDC auth, same shape as `/sweep`/`/audit`'s
  existing Cloud Run IAM gating.
- [x] **Sub-decision: unwrap inline in `/classify`.** Detect the Pub/Sub envelope shape
  (`{message: {data: base64}, subscription}`), base64-decode `message.data` into the alert JSON,
  then validate against the existing `ClassifierRequest`. Direct/test callers can still POST raw
  alert JSON — one endpoint handles both shapes, no separate `/pubsub/classify` route to secure.
  Ready for a design doc (Task 11) + implementation.

### Task 2 — Synthetic dataset generation (6 cases)
- [x] **Resolved** — `vor_agents/datasets.py` generates all 6 cases deterministically from an
  explicit seed. Cases #1/#3/#6 keep the numbering already used in the codebase; #2/#4/#5 were
  never enumerated anywhere and were chosen to complete the decision surface (see
  `docs/DATASET_RUNBOOK.md`, which records that choice explicitly so it can be corrected).
  `tests/test_datasets.py` asserts each case against the real graduation/diversity/diffing code
  rather than hardcoded expectations.
- [ ] **Still open:** `conftest.py`'s hand-written fixtures for #3 and #6 predate the generator
  and are maintained separately. They agree today (verified); worth collapsing onto the
  generator next time either is touched.

### Task 3 — Seeding script for `enrichment.seed_template()`
- [x] **Resolved** — `scripts/seed_firestore.py` seeds either a synthetic case (`--case`) or
  real history (`--file`), with `--dry-run` reporting the tier each batch would land at before
  anything is written. Input is fully validated up front so a malformed record can't leave a
  half-seeded collection. Documented in `docs/DATASET_RUNBOOK.md` and DEPLOY.md step 3e.

### Task 4 — MLflow/OTel tracing
- [x] CLAUDE.md requires "the option to log activities to mlflow or another otel compatible
  app." **Resolved** — see `docs/superpowers/plans/2026-08-24-mlflow-tracing.md`.
  `vor_agents/tracing.py` logs both agent calls to MLflow (best-effort, with a Firestore
  `pending_traces` fallback queue and a `/replay-traces` scheduled drain job).
- [x] **Decision (2026-08-24): full MLflow tracking integration**, not minimal OTel spans.
  Scope: an MLflow experiment logging each `classify_alert()`/`audit_pattern()` call — prompt,
  model output, resolved decision, and any deterministic-override that fired (the asymmetric
  reconciliation, `under_review`/provisional overrides).
- [x] **Sub-decision: managed MLflow tracking server** (Databricks-hosted or self-run with a GCS
  artifact store + DB backend store) — Cloud Run's ephemeral per-instance filesystem rules out a
  local file store, it won't persist across cold starts/scaling.
- [x] **Sub-decision: best-effort logging with a durable fallback queue, not a hard
  dependency.** MLflow logging must never fail the classify/audit request itself (same posture
  as `_enqueue()`'s Cloud Tasks handling). On an MLflow-logging failure, write the trace record
  to a small Firestore `pending_traces` collection instead of dropping it or buffering to local
  disk (local disk isn't durable on Cloud Run — an instance recycle mid-outage would silently
  lose buffered traces). A background job/cron replays `pending_traces` into MLflow once it's
  reachable again, then deletes the replayed docs. Ready for a design doc — this needs its own
  spec (the replay job is a second moving piece, not just the inline logging call).

### Task 5 — One-time Firestore backfill script (identity_key migration)
- [x] **Resolved** — `scripts/backfill_identity_key.py`. Recovers each legacy doc's identity_key
  from its own `confirmed_instances` (never by splitting the ambiguous legacy doc ID), rewrites
  it under the hashed doc ID, and deletes the old one. Idempotent, `--dry-run` supported, exits
  non-zero if any doc was unrecoverable. Documented in DEPLOY.md step 3d.

### Task 6 — Consecutive-audit-failure escalation
- [x] Outstanding decision #4 from Code-review-Aug15/TODO-Aug15. **Resolved** — see
  `docs/superpowers/plans/2026-08-24-audit-failure-escalation.md`. `failure_count` now tracked
  on the confidence doc (`vor_agents/review_flag.py`'s `clear_under_review()`), surfaced through
  `enrich()`, and enforced in `classify_alert()` (`vor_agents/orchestrator.py`) — a repeatedly-
  failing audit no longer silently keeps a pattern autonomously suppressing.
- [x] **Decision (2026-08-24): N consecutive failures escalates to a human.** `failure_count`
  field on the confidence doc, stamped in `audit_pattern()`'s except branch, reset on a
  genuinely successful audit.
- [x] **Sub-decision: threshold = 3**, matching `GRADUATION_THRESHOLD`'s count — consistent with
  the project's other unvalidated-starting-point constants, flagged as unvalidated same as those.
- [x] **Sub-decision: mechanism = both.** Once `failure_count >= 3`: (1) force the pattern's
  decision to ESCALATE in-band on the next `classify_alert()` call, same override shape as the
  existing `under_review`/provisional-tier overrides — deterministic, in code, not a prompt ask;
  (2) write a visible record (a `needs_attention` Firestore doc or CRITICAL-level log a human
  dashboard/query can surface) so the stuck pattern is diagnosable, not just silently blocked.
  Ready for implementation.

### Task 7 — Blast-radius table promotion workflow
- [x] Outstanding decision #6. **Resolved** — see
  `docs/superpowers/plans/2026-08-24-blast-radius-firestore.md`. `BLAST_RADIUS_TABLE` is now
  Firestore-backed (`vor_agents/blast_radius.py`), CRITICAL/HIGH proposals auto-commit,
  MEDIUM/LOW sit pending until a human calls `POST /blast-radius/commit`.
- [x] **Decision (2026-08-24): build a review/commit step.**
- [x] **Sub-decision: move `BLAST_RADIUS_TABLE` to a Firestore-backed table.** A commit
  endpoint/step writes directly to Firestore — no code deploy needed to add an entry. Real
  design/implementation cost: every current read site (`select_audit_targets()`,
  `propose_blast_radius()`'s lookup, etc.) needs to read from Firestore instead of the in-memory
  Python dict — likely wants a small cached-read wrapper so this doesn't become a Firestore read
  per pattern per sweep. `BLAST_RADIUS_PLAYBOOK.md` needs an update: the "curated table" language
  currently implies code-review-gated; a Firestore-backed table needs its own access-control
  story (who can call the commit endpoint) to preserve that same trust bar. Needs a design doc
  before implementation — this is the biggest-scope of the four.

### Task 8 — Re-run Vertex AI billing smoke test
- [ ] Outstanding decision #3. Smoke test previously blocked on `BILLING_DISABLED` for the
  project, never re-run since. Not a code gap — an unverified claim currently sitting in
  README/DEPLOY.md as "resolved."
- **Cannot be closed from a dev environment**: needs a real GCP project with billing enabled and
  Vertex AI credentials. `tests/test_integration_gemini.py` (Task 9) is the mechanism to verify
  it — run `pytest -m integration` against the billed project and this closes with Task 9's
  second checkbox.

### Task 9 — `@pytest.mark.integration` suite against real Gemini
- [x] **Resolved** — `tests/test_integration_gemini.py`, marked `integration` and deselected by
  default via `pytest.ini`'s `addopts = -m "not integration"`, so it never gates CI. Asserts
  shape (real call round-trips into a valid `ClassifierOutput`/`AuditorOutput`), not model
  content — making a non-deterministic model a pass/fail gate is exactly what TESTING_PLAN.md's
  philosophy rejects. Skips cleanly without credentials rather than failing red.
- [ ] **Not yet actually executed against the real API** — no Vertex AI credentials/billing
  available in this environment. Pairs with Task 8.

---

## Missing docs

### Task 10 — Add `.env.example`
- [x] **Resolved** — `.env.example` lists every variable with placeholder values and notes what
  breaks when each is missing. Required a `!.env.example` negation in `.gitignore`, whose
  `.env.*` rule was silently ignoring it.

### Task 11 — Trigger-source design doc (pairs with Task 1)
- [x] **Resolved** — see `docs/superpowers/specs/2026-08-24-pubsub-classify-trigger-design.md`
  (spec) and `docs/superpowers/plans/2026-08-24-pubsub-classify-trigger.md` (implementation
  plan). DEPLOY.md step 4 now documents the `vor-alerts` topic, the `vor-alerts-sub` push
  subscription (OIDC-authenticated, `--ack-deadline 600`), and the publisher IAM binding the
  ingest source needs.

### Task 12 — Dataset/seeding runbook (pairs with Tasks 2–3)
- [x] **Resolved** — `docs/DATASET_RUNBOOK.md` enumerates all 6 cases, explains why they are
  those six (they span the decision surface, read as three pairs), and documents both seeding
  paths. Flags explicitly which case numbers were pre-existing and which were newly chosen.

### Task 13 — MLflow/OTel integration doc (pairs with Task 4)
- [x] **Resolved** — landed alongside Task 4, not as an afterthought. See
  `docs/superpowers/specs/2026-08-24-mlflow-tracing-design.md` and DEPLOY.md step 5
  (`MLFLOW_TRACKING_URI`, the `vor-trace-replay` Scheduler job draining `pending_traces`, and
  the still-open unbounded-growth caveat during an extended MLflow outage).

### Task 14 — Fix stale "Known gaps" section header in README
- [x] **Resolved** — retitled "Known gaps", with the resolved half and the still-open half
  (pre-existing data needing the backfill) called out separately instead of buried in a
  parenthetical.

---

## Testing gaps

### Task 15 — Agent-construction smoke test
- [x] **Resolved** — `tests/test_agents.py` covers construction, output schema/key, the model
  override seam, that neither agent carries tools (the design boundary auditability rests on),
  and that neither system prompt is empty.

### Task 16 — `/classify` non-enqueue on ESCALATE/UNCERTAIN
- [x] **Resolved** — `test_classify_no_enqueue_on_non_suppress` in `tests/test_main.py`, covering
  both UNCERTAIN and ESCALATE.

### Task 17 — Enqueued task body shape
- [x] **Resolved** — `test_enqueued_task_body_shape` asserts URL, HTTP method, headers, OIDC
  service account and audience, and the JSON body; a companion test round-trips that body back
  through `AuditRequest` so drift between `task_queue.py` and the model fails here rather than
  after deploy.

### Task 18 — Integration suite (repeats Task 9, testing-specific framing)
- [x] **Resolved** with Task 9. TESTING_PLAN.md's coverage map now has a row for it, plus rows
  for `datasets.py`, both agents, and both scripts.

### Task 19 — Consecutive-failure-escalation tests
- [x] **Resolved** — unblocked by Task 6 landing. `tests/test_review_flag.py`
  (`TestFailureCountTracking`, `TestRecordNeedsAttention`) covers increment/reset/accumulate and
  the `needs_attention` write; `tests/test_orchestrator.py` (`TestFailureEscalation`,
  `TestFailureCountBlocksSuppress`) covers escalation at the threshold, no escalation below it,
  reset-on-success, and the deterministic SUPPRESS → UNCERTAIN override.

---

## Not a gap — verified in place

- Cloud Tasks audit-queue: `main.py`, `task_queue.py`, `orchestrator.py` all match the design
  doc and implementation plan exactly. `/classify`, `/sweep`, `/audit` all present and tested.
- `docs/TODO-Aug15.md`'s 22 tasks: all ✅ DONE, spot-checked against source (mypy --strict clean,
  pre-commit config present, requirements pinned, etc).
- `pytest.ini` and `pyproject.toml`'s `[tool.pytest.ini_options]` were duplicate config — now
  deduplicated. `pytest.ini` is the single source (it won anyway); the dead `pyproject.toml`
  block was removed while adding the `integration` marker, per this note's own instruction to
  do it next time either file was touched.
