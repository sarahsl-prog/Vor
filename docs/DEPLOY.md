# Deploying Vör to Cloud Run + Cloud Scheduler

None of these commands have been run yet — this is the plan, not a record
of what's live. Run them yourself when ready; none of this should execute
without you actually reviewing it first, especially the parts that create
billable resources.

For a one-command, idempotent deploy, see `scripts/deploy.sh` and its
tear-down companion `scripts/deploy-cleanup.sh`. The sections below still
show the equivalent manual `gcloud` commands so you can review exactly what
the script does before running it.

## 1. Build and deploy the Cloud Run service

```bash
gcloud run deploy vor \
  --source . \
  --region us-central1 \
  --no-allow-unauthenticated \
  --min-instances 0 \
  --max-instances 3 \
  --memory 512Mi
```

`--no-allow-unauthenticated` on purpose — both `/classify` and `/sweep`
should require a caller identity, not sit open on the internet. `--min-
instances 0` is the scale-to-zero cost tip from the hackathon resources;
`--max-instances 3` caps runaway spend from an unexpected traffic spike.

## 2. Create a service account for Cloud Scheduler to invoke as

```bash
gcloud iam service-accounts create vor-scheduler \
  --display-name "Vör Cloud Scheduler invoker"

gcloud run services add-iam-policy-binding vor \
  --region us-central1 \
  --member "serviceAccount:vor-scheduler@YOUR_PROJECT_ID.iam.gserviceaccount.com" \
  --role "roles/run.invoker"
```

## 3. Create the weekly sweep job

```bash
gcloud scheduler jobs create http vor-weekly-sweep \
  --location us-central1 \
  --schedule "0 3 * * 1" \
  --uri "https://YOUR_CLOUD_RUN_URL/sweep" \
  --http-method POST \
  --oidc-service-account-email "vor-scheduler@YOUR_PROJECT_ID.iam.gserviceaccount.com" \
  --oidc-token-audience "https://YOUR_CLOUD_RUN_URL"
```

Runs Monday 3am UTC — low-traffic window, weekly cadence per the hybrid
cadence design (safety net for patterns the event-triggered path never
revisits, not meant to be frequent).

## 3a. Create the Cloud Tasks queue and grant enqueue/callback IAM

```bash
gcloud tasks queues create vor-audit-queue \
  --location us-central1 \
  --max-attempts 5 \
  --min-backoff 10s \
  --max-backoff 300s
```

Retry config (5 attempts, 10s-300s exponential backoff) is a starting
point, not calibrated against real audit failure rates — same posture
as `GRADUATION_THRESHOLD`/`MIN_DIVERSITY` elsewhere in this project.
Revisit once real traffic data exists.

The Cloud Run service's own identity needs permission to enqueue tasks:

```bash
gcloud tasks queues add-iam-policy-binding vor-audit-queue \
  --location us-central1 \
  --member "serviceAccount:YOUR_CLOUD_RUN_SERVICE_ACCOUNT" \
  --role "roles/cloudtasks.enqueuer"
```

Cloud Tasks calls back into `POST /audit` the same way Cloud Scheduler
calls `POST /sweep` — reuse the `vor-scheduler` service account created
in step 2, since it already has `roles/run.invoker` on this service.
No new service account or binding is needed for the callback itself,
only for enqueueing (above).

Set the environment variables `/classify`, `/sweep`, and `/audit` all
need, on the Cloud Run service itself:

```bash
gcloud run services update vor \
  --region us-central1 \
  --set-env-vars "GCP_PROJECT=YOUR_PROJECT_ID,TASKS_LOCATION=us-central1,TASKS_QUEUE=vor-audit-queue,TASKS_OIDC_SA_EMAIL=vor-scheduler@YOUR_PROJECT_ID.iam.gserviceaccount.com,SERVICE_URL=https://YOUR_CLOUD_RUN_URL,GOOGLE_GENAI_USE_VERTEXAI=true,GOOGLE_CLOUD_PROJECT=YOUR_PROJECT_ID,GOOGLE_CLOUD_LOCATION=us-central1"
```

Three further variables are optional and omitted above because each has a
working default — add them to `--set-env-vars` if the default isn't what
you want:

