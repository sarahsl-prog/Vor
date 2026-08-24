# Vör — Fix To-Do List (from Code-review-Aug15.md)

**Status:** All 20 findings in `Code-review-Aug15.md` validated against current source (2026-08-15). None stale. Order below = fix order (critical → low), per CLAUDE.md's one-task-at-a-time rule.

**Update (2026-08-24):** All 20 tasks now ✅ DONE. Only Task 21 (self-surfaced during Task 13, not in the original report) and the "Outstanding decisions" items below remain open.

---

## 🔴 Critical — fix first

### Task 1 — Wrap `audit_pattern()` body in try/finally ✅ DONE
- [x] `vor_agents/orchestrator.py:179-217` — wrap body between `mark_under_review()` and `clear_under_review()` in try/except/finally per report's snippet.
- [x] On exception: log with `logger.exception`, build `AuditorOutput(action=NO_ACTION, reasoning=f"Audit failed: {exc!r}")`.
- [x] `finally`: always call `clear_under_review(identity_key, firestore_client, decision.model_dump())`.
- [x] Add test: `_run_agent` raises → `under_review` becomes `False`, `last_reviewed_at` stamped.
- [x] Add test: invalid model output enum → same graceful degradation.
- [x] Run: `pytest tests/ -k audit -v` — 2 new tests pass, full suite 73 passed/1 xfailed (was 71/1).
- [x] ruff clean. mypy/bandit/black show only pre-existing unrelated findings (Tasks 19/15/20).
- [x] Commit: `6b3f85a` — `Clear under_review flag on audit failure via try/finally` on branch `fix/audit-under-review-flag`

### Task 2 — Wrap JSON parsing in `_run_agent()` ✅ DONE
- [x] `vor_agents/orchestrator.py` — defined `AgentOutputError` locally in `orchestrator.py` (matches project convention: `MalformedAlertError` in `identity.py`, `AuditEnqueueError` in `task_queue.py` are both local to their raising module).
- [x] Wrapped `json.loads(result_text)` in try/except, raises `AgentOutputError` with context (length) on `JSONDecodeError`.
- [x] `audit_pattern`: no code change needed here — Task 1's broad `except Exception` (on branch `fix/audit-under-review-flag`, not yet merged to this branch) already catches `AgentOutputError` as a subclass; confirmed by inspection, not re-implemented to keep this commit scoped.
- [x] `classify_alert`: catches `AgentOutputError`, returns `ClassifierOutput(decision=UNCERTAIN, uncertain_reason=MISSING_DATA, reasoning=...)` instead of letting it 500 at `/classify`.
- [x] Added test: `test_run_agent_bad_json_raises_agent_output_error` (unit-level, fakes `Runner`) + `test_classify_alert_degrades_to_uncertain_on_unparseable_output` (integration-level).
- [x] Suite: 73 passed/1 xfailed. ruff clean.
- [x] Commit: `d9d70fb` — `Wrap _run_agent JSON parsing, add AgentOutputError` on branch `fix/run-agent-json-parsing`

### Task 3 — Fix identity-key round-trip / doc-ID collisions ✅ DONE
- [x] `vor_agents/enrichment.py` (`_doc_id`) — switched to `hashlib.sha256` of the JSON-encoded identity_key tuple.
- [x] `record_confirmed_negative` / `seed_template` — both now write `identity_key: list(identity_key)` as a first-class field alongside the doc.
- [x] `vor_agents/orchestrator.py` (`_fetch_all_suppressed_patterns`) — reads `data.get("identity_key")` instead of `tuple(doc.id.split("_"))`. Docs missing the field are skipped with a `logger.warning`, not crashed.
- [x] `vor_agents/task_queue.py` (`_task_name`) — folded in here rather than as separate Task 15: same root-cause collision (`"_".join(identity_key)` before hashing), same fix shape (hash the JSON-encoded tuple instead). Also added `usedforsecurity=False`, resolving the Bandit B324 finding as a side effect.
- [x] **Migration note (not executed, no real data exists yet):** DEPLOY.md confirms no gcloud commands have been run — there is no live Firestore data to migrate. If/when this ships against a real `confidence_docs` collection that predates this fix, those docs will lack `identity_key` and get silently skipped by every sweep until backfilled. **Flagging this now, don't defer it** — write a one-time backfill script before first production deploy.
- [x] `tests/test_known_gaps.py` — flipped from `xfail(strict=True)` to a normal passing test (rewritten to check the actual collision case: `("a","b_c")` vs `("a_b","c")` vs `("a","b","c")` now produce distinct doc IDs).
- [x] `tests/test_orchestrator.py::TestRunScheduledSweep` — the underscore-avoidance workaround comment removed; that test's `detection_rule_id` now deliberately contains underscores (`Test_Rule_With_Underscores`) to prove the fix, plus a new test for the missing-field-skip path.
- [x] `README.md`'s "Known gaps" #1 updated to reflect the fix + the outstanding migration note.
- [x] Suite: 73 passed, 0 xfailed (was 71 passed/1 xfailed). ruff clean. bandit: **0 issues** (was 1 high).
- [x] Commit: `1153a8d` — `Store identity_key as Firestore field, hash doc IDs to fix round-trip` on branch `fix/identity-key-round-trip`

