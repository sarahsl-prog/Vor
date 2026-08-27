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
#   MLFLOW_TRACKING_URI  -- Optional external MLflow tracking server.
#   MLFLOW_EXPERIMENT_NAME -- Optional MLflow experiment name.
#   SWEEP_MAX_TARGETS    -- Optional sweep target cap.
#   BLAST_RADIUS_CACHE_TTL_SECONDS -- Optional blast-radius cache TTL.
#   SESSION_DB_INSTANCE  -- Cloud SQL instance name for session persistence.
#                           Default: vor-sessions.
#   SESSION_DB_NAME      -- Cloud SQL database name. Default: vor_sessions.
#   SESSION_DB_USER      -- Cloud SQL database user. Default: vor.
#   SESSION_DB_PASSWORD  -- Password for SESSION_DB_USER. No default --
#                           the script exits if this isn't set.
#
# Usage:
#   export GCP_PROJECT=your-project-id
#   export GCP_REGION=us-central1
#   export SERVICE_URL=https://vor-xyz-uc.a.run.app   # optional if service exists
#   ./scripts/deploy.sh
#
# The script exits non-zero on any unhandled gcloud error. Idempotent re-runs
# are safe: gcloud "create" commands that fail with "already exists" are
# handled, and "update" commands overwrite previous settings.

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

echo ""
echo "=== Vör deployment ==="
echo "Project:        ${GCP_PROJECT}"
echo "Region:         ${GCP_REGION}"
echo "Cloud Run SA:   ${CLOUD_RUN_SA}"
echo "Scheduler SA:   ${SCHEDULER_SA_EMAIL}"
echo "Firestore DB:   ${FIRESTORE_DATABASE}"
echo ""

# ---------------------------------------------------------------------------
# Helper: run gcloud and swallow "already exists" / "already has binding" errors.
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# 1. Build and deploy the Cloud Run service.
# ---------------------------------------------------------------------------
echo "[1/11] Deploying Cloud Run service ${SERVICE_NAME}..."
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
  echo "[2/11] SERVICE_URL not set; looking up Cloud Run service URL..."
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
echo "[3/11] Ensuring scheduler/invoker service account ${SCHEDULER_SA_EMAIL}..."
_run_idempotent iam service-accounts create "${SCHEDULER_SA}" \
  --display-name "Vör Cloud Scheduler invoker" || true

# Grant the scheduler SA permission to invoke the Cloud Run service.
echo "Granting roles/run.invoker to ${SCHEDULER_SA_EMAIL} on ${SERVICE_NAME}..."
_run_idempotent run services add-iam-policy-binding "${SERVICE_NAME}" \
  --region "${GCP_REGION}" \
  --member "serviceAccount:${SCHEDULER_SA_EMAIL}" \
  --role "roles/run.invoker" \
  --platform managed || true

# ---------------------------------------------------------------------------
# 4. Cloud Tasks queue + IAM.
# ---------------------------------------------------------------------------
echo "[4/11] Ensuring Cloud Tasks queue ${TASKS_QUEUE}..."
_run_idempotent tasks queues create "${TASKS_QUEUE}" \
  --location "${GCP_REGION}" \
  --max-attempts 5 \
  --min-backoff 10s \
  --max-backoff 300s || true

echo "Granting roles/cloudtasks.enqueuer to ${CLOUD_RUN_SA} on ${TASKS_QUEUE}..."
_run_idempotent tasks queues add-iam-policy-binding "${TASKS_QUEUE}" \
  --location "${GCP_REGION}" \
  --member "serviceAccount:${CLOUD_RUN_SA}" \
  --role "roles/cloudtasks.enqueuer" || true

# ---------------------------------------------------------------------------
# 5. Cloud SQL instance + database for session persistence.
# ---------------------------------------------------------------------------
: "${SESSION_DB_INSTANCE:=vor-sessions}"
: "${SESSION_DB_NAME:=vor_sessions}"
: "${SESSION_DB_USER:=vor}"

echo "[5/11] Ensuring Cloud SQL instance ${SESSION_DB_INSTANCE} exists..."
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

