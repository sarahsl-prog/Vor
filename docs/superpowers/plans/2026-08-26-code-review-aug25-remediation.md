# Code-Review-Aug25 Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix every Critical/High/Medium finding in `docs/Code-review-Aug25.md`, resolve all 7 "Outstanding Technical Decisions," and close the listed test gaps — one commit per task, per CLAUDE.md's implementation rules.

**Architecture:** No new subsystems except one: a persistent ADK `SessionService` backed by Cloud SQL. Everything else is a targeted fix inside the existing `vor_agents/` package (blast_radius, orchestrator, enrichment, tracing, identity, evidence_diversity, schemas, classifier_agent) plus doc/deploy-script updates. Each task is independently testable and independently committable.

**Tech Stack:** Python 3.13, pytest (+ pytest-asyncio), FastAPI, google-adk 2.7.0, google-cloud-firestore, loguru, mypy --strict, ruff, black, bandit.

**Spec:** `docs/Code-review-Aug25.md` (the review) — this plan implements Section 7's recommended actions in order, using the decisions recorded below (all confirmed with the project owner before this plan was written).

## Decisions this plan implements (from docs/Code-review-Aug25.md Section 6)

1. **Default Gemini model:** `gemini-2.0-flash` (already the value used everywhere else in the codebase; `gemini-3.5-flash` does not exist as a model).
2. **Blast-radius table writes:** append-only history — every commit writes a *new* Firestore doc (never overwrites), and the read path (`_load_table`) resolves the current score per indicator as the entry with the latest `committed_at`.
3. **`confirmed_instances` storage:** allow-list only — `identity_key` fields, `DIFFABLE_FIELDS`, `host`, `user`, `timestamp`, `instance_id`, `verified_by`. Nothing else is persisted.
4. **`structural_deviations_found` schema:** structured objects — `{"field": ..., "template": ..., "observed": ...}` — replacing the free-form `"field: template=X, observed=Y"` strings. Breaking change to `ClassifierOutput`; every producer/consumer/test updates in the same task.
5. **`last_reviewed_at` staleness:** left unbounded (no forced-reaudit threshold) — documented as a deliberate, uncalibrated design choice.
6. **Session persistence:** replace `InMemorySessionService` with ADK's `DatabaseSessionService`, backed by Cloud SQL (Postgres) in production and file-free in-memory SQLite by default locally/in tests.
7. **`pending_traces` TTL:** yes — add a Firestore TTL policy on `queued_at`, alongside the pagination fix.

## Global Constraints

- Python 3.13, `mypy --strict` on `vor_agents/` (not `tests/`).
- `ruff check --fix .`, `black .`, `bandit -r vor_agents/` must stay clean.
- Every new/changed function gets type annotations; no bare `except:`; new broad `except Exception` blocks need a `# noqa: BLE001` comment justifying the catch-all, matching the existing style in this codebase.
- One task = one commit. Imperative, one-line commit messages, no trailing period.
- Never let a Firestore write shape change break an existing reader without updating that reader in the same task (CLAUDE.md: "check for callers before changing signatures or data shapes").
- Run the narrowest relevant test file after each step; run the full suite (`pytest`) plus `ruff`/`black --check`/`mypy`/`bandit` at the end of every task before committing.
- Docs updated alongside code in the same task/commit whenever a task changes documented behavior.

---

### Task 1: Fix the default Gemini model

**Files:**
- Modify: `vor_agents/model_config.py:22`
- Modify: `.env.example:35`
- Test: `tests/test_agents.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `DEFAULT_GEMINI_MODEL: str` (unchanged name/type, new value) — every later task that imports it (none do) sees the corrected value.

- [x] **Step 1: Write the failing test**

Add to `tests/test_agents.py`, inside `class TestModelResolution`:

```python
    def test_default_model_is_a_real_flash_model_id(self):
        """Regression for the Aug25 code review: DEFAULT_GEMINI_MODEL was
        `gemini-3.5-flash`, which does not exist as a Google model. This
        pins the default to the well-known-valid family/version pattern
        so a future typo'd default fails a fast unit test instead of
        the first real Vertex AI call in production."""
        assert DEFAULT_GEMINI_MODEL == "gemini-2.0-flash"
```

- [x] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_agents.py -k test_default_model_is_a_real_flash_model_id -v`
Expected: FAIL — `assert 'gemini-3.5-flash' == 'gemini-2.0-flash'`

- [x] **Step 3: Fix the default**

In `vor_agents/model_config.py`:

```python
DEFAULT_GEMINI_MODEL = "gemini-2.0-flash"
```

In `.env.example` line 35, change the commented example to match:

```
# GEMINI_MODEL=gemini-2.0-flash
```

- [x] **Step 4: Run the full model/agent test file**

Run: `pytest tests/test_agents.py -v`
Expected: all PASS (this also fixes `test_builders_default_when_env_var_unset` and `test_default_when_env_var_unset`, which were silently asserting against the broken value before).

- [x] **Step 5: Commit**

```bash
git add vor_agents/model_config.py .env.example tests/test_agents.py
git commit -m "Fix DEFAULT_GEMINI_MODEL to a real model id"
```

---

### Task 2: Fix blast-radius re-scoring (append-only history)

**Files:**
- Modify: `vor_agents/blast_radius.py:91-138` (`_load_table`), `:160-200` (`_table_doc_id`, `_commit_indicators`)
- Test: `tests/test_blast_radius.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `_commit_indicators(cited_indicators: list[str], score: float, firestore_client: Client) -> None` (signature unchanged — callers `propose_blast_radius`/`commit_blast_radius_proposal` are untouched).

- [x] **Step 1: Write the failing regression test**

Add to `tests/test_blast_radius.py`, inside `TestEstimateBlastRadiusFromFirestore`:

```python
    def test_recommitting_same_indicator_with_a_new_score_wins(self, fake_firestore):
        """Regression for Code-review-Aug25 1.2: set(merge=True) on a
        content-hash doc ID silently kept the OLD score on a re-commit
        because Firestore merge doesn't overwrite an existing top-level
        scalar field. _commit_indicators now writes a new, timestamped
        doc per commit; _load_table must resolve the latest one."""
        from vor_agents.blast_radius import _commit_indicators

        _commit_indicators(["parent_image=lsass.exe"], 0.95, fake_firestore)
        reset_table_cache()
        first = estimate_blast_radius({"parent_image": "lsass.exe"}, fake_firestore)
        assert first == 0.95

        _commit_indicators(["parent_image=lsass.exe"], 0.30, fake_firestore)
        reset_table_cache()
        second = estimate_blast_radius({"parent_image": "lsass.exe"}, fake_firestore)
        assert second == 0.30  # the new score must win, not the stale 0.95

    def test_recommit_preserves_history_does_not_delete_old_entries(self, fake_firestore):
        """The whole point of append-only: the prior score is still a
        readable doc in blast_radius_table after a re-commit, not
        overwritten or deleted -- an auditor can reconstruct history."""
        from vor_agents.blast_radius import _commit_indicators

        _commit_indicators(["parent_image=lsass.exe"], 0.95, fake_firestore)
        _commit_indicators(["parent_image=lsass.exe"], 0.30, fake_firestore)

        docs = list(fake_firestore.collection(BLAST_RADIUS_TABLE_COLLECTION).stream())
        scores = sorted(d.to_dict()["score"] for d in docs)
        assert scores == [0.30, 0.95]
```

- [x] **Step 2: Run to verify it fails**

Run: `pytest tests/test_blast_radius.py -k "recommit" -v`
Expected: FAIL — `first == 0.95` may pass, but `second == 0.30` fails (still 0.95) since `set(merge=True)` on the same content-hash doc ID doesn't overwrite the stored score today.

- [x] **Step 3: Make `_commit_indicators` append-only**

In `vor_agents/blast_radius.py`, delete `_table_doc_id` entirely (dead code after this change — doc IDs are now random uuids, not a content hash of `(indicator_type, value)`, and nothing else in the module or its tests calls it) and replace `_commit_indicators`:

```python
def _commit_indicators(cited_indicators: list[str], score: float, firestore_client: Client) -> None:
    """
    Writes each cited indicator as a NEW, timestamped doc in
    blast_radius_table -- never overwrites or merges into an existing
    doc. This is deliberate: `set(merge=True)` on a content-hash doc ID
    (the old scheme) silently kept a STALE score on re-commit, because
    Firestore's merge=True does not overwrite an existing top-level
    scalar field -- see docs/Code-review-Aug25.md 1.2. Append-only also
    gives a free audit trail: every score this indicator has ever been
    assigned is a readable doc, not just the latest.

    doc_id is a random uuid4, not a content hash of (indicator_type,
    value) -- a content hash would map every commit for the same
    indicator back onto the SAME doc, defeating the append-only design
    by definition. _load_table() is responsible for picking the winner
    (latest committed_at) across every doc for a given indicator.
    """
    for indicator in cited_indicators:
        indicator_type, value = _parse_cited_indicator(indicator)
        firestore_client.collection(BLAST_RADIUS_TABLE_COLLECTION).document(
            str(uuid.uuid4())
        ).set(
            {
                "indicator_type": indicator_type,
                "value": value,
                "score": score,
                "committed_at": datetime.now(UTC).isoformat(),
            }
        )
    _invalidate_table_cache()
