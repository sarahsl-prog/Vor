# Vör — "Trust, audited."

Vör is a self-tuning confidence layer for Windows Event Log / Hayabusa-style
alert triage. It decides when it is safe to autonomously suppress a
known-benign alert and when to escalate to a human — without letting that
trust go stale or unnoticed.

A **classifier** agent makes the call. A separate **auditor** agent
periodically re-checks past suppressions against the evidence behind them,
with the authority to downgrade trust on its own but never to grant it
without a human signing off.

---

## How it works

An alert arrives at `POST /classify`. Before any model is involved, Vör
builds a **pattern identity key** — `(detection_rule_id, parent_image,
child_image, endpoint_family)` — and looks up what it already knows about
that pattern in Firestore. The classifier is then asked to compare the alert
against that history and return `SUPPRESS`, `ESCALATE`, or `UNCERTAIN`.

Three things constrain what the model is allowed to conclude:

- **A pattern must graduate before it can be suppressed autonomously.**
  Graduation is a two-part gate: enough confirmed instances *and* enough
  diversity across host/user/hour. Repetition on one machine is one
  observation, not many.
- **The model's diffing is checked, not trusted.** Deviations are computed
  deterministically in Python and reconciled against what the model
  reported. If ground truth found a deviation the model missed and it still
  said `SUPPRESS`, the decision is overridden to `ESCALATE` in code — and
  the override is recorded in the returned reasoning, never silent. The
  reverse (model more cautious than ground truth) stands as-is.
- **Unassessed means risky, not safe.** A pattern with no blast-radius score
  defaults to HIGH.

A `SUPPRESS` decision enqueues an audit onto Cloud Tasks. `POST /sweep`, run
on a schedule, is the safety net for quiet patterns that event-triggering
would rarely revisit. Audits run in their own request at `POST /audit`.

When an audit fails repeatedly for the same pattern, that pattern is forced
to `UNCERTAIN` and a record is written to the `needs_attention` collection —
a pattern that has never actually been re-verified doesn't get to keep
suppressing.

The full reasoning behind these choices is archived in
[`docs/DESIGN_DECISIONS.md`](docs/DESIGN_DECISIONS.md).

---

## Structure

| File | What it is | Calls an LLM? |
|---|---|---|
| `vor_agents/schemas.py` | Pydantic output schemas for both agents | No |
| `vor_agents/identity.py` | Pattern identity key, structural template, diff logic, graduation gate | No |
| `vor_agents/enrichment.py` | Firestore reads/writes feeding the classifier | No |
| `vor_agents/review_flag.py` | `under_review` lifecycle, consecutive-failure tracking | No |
| `vor_agents/evidence_diversity.py` | Evidence diversity scoring over confirmed instances | No |
| `vor_agents/blast_radius.py` | Firestore-backed risk table + gated proposal path | No |
| `vor_agents/audit_targets.py` | Deterministic auditor target prioritization | No |
| `vor_agents/task_queue.py` | Cloud Tasks audit-enqueue path with server-side dedup | No |
| `vor_agents/tracing.py` | MLflow tracing, with a Firestore fallback queue | No |
| `vor_agents/datasets.py` | Synthetic dataset generation for the 6 canonical cases | No |
| `vor_agents/event_stream.py` | Synthetic alert *stream*: background traffic with the 6 cases injected | No |
| `vor_agents/model_config.py` | Gemini model selection (`GEMINI_MODEL`) | No |
| `vor_agents/firestore_config.py` | Firestore database selection (`FIRESTORE_DATABASE`) | No |
| `vor_agents/env_config.py` | Integer settings read from the environment | No |
| `vor_agents/classifier_agent.py` | ADK `Agent` definition, classifier prompt | Yes (Gemini) |
| `vor_agents/auditor_agent.py` | ADK `Agent` definition, auditor prompt, separate context | Yes (Gemini) |
| `vor_agents/orchestrator.py` | Wires everything together; the only file that calls `Runner` | Orchestrates both |
| `main.py` | Cloud Run entrypoint (FastAPI) | No |