| Variable | Default if unset | Set it when |
|---|---|---|
| `FIRESTORE_DATABASE` | `(default)` | Your data lives in a **named** Firestore database. Nothing errors if this is wrong — the service and every `scripts/` entrypoint just read and write the default database instead. |
| `GEMINI_MODEL` | `DEFAULT_GEMINI_MODEL` (`vor_agents/model_config.py`) | Pinning or upgrading the model without a code deploy. |
| `MLFLOW_EXPERIMENT_NAME` | MLflow's `Default` experiment | Always, on a shared tracking server — otherwise dev/staging/prod traces are indistinguishable after the fact. |
| `SWEEP_MAX_TARGETS` | `10` | Tuning how much the weekly sweep costs. Every target is a model call, so this is the sweep's cost/coverage dial. Minimum 1; `0` is **rejected**, not honored — it would disable the safety-net audit path while looking like a sweep that found nothing. |
| `BLAST_RADIUS_CACHE_TTL_SECONDS` | `300` | Trading Firestore reads against how fast a committed blast-radius entry goes live. `0` means never serve from cache. |
| `SESSION_DB_URL` | In-memory SQLite (`vor_agents/session_config.py`) | Deploying to Cloud Run. `scripts/deploy.sh` builds the Cloud SQL Postgres connection string and mounts it from Secret Manager via `--set-secrets`, not as a plaintext env var — it embeds the DB password (see section 3f below). A value that can't be used falls back to the in-memory default rather than failing the deploy. |
| `TRACE_REPLAY_BATCH_SIZE` | `1000` | Capping how many `pending_traces` docs one scheduled replay run reads into memory. Every 15 minutes (see MLflow tracing section below) this many docs get materialized at once, so this is the replay run's memory dial. Minimum 1; a value below that is rejected and the default is used. |

`SWEEP_MAX_TARGETS` and `BLAST_RADIUS_CACHE_TTL_SECONDS` are integers. A value
that isn't a valid integer, or is below its minimum, is logged at WARNING
and the default is used — a typo in a deploy flag must not take a request
path down. Check the logs after changing either if the new value doesn't
seem to be taking effect.

`/audit` must never be deployed with `--allow-unauthenticated`, same as
`/classify` and `/sweep` — it's reached exclusively via Cloud Tasks'
OIDC-authenticated dispatch.

The `GOOGLE_GENAI_USE_VERTEXAI`/`GOOGLE_CLOUD_PROJECT`/`GOOGLE_CLOUD_LOCATION`
trio is what `classifier_agent.py`/`auditor_agent.py` actually run
against — no code change needed, `google-genai` (an ADK dependency) reads
these directly. No API key involved: the Cloud Run service authenticates
to Vertex AI as itself, via its own service account's Application Default
Credentials, same identity model as every other GCP call this service
already makes (Firestore, Cloud Tasks). That service account needs the
Vertex AI role granted:

```bash
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
  --member "serviceAccount:YOUR_CLOUD_RUN_SERVICE_ACCOUNT" \
  --role "roles/aiplatform.user"
```

`YOUR_CLOUD_RUN_SERVICE_ACCOUNT` here is the Cloud Run service's own
runtime identity (the default compute service account unless `--service-
account` was passed in step 1), not `vor-scheduler` — that one is only
for invoking this service and enqueueing Cloud Tasks, unrelated to what
this service calls outward to Vertex AI.

## 3b. needs_attention collection (no setup required)

A pattern whose audits fail 3 times consecutively gets a doc written to
the `needs_attention` Firestore collection (same project, same
credentials already in use -- Firestore is schemaless, nothing to
provision). **Nothing currently pushes this to a human** -- no
dashboard, no alerting integration. Check it manually:

```bash
gcloud firestore documents list --collection-ids=needs_attention
```

Revisit once there's an actual notification channel to wire this into.

## 3c. Seed the blast-radius table and gate the commit endpoint

```bash
uv run python scripts/seed_blast_radius_table.py
```

Run once, before first production deploy — populates `blast_radius_table`
with the 5 entries that used to be hardcoded. Without this,
`estimate_blast_radius()` falls back to `UNSCORED_DEFAULT` for every
alert until someone re-proposes and commits each entry by hand.

`/blast-radius/commit` must never be deployed with
`--allow-unauthenticated`, same as `/classify`/`/sweep`/`/audit` —
gate it with the same Cloud Run IAM approach (OIDC-authenticated caller).
Unlike the others, this endpoint is meant to be called by a human, not a
machine dispatcher — grant `roles/run.invoker` to whichever human
identities (or a shared review service account) should be allowed to
commit blast-radius proposals.

## 3d. Backfill identity_key on pre-existing data

Only needed if the Firestore project already holds confidence docs written
before the doc-ID scheme changed (`docs/TODO-Aug15.md` Task 3). Those docs
carry no `identity_key` field, so `_fetch_all_confirmed_patterns()` skips
them with a warning — they are not lost, but they stop being audited.

