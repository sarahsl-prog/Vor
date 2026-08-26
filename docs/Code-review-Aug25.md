# Vör Code Review — 2026-08-25

**Scope:** full repository as of HEAD (`main`).
**Method:** read source + docs side-by-side, ran the full test suite, ran `ruff`, `black --check`, `mypy`, and `bandit`, and inspected test coverage for the safety-critical paths.
**Verdict at a glance:** the deterministic safety layer is well-architected and thoroughly tested. Most issues are medium/low operational gaps or doc/code drift rather than acute safety bugs. The highest-priority items are an invalid default model name and a few places where production data shape/scale can break an otherwise clean path.

---

## 1. Critical / High Priority

### 1.1. Default Gemini model name does not exist

- **File:** `vor_agents/model_config.py:22`
- **Current:** `DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"`
- **Problem:** There is no Google model called `gemini-3.5-flash`. The real Flash-family names are `gemini-1.5-flash`, `gemini-2.0-flash`, `gemini-2.5-flash-preview-05-20`, etc. With `GEMINI_MODEL` unset, both agents will be built with a non-existent model string and fail at first real invocation. This is especially bad because the README / `.env.example` present the default as "working."
- **Fix:**
  ```python
  DEFAULT_GEMINI_MODEL = "gemini-2.0-flash"
  ```
  or whatever validated model the project intends. Update `docs/DEPLOY.md` and `.env.example` (line 35) to match. Add a regression test asserting the default string is a known-valid model ID.
- **Test gap:** `tests/test_agents.py` only checks that the default equals `DEFAULT_GEMINI_MODEL`, not that the value itself is sensible. The existing integration suite is deselected by default, so this default would not be exercised in CI.

### 1.2. Blast-radius table entries cannot be safely re-scored

- **File:** `vor_agents/blast_radius.py:184-200`
- **Current:** `_commit_indicators` uses `DocumentReference.set(..., merge=True)`.
- **Problem:** Firestore `set(merge=True)` *merges* top-level keys but **does not overwrite an existing top-level field value if that field is present in the stored document**. A re-proposal (or later correction) that cites the same `(indicator_type, value)` with a different `score` will write the new doc, but the stored `score` will silently remain the old one. This means the blast-radius table can effectively never be updated through the supported proposal/commit paths.
  - Note: tests pass because the in-memory `FakeFirestoreClient` (`tests/conftest.py:164-168`) implements `set(merge=True)` via `dict.update`, which **does** overwrite top-level scalar values — the fake behaves differently from real Firestore here.
- **Fix:** Use an explicit overwrite, or use `update(["score", "committed_at"])`, or first delete the doc. Suggested:
  ```python
  firestore_client.collection(BLAST_RADIUS_TABLE_COLLECTION).document(doc_id).set(
      {
          "indicator_type": indicator_type,
          "value": value,
          "score": score,
          "committed_at": datetime.now(UTC).isoformat(),
      }
  )
  ```
  Since `doc_id` is a content hash, the write is idempotent and an overwrite is the desired behavior.
- **Tests:** add a regression test in `tests/test_blast_radius.py` that commits an indicator, then re-commits the same indicator with a *different* score (and a cache reset), and asserts the new score wins. This must be written against a fake that matches real Firestore merge semantics, or against the real SDK with a mocked transport.

### 1.3. Sweep crashes on malformed `last_reviewed_at`

- **File:** `vor_agents/orchestrator.py:650-664`
- **Current:** `datetime.fromisoformat(last_reviewed_at)` is called without exception handling.
- **Problem:** A single corrupted `last_reviewed_at` string in one `confidence_docs` document will crash the entire weekly sweep. Because `_fetch_all_confirmed_patterns` runs a broad query, this is a single-bad-doc kills-the-safety-net failure mode.
- **Fix:** Wrap the parse and fall back to the never-audited sentinel (9999 days), logging the bad value and doc id:
  ```python
  try:
      reviewed_dt = datetime.fromisoformat(last_reviewed_at)
      days_since = max((datetime.now(UTC) - reviewed_dt).days, 0)
  except (ValueError, TypeError):
      logger.bind(doc_id=doc.id, last_reviewed_at=last_reviewed_at).warning(
          "Malformed last_reviewed_at, treating as never audited"
      )
      days_since = 9999
  ```