```

- [x] **Step 4: Make `_load_table` resolve the latest entry per indicator**

Replace the body of `_load_table`'s try block (the part that builds `fresh`):

```python
    try:
        fresh: dict[tuple[str, str], float] = {}
        # (indicator_type, value) -> the committed_at of the entry
        # currently winning for that key, so a later doc with an OLDER
        # committed_at (arbitrary Firestore stream order) never
        # clobbers a newer one already seen.
        newest_seen: dict[tuple[str, str], datetime] = {}
        for doc in firestore_client.collection(BLAST_RADIUS_TABLE_COLLECTION).stream():
            data = doc.to_dict() or {}
            indicator_type = data.get("indicator_type")
            value = data.get("value")
            score = data.get("score")
            if indicator_type is None or value is None or score is None:
                logger.bind(doc_id=doc.id).warning(
                    "blast_radius_table doc missing indicator_type/value/score, skipping"
                )
                continue
            key = (indicator_type, value)

            committed_at_raw = data.get("committed_at")
            try:
                committed_at = datetime.fromisoformat(committed_at_raw)
            except (TypeError, ValueError):
                # Malformed/missing committed_at -- treat as the oldest
                # possible entry so any doc WITH a valid timestamp always
                # wins over it, but it still populates the table if it's
                # the only entry for this key (degrade gracefully, same
                # posture as every other malformed-timestamp handling in
                # this project -- see orchestrator._fetch_all_confirmed_patterns).
                committed_at = datetime.min.replace(tzinfo=UTC)

            if key not in newest_seen or committed_at >= newest_seen[key]:
                newest_seen[key] = committed_at
                fresh[key] = score
        _TABLE_CACHE = fresh
        _TABLE_CACHE_LOADED_AT = now
        return _TABLE_CACHE
```

Note: `TestEstimateBlastRadius`/`TestEstimateBlastRadiusFromFirestore`'s existing `_seed_entry` helper writes docs with no `committed_at` field at all — the `except (TypeError, ValueError)` branch above handles that (`datetime.fromisoformat(None)` raises `TypeError`), so those pre-existing tests keep passing unchanged.

- [x] **Step 5: Run the full blast-radius suite**

Run: `pytest tests/test_blast_radius.py -v`
Expected: all PASS, including the two new regression tests.

- [x] **Step 6: Type-check and lint**

Run: `mypy vor_agents/blast_radius.py && ruff check vor_agents/blast_radius.py && black --check vor_agents/blast_radius.py`
Expected: clean.

- [x] **Step 7: Commit**

```bash
git add vor_agents/blast_radius.py tests/test_blast_radius.py
git commit -m "Make blast_radius_table writes append-only so re-scoring works"
```

---

### Task 3: Harden the sweep against malformed `last_reviewed_at`

**Files:**
- Modify: `vor_agents/orchestrator.py:650-664`
- Test: `tests/test_orchestrator.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: no signature change to `_fetch_all_confirmed_patterns`.

- [x] **Step 1: Write the failing test**

Add to `tests/test_orchestrator.py` (new test class, near the other `_fetch_all_confirmed_patterns`-exercising tests — search the file for `run_scheduled_sweep` usage to place it alongside):

```python
class TestSweepSurvivesMalformedLastReviewedAt:
    def test_bad_last_reviewed_at_does_not_crash_the_sweep(
        self, fake_firestore, diverse_confirmed_instances
    ):
        """Regression for Code-review-Aug25 1.3: a single corrupted
        last_reviewed_at string used to raise inside
        datetime.fromisoformat() with no handling, crashing the ENTIRE
        weekly sweep over one bad doc."""
        for instance in diverse_confirmed_instances:
            record_confirmed_negative(instance, fake_firestore)
        identity_key = pattern_identity_key(diverse_confirmed_instances[0])
        fake_firestore.collection(CONFIDENCE_COLLECTION).document(_doc_id(identity_key)).update(
            {"last_reviewed_at": "not-a-date"}
        )

        enqueued = run_scheduled_sweep(fake_firestore, enqueue_audit_fn=lambda k, p: True)

        assert identity_key in enqueued  # still surfaced, not dropped
```

- [x] **Step 2: Run to verify it fails**

Run: `pytest tests/test_orchestrator.py -k test_bad_last_reviewed_at_does_not_crash_the_sweep -v`
Expected: FAIL — `ValueError: Invalid isoformat string: 'not-a-date'` propagating out of `run_scheduled_sweep`.

- [x] **Step 3: Wrap the parse**

In `vor_agents/orchestrator.py`, replace:

```python
        last_reviewed_at = data.get("last_reviewed_at")
        if last_reviewed_at:
            reviewed_dt = datetime.fromisoformat(last_reviewed_at)
            days_since = max((datetime.now(UTC) - reviewed_dt).days, 0)
        else:
            days_since = 9999  # never audited — treat as maximally stale
```

with:

```python
        last_reviewed_at = data.get("last_reviewed_at")
        if last_reviewed_at:
            try:
                reviewed_dt = datetime.fromisoformat(last_reviewed_at)
                days_since = max((datetime.now(UTC) - reviewed_dt).days, 0)
            except (ValueError, TypeError):
                # A single corrupted timestamp must never take down the
                # whole sweep -- see docs/Code-review-Aug25.md 1.3. Treat
                # it the same as never-audited (the maximally-stale
                # sentinel below) so it still gets surfaced for review
                # rather than silently skipped.
                logger.bind(doc_id=doc.id, last_reviewed_at=last_reviewed_at).warning(
                    "Malformed last_reviewed_at, treating pattern as never audited"
                )
                days_since = 9999
        else:
            days_since = 9999  # never audited — treat as maximally stale
```

- [x] **Step 4: Run the test again**

Run: `pytest tests/test_orchestrator.py -k test_bad_last_reviewed_at_does_not_crash_the_sweep -v`
Expected: PASS.

- [x] **Step 5: Run the full orchestrator suite**

Run: `pytest tests/test_orchestrator.py -v`
Expected: all PASS.

- [x] **Step 6: Commit**

```bash
git add vor_agents/orchestrator.py tests/test_orchestrator.py
git commit -m "Survive malformed last_reviewed_at during the sweep"
```

---

### Task 4: Align `enrich()`'s stale-pattern default with the sweep's sentinel

**Files:**
- Modify: `vor_agents/enrichment.py:85`
- Test: `tests/test_enrichment.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: no signature change to `enrich()`; only the default value inside the returned dict's `days_since_last_review` key changes (0 → 9999) when the field is absent from the stored doc.

- [x] **Step 1: Write the failing test**

Add to `tests/test_enrichment.py` (find the class testing `enrich()` — likely `TestEnrich` — and add):

```python
    def test_never_audited_pattern_reports_the_stale_sentinel_not_zero(
        self, fake_firestore, diverse_confirmed_instances
    ):
        """Regression for Code-review-Aug25 2.1: a freshly-graduated
        pattern with no last_reviewed_at field yet used to report
        days_since_last_review=0 ('reviewed today'), contradicting the
        sweep's own 9999 ('never audited') sentinel for the exact same
        condition -- see orchestrator._fetch_all_confirmed_patterns."""
        for instance in diverse_confirmed_instances:
            record_confirmed_negative(instance, fake_firestore)

        result = enrich(diverse_confirmed_instances[0], fake_firestore)

        assert result["days_since_last_review"] == 9999
```

- [x] **Step 2: Run to verify it fails**

Run: `pytest tests/test_enrichment.py -k test_never_audited_pattern_reports_the_stale_sentinel_not_zero -v`
Expected: FAIL — `assert 0 == 9999`.

- [x] **Step 3: Fix the default**

In `vor_agents/enrichment.py`, in `enrich()`'s returned dict:

```python
        "days_since_last_review": data.get("days_since_last_review", 9999),
```

Update the docstring's default-value note too (the dict shape shown near the top of `enrich()`'s docstring) — no other change needed since the field is never actually stored on the doc at all (it's computed by the sweep, not stamped by `record_confirmed_negative`/`seed_template`); this only changes what a reader sees when it's absent.

- [x] **Step 4: Run the enrichment suite**

Run: `pytest tests/test_enrichment.py -v`
Expected: all PASS.

- [x] **Step 5: Commit**

```bash
git add vor_agents/enrichment.py tests/test_enrichment.py
git commit -m "Align enrich()'s stale-pattern default with the sweep's sentinel"
```

---

### Task 5: Bound `replay_pending_traces` + add a Firestore TTL policy on `queued_at`

**Files:**
- Modify: `vor_agents/tracing.py:108-137`
- Modify: `tests/conftest.py` (`_FakeCollection` needs `.limit()`)
- Modify: `scripts/deploy.sh` (add the TTL policy step)
- Modify: `docs/DEPLOY.md` (document the TTL policy + the new env var)
- Test: `tests/test_tracing.py`

**Interfaces:**
- Consumes: `env_int` from `vor_agents.env_config` (already used elsewhere in this codebase — see `orchestrator.py`'s `SWEEP_MAX_TARGETS_ENV_VAR` for the pattern being mirrored).
- Produces: `replay_pending_traces(firestore_client: Client, max_docs: int | None = None) -> int` — new optional keyword-only-in-spirit param, positional-compatible, so `main.py`'s existing `replay_pending_traces(client)` call keeps working untouched.

- [x] **Step 1: Add `.limit()` support to the Firestore fake**

In `tests/conftest.py`, add a `limit` method to `_FakeCollection`:

```python
    def limit(self, count):
        limited = dict(list(self._store.items())[:count])
        return _FakeQuery(limited)