**Neither agent has ADK `tools=` attached.** Enrichment, template-building,
and every Firestore write happen in plain Python before or after the agent
call, never inside it. That boundary is what makes the deterministic
override possible, and it is deliberate — see the agent module docstrings.

### Endpoints

| Endpoint | Trigger | Purpose |
|---|---|---|
| `POST /classify` | Pub/Sub push, or a direct call | Classify one alert |
| `POST /sweep` | Cloud Scheduler | Enqueue audits for stale patterns |
| `POST /audit` | Cloud Tasks | Run one audit |
| `POST /blast-radius/commit` | Human | Commit a pending MEDIUM/LOW proposal |
| `POST /replay-traces` | Cloud Scheduler | Drain the `pending_traces` fallback queue |
| `GET /healthz` | Cloud Run | Health check |

Only `/blast-radius/commit` is meant to be called by a person. None of these
should ever be deployed with `--allow-unauthenticated`.

### Firestore collections

`confidence_docs` (pattern history and templates) · `blast_radius_table` ·
`blast_radius_proposals` · `needs_attention` · `pending_traces`

---

## Getting started

Dependencies are managed with [uv](https://docs.astral.sh/uv/). `uv sync`
creates `.venv/` and installs the exact versions from `uv.lock` (runtime
deps plus the `dev` tool group).

```bash
uv sync
cp .env.example .env    # then fill it in
```

Run the test suite and the quality gates:

```bash
uv run pytest                                    # 395 tests, no network or credentials needed
uv run ruff check .
uv run black --check .
uv run mypy vor_agents/ main.py scripts/ dashboard/
uv run bandit -r vor_agents/ main.py scripts/
```

Integration tests that call the real Gemini API are excluded from the
default run because they cost money and aren't deterministic. Run them
deliberately:

```bash
uv run pytest -m integration
```

Without Vertex AI credentials configured they report skipped rather than
failing.

---

## Configuration

All settings come from the environment; see [`.env.example`](.env.example)
for the full list with notes on what each one does.

**Required:** `GCP_PROJECT`, `TASKS_LOCATION`, `TASKS_QUEUE`,
`TASKS_OIDC_SA_EMAIL`, `SERVICE_URL`, plus the Vertex AI variables
(`GOOGLE_GENAI_USE_VERTEXAI`, `GOOGLE_CLOUD_PROJECT`,
`GOOGLE_CLOUD_LOCATION`).

**Optional, each with a working default:** `GEMINI_MODEL`,
`FIRESTORE_DATABASE`, `MLFLOW_TRACKING_URI`, `MLFLOW_EXPERIMENT_NAME`,
`SWEEP_MAX_TARGETS`, `BLAST_RADIUS_CACHE_TTL_SECONDS`.

Secrets belong in `.env`, which is gitignored and must never be committed.

Backend selection is environment-driven and read by `google-genai` rather
than by this repo: setting the three Vertex AI variables routes model calls
through Vertex AI using Application Default Credentials, so the Cloud Run
service authenticates as itself instead of carrying an API key to rotate.

---

## Scripts

```bash
uv run python scripts/seed_firestore.py --case seeded_confirmed --dry-run
uv run python scripts/seed_blast_radius_table.py
uv run python scripts/backfill_identity_key.py --dry-run
uv run python scripts/generate_events.py --count 20 --dry-run
```

`seed_firestore.py` loads confirmed-negative history — either a synthetic
case or your own JSON export. `seed_blast_radius_table.py` populates the
risk table on a fresh project. `backfill_identity_key.py` is a one-time
migration for Firestore data predating the current doc-ID scheme.

`seed_firestore.py` and `backfill_identity_key.py` both support
`--dry-run`; use it first. `seed_blast_radius_table.py` takes no arguments
and is idempotent — re-running rewrites the same entries with the same
values.

### Generating alert traffic

`seed_firestore.py` loads the *history* a pattern is judged against.
`generate_events.py` is the other half — the live traffic that gets
judged. It publishes alerts to the `vor-alerts` Pub/Sub topic, so events
arrive through the real push subscription at `POST /classify` rather than
a test shortcut.

The stream is background noise with the 6 canonical cases injected into
it, which is closer to what triage actually looks like than replaying six
labelled probes: a pool of recurring benign patterns
(`vor_agents/event_stream.py`), a small fraction of events breaking one or
two diffable fields, a small fraction carrying a never-seen identity key,
and one canonical probe every `--case-interval` events.

```bash
# Inspect what would be sent, without sending it or needing credentials
uv run python scripts/generate_events.py --count 20 --dry-run

# 500 events at 5/sec to the default topic
uv run python scripts/generate_events.py --count 500 --rate 5

# Soak for 10 minutes at 2/sec; Ctrl-C stops cleanly and still summarizes
uv run python scripts/generate_events.py --duration 600 --rate 2

# Replay a JSON list of real alerts instead of generating them
uv run python scripts/generate_events.py --file alerts.json
```

`--dry-run` prints one alert per line on stdout (summary goes to stderr),
so it pipes into `jq` and back into `--file` unchanged. Run it first:
every published event is a real Gemini call, plus an audit enqueue per
`SUPPRESS`.

`--file` is the path for a Hayabusa/EVTX export, but the mapping is still
yours to write — see Known gaps. Records are validated for all four
identity fields and all five `DIFFABLE_FIELDS` before anything is
published, and one bad record refuses the whole run rather than leaving
half of it classified.

---

## Documentation

| Doc | What's in it |
|---|---|
| [`docs/DEPLOY.md`](docs/DEPLOY.md) | Deploying to Cloud Run, Scheduler, Tasks and Pub/Sub |
| [`docs/DATASET_RUNBOOK.md`](docs/DATASET_RUNBOOK.md) | The 6 synthetic cases and how to seed Firestore |
| [`docs/TESTING_PLAN.md`](docs/TESTING_PLAN.md) | What's tested where, and what deliberately isn't |
| [`docs/BLAST_RADIUS_PLAYBOOK.md`](docs/BLAST_RADIUS_PLAYBOOK.md) | How risk scores are set and promoted |
| [`docs/AGENT_DATA_FLOW.md`](docs/AGENT_DATA_FLOW.md) | What each agent sees and when |
| [`docs/DESIGN_DECISIONS.md`](docs/DESIGN_DECISIONS.md) | Archived/historical — rationale for the core design choices as of when they were made; parts are known out of date (see the doc's own header) |

---

## Known gaps

- **Not yet run against a real model.** The integration suite exists but has
  never been executed against a billed project, so the configured default
  model ID is unverified. See `docs/TODO-Aug24.md` Task 8.
- **Audit cost isn't throttled.** Every `SUPPRESS` enqueues an audit. Cloud
  Tasks dedups concurrent audits for the same pattern, but there's no rate
  limit or sampling — worth revisiting once real alert volume exists.
- **`needs_attention` has no alerting.** Escalations are written to
  Firestore and logged at CRITICAL, but nothing pushes them to a human yet.
- **No dead-letter topic** on the Pub/Sub subscription, so a permanently
  malformed message retries until it ages out.
- **Thresholds are unvalidated starting points.** `GRADUATION_THRESHOLD`,
  `MIN_DIVERSITY` and `AUDIT_FAILURE_ESCALATION_THRESHOLD` were chosen
  without production data to calibrate against, and are flagged as such in
  code.
- **Pre-existing Firestore data needs a backfill.** Docs written before the
  current doc-ID scheme have no `identity_key` field; readers skip them with
  a warning rather than crashing. Run `scripts/backfill_identity_key.py`
  before deploying against such data.
- **No Hayabusa/EVTX exporter.** Turning raw output into the JSON that
  `seed_firestore.py --file` and `generate_events.py --file` expect
  depends on your ingest pipeline and isn't built here. Note that this is
  a real mapping job, not a format conversion: of the five
  `DIFFABLE_FIELDS`, only `integrity_level` has a native Sysmon/EVTX
  counterpart. `auth_method_present`, `session_cookie_present`,
  `file_access_mode` and `egress_follows_access` are Vör-specific
  enrichment that something upstream has to supply. Both `--file` paths
  validate that all five are present rather than guessing them.