# db-f1-micro is the smallest/cheapest Cloud SQL tier, matching this
# project's existing "scale-to-zero, cap runaway spend" cost posture
# (--min-instances 0, --max-instances 3 above) -- an unvalidated starting
# point, same posture as every other capacity default in this project.
# Revisit once real traffic volume exists.

# ---------------------------------------------------------------------------
# 6. Set environment variables on the Cloud Run service.
# ---------------------------------------------------------------------------
echo "[6/11] Updating Cloud Run environment variables..."

ENV_VARS=(
  "GCP_PROJECT=${GCP_PROJECT}"
  "TASKS_LOCATION=${GCP_REGION}"
  "TASKS_QUEUE=${TASKS_QUEUE}"
  "TASKS_OIDC_SA_EMAIL=${SCHEDULER_SA_EMAIL}"
  "SERVICE_URL=${SERVICE_URL}"
  "GOOGLE_GENAI_USE_VERTEXAI=true"
  "GOOGLE_CLOUD_PROJECT=${GCP_PROJECT}"
  "GOOGLE_CLOUD_LOCATION=${GCP_REGION}"
  "SESSION_DB_URL=${SESSION_DB_URL}"
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

ENV_VARS_STRING=$(IFS=,; echo "${ENV_VARS[*]}")

_run_idempotent run services update "${SERVICE_NAME}" \
  --region "${GCP_REGION}" \
  --set-env-vars "${ENV_VARS_STRING}" || true

# ---------------------------------------------------------------------------
# 7. Grant the Cloud Run service account access to Vertex AI.
# ---------------------------------------------------------------------------
echo "[7/11] Granting roles/aiplatform.user to ${CLOUD_RUN_SA}..."
_run_idempotent projects add-iam-policy-binding "${GCP_PROJECT}" \
  --member "serviceAccount:${CLOUD_RUN_SA}" \
  --role "roles/aiplatform.user" || true

# ---------------------------------------------------------------------------
# 8. Cloud Scheduler jobs for /sweep and /replay-traces.
# ---------------------------------------------------------------------------
echo "[8/11] Ensuring Cloud Scheduler jobs..."

_run_idempotent scheduler jobs create http "vor-weekly-sweep" \
  --location "${GCP_REGION}" \
  --schedule "0 3 * * 1" \
  --uri "${SERVICE_URL}/sweep" \
  --http-method POST \
  --oidc-service-account-email "${SCHEDULER_SA_EMAIL}" \
  --oidc-token-audience "${SERVICE_URL}" || true

_run_idempotent scheduler jobs create http "vor-trace-replay" \
  --location "${GCP_REGION}" \
  --schedule "*/15 * * * *" \
  --uri "${SERVICE_URL}/replay-traces" \
  --http-method POST \
  --oidc-service-account-email "${SCHEDULER_SA_EMAIL}" \
  --oidc-token-audience "${SERVICE_URL}" || true

# ---------------------------------------------------------------------------
# 9. Pub/Sub topic + push subscription for /classify.
# ---------------------------------------------------------------------------
echo "[9/11] Ensuring Pub/Sub topic and subscription..."
_run_idempotent pubsub topics create "${PUBSUB_TOPIC}" || true

_run_idempotent pubsub subscriptions create "${PUBSUB_SUBSCRIPTION}" \
  --topic "${PUBSUB_TOPIC}" \
  --push-endpoint "${SERVICE_URL}/classify" \
  --push-auth-service-account "${SCHEDULER_SA_EMAIL}" \
  --ack-deadline 600 || true

# ---------------------------------------------------------------------------
# 10. Set a Firestore TTL policy on pending_traces.queued_at.
# ---------------------------------------------------------------------------
echo "[10/11] Setting TTL policy on pending_traces.queued_at..."
_run_idempotent firestore fields ttls update queued_at \
  --collection-group=pending_traces \
  --enable-ttl \
  --database="${FIRESTORE_DATABASE}"

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
echo "  4. Grant pubsub.publisher to your ingest pipeline:"
echo "     gcloud pubsub topics add-iam-policy-binding ${PUBSUB_TOPIC} \\"
echo "       --member \"serviceAccount:YOUR_INGEST_SOURCE_SERVICE_ACCOUNT\" \\"
echo "       --role roles/pubsub.publisher"
echo ""