```

(Mirrors real `google.cloud.firestore.CollectionReference.limit()`, which returns a `Query` that `.stream()`s the same way `_FakeQuery` already does.)

- [x] **Step 2: Write the failing regression test**

Add to `tests/test_tracing.py`, inside `TestReplayPendingTraces`:

```python
    def test_replay_is_bounded_by_max_docs(self, fake_firestore, monkeypatch):
        """Regression for Code-review-Aug25 2.2: replay_pending_traces
        used to materialize the ENTIRE pending_traces collection into
        memory every run -- an OOM risk during an extended MLflow
        outage. A bounded query caps memory use per replay run; the rest
        of the queue is picked up on the next scheduled run."""
        monkeypatch.setattr("vor_agents.tracing.mlflow", _FakeMlflowSuccess())
        for i in range(5):
            self._seed_pending(fake_firestore, f"id-{i}")

        count = replay_pending_traces(fake_firestore, max_docs=2)

        assert count == 2
        remaining = list(fake_firestore.collection(PENDING_TRACES_COLLECTION).stream())
        assert len(remaining) == 3  # untouched, picked up next run

    def test_replay_max_docs_defaults_from_env_var(self, fake_firestore, monkeypatch):
        monkeypatch.setattr("vor_agents.tracing.mlflow", _FakeMlflowSuccess())
        monkeypatch.setenv("TRACE_REPLAY_BATCH_SIZE", "1")
        for i in range(3):
            self._seed_pending(fake_firestore, f"id-{i}")

        count = replay_pending_traces(fake_firestore)

        assert count == 1
```

- [x] **Step 3: Run to verify it fails**

Run: `pytest tests/test_tracing.py -k "bounded or defaults_from_env_var" -v`
Expected: FAIL — `TypeError: replay_pending_traces() got an unexpected keyword argument 'max_docs'`.

- [x] **Step 4: Implement the bound**

In `vor_agents/tracing.py`, add `from .env_config import env_int` to the existing import block at the top of the file (alongside `from google.cloud.firestore import Client`), and add these two new constants after `PENDING_TRACES_COLLECTION`:

```python
DEFAULT_TRACE_REPLAY_BATCH_SIZE = 1000
TRACE_REPLAY_BATCH_SIZE_ENV_VAR = "TRACE_REPLAY_BATCH_SIZE"
# Caps how many pending_traces docs one replay run reads into memory.
# Previously unbounded -- see docs/Code-review-Aug25.md 2.2 -- an extended
# MLflow outage grows this collection without limit, and materializing
# all of it every 15 minutes (see docs/DEPLOY.md's replay schedule) is an
# avoidable OOM risk. 1000 is an unvalidated starting point, same posture
# as every other unvalidated interval/threshold in this project.
```

Replace `replay_pending_traces`'s signature and query:

```python
def replay_pending_traces(firestore_client: Client, max_docs: int | None = None) -> int:
    """
    Reads up to `max_docs` docs in pending_traces (default: $TRACE_REPLAY_BATCH_SIZE,
    else DEFAULT_TRACE_REPLAY_BATCH_SIZE), attempts to log each to MLflow;
    on success deletes the doc, on failure leaves it for the next
    scheduled run (see main.py's POST /replay-traces). Returns the count
    successfully replayed. Each doc gets its own try/except -- one bad or
    still-failing doc doesn't block the rest of the batch.

    Bounded rather than draining the whole collection in one call --
    during an extended MLflow outage this collection can grow far larger
    than fits comfortably in memory; the next scheduled run picks up
    whatever this one didn't reach. See docs/Code-review-Aug25.md 2.2.
    """
    if max_docs is None:
        max_docs = env_int(
            TRACE_REPLAY_BATCH_SIZE_ENV_VAR, DEFAULT_TRACE_REPLAY_BATCH_SIZE, minimum=1
        )

    replayed = 0
    still_pending = 0
    docs = list(
        firestore_client.collection(PENDING_TRACES_COLLECTION).limit(max_docs).stream()
    )
    for doc in docs:
        data = doc.to_dict() or {}
        run_type = data.get("run_type", "unknown")
        run_data = data.get("run_data", {})
        try:
            with mlflow.start_run(run_name=f"{run_type}_{run_data.get('identity_key')}_replayed"):
                mlflow.log_params(
                    {"run_type": run_type, "identity_key": str(run_data.get("identity_key"))}
                )
                mlflow.log_dict(run_data, "run_data.json")
            firestore_client.collection(PENDING_TRACES_COLLECTION).document(doc.id).delete()
            replayed += 1
        except Exception as exc:  # noqa: BLE001 — deliberate: one doc's
            # failure must not stop the rest of the batch from replaying.
            still_pending += 1
            logger.bind(doc_id=doc.id, run_type=run_type).warning(
                "Replay attempt failed, leaving doc for next run: {}", exc
            )
    logger.bind(replayed=replayed, still_pending=still_pending).info("Trace replay run complete")
    return replayed
```

- [x] **Step 5: Run the tracing suite**

Run: `pytest tests/test_tracing.py -v`
Expected: all PASS.

- [x] **Step 6: Add the Firestore TTL policy to the deploy script**

In `scripts/deploy.sh`, add a new numbered step after the Pub/Sub section (renumber the trailing `[N/9]` counters to match — check the existing step count first with `grep -n '\[[0-9]/9\]' scripts/deploy.sh` and bump the total):

```bash
# ---------------------------------------------------------------------------
# N. Set a Firestore TTL policy on pending_traces.queued_at.
# ---------------------------------------------------------------------------
echo "[N/10] Setting TTL policy on pending_traces.queued_at..."
_run_idempotent firestore fields ttls update queued_at \
  --collection-group=pending_traces \
  --enable-ttl \
  --database="${FIRESTORE_DATABASE}"
```

Firestore TTL deletion isn't immediate (Google documents up to ~24h lag), so this bounds long-term growth without replacing the `max_docs` cap above, which bounds a single replay call's memory use — the two address different failure windows.

- [x] **Step 7: Document both fixes in DEPLOY.md**

In `docs/DEPLOY.md`, replace the "Not addressed here" closing paragraph of the MLflow tracing section with:

```markdown
`pending_traces` growth during an extended MLflow outage is now bounded
two ways: `replay_pending_traces()` reads at most `$TRACE_REPLAY_BATCH_SIZE`
docs per scheduled run (default 1000, see `vor_agents/tracing.py`), and
`scripts/deploy.sh` sets a Firestore TTL policy on `queued_at` so docs
older than the TTL window are eventually purged even if MLflow never
recovers. Set `TRACE_REPLAY_BATCH_SIZE` on the Cloud Run service if the
default batch size doesn't match your traffic.
```

Also add `TRACE_REPLAY_BATCH_SIZE` to the optional-env-vars table earlier in the same doc, matching the existing row style for `SWEEP_MAX_TARGETS`.

- [x] **Step 8: Commit**

```bash
git add vor_agents/tracing.py tests/conftest.py tests/test_tracing.py scripts/deploy.sh docs/DEPLOY.md
git commit -m "Bound trace replay batch size and add a pending_traces TTL policy"
```

---

### Task 6: Restrict `confirmed_instances` to an explicit allow-list of fields

**Files:**
- Modify: `vor_agents/enrichment.py:91-201` (`record_confirmed_negative`, `seed_template`)
- Test: `tests/test_enrichment.py`

**Interfaces:**
- Consumes: `DIFFABLE_FIELDS` from `vor_agents.identity` (already imported in `enrichment.py`).
- Produces: no signature change to `record_confirmed_negative`/`seed_template`; only what gets written into each stored instance dict changes.

- [x] **Step 1: Write the failing test**

In `tests/test_enrichment.py`, add `CONFIDENCE_COLLECTION` and `_doc_id` to the existing `from vor_agents.enrichment import (...)` block (both tests below read the stored doc directly to inspect what was persisted):

```python
from vor_agents.enrichment import (
    CONFIDENCE_COLLECTION,
    _doc_id,
    enrich,
    invalidate_instances,
    record_confirmed_negative,
    seed_template,
)
```

Then add the tests:

```python
    def test_record_confirmed_negative_drops_fields_outside_the_allow_list(
        self, fake_firestore, baseline_alert
    ):
        """Regression for Code-review-Aug25 2.3: confirmed_instances used
        to store the entire alert dict verbatim, unbounded -- a large or
        deeply-nested alert repeated across many instances can push a
        confidence doc over Firestore's 1MiB-per-doc limit. Only the
        fields the deterministic logic actually uses should be kept."""
        alert = {**baseline_alert, "raw_event_xml": "x" * 5000, "extra_vendor_blob": {"a": 1}}

        record_confirmed_negative(alert, fake_firestore)

        identity_key = pattern_identity_key(alert)
        doc = fake_firestore.collection(CONFIDENCE_COLLECTION).document(_doc_id(identity_key)).get()
        stored_instance = doc.to_dict()["confirmed_instances"][0]

        assert "raw_event_xml" not in stored_instance
        assert "extra_vendor_blob" not in stored_instance
        assert stored_instance["host"] == baseline_alert["host"]
        assert stored_instance["integrity_level"] == baseline_alert["integrity_level"]
        assert stored_instance["verified_by"] == "human"

    def test_seed_template_also_drops_fields_outside_the_allow_list(
        self, fake_firestore, diverse_confirmed_instances
    ):
        polluted = [
            {**inst, "raw_event_xml": "x" * 5000} for inst in diverse_confirmed_instances
        ]

        seed_template(("rule", "p.exe", "c.exe", "family"), polluted, fake_firestore)

        doc = fake_firestore.collection(CONFIDENCE_COLLECTION).document(
            _doc_id(("rule", "p.exe", "c.exe", "family"))
        ).get()
        for instance in doc.to_dict()["confirmed_instances"]:
            assert "raw_event_xml" not in instance
