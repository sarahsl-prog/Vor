#!/usr/bin/env bash
#
# Deploy Vör to Google Cloud Run + Cloud Scheduler + Cloud Tasks + Pub/Sub.
#
# This script turns the manual steps in docs/DEPLOY.md into a single,
# idempotent command. It creates or updates every resource the running
# service needs, but it does NOT seed data -- run the seed scripts
# separately after deployment (see docs/DEPLOY.md sections 3c/3e).
#
# Required environment variables:
#   GCP_PROJECT          -- Google Cloud project ID.
#   GCP_REGION           -- GCP region for all resources (default: us-central1).
#   SERVICE_URL          -- Fully-qualified URL the service will be reachable at,
#                           e.g. https://vor-xxxxx-uc.a.run.app. If not set, the
#                           script attempts to read the URL of an already-deployed
#                           Cloud Run service named "vor" in GCP_REGION.
#
# Optional environment variables:
#   CLOUD_RUN_SA         -- Service account the Cloud Run service runs as.
#                           Defaults to the project's compute service account:
#                           ${GCP_PROJECT_NUMBER}-compute@developer.gserviceaccount.com.
#   SCHEDULER_SA         -- Service account used by Cloud Scheduler / Cloud Tasks
#                           to invoke the service. Defaults to vor-scheduler.
#   TASKS_QUEUE          -- Cloud Tasks queue name. Default: vor-audit-queue.
#   PUBSUB_TOPIC         -- Pub/Sub topic for inbound alerts. Default: vor-alerts.
#   PUBSUB_SUBSCRIPTION  -- Pub/Sub push subscription name. Default: vor-alerts-sub.
#   FIRESTORE_DATABASE   -- Firestore database name. Default: (default).
#   GEMINI_MODEL         -- Model override. If unset, the service uses its
#                           built-in default.
#   MLFLOW_TRACKING_URI  -- External MLflow tracking server. Optional, but
#                           without it every trace queues to Firestore
#                           instead of reaching MLflow, and the dashboard's
#                           trace pages show nothing. The script warns.
#   MLFLOW_EXPERIMENT_NAME -- MLflow experiment name. Give the dashboard the
#                           same value or it reads a different experiment.
#   EVENT_PUBLISHER_SA   -- Service account that publishes alerts to the
#                           topic (a real ingest pipeline, or the VM you run
#                           scripts/generate_events.py from). Granted
#                           roles/pubsub.publisher when set.
#   TRACE_REPLAY_BATCH_SIZE -- Optional replay batch cap.
#   PENDING_TRACE_RETENTION_DAYS -- Optional; how long a queued trace lives
#                           before the Firestore TTL policy deletes it.
#   SWEEP_MAX_TARGETS    -- Optional sweep target cap.
#   BLAST_RADIUS_CACHE_TTL_SECONDS -- Optional blast-radius cache TTL.
#   SESSION_DB_INSTANCE  -- Cloud SQL instance name for session persistence.
#                           Default: vor-sessions.
#   SESSION_DB_NAME      -- Cloud SQL database name. Default: vor_sessions.
#   SESSION_DB_USER      -- Cloud SQL database user. Default: vor.
#   SESSION_DB_PASSWORD  -- Password for SESSION_DB_USER. No default --
#                           the script exits if this isn't set.
#   SESSION_DB_URL_SECRET -- Secret Manager secret holding the assembled
#                           SESSION_DB_URL (which embeds the password).
#                           Mounted into Cloud Run via --set-secrets rather
#                           than passed as a plaintext env var.
#                           Default: vor-session-db-url.
#
# Usage:
#   export GCP_PROJECT=your-project-id
#   export GCP_REGION=us-central1
#   export SERVICE_URL=https://vor-xyz-uc.a.run.app   # optional if service exists
#   ./scripts/deploy.sh
#
# The script exits non-zero on any unhandled gcloud error -- and, unlike an
# earlier version, actually stops rather than continuing to a green summary.
# Idempotent re-runs are safe: gcloud "create" commands that fail with
# "already exists" are handled, and "update" commands overwrite previous
# settings.
#
# NOTE: step 6 uses --set-env-vars, which REPLACES the service's entire
# environment. Export every variable you want set (MLFLOW_TRACKING_URI in
# particular) before each run; setting them afterwards with
# --update-env-vars works until the next deploy silently removes them.

