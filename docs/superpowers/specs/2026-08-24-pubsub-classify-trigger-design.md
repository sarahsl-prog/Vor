# Pub/Sub trigger for /classify — design

**Status:** approved for implementation (spec review pending)
**Date:** 2026-08-24
**Classification:** architectural

## Problem

`POST /classify` has no caller. DEPLOY.md step 4 and README both flag this
as open since the first build pass — nothing in this repo, and nothing
external, currently publishes an alert to it. Vör cannot do its one job
(triage incoming alerts) until something wires an ingest source to this
endpoint.

## Goal

Ingest source (Hayabusa output, a Sigma rule webhook, or anything else
that produces alert JSON) publishes to a Pub/Sub topic. A push
subscription on that topic calls `POST /classify`, authenticated the same
OIDC way `/sweep` and `/audit` already are — no new auth model introduced.

## Non-goals

- Building or choosing the actual upstream ingest source (Hayabusa
  integration, Sigma webhook, etc.) — out of scope. This spec only covers
  the Pub/Sub → `/classify` leg.
- Changing `classify_alert()`'s logic or `ClassifierRequest`'s validated
  fields — this is purely a delivery-mechanism change, same posture as the
  Cloud Tasks spec was for the audit path.
- A dead-letter queue / poison-message handling beyond what Pub/Sub push
  retry already provides by default — flagged as a follow-up, not solved
  here.

## Architecture

```
Ingest source --(publish)--> Pub/Sub topic vor-alerts
                                     |
                             (push subscription, OIDC)
                                     v
                              POST /classify
                                     |
                          unwrap envelope -> ClassifierRequest
                                     |
                             classify_alert() (unchanged)
```

One endpoint (`POST /classify`) handles both shapes: a Pub/Sub push
envelope, and a raw alert JSON body (direct/test callers, existing test
suite). No separate `/pubsub/classify` route.

## Components

### `main.py` changes

- `/classify`'s handler currently takes `payload: ClassifierRequest`
  directly as the FastAPI body model. A Pub/Sub push request's body is
  `{"message": {"data": "<base64>", "messageId": ..., "publishTime": ...},
  "subscription": "..."}` — the alert JSON is not the top-level body, it's
  base64-encoded inside `message.data`.
- Add a small `PubSubPushEnvelope` Pydantic model (`vor_agents/schemas.py`,
  next to `ClassifierRequest`/`AuditRequest`) with `message: PubSubMessage`
  where `PubSubMessage.data: str` (base64).
- `/classify`'s handler changes from `payload: ClassifierRequest` (a typed
  FastAPI body param) to `request: Request`, and does the shape detection
  itself:
  1. Parse the raw body as JSON.
  2. If it matches the Pub/Sub envelope shape (`"message" in body and
     "data" in body["message"]`), base64-decode `message.data`, parse
     *that* as JSON, and validate the result against `ClassifierRequest`.
  3. Otherwise, validate the raw body directly against `ClassifierRequest`
     (today's behavior — direct/test callers unaffected).
  4. Either path funnels into the same `ClassifierRequest.model_validate()`
     call, so malformed alert JSON still 422s exactly as it does today,
     with the same error shape.
- This is a real (small) behavior change from today's typed-body param:
  FastAPI's automatic body validation before the handler runs no longer
  applies at the route-declaration level — validation is called
  explicitly inside the handler instead, and its `ValidationError` is
  converted to the same 422 shape FastAPI would have produced, so callers
  can't tell the difference.

### DEPLOY.md additions

- `gcloud pubsub topics create vor-alerts`
- `gcloud pubsub subscriptions create vor-alerts-sub --topic vor-alerts
  --push-endpoint https://YOUR_CLOUD_RUN_URL/classify --push-auth-service-account
  vor-scheduler@YOUR_PROJECT_ID.iam.gserviceaccount.com` — reuses the
  existing `vor-scheduler` service account (already has `roles/run.invoker`
  on this service from step 2), same reuse pattern the Cloud Tasks spec
  used for its own OIDC callback.
- Whatever the ingest source is needs `roles/pubsub.publisher` on the
  `vor-alerts` topic — left as a TODO with a placeholder, since the actual
  ingest source is a non-goal here (see above).

## Data flow

1. Ingest source publishes alert JSON as the Pub/Sub message body.
2. Pub/Sub push delivers `{"message": {"data": base64(alert_json), ...},
   "subscription": "..."}` to `POST /classify`.
3. `main.py` detects the envelope shape, decodes and re-parses, validates
   against `ClassifierRequest` — from here on, identical to today's flow:
   `classify_alert()` runs, SUPPRESS enqueues an audit via the existing
   Cloud Tasks path, response returns.
4. A **direct** caller (test, manual curl, a future non-Pub/Sub source)
   posts alert JSON straight to `/classify` — shape detection falls
   through to the non-envelope branch, behaves exactly as today.

## Error handling & retry safety

- **Malformed envelope** (missing `message.data`, bad base64, non-JSON
  after decode): 422, same as any other malformed-body case. Pub/Sub push
  treats a non-2xx as a delivery failure and retries per the
  subscription's own retry policy — a permanently-malformed message will
  retry and 422 forever until it ages out of the subscription's retention
  window. Acceptable: same posture as `/audit`'s 422-for-permanent-errors
  design in the Cloud Tasks spec — retrying a message that can never
  succeed isn't dangerous, just wasted retries, and Pub/Sub's own
  `maxDeliveryAttempts` + dead-letter-topic config (not built here, see
  non-goals) is the eventual fix.
- **classify_alert() failure paths**: unchanged — `AgentOutputError`
  already degrades to `UNCERTAIN` in code, doesn't 500.
- **Double delivery**: Pub/Sub is at-least-once, same as Cloud Tasks.
  `classify_alert()` has no side effect that isn't already safe to run
  twice for the same alert (enrichment reads are idempotent;
  `_enqueue()`'s Cloud Tasks dedup already handles a duplicate SUPPRESS
  triggering only one audit) — no new idempotency work needed here.

## Logging

`loguru`, consistent with the rest of the codebase. Log at `WARNING` when
envelope detection falls through to the non-envelope branch unexpectedly
(helps distinguish "a direct test caller" from "Pub/Sub started sending a
different shape") — not a new logging surface otherwise, this reuses
`classify_alert()`'s existing log points untouched.

## Testing

- `tests/test_main.py`: new tests — a well-formed Pub/Sub envelope
  containing valid alert JSON reaches `classify_alert()` correctly
  (assert `classify_alert` called with the decoded dict); a malformed
  envelope (bad base64, non-JSON payload) returns 422; a raw (non-envelope)
  body still works exactly as every existing `/classify` test expects,
  unchanged.
- `tests/test_schemas.py` (new, if it doesn't already exist) or added to
  an existing schema test file: `PubSubPushEnvelope` round-trips a
  representative real Pub/Sub push payload shape.

## Open items carried forward, not resolved here

- Actual ingest source (Hayabusa/Sigma/other) — still completely open,
  same as before this spec.
- Dead-letter topic / `maxDeliveryAttempts` config for `vor-alerts-sub` —
  not configured, flagged as a follow-up once real traffic volume exists
  to calibrate against (same posture as Cloud Tasks' retry config).