```bash
uv run python scripts/backfill_identity_key.py --dry-run
uv run python scripts/backfill_identity_key.py
```

Recovers each doc's identity_key from its own `confirmed_instances`, then
rewrites it under the new hashed doc ID and deletes the legacy one.
Idempotent — docs that already carry the field are skipped, so a re-run is
a no-op. Exits non-zero if any doc could not be migrated; the log names
each one and why. A fresh project needs none of this.

## 3e. Seed confirmed-negative history (optional)

Vör starts knowing nothing: every pattern is NO_HISTORY until evidence
accumulates, so nothing is autonomously suppressed on day one. To start
from existing history instead:

```bash
uv run python scripts/seed_firestore.py --file history.json --dry-run
uv run python scripts/seed_firestore.py --file history.json
```

`--dry-run` first, always — it reports the tier each batch would land at
without writing, and a batch that lands `provisional` (too few instances,
or too uniform) will not autonomously suppress. Everything seeded this way
is marked provenance `seeded` / `verified_by: bulk`, because no human
signed off on the instances individually. See `docs/DATASET_RUNBOOK.md`
for the file format and for seeding synthetic cases instead.

## 3f. Cloud SQL for session persistence

The ADK Runner needs a `SessionService` to track in-flight conversation
state during a single classify/audit call. Vör uses ADK's
`DatabaseSessionService`, backed by Cloud SQL for Postgres in production
(an in-memory SQLite database is used automatically when `SESSION_DB_URL`
is unset — that's what local dev and the test suite run against; see
`vor_agents/session_config.py`).

`scripts/deploy.sh` provisions the Cloud SQL instance, database, and DB
user, wires the Cloud Run service to it via `--add-cloudsql-instances`
(a Unix-socket connection through the built-in Cloud SQL proxy sidecar,
not a public IP), and grants the Cloud Run service account
`roles/cloudsql.client`. Set `SESSION_DB_PASSWORD` before running it —
there's no default, and the script refuses to run without one.

The assembled `SESSION_DB_URL` embeds that password, so it is **not**
passed to Cloud Run as a plaintext environment variable. Cloud Run env
vars are readable by anyone with `run.services.get`, via `gcloud run
services describe` or the console. Instead the script stores the URL in
Secret Manager — secret name from `SESSION_DB_URL_SECRET`, default
`vor-session-db-url` — grants the Cloud Run service account
`roles/secretmanager.secretAccessor`, and mounts it into the service with
`--set-secrets "SESSION_DB_URL=<secret>:latest"`. Same rule this document
already states for the MLflow credential below: that credential goes in
Secret Manager / `.env`, never hardcoded, per CLAUDE.md's secrets rule.

One residual exposure remains and is accepted for now: the
`gcloud sql users create --password=...` call passes the password as a
command-line argument, which is briefly visible via `ps` on whatever
machine runs the script — removing it means either an interactive prompt
(which breaks automation) or migrating to Cloud SQL IAM database
authentication (no password at all).

Every session created by `_run_agent()` is deleted again in the same
request's `finally` block (see `orchestrator._discard_session`) — nothing
here changes that lifecycle. What changes is durability: a Cloud Run
instance recycled mid-request (autoscaling, deploy, OOM) no longer
silently drops session state that lived only in that instance's heap.

Tier is `db-f1-micro`, the smallest/cheapest Cloud SQL option — an
unvalidated starting point, same posture as every other capacity default
in this project (`SWEEP_MAX_TARGETS`, the Cloud Tasks retry backoff).
Revisit once real traffic volume exists.

## 4. Wire /classify to a Pub/Sub push subscription

```bash
gcloud pubsub topics create vor-alerts

gcloud pubsub subscriptions create vor-alerts-sub \
  --topic vor-alerts \
  --push-endpoint "https://YOUR_CLOUD_RUN_URL/classify" \
  --push-auth-service-account "vor-scheduler@YOUR_PROJECT_ID.iam.gserviceaccount.com" \
  --ack-deadline 600
```

