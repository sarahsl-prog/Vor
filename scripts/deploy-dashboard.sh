#!/usr/bin/env bash
#
# Deploy the Vör operator dashboard (dashboard/app.py) as its own Cloud Run
# service, private, behind Identity-Aware Proxy.
#
# This is NOT part of scripts/deploy.sh on purpose. The dashboard is an
# operator tool, not part of the request path, and its viewer allowlist is
# a human decision that should not live in the main deploy. See
# docs/DEPLOY.md section 6.
#
# Why the extra machinery vs. a plain `gcloud run deploy`:
#
#   * The dashboard has NO authentication of its own and renders every
#     alert, decision and reasoning trace. It must never be deployed with
#     --allow-unauthenticated. IAP is the gate; this script enables it and
#     refuses to finish without at least one viewer to grant access to.
#   * It reads MLflow at the VM's internal address, which the
#     allow-mlflow-from-vor firewall rule only admits from the vor-run
#     subnet (docs/DEPLOY.md section 5). The service is deployed onto that
#     subnet with Direct VPC egress; no firewall change is needed.
#   * Streamlit keeps per-session state in the serving process, so the
#     service runs --max-instances 1 with --session-affinity. It is
#     read-only, so one instance is not a capacity concern.
#
# Required environment variables:
#   GCP_PROJECT           -- Google Cloud project ID.
#   DASHBOARD_VIEWERS     -- Comma-separated IAM members allowed to open the
#                            dashboard, e.g.
#                            "user:a@example.com,group:ops@example.com".
#                            Each is granted roles/iap.httpsResourceAccessor
#                            on the service. The script exits if this is empty.
#
# Strongly recommended:
#   MLFLOW_TRACKING_URI   -- The tracking server the `vor` service uses,
#                            e.g. http://10.10.0.5:5000 (the MLflow VM's
#                            internal IP). Without it the Traces / Home /
#                            Pipeline pages fall back to demo data. The
#                            script warns but continues.
#   MLFLOW_EXPERIMENT_NAME -- MUST match the `vor` service exactly. A
#                            different value gives an empty Traces page,
#                            not an error.
#
# Optional environment variables:
#   GCP_REGION            -- Region for all resources. Default: us-central1.
#   SERVICE_NAME          -- Cloud Run service name. Default: vor-dashboard.
#   DASHBOARD_SA          -- Service account the dashboard runs as.
#                            Default: vor-dashboard. Granted
#                            roles/datastore.viewer and nothing else --
#                            the dashboard never writes.
#   FIRESTORE_DATABASE    -- Named Firestore database. Default: (default).
#   VPC_NETWORK           -- VPC to attach to. Default: vor-vpc.
#   VPC_SUBNET            -- Subnet for Direct VPC egress. Default: vor-run.
#                            Must be a subnet the allow-mlflow-from-vor
#                            firewall rule admits (its source range).
#   ARTIFACT_REPO         -- Artifact Registry repo for the built image.
#                            Default: cloud-run-source-deploy (the repo
#                            `gcloud run deploy --source` already creates).
#
# Usage:
#   export GCP_PROJECT=your-project-id
#   export DASHBOARD_VIEWERS="user:you@example.com"
#   export MLFLOW_TRACKING_URI=http://10.10.0.5:5000
#   export MLFLOW_EXPERIMENT_NAME=vor-prod
#   export FIRESTORE_DATABASE=vor-db
#   ./scripts/deploy-dashboard.sh
#
# Idempotent: re-running rebuilds the image and updates the service in
# place. A real gcloud failure stops the script (set -e); "already exists"
# on a re-run is absorbed.

set -euo pipefail

: "${GCP_PROJECT:?GCP_PROJECT must be set}"
: "${GCP_REGION:=us-central1}"
: "${SERVICE_NAME:=vor-dashboard}"
: "${DASHBOARD_SA:=vor-dashboard}"
: "${FIRESTORE_DATABASE:=(default)}"
: "${VPC_NETWORK:=vor-vpc}"
: "${VPC_SUBNET:=vor-run}"
: "${ARTIFACT_REPO:=cloud-run-source-deploy}"