---

## 🟠 High

### Task 4 — Enforce `under_review` → `UNCERTAIN` in code ✅ DONE
- [x] `vor_agents/orchestrator.py` (`classify_alert`) — added override right after `ClassifierOutput.model_validate(result)`, before the other reconciliation blocks: if `enrichment.get("under_review")` and `decision == SUPPRESS` → force `UNCERTAIN` / `uncertain_reason=under_review`.
- [x] Added test: `test_suppress_overridden_to_uncertain_when_under_review` + a second test (`test_escalate_while_under_review_left_untouched`) confirming the override is scoped to SUPPRESS only, not a blanket override of every decision.
- [x] Suite: 73 passed/1 xfailed (was 71/1 — the xfail here is the not-yet-fixed identity-key gap from Task 3's branch, not merged into this one). ruff clean.
- [x] Commit: `bd1efb1` — `Deterministically block SUPPRESS while pattern is under_review` on branch `fix/under-review-blocks-suppress`

### Task 5 — Use or remove `human_confirmed` param ✅ DONE
- [x] Decision made (deviated from the report's literal snippet after inspection): **not** folded into `provenance` — `provenance` ("live"/"seeded") is doc-level and already means something else (single-alert vs batch entry path); overloading it with human/bulk would've conflicted with that and with `seed_template`'s existing "seeded" value. Instead added a new **per-instance** `verified_by` field ("human"/"bulk"). Per-instance, not doc-level, because `confirmed_instances` accumulates across many calls — a doc-level field would only reflect the most recent call and mislabel every earlier instance.
- [x] `vor_agents/enrichment.py` — `record_confirmed_negative` tags each appended instance `verified_by: "human" if human_confirmed else "bulk"`. `seed_template` tags every instance `"bulk"` (no per-alert human ever signed off on a seeded batch).
- [x] `vor_agents/auditor_agent.py` — updated the "never complained about (absence of complaint is not confirmation)" prompt bullet to reason over `verified_by` directly instead of inferring it.
- [x] Added 5 new tests across `test_enrichment.py`: human/bulk tagging for both write paths, plus the per-instance-not-clobbered regression (the reason this wasn't a doc-level field).
- [x] Suite: 83 passed (was 79). ruff/mypy/bandit all clean.
- [x] Commit: `2ac8608` — `Use human_confirmed to tag confirmed instances' provenance` on branch `fix/human-confirmed-provenance`, merged to `main`.

### Task 6 — Validate `/classify` request body with Pydantic ✅ DONE
- [x] `vor_agents/schemas.py` — added `ClassifierRequest` (4 required identity fields; `model_config = ConfigDict(extra="allow")` so DIFFABLE_FIELDS/host/user/timestamp/anything else pass through untouched, since downstream code reads a plain dict, not this model).
- [x] `main.py` — `/classify` now takes `payload: ClassifierRequest` instead of `request: Request` + manual `await request.json()`.
- [x] Confirmed FastAPI returns 422 on missing identity fields.
- [x] Added tests: `test_classify_rejects_missing_identity_fields`, `test_classify_allows_extra_context_fields` (proves `extra="allow"` actually passes extras through to `classify_alert`, not just accepts them).
- [x] Commit: `fd2067e` — `Validate /classify request body with Pydantic model` on branch `fix/classify-request-validation`, merged to `main`.

### Task 7 — Handle invalid JSON body on `/classify` ✅ DONE (resolved by Task 6, verified not re-implemented)
- [x] Verified, did not re-implement: sent raw non-JSON bytes at `/classify` in a test (`test_classify_rejects_invalid_json_body`) — FastAPI's own body-parsing layer (same mechanism already protecting `/audit`'s `AuditRequest`) returns 422 before any `main.py` code runs. No extra exception handler needed.
- [x] Same commit as Task 6 (`fd2067e`) — no separate commit, per the to-do's own "only if extra code needed" note.

### Task 8 — Error handling for `/audit` ✅ DONE
- [x] Confirmed Task 1 doesn't fully cover this: `mark_under_review()` (before `audit_pattern`'s try block) and the `invalidate_instances()` rebuild inside `clear_under_review()`'s `finally` block are both outside that try/except, so a Firestore write failure or malformed stored evidence can still escape `audit_pattern()`.
- [x] `main.py` — `/audit` now wraps the `audit_pattern()` call: `MalformedAlertError` → 422 (permanent, retrying won't help), everything else → 500 (retryable, let Cloud Tasks retry), both logged with `identity_key` context via `HTTPException`.
- [x] Added `test_audit_endpoint_returns_422_on_malformed_stored_data` + `test_audit_endpoint_returns_500_on_unexpected_failure`.
- [x] Added `test_sweep_returns_result_if_enqueue_misconfigured` — closes the last row of the report's Test Gaps table, exercised through the real `run_scheduled_sweep` → `_enqueue` path (not mocked) with an actual confirmed pattern in Firestore.
- [x] Suite: 89 passed (was 86). ruff/mypy/bandit all clean.
- [x] Commit: `90484ff` — `Add error handling to /audit endpoint` on branch `fix/audit-endpoint-error-handling`, merged to `main`.

### Task 9 — Defensive parsing in `_deviation_field_names()` ✅ DONE
- [x] `vor_agents/orchestrator.py` — colon-less strings now skipped with `logger.warning` instead of treated as a whole-string field name.
- [x] Longer-term structured-output idea (from report) still open, not pursued — bigger change, separate ticket if ever needed.
- [x] Added 3 unit tests: well-formed extraction, colon-less skip, mixed well-formed/malformed list.
- [x] Suite: 92 passed (was 89). ruff/mypy/bandit all clean.
- [x] Commit: `b784f64` — `Skip malformed deviation strings in _deviation_field_names` on branch `fix/deviation-field-name-parsing`, merged to `main`.

### Task 10 — ISO-validate timestamps in `evidence_diversity_score()` ✅ DONE
- [x] `vor_agents/evidence_diversity.py` — replaced `timestamp[11:13]` slice with `datetime.fromisoformat(ts.replace("Z", "+00:00"))`, guarded by `try/except (ValueError, TypeError)`, skipped on failure.
- [x] Added 2 tests: malformed timestamp (`"...T99:00:00Z"`) no longer counted as a distinct hour; mixed valid/malformed list only counts the valid ones.
- [x] Suite: 94 passed (was 92). ruff/mypy/bandit all clean.
- [x] Commit: `4d753f5` — `Validate timestamps before extracting hour in evidence_diversity_score` on branch `fix/evidence-diversity-timestamp-validation`, merged to `main`.

---

## 🟡 Medium

### Task 11 — Clamp/tie-break `select_audit_targets()` priority ✅ DONE
- [x] Clamped in both places (defense in depth): `select_audit_targets()`'s own `priority()` closure clamps `days_since_last_review` to `max(0, ...)`, and `_fetch_all_suppressed_patterns()` in `orchestrator.py` clamps at the source too — so the function's contract doesn't silently depend on every caller having already sanitized input.
- [x] Added deterministic tie-breaker (`str(identity_key)`) to the priority tuple.
- [x] Added 2 tests: clock-skew negative days doesn't rank below a genuinely-never-audited pattern; tied priority sorts identically regardless of input order.
- [x] Suite: 96 passed (was 94). ruff/mypy/bandit all clean.
- [x] Commit: `ee41492` — `Clamp days_since_last_review and add deterministic tie-break to audit priority` on branch `fix/audit-target-priority-clamp`, merged to `main`.

### Task 12 — Validate tier/score in `propose_blast_radius()` ✅ DONE
- [x] `vor_agents/blast_radius.py` — added `TIER_RANGES` dict sourced from `BLAST_RADIUS_PLAYBOOK.md`'s documented ranges (CRITICAL 0.90–1.0, HIGH 0.60–0.89, MEDIUM 0.30–0.59, LOW 0.0–0.29, inclusive both ends).
- [x] `propose_blast_radius()` now raises `ValueError` for an unknown tier, and separately for a `proposed_score` outside that tier's range.
- [x] Added 3 tests: unknown tier rejected, tier/score mismatch rejected, boundary value (0.90 for CRITICAL) accepted. All 4 pre-existing tests still pass unchanged (their scores already matched their tiers).
- [x] Suite: 99 passed (was 96). ruff/mypy/bandit all clean.
- [x] Commit: `de152c9` — `Validate tier/score in propose_blast_radius` on branch `fix/propose-blast-radius-validation`, merged to `main`.

### Task 13 — Rename/document `_fetch_all_suppressed_patterns()` ✅ DONE
- [x] Outstanding Decision #1 resolved by evidence before implementing: checked `classifier_agent.py`'s `CLASSIFIER_SYSTEM_PROMPT` rule 6 — provisional tier never autonomously SUPPRESSes (always UNCERTAIN), so the sweep has nothing to catch there. No query change needed.
- [x] Renamed to `_fetch_all_confirmed_patterns()` across `orchestrator.py`, `enrichment.py`, `audit_targets.py`, `README.md`, `tests/test_orchestrator.py` (comment only). Local variable `all_suppressed` → `all_confirmed` too.
- [x] Docstring now explains why confirmed-only is correct, not just renamed blind.
- [x] **New finding surfaced while checking this** (not in original report): provisional→UNCERTAIN is enforced only in the prompt, same unenforced-in-code shape as the `under_review`→UNCERTAIN gap Task 4 fixed. You said harden it — tracked separately below, not yet implemented.
- [x] Suite: 99 passed (unchanged, rename-only). ruff/mypy clean.
- [x] Commit: `3548bc9` — `Rename _fetch_all_suppressed_patterns to reflect confirmed-tier-only query` on branch `fix/rename-fetch-confirmed-patterns`, merged to `main`.

### Task 14 — Remove or wire up `confidence_used` ✅ DONE
- [x] User decision: remove. Discussed the actual downside first (API surface change for future consumers, re-add cost later) — both minor since nothing's deployed yet and the field carries zero information today (always `None`).
- [x] `vor_agents/schemas.py` — field removed from `ClassifierOutput`.
- [x] All fake/test references removed (`tests/test_main.py`, `tests/test_orchestrator.py` — 10 lines total).
- [x] Note found during this task: `schemas.py`'s module docstring says to keep it "in sync with agent_prompts.py by hand" — that file doesn't exist in the repo (stale/aspirational reference), so no second file needed updating.
- [x] Suite: 99 passed (unchanged — removal was dead-key cleanup only). ruff/mypy/bandit all clean.
- [x] Commit: `dfa965f` — `Remove unused confidence_used field from ClassifierOutput` on branch `fix/remove-confidence-used`, merged to `main`.

### Task 15 — SHA-1 `usedforsecurity=False` in `task_queue.py` ✅ DONE (folded into Task 3's commit `1153a8d`)
- [x] Done together with Task 3 as planned — same root cause, same commit. `bandit -r vor_agents/ main.py` — **0 issues** (was 1 high).

---

## 🟢 Low / Tooling & Docs

### Task 16 — Align Python version (3.13 everywhere) ✅ DONE
- [x] `.github/workflows/ci.yml` was already at 3.13 (landed earlier, in `3c38e6f`). Only `Dockerfile:1` still lagged at `python:3.12-slim`.
- [x] Decision: 3.13, per CLAUDE.md and the already-aligned CI — not a live ambiguity by the time this ran, so proceeded rather than re-blocking on a question CI's prior fix had already answered.
- [x] `Dockerfile` → `python:3.13-slim`.
- [x] Commit: `c41de63` — `Align Dockerfile to Python 3.13, matching CLAUDE.md and CI`

### Task 17 — Add `.pre-commit-config.yaml` ✅ DONE
- [x] Confirmed missing at repo root.
- [x] Added config with `ruff`, `black`, `mypy`, `bandit` hooks — **`language: system`** (not the usual hosted mirrors) so hooks run the exact tool versions pinned in `requirements-dev.txt`, guaranteeing pre-commit and CI can't drift apart. `mypy --strict` deliberately **not** used: `mypy --strict vor_agents/ main.py` currently reports 53 errors (missing generic type args, untyped function signatures across `enrichment.py`, `orchestrator.py`, `main.py`, etc.) — enabling it would mean either a large unrelated signature-annotation pass or a red hook out of the gate. Flagging as its own decision, not bundled in here: **does the team want to invest in full strict-mode compliance?**
- [x] `.github/workflows/ci.yml` already runs the same four checks (ruff/black/mypy/bandit/pytest, landed earlier in `3c38e6f`) — no CI change needed here.
- [x] Added `pre-commit` itself to `requirements-dev.txt`.
- [x] Verified: `pre-commit run --all-files` — all 4 hooks pass (black's first run reformatted 8 files, folded into Task 20 below as its own commit; second run clean).
- [x] Commit: `62b7d51` — `Add pre-commit config with ruff/black/mypy/bandit`

### Task 18 — Pin `requirements.txt` ✅ DONE
- [x] Confirmed unpinned: `google-adk`, `google-cloud-firestore`, `google-cloud-tasks`, `loguru`, `pydantic`, `fastapi`, `uvicorn[standard]`. Also pinned `requirements-dev.txt` (same reproducibility risk, same fix shape).
- [x] Pinned exact versions from the working `.venv` (read via `importlib.metadata`/`pip3 list` — note: this venv is `uv`-managed with no `pip` shim on PATH, only `pip3`/`pip3.13`; a bare `pip` in a shell here resolves to the *system* pip instead and silently reports a different/wrong package set — tripped over this mid-task, worth remembering next time in this repo).
- [x] Verified by installing into a **fresh** venv from the pinned files alone (not the working `.venv`) and running the full suite: 99/99 pass.
- [x] No lock file added (`uv.lock`/`pip-compile` output) — plain `==` pins judged sufficient for a dependency tree this size; revisit if it grows.
- [x] Commit: `3277b79` — `Pin requirements.txt and requirements-dev.txt to exact versions`

### Task 19 — Fix `mypy` error in `_run_agent` ✅ DONE (folded into Task 2's commit `d9d70fb`)
- [x] `vor_agents/orchestrator.py` — resolved as part of the Task 2 rewrite: `text = getattr(part, "text", None)` assigns to a local (mypy sees `Any`) instead of re-reading `part.text` directly after the guard.
- [x] Verify: `mypy vor_agents/ main.py tests/` — **0 errors** (was 1). Note: repo has no `mypy.ini`/`pyproject.toml` `[tool.mypy]` section, so this ran under mypy defaults, not `--strict` — strict mode not yet configured (ties into Task 17's pre-commit config).

### Task 20 — Run `black .` ✅ DONE
- [x] Confirmed at time of running: `black --check .` reported 8 files would reformat (down from the report's original 16 — several had already been reformatted incidentally by earlier fix commits).
- [x] Ran `black .` repo-wide (surfaced via `pre-commit run --all-files` while validating Task 17's new config — same action either way).
- [x] Already in CI (`3c38e6f`) and now in pre-commit (Task 17) so it can't drift again.
- [x] Suite: 99 passed, unchanged — formatting only. ruff/mypy/bandit clean.
- [x] Commit: `e0e24b6` — `Reformat codebase with black` (standalone, no logic changes mixed in; committed *before* Task 17's config commit despite being discovered during it, to keep the two changes separable)

---

## ⚠️ Environment note (found during validation, not in original report)

`.venv` is currently missing `mypy`, `black`, `bandit`, and `google.api_core` (breaks `tests/conftest.py` import, so `pytest` can't currently run at all). Fix before starting Task 1:
```bash
pip install -r requirements.txt -r requirements-dev.txt  # or reinstall mypy/black/bandit directly
```
This should be step 0 — none of the "Run tests" checkpoints above work until it's fixed.

---

## Outstanding decisions to resolve before/alongside implementation

These block or shape specific tasks above — resolve with the user before or during implementation, not unilaterally:

1. ~~**Sweep provisional patterns too?**~~ — RESOLVED in Task 13: no, provisional never autonomously SUPPRESSes (`CLASSIFIER_SYSTEM_PROMPT` rule 6), so the sweep has nothing to catch there. Surfaced a new related finding instead — see Task 21 below.
2. ~~**`human_confirmed` semantics**~~ — RESOLVED in Task 5: added a separate per-instance `verified_by` field rather than overloading `provenance`.
3. ~~**Vertex AI vs Gemini API key?**~~ — RESOLVED: Vertex AI. No code change needed (backend is env-var-driven, read by `google-genai` directly). `.env` updated locally (not committed), `DEPLOY.md`/`README.md` updated in commit `8add31f` on branch `feat/vertex-ai-config`, merged to `main`. Live smoke test confirmed auth/project/location all resolve correctly — request reached Vertex AI, blocked only on `BILLING_DISABLED` for `vor-hackathon2026` (not a config problem). **Re-run the smoke test once billing is enabled on the project** — not yet done.
4. **Permanently-failed audits** — after Task 1's fix, a repeatedly-failing audit now clears the flag and stores a `NO_ACTION` decision each time. Should N consecutive failures escalate to a human/alert rather than going silent? Not in original report's task list — worth deciding. **Still open.**
5. ~~**`confidence_used`**~~ — RESOLVED in Task 14: removed.
6. **Blast radius table workflow** — `propose_blast_radius` returns inert records forever, by design. Confirm this manual-promotion workflow is intentional long-term, not a gap to close. **Still open.**

### Task 21 (new, not in original report) — Enforce provisional-tier UNCERTAIN in code ✅ DONE
- [x] Same shape as Task 4: `classify_alert()` didn't check `enrichment["tier"] == "provisional"` after the model responds — only the prompt (rule 6) told the model not to SUPPRESS a provisional pattern. A non-compliant/hallucinating model returning SUPPRESS for a provisional pattern sailed through untouched, same class of gap Task 4 closed for `under_review`.
- [x] User confirmed: yes, harden it now.
- [x] `vor_agents/orchestrator.py` (`classify_alert`) — added override right after Task 4's `under_review` override: if `enrichment.get("tier") == "provisional"` and `decision == "SUPPRESS"` → force `UNCERTAIN` / `uncertain_reason="graduation_pending"` (enum value already existed, unused until now). Checked after (not merged into) the `under_review` check so a pattern that's both `under_review` AND provisional keeps the more specific `under_review` reason.
- [x] Added 3 tests mirroring Task 4's, in new `TestProvisionalTierBlocksSuppress`: SUPPRESS overridden when provisional (using the existing `low_diversity_confirmed_instances` fixture — 3 instances meets raw count but fails `MIN_DIVERSITY`, stays provisional); ESCALATE left untouched when provisional; SUPPRESS NOT overridden once graduated (isolates the new check from the pre-existing graduated-pattern coverage).
- [x] Suite: 102 passed (was 99). ruff/black/mypy/bandit all clean, `pre-commit run --all-files` passes.
- [x] Commit: `Deterministically block SUPPRESS for provisional-tier patterns`

---

## Suggested execution order

Critical (1-3) → High (4-10) → Medium (11-15) → Low (16-20), one commit per task per CLAUDE.md's "never batch multiple fixes into a single commit" rule. Tasks 6+7 and 2+19 naturally pair — call that out in the commit but keep the diff scoped to what the task actually touches.
