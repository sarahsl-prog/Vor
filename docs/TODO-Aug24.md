# Vör — Gap-analysis To-Do List (docs vs. implementation, 2026-08-24)

**Status:** Compiled by auditing `docs/` planning docs against current `vor_agents/`, `main.py`,
`tests/`, CI, and `pyproject.toml`. Cloud Tasks audit-queue plan (spec + implementation plan) is
**fully implemented and verified** — not repeated here. `docs/TODO-Aug15.md`'s 22 tasks are all
✅ DONE, also verified. Everything below is a real, currently-open gap.

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
- [ ] README "Not yet built." `conftest.py` only has fixtures for case #3 (drift) and case #6
  (field-level deviation) — the two the reconciliation tests needed. No full generator, no doc
  enumerating all 6 cases.

### Task 3 — Seeding script for `enrichment.seed_template()`
- [ ] Function exists and is exercised in tests, but there's no CLI/script entrypoint to seed a
  real Firestore instance with historical confirmed patterns.

### Task 4 — MLflow/OTel tracing
- [ ] CLAUDE.md requires "the option to log activities to mlflow or another otel compatible
  app." Zero implementation — only mentioned in CLAUDE.md itself and the Cloud Tasks design
  doc's explicit non-goals (deferred there on purpose). Not started.
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
- [ ] Flagged explicitly in `docs/TODO-Aug15.md` Task 3: "write a one-time backfill script
  before first production deploy." No script exists. Low urgency — no live Firestore data yet —
  but must land before first real deploy against pre-existing data.

### Task 6 — Consecutive-audit-failure escalation
- [ ] Outstanding decision #4 from Code-review-Aug15/TODO-Aug15, still open. A repeatedly-failing
  audit clears `under_review` and logs `NO_ACTION` silently every time — no `failure_count` or
  escalation-to-human path anywhere in `vor_agents/`.
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
- [ ] Outstanding decision #6, still open. `propose_blast_radius()` returns inert pending records
  forever; nothing commits a MEDIUM/LOW proposal into `BLAST_RADIUS_TABLE`. Confirm this is
  intentionally manual-forever, or design the missing commit step.
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

### Task 9 — `@pytest.mark.integration` suite against real Gemini
- [ ] TESTING_PLAN.md commits to this; doesn't exist. Zero coverage today that a real model call
  round-trips into a valid `ClassifierOutput`/`AuditorOutput` schema.

---

## Missing docs

### Task 10 — Add `.env.example`
- [ ] CLAUDE.md: "All secrets go in `.env` (never committed)" — no template listing the vars a
  fresh clone needs: `GCP_PROJECT`, `TASKS_LOCATION`, `TASKS_QUEUE`, `TASKS_OIDC_SA_EMAIL`,
  `SERVICE_URL`, `GOOGLE_GENAI_USE_VERTEXAI`, `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`.
  Currently scattered across DEPLOY.md's gcloud commands only.

### Task 11 — Trigger-source design doc (pairs with Task 1)
- [ ] No spec for how `/classify` actually gets called — DEPLOY.md only gestures at "front it
  with a Pub/Sub push subscription." Cloud Tasks work got a full design doc + plan; this gap
  hasn't.

### Task 12 — Dataset/seeding runbook (pairs with Tasks 2–3)
- [ ] No doc enumerates what all 6 synthetic cases actually are — only cases #3 and #6 are named
  anywhere (TESTING_PLAN.md).

### Task 13 — MLflow/OTel integration doc (pairs with Task 4)
- [ ] Nothing to write until the feature exists, but flagging since CLAUDE.md treats it as a
  standing requirement, not optional — should land alongside Task 4, not as an afterthought.

### Task 14 — Fix stale "Known gaps" section header in README
- [ ] Cosmetic: section is titled "not yet resolved" but its one item (identity-key round-trip)
  is marked fixed inline. Misleading on skim.

---

## Testing gaps

### Task 15 — Agent-construction smoke test
- [ ] TESTING_PLAN.md names this as deliberately not-yet-covered ("worth a smoke test that they
  construct without error"). Confirmed absent from `tests/`.

### Task 16 — `/classify` non-enqueue on ESCALATE/UNCERTAIN
- [ ] Code-review-Aug15's Test Gaps table lists `test_classify_no_enqueue_on_non_suppress` — not
  present in current `test_main.py` (only SUPPRESS-path enqueue tests exist).

### Task 17 — Enqueued task body shape
- [ ] Same table: `test_enqueued_task_body_shape` (assert actual `Task` payload — URL, OIDC
  audience, identity_key list). `test_task_queue.py` covers dedup/naming/error-wrapping but not
  payload contents.

### Task 18 — Integration suite (repeats Task 9, testing-specific framing)
- [ ] Same gap as Task 9 — listed here too since it's also a named row in TESTING_PLAN.md's
  coverage map, not just a "features" gap.

### Task 19 — Consecutive-failure-escalation tests
- [ ] Blocked on Task 6 existing first. Flagging now since it's a named safety gap with zero
  coverage of the current "clears and logs, forever, silently" behavior either.

---

## Not a gap — verified in place

- Cloud Tasks audit-queue: `main.py`, `task_queue.py`, `orchestrator.py` all match the design
  doc and implementation plan exactly. `/classify`, `/sweep`, `/audit` all present and tested.
- `docs/TODO-Aug15.md`'s 22 tasks: all ✅ DONE, spot-checked against source (mypy --strict clean,
  pre-commit config present, requirements pinned, etc).
- `pytest.ini` and `pyproject.toml`'s `[tool.pytest.ini_options]` are duplicate config (not a
  functional gap — pytest.ini wins — but worth deduplicating next time either file is touched).
