# Vör — Testing Pipeline Guide

How to generate synthetic alert data, seed it into Firestore, and send alerts
to the `/classify` endpoint — both directly and through the production
Pub/Sub path.

This pairs with:

- `docs/DATASET_RUNBOOK.md` — what the 6 synthetic cases mean.
- `docs/DEPLOY.md` — deploying the service and wiring Pub/Sub.
- `scripts/seed_firestore.py` — seeding confirmed-negative history.
- `scripts/seed_blast_radius_table.py` — seeding the risk table.

---

## Before you classify anything

Vör will not autonomously suppress an alert unless the pattern already has
enough confirmed-negative history. So the pipeline is always:

1. **Seed history** into Firestore.
2. **Send an alert** that matches (or deviates from) that history.
3. **Inspect the decision** in the response, logs, or Firestore traces.

If you skip step 1, every pattern returns `UNCERTAIN` with reason
`no_history`, which is the safe default but not a useful test of the
classification path.

---

## 1. Seed required tables

### Blast-radius table

Run once per project:

```bash
.venv/bin/python scripts/seed_blast_radius_table.py
```

Without this, `estimate_blast_radius()` falls back to `UNSCORED_DEFAULT`
(`HIGH`) for every alert. That is safe, but it hides the blast-radius
priority logic from your test.

### Confirmed-negative history

Choose one of the two supported paths.

#### Synthetic case (quick demo)

```bash
.venv/bin/python scripts/seed_firestore.py --case seeded_confirmed --dry-run
.venv/bin/python scripts/seed_firestore.py --case seeded_confirmed
```

This creates a `confirmed` pattern for:

- `detection_rule_id`: `SharePoint_ToolPane_Rule`
- `parent_image`: `w3wp.exe`
- `child_image`: `csc.exe`
- `endpoint_family`: `ToolPane_admin`

The dry-run prints the tier the batch would land at. Always dry-run first,
especially with real data.

#### Real history JSON (production path)

```bash
.venv/bin/python scripts/seed_firestore.py --file history.json --dry-run
.venv/bin/python scripts/seed_firestore.py --file history.json
```

`history.json` must be a JSON list of alert objects, each carrying the four
identity fields and all five `DIFFABLE_FIELDS`:

```json
[
  {
    "detection_rule_id": "SharePoint_ToolPane_Rule",
    "parent_image": "w3wp.exe",
    "child_image": "csc.exe",
    "endpoint_family": "ToolPane_admin",
    "auth_method_present": true,
    "session_cookie_present": true,
    "integrity_level": "Medium",
    "file_access_mode": "read",
    "egress_follows_access": false,
    "host": "SRV-SP-01",
    "user": "CONTOSO\\jsmith",
    "timestamp": "2026-08-26T09:00:00Z"
  }
]
```

Instances are grouped by identity key automatically, so one file may
contain many patterns.

---

## 2. Generate a probe alert

The same `vor_agents/datasets.py` module that builds synthetic history can
build a matching (or deviating) alert to classify.

### From Python

```python
from vor_agents.datasets import DatasetCase, generate_case
import json

case = generate_case(DatasetCase.SEEDED_CONFIRMED, seed=0)

# History to seed
history = case["instances"]

# Alert to classify
probe = case["probe_alert"]

print("=== history (save to history.json) ===")
print(json.dumps(history, indent=2))

print("=== probe (send to /classify) ===")
print(json.dumps(probe, indent=2))

print("=== expected outcome ===")
print(case["expected_outcome"])
```

### Useful cases for testing

| Case | What to expect | Why use it |
|---|---|---|
| `seeded_confirmed` | `SUPPRESS` allowed | Baseline happy path. |
| `identity_drift` | `UNCERTAIN` (`no_history`) | Different identity key; tests key rejection. |
| `field_deviation` | `ESCALATE` | Same key, every diffable field wrong; tests deterministic override. |
| `low_diversity` | `UNCERTAIN` (`graduation_pending`) | Count passes but diversity fails. |
| `insufficient_history` | `UNCERTAIN` (`graduation_pending`) | Too few instances. |

Save the generated `history` list to `history.json` and seed it, then send
the `probe_alert`.

---

## 3. Send the alert to `/classify`

### Option A: Direct HTTP call

Useful for local testing or one-off manual checks against the deployed
service. The endpoint is IAM-protected, so you need an identity token.

```bash
export SERVICE_URL=https://vor-xyz-uc.a.run.app
export TOKEN=$(gcloud auth print-identity-token)

curl -X POST "${SERVICE_URL}/classify" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "detection_rule_id": "SharePoint_ToolPane_Rule",
    "parent_image": "w3wp.exe",
    "child_image": "csc.exe",
    "endpoint_family": "ToolPane_admin",
    "auth_method_present": true,
    "session_cookie_present": true,
    "integrity_level": "Medium",
    "file_access_mode": "read",
    "egress_follows_access": false,
    "host": "SRV-SP-01",
    "user": "CONTOSO\\jsmith",
    "timestamp": "2026-08-26T09:00:00Z"
  }'
```

