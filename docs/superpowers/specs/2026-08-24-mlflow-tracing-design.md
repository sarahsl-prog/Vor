# MLflow tracing integration — design

**Status:** approved for implementation (spec review pending)
**Date:** 2026-08-24
**Classification:** architectural / observability

## Problem

CLAUDE.md requires "the option to log activities to mlflow or another
otel compatible app" on all major features. Nothing in this codebase does
this today — the Cloud Tasks design doc explicitly deferred it as a
non-goal, and it's stayed unbuilt since. Every classification and audit
decision (including every deterministic override that fires — the
asymmetric reconciliation, `under_review`/provisional/failure-escalation
overrides in `classify_alert()`) currently leaves no trace beyond
whatever `loguru` happened to log at the time, and log lines aren't
queryable/comparable the way an MLflow experiment is.

## Goal

Full MLflow tracking integration: every `classify_alert()` and
`audit_pattern()` call logs a run — prompt, raw model output, resolved
decision, and which (if any) deterministic override fired — to a managed
MLflow tracking server. Logging must never fail the request itself
(best-effort), and a tracking-server outage must not silently drop trace
data — failed logs queue durably in Firestore and get replayed once the
server is reachable again.

## Non-goals

- Minimal OTel spans — considered and explicitly not chosen; full MLflow
  experiment tracking (not just latency/error spans) is the scope here.
- Standing up the managed MLflow tracking server itself as part of this
  repo's infra (Databricks-hosted vs. self-run on GKE/Cloud Run) — that's
  an infra decision made once, documented in DEPLOY.md, not code this
  repo owns or provisions.
- Retrofitting tracing onto every other module — same "only at the points
  this change actually introduces" restraint the Cloud Tasks spec applied
  to its own `loguru` adoption. Scope stays at `classify_alert()` and
  `audit_pattern()`, the two LLM-calling entry points.

## Architecture

```
classify_alert() / audit_pattern()
        |
   _run_agent() call (unchanged)
        |
   log_trace(run_data) -- best effort
        |                        \
   MLflow reachable          MLflow unreachable
        |                        |
   mlflow.log_* (direct)   write to pending_traces (Firestore)
                                  |
                          (separate replay job, on a timer)
                                  |
                          reads pending_traces -> mlflow.log_*
                          -> delete replayed docs
```

## Components

### New `vor_agents/tracing.py`

Small, single-purpose module, matching the existing convention
(`task_queue.py`, `blast_radius.py`, etc.):

- `log_classification_trace(alert, enrichment, classifier_output,
  overrides_fired: list[str], firestore_client) -> None` — builds an
  MLflow run: params (`identity_key`, `enrichment` summary), the raw
  model prompt/output as artifacts, the final `ClassifierOutput` as a
  logged dict, and `overrides_fired` (a list of which of
  `classify_alert()`'s deterministic overrides actually changed the
  decision — e.g. `["under_review"]`, `["provisional_tier",
  "ground_truth_missed"]`, or `[]` if the model's own decision stood
  untouched) as a tag, so overridden vs. model-trusted decisions are
  queryable/filterable in the MLflow UI without parsing `reasoning` text.
- `log_audit_trace(identity_key, pattern_data, auditor_output,
  audit_failed: bool, firestore_client) -> None` — same shape for the
  audit path: prompt, raw output, final `AuditorOutput`, and whether this
  was a successful or failed (degraded `NO_ACTION`) run.
- Both wrap the actual `mlflow.start_run()`/`mlflow.log_*` calls in
  `try/except`. On any exception (connection refused, auth failure,
  timeout): write the same run data to a new `pending_traces` Firestore
  collection instead, log at `WARNING` (not `ERROR` — this is a handled,
  expected-during-an-outage fallback, same "dedup hit, not a failure"
  logging posture `task_queue.py` uses for `AlreadyExists`), and return
  normally. **Never raises** — matches `_enqueue()`'s "never fail the
  caller's own response" standard exactly.
- `TracingError` — not actually raised to callers (both functions above
  swallow everything internally), but defined for the internal
  MLflow-call wrapper to distinguish "MLflow call failed, fall back to
  Firestore" from a bug in the fallback-write path itself, which *should*
  surface (if even the Firestore fallback fails, that's worth a louder
  log than the everyday best-effort case).

### `vor_agents/orchestrator.py` changes

- `classify_alert()`: track `overrides_fired` as a local list, appending
  a string at each of the existing override sites (`under_review`,
  `provisional_tier`, the new `audit_failing` override from the
  failure-escalation spec, `ground_truth_missed`,
  `self_consistency_deviation`) as they fire — the code paths already
  exist and already know which branch they're in, this just also appends
  to the list rather than only mutating `classifier_output`. Call
  `log_classification_trace(...)` once, right before `return
  classifier_output, identity_key`, so it captures the fully-reconciled
  final decision, not an intermediate one.
- `audit_pattern()`: call `log_audit_trace(...)` in the `finally` block,
  after `clear_under_review()` — same "always runs, success or failure"
  guarantee, with `audit_failed` set from whether the `except` branch ran.

### Replay job

- A `replay_pending_traces(firestore_client) -> int` function (also in
  `tracing.py`), invoked on a timer — a new Cloud Scheduler job hitting a
  new `POST /replay-traces` endpoint, same shape as the existing
  `/sweep` scheduled path (OIDC, IAM-gated, never
  `--allow-unauthenticated`).