set -euo pipefail

: "${GCP_PROJECT:?GCP_PROJECT must be set}"
: "${GCP_REGION:=us-central1}"
: "${SCHEDULER_SA:=vor-scheduler}"
: "${TASKS_QUEUE:=vor-audit-queue}"
: "${PUBSUB_TOPIC:=vor-alerts}"
: "${PUBSUB_SUBSCRIPTION:=vor-alerts-sub}"
: "${FIRESTORE_DATABASE:=(default)}"

SCHEDULER_SA_EMAIL="${SCHEDULER_SA}@${GCP_PROJECT}.iam.gserviceaccount.com"
SERVICE_NAME="vor"

# Resolve project number once; needed for the default Cloud Run service account.
echo "Resolving project number for ${GCP_PROJECT}..."
PROJECT_NUMBER=$(gcloud projects describe "${GCP_PROJECT}" --format='value(projectNumber)')
: "${CLOUD_RUN_SA:=${PROJECT_NUMBER}-compute@developer.gserviceaccount.com}"

# ---------------------------------------------------------------------------
# 0. Enable the APIs every later step depends on.
# ---------------------------------------------------------------------------
# Without this a first run on a fresh project dies at whichever service
# happens to be reached first, with an API-not-enabled error that reads
# like a permissions problem. Enabling is idempotent and free.
REQUIRED_APIS=(
  run.googleapis.com
  cloudbuild.googleapis.com
  cloudtasks.googleapis.com
  cloudscheduler.googleapis.com
  sqladmin.googleapis.com
  secretmanager.googleapis.com
  aiplatform.googleapis.com
  firestore.googleapis.com
  pubsub.googleapis.com
  iam.googleapis.com
)

echo ""
echo "=== Vör deployment ==="
echo "Project:        ${GCP_PROJECT}"
echo "Region:         ${GCP_REGION}"
echo "Cloud Run SA:   ${CLOUD_RUN_SA}"
echo "Scheduler SA:   ${SCHEDULER_SA_EMAIL}"
echo "Firestore DB:   ${FIRESTORE_DATABASE}"
echo "MLflow:         ${MLFLOW_TRACKING_URI:-<unset - traces will queue to Firestore>}"
echo ""

# MLflow is not required for the service to run, but leaving it unset is
# almost never what an operator wants: every trace goes to the
# pending_traces fallback queue instead of a tracking server, and the
# dashboard's Traces/Home/Pipeline pages have nothing to read. Warn
# rather than fail -- a deploy without tracing is still a valid deploy.
if [[ -z "${MLFLOW_TRACKING_URI:-}" ]]; then
  echo "WARNING: MLFLOW_TRACKING_URI is not set." >&2
  echo "  Traces will be queued to Firestore (pending_traces) and never replayed," >&2
  echo "  and the dashboard's trace pages will show nothing." >&2
  echo "  See docs/DEPLOY.md section 5." >&2
  echo "" >&2
fi

# ---------------------------------------------------------------------------
# Helper: run gcloud and swallow "already exists" / "already has binding" errors.
# ---------------------------------------------------------------------------
# A real failure returns non-zero and, with `set -e`, stops the script.
# Call sites deliberately do NOT append `|| true`: that used to discard
# this return value, so a genuine failure printed "ERROR:" to stderr and
# the script carried on to print "=== Deployment complete ===" and exit 0.
# The env-var step was among them, and main.py's _enqueue() deliberately
# swallows a missing TASKS_QUEUE/SERVICE_URL -- so a failed step 6 gave a
# green deploy and a service that silently never enqueued a single audit.
# "Already exists" on a re-run is still absorbed here, which is what makes
# the script idempotent; that is this function's whole job.
_run_idempotent() {
  local cmd="$1"
  shift
  local out
  if out=$(gcloud ${cmd} "$@" 2>&1); then
    echo "${out}"
  elif echo "${out}" | grep -qiE "(already exists|already has|duplicate|conflict|not changed|unchanged)"; then
    echo "(idempotent skip) ${out}"
  else
    echo "ERROR: gcloud ${cmd} failed:" >&2
    echo "${out}" >&2
    return 1
  fi
}