```

- [x] **Step 2: Run to verify it fails**

Run: `pytest tests/test_enrichment.py -k "allow_list" -v`
Expected: FAIL — `raw_event_xml` present in the stored instance.

- [x] **Step 3: Add the allow-list and apply it at both write sites**

In `vor_agents/enrichment.py`, change the existing `from .identity import build_structural_template, pattern_identity_key` line to also pull in `DIFFABLE_FIELDS`:

```python
from .identity import DIFFABLE_FIELDS, build_structural_template, pattern_identity_key
```

Then add, after the `CONFIDENCE_COLLECTION` constant:

```python
# Every field a stored confirmed instance is allowed to carry. Anything
# else on the incoming alert is dropped before it's persisted -- see
# docs/Code-review-Aug25.md 2.3. identity_key fields are included because
# the auditor/classifier prompts and diff_alert_against_template all read
# them straight off a stored instance; DIFFABLE_FIELDS is what the
# deterministic diffing logic needs; the rest are bookkeeping fields
# written by this module itself (instance_id, verified_by) or used by
# evidence_diversity_score (host, user, timestamp).
_IDENTITY_FIELDS = ("detection_rule_id", "parent_image", "child_image", "endpoint_family")
CONFIRMED_INSTANCE_ALLOWED_FIELDS = frozenset(
    _IDENTITY_FIELDS + tuple(DIFFABLE_FIELDS) + ("host", "user", "timestamp", "instance_id", "verified_by")
)


def _restrict_to_allowed_fields(instance: dict[str, Any]) -> dict[str, Any]:
    """Drops every key not in CONFIRMED_INSTANCE_ALLOWED_FIELDS before an
    instance is persisted to confirmed_instances. Bounds document growth
    against Firestore's 1MiB-per-doc limit and keeps the stored evidence
    to exactly what the deterministic logic (diffing, diversity scoring,
    identity) actually reads -- nothing else is used downstream anyway."""
    return {k: v for k, v in instance.items() if k in CONFIRMED_INSTANCE_ALLOWED_FIELDS}
```

In `record_confirmed_negative`, change the `instances.append(...)` call:

```python
    instances.append(
        _restrict_to_allowed_fields(
            {
                **alert,
                "instance_id": alert.get("instance_id", str(uuid.uuid4())),
                "verified_by": "human" if human_confirmed else "bulk",
            }
        )
    )
```

In `seed_template`, change the `seeded_instances` list comprehension:

```python
    seeded_instances = [
        _restrict_to_allowed_fields(
            {
                **instance,
                "instance_id": instance.get("instance_id", str(uuid.uuid4())),
                "verified_by": "bulk",
            }
        )
        for instance in confirmed_negative_instances
    ]
```

- [x] **Step 4: Run the enrichment suite**

Run: `pytest tests/test_enrichment.py -v`
Expected: all PASS. Also re-run `pytest tests/test_orchestrator.py tests/test_identity.py tests/test_evidence_diversity.py -v` — these all consume `confirmed_instances` shapes built via the `conftest.py` fixtures directly (not through `record_confirmed_negative`), so they are unaffected, but confirm it explicitly since this is a data-shape change CLAUDE.md says to check callers on.

- [x] **Step 5: Commit**

```bash
git add vor_agents/enrichment.py tests/test_enrichment.py
git commit -m "Restrict confirmed_instances storage to an explicit field allow-list"
```

---

### Task 7: Use the truncated error repr consistently in `AuditorOutput.reasoning`

**Files:**
- Modify: `vor_agents/orchestrator.py:494-498`
- Test: `tests/test_orchestrator.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: no signature change.

- [x] **Step 1: Write the failing test**

Add to `tests/test_orchestrator.py` (near other `audit_pattern` failure-path tests):

```python
@pytest.mark.asyncio
class TestAuditFailureReasoningIsBounded:
    async def test_reasoning_uses_the_truncated_error_repr(self, fake_firestore):
        """Regression for Code-review-Aug25 2.4: `reasoning` embedded the
        FULL repr(exc) while last_error_repr (written to needs_attention)
        was truncated to 500 chars -- an inconsistency that let an
        unbounded exception repr (request IDs, URLs, stack context) reach
        MLflow/Firestore via AuditorOutput.reasoning even though the
        exact same string was being deliberately bounded two lines away."""
        identity_key = ("rule", "p.exe", "c.exe", "family")
        long_message = "x" * 2000
        with patch(
            "vor_agents.orchestrator._run_agent",
            new=AsyncMock(side_effect=RuntimeError(long_message)),
        ):
            result = await audit_pattern(identity_key, {"triggered_by": "test"}, fake_firestore)

        assert len(result.reasoning) < 600  # bounded, not ~2000+ chars
```

- [x] **Step 2: Run to verify it fails**

Run: `pytest tests/test_orchestrator.py -k test_reasoning_uses_the_truncated_error_repr -v`
Expected: FAIL — `reasoning` is ~2030 chars.

- [x] **Step 3: Fix it**

In `vor_agents/orchestrator.py`, inside `audit_pattern`'s `except Exception as exc:` block, change:

```python
        decision = AuditorOutput(
            action=AuditorAction.NO_ACTION,
            reasoning=f"Audit failed with error: {exc!r}",
        )
```

to:

```python
        decision = AuditorOutput(
            action=AuditorAction.NO_ACTION,
            reasoning=f"Audit failed with error: {last_error_repr}",
        )
```

(`last_error_repr` is already computed one line above this block — this just reuses it instead of re-deriving an unbounded copy.)

- [x] **Step 4: Run the orchestrator suite**

Run: `pytest tests/test_orchestrator.py -v`
Expected: all PASS.

- [x] **Step 5: Commit**

```bash
git add vor_agents/orchestrator.py tests/test_orchestrator.py
git commit -m "Bound AuditorOutput.reasoning consistently with last_error_repr"
```

---

### Task 8: Convert `pattern_identity_key`'s `KeyError` to `MalformedAlertError`

**Files:**
- Modify: `vor_agents/identity.py:59-73`
- Modify: `scripts/backfill_identity_key.py:79-82`
- Modify: `scripts/seed_firestore.py:94-97`
- Test: `tests/test_identity.py`

**Interfaces:**
- Consumes: `MalformedAlertError` (already defined in `vor_agents/identity.py`).
- Produces: `pattern_identity_key(alert: dict[str, Any]) -> tuple[str, ...]` — same signature, now raises `MalformedAlertError` instead of `KeyError` on a missing identity field.

- [x] **Step 1: Write the failing test**

Add to `tests/test_identity.py` (near the top-level identity-key tests):

```python
class TestPatternIdentityKeyValidation:
    def test_missing_identity_field_raises_malformed_alert_error(self, baseline_alert):
        """Regression for Code-review-Aug25 3.1: pattern_identity_key
        indexed the alert dict directly, so a missing identity field
        raised a raw KeyError -- inconsistent with this project's 'never
        surface raw exceptions' standard, which every OTHER validation
        path (ClassifierRequest, build_structural_template) already
        follows."""
        broken = dict(baseline_alert)
        del broken["parent_image"]

        with pytest.raises(MalformedAlertError, match="parent_image"):
            pattern_identity_key(broken)
```

- [x] **Step 2: Run to verify it fails**

Run: `pytest tests/test_identity.py -k test_missing_identity_field_raises_malformed_alert_error -v`
Expected: FAIL — raises `KeyError`, not `MalformedAlertError`.

- [x] **Step 3: Fix `pattern_identity_key`**

In `vor_agents/identity.py`, replace:

```python
def pattern_identity_key(alert: dict[str, Any]) -> tuple[str, ...]:
    """
    (detection_rule_id, parent_image, child_image, endpoint_family)

    Deliberately excludes every field in DIFFABLE_FIELDS. If auth-presence
    were part of identity, an attacker repeating a technique would just
    spawn "new, unmatched patterns" forever instead of ever tripping the
    deviation check against the legitimate one.
    """
    return (
        alert["detection_rule_id"],
        alert["parent_image"],
        alert["child_image"],
        alert["endpoint_family"],
    )
```

with:

```python
IDENTITY_KEY_FIELDS = ("detection_rule_id", "parent_image", "child_image", "endpoint_family")


def pattern_identity_key(alert: dict[str, Any]) -> tuple[str, ...]:
    """
    (detection_rule_id, parent_image, child_image, endpoint_family)

    Deliberately excludes every field in DIFFABLE_FIELDS. If auth-presence
    were part of identity, an attacker repeating a technique would just
    spawn "new, unmatched patterns" forever instead of ever tripping the
    deviation check against the legitimate one.

    Raises MalformedAlertError (not a raw KeyError) if any identity field
    is missing -- HTTP callers already get this via ClassifierRequest's
    validation, but internal callers (scripts/seed_firestore.py,
    scripts/backfill_identity_key.py, enrichment.record_confirmed_negative)
    pass plain dicts straight through, and a raw KeyError there violated
    this project's "never surface raw exceptions" standard. See
    docs/Code-review-Aug25.md 3.1.
    """
    missing = [field for field in IDENTITY_KEY_FIELDS if field not in alert]
    if missing:
        raise MalformedAlertError(f"Alert is missing required identity field(s): {missing}")
    return tuple(alert[field] for field in IDENTITY_KEY_FIELDS)
```

- [x] **Step 4: Update the two script call sites that caught `KeyError`**

In `scripts/backfill_identity_key.py`, in `_recover_identity_key`, change:

```python
        try:
            recovered.add(pattern_identity_key(instance))
        except KeyError as exc:
            raise BackfillError(f"confirmed_instance is missing identity field {exc}") from exc
```

to:

```python
        try:
            recovered.add(pattern_identity_key(instance))
        except MalformedAlertError as exc:
            raise BackfillError(f"confirmed_instance is malformed: {exc}") from exc
```

