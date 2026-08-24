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
  --set-env-vars "GCP_PROJECT=YOUR_PROJECT_ID,TASKS_LOCATION=us-central1,TASKS_QUEUE=vor-audit-queue,TASKS_OIDC_SA_EMAIL=vor-scheduler@YOUR_PROJECT_ID.iam.gserviceaccount.com,SERVICE_URL=https://YOUR_CLOUD_RUN_URL,GOOGLE_GENAI_USE_VERTEXAI=true,GOOGLE_CLOUD_PROJECT=YOUR_PROJECT_ID,GOOGLE_CLOUD_LOCATION=us-central1"
```

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

## 4. Wire /classify to a Pub/Sub push subscription

```bash
gcloud pubsub topics create vor-alerts

gcloud pubsub subscriptions create vor-alerts-sub \
  --topic vor-alerts \
  --push-endpoint "https://YOUR_CLOUD_RUN_URL/classify" \
  --push-auth-service-account "vor-scheduler@YOUR_PROJECT_ID.iam.gserviceaccount.com"
```

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

**Still open:** no dead-letter topic or `--max-delivery-attempts`
configured on `vor-alerts-sub` yet -- a permanently-malformed message
will retry and 422 until it ages out of the subscription's retention
window. Revisit once real traffic volume exists to calibrate against.