echo "[0/12] Enabling required APIs..."
gcloud services enable "${REQUIRED_APIS[@]}" --project "${GCP_PROJECT}"

# ---------------------------------------------------------------------------
# 1. Build and deploy the Cloud Run service.
# ---------------------------------------------------------------------------
echo "[1/12] Deploying Cloud Run service ${SERVICE_NAME}..."
_run_idempotent run deploy "${SERVICE_NAME}" \
  --source . \
  --region "${GCP_REGION}" \
  --no-allow-unauthenticated \
  --min-instances 0 \
  --max-instances 3 \
  --memory 512Mi \
  --service-account "${CLOUD_RUN_SA}"

# ---------------------------------------------------------------------------
# 2. Resolve SERVICE_URL if not provided.
# ---------------------------------------------------------------------------
if [[ -z "${SERVICE_URL:-}" ]]; then
  echo "[2/12] SERVICE_URL not set; looking up Cloud Run service URL..."
  SERVICE_URL=$(gcloud run services describe "${SERVICE_NAME}" \
    --region "${GCP_REGION}" \
    --format 'value(status.url)')
  if [[ -z "${SERVICE_URL}" ]]; then
    echo "ERROR: Could not determine service URL. Set SERVICE_URL explicitly." >&2
    exit 1
  fi
fi
# Strip any trailing slash.
SERVICE_URL="${SERVICE_URL%/}"
echo "Service URL: ${SERVICE_URL}"

# ---------------------------------------------------------------------------
# 3. Create the scheduler service account.
# ---------------------------------------------------------------------------
echo "[3/12] Ensuring scheduler/invoker service account ${SCHEDULER_SA_EMAIL}..."
_run_idempotent iam service-accounts create "${SCHEDULER_SA}" \
  --display-name "Vör Cloud Scheduler invoker"

# Grant the scheduler SA permission to invoke the Cloud Run service.
echo "Granting roles/run.invoker to ${SCHEDULER_SA_EMAIL} on ${SERVICE_NAME}..."
_run_idempotent run services add-iam-policy-binding "${SERVICE_NAME}" \
  --region "${GCP_REGION}" \
  --member "serviceAccount:${SCHEDULER_SA_EMAIL}" \
  --role "roles/run.invoker" \
  --platform managed

# ---------------------------------------------------------------------------
# 4. Cloud Tasks queue + IAM.
# ---------------------------------------------------------------------------
echo "[4/12] Ensuring Cloud Tasks queue ${TASKS_QUEUE}..."
_run_idempotent tasks queues create "${TASKS_QUEUE}" \
  --location "${GCP_REGION}" \
  --max-attempts 5 \
  --min-backoff 10s \
  --max-backoff 300s

echo "Granting roles/cloudtasks.enqueuer to ${CLOUD_RUN_SA} on ${TASKS_QUEUE}..."
_run_idempotent tasks queues add-iam-policy-binding "${TASKS_QUEUE}" \
  --location "${GCP_REGION}" \
  --member "serviceAccount:${CLOUD_RUN_SA}" \
  --role "roles/cloudtasks.enqueuer"

# ---------------------------------------------------------------------------
# 5. Cloud SQL instance + database for session persistence.
# ---------------------------------------------------------------------------
: "${SESSION_DB_INSTANCE:=vor-sessions}"
: "${SESSION_DB_NAME:=vor_sessions}"
: "${SESSION_DB_USER:=vor}"

echo "[5/12] Ensuring Cloud SQL instance ${SESSION_DB_INSTANCE} exists..."
_run_idempotent sql instances create "${SESSION_DB_INSTANCE}" \
  --database-version=POSTGRES_16 \
  --tier=db-f1-micro \
  --region="${GCP_REGION}"

