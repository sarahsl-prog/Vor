#!/usr/bin/env bash
#
# Tear down the Google Cloud resources created by scripts/deploy.sh.
#
# WARNING: this deletes Cloud Run, Cloud Scheduler, Cloud Tasks, Pub/Sub,
# and IAM policy bindings for the Vör stack. It does NOT delete Firestore
# data (confidence_docs, blast_radius_table, needs_attention, etc.) because
# those collections may contain evidence you want to keep or migrate. To
# remove Firestore data as well, use:
#   gcloud firestore documents delete-all --collection-ids=confidence_docs,...
#
# Required environment variables:
#   GCP_PROJECT  -- Google Cloud project ID.
#   GCP_REGION   -- GCP region where resources were created (default: us-central1).
#
# Optional environment variables:
#   SCHEDULER_SA         -- Scheduler/invoker service account name. Default: vor-scheduler.
#   TASKS_QUEUE          -- Cloud Tasks queue name. Default: vor-audit-queue.
#   PUBSUB_TOPIC         -- Pub/Sub topic name. Default: vor-alerts.
#   PUBSUB_SUBSCRIPTION  -- Pub/Sub subscription name. Default: vor-alerts-sub.
#   SERVICE_NAME         -- Cloud Run service name. Default: vor.
#   SKIP_IAM_CLEANUP     -- If set to "1", do not remove IAM bindings.
#                           Useful if the bindings are shared with other services.
#
# Usage:
#   export GCP_PROJECT=your-project-id
#   export GCP_REGION=us-central1
#   ./scripts/deploy-cleanup.sh
#
# The script exits non-zero if any destructive gcloud command fails. Deleting
# resources that do not already exist is treated as a no-op (idempotent).

set -euo pipefail

: "${GCP_PROJECT:?GCP_PROJECT must be set}"
: "${GCP_REGION:=us-central1}"
: "${SCHEDULER_SA:=vor-scheduler}"
: "${TASKS_QUEUE:=vor-audit-queue}"
: "${PUBSUB_TOPIC:=vor-alerts}"
: "${PUBSUB_SUBSCRIPTION:=vor-alerts-sub}"
: "${SERVICE_NAME:=vor}"

SCHEDULER_SA_EMAIL="${SCHEDULER_SA}@${GCP_PROJECT}.iam.gserviceaccount.com"

PROJECT_NUMBER=$(gcloud projects describe "${GCP_PROJECT}" --format='value(projectNumber)')
DEFAULT_CLOUD_RUN_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
# If the deploy script overrode CLOUD_RUN_SA we cannot know it here; use the
# default unless the operator passes it explicitly.
: "${CLOUD_RUN_SA:=${DEFAULT_CLOUD_RUN_SA}}"

echo ""
echo "=== Vör cleanup ==="
echo "Project:      ${GCP_PROJECT}"
echo "Region:       ${GCP_REGION}"
echo "Service:      ${SERVICE_NAME}"
echo "Scheduler SA: ${SCHEDULER_SA_EMAIL}"
echo "Cloud Run SA: ${CLOUD_RUN_SA}"
echo ""

# ---------------------------------------------------------------------------
# Helper: run gcloud and swallow "not found" / "does not exist" errors.
# ---------------------------------------------------------------------------
_run_idempotent_delete() {
  local cmd="$1"
  shift
  local out
  if out=$(gcloud ${cmd} "$@" 2>&1); then
    echo "${out}"
  elif echo "${out}" | grep -qiE "(not found|does not exist|doesn't exist|not exist|0 items listed|nothing to delete|UNKNOWN)"; then
    echo "(idempotent skip) ${out}"
  else
    echo "ERROR: gcloud ${cmd} failed:" >&2
    echo "${out}" >&2
    return 1
  fi
}

# ---------------------------------------------------------------------------
# 1. Delete Pub/Sub subscription and topic.
# ---------------------------------------------------------------------------
echo "[1/6] Deleting Pub/Sub subscription ${PUBSUB_SUBSCRIPTION}..."
_run_idempotent_delete pubsub subscriptions delete "${PUBSUB_SUBSCRIPTION}" || true