- **Tests:** add a regression test in `tests/test_orchestrator.py` with a confirmed doc whose `last_reviewed_at` is `"not-a-date"` and assert it is still returned with `days_since_last_review == 9999`.

---

## 2. Medium Priority

### 2.1. `enrich()` reports "0 days since last review" for never-audited patterns

- **File:** `vor_agents/enrichment.py:85`
- **Current:** `days_since_last_review` defaults to `0` when missing from the doc.
- **Problem:** In the classifier prompt, `0` implies "reviewed today." That contradicts the sweep logic, which uses `9999` for never-audited patterns so they look maximally stale. The classifier prompt itself does not make decisions off this number, but a future maintainer (or a future model) could. It also makes logs/traces misleading.
- **Fix:** Align the default with the sweep sentinel:
  ```python
  "days_since_last_review": data.get("days_since_last_review", 9999),
  ```
  or, better, read the doc's `last_reviewed_at` and compute it the same way `_fetch_all_confirmed_patterns` does. If the field is absent, use `9999`.
- **Tests:** add a test asserting `enrich()` on a freshly-created confirmed pattern returns `9999` (or whatever sentinel is chosen) for `days_since_last_review`.

### 2.2. `replay_pending_traces` materializes the whole fallback queue

- **File:** `vor_agents/tracing.py:118`
- **Current:** `for doc in list(firestore_client.collection(PENDING_TRACES_COLLECTION).stream()):`
- **Problem:** During an extended MLflow outage, `pending_traces` grows unbounded (already noted in `DEPLOY.md`). Materializing the entire collection into memory for every replay run is an unnecessary OOM risk. The doc notes this as "worth a TTL/max-size policy" but the code itself has no pagination cap.
- **Fix:** Add a `limit()` to the query (e.g. 1000 docs per replay) and/or delete docs as you go rather than building the full list first. Also consider a Firestore TTL policy on `queued_at`.
- **Tests:** add a test that creates many pending docs and asserts replay only processes/loads a bounded number per call.

### 2.3. `record_confirmed_negative` can store arbitrarily large alert blobs

- **File:** `vor_agents/enrichment.py:131-137`
- **Current:** `instances.append({**alert, ...})` stores the entire alert dict verbatim.
- **Problem:** Firestore documents are capped at 1 MiB. A large or deeply-nested alert (e.g. a big Windows Event Log record) appended repeatedly can eventually push the confidence doc over the limit, causing all future writes to fail. The failure is not catastrophic immediately, but it is unbounded growth in a hot path.
- **Fix:** Define a small allow-list of fields to retain in `confirmed_instances`, or validate/truncate the stored dict. At minimum, store only the identity fields, `DIFFABLE_FIELDS`, `host`, `user`, `timestamp`, `instance_id`, and `verified_by`. Anything else is not used by the deterministic logic anyway.
- **Tests:** add a test that an alert with a large extra field is stored with the extra field removed/truncated, and that repeated calls stay under a bounded size.

### 2.4. Exception `repr` is truncated in one place but not the other

- **File:** `vor_agents/orchestrator.py:494-498`
- **Current:**
  ```python
  last_error_repr = repr(exc)[:500]
  ...
  reasoning=f"Audit failed with error: {exc!r}",
  ```
- **Problem:** `reasoning` gets the full `repr(exc)` (could include request IDs, URLs, stack context) while `last_error_repr` is bounded. This is a minor inconsistency; both should be bounded before hitting Firestore or logs. The reasoning field ends up in `AuditorOutput`, which is logged to MLflow/Firestore.
- **Fix:** Use `last_error_repr` in the reasoning string:
  ```python
  reasoning=f"Audit failed with error: {last_error_repr}",
  ```

---

## 3. Low Priority / Polish

### 3.1. `identity.py` uses raw dict indexing and can raise `KeyError`

- **File:** `vor_agents/identity.py:68-73`
- **Current:** `pattern_identity_key` indexes `alert["detection_rule_id"]` etc.
- **Problem:** The docstring says this is intentional because callers should validate first, and HTTP callers do via `ClassifierRequest`. But internal callers (e.g. `scripts/seed_firestore.py`, `enrichment.record_confirmed_negative`, `tests`) sometimes pass plain dicts. A missing key surfaces as a raw `KeyError`, not the project's own `MalformedAlertError`. This is inconsistent with the "never surface raw exceptions" standard.
- **Fix:** Convert missing identity fields into `MalformedAlertError` (or a dedicated exception) with the missing field names listed. Update the docstring. Keep the function pure/deterministic.