_run_idempotent sql databases create "${SESSION_DB_NAME}" \
  --instance="${SESSION_DB_INSTANCE}"

if [[ -z "${SESSION_DB_PASSWORD:-}" ]]; then
  echo "ERROR: SESSION_DB_PASSWORD must be set (the vor DB user's password)." >&2
  exit 1
fi
# NOTE: --password on the command line is briefly visible via `ps` on
# whatever machine runs this script (see final-review.md C-4). Accepted
# for now -- a full fix means either an interactive password prompt
# (breaks automation) or migrating to Cloud SQL IAM database auth
# (no password at all), both out of scope for this pass.
_run_idempotent sql users create "${SESSION_DB_USER}" \
  --instance="${SESSION_DB_INSTANCE}" \
  --password="${SESSION_DB_PASSWORD}"

INSTANCE_CONNECTION_NAME=$(gcloud sql instances describe "${SESSION_DB_INSTANCE}" \
  --format='value(connectionName)')

_run_idempotent run services update "${SERVICE_NAME}" \
  --region "${GCP_REGION}" \
  --add-cloudsql-instances "${INSTANCE_CONNECTION_NAME}"

_run_idempotent projects add-iam-policy-binding "${GCP_PROJECT}" \
  --member "serviceAccount:${CLOUD_RUN_SA}" \
  --role "roles/cloudsql.client"

SESSION_DB_URL="postgresql+asyncpg://${SESSION_DB_USER}:${SESSION_DB_PASSWORD}@/${SESSION_DB_NAME}?host=/cloudsql/${INSTANCE_CONNECTION_NAME}"

# SESSION_DB_URL embeds the live DB password, so it goes into Secret
# Manager and is mounted with --set-secrets below -- NOT into
# --set-env-vars. Cloud Run env vars are plaintext and readable by anyone
# with run.services.get (via `gcloud run services describe` or the
# console). Same rule docs/DEPLOY.md already states for the scheduler
# credential: that credential goes in Secret Manager / .env, never
# hardcoded, per CLAUDE.md's secrets rule. See final-review.md C-4.
: "${SESSION_DB_URL_SECRET:=vor-session-db-url}"

if gcloud secrets describe "${SESSION_DB_URL_SECRET}" >/dev/null 2>&1; then
  printf '%s' "${SESSION_DB_URL}" | gcloud secrets versions add "${SESSION_DB_URL_SECRET}" --data-file=-
else
  printf '%s' "${SESSION_DB_URL}" | gcloud secrets create "${SESSION_DB_URL_SECRET}" \
    --data-file=- --replication-policy=automatic
fi

_run_idempotent projects add-iam-policy-binding "${GCP_PROJECT}" \
  --member "serviceAccount:${CLOUD_RUN_SA}" \
  --role "roles/secretmanager.secretAccessor"

# db-f1-micro is the smallest/cheapest Cloud SQL tier, matching this
# project's existing "scale-to-zero, cap runaway spend" cost posture
# (--min-instances 0, --max-instances 3 above) -- an unvalidated starting
# point, same posture as every other capacity default in this project.
# Revisit once real traffic volume exists.

# ---------------------------------------------------------------------------
# 6. Set environment variables on the Cloud Run service.
# ---------------------------------------------------------------------------
echo "[6/12] Updating Cloud Run environment variables..."

ENV_VARS=(
  "GCP_PROJECT=${GCP_PROJECT}"
  "TASKS_LOCATION=${GCP_REGION}"
  "TASKS_QUEUE=${TASKS_QUEUE}"
  "TASKS_OIDC_SA_EMAIL=${SCHEDULER_SA_EMAIL}"
  "SERVICE_URL=${SERVICE_URL}"
  "GOOGLE_GENAI_USE_VERTEXAI=true"
  "GOOGLE_CLOUD_PROJECT=${GCP_PROJECT}"
  "GOOGLE_CLOUD_LOCATION=${GCP_REGION}"
)