- Reads all `pending_traces` docs, attempts `mlflow.log_*` for each; on
  success, deletes the doc; on failure (server still unreachable),
  leaves it for the next scheduled run. Returns the count successfully
  replayed, same `{"replayed": N}` response shape convention as
  `/sweep`'s `{"enqueued": N}`.
- Cadence: every 15 minutes is a reasonable starting point (frequent
  enough that an outage doesn't leave a large backlog, infrequent enough
  not to hammer a recovering server) — explicitly flagged as unvalidated,
  same posture as every other unvalidated constant in this codebase.

## Data flow

1. `classify_alert()`/`audit_pattern()` runs as today.
2. On completion, `log_*_trace()` is called. MLflow reachable → run
   logged directly, done.
3. MLflow unreachable → run data written to `pending_traces` instead,
   `WARNING` logged, caller's response is completely unaffected either
   way.
4. Every 15 minutes, `POST /replay-traces` fires: reads `pending_traces`,
   re-attempts logging each, deletes what succeeds.
5. Once the MLflow server is back, the next scheduled replay drains the
   backlog — no trace is permanently lost unless `pending_traces` itself
   is unavailable at write time too (see error handling below).

## Error handling & retry safety

- **MLflow unreachable at trace-write time**: falls back to
  `pending_traces`, never blocks the request — this is the core
  guarantee this spec exists to provide.
- **Firestore also unavailable** (the fallback write itself fails): this
  is the one case where a trace is genuinely lost. Logged at `ERROR`
  (louder than the normal `WARNING` fallback case) with the full run data
  serialized into the log line itself, so it's at least recoverable from
  log storage by hand in the worst case — not silently dropped with zero
  trace anywhere.
- **Replay job partial failure**: if `mlflow.log_*` fails for some docs
  and succeeds for others in one replay run, each doc is handled
  independently (own try/except per doc) — one bad/malformed
  `pending_traces` doc doesn't block the rest of the batch from replaying.
- **At-least-once logging**: if a replay's `mlflow.log_*` call succeeds
  but the subsequent Firestore delete fails, the same trace could be
  logged twice on the next replay attempt. Accepted — a duplicate MLflow
  run is a minor annoyance (filterable/dedupable in the MLflow UI by the
  `identity_key` tag + timestamp), not a correctness bug on the scale of
  a lost trace or a blocked request.

## Logging

`loguru`. `WARNING` on every MLflow-unreachable fallback (expected during
an outage). `ERROR` on the Firestore-fallback-also-failed case (genuinely
lost trace, needs a human to notice). `INFO` on each replay job run
(`{"replayed": N, "still_pending": M}`), so replay activity is visible in
normal logs, not just inferred from the endpoint's response.

## Testing

- `tests/test_tracing.py` (new): `log_classification_trace()`/
  `log_audit_trace()` call MLflow successfully (mock `mlflow` module) —
  no Firestore write happens; MLflow raising → falls back to a
  `pending_traces` write, function still returns normally (never raises
  to the caller); MLflow **and** Firestore both failing → logged at
  `ERROR`, still returns normally (never raises).
- `replay_pending_traces()`: replays and deletes docs that succeed;
  leaves docs whose MLflow call fails; returns the correct count; one bad
  doc doesn't block others in the same batch (partial-failure test).
- `tests/test_orchestrator.py`: `classify_alert()` calls
  `log_classification_trace()` exactly once, with `overrides_fired`
  correctly populated for each override scenario already covered by
  existing tests (`under_review`, provisional, ground-truth-missed,
  self-consistency, and the no-override case) — extend existing test
  classes rather than adding a parallel set, since the override logic
  itself isn't changing.
- `tests/test_main.py`: `POST /replay-traces` returns the replayed count;
  gated the same IAM way as `/sweep` (no manual auth-check test needed,
  same reasoning as the existing `/sweep`/`/audit` tests).

## Deploy (`DEPLOY.md` additions)

- `MLFLOW_TRACKING_URI` env var pointing at the managed server, set on
  the Cloud Run service alongside the existing Cloud Tasks/Vertex AI env
  vars.
- If the managed server requires its own auth (API key, service-account
  token) — that credential goes in `.env`/Secret Manager, never
  hardcoded, per CLAUDE.md's existing secrets rule.
- New Cloud Scheduler job: `vor-trace-replay`, `POST /replay-traces`,
  every 15 minutes, OIDC via the existing `vor-scheduler` service account
  — same reuse pattern as every other scheduled trigger in this codebase.
- `/replay-traces` added to the "never `--allow-unauthenticated`" list.
- New dependency: `mlflow` (client library) in `requirements.txt`, pinned
  per the project's existing exact-version-pin convention.

## Open items carried forward, not resolved here

- Which managed MLflow offering (Databricks-hosted vs. self-run) —
  explicitly deferred to a deploy-time/infra decision, not a code
  decision; this spec codes against the standard `mlflow` client API
  either way.
- 15-minute replay cadence is a guess, unvalidated against real outage
  frequency/duration data.
- No cap on `pending_traces` growth during an extended outage — if MLflow
  is down for days, this collection grows unbounded. Not addressed here;
  worth a TTL/max-size policy if real outages turn out to be long.
