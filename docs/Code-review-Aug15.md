# Vör — Code Review Report

**Date:** 2026-08-15  
**Scope:** Full repository (`vor_agents/`, `main.py`, tests, docs, deployment/config files)  
**Method:** Static analysis, test execution, targeted runtime probes, lint/type/security checks.  

---

## Executive Summary

The codebase is well-architected, thoroughly tested for the deterministic layer, and the core safety story (asymmetric reconciliation, two-part graduation gate, targeted invalidation, blast-radius conservatism) is clearly implemented. However, several bugs and gaps remain that could cause production incidents: **audit failures can permanently lock patterns under review**, **identity-key round-tripping is broken by design**, **model non-compliance during active reviews is not enforced in code**, and **CI/deployment tooling is out of sync with the documented Python version**. This report lists findings by priority with concrete fixes.

---

## Findings by Priority

### 🔴 Critical

#### 1. `audit_pattern()` does not clear `under_review` on failure, permanently blocking a pattern

- **Location:** `vor_agents/orchestrator.py:179-217` (`audit_pattern`)
- **Problem:** `mark_under_review()` is called *before* the LLM call and `clear_under_review()` is only called *after* a successful model response and validation. If the model returns malformed JSON, the auditor emits an invalid enum value, Firestore is unavailable, or any other exception is raised, `clear_under_review()` never runs and `under_review` stays `True`. Because the classifier prompt tells the model to treat `under_review` as provisional, a failed audit effectively disables autonomous suppression for that pattern until a human manually clears the flag.
- **Evidence:** Runtime probe confirmed that raising `RuntimeError` in `_run_agent` leaves `under_review=True`. Cloud Tasks will retry and fail again until `max-attempts` is exhausted; the flag still remains stuck.
- **Impact:** High — operational denial of autonomous triage for a pattern, likely requiring manual Firestore intervention.
- **Recommended fix:** Wrap the audit body in `try/finally` so the flag is always cleared. Store the outcome/error in the document so a failed audit is still visible and `last_reviewed_at` is stamped, preventing the pattern from being immediately re-prioritized by the sweep.
  ```python
  async def audit_pattern(identity_key, pattern_data, firestore_client) -> AuditorOutput:
      mark_under_review(identity_key, firestore_client)
      try:
          doc = firestore_client.collection(CONFIDENCE_COLLECTION).document(_doc_id(identity_key)).get()
          instances = doc.to_dict().get("confirmed_instances", []) if doc.exists else []
          prompt = (...)
          auditor = build_auditor_agent()
          result = await _run_agent(auditor, prompt, session_id=f"audit_{'_'.join(identity_key)}_{uuid.uuid4()}")
          decision = AuditorOutput.model_validate(result)
      except Exception as exc:
          logger.bind(identity_key=identity_key).exception("Audit failed")
          # Force a no-op decision but still clear the flag and record the failure.
          decision = AuditorOutput(
              action=AuditorAction.NO_ACTION,
              reasoning=f"Audit failed with error: {exc!r}",
          )
      finally:
          clear_under_review(identity_key, firestore_client, decision.model_dump())
      return decision
  ```
- **Tests to add:**
  - `_run_agent` raises → `under_review` becomes `False` and `last_reviewed_at` is stamped.
  - Invalid model output enum → same graceful degradation.

---

#### 2. `_run_agent()` can crash with `json.JSONDecodeError` and is not wrapped