# Optional variables are only added when explicitly set.
if [[ -n "${FIRESTORE_DATABASE:-}" && "${FIRESTORE_DATABASE}" != "(default)" ]]; then
  ENV_VARS+=("FIRESTORE_DATABASE=${FIRESTORE_DATABASE}")
fi
if [[ -n "${GEMINI_MODEL:-}" ]]; then
  ENV_VARS+=("GEMINI_MODEL=${GEMINI_MODEL}")
fi
if [[ -n "${MLFLOW_TRACKING_URI:-}" ]]; then
  ENV_VARS+=("MLFLOW_TRACKING_URI=${MLFLOW_TRACKING_URI}")
fi
if [[ -n "${MLFLOW_EXPERIMENT_NAME:-}" ]]; then
  ENV_VARS+=("MLFLOW_EXPERIMENT_NAME=${MLFLOW_EXPERIMENT_NAME}")
fi
if [[ -n "${SWEEP_MAX_TARGETS:-}" ]]; then
  ENV_VARS+=("SWEEP_MAX_TARGETS=${SWEEP_MAX_TARGETS}")
fi
if [[ -n "${BLAST_RADIUS_CACHE_TTL_SECONDS:-}" ]]; then
  ENV_VARS+=("BLAST_RADIUS_CACHE_TTL_SECONDS=${BLAST_RADIUS_CACHE_TTL_SECONDS}")
fi
if [[ -n "${TRACE_REPLAY_BATCH_SIZE:-}" ]]; then
  ENV_VARS+=("TRACE_REPLAY_BATCH_SIZE=${TRACE_REPLAY_BATCH_SIZE}")
fi
if [[ -n "${PENDING_TRACE_RETENTION_DAYS:-}" ]]; then
  ENV_VARS+=("PENDING_TRACE_RETENTION_DAYS=${PENDING_TRACE_RETENTION_DAYS}")
fi

ENV_VARS_STRING=$(IFS=,; echo "${ENV_VARS[*]}")

# --set-env-vars REPLACES the service's entire env-var set; it does not
# merge. That is deliberate -- it makes this script the single source of
# truth for the service's configuration -- but it means anything set
# out-of-band with `gcloud run services update --update-env-vars` is
# removed by the next deploy. So export every variable you want (notably
# MLFLOW_TRACKING_URI and MLFLOW_EXPERIMENT_NAME) before running this,
# rather than setting them afterwards.

_run_idempotent run services update "${SERVICE_NAME}" \
  --region "${GCP_REGION}" \
  --set-env-vars "${ENV_VARS_STRING}" \
  --set-secrets "SESSION_DB_URL=${SESSION_DB_URL_SECRET}:latest"

# ---------------------------------------------------------------------------
# 7. Grant the Cloud Run service account access to Vertex AI.
# ---------------------------------------------------------------------------
echo "[7/12] Granting roles/aiplatform.user to ${CLOUD_RUN_SA}..."
_run_idempotent projects add-iam-policy-binding "${GCP_PROJECT}" \
  --member "serviceAccount:${CLOUD_RUN_SA}" \
  --role "roles/aiplatform.user"

# ---------------------------------------------------------------------------
# 8. Cloud Scheduler jobs for /sweep and /replay-traces.
# ---------------------------------------------------------------------------
echo "[8/12] Ensuring Cloud Scheduler jobs..."

_run_idempotent scheduler jobs create http "vor-weekly-sweep" \
  --location "${GCP_REGION}" \
  --schedule "0 3 * * 1" \
  --uri "${SERVICE_URL}/sweep" \
  --http-method POST \
  --oidc-service-account-email "${SCHEDULER_SA_EMAIL}" \
  --oidc-token-audience "${SERVICE_URL}"

_run_idempotent scheduler jobs create http "vor-trace-replay" \
  --location "${GCP_REGION}" \
  --schedule "*/15 * * * *" \
  --uri "${SERVICE_URL}/replay-traces" \
  --http-method POST \
  --oidc-service-account-email "${SCHEDULER_SA_EMAIL}" \
  --oidc-token-audience "${SERVICE_URL}"