if [[ -z "${DASHBOARD_VIEWERS:-}" ]]; then
  echo "ERROR: DASHBOARD_VIEWERS must be set (comma-separated IAM members)." >&2
  echo "  Example: export DASHBOARD_VIEWERS=\"user:you@example.com\"" >&2
  echo "  Deploying without it would leave the service reachable only by" >&2
  echo "  project owners, or -- worse, if you then \"fix\" that by hand with" >&2
  echo "  --allow-unauthenticated -- open to the internet." >&2
  exit 1
fi

DASHBOARD_SA_EMAIL="${DASHBOARD_SA}@${GCP_PROJECT}.iam.gserviceaccount.com"
IMAGE="${GCP_REGION}-docker.pkg.dev/${GCP_PROJECT}/${ARTIFACT_REPO}/${SERVICE_NAME}"

echo ""
echo "=== Vör dashboard deployment ==="
echo "Project:      ${GCP_PROJECT}"
echo "Region:       ${GCP_REGION}"
echo "Service:      ${SERVICE_NAME}"
echo "Dashboard SA: ${DASHBOARD_SA_EMAIL}"
echo "VPC:          ${VPC_NETWORK}/${VPC_SUBNET} (Direct VPC egress)"
echo "Firestore DB: ${FIRESTORE_DATABASE}"
echo "MLflow:       ${MLFLOW_TRACKING_URI:-<unset - dashboard shows demo traces>}"
echo "Viewers:      ${DASHBOARD_VIEWERS}"
echo ""

if [[ -z "${MLFLOW_TRACKING_URI:-}" ]]; then
  echo "WARNING: MLFLOW_TRACKING_URI is not set." >&2
  echo "  The Traces / Home / Pipeline pages will show demo data, not live" >&2
  echo "  traces. Point it at the same tracking server the 'vor' service" >&2
  echo "  uses (the MLflow VM's internal IP, e.g. http://10.10.0.5:5000)." >&2
  echo "" >&2
fi
if [[ -n "${MLFLOW_TRACKING_URI:-}" && -z "${MLFLOW_EXPERIMENT_NAME:-}" ]]; then
  echo "WARNING: MLFLOW_EXPERIMENT_NAME is not set while MLFLOW_TRACKING_URI is." >&2
  echo "  The dashboard will read MLflow's 'Default' experiment. If the 'vor'" >&2
  echo "  service logs to a named experiment, the Traces page will be empty." >&2
  echo "" >&2
fi

# ---------------------------------------------------------------------------
# Helper: run gcloud, absorb "already exists" style errors, fail on the rest.
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
# 0. APIs.
# ---------------------------------------------------------------------------
echo "[0/5] Enabling required APIs..."
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  iap.googleapis.com \
  compute.googleapis.com \
  firestore.googleapis.com \
  --project "${GCP_PROJECT}"

# ---------------------------------------------------------------------------
# 1. Read-only service account.
# ---------------------------------------------------------------------------
echo "[1/5] Ensuring dashboard service account ${DASHBOARD_SA_EMAIL}..."
_run_idempotent iam service-accounts create "${DASHBOARD_SA}" \
  --project "${GCP_PROJECT}" \
  --display-name "Vör dashboard (read-only)"

# datastore.viewer and nothing else -- the dashboard never writes, and
# MLflow is network-gated rather than IAM-gated so needs no grant.
_run_idempotent projects add-iam-policy-binding "${GCP_PROJECT}" \
  --member "serviceAccount:${DASHBOARD_SA_EMAIL}" \
  --role "roles/datastore.viewer"

# ---------------------------------------------------------------------------
# 2. Build the image from Dockerfile.dashboard.
# ---------------------------------------------------------------------------
echo "[2/5] Building ${IMAGE}..."
gcloud builds submit \
  --project "${GCP_PROJECT}" \
  --config cloudbuild.dashboard.yaml \
  --substitutions "_IMAGE=${IMAGE}"

# ---------------------------------------------------------------------------
# 3. Deploy -- private, on the VPC, single instance with session affinity.
# ---------------------------------------------------------------------------
echo "[3/5] Deploying Cloud Run service ${SERVICE_NAME}..."

