# Deploying Vör to Cloud Run + Cloud Scheduler

None of these commands have been run yet — this is the plan, not a record
of what's live. Run them yourself when ready; none of this should execute
without you actually reviewing it first, especially the parts that create
billable resources.

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
  --set-env-vars "GCP_PROJECT=YOUR_PROJECT_ID,TASKS_LOCATION=us-central1,TASKS_QUEUE=vor-audit-queue,TASKS_OIDC_SA_EMAIL=vor-scheduler@YOUR_PROJECT_ID.iam.gserviceaccount.com,SERVICE_URL=https://YOUR_CLOUD_RUN_URL"
```

`/audit` must never be deployed with `--allow-unauthenticated`, same as
`/classify` and `/sweep` — it's reached exclusively via Cloud Tasks'
OIDC-authenticated dispatch.

## 4. The `/classify` endpoint itself

`/classify` isn't wired to a trigger source yet — that's genuinely still
open. Whatever ingests alerts (Hayabusa output, a Sigma rule webhook, a
Pub/Sub topic something else publishes to) needs to call `POST /classify`
with the alert JSON. If that source is another GCP service, grant it
`roles/run.invoker` the same way as step 2; if it's an external webhook,
front it with a Pub/Sub push subscription authenticated the same way
rather than exposing `/classify` directly.