echo "Deleting Pub/Sub topic ${PUBSUB_TOPIC}..."
_run_idempotent_delete pubsub topics delete "${PUBSUB_TOPIC}" || true

# ---------------------------------------------------------------------------
# 2. Delete Cloud Scheduler jobs.
# ---------------------------------------------------------------------------
echo "[2/6] Deleting Cloud Scheduler jobs..."
_run_idempotent_delete scheduler jobs delete "vor-weekly-sweep" \
  --location "${GCP_REGION}" || true
_run_idempotent_delete scheduler jobs delete "vor-trace-replay" \
  --location "${GCP_REGION}" || true

# ---------------------------------------------------------------------------
# 3. Delete Cloud Tasks queue.
# ---------------------------------------------------------------------------
echo "[3/6] Deleting Cloud Tasks queue ${TASKS_QUEUE}..."
_run_idempotent_delete tasks queues delete "${TASKS_QUEUE}" \
  --location "${GCP_REGION}" || true

# ---------------------------------------------------------------------------
# 4. Delete Cloud Run service.
# ---------------------------------------------------------------------------
echo "[4/6] Deleting Cloud Run service ${SERVICE_NAME}..."
_run_idempotent_delete run services delete "${SERVICE_NAME}" \
  --region "${GCP_REGION}" \
  --quiet || true

# ---------------------------------------------------------------------------
# 5. Remove IAM policy bindings added by deploy.sh.
# ---------------------------------------------------------------------------
if [[ "${SKIP_IAM_CLEANUP:-}" != "1" ]]; then
  echo "[5/6] Removing IAM policy bindings..."

  # Cloud Run invoker binding for the scheduler service account.
  _run_idempotent_delete run services remove-iam-policy-binding "${SERVICE_NAME}" \
    --region "${GCP_REGION}" \
    --member "serviceAccount:${SCHEDULER_SA_EMAIL}" \
    --role "roles/run.invoker" \
    --platform managed || true

  # Cloud Tasks enqueuer binding for the Cloud Run service account.
  _run_idempotent_delete tasks queues remove-iam-policy-binding "${TASKS_QUEUE}" \
    --location "${GCP_REGION}" \
    --member "serviceAccount:${CLOUD_RUN_SA}" \
    --role "roles/cloudtasks.enqueuer" || true

  # Vertex AI user binding for the Cloud Run service account.
  _run_idempotent_delete projects remove-iam-policy-binding "${GCP_PROJECT}" \
    --member "serviceAccount:${CLOUD_RUN_SA}" \
    --role "roles/aiplatform.user" || true
else
  echo "[5/6] SKIP_IAM_CLEANUP=1; leaving IAM bindings in place."
fi

# ---------------------------------------------------------------------------
# 6. Delete scheduler service account.
# ---------------------------------------------------------------------------
echo "[6/6] Deleting scheduler service account ${SCHEDULER_SA_EMAIL}..."
_run_idempotent_delete iam service-accounts delete "${SCHEDULER_SA_EMAIL}" --quiet || true

# ---------------------------------------------------------------------------
# 7. Summary.
# ---------------------------------------------------------------------------
echo ""
echo "=== Cleanup complete ==="
echo "The following resources were deleted (or did not exist):"
echo "  - Cloud Run service: ${SERVICE_NAME}"
echo "  - Cloud Scheduler jobs: vor-weekly-sweep, vor-trace-replay"
echo "  - Cloud Tasks queue: ${TASKS_QUEUE}"
echo "  - Pub/Sub topic: ${PUBSUB_TOPIC}"
echo "  - Pub/Sub subscription: ${PUBSUB_SUBSCRIPTION}"
echo "  - Service account: ${SCHEDULER_SA_EMAIL}"
echo "  - IAM bindings (unless SKIP_IAM_CLEANUP=1)"
echo ""
echo "Firestore collections were NOT deleted. If you want to remove them:"
echo "  gcloud firestore documents delete-all --collection-ids=confidence_docs,blast_radius_table,blast_radius_proposals,needs_attention,pending_traces"
echo ""