# ---------------------------------------------------------------------------
# 9. Pub/Sub topic + push subscription for /classify.
# ---------------------------------------------------------------------------
echo "[9/12] Ensuring Pub/Sub topic and subscription..."
_run_idempotent pubsub topics create "${PUBSUB_TOPIC}"

_run_idempotent pubsub subscriptions create "${PUBSUB_SUBSCRIPTION}" \
  --topic "${PUBSUB_TOPIC}" \
  --push-endpoint "${SERVICE_URL}/classify" \
  --push-auth-service-account "${SCHEDULER_SA_EMAIL}" \
  --ack-deadline 600

# Whatever publishes alerts -- a real ingest pipeline, or the VM you run
# scripts/generate_events.py from -- needs pubsub.publisher on the topic.
# Previously this was only ECHOED as a manual next step with a
# placeholder service account, so the one binding needed to actually feed
# the system was the one thing the deploy script did not do.
if [[ -n "${EVENT_PUBLISHER_SA:-}" ]]; then
  echo "Granting roles/pubsub.publisher to ${EVENT_PUBLISHER_SA} on ${PUBSUB_TOPIC}..."
  _run_idempotent pubsub topics add-iam-policy-binding "${PUBSUB_TOPIC}" \
    --member "serviceAccount:${EVENT_PUBLISHER_SA}" \
    --role "roles/pubsub.publisher"
fi

# ---------------------------------------------------------------------------
# 10. Set a Firestore TTL policy on pending_traces.queued_at.
# ---------------------------------------------------------------------------
echo "[10/12] Setting TTL policy on pending_traces.expires_at..."
# expires_at, NOT queued_at. queued_at is written as an ISO string, and a
# Firestore TTL policy only acts on timestamp fields -- pointed at a
# string it silently deletes nothing, so the documented bound on
# pending_traces growth never existed. expires_at is a real timestamp,
# set to write time + PENDING_TRACE_RETENTION_DAYS (see
# vor_agents/tracing.py), because Firestore's expiry IS the field value:
# a policy on the write time would sweep the queue almost immediately.
_run_idempotent firestore fields ttls update expires_at \
  --collection-group=pending_traces \
  --enable-ttl \
  --database="${FIRESTORE_DATABASE}"

# Disable the old, inert policy so a project deployed before this fix
# does not keep a misleading TTL policy on a field nothing expires by.
gcloud firestore fields ttls update queued_at \
  --collection-group=pending_traces \
  --disable-ttl \
  --database="${FIRESTORE_DATABASE}" >/dev/null 2>&1 || true

# ---------------------------------------------------------------------------
# 11. Summary.
# ---------------------------------------------------------------------------
echo ""
echo "=== Deployment complete ==="
echo "Cloud Run service: ${SERVICE_URL}"
echo "Scheduler SA:      ${SCHEDULER_SA_EMAIL}"
echo "Cloud Run SA:      ${CLOUD_RUN_SA}"
echo ""
echo "Next steps (manual):"
echo "  1. Seed the blast-radius table:"
echo "     .venv/bin/python scripts/seed_blast_radius_table.py"
echo "  2. (Optional) Seed confirmed-negative history:"
echo "     .venv/bin/python scripts/seed_firestore.py --file history.json --dry-run"
echo "  3. (If pre-existing data lacks identity_key) Run backfill:"
echo "     .venv/bin/python scripts/backfill_identity_key.py --dry-run"
if [[ -z "${EVENT_PUBLISHER_SA:-}" ]]; then
  echo "  4. Grant pubsub.publisher to whatever publishes alerts (set"
  echo "     EVENT_PUBLISHER_SA before re-running to have this done for you):"
  echo "     gcloud pubsub topics add-iam-policy-binding ${PUBSUB_TOPIC} \\"
  echo "       --member \"serviceAccount:YOUR_PUBLISHER_SERVICE_ACCOUNT\" \\"
  echo "       --role roles/pubsub.publisher"
else
  echo "  4. (done) roles/pubsub.publisher granted to ${EVENT_PUBLISHER_SA}"
fi
echo ""