- **Location:** `vor_agents/orchestrator.py:31-48`
- **Problem:** The function concatenates `part.text` from the model and calls `json.loads(result_text)` with no error handling. If the model returns empty output, Markdown fences, or any non-JSON text, a `JSONDecodeError` propagates and triggers finding #1 (stuck `under_review`) or a 500 on `/classify`.
- **Evidence:** Static analysis; runtime probe shows `_run_agent` without API key fails before JSON parsing, but the parsing path itself is unprotected.
- **Impact:** High — same cascading failure as #1 for audits; also surfaces raw exceptions to callers.
- **Recommended fix:** Wrap JSON parsing and model iteration in a try/except, raising a project-specific exception (e.g. `AgentOutputError`) with context. In `audit_pattern`, treat it as a failed audit; in `classify_alert`, map it to `UNCERTAIN` with `reasoning` explaining the parsing failure rather than a 500.
  ```python
  try:
      return json.loads(result_text)
  except json.JSONDecodeError as exc:
      raise AgentOutputError(
          f"Model did not return valid JSON (length={len(result_text)}): {exc}"
      ) from exc
  ```

---

#### 3. Identity-key round-trip is broken for any component containing an underscore

- **Location:** `vor_agents/enrichment.py:16-17` (`_doc_id`) and `vor_agents/orchestrator.py:294` (`tuple(doc.id.split("_"))`)
- **Problem:** `_doc_id` joins key components with `"_"`; `_fetch_all_suppressed_patterns` reconstructs the tuple by splitting on `"_"`. Any component containing an underscore (common in rule IDs, process names, or endpoint families) yields the wrong tuple length. This also causes **doc-ID collisions**: `("a", "b_c")`, `("a_b", "c")`, and `("a", "b", "c")` all map to `a_b_c`.
- **Evidence:** Runtime probe showed `("Sigma_Rule_123", "proc.exe", "child.exe", "family")` reconstructed as 7-tuple. The repo already has a strict `xfail` test for this (`tests/test_known_gaps.py`).
- **Impact:** Critical for correctness — scheduled sweeps will target the wrong identity key (or fail to find the doc), and Cloud Tasks task names derived from the same join are also ambiguous (`task_queue.py` hashes the joined string, so distinct keys collide).
- **Recommended fix:** Store the identity-key tuple as a first-class array field in Firestore and stop encoding it into the doc ID. Add a migration step for existing docs.
  ```python
  def _doc_id(identity_key: tuple) -> str:
      # Use a non-reversible, collision-resistant hash for the doc ID.
      return hashlib.sha256(json.dumps(identity_key, separators=(",", ":")).encode()).hexdigest()
  ```
  Or keep a human-readable ID but encode each component with percent/JSON escaping. At minimum, read the stored `identity_key` field instead of splitting `doc.id`.

---

### 🟠 High

#### 4. `classify_alert()` does not deterministically enforce `under_review` → `UNCERTAIN`

- **Location:** `vor_agents/orchestrator.py:65-176` (`classify_alert`)
- **Problem:** The classifier prompt tells the model to treat `under_review=True` as provisional, but the orchestrator never checks it after the model response. If a non-compliant/hallucinating model returns `SUPPRESS` for a pattern currently under audit, the orchestrator accepts it (assuming no ground-truth deviations). This leaves the burst-replay race open to model failure rather than closed in code.
- **Evidence:** Runtime probe with `under_review=True` and a mocked `SUPPRESS` response returned `SUPPRESS` without override.
- **Impact:** High — the exact race condition `under_review` was designed to prevent can be violated by model non-compliance.
- **Recommended fix:** After receiving the model output, if `enrichment["under_review"]` is `True` and the model emitted `SUPPRESS`, override to `UNCERTAIN` with `uncertain_reason="under_review"`, regardless of whether deviations exist.
  ```python
  if enrichment.get("under_review") and classifier_output.decision == Decision.SUPPRESS:
      classifier_output = classifier_output.model_copy(update={
          "decision": "UNCERTAIN",
          "uncertain_reason": "under_review",
          "reasoning": classifier_output.reasoning + " [Vör correctness override: pattern is under active audit; SUPPRESS not allowed until review completes.]",
      })
  ```
- **Tests to add:** Model returns `SUPPRESS` while `under_review=True` → `UNCERTAIN` with `under_review` reason.

---

#### 5. `record_confirmed_negative()` accepts but ignores `human_confirmed`

