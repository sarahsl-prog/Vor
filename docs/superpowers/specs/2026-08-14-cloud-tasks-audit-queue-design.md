# Cloud Tasks audit queue — design

**Status:** approved for implementation (spec review pending)
**Date:** 2026-08-14
**Classification:** architectural

## Problem

`/classify`'s SUPPRESS path fires the follow-up audit as a FastAPI
`BackgroundTasks` job that runs *after* the HTTP response is sent. Cloud
Run's default billing/CPU model only guarantees CPU allocation during
active request handling — outside that window (unless the service has
`--no-cpu-throttling` set) the instance can be throttled or frozen
mid-task. The audit's async LLM call and Firestore writes can silently
fail to complete, with no error, no retry, and no visibility.

Separately, `/sweep` runs its up to `max_targets` (default 10) audits
sequentially, awaited, inside a single HTTP request — a real timeout
risk as pattern volume grows, since each audit is itself an LLM call.

Finally, the duplicate-audit guard in `main.py` (`/classify` reads
`under_review` before scheduling a background task) is read-then-act,
not atomic. A true concurrent burst of `/classify` calls for the same
pattern can still schedule duplicate audits.

## Goal

Move all audit execution — regardless of trigger — onto Cloud Tasks, so:
- audits always run inside their own fully-CPU-allocated HTTP request
  (durability fix for the throttling problem)
- Cloud Tasks retries transient failures automatically (reliability the
  current fire-and-forget background task has none of)
- deterministic, named tasks give real dedup (Cloud Tasks rejects a
  second task with the same name inside its dedup window), replacing the
  best-effort `under_review` read-then-act check
- `/sweep` stops awaiting audits in-request, removing its sequential
  timeout risk

## Non-goals

- Changing `audit_pattern()`, `classify_alert()`, or any deterministic
  scoring/identity logic — this is purely a trigger/execution-path
  change.
- Full OTel/MLflow tracing (CLAUDE.md mentions this as an option) — out
  of scope for this pass; flagged as a separate follow-up.
- Fixing the identity-key underscore round-trip (known gap #1, tracked
  separately with an `xfail` test) — deterministic task names are
  derived from the same `_doc_id()` and inherit that gap unchanged, not
  worsened.

## Architecture

```
POST /classify --(SUPPRESS)--> enqueue_audit() --> Cloud Tasks queue
POST /sweep     --(N targets)--> enqueue_audit() x N --> Cloud Tasks queue
                                                             |
                                                     (OIDC, at-least-once)
                                                             v
                                                     POST /audit
                                                             |
                                                     audit_pattern()
```

One execution path (`POST /audit` → `audit_pattern()`) for every audit,
regardless of which trigger fired it.

## Components

### Configuration

Queue path (`projects/{project}/locations/{location}/queues/{queue}`) and
the OIDC service-account email for the `/audit` callback are read from
environment variables (`GCP_PROJECT`, `TASKS_LOCATION`, `TASKS_QUEUE`,
`TASKS_OIDC_SA_EMAIL`), consistent with `.env`-based secrets/config per
CLAUDE.md — not hardcoded, not inferred from the Firestore client.
`get_tasks_client()` in `main.py` builds the queue path once from these
at cold start, same lazy-singleton shape as `get_firestore_client()`.

### `vor_agents/task_queue.py` (new)

Small, single-purpose module — matches the existing convention
(`audit_targets.py`, `blast_radius.py`, etc.): deterministic logic lives
in its own file, `main.py` stays a thin FastAPI shell.

- `_task_name(queue_path: str, identity_key: tuple) -> str` — pure,
  deterministic. `f"{queue_path}/tasks/audit-{sha1('_'.join(identity_key)).hexdigest()}"`.
  Same identity_key always produces the same task name within a queue.
- `AuditEnqueueError(Exception)` — wraps any Cloud Tasks client failure
  that isn't the expected duplicate-name case. Never let a raw GCP SDK
  exception escape this module, per the project's existing error-handling
  standard (see `identity.py`'s `MalformedAlertError` for the same
  pattern applied to a different boundary).
- `enqueue_audit(identity_key: tuple, pattern_data: dict, tasks_client) -> bool` —
  builds the task name and HTTP-target `Task` payload (OIDC token,
  `POST /audit` on this service, JSON body `{identity_key, pattern_data}`),
  calls `tasks_client.create_task(...)`.
  - Returns `True` on a genuinely new task.
  - Catches `AlreadyExists`, logs at `WARNING` (dedup hit — expected,
    not an error), returns `False`.
  - Any other exception: logs at `ERROR`, re-raised wrapped as
    `AuditEnqueueError`.

### `orchestrator.py` changes

- `run_scheduled_sweep(firestore_client, tasks_client, max_targets=10)` —
  now takes `tasks_client` too. Selects targets via
  `select_audit_targets()` (unchanged), calls `enqueue_audit()` per
  target instead of `await audit_pattern(...)`. Returns the list of
  identity_keys it enqueued (callers previously got back
  `(identity_key, decision)` pairs — this is a real return-shape change,
  since decisions no longer exist synchronously at this point).
- `audit_pattern()` — unchanged. It's now called from the new `/audit`
  endpoint instead of from `run_scheduled_sweep()`'s loop or a
  `BackgroundTasks` job, but its own logic (mark → LLM call → clear) is
  identical either way.

### `main.py` changes

- New `get_tasks_client()` — lazy singleton, same shape as
  `get_firestore_client()`.