Expected response shape:

```json
{
  "decision": "SUPPRESS",
  "matched_pattern_id": null,
  "uncertain_reason": "not_applicable",
  "structural_deviations_found": [],
  "reasoning": "..."
}
```

A `SUPPRESS` decision will enqueue an audit task onto Cloud Tasks. If the
enqueue fails, the response still returns `SUPPRESS`; check the Cloud Run
logs for the failure reason.

### Option B: Pub/Sub (production path)

This is how real ingest pipelines should send alerts. The alert body is
base64-encoded and published to the `vor-alerts` topic; the push
subscription delivers it to `/classify`.

```bash
export GCP_PROJECT=your-project-id
export PUBSUB_TOPIC=vor-alerts

ALERT='{
  "detection_rule_id": "SharePoint_ToolPane_Rule",
  "parent_image": "w3wp.exe",
  "child_image": "csc.exe",
  "endpoint_family": "ToolPane_admin",
  "auth_method_present": true,
  "session_cookie_present": true,
  "integrity_level": "Medium",
  "file_access_mode": "read",
  "egress_follows_access": false,
  "host": "SRV-SP-01",
  "user": "CONTOSO\\jsmith",
  "timestamp": "2026-08-26T09:00:00Z"
}'

gcloud pubsub topics publish "${PUBSUB_TOPIC}" \
  --message="$(echo "${ALERT}" | base64 -w0)" \
  --project "${GCP_PROJECT}"
```

Pub/Sub hides the HTTP response from you, so inspect the result in Cloud
Run logs or in the `confidence_docs` / `pending_traces` Firestore
collections instead.

---

## 4. Local smoke test without GCP

If you only want to validate the request shape and endpoint wiring, run
the service locally with `uvicorn`:

```bash
# Terminal 1
.venv/bin/uvicorn main:app --reload --port 8080

# Terminal 2
curl -X POST http://localhost:8080/classify \
  -H "Content-Type: application/json" \
  -d @probe_alert.json
```

Caveats:

- Local mode still needs a Firestore client. Use the Firestore emulator or
  point `GOOGLE_CLOUD_PROJECT` at a real project.
- Real model calls need Vertex AI credentials. Without them,
  `classify_alert()` catches the failure and degrades the decision to
  `UNCERTAIN` — that is the intended behavior, but it means you are testing
  the degradation path, not the model path.
- The Cloud Tasks enqueue path needs a real `GCP_PROJECT` / `TASKS_QUEUE`
  or the enqueue is silently swallowed.

---

## 5. Inspect results

### Cloud Run logs

```bash
gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=vor" \
  --project your-project-id \
  --limit 50 \
  --format json
```

Look for:

- `decision=SUPPRESS` / `ESCALATE` / `UNCERTAIN`
- `overrides_fired` (e.g. `ground_truth_missed`, `self_consistency_deviation`)
- `Audit enqueue failed` if the task queue is misconfigured

### Firestore traces

If MLflow is reachable, traces land there. If not, they land in the
`pending_traces` collection and are replayed every 15 minutes by the
`vor-trace-replay` scheduler job.

### Pub/Sub metrics

```bash
gcloud pubsub subscriptions describe vor-alerts-sub --project your-project-id
```

Check `pushConfig`, `ackDeadlineSeconds`, and subscription message counts
to confirm pushes are reaching the endpoint.

---

## 6. End-to-end test checklist

After running `scripts/deploy.sh`:

- [ ] `scripts/seed_blast_radius_table.py` ran successfully.
- [ ] `scripts/seed_firestore.py --case seeded_confirmed` created a
      `confirmed` pattern.
- [ ] A matching alert returns `SUPPRESS` when sent directly to `/classify`.
- [ ] The same alert published to Pub/Sub produces a `SUPPRESS` decision
      (verified via logs).
- [ ] A `field_deviation` probe returns `ESCALATE` and the response reason
      mentions the deterministic override.
- [ ] A `SUPPRESS` decision enqueues one Cloud Tasks audit.
- [ ] `pending_traces` stays empty when MLflow is reachable, or grows and
      drains when MLflow is down.

---

## 7. Common mistakes

- **Sending a probe before seeding history.** Every pattern starts as
  `NO_HISTORY` and returns `UNCERTAIN`.
- **Using a mismatched identity key.** For example, changing `child_image`
  creates a different pattern entirely; the seeded history no longer
  applies.
- **Forgetting the identity token.** `/classify` is not public; it needs
  a valid OIDC or gcloud identity token.
- **Publishing raw JSON to Pub/Sub.** Pub/Sub push encodes `message.data`
  as base64; the raw alert must be base64-encoded before publish.
- **Expecting the response body from Pub/Sub.** Push subscriptions do not
  return the HTTP response to the publisher; check logs and Firestore.

---

*See also: `docs/DATASET_RUNBOOK.md` for case definitions and
`docs/DEPLOY.md` for production wiring.*