- **Location:** `vor_agents/enrichment.py:65-108`
- **Problem:** The function signature includes `human_confirmed: bool = True`, but the value is never used. The docstring says it is called by a human or a seed batch, implying different provenance semantics, yet everything is written as `provenance="live"` regardless.
- **Impact:** Medium — misleading API; bulk imports or programmatic confirmations cannot be distinguished from per-alert human approvals. This weakens the auditor's ability to flag "absence of complaint is not confirmation" for unverified instances.
- **Recommended fix:** Use `human_confirmed` to set provenance or a new verification flag:
  ```python
  provenance = "human_confirmed" if human_confirmed else "bulk_imported"
  template = build_structural_template(instances, provenance=provenance)
  ```
  Update tests and the classifier/auditor prompts to explain the new provenance values, or at minimum remove the unused parameter if it has no meaning.

---

#### 6. `/classify` does not validate the incoming alert and exposes raw `KeyError`

- **Location:** `main.py:100-109`
- **Problem:** `request.json()` is awaited and passed straight to `classify_alert`. If the alert is missing any identity field (`detection_rule_id`, `parent_image`, `child_image`, `endpoint_family`), `pattern_identity_key` raises a raw `KeyError` that becomes a 500.
- **Evidence:** Runtime probe with `{"detection_rule_id": "r"}` raised `KeyError: 'parent_image'`.
- **Impact:** Medium — callers receive 500 for malformed input instead of a structured 422, and the error is not logged with context.
- **Recommended fix:** Define a `ClassifierRequest` Pydantic model with the four required identity fields and all optional diffable/context fields. Use it as the endpoint parameter (FastAPI will return 422 for missing required fields). Alternatively, validate explicitly and raise a project-specific `MalformedAlertError` that is caught and returned as 400/422.

---

#### 7. `/classify` does not handle invalid JSON body

- **Location:** `main.py:102` (`await request.json()`)
- **Problem:** If the request body is not valid JSON, `request.json()` raises `json.decoder.JSONDecodeError`, which is not caught. The endpoint returns an unhandled 500.
- **Evidence:** Runtime probe with `b"not json"` raised `JSONDecodeError`.
- **Impact:** Medium — webhook callers sending bad payloads will see 500s and retry.
- **Recommended fix:** Add an exception handler for `json.JSONDecodeError` (and possibly a custom `MalformedAlertError`) that returns 400 with a concise error body. FastAPI’s built-in `RequestValidationError` handler already covers Pydantic models, so switching to a model input also fixes this.

---

#### 8. `/audit` has no error handling for `audit_pattern` failures