### 3.2. `evidence_diversity_score` can raise on non-hashable host/user values

- **File:** `vor_agents/evidence_diversity.py:35-38`
- **Current:** `values = {inst[dim] for inst in confirmed_instances if dim in inst}`
- **Problem:** If a bad ingestion pipeline stores a list or dict in `host` or `user`, this set comprehension raises `TypeError`. The function is otherwise designed to degrade gracefully.
- **Fix:** Defensive stringification before adding to the set, or catch `TypeError` and skip that value with a warning.

### 3.3. `_deviation_field_names` silently drops deviation strings without a colon

- **File:** `vor_agents/orchestrator.py:180-209`
- **Current:** Deviation strings that don't match `"field: ..."` are logged and ignored.
- **Problem:** If the model reports a real deviation in a free-form sentence (e.g. `"integrity_level observed High instead of Medium"`), the reconciliation treats it as "not reported" and could override a correct SUPPRESS to ESCALATE, or miss a model-reported deviation. This is the safe direction for the former case, but the asymmetry is not obvious from logs.
- **Fix:** Consider a stricter schema for `structural_deviations_found` (a list of `{field, template, observed}` objects rather than free strings). That would remove parsing ambiguity entirely and is more robust than string heuristics.

### 3.4. Docs/code drift in `AGENT_DATA_FLOW.md`

- **File:** `docs/AGENT_DATA_FLOW.md:102-106`
- **Current:** Status section says "`needs_attention` and the MLflow tracing branch are specced and planned ... but not yet built."
- **Problem:** Both `needs_attention` (see `review_flag.py`, `orchestrator.py`) and the MLflow tracing fallback/replay path (see `vor_agents/tracing.py`, `main.py /replay-traces`) are implemented and tested. The status paragraph is out of date.
- **Fix:** Update the status paragraph to say these are implemented and list the endpoints/collections that exercise them.

### 3.5. `DESIGN_DECISIONS.md` already warns it is historical

- **File:** `docs/DESIGN_DECISIONS.md:12-16`
- **Current:** Header notes it is a historical record and parts are out of date.
- **Problem:** The repo still links to it from `README.md` as one of the documentation files. The out-of-date content (e.g. "`docs/TODO-Aug24.md` Task 8" references, old model defaults, old Pub/Sub status) is fine if clearly labeled, but the `README.md` table should perhaps note that `DESIGN_DECISIONS.md` is archived/historical.
- **Fix:** Add "(archived/historical)" to the `README.md` table entry for `DESIGN_DECISIONS.md`.

### 3.6. `__pycache__` and local artifacts exist in the working tree

- **Observation:** `__pycache__/` and `.pytest_cache/` directories are visible in `git status` outputs (they are usually gitignored here, but a check showed `.ruff_cache`, `.mypy_cache`, `.pytest_cache` in the repo listing). `.gitignore` already covers them, so this is a cleanup reminder, not a bug.
- **Fix:** `git clean -fdX` or `pre-commit` auto-cleanup before final commit. Ensure nothing is committed.

---

## 4. Test Gaps

| Area | What's missing | Why it matters |
|------|----------------|----------------|
| **Default model validity** | No test asserts `DEFAULT_GEMINI_MODEL` is a real model string. | Prevents deploying with a broken default (Issue 1.1). |
| **Blast-radius update semantics** | No test re-commits the same indicator with a different score and verifies the new score wins against real Firestore merge behavior. | The fake diverges from real Firestore (Issue 1.2). |
| **Malformed `last_reviewed_at`** | No test corrupts that field and asserts the sweep survives. | One bad doc kills the weekly safety net (Issue 1.3). |
| **`enrich()` days default** | No test asserts the sentinel value for a never-audited pattern. | Logs/prompt context would be misleading (Issue 2.1). |
| **Trace replay pagination** | No test with a large queue asserts bounded reads. | OOM risk during MLflow outage (Issue 2.2). |
| **Large alert storage** | No test bounds the size of stored `confirmed_instances`. | Firestore 1 MiB limit risk (Issue 2.3). |
| **`pattern_identity_key` on bad dict** | No test asserts a project-specific exception instead of `KeyError`. | Inconsistent error-handling standard (Issue 3.1). |
| **End-to-end `/audit` with real `audit_pattern`** | `test_main.py` mostly mocks `audit_pattern`. The `test_orchestrator.py` integration path tests it, but not through the HTTP layer. | Catches drift between `task_queue.py` body shape and `AuditRequest` (partially covered by `test_enqueued_task_body_parses_back_into_an_audit_request`). |