(add `MalformedAlertError` to that file's existing `from vor_agents.identity import ...` line).

In `scripts/seed_firestore.py`, in `load_instances_from_file`, change:

```python
        try:
            pattern_identity_key(instance)
        except KeyError as exc:
            raise SeedInputError(f"{path}[{index}] is missing identity field {exc}") from exc
```

to:

```python
        try:
            pattern_identity_key(instance)
        except MalformedAlertError as exc:
            raise SeedInputError(f"{path}[{index}] is malformed: {exc}") from exc
```

(`MalformedAlertError` is already imported in this file, per the existing `from vor_agents.identity import (... MalformedAlertError ...)` block used lower down for `seed()`'s own error handling.)

- [x] **Step 5: Run the identity suite plus the two script test files**

Run: `pytest tests/test_identity.py tests/test_backfill_identity_key.py tests/test_seed_firestore.py -v`
Expected: all PASS (no prior test in either script's suite asserted on `KeyError` directly, per a repo-wide grep before writing this task — only these two live call sites needed updating).

- [x] **Step 6: Commit**

```bash
git add vor_agents/identity.py scripts/backfill_identity_key.py scripts/seed_firestore.py tests/test_identity.py
git commit -m "Raise MalformedAlertError instead of KeyError from pattern_identity_key"
```

---

### Task 9: Make `evidence_diversity_score` robust to non-hashable host/user values

**Files:**
- Modify: `vor_agents/evidence_diversity.py:35-38`
- Test: `tests/test_evidence_diversity.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: no signature change to `evidence_diversity_score`.

- [x] **Step 1: Write the failing test**

Add to `tests/test_evidence_diversity.py` (this file uses bare functions, not a test class — match that style):

```python
def test_non_hashable_host_value_is_skipped_not_a_crash():
    """Regression for Code-review-Aug25 3.2: a bad ingestion pipeline
    storing a list/dict in `host` or `user` used to raise TypeError
    from the set comprehension -- this function is otherwise designed
    to degrade gracefully (see module docstring), so a malformed
    dimension should be skipped, not fatal."""
    instances = [
        {"host": ["not", "hashable"], "user": "jsmith", "timestamp": "2026-08-01T09:00:00Z"},
        {"host": "SRV-01", "user": "mjones", "timestamp": "2026-08-02T10:00:00Z"},
    ]

    score = evidence_diversity_score(instances)

    assert isinstance(score, float)
    assert 0.0 <= score <= 1.0
```

- [x] **Step 2: Run to verify it fails**

Run: `pytest tests/test_evidence_diversity.py -k test_non_hashable_host_value_is_skipped_not_a_crash -v`
Expected: FAIL — `TypeError: unhashable type: 'list'`.

- [x] **Step 3: Fix it**

In `vor_agents/evidence_diversity.py`, replace the `for dim in ("host", "user"):` loop body:

```python
    for dim in ("host", "user"):
        values = set()
        for inst in confirmed_instances:
            if dim not in inst:
                continue
            try:
                values.add(inst[dim])
            except TypeError:
                # A non-hashable value (list/dict) from a bad ingestion
                # pipeline -- skip it rather than crash. This function is
                # designed to degrade gracefully when a dimension isn't
                # usable, same as the "field absent" case already handled
                # by `if dim not in inst`. See docs/Code-review-Aug25.md 3.2.
                continue
        if values:
            ratios.append(min(len(values) / n, 1.0))
```

- [x] **Step 4: Run the evidence-diversity suite**

Run: `pytest tests/test_evidence_diversity.py -v`
Expected: all PASS.

- [x] **Step 5: Commit**

```bash
git add vor_agents/evidence_diversity.py tests/test_evidence_diversity.py
git commit -m "Skip non-hashable host/user values in evidence_diversity_score"
```

---

### Task 10: Migrate `structural_deviations_found` to structured objects

This is the largest task — a breaking schema change touching the schema, the deterministic diff, the classifier prompt's field description, the reconciliation logic, and every test that constructs a deviation string. Read every step before starting; the dedup/merge logic in Step 4 depends on the shape chosen in Step 1.

**Files:**
- Modify: `vor_agents/schemas.py` (`ClassifierOutput.structural_deviations_found`)
- Modify: `vor_agents/identity.py:128-140` (`diff_alert_against_template`)
- Modify: `vor_agents/classifier_agent.py` (Field description flows into the prompt via `output_schema`, but double-check the prompt body doesn't also hardcode the old string format)
- Modify: `vor_agents/orchestrator.py:180-209` (`_deviation_field_names`), `:366-424` (reconciliation merge/self-consistency blocks)
- Test: `tests/test_orchestrator.py`, `tests/test_identity.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: a deviation is now `dict[str, Any]` shaped `{"field": str, "template": Any, "observed": Any}` everywhere (identity.py's output, ClassifierOutput's field, orchestrator's internal handling) — no separate Pydantic submodel; kept as a plain dict to match how `pattern_data`/`enrichment` are already represented in this codebase, and so `identity.py` (deliberately dependency-free of pydantic) doesn't need a new import.

- [x] **Step 1: Change the schema field**

In `vor_agents/schemas.py`, replace `ClassifierOutput.structural_deviations_found`:

```python
    structural_deviations_found: list[dict[str, Any]] = Field(
        default_factory=list,
        description="EXHAUSTIVE list — every field-level mismatch found, "
        "not just the first. Each item is an object: "
        '{"field": <field name>, "template": <expected value>, '
        '"observed": <value on this alert>}.',
    )
```

- [x] **Step 2: Change `diff_alert_against_template` to emit structured objects**

In `vor_agents/identity.py`, replace:

```python
def diff_alert_against_template(
    alert: dict[str, Any], template_fields: dict[str, Any]
) -> list[str]:
    """
    Exhaustive diff — every field checked, never short-circuits on first
    mismatch. Returns human-readable deviation strings, empty list if none.
    """
    deviations = []
    for field, expected in template_fields.items():
        observed = alert.get(field)
        if observed != expected:
            deviations.append(f"{field}: template={expected!r}, observed={observed!r}")
    return deviations
```

with:

```python
def diff_alert_against_template(
    alert: dict[str, Any], template_fields: dict[str, Any]
) -> list[dict[str, Any]]:
    """
    Exhaustive diff — every field checked, never short-circuits on first
    mismatch. Returns structured deviation objects, empty list if none:
    [{"field": str, "template": <expected value>, "observed": <alert's
    value>}, ...]. Structured rather than a formatted string (see
    docs/Code-review-Aug25.md 3.3/3.4/decision 4) so orchestrator.py's
    reconciliation compares by field name without parsing free text, and
    a caller inspecting a real mismatch's values doesn't have to un-repr
    them out of a sentence.
    """
    deviations = []
    for field, expected in template_fields.items():
        observed = alert.get(field)
        if observed != expected:
            deviations.append({"field": field, "template": expected, "observed": observed})
    return deviations
```

- [x] **Step 3: Update `_deviation_field_names` for the new shape**

In `vor_agents/orchestrator.py`, replace:

```python
def _deviation_field_names(deviation_strings: list[str]) -> set[str]:
    """
    Extracts just the field name from each deviation string (format:
    "field_name: template=X, observed=Y" — see diff_alert_against_template()
    and the schema description for structural_deviations_found). Comparing
    by field name rather than exact string match is deliberate: the model
    is asked to follow this format but isn't guaranteed to phrase the
    template/observed values identically to the Python-computed version
    (repr formatting, quoting, etc.) — field name is the part that actually
    matters for reconciliation, not incidental text differences.

    A string with no colon (the model not following the format at all,
    e.g. "integrity_level observed High instead of Medium") previously got
    treated as a whole-string field name, which can't match a real
    template field and would never be found equal to anything on either
    side of the reconciliation diff in classify_alert(). That's silently
    fragile in the dangerous direction: it can make a real deviation look
    unreported ("missed_by_model") when the model actually did report it,
    just not in the expected format — skip and log instead of guessing.
    """
    parsed = set()
    for d in deviation_strings:
        d = d.strip()
        if not d:
            continue
        if ":" not in d:
            logger.warning("Deviation string missing expected 'field:' prefix: {}", d)
            continue
        parsed.add(d.split(":", 1)[0].strip())
    return parsed
```

with:

```python
def _deviation_field_names(deviations: list[dict[str, Any]]) -> set[str]:
    """
    Extracts just the field name from each structured deviation object
    (format: {"field": ..., "template": ..., "observed": ...} — see
    diff_alert_against_template() and ClassifierOutput.structural_deviations_found's
    schema description). Comparing by field name rather than the full
    object is deliberate: the model is asked to follow this schema but
    isn't guaranteed to serialize template/observed identically to the
    Python-computed version (type coercion, formatting) — field name is
    the part that actually matters for reconciliation.

    A dict missing the "field" key (the model not following its own
    output schema) is skipped and logged rather than guessed at — same
    "when in doubt, don't silently corrupt the comparison" posture as
    every other malformed-model-output handling in this module. This can
    only make a real deviation look unreported, never the reverse, which
    is the safe direction to fail in (see the ground-truth reconciliation
    block below, which treats "unreported" as the dangerous case to catch).
    """
    parsed = set()
    for d in deviations:
        field = d.get("field") if isinstance(d, dict) else None
        if not field:
            logger.warning("Deviation object missing a 'field' key: {}", d)
            continue
        parsed.add(field)
    return parsed
```

- [x] **Step 4: Fix the dict-merge in the ground-truth-missed override**

`classify_alert`'s reconciliation block currently does `sorted(set(...) | set(precomputed_deviations))`, which raises `TypeError: unhashable type: 'dict'` now that deviations are dicts. In `vor_agents/orchestrator.py`, add a small helper above `classify_alert`:

```python
def _merge_deviations(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Merges deviation-object lists with de-duplication, replacing the old
    `sorted(set(a) | set(b))` now that a deviation is a dict (unhashable).
    Dedup key is the JSON-serialized object (sorted keys) so two
    structurally-identical deviations from different sources (ground
    truth vs. model) collapse into one; sorted by that same key for
    deterministic output ordering, matching the old sorted-set behavior.
    """
    seen: dict[str, dict[str, Any]] = {}
    for group in groups:
        for deviation in group:
            key = json.dumps(deviation, sort_keys=True, default=str)
            seen[key] = deviation
    return [seen[key] for key in sorted(seen)]
```

Then in `classify_alert`, replace:

```python
            classifier_output = classifier_output.model_copy(
                update={
                    "decision": "ESCALATE",
                    "structural_deviations_found": sorted(
                        set(classifier_output.structural_deviations_found)
                        | set(precomputed_deviations)
                    ),
                    "reasoning": (
```

with:

```python
            classifier_output = classifier_output.model_copy(
                update={
                    "decision": "ESCALATE",
                    "structural_deviations_found": _merge_deviations(
                        classifier_output.structural_deviations_found, precomputed_deviations
                    ),
                    "reasoning": (
```

- [x] **Step 5: Update `tests/test_orchestrator.py`'s deviation strings to structured objects**

Every `"structural_deviations_found": [<string>]` fixture in this file that is NOT already `[]` must become a list of dicts. Specifically:

Line ~106 (`test_model_more_cautious_than_ground_truth_not_touched`):
```python
            "structural_deviations_found": [
                {"field": "some_field", "template": "X", "observed": "Y"}
            ],
```

Lines ~169 and ~192 (both `TestSelfConsistency` tests, same string):
```python
            "structural_deviations_found": [
                {"field": "integrity_level", "template": "Medium", "observed": "High"}
            ],
```

Replace `class TestDeviationFieldNames` entirely:

```python
class TestDeviationFieldNames:
    """
    Regression coverage: a deviation object missing its "field" key (the
    model not following its own output schema) used to be impossible to
    represent under the old free-string format's failure mode (a
    colon-less string) -- now the equivalent malformed case is a dict
    with no "field" key. Must be skipped (and logged), not guessed at.
    """

    def test_well_formed_objects_extract_field_name(self):
        result = _deviation_field_names(
            [
                {"field": "integrity_level", "template": "Medium", "observed": "High"},
                {"field": "file_access_mode", "template": "read", "observed": "write"},
            ]
        )
        assert result == {"integrity_level", "file_access_mode"}

    def test_object_missing_field_key_is_skipped_not_treated_as_a_field_name(self):
        result = _deviation_field_names(
            [
                {"template": "Medium", "observed": "High"},
            ]
        )
        assert result == set()

    def test_mix_of_well_formed_and_malformed_keeps_only_well_formed(self):
        result = _deviation_field_names(
            [
                {"field": "integrity_level", "template": "Medium", "observed": "High"},
                {"template": "no field key here"},
                {},
            ]
        )
        assert result == {"integrity_level"}
```

- [x] **Step 6: Update `tests/test_identity.py`'s `TestDiffAlertAgainstTemplate`**

Replace:

```python
    def test_exhaustive_not_first_match_only(
        self, field_level_drift_alert, confirmed_template_fields
    ):
        """Dataset case #6: ALL 5 fields deviate simultaneously. Must
        report every one, not short-circuit on the first mismatch — this
        was an explicit design decision (see agent_prompts.py / classifier
        prompt rule 3: 'do not stop at the first mismatch')."""
        deviations = diff_alert_against_template(field_level_drift_alert, confirmed_template_fields)
        deviated_fields = {d.split(":", 1)[0] for d in deviations}
        assert deviated_fields == {
            "auth_method_present",
            "session_cookie_present",
            "integrity_level",
            "file_access_mode",
            "egress_follows_access",
        }

    def test_single_field_deviation_detected(self, baseline_alert, confirmed_template_fields):
        drifted = {**baseline_alert, "integrity_level": "High"}
        deviations = diff_alert_against_template(drifted, confirmed_template_fields)
        assert len(deviations) == 1
        assert "integrity_level" in deviations[0]
```

with:

```python
    def test_exhaustive_not_first_match_only(
        self, field_level_drift_alert, confirmed_template_fields
    ):
        """Dataset case #6: ALL 5 fields deviate simultaneously. Must
        report every one, not short-circuit on the first mismatch — this
        was an explicit design decision (see classifier_agent.py's
        prompt rule 3: 'do not stop at the first mismatch')."""
        deviations = diff_alert_against_template(field_level_drift_alert, confirmed_template_fields)
        deviated_fields = {d["field"] for d in deviations}
        assert deviated_fields == {
            "auth_method_present",
            "session_cookie_present",
            "integrity_level",
            "file_access_mode",
            "egress_follows_access",
        }

    def test_single_field_deviation_detected(self, baseline_alert, confirmed_template_fields):
        drifted = {**baseline_alert, "integrity_level": "High"}
        deviations = diff_alert_against_template(drifted, confirmed_template_fields)
        assert len(deviations) == 1
        assert deviations[0]["field"] == "integrity_level"
        assert deviations[0]["template"] == "Medium"
        assert deviations[0]["observed"] == "High"
```

- [x] **Step 7: Run the full affected suite**

Run: `pytest tests/test_orchestrator.py tests/test_identity.py tests/test_schemas.py tests/test_tracing.py tests/test_main.py -v`
Expected: all PASS. (`test_tracing.py`/`test_main.py`/`test_schemas.py` only ever used the empty-list case, so they need no edits, but run them to confirm — `ClassifierOutput`'s type change to `list[dict[str, Any]]` must not break constructing it with `structural_deviations_found=[]`.)

- [x] **Step 8: Run the full suite plus quality gates**

Run: `pytest && ruff check --fix . && black . && mypy vor_agents/ && bandit -r vor_agents/`
Expected: all clean.

- [x] **Step 9: Commit**

```bash
git add vor_agents/schemas.py vor_agents/identity.py vor_agents/orchestrator.py tests/test_orchestrator.py tests/test_identity.py
git commit -m "Replace structural_deviations_found strings with structured objects"
```

---

### Task 11: Persistent session store — `DatabaseSessionService` on Cloud SQL

**Files:**
- Create: `vor_agents/session_config.py`
- Modify: `vor_agents/orchestrator.py:16, 45-47` (import + `session_service` construction)
- Modify: `requirements.txt` (add `sqlalchemy`, `asyncpg`)
- Modify: `scripts/deploy.sh` (provision Cloud SQL, grant IAM, set `SESSION_DB_URL`)
- Modify: `docs/DEPLOY.md` (new Cloud SQL section)
- Test: `tests/test_session_config.py` (new)

**Interfaces:**
- Consumes: `google.adk.sessions.DatabaseSessionService` (already installed — `sqlalchemy` and `aiosqlite` are transitive deps of `google-adk==2.7.0` already present in this project's venv; only `asyncpg`, the Postgres async driver for production, and an explicit pin of `sqlalchemy` need adding to `requirements.txt`).
- Produces: `build_session_service() -> BaseSessionService`; `orchestrator.session_service: BaseSessionService` (same name, same usage sites — `_run_agent`/`_discard_session` are untouched).

- [x] **Step 1: Pin the current package versions before adding new ones**

Run: `pip index versions asyncpg` and `pip index versions sqlalchemy` (or check PyPI directly) to confirm the current latest stable release — at the time this plan was written, `sqlalchemy==2.0.52` and `asyncpg==0.31.0` (`asyncpg==0.30.0` if you'd rather stay one release behind bleeding-edge). Use whatever is actually latest-stable at execution time; the exact patch version isn't load-bearing.

- [x] **Step 2: Add the dependencies**

In `requirements.txt`, add two lines (matching the file's exact-pin style):

```
sqlalchemy==2.0.52
asyncpg==0.31.0
```

Run: `pip install -r requirements.txt` (or `uv pip install -r requirements.txt`, matching however this repo's venv is normally synced) and confirm no conflicts.

- [x] **Step 3: Write `vor_agents/session_config.py`**

```python
"""
Vör -- ADK session-service selection.

One place for choosing which BaseSessionService backs every
classify_alert()/audit_pattern() call, mirroring model_config.py's
"resolve from env, single source of truth" shape.
"""

import os

from google.adk.sessions import BaseSessionService, DatabaseSessionService

SESSION_DB_URL_ENV_VAR = "SESSION_DB_URL"
# In-memory SQLite by default: zero setup for local dev and the test
# suite, while still exercising the REAL DatabaseSessionService class
# (not InMemorySessionService) -- see build_session_service()'s docstring
# for why that distinction matters. Production sets SESSION_DB_URL to a
# Cloud SQL Postgres connection string (see docs/DEPLOY.md's Cloud SQL
# section).
DEFAULT_SESSION_DB_URL = "sqlite+aiosqlite:///:memory:"


def build_session_service() -> BaseSessionService:
    """
    Builds the session store used for the life of the process. Unlike
    resolve_model()/env_int() (read per call, deliberately), this is
    constructed ONCE at import time in orchestrator.py and reused --
    DatabaseSessionService owns a SQLAlchemy connection pool, which is
    meant to be a long-lived singleton, not rebuilt per request.

    Reads $SESSION_DB_URL at call time (not bound at import into a
    default argument -- same reasoning as model_config.py) so a test can
    monkeypatch it before calling this function directly, even though
    orchestrator.py itself only calls it once.

    Every classify_alert()/audit_pattern() call already creates a fresh
    session and deletes it in a finally block before returning (see
    orchestrator._discard_session) -- no session is ever reused across
    requests today. A persistent backing store still matters for two
    reasons that are independent of that: (1) InMemorySessionService's
    entire state lives in one process's heap, so a Cloud Run instance
    recycled mid-request (autoscaling, deploy, OOM) silently drops any
    session created but not yet cleaned up; (2) it's the seam that lets a
    future feature reuse a session across calls (e.g. multi-turn audit
    review) without a second migration.
    """
    db_url = os.environ.get(SESSION_DB_URL_ENV_VAR, "").strip() or DEFAULT_SESSION_DB_URL
    return DatabaseSessionService(db_url=db_url)
```

- [x] **Step 4: Write the failing test for env-var resolution**

Create `tests/test_session_config.py`:

```python
"""
Tests for vor_agents.session_config -- which BaseSessionService backs the
ADK Runner, and that it's actually persistent (survives past one
in-process instance), not just "a different class with the same API."
"""

import asyncio

import pytest
from google.adk.sessions import DatabaseSessionService

from vor_agents.session_config import (
    DEFAULT_SESSION_DB_URL,
    SESSION_DB_URL_ENV_VAR,
    build_session_service,
)


class TestBuildSessionService:
    def test_defaults_to_in_memory_sqlite_when_env_var_unset(self, monkeypatch):
        monkeypatch.delenv(SESSION_DB_URL_ENV_VAR, raising=False)

        service = build_session_service()

        assert isinstance(service, DatabaseSessionService)

    def test_honors_the_env_var(self, monkeypatch, tmp_path):
        db_path = tmp_path / "sessions.db"
        monkeypatch.setenv(SESSION_DB_URL_ENV_VAR, f"sqlite+aiosqlite:///{db_path}")

        service = build_session_service()

        assert isinstance(service, DatabaseSessionService)

    def test_blank_env_var_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setenv(SESSION_DB_URL_ENV_VAR, "   ")

        service = build_session_service()

        assert isinstance(service, DatabaseSessionService)  # did not raise on a blank URL


@pytest.mark.asyncio
class TestSessionSurvivesAcrossInstances:
    async def test_a_session_created_by_one_instance_is_readable_by_another(
        self, monkeypatch, tmp_path
    ):
        """THE regression this task exists for: InMemorySessionService's
        state is process-local, so a session created by one instance is
        invisible to a second instance even pointed at 'the same' store
        -- there's no shared store at all. A real persistent backing
        store (a file-based SQLite DB standing in for Cloud SQL here)
        must let a SECOND, independently-constructed service instance
        see a session the FIRST instance created -- proving persistence
        isn't just "a different class," it's actually shared storage."""
        db_path = tmp_path / "sessions.db"
        monkeypatch.setenv(SESSION_DB_URL_ENV_VAR, f"sqlite+aiosqlite:///{db_path}")

        first_instance = build_session_service()
        session = await first_instance.create_session(app_name="vor", user_id="vor-system")

        second_instance = build_session_service()
        recovered = await second_instance.get_session(
            app_name="vor", user_id="vor-system", session_id=session.id
        )

        assert recovered is not None
        assert recovered.id == session.id
```

- [x] **Step 5: Run to verify it fails**

Run: `pytest tests/test_session_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'vor_agents.session_config'` (file doesn't exist yet if you're doing TDD strictly; since Step 3 already wrote it above, running now should mostly PASS except confirm `test_a_session_created_by_one_instance_is_readable_by_another` actually exercises real persistence — this is the one worth running FIRST against the OLD `InMemorySessionService` wiring if you want to see it fail meaningfully; against `session_config.py` alone it should already pass once Step 3 is in place).

- [x] **Step 6: Wire it into orchestrator.py**

In `vor_agents/orchestrator.py`, replace:

```python
from google.adk.sessions import InMemorySessionService
```

with:

```python
from .session_config import build_session_service
```

(remove the now-unused `InMemorySessionService` import), and replace:

```python
session_service = InMemorySessionService()  # swap for a persistent
# SessionService in production;
# fine for a hackathon demo
```

with:

```python
session_service = build_session_service()  # see session_config.py --
# DatabaseSessionService, backed by Cloud SQL in production and an
# in-memory SQLite database locally/in tests.
```

- [x] **Step 7: Run the full test suite**

Run: `pytest -v`
Expected: all PASS, including `tests/test_run_agent.py` (imports `session_service` from `orchestrator` directly and calls `.get_session()` on it — this now exercises the real `DatabaseSessionService` against in-memory SQLite instead of `InMemorySessionService`, which is the point).

- [x] **Step 8: Provision Cloud SQL in the deploy script**

In `scripts/deploy.sh`, add a new section (after the Cloud Tasks queue section, before the env-vars `gcloud run services update` call — `SESSION_DB_URL` needs to be known before that call):

```bash
# ---------------------------------------------------------------------------
# N. Cloud SQL instance + database for session persistence.
# ---------------------------------------------------------------------------
: "${SESSION_DB_INSTANCE:=vor-sessions}"
: "${SESSION_DB_NAME:=vor_sessions}"
: "${SESSION_DB_USER:=vor}"

echo "[N/11] Ensuring Cloud SQL instance ${SESSION_DB_INSTANCE} exists..."
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
```

Then add `SESSION_DB_URL` to the existing `gcloud run services update ... --set-env-vars` call alongside the other required vars, and document `SESSION_DB_INSTANCE`/`SESSION_DB_NAME`/`SESSION_DB_USER`/`SESSION_DB_PASSWORD` in the script's header comment block, matching the existing `SCHEDULER_SA`/`TASKS_QUEUE`-style documentation. `db-f1-micro` is the smallest/cheapest Cloud SQL tier, matching this project's existing "scale-to-zero, cap runaway spend" cost posture (`--min-instances 0`, `--max-instances 3` on Cloud Run) — call this out as an unvalidated starting point, same posture as every other default in this project.

- [x] **Step 9: Document the Cloud SQL setup in DEPLOY.md**

In `docs/DEPLOY.md`, add a new section after "## 3e. Seed confirmed-negative history (optional)" and before "## 4. Wire /classify to a Pub/Sub push subscription" (keeps the lettered 3x sub-sections together, in step order):

```markdown
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

Every session created by `_run_agent()` is deleted again in the same
request's `finally` block (see `orchestrator._discard_session`) — nothing
here changes that lifecycle. What changes is durability: a Cloud Run
instance recycled mid-request (autoscaling, deploy, OOM) no longer
silently drops session state that lived only in that instance's heap.

Tier is `db-f1-micro`, the smallest/cheapest Cloud SQL option — an
unvalidated starting point, same posture as every other capacity default
in this project (`SWEEP_MAX_TARGETS`, the Cloud Tasks retry backoff).
Revisit once real traffic volume exists.
```

Also add `SESSION_DB_URL` to the optional-env-vars table (default: in-memory SQLite; set it when deploying to Cloud Run).

- [x] **Step 10: Full suite + quality gates**

Run: `pytest && ruff check --fix . && black . && mypy vor_agents/ && bandit -r vor_agents/`
Expected: all clean.

- [x] **Step 11: Commit**

```bash
git add vor_agents/session_config.py vor_agents/orchestrator.py requirements.txt scripts/deploy.sh docs/DEPLOY.md tests/test_session_config.py
git commit -m "Replace InMemorySessionService with a Cloud SQL-backed DatabaseSessionService"
```

---

### Task 12: Update stale docs (AGENT_DATA_FLOW.md, README.md) and record the remaining open decisions

**Files:**
- Modify: `docs/AGENT_DATA_FLOW.md:102-106`
- Modify: `README.md` (table entry for `DESIGN_DECISIONS.md`)
- Modify: `docs/TODO-Aug24.md` (record the staleness-policy decision)

**Interfaces:** none (docs only).

- [x] **Step 1: Fix `AGENT_DATA_FLOW.md`'s stale Status section**

Replace:

```markdown
## Status

Solid edges (Pub/Sub trigger, Cloud Tasks enqueue/dispatch, both agent
calls, `confidence_docs` reads/writes) are implemented and tested today.
`needs_attention` and the MLflow tracing branch are specced and planned
(`docs/superpowers/specs/`, `docs/superpowers/plans/`) but not yet built —
see `docs/TODO-Aug24.md` for current status.
```

with:

```markdown
## Status

Every edge in this diagram is implemented and tested: the Pub/Sub
trigger, Cloud Tasks enqueue/dispatch, both agent calls, `confidence_docs`
reads/writes, `needs_attention` escalation (`review_flag.py`,
`orchestrator.py`'s `AUDIT_FAILURE_ESCALATION_THRESHOLD` path), and the
MLflow tracing fallback/replay branch (`vor_agents/tracing.py`,
`POST /replay-traces` in `main.py`). See `docs/TODO-Aug24.md` for the
handful of remaining non-flow gaps (real-API smoke tests, dead-letter
topic config) that don't change this diagram.
```

- [x] **Step 2: Mark `DESIGN_DECISIONS.md` as archived in the README table**

In `README.md`, change:

```markdown
| [`docs/DESIGN_DECISIONS.md`](docs/DESIGN_DECISIONS.md) | Archived rationale for the core design choices |
```

to:

```markdown
| [`docs/DESIGN_DECISIONS.md`](docs/DESIGN_DECISIONS.md) | Archived/historical — rationale for the core design choices as of when they were made; parts are known out of date (see the doc's own header) |
```

(The word "Archived" was already there — this just makes the "known out of date, read the header" caveat visible from the README's own table instead of only inside the linked doc, per Code-review-Aug25.md 3.5.)

- [x] **Step 3: Record the staleness-policy decision**

In `docs/TODO-Aug24.md`, add a new entry following the file's existing "**Decision (date):**" convention (see Task 1's entries for the exact style to match) — add it near the bottom, in a new short section:

```markdown
## Outstanding decision — last_reviewed_at staleness (2026-08-26)

- [x] **Decision: leave `days_since_last_review` unbounded, no forced-reaudit
  threshold.** `select_audit_targets()` uses staleness as a priority signal
  (older = higher priority) but nothing forces a re-audit past any fixed
  age. Revisit once real audit volume exists to calibrate an actual
  threshold — there's no production Hayabusa/EVTX history yet to pick a
  number against, same gap noted for `GRADUATION_THRESHOLD`/`MIN_DIVERSITY`
  elsewhere in this project. See `docs/Code-review-Aug25.md` Section 6,
  decision 5.
```

- [x] **Step 4: Commit**

```bash
git add docs/AGENT_DATA_FLOW.md README.md docs/TODO-Aug24.md
git commit -m "Update stale docs and record the staleness-policy decision"
```

---

### Task 13: End-to-end `/audit` test through the real `audit_pattern()`

Closes the last unaddressed row in Section 4's test-gap table: `test_main.py` exercises `POST /audit` only with `audit_pattern` mocked out, so nothing catches a drift between the Cloud-Tasks-constructed body shape (`task_queue.py`) and what `audit_pattern()` actually does with it, end to end through the HTTP layer.

**Files:**
- Test: `tests/test_main.py`

**Interfaces:** none — test-only, no production code changes.

- [x] **Step 1: Add the one missing import**

In `tests/test_main.py`'s existing `from vor_agents...` import block, add:

```python
from vor_agents.enrichment import _doc_id
```

- [x] **Step 2: Write the end-to-end test**

Add to `tests/test_main.py`, near `test_audit_endpoint_invokes_audit_pattern`:

```python
def test_audit_endpoint_runs_the_real_audit_pattern_end_to_end(fake_firestore):
    """Regression for the Code-review-Aug25 Section 4 test gap: every
    other /audit test mocks audit_pattern() itself, so nothing exercises
    the real path from an HTTP POST body through AuditRequest validation,
    audit_pattern()'s mark/run/clear_under_review lifecycle, and back out
    as a JSON response. Only the LLM call itself is faked (via
    _run_agent, same seam test_orchestrator.py's reconciliation tests
    use) -- everything else in this request is the real code."""
    identity_key = ["rule", "w3wp.exe", "csc.exe", "family"]
    fake_model_response = {
        "action": "NO_ACTION",
        "invalidated_instance_ids": [],
        "concerns_found": [],
        "reasoning": "Evidence still looks solid.",
    }

    with (
        patch("main.get_firestore_client", return_value=fake_firestore),
        patch(
            "vor_agents.orchestrator._run_agent",
            new=AsyncMock(return_value=fake_model_response),
        ),
    ):
        client = TestClient(main.app)
        resp = client.post(
            "/audit",
            json={
                "identity_key": identity_key,
                "pattern_data": {"triggered_by": "test", "blast_radius": 0.5},
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["action"] == "NO_ACTION"
    assert body["reasoning"] == "Evidence still looks solid."

    # under_review must be cleared by the real clear_under_review() call,
    # not just returned in the response -- proves the full lifecycle ran,
    # not only the LLM-call seam.
    doc = fake_firestore.collection("confidence_docs").document(
        _doc_id(tuple(identity_key))
    ).get()
    assert doc.to_dict().get("under_review", False) is False
```

- [x] **Step 3: Run it, and confirm it actually exercises the real path**

Run: `pytest tests/test_main.py -k test_audit_endpoint_runs_the_real_audit_pattern_end_to_end -v`
Expected: PASS. This is new coverage, not a bug fix, so there's no pre-existing failure to reproduce — instead confirm the test is load-bearing by temporarily breaking something it should catch (e.g. comment out the `clear_under_review(...)` call inside `audit_pattern`), re-running, confirming this test fails while the mocked-`audit_pattern` tests elsewhere in the file still pass, then reverting the temporary breakage.

- [x] **Step 4: Commit**

```bash
git add tests/test_main.py
git commit -m "Add an end-to-end /audit test through the real audit_pattern"
```

---

### Task 14: Final verification pass — full quality gate + working-tree cleanup

**Files:** none changed (verification only), except removing untracked cache dirs.

- [x] **Step 1: Clean caches**

Run: `git clean -ndX` first to see what would be removed (never skip the dry run), then `git clean -fdX` if the list is only `__pycache__`/`.pytest_cache`/`.ruff_cache`/`.mypy_cache`-type entries already covered by `.gitignore`.

- [x] **Step 2: Run the full quality gate**

Run: `pre-commit run --all-files` (or, if `pre-commit` isn't installed in this environment, run each check it wraps individually: `ruff check --fix . && black . && mypy vor_agents/ && bandit -r vor_agents/`)
Expected: clean.

- [x] **Step 3: Run the full test suite**

Run: `pytest -v`
Expected: every test passes (the review's baseline was 288 passed, 4 deselected — expect that count to have grown by roughly the number of regression tests added across Tasks 1–13).

- [x] **Step 4: Confirm no stray changes**

Run: `git status`
Expected: clean working tree (everything already committed task-by-task).

- [x] **Step 5: No commit needed for this task** — it's verification-only. If `git clean` removed anything, that's untracked-file cleanup, not a commit.

---

## Post-plan: re-read `docs/Code-review-Aug25.md` against the result

After Task 14, Section 7's 12 recommended actions and Section 4's test gaps should all be closed except: item 10 (docs) is Task 12's scope only for AGENT_DATA_FLOW.md/README.md — DESIGN_DECISIONS.md's own body is left alone (it already says it's historical); and item 12 (record outstanding decisions) is now split across TODO-Aug24.md (Task 12) and this plan's own decision log (Tasks 2/3/6/10/11 headers) rather than a single new design note — that was a deliberate choice, not a gap, since the decisions were resolved by shipping code in most cases rather than left open.