ENV_VARS=("STREAMLIT_SERVER_HEADLESS=true")
if [[ -n "${FIRESTORE_DATABASE:-}" && "${FIRESTORE_DATABASE}" != "(default)" ]]; then
  ENV_VARS+=("FIRESTORE_DATABASE=${FIRESTORE_DATABASE}")
fi
if [[ -n "${MLFLOW_TRACKING_URI:-}" ]]; then
  ENV_VARS+=("MLFLOW_TRACKING_URI=${MLFLOW_TRACKING_URI}")
fi
if [[ -n "${MLFLOW_EXPERIMENT_NAME:-}" ]]; then
  ENV_VARS+=("MLFLOW_EXPERIMENT_NAME=${MLFLOW_EXPERIMENT_NAME}")
fi
ENV_VARS_STRING=$(IFS=,; echo "${ENV_VARS[*]}")

_run_idempotent run deploy "${SERVICE_NAME}" \
  --project "${GCP_PROJECT}" \
  --region "${GCP_REGION}" \
  --image "${IMAGE}" \
  --no-allow-unauthenticated \
  --service-account "${DASHBOARD_SA_EMAIL}" \
  --network "${VPC_NETWORK}" \
  --subnet "${VPC_SUBNET}" \
  --vpc-egress private-ranges-only \
  --min-instances 0 \
  --max-instances 1 \
  --session-affinity \
  --memory 512Mi \
  --set-env-vars "${ENV_VARS_STRING}"

# ---------------------------------------------------------------------------
# 4. Enable IAP on the service.
# ---------------------------------------------------------------------------
echo "[4/5] Enabling Identity-Aware Proxy on ${SERVICE_NAME}..."
# The first --iap enable on a project may require the OAuth consent screen
# (brand) to be configured once. If this step fails asking for a brand,
# create it in the console (APIs & Services > OAuth consent screen) or:
#   gcloud iap oauth-brands create --application_title=Vor --support_email=YOU
# then re-run this script.
_run_idempotent beta run services update "${SERVICE_NAME}" \
  --project "${GCP_PROJECT}" \
  --region "${GCP_REGION}" \
  --iap

# ---------------------------------------------------------------------------
# 5. Grant each viewer access through IAP.
# ---------------------------------------------------------------------------
echo "[5/5] Granting roles/iap.httpsResourceAccessor to viewers..."
IFS=',' read -ra VIEWERS <<< "${DASHBOARD_VIEWERS}"
for member in "${VIEWERS[@]}"; do
  member="$(echo "${member}" | xargs)"  # trim whitespace
  [[ -z "${member}" ]] && continue
  echo "  - ${member}"
  _run_idempotent run services add-iam-policy-binding "${SERVICE_NAME}" \
    --project "${GCP_PROJECT}" \
    --region "${GCP_REGION}" \
    --member "${member}" \
    --role "roles/iap.httpsResourceAccessor"
done

SERVICE_URL=$(gcloud run services describe "${SERVICE_NAME}" \
  --project "${GCP_PROJECT}" --region "${GCP_REGION}" \
  --format 'value(status.url)' 2>/dev/null || true)

echo ""
echo "=== Dashboard deployment complete ==="
echo "URL:     ${SERVICE_URL:-<run: gcloud run services describe ${SERVICE_NAME}>}"
echo "Auth:    IAP -- only the members listed above can open it."
echo ""
echo "To add or remove a viewer later:"
echo "  gcloud run services add-iam-policy-binding ${SERVICE_NAME} \\"
echo "    --region ${GCP_REGION} --member user:NAME@example.com \\"
echo "    --role roles/iap.httpsResourceAccessor"
echo "  gcloud run services remove-iam-policy-binding ${SERVICE_NAME} \\"
echo "    --region ${GCP_REGION} --member user:NAME@example.com \\"
echo "    --role roles/iap.httpsResourceAccessor"
echo ""
echo "Tear down with: ./scripts/deploy-dashboard-cleanup.sh"
echo ""