---

## 5. Security / Operational Observations

- **No secrets in code:** all secrets are env-driven; `.env` is gitignored. Good.
- **No `--allow-unauthenticated` in deploy docs:** consistently warned against. Good.
- **OIDC tokens with explicit audience:** `task_queue.py` sets `audience=audit_url`. Good.
- **Cloud Tasks dedup prevents duplicate concurrent audits:** good replacement for the old `under_review` app-level guard.
- **Pub/Sub dead-letter topic:** already listed as a known gap in `README.md` and `DEPLOY.md`. Not a code bug, but a real production gap.
- **`needs_attention` alerting:** already listed as a known gap. Not a code bug.
- **Broad `except Exception` blocks:** all are `# noqa: BLE001` with explicit docstrings justifying why the catch-all is the safe degradation path. This is intentional and consistent with the "force the safe outcome" design philosophy.

---

## 6. Outstanding Technical Decisions / Clarifications Needed

1. **Which model should be the default?** `gemini-2.0-flash` is referenced in several places (`orchestrator.py:133`, `tests/test_agents.py`, `DESIGN_DECISIONS.md:186`) as the expected model, but `model_config.py` and `.env.example` say `gemini-3.5-flash`. Pick one real model, update all references, and add a regression test.
2. **Should `blast_radius_table` entries be fully overwritable or versioned?** If you want an audit trail of score changes, the current `set(merge=True)` is the wrong tool anyway. Decide whether to (a) overwrite, (b) write new docs with timestamps and keep history, or (c) use `update()` on specific fields. The playbook currently reads as if the live table is authoritative.
3. **What is the maximum size/field set for stored `confirmed_instances`?** If the answer is "store the whole alert," the 1 MiB Firestore limit needs explicit handling. If the answer is "store only fields Vör uses," the code should enforce that.
4. **Should `structural_deviations_found` remain a list of free strings?** A structured object would make the reconciliation in `orchestrator.py` robust and remove the colon-prefix heuristic. Changing the schema is a breaking API change for any downstream consumer of `ClassifierOutput`, so this needs an explicit decision.
5. **How stale may `last_reviewed_at` become before the sweep no longer trusts it?** Currently `days_since_last_review` is unbounded and just adds to priority. There is no "older than X days, force re-audit every Y" policy. Worth calibrating once real volume exists, but the design intent should be documented.
6. **Is the in-memory `InMemorySessionService` acceptable for production?** The code comments say "swap for a persistent SessionService in production; fine for a hackathon demo." If this repo is moving toward production readiness, that decision should be tracked in `TODO-Aug24.md` or `DEPLOY.md` with a concrete replacement plan.
7. **Should `pending_traces` have a TTL or max-age policy?** The doc notes unbounded growth. A Firestore TTL index on `queued_at` is a one-line deployment addition but needs an explicit decision.

---

## 7. Summary of Recommended Actions (in priority order)

1. Fix `DEFAULT_GEMINI_MODEL` to a real model string and update docs/tests.
2. Change `_commit_indicators` from `set(merge=True)` to an explicit overwrite and add a regression test for re-scoring.
3. Harden `_fetch_all_confirmed_patterns` against malformed `last_reviewed_at` and add a regression test.
4. Align `enrich()`'s default `days_since_last_review` with the sweep sentinel.
5. Bound `replay_pending_traces` with a query limit and consider a TTL.
6. Restrict `confirmed_instances` to the fields Vör actually uses (or add size validation).
7. Use the truncated `last_error_repr` consistently in `AuditorOutput.reasoning`.
8. Convert `pattern_identity_key` missing-field errors to `MalformedAlertError`.
9. Make `evidence_diversity_score` robust to non-hashable values.
10. Update `AGENT_DATA_FLOW.md` status section and `README.md` doc table for `DESIGN_DECISIONS.md`.
11. Add the test gaps listed in Section 4.
12. Record the outstanding decisions from Section 6 in `docs/TODO-Aug24.md` or a new design note.

---

*Report generated on 2026-08-25. Test suite: 288 passed, 4 deselected. Ruff/Black/mypy/bandit all clean.*