`--ack-deadline 600` (Pub/Sub's max) is required, not cosmetic: `/classify`
synchronously calls `classify_alert()`, which does a Firestore read plus a
Gemini call via ADK, and that round-trip routinely exceeds the 10s default
ack deadline -- without this flag Pub/Sub redelivers a slow-but-successful
request as if it failed. Cloud Run's own request timeout (step 1) is still
the outer bound on how long a single attempt can run.

Reuses the `vor-scheduler` service account created in step 2 -- it already
has `roles/run.invoker` on this service, same reuse pattern as the Cloud
Tasks `/audit` callback (step 3a).

Whatever the ingest source is (Hayabusa output, a Sigma rule webhook,
etc. -- not built as part of this repo) needs `roles/pubsub.publisher` on
the `vor-alerts` topic:

```bash
gcloud pubsub topics add-iam-policy-binding vor-alerts \
  --member "serviceAccount:YOUR_INGEST_SOURCE_SERVICE_ACCOUNT" \
  --role "roles/pubsub.publisher"
```

`/classify` accepts both a Pub/Sub push envelope and a raw alert JSON
body -- see `main.py`'s `_decode_classify_body()`. No separate endpoint
for direct/manual calls.

To verify the path end to end before a real ingest source exists,
`scripts/generate_events.py` publishes synthetic alerts to this topic
(see `docs/DATASET_RUNBOOK.md`). Whatever identity you run it as needs
the same `roles/pubsub.publisher` binding above. Run it with `--dry-run`
first: every event that lands is a real Gemini call, plus an audit
enqueue per `SUPPRESS`.

**Still open:** no dead-letter topic or `--max-delivery-attempts`
configured on `vor-alerts-sub` yet -- a permanently-malformed message
will retry and 422 until it ages out of the subscription's retention
window. Revisit once real traffic volume exists to calibrate against.

## Scripted deploy / cleanup

```bash
# Deploy everything described below
export GCP_PROJECT=your-project-id
export GCP_REGION=us-central1
./scripts/deploy.sh

# Tear it down when you are done (does not delete Firestore data)
./scripts/deploy-cleanup.sh
```

`deploy.sh` creates/updates the Cloud Run service, the scheduler service
account, the Cloud Tasks queue, the Cloud Scheduler jobs, the Pub/Sub topic
and subscription, and the required IAM bindings. It also sets the
environment variables the service needs. Re-running it is safe.

`deploy-cleanup.sh` deletes the same resources in the reverse order. It
does **not** delete Firestore collections, since they may contain evidence
you want to keep; if you want to remove that data too, use the
`gcloud firestore documents delete-all` command shown in the script's
summary.

Both scripts require `gcloud` to be authenticated with sufficient
permissions (Project Owner or equivalent roles to manage Cloud Run,
Scheduler, Tasks, Pub/Sub, IAM, and service accounts).

## 5. MLflow tracing

Set the tracking server URI on the Cloud Run service, alongside the
existing env vars:

```bash
gcloud run services update vor \
  --region us-central1 \
  --update-env-vars "MLFLOW_TRACKING_URI=https://YOUR_MLFLOW_SERVER"
```

Set `MLFLOW_EXPERIMENT_NAME` alongside it. Without it every run lands in
MLflow's `Default` experiment, so a tracking server shared across
environments mixes their traces together irreversibly:

```bash
gcloud run services update vor \
  --region us-central1 \
  --update-env-vars "MLFLOW_EXPERIMENT_NAME=vor-prod"
```

Both are read by the `mlflow` client itself, not by this repo's code.

If the managed server requires its own auth (API key, service-account
token), that credential goes in Secret Manager / `.env`, never
hardcoded, per CLAUDE.md's secrets rule -- consult whichever managed
MLflow offering you're using (Databricks-hosted or self-run) for its own
auth mechanism; this repo's code just reads `MLFLOW_TRACKING_URI` and
whatever auth env vars the `mlflow` client itself expects.

Scheduled replay job for the pending_traces fallback queue:

```bash
gcloud scheduler jobs create http vor-trace-replay \
  --location us-central1 \
  --schedule "*/15 * * * *" \
  --uri "https://YOUR_CLOUD_RUN_URL/replay-traces" \
  --http-method POST \
  --oidc-service-account-email "vor-scheduler@YOUR_PROJECT_ID.iam.gserviceaccount.com" \
  --oidc-token-audience "https://YOUR_CLOUD_RUN_URL"
```

Every 15 minutes -- unvalidated starting point, same posture as every
other unvalidated interval/threshold in this project. Reuses the
existing `vor-scheduler` service account, same as `/sweep`. `/replay-traces`
must never be deployed with `--allow-unauthenticated`.

`pending_traces` growth during an extended MLflow outage is now bounded
two ways: `replay_pending_traces()` reads at most `$TRACE_REPLAY_BATCH_SIZE`
docs per scheduled run (default 1000, see `vor_agents/tracing.py`), and
`scripts/deploy.sh` sets a Firestore TTL policy on `queued_at` so docs
older than the TTL window are eventually purged even if MLflow never
recovers. Set `TRACE_REPLAY_BATCH_SIZE` on the Cloud Run service if the
default batch size doesn't match your traffic.