- **Location:** `main.py:126-144`
- **Problem:** Any exception inside `audit_pattern` (Firestore unavailable, model failure, validation error) propagates as a 500. While Cloud Tasks retries on 5xx, this is indistinguishable from transient vs. permanent failures and compounds finding #1.
- **Impact:** Medium — wastes Cloud Tasks retry budget and leaves patterns stuck under review.
- **Recommended fix:** The fix should primarily live in `audit_pattern` (see #1). In `main.py`, add a top-level exception handler that logs context and returns a 500 only for truly retryable errors; for permanent data errors return 422/400 so Cloud Tasks stops retrying. Ensure `under_review` is always cleared in `audit_pattern` so the HTTP status is decoupled from the flag state.

---

#### 9. `_deviation_field_names()` silently treats malformed deviation strings as valid field names

- **Location:** `vor_agents/orchestrator.py:51-62`
- **Problem:** The helper splits each deviation string on the first colon. If the model omits the colon or formats the string differently (e.g., `"integrity_level observed High instead of Medium"`), the entire string is treated as a field name. This can cause false negatives in the ground-truth reconciliation: a real deviation reported by the model in an unexpected format will not match the deterministic field name, and the "missed by model" override may fire incorrectly.
- **Evidence:** Runtime probe returned `{"integrity_level observed High instead of Medium"}` for a colon-less string.
- **Impact:** Medium — fragile reconciliation that depends on model formatting.
- **Recommended fix:** Parse more defensively. If a string has no colon, log a warning and skip it (or attempt fuzzy matching only after logging). Better yet, have the deterministic diff and the model both return structured data (list of `{field, expected, observed}`) instead of relying on string parsing.
  ```python
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

---

#### 10. `evidence_diversity_score()` extracts "hours" from arbitrary strings without ISO validation

- **Location:** `vor_agents/evidence_diversity.py:37-43`
- **Problem:** The function slices `timestamp[11:13]` whenever `len(timestamp) >= 13`, regardless of format. A timestamp like `"2026-08-01T99:00:00Z"` yields hour `99`, which is nonsensical but counted as a distinct hour.
- **Impact:** Low-Medium — diversity scores can be artificially inflated or deflated by malformed ingestion timestamps.
- **Recommended fix:** Parse with `datetime.fromisoformat` (or `datetime.strptime` with fallback) and only use the hour component if parsing succeeds. Handle `Z` suffix.
  ```python
  from datetime import datetime
  hours = set()
  for inst in confirmed_instances:
      ts = inst.get("timestamp")
      try:
          dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
          hours.add(f"{dt.hour:02d}")
      except (ValueError, TypeError, AttributeError):
          continue
  if hours:
      ratios.append(min(len(hours) / n, 1.0))
  ```

---

### 🟡 Medium

#### 11. `select_audit_targets()` priority formula is unbounded and can produce negative priorities

- **Location:** `vor_agents/audit_targets.py:10-27`
- **Problem:** `days_since_last_review` is unbounded. A pattern with a future `last_reviewed_at` (clock skew) produces a negative `days_since_last_review`, making its priority lower than it should be. Also, ties are broken only by Python sort stability, so selection is not deterministic across process restarts.
- **Impact:** Low-Medium — audit selection may ignore skewed or just-reviewed high-risk patterns; non-deterministic tie-breaking reduces reproducibility.
- **Recommended fix:**
  - Clamp `days_since_last_review` to `max(0, ...)` in `_fetch_all_suppressed_patterns`.
  - Add a deterministic tie-breaker in `select_audit_targets`, e.g. `identity_key` string.
  ```python
  def priority(pattern):
      return (
          max(pattern["days_since_last_review"], 0) * 1.0
          + (1.0 - pattern["evidence_diversity_score"]) * 2.0
          + pattern["blast_radius_estimate"] * 3.0,
          str(pattern["identity_key"]),
      )
  return sorted(all_suppressed_patterns, key=priority, reverse=True)[:max_targets]
  ```

---

#### 12. `propose_blast_radius()` does not validate tier or score

- **Location:** `vor_agents/blast_radius.py:58-80`
- **Problem:** The function accepts any `proposed_tier` string and any `proposed_score`. An unknown tier returns `requires_review=False`, and a tier/score mismatch (e.g. `LOW` with score `CRITICAL`) is silently accepted.
- **Impact:** Low-Medium — weak API contract; downstream human review may be misled.
- **Recommended fix:** Validate against the known tier set and the documented score ranges. Raise `ValueError` for unknown tiers or out-of-range scores. For `MEDIUM`/`LOW`, always require review; for `CRITICAL`/`HIGH`, optionally require review only if the score is out of range.
  ```python
  if proposed_tier not in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
      raise ValueError(f"Unknown blast-radius tier: {proposed_tier}")
  # Optional: validate score is within tier range per BLAST_RADIUS_PLAYBOOK.md
  ```

---

#### 13. `_fetch_all_suppressed_patterns()` filters by `tier == "confirmed"` but `run_scheduled_sweep()` doc says "suppressed patterns"

- **Location:** `vor_agents/orchestrator.py:255-305`
- **Problem:** The function name and docstring refer to "suppressed patterns," but it queries only `tier == "confirmed"`. Provisional patterns that are suppressed (which should never happen per the classifier rules, but could exist via seeding or prior data) are never swept. This is mostly a naming/documentation mismatch, but it means the scheduled sweep does not revisit provisional patterns even if they have alerts.
- **Impact:** Low — mostly semantic, but could hide data-quality issues.
- **Recommended fix:** Rename to `_fetch_all_confirmed_patterns()` or document explicitly that only confirmed-tier patterns are swept. If provisional patterns should also be swept, extend the query and adjust priority scoring.

---

#### 14. `confidence_used` field in `ClassifierOutput` is dead code

- **Location:** `vor_agents/schemas.py:30-46`
- **Problem:** `confidence_used` is defined and included in tests/fakes, but never populated from model output or used in any decision logic. It adds API surface area without purpose.
- **Impact:** Low — confusion for API consumers; unused schema surface.
- **Recommended fix:** Either remove the field (and all fake/test references), or implement a real confidence extraction from the model and use it for observability/alerting. Given the project's decision to avoid a separate confidence float, removal is likely the right call.

---

#### 15. `task_queue.py` uses SHA-1 and triggers a Bandit high-severity warning

- **Location:** `vor_agents/task_queue.py:43`
- **Problem:** `hashlib.sha1(...)` is used only for deterministic task naming, not security, but Bandit flags it as a high-severity issue (B324). The existing code is functionally correct for naming/dedup, but CI/security gates may fail.
- **Impact:** Low for functionality; high for CI/security hygiene.
- **Recommended fix:** Add `usedforsecurity=False` to signal intent.
  ```python
  key_hash = hashlib.sha1("_".join(identity_key).encode(), usedforsecurity=False).hexdigest()
  ```

---

### 🟢 Low / Documentation & Tooling

#### 16. Python version mismatch between CLAUDE.md, Dockerfile, and CI

- **Location:** `CLAUDE.md`, `Dockerfile`, `.github/workflows/ci.yml`
- **Problem:** CLAUDE.md states **Python 3.13**. Dockerfile uses `python:3.12-slim`. CI uses `python-version: "3.12"`.
- **Impact:** Low — tests may pass on 3.12 but runtime/deployment docs are inconsistent.
- **Recommended fix:** Align all three on 3.13 (or downgrade CLAUDE.md if 3.12 is intentional). Update Dockerfile to `python:3.13-slim` and CI to `python-version: "3.13"`.

---

#### 17. No pre-commit config despite CLAUDE.md instructions

- **Location:** project root, `CLAUDE.md`
- **Problem:** CLAUDE.md tells developers to run `pre-commit run --all-files`, but `.pre-commit-config.yaml` does not exist. CI also only runs `ruff`, not `mypy`, `black`, or `bandit`.
- **Impact:** Low — style/type/security drift likely in future commits.
- **Recommended fix:** Add `.pre-commit-config.yaml` with `ruff`, `black`, `mypy --strict`, and `bandit`. Update CI to run the same checks.

---

#### 18. `requirements.txt` has unpinned dependencies

- **Location:** `requirements.txt`
- **Problem:** All packages are unpinned (e.g. `google-adk`, `fastapi`). A future breaking change in `google-adk` or `pydantic` could break deployment or CI.
- **Impact:** Low-Medium — reproducibility risk.
- **Recommended fix:** Pin versions and add a lock file (`requirements-lock.txt` or `uv.lock`). Use `pip-compile` or `uv pip compile`.

---

#### 19. `mypy` reports an error in `_run_agent` under default settings

- **Location:** `vor_agents/orchestrator.py:47`
- **Problem:** `result_text += part.text` is flagged because `part.text` is typed as `str | None`. Under `mypy --strict` this would fail.
- **Impact:** Low — CI type checking not currently enabled, but future enablement will fail.
- **Recommended fix:**
  ```python
  text = getattr(part, "text", None)
  if text:
      result_text += text
  ```

---

#### 20. `black` would reformat 16 source/test files

- **Location:** multiple files
- **Problem:** Running `black .` would reformat the majority of the codebase, indicating it is not part of the current CI/formatting workflow.
- **Impact:** Low — style inconsistency.
- **Recommended fix:** Run `black .` once, add a `black` check to CI/pre-commit, and enforce it going forward.

---

## Test Gaps Summary

| Missing Coverage | Suggested Test |
|---|---|
| Audit failure clears `under_review` | `test_audit_failure_clears_flag` |
| Model output is not valid JSON | `test_run_agent_bad_json` |
| `under_review=True` deterministically blocks `SUPPRESS` | `test_suppress_blocked_when_under_review` |
| Invalid alert payload on `/classify` returns 422 | `test_classify_rejects_missing_fields` |
| Invalid JSON body on `/classify` returns 400/422 | `test_classify_rejects_invalid_json` |
| `/classify` does not enqueue for `ESCALATE`/`UNCERTAIN` | `test_classify_no_enqueue_on_non_suppress` |
| `propose_blast_radius` rejects unknown tier | `test_propose_blast_radius_rejects_unknown_tier` |
| `evidence_diversity_score` rejects/ignores bad timestamps | `test_evidence_diversity_invalid_timestamp` |
| `record_confirmed_negative(human_confirmed=False)` behavior | clarify or remove parameter |
| Task body content (URL, OIDC, identity_key list) | `test_enqueued_task_body_shape` |
| `/sweep` behavior when task env vars are missing | `test_sweep_returns_result_if_enqueue_misconfigured` |

---

## Documentation / Implementation Divergence

1. **Graduation criteria mismatch.** `classifier_agent.py` prompt says `provisional` means "fewer than 3 confirmed instances" and `confirmed` means "3 or more." The actual implementation requires both `instance_count >= 3` **and** `diversity_score >= 0.5` (`identity.py:115`). The prompt should be updated to reflect the two-part gate.
2. **No trigger source for `/classify`.** README and DEPLOY.md correctly note this is unresolved, but it is a real production gap.
3. **No dataset generation / seeding script.** README "Not yet built" section lists these as open. They remain unimplemented.
4. **No integration suite.** TESTING_PLAN.md says real Gemini calls should be a separate `@pytest.mark.integration` suite; no such suite exists.
5. **Deployment plan not executed.** DEPLOY.md says none of the gcloud commands have been run.

---

## Outstanding Technical Decisions / Clarifications Needed

1. **Should provisional-tier patterns be included in the scheduled sweep?** Currently only `confirmed` patterns are swept. If provisional patterns can be "suppressed" via seeding or future changes, the sweep will miss them.
2. **What should happen to `human_confirmed`?** The parameter is unused. Decide whether to remove it, use it to set provenance, or add a separate `verified_by` field.
3. **What is the intended model provider?** CLAUDE.md and README say "meant to be run in Google Cloud," but the agents default to the Gemini API (`gemini-2.0-flash`) with no Vertex AI/project/region configuration. Decide whether to configure Vertex AI or document that API keys are expected.
4. **How should permanently failed audits be surfaced?** The current design relies on Cloud Tasks retries. If an audit fails repeatedly and clears the flag (after fix #1), should the failure be escalated to a human or logged only?
5. **Confidence representation.** `confidence_used` is dead. Is there any plan to use model confidence for observability, or should it be removed?
6. **Blast radius table maintenance.** `propose_blast_radius` returns inert records. Is there a planned workflow to commit proposals, or is this intentionally manual forever?

---

## Verification Commands Run

```bash
.venv/bin/python -m pytest -q          # 71 passed, 1 xfailed
.venv/bin/python -m ruff check .        # all checks passed
.venv/bin/python -m mypy vor_agents/ main.py tests/  # 1 error: orchestrator.py:47
.venv/bin/python -m bandit -r vor_agents/ main.py    # 1 high: SHA-1 in task_queue.py
.venv/bin/python -m black --check .     # 16 files would be reformatted
```
