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
| `PENDING_TRACE_RETENTION_DAYS` | `30` | Changing how long a queued trace survives before the Firestore TTL policy deletes it. Minimum 1; a bad value is logged and the default used. This is the only thing bounding `pending_traces` if MLflow never recovers. |
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
(see `docs/DATASET_RUNBOOK.md`). Set `EVENT_PUBLISHER_SA` before running
`deploy.sh` and the binding above is granted for you.

### Running the generator from a GCE VM

A convenient place to run it is the same VM that hosts MLflow — see
"Network setup for the MLflow VM" in section 5 for a VPC layout that keeps
that box reachable only from Vör and your own SSH. What it needs:

```bash
# 1. The repo and its dependencies (needs Python 3.13)
git clone https://github.com/sarahsl-prog/Vor.git && cd Vor
uv sync

# 2. Credentials. Either the VM's attached service account, or your own:
gcloud auth application-default login

# 3. Confirm what would be sent -- no credentials or topic needed for this
uv run python scripts/generate_events.py --count 20 --dry-run
```

The VM's attached service account needs `roles/pubsub.publisher` on
`vor-alerts` (that's what `EVENT_PUBLISHER_SA` grants). Note that the
**default GCE access scopes do not include Pub/Sub publish** — if the VM
was created without `--scopes cloud-platform`, publishing fails with a
403 even though the IAM binding is correct. Either recreate the VM with
that scope or authenticate as a user with `gcloud auth
application-default login`.

On a VM with no external IP, the `git clone` and `uv sync` above need
Cloud NAT and the Google API calls need Private Google Access — both are
in the section 5 network setup.

Then send for real:

```bash
export GCP_PROJECT=your-project-id
uv run python scripts/generate_events.py --count 200 --rate 1
```

**Start at `--rate 1`.** Every event is a real Gemini call plus an audit
enqueue per `SUPPRESS`, so the rate is a spend dial as much as a load
dial. It is also a backpressure dial: the service deploys with
`--max-instances 3`, and a rate that outruns three instances leaves
messages unacknowledged until the 600s deadline, at which point Pub/Sub
redelivers them and the same alerts get classified twice. Watch the
Cloud Run request count and the subscription's oldest-unacked-message age
before turning it up.

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

`deploy.sh` enables the required APIs, then creates/updates the Cloud Run
service, the scheduler service account, the Cloud Tasks queue, the Cloud
SQL instance and session-DB secret, the Cloud Scheduler jobs, the Pub/Sub
topic and subscription, the Firestore TTL policy, and the required IAM
bindings. It also sets the environment variables the service needs.

Re-running it is safe, with one thing to know: it sets the service's
environment with `--set-env-vars`, which **replaces** the whole set rather
than merging. That makes the script the single source of truth for the
service's configuration, and it means any variable you applied separately
with `--update-env-vars` is removed on the next run. Export everything you
want — `MLFLOW_TRACKING_URI` above all — in the same shell each time:

```bash
export GCP_PROJECT=your-project-id
export GCP_REGION=us-central1
export SESSION_DB_PASSWORD='...'                     # required
export MLFLOW_TRACKING_URI=http://10.128.0.5:5000    # else traces only queue
export MLFLOW_EXPERIMENT_NAME=vor-prod
export EVENT_PUBLISHER_SA=my-vm@PROJECT.iam.gserviceaccount.com
./scripts/deploy.sh
```

A failing step now stops the script. An earlier version discarded the
error and still printed a success summary, which was worst for the
env-var step: `main.py`'s `_enqueue()` deliberately swallows a missing
`TASKS_QUEUE`/`SERVICE_URL`, so a green deploy could leave a service that
accepted alerts and silently never enqueued a single audit.

`deploy-cleanup.sh` deletes the same resources in reverse order, including
the Cloud SQL instance and the session-DB secret — pass
`SKIP_CLOUDSQL_CLEANUP=1` to keep those, and note that the instance keeps
billing if you do. It does **not** delete Firestore collections, since
they may contain evidence you want to keep; if you want to remove that
data too, use the `gcloud firestore documents delete-all` command shown in
the script's summary.

Both scripts require `gcloud` to be authenticated with sufficient
permissions (Project Owner or equivalent roles to manage Cloud Run,
Scheduler, Tasks, Pub/Sub, IAM, and service accounts).

## 5. MLflow tracing

**Set these before running `deploy.sh`, not after.** Step 6 of the script
uses `--set-env-vars`, which *replaces* the service's whole environment.
Anything applied out-of-band with `--update-env-vars` survives only until
the next deploy, which then silently removes it and sends every trace to
the fallback queue instead:

```bash
export MLFLOW_TRACKING_URI="http://10.128.0.5:5000"   # your server
export MLFLOW_EXPERIMENT_NAME="vor-prod"
./scripts/deploy.sh
```

`deploy.sh` warns when `MLFLOW_TRACKING_URI` is unset, because without it
nothing reaches MLflow at all: every trace queues to `pending_traces`,
and the dashboard's Traces/Home/Pipeline pages have nothing to read.

Without `MLFLOW_EXPERIMENT_NAME` every run lands in MLflow's `Default`
experiment, so a tracking server shared across environments mixes their
traces together irreversibly.

### Network setup for the MLflow VM

The goal: the tracking server is reachable **only** from Vör, plus your own
SSH. That is achievable with no external IP on the VM at all, which is both
simpler and stronger than firewalling a public address — Cloud Run's egress
has no fixed source range to restrict without VPC egress anyway.

```
Cloud Run (Vör)  ──Direct VPC egress──▶ subnet: vor-run (10.10.1.0/26)
                                             │
                          firewall: tcp:5000 │ source = 10.10.1.0/26
                                             ▼
                                    VM (no external IP), tag: mlflow
                                    subnet: vor-data (10.10.0.0/24)
                                             ▲
                          firewall: tcp:22   │ source = 35.235.240.0/20 (IAP)
                                             │
You ──gcloud compute ssh --tunnel-through-iap┘

VM ──▶ Google APIs (Pub/Sub, Firestore)  via Private Google Access
VM ──▶ PyPI / GitHub                     via Cloud NAT
```

Two subnets in one VPC. The Cloud Run subnet is dedicated to the service, so
its CIDR *is* the identity being filtered on — which matters, because MLflow
ships with **no authentication of its own**. The firewall is doing the
authenticating, and the tracking server holds the same alert, decision and
reasoning data Firestore does.

**1. VPC and subnets.** Private Google Access on the data subnet is what
lets the VM reach Pub/Sub and Firestore without an external IP:

```bash
gcloud compute networks create vor-vpc --subnet-mode custom

gcloud compute networks subnets create vor-data \
  --network vor-vpc --region us-central1 \
  --range 10.10.0.0/24 \
  --enable-private-ip-google-access

gcloud compute networks subnets create vor-run \
  --network vor-vpc --region us-central1 \
  --range 10.10.1.0/26
```

`/26` is the floor Google recommends for Direct VPC egress; at
`--max-instances 3` (step 1) it is ample.

**2. Firewall.** GCP already denies all other ingress at priority 65535, so
only two allows are needed. The explicit logged deny is for visibility, not
enforcement:

```bash
gcloud compute firewall-rules create allow-mlflow-from-vor \
  --network vor-vpc --direction INGRESS --action ALLOW \
  --rules tcp:5000 --source-ranges 10.10.1.0/26 --target-tags mlflow

gcloud compute firewall-rules create allow-ssh-from-iap \
  --network vor-vpc --direction INGRESS --action ALLOW \
  --rules tcp:22 --source-ranges 35.235.240.0/20 --target-tags mlflow

gcloud compute firewall-rules create deny-all-ingress-logged \
  --network vor-vpc --direction INGRESS --action DENY \
  --rules all --source-ranges 0.0.0.0/0 --priority 65000 \
  --enable-logging
```

`35.235.240.0/20` is IAP's TCP-forwarding range: fixed, Google-owned, and
the only source that can reach port 22.

**3. The VM, with no external IP:**

```bash
gcloud compute instances create vor-mlflow \
  --zone us-central1-a \
  --subnet vor-data --no-address \
  --tags mlflow \
  --scopes cloud-platform \
  --service-account vor-mlflow@YOUR_PROJECT_ID.iam.gserviceaccount.com
```

`--scopes cloud-platform` is the one that bites otherwise: the default GCE
scopes omit Pub/Sub publish, and `scripts/generate_events.py` then 403s from
this box even with the IAM binding correct.

**4. Cloud NAT**, because `uv sync` and `git clone` need the public internet
and Private Google Access only covers Google APIs. This is egress-only — it
gives the VM outbound reach without making it reachable:

```bash
gcloud compute routers create vor-router --network vor-vpc --region us-central1

gcloud compute routers nats create vor-nat \
  --router vor-router --region us-central1 \
  --nat-all-subnet-ip-ranges --auto-allocate-nat-external-ip
```

**5. Point Cloud Run at the VPC:**

```bash
gcloud run services update vor --region us-central1 \
  --network vor-vpc --subnet vor-run \
  --vpc-egress private-ranges-only
```

`private-ranges-only` matters: only RFC1918 traffic goes through the VPC, so
Vertex AI, Firestore and Cloud Tasks keep their normal Google path.
`all-traffic` would force everything through the VPC and require NAT for the
Cloud Run service too. A Serverless VPC Access connector is the older
equivalent if Direct VPC egress is unavailable in your region.

**6. Your own access**, for SSH and both UIs over one tunnel:

```bash
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
  --member "user:YOUR_EMAIL" \
  --role "roles/iap.tunnelResourceAccessor"

gcloud compute ssh vor-mlflow --zone us-central1-a --tunnel-through-iap \
  -- -L 8501:localhost:8501 -L 5000:localhost:5000
```

That puts the Streamlit dashboard on `localhost:8501` and the MLflow UI on
`localhost:5000` with neither port open to anything.

`MLFLOW_TRACKING_URI` is then the VM's internal address for Cloud Run
(`http://10.10.0.2:5000`), and `http://localhost:5000` for the dashboard if
you run it on the VM. Reserve the internal IP as static
(`gcloud compute addresses create --subnet vor-data`) or use the stable
internal DNS name `vor-mlflow.us-central1-a.c.YOUR_PROJECT_ID.internal`, or
the URI breaks when the VM is recreated.

Bind MLflow to `--host 0.0.0.0` (see the next section). That is safe *only*
because there is no external IP and the firewall is closed — it is the
firewall making it safe, so don't relax either without adding real auth. Any
credential for auth you do add goes in Secret Manager / `.env`, never
hardcoded, per CLAUDE.md's secrets rule.

**Two caveats on this design, neither verified against a live project:**

- Firewall filtering by **source service account** would be a stronger
  control than a CIDR, but it is a GCE-source feature and is not expected to
  apply to Cloud Run Direct VPC egress traffic. Hence the dedicated subnet,
  whose CIDR stands in for the service's identity. If SA-based filtering
  does work for it, prefer that.
- **Migrating an existing VM** that has an external IP: remove it with
  `gcloud compute instances delete-access-config`, but only *after* Cloud NAT
  is up, or the VM loses outbound access mid-flight.

### Check the server's backend store

MLflow 3.15 put the bare `./mlruns` **file store into maintenance mode**:
it raises unless `MLFLOW_ALLOW_FILE_STORE=true`. If your server was
started without `--backend-store-uri`, move it to a database backend
before pointing Vör at it:

```bash
mlflow server --host 0.0.0.0 --port 5000 \
  --backend-store-uri sqlite:////var/lib/mlflow/mlflow.db \
  --artifacts-destination /var/lib/mlflow/artifacts
```

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

Runs are logged with the scalar fields (`decision`, `action`,
`overrides_fired`, `uncertain_reason`, `audit_failed`, `identity_key`) as
MLflow **params** and the reasoning as a **tag**, alongside the full
`run_data.json` artifact. The params are what `search_runs()` returns, so
a reader can filter and aggregate over one query; the artifact stays the
complete record for anything else. The dashboard's Traces, Home and
Pipeline pages depend on this -- give the dashboard the same
`MLFLOW_TRACKING_URI` and `MLFLOW_EXPERIMENT_NAME` as the service, or it
has nothing to read.

Runs logged before this change carry only `run_type` and `identity_key`,
so they appear in the dashboard with `—` for decision and action. That is
historical data, not a fault; nothing backfills them.

`pending_traces` growth during an extended MLflow outage is bounded two
ways: `replay_pending_traces()` reads at most `$TRACE_REPLAY_BATCH_SIZE`
docs per scheduled run (default 1000, see `vor_agents/tracing.py`), and
`scripts/deploy.sh` sets a Firestore TTL policy on **`expires_at`**, a
timestamp written as queue time + `$PENDING_TRACE_RETENTION_DAYS`
(default 30), so docs are purged even if MLflow never recovers.

The policy targets `expires_at` and not `queued_at` for two reasons, both
of which bit an earlier version of this document. Firestore TTL acts only
on **timestamp** fields, and `queued_at` is an ISO string — so the policy
deleted nothing, and the bound described here did not exist. And
Firestore's expiry *is* the field's value, with no separate window to
configure, so a policy on the queue time would mark every doc expired the
instant it was written and sweep the queue within about a day — exactly
the long outage it is meant to survive.

Set `TRACE_REPLAY_BATCH_SIZE` or `PENDING_TRACE_RETENTION_DAYS` before
running `deploy.sh` if the defaults don't match your traffic.

## 6. The dashboard

`dashboard/` is a Streamlit app and is deliberately **not** deployed by
`deploy.sh` — it is an operator tool, not part of the request path, and
it reads the same two stores the service writes.

It needs:

| | |
|---|---|
| Firestore | Application Default Credentials with read access to the project, plus `FIRESTORE_DATABASE` if you use a named database. Without them it renders clearly-labelled demo data. |
| MLflow | The **same** `MLFLOW_TRACKING_URI` and `MLFLOW_EXPERIMENT_NAME` as the Cloud Run service. A different experiment means an empty Traces page rather than an error. |

Running it on the MLflow VM is the least work — the tracking URI is then
just `http://localhost:5000`, with no Cloud Run networking involved:

```bash
export MLFLOW_TRACKING_URI=http://localhost:5000
export MLFLOW_EXPERIMENT_NAME=vor-prod
uv run streamlit run dashboard/app.py --server.port 8501
```

Reach it over a tunnel rather than opening the port — the dashboard has no
authentication of its own and shows every alert, decision and reasoning
trace. With the section 5 network setup the VM has no external IP, so the
tunnel goes through IAP and carries the MLflow UI along with it:

```bash
gcloud compute ssh vor-mlflow --zone us-central1-a --tunnel-through-iap \
  -- -L 8501:localhost:8501 -L 5000:localhost:5000
```

Do not add a firewall rule for 8501. The dashboard is reachable only
through this tunnel by design.

The Traces page shows a banner when `pending_traces` is non-empty, naming
how many runs the feed is behind by. In a healthy deployment that count
is 0; a growing one means MLflow logging is failing and `/replay-traces`
has not caught up.