- `/classify`: on SUPPRESS, calls `enqueue_audit(identity_key, {"triggered_by": "classify_suppress"}, tasks_client)`
  inside a `try/except AuditEnqueueError` — logs the error, still returns
  `result.model_dump()`. A failed audit *trigger* must never fail the
  *classification response*. Drops the old manual `under_review` pre-read
  entirely — Cloud Tasks' own dedup replaces it.
- `/sweep`: calls `run_scheduled_sweep(client, tasks_client)`, returns
  `{"enqueued": len(results)}` (renamed from `audited_count` — sweep no
  longer waits for the audits to finish, so "audited" would be
  inaccurate).
- New `POST /audit` — receives `{identity_key, pattern_data}`, calls
  `audit_pattern(identity_key, pattern_data, firestore_client)` directly,
  returns the decision. Gated by Cloud Run IAM the same way `/sweep` is
  today (OIDC-authenticated caller, never `--allow-unauthenticated`) — no
  manual token check needed in application code.
- `BackgroundTasks` import/parameter dropped from `/classify` — no
  longer used once the audit trigger goes through `enqueue_audit()`
  instead of `background_tasks.add_task(...)`.

## Data flow

1. **`/classify` (event trigger):** `classify_alert()` runs unchanged.
   On SUPPRESS, `enqueue_audit()` is called synchronously; the response
   returns regardless of whether the enqueue succeeded, failed, or hit
   the dedup case.
2. **Cloud Tasks dispatch:** at its own schedule (near-immediate, no
   delay configured), Cloud Tasks issues an OIDC-authenticated
   `POST /audit` to this Cloud Run service — a fresh inbound request,
   full CPU guaranteed for its duration.
3. **`/sweep` (scheduled trigger):** selects targets, enqueues one task
   per target, returns immediately with the enqueued count. Each
   enqueued task then flows through step 2 identically to a
   `/classify`-triggered one.

## Error handling & retry safety

- **Enqueue failures** (auth, quota, queue missing): wrapped in
  `AuditEnqueueError`, logged via `loguru` with `identity_key` as
  context, swallowed at the `/classify` call site so the classification
  response still succeeds. This is a real, accepted trade-off: an
  enqueue failure is now visible in logs but not to the caller.
- **Dedup** (`AlreadyExists`): expected, not an error. Logged at
  `WARNING`, not `ERROR`.
- **At-least-once delivery:** Cloud Tasks does not guarantee exactly-once.
  `audit_pattern()` must be safe to run twice for the same task, and
  already is: `mark_under_review()` is idempotent (sets a flag `True`
  again), `invalidate_instances()`/`clear_under_review()` are idempotent
  (removing already-removed instance IDs is a no-op; `last_reviewed_at`
  just gets restamped). This spec relies on that property explicitly —
  it was true before, but was never load-bearing until now.
- **`/audit` failures:** a non-2xx response causes Cloud Tasks to retry
  per the queue's retry config. Default proposed: 5 max attempts,
  exponential backoff — unvalidated against real traffic, same posture
  as `GRADUATION_THRESHOLD`/`MIN_DIVERSITY` elsewhere in this project.
  Revisit once real audit-failure-rate data exists.

## Logging

`loguru` adopted for the first time in this codebase as part of this
change (CLAUDE.md requires it on all major features; this is the first
feature where a genuinely-silent failure mode — enqueue errors — made
deferring it further indefensible). Scope kept minimal: logger calls at
the new failure/dedup points in `task_queue.py` and `main.py`'s
enqueue-failure catch site, with `identity_key` as context on every
call. Not in scope: OTel/MLflow tracing, structured log sinks, or
retrofitting logging onto existing modules that don't have it yet.

## Testing

- `FakeTasksClient` in `tests/conftest.py`, mirroring the existing
  `FakeFirestoreClient` pattern: `create_task()` tracks task names in a
  dict, raises a stand-in `AlreadyExists` on a repeat name within the
  "queue".
- `tests/test_task_queue.py` (new): `_task_name()` determinism (same
  identity_key → same name, different identity_key → different name),
  `enqueue_audit()` returns `True` on first call / `False` on dedup hit
  without a second real create attempt, non-dedup client errors surface
  as `AuditEnqueueError`.
- `tests/test_main.py` updates: remove the old `under_review`-guard
  tests (guard no longer exists in `main.py`); add tests for the new
  `/audit` endpoint invoking `audit_pattern()`; add a test asserting
  `/classify` still returns 200 with the classification result when
  `enqueue_audit()` raises.
- `tests/test_orchestrator.py`: new tests for `run_scheduled_sweep()`'s
  enqueue-based behavior (no dedicated test currently exists for this
  function — this is the first).

## Deploy (`DEPLOY.md` additions)

- `gcloud tasks queues create vor-audit-queue --location us-central1 --max-attempts 5 ...`
  — retry config flagged as an unvalidated starting point, same as noted
  above.
- IAM: the identity enqueuing tasks needs `roles/cloudtasks.enqueuer` on
  the queue; the OIDC identity Cloud Tasks uses to call `/audit` needs
  `roles/run.invoker` on the Cloud Run service — same shape as the
  existing Cloud Scheduler → `/sweep` binding (DEPLOY.md step 2), likely
  reusable.
- `/audit` added to the "never `--allow-unauthenticated`" list alongside
  `/classify` and `/sweep`.
- New dependency: `google-cloud-tasks` in `requirements.txt`.
- New dependency: `loguru` in `requirements.txt`.

## Open items carried forward, not resolved here

- Retry-count/backoff defaults are a guess, not calibrated against real
  audit failure rates.
- Enqueue-failure visibility is log-only; no alerting is wired up.
- `/classify` still has no actual trigger source connected (pre-existing
  gap, DEPLOY.md step 4 — unaffected by this change).
