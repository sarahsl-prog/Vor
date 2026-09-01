#!/usr/bin/env bash
#
# Tear down the resources created by scripts/deploy-dashboard.sh:
# the vor-dashboard Cloud Run service, its read-only service account, and
# the datastore.viewer binding. Does NOT touch the main `vor` stack, the
# VPC, the firewall, or any Firestore / MLflow data.
#
# Required environment variables:
#   GCP_PROJECT  -- Google Cloud project ID.
#
# Optional environment variables:
#   GCP_REGION        -- Region resources were created in. Default: us-central1.
#   SERVICE_NAME      -- Cloud Run service name. Default: vor-dashboard.
#   DASHBOARD_SA      -- Dashboard service account name. Default: vor-dashboard.
#   ARTIFACT_REPO     -- Artifact Registry repo holding the image.
#                        Default: cloud-run-source-deploy.
#   KEEP_IMAGE        -- If "1", leave the built container image in place.
#   SKIP_IAM_CLEANUP  -- If "1", leave the datastore.viewer binding.
#
# Usage:
#   export GCP_PROJECT=your-project-id
#   ./scripts/deploy-dashboard-cleanup.sh
#
# Deleting something that does not exist is a no-op (idempotent).

set -euo pipefail

: "${GCP_PROJECT:?GCP_PROJECT must be set}"
: "${GCP_REGION:=us-central1}"
: "${SERVICE_NAME:=vor-dashboard}"
: "${DASHBOARD_SA:=vor-dashboard}"
: "${ARTIFACT_REPO:=cloud-run-source-deploy}"

DASHBOARD_SA_EMAIL="${DASHBOARD_SA}@${GCP_PROJECT}.iam.gserviceaccount.com"
IMAGE="${GCP_REGION}-docker.pkg.dev/${GCP_PROJECT}/${ARTIFACT_REPO}/${SERVICE_NAME}"

echo ""
echo "=== Vör dashboard cleanup ==="
echo "Project: ${GCP_PROJECT}"
echo "Region:  ${GCP_REGION}"
echo "Service: ${SERVICE_NAME}"
echo ""

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
# 1. Delete the Cloud Run service. This also removes its IAP config and the
#    per-viewer iap.httpsResourceAccessor bindings attached to it.
# ---------------------------------------------------------------------------
echo "[1/4] Deleting Cloud Run service ${SERVICE_NAME}..."
_run_idempotent_delete run services delete "${SERVICE_NAME}" \
  --project "${GCP_PROJECT}" \
  --region "${GCP_REGION}" \
  --quiet || true

# ---------------------------------------------------------------------------
# 2. Remove the project-level datastore.viewer binding.
# ---------------------------------------------------------------------------
if [[ "${SKIP_IAM_CLEANUP:-}" != "1" ]]; then
  echo "[2/4] Removing roles/datastore.viewer from ${DASHBOARD_SA_EMAIL}..."
  _run_idempotent_delete projects remove-iam-policy-binding "${GCP_PROJECT}" \
    --member "serviceAccount:${DASHBOARD_SA_EMAIL}" \
    --role "roles/datastore.viewer" || true
else
  echo "[2/4] SKIP_IAM_CLEANUP=1; leaving datastore.viewer binding in place."
fi

# ---------------------------------------------------------------------------
# 3. Delete the service account.
# ---------------------------------------------------------------------------
echo "[3/4] Deleting service account ${DASHBOARD_SA_EMAIL}..."
_run_idempotent_delete iam service-accounts delete "${DASHBOARD_SA_EMAIL}" \
  --project "${GCP_PROJECT}" --quiet || true

# ---------------------------------------------------------------------------
# 4. Delete the built image.
# ---------------------------------------------------------------------------
if [[ "${KEEP_IMAGE:-}" != "1" ]]; then
  echo "[4/4] Deleting image ${IMAGE}..."
  _run_idempotent_delete artifacts docker images delete "${IMAGE}" \
    --project "${GCP_PROJECT}" --delete-tags --quiet || true
else
  echo "[4/4] KEEP_IMAGE=1; leaving ${IMAGE} in place."
fi

echo ""
echo "=== Dashboard cleanup complete ==="
echo "The main 'vor' service, VPC, firewall and all data were left untouched."
echo ""
