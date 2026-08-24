# MLflow Tracing Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Log every `classify_alert()`/`audit_pattern()` call as an MLflow run (prompt, output, resolved decision, which deterministic overrides fired), best-effort, with a durable Firestore fallback queue + scheduled replay job so a tracking-server outage never silently drops trace data.

**Architecture:** A new `vor_agents/tracing.py` wraps `mlflow.log_*` calls; on failure it writes the same run data to a `pending_traces` Firestore collection instead of raising. `orchestrator.py` calls into it from both LLM-calling entry points. A new `POST /replay-traces` endpoint (Cloud Scheduler-triggered, same shape as `/sweep`) drains the fallback queue periodically.

**Tech Stack:** Python 3.13, `mlflow` (new dependency), `google-cloud-firestore`, `loguru`, FastAPI, existing `FakeFirestoreClient` test double (extended with `.delete()`).

**Spec:** `docs/superpowers/specs/2026-08-24-mlflow-tracing-design.md`

## Global Constraints

- Tracing calls must **never raise** to `classify_alert()`/`audit_pattern()` callers — every failure mode (MLflow unreachable, Firestore fallback also unreachable) is caught and logged, never propagated.
- Scope stays at the two LLM-calling entry points only — no tracing retrofitted onto other modules, matching the `loguru`-adoption precedent set by the Cloud Tasks spec.
- `MLFLOW_TRACKING_URI` (a managed server, not a local file store — Cloud Run's ephemeral filesystem rules that out) is read from the environment, not hardcoded, per CLAUDE.md's secrets/config rule.
- This plan is written against the current baseline of `vor_agents/orchestrator.py` (no consecutive-audit-failure-escalation code yet). If `docs/superpowers/plans/2026-08-24-audit-failure-escalation.md` has already landed when this plan is executed, `audit_pattern()` will already have a local `audit_failed` variable and a `finally` block shape — reuse that variable for `log_audit_trace()`'s `audit_failed` argument instead of re-adding it, and resolve the two plans' overlapping edits to `audit_pattern()`/`classify_alert()` by hand (expected when two independent plans touch the same function).

---

## Task 1: `FakeFirestoreClient` gains `.delete()`; `mlflow` dependency

**Files:**
- Modify: `tests/conftest.py`
- Modify: `requirements.txt`

**Interfaces:**
- Produces: `_FakeDocRef.delete() -> None`. Consumed by Task 4's `replay_pending_traces()` tests.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_conftest_fakes.py` (create it if the blast-radius-firestore plan hasn't already; if it exists, add this test to it):

```python
def test_fake_doc_ref_delete_removes_the_doc(fake_firestore):
    fake_firestore.collection("things").document("a").set({"x": 1})

    fake_firestore.collection("things").document("a").delete()

    doc = fake_firestore.collection("things").document("a").get()
    assert not doc.exists
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_conftest_fakes.py -k delete -v`
Expected: `AttributeError: '_FakeDocRef' object has no attribute 'delete'`

- [ ] **Step 3: Add `.delete()` to `_FakeDocRef`**

In `tests/conftest.py`, add to the existing `_FakeDocRef` class:

```python
    def delete(self) -> None:
        self._store.pop(self._doc_id, None)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_conftest_fakes.py -v`
Expected: PASS.

- [ ] **Step 5: Add the `mlflow` dependency**

Run: `uv pip install mlflow --python .venv/bin/python`

Then read the installed version and pin it exactly in `requirements.txt` (same convention as every other pin in this file — see `docs/TODO-Aug15.md` Task 18):

```bash
.venv/bin/python -c "import importlib.metadata; print(importlib.metadata.version('mlflow'))"
```

Add the printed version to `requirements.txt`, alphabetically among the existing pins:

```
mlflow==<installed-version>
```

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: same pass count as before plus 1 new test — nothing else uses `.delete()` or `mlflow` yet.

- [ ] **Step 7: Commit**

```bash
git add tests/conftest.py tests/test_conftest_fakes.py requirements.txt
git commit -m "FakeFirestoreClient: add delete(); pin mlflow dependency"
```

---

## Task 2: `vor_agents/tracing.py` — best-effort logging with Firestore fallback

**Files:**
- Create: `vor_agents/tracing.py`
- Create: `tests/test_tracing.py`

**Interfaces:**
- Consumes: `fake_firestore` fixture, `_FakeDocRef.delete()` (Task 1).
- Produces: `PENDING_TRACES_COLLECTION = "pending_traces"`, `TracingError(Exception)`, `log_classification_trace(alert, enrichment, classifier_output, overrides_fired: list[str], firestore_client) -> None`, `log_audit_trace(identity_key, pattern_data, auditor_output, audit_failed: bool, firestore_client) -> None`. Both consumed by Task 3 (`orchestrator.py`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_tracing.py`:

```python
"""
Tests for vor_agents.tracing -- best-effort MLflow logging with a
Firestore fallback queue. mlflow itself is never called for real; the
module-level `mlflow` name inside vor_agents.tracing is monkeypatched
with small fakes so these tests never need network access or a real
tracking server.
"""

from vor_agents.schemas import (
    AuditorAction,
    AuditorOutput,
    ClassifierOutput,
    Decision,
    UncertainReason,
)
from vor_agents.tracing import PENDING_TRACES_COLLECTION, log_audit_trace, log_classification_trace


class _FakeMlflowRunContext:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeMlflowSuccess:
    def start_run(self, run_name=None):
        return _FakeMlflowRunContext()

    def log_params(self, params):
        pass

    def log_dict(self, data, path):
        pass


class _FakeMlflowAlwaysFails:
    def start_run(self, run_name=None):
        raise RuntimeError("connection refused")


class _BoomFirestoreClient:
    def collection(self, name):
        raise RuntimeError("Firestore also unavailable")


def _classifier_output():
    return ClassifierOutput(
        decision=Decision.SUPPRESS,
        matched_pattern_id="test",
        uncertain_reason=UncertainReason.NOT_APPLICABLE,
        structural_deviations_found=[],
        reasoning="matches template",
    )


def _auditor_output():
    return AuditorOutput(action=AuditorAction.NO_ACTION, reasoning="clean")


class TestLogClassificationTrace:
    def test_success_does_not_write_to_firestore(self, fake_firestore, monkeypatch):
        monkeypatch.setattr("vor_agents.tracing.mlflow", _FakeMlflowSuccess())

        log_classification_trace(
            {"detection_rule_id": "r"}, {"status": "NO_HISTORY"}, _classifier_output(), [], fake_firestore
        )

        assert list(fake_firestore.collection(PENDING_TRACES_COLLECTION).stream()) == []

    def test_mlflow_failure_falls_back_to_pending_traces(self, fake_firestore, monkeypatch):
        monkeypatch.setattr("vor_agents.tracing.mlflow", _FakeMlflowAlwaysFails())

        log_classification_trace(
            {"detection_rule_id": "r"}, {"status": "NO_HISTORY"}, _classifier_output(), ["under_review"], fake_firestore
        )

        docs = list(fake_firestore.collection(PENDING_TRACES_COLLECTION).stream())
        assert len(docs) == 1
        assert docs[0].to_dict()["run_type"] == "classification"
        assert docs[0].to_dict()["run_data"]["overrides_fired"] == ["under_review"]

    def test_never_raises_even_if_firestore_fallback_also_fails(self, monkeypatch):
        monkeypatch.setattr("vor_agents.tracing.mlflow", _FakeMlflowAlwaysFails())

        # Must not raise -- this is the whole point of the fallback design.
        log_classification_trace(
            {"detection_rule_id": "r"}, {"status": "NO_HISTORY"}, _classifier_output(), [], _BoomFirestoreClient()
        )


class TestLogAuditTrace:
    def test_success_does_not_write_to_firestore(self, fake_firestore, monkeypatch):
        monkeypatch.setattr("vor_agents.tracing.mlflow", _FakeMlflowSuccess())
        identity_key = ("rule", "w3wp.exe", "csc.exe", "family")

        log_audit_trace(identity_key, {"triggered_by": "test"}, _auditor_output(), False, fake_firestore)

        assert list(fake_firestore.collection(PENDING_TRACES_COLLECTION).stream()) == []

    def test_mlflow_failure_falls_back_to_pending_traces(self, fake_firestore, monkeypatch):
        monkeypatch.setattr("vor_agents.tracing.mlflow", _FakeMlflowAlwaysFails())
        identity_key = ("rule", "w3wp.exe", "csc.exe", "family")

        log_audit_trace(identity_key, {"triggered_by": "test"}, _auditor_output(), True, fake_firestore)

        docs = list(fake_firestore.collection(PENDING_TRACES_COLLECTION).stream())
        assert len(docs) == 1
        assert docs[0].to_dict()["run_type"] == "audit"
        assert docs[0].to_dict()["run_data"]["audit_failed"] is True
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_tracing.py -v`
Expected: `ModuleNotFoundError: No module named 'vor_agents.tracing'`

- [ ] **Step 3: Write `vor_agents/tracing.py`**

```python
"""
Vör — MLflow tracing for the two LLM-calling entry points
(classify_alert(), audit_pattern()). Best-effort: an MLflow-logging
failure never fails the caller's own request. On a failure, the run data
is queued to a durable Firestore fallback (pending_traces) instead of
being dropped, and replayed later by replay_pending_traces() -- see
docs/superpowers/specs/2026-08-24-mlflow-tracing-design.md.
"""

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import mlflow
from google.cloud.firestore import Client
from loguru import logger

if TYPE_CHECKING:
    from .schemas import AuditorOutput, ClassifierOutput

PENDING_TRACES_COLLECTION = "pending_traces"


class TracingError(Exception):
    """
    Raised internally when even the Firestore fallback write fails --
    distinguishes "MLflow unreachable, fell back to Firestore" (expected,
    logged at WARNING, not this exception) from "the fallback itself is
    broken" (unexpected, logged at ERROR). Never raised to
    classify_alert()/audit_pattern() -- _log_run() catches it internally,
    per this module's "never fail the caller's own request" rule.
    """


def _write_pending_trace(run_type: str, run_data: dict[str, Any], firestore_client: Client) -> None:
    doc_id = str(uuid.uuid4())
    try:
        firestore_client.collection(PENDING_TRACES_COLLECTION).document(doc_id).set(
            {
                "run_type": run_type,
                "run_data": run_data,
                "queued_at": datetime.now(UTC).isoformat(),
            }
        )
    except Exception as exc:
        raise TracingError(f"Failed to queue pending trace: {exc}") from exc


def _log_run(run_type: str, run_data: dict[str, Any], firestore_client: Client) -> None:
    """
    Shared best-effort logging path for both public functions below.
    Tries MLflow directly first; on ANY failure, falls back to the
    durable Firestore queue instead of dropping the trace. Never raises
    to its caller -- if even the fallback write fails, that's logged at
    ERROR with the run data serialized into the log line itself, so it's
    at least recoverable from log storage by hand in the worst case.
    """
    try:
        with mlflow.start_run(run_name=f"{run_type}_{run_data.get('identity_key')}"):
            mlflow.log_params(
                {"run_type": run_type, "identity_key": str(run_data.get("identity_key"))}
            )
            mlflow.log_dict(run_data, "run_data.json")
        return
    except Exception as exc:  # noqa: BLE001 — deliberate: any MLflow
        # failure (auth, connection, timeout) falls back, never raises.
        logger.bind(run_type=run_type).warning(
            "MLflow logging failed, falling back to pending_traces: {}", exc
        )

    try:
        _write_pending_trace(run_type, run_data, firestore_client)
    except TracingError as exc:
        logger.bind(run_type=run_type, run_data=run_data).error(
            "MLflow AND the pending_traces fallback both failed; trace data "
            "is only in this log line: {}",
            exc,
        )


def log_classification_trace(
    alert: dict[str, Any],
    enrichment: dict[str, Any],
    classifier_output: "ClassifierOutput",
    overrides_fired: list[str],
    firestore_client: Client,
) -> None:
    """
    Logs one classify_alert() call. overrides_fired lists which of
    classify_alert()'s deterministic overrides actually changed the
    decision (e.g. ["under_review"], ["ground_truth_missed"], or [] if
    the model's own decision stood untouched) -- queryable/filterable in
    MLflow without parsing the reasoning text.
    """
    run_data = {
        "identity_key": list(enrichment.get("pattern_identity_key", ())),
        "alert": alert,
        "enrichment": enrichment,
        "decision": classifier_output.decision,
        "uncertain_reason": classifier_output.uncertain_reason,
        "structural_deviations_found": classifier_output.structural_deviations_found,
        "reasoning": classifier_output.reasoning,
        "overrides_fired": overrides_fired,
    }
    _log_run("classification", run_data, firestore_client)


def log_audit_trace(
    identity_key: tuple[str, ...],
    pattern_data: dict[str, Any],
    auditor_output: "AuditorOutput",
    audit_failed: bool,
    firestore_client: Client,
) -> None:
    """Logs one audit_pattern() call, success or failure alike."""
    run_data = {
        "identity_key": list(identity_key),
        "pattern_data": pattern_data,
        "action": auditor_output.action,
        "invalidated_instance_ids": auditor_output.invalidated_instance_ids,
        "concerns_found": auditor_output.concerns_found,
        "reasoning": auditor_output.reasoning,
        "audit_failed": audit_failed,
    }
    _log_run("audit", run_data, firestore_client)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_tracing.py -v`
Expected: all 5 tests PASS.

- [ ] **Step 5: Run the full suite and lint**

Run: `.venv/bin/python -m pytest -q && .venv/bin/python -m ruff check . && .venv/bin/python -m mypy vor_agents/ main.py`
Expected: all pass, no regressions. (`mypy --strict` may need `# type: ignore[import-untyped]` on the `import mlflow` line if `mlflow` ships no type stubs — check the actual error before adding one; only add if mypy reports `import-untyped` for that specific line.)

- [ ] **Step 6: Commit**

```bash
git add vor_agents/tracing.py tests/test_tracing.py
git commit -m "Add vor_agents.tracing: best-effort MLflow logging + Firestore fallback"
```

---

## Task 3: Wire tracing into `classify_alert()` and `audit_pattern()`

**Files:**
- Modify: `vor_agents/orchestrator.py`
- Modify: `tests/test_orchestrator.py`

**Interfaces:**
- Consumes: `log_classification_trace()`, `log_audit_trace()` (Task 2).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_orchestrator.py`:

```python
class TestTracingWiring:
    """
    classify_alert()/audit_pattern() call the tracing functions exactly
    once per call, with overrides_fired/audit_failed populated correctly.
    mlflow itself is monkeypatched to the success fake from
    test_tracing.py's pattern -- these tests only check that orchestrator
    calls into tracing.py correctly, not tracing.py's own fallback
    behavior (already covered in tests/test_tracing.py).
    """

    async def test_classify_alert_logs_a_trace_with_no_overrides_on_a_clean_suppress(
        self, fake_firestore, baseline_alert, diverse_confirmed_instances, monkeypatch
    ):
        for instance in diverse_confirmed_instances:
            record_confirmed_negative(instance, fake_firestore)

        async def _fake_run_agent(*args, **kwargs):
            return {
                "decision": "SUPPRESS",
                "matched_pattern_id": "test",
                "uncertain_reason": "not_applicable",
                "structural_deviations_found": [],
                "reasoning": "matches template",
            }

        monkeypatch.setattr("vor_agents.orchestrator._run_agent", _fake_run_agent)
        captured = {}

        def _fake_log_classification_trace(alert, enrichment, classifier_output, overrides_fired, client):
            captured["overrides_fired"] = overrides_fired

        monkeypatch.setattr(
            "vor_agents.orchestrator.log_classification_trace", _fake_log_classification_trace
        )

        await classify_alert(baseline_alert, fake_firestore)

        assert captured["overrides_fired"] == []

    async def test_classify_alert_logs_under_review_override(
        self, fake_firestore, baseline_alert, diverse_confirmed_instances, monkeypatch
    ):
        for instance in diverse_confirmed_instances:
            record_confirmed_negative(instance, fake_firestore)
        identity_key = pattern_identity_key(baseline_alert)
        fake_firestore.collection(CONFIDENCE_COLLECTION).document(_doc_id(identity_key)).set(
            {"under_review": True}, merge=True
        )

        async def _fake_run_agent(*args, **kwargs):
            return {
                "decision": "SUPPRESS",
                "matched_pattern_id": "test",
                "uncertain_reason": "not_applicable",
                "structural_deviations_found": [],
                "reasoning": "matches template",
            }

        monkeypatch.setattr("vor_agents.orchestrator._run_agent", _fake_run_agent)
        captured = {}

        def _fake_log_classification_trace(alert, enrichment, classifier_output, overrides_fired, client):
            captured["overrides_fired"] = overrides_fired

        monkeypatch.setattr(
            "vor_agents.orchestrator.log_classification_trace", _fake_log_classification_trace
        )

        await classify_alert(baseline_alert, fake_firestore)

        assert captured["overrides_fired"] == ["under_review"]

    async def test_audit_pattern_logs_a_trace(self, fake_firestore, monkeypatch):
        identity_key = ("rule", "w3wp.exe", "csc.exe", "family")

        async def _ok(*args, **kwargs):
            return {
                "action": "NO_ACTION",
                "invalidated_instance_ids": [],
                "concerns_found": [],
                "reasoning": "clean",
            }

        monkeypatch.setattr("vor_agents.orchestrator._run_agent", _ok)
        captured = {}

        def _fake_log_audit_trace(ident_key, pattern_data, auditor_output, audit_failed, client):
            captured["audit_failed"] = audit_failed

        monkeypatch.setattr("vor_agents.orchestrator.log_audit_trace", _fake_log_audit_trace)

        await audit_pattern(identity_key, {"triggered_by": "test"}, fake_firestore)

        assert captured["audit_failed"] is False

    async def test_audit_pattern_logs_audit_failed_true_on_failure(self, fake_firestore, monkeypatch):
        identity_key = ("rule", "w3wp.exe", "csc.exe", "family")

        async def _boom(*args, **kwargs):
            raise RuntimeError("model call failed")

        monkeypatch.setattr("vor_agents.orchestrator._run_agent", _boom)
        captured = {}

        def _fake_log_audit_trace(ident_key, pattern_data, auditor_output, audit_failed, client):
            captured["audit_failed"] = audit_failed

        monkeypatch.setattr("vor_agents.orchestrator.log_audit_trace", _fake_log_audit_trace)

        await audit_pattern(identity_key, {"triggered_by": "test"}, fake_firestore)

        assert captured["audit_failed"] is True
```

Add to the imports at the top of `tests/test_orchestrator.py` (if not already present from an earlier plan): `from vor_agents.enrichment import CONFIDENCE_COLLECTION, _doc_id` and `from vor_agents.identity import pattern_identity_key`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_orchestrator.py -k Tracing -v`
Expected: `AttributeError: <module 'vor_agents.orchestrator'> does not have the attribute 'log_classification_trace'` (monkeypatch target doesn't exist yet).

- [ ] **Step 3: Wire tracing into `vor_agents/orchestrator.py`**

Add the import (alongside the other `.` imports at the top of the file):

```python
from .tracing import log_audit_trace, log_classification_trace
```

In `classify_alert()`, add `overrides_fired: list[str] = []` right after the `enrichment = enrich(alert, firestore_client)` line.

At the `AgentOutputError` early-return path, add a trace call before returning (the model output was unparseable — no overrides could have fired, but this call still exists and its outcome is worth a trace record):

```python
    except AgentOutputError as exc:
        logger.bind(identity_key=identity_key).error("Classifier output unparseable: {}", exc)
        unparseable_result = ClassifierOutput(
            decision=Decision.UNCERTAIN,
            matched_pattern_id=None,
            uncertain_reason=UncertainReason.MISSING_DATA,
            structural_deviations_found=[],
            reasoning=f"Classifier returned unparseable output: {exc}",
        )
        log_classification_trace(alert, enrichment, unparseable_result, [], firestore_client)
        return (unparseable_result, identity_key)
```

(This replaces the existing `return (ClassifierOutput(...), identity_key)` tuple literal with an intermediate `unparseable_result` variable so it can be passed to `log_classification_trace()` before returning — same values, just named so both places use it.)

In the `under_review` override block, add one line right after the `classifier_output = classifier_output.model_copy(...)` assignment closes:

```python
        overrides_fired.append("under_review")
```

In the provisional-tier override block, same pattern:

```python
        overrides_fired.append("provisional_tier")
```

In the ground-truth-missed override block (inside `if missed_by_model and classifier_output.decision == "SUPPRESS":`):

```python
            overrides_fired.append("ground_truth_missed")
```

In the self-consistency override block (`if classifier_output.structural_deviations_found and classifier_output.decision == "SUPPRESS":`):

```python
        overrides_fired.append("self_consistency_deviation")
```

Replace the function's final line:

```python
    log_classification_trace(alert, enrichment, classifier_output, overrides_fired, firestore_client)
    return classifier_output, identity_key
```

In `audit_pattern()`, add a local `audit_failed = False` right after `mark_under_review(identity_key, firestore_client)`, set it `True` in the `except` branch, and log the trace after the `finally` block:

```python
    mark_under_review(identity_key, firestore_client)
    audit_failed = False

    try:
        ...  # unchanged
        decision = AuditorOutput.model_validate(result)
    except Exception as exc:  # noqa: BLE001 — unchanged rationale
        audit_failed = True
        logger.bind(identity_key=identity_key).exception("Audit failed")
        decision = AuditorOutput(
            action=AuditorAction.NO_ACTION,
            reasoning=f"Audit failed with error: {exc!r}",
        )
    finally:
        clear_under_review(identity_key, firestore_client, decision.model_dump())

    log_audit_trace(identity_key, pattern_data, decision, audit_failed, firestore_client)
    return decision
```

(If `docs/superpowers/plans/2026-08-24-audit-failure-escalation.md` has already landed, `audit_failed` and the `finally` block's `clear_under_review(..., audit_failed=audit_failed)` call already exist with this exact shape — just add the `log_audit_trace(...)` call after the `finally` block and the escalation-check code that follows it, don't re-add `audit_failed`.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_orchestrator.py -v`
Expected: all tests in the file PASS, including the 4 new ones.

- [ ] **Step 5: Run the full suite and lint**

Run: `.venv/bin/python -m pytest -q && .venv/bin/python -m ruff check . && .venv/bin/python -m black --check . && .venv/bin/python -m mypy vor_agents/ main.py && .venv/bin/python -m bandit -r vor_agents/ main.py`
Expected: all pass, no regressions.

- [ ] **Step 6: Commit**

```bash
git add vor_agents/orchestrator.py tests/test_orchestrator.py
git commit -m "Log classification/audit traces via vor_agents.tracing"
```

---

## Task 4: `replay_pending_traces()` + `POST /replay-traces`

**Files:**
- Modify: `vor_agents/tracing.py`
- Modify: `tests/test_tracing.py`
- Modify: `main.py`
- Modify: `tests/test_main.py`

**Interfaces:**
- Produces: `replay_pending_traces(firestore_client: Client) -> int`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_tracing.py`:

```python
from vor_agents.tracing import replay_pending_traces


class TestReplayPendingTraces:
    def _seed_pending(self, fake_firestore, identity_key, run_type="classification"):
        fake_firestore.collection(PENDING_TRACES_COLLECTION).document(
            f"pending-{identity_key}"
        ).set({"run_type": run_type, "run_data": {"identity_key": [identity_key]}})

    def test_replays_and_deletes_successful_docs(self, fake_firestore, monkeypatch):
        monkeypatch.setattr("vor_agents.tracing.mlflow", _FakeMlflowSuccess())
        self._seed_pending(fake_firestore, "a")
        self._seed_pending(fake_firestore, "b")

        count = replay_pending_traces(fake_firestore)

        assert count == 2
        assert list(fake_firestore.collection(PENDING_TRACES_COLLECTION).stream()) == []

    def test_failed_replay_leaves_the_doc_pending(self, fake_firestore, monkeypatch):
        monkeypatch.setattr("vor_agents.tracing.mlflow", _FakeMlflowAlwaysFails())
        self._seed_pending(fake_firestore, "a")

        count = replay_pending_traces(fake_firestore)

        assert count == 0
        assert len(list(fake_firestore.collection(PENDING_TRACES_COLLECTION).stream())) == 1

    def test_one_bad_doc_does_not_block_the_rest_of_the_batch(self, fake_firestore, monkeypatch):
        class _FailsForA:
            def start_run(self, run_name=None):
                if run_name and "'a'" in run_name:
                    raise RuntimeError("still down for this one")
                return _FakeMlflowRunContext()

            def log_params(self, params):
                pass

            def log_dict(self, data, path):
                pass

        monkeypatch.setattr("vor_agents.tracing.mlflow", _FailsForA())
        self._seed_pending(fake_firestore, "a")
        self._seed_pending(fake_firestore, "b")

        count = replay_pending_traces(fake_firestore)

        assert count == 1
        remaining = list(fake_firestore.collection(PENDING_TRACES_COLLECTION).stream())
        assert len(remaining) == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_tracing.py -k Replay -v`
Expected: `ImportError: cannot import name 'replay_pending_traces'`

- [ ] **Step 3: Add `replay_pending_traces()` to `vor_agents/tracing.py`**

```python
def replay_pending_traces(firestore_client: Client) -> int:
    """
    Reads every doc in pending_traces, attempts to log each to MLflow;
    on success deletes the doc, on failure leaves it for the next
    scheduled run (see main.py's POST /replay-traces). Returns the count
    successfully replayed. Each doc gets its own try/except -- one bad or
    still-failing doc doesn't block the rest of the batch.
    """
    replayed = 0
    still_pending = 0
    for doc in list(firestore_client.collection(PENDING_TRACES_COLLECTION).stream()):
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

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_tracing.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Add `POST /replay-traces` to `main.py`**

Write the failing test first, in `tests/test_main.py`:

```python
def test_replay_traces_returns_replayed_count(fake_firestore):
    with patch("main.get_firestore_client", return_value=fake_firestore), \
         patch("main.replay_pending_traces", return_value=4):
        client = TestClient(main.app)
        resp = client.post("/replay-traces", json={})

    assert resp.status_code == 200
    assert resp.json() == {"replayed": 4}
```

Run: `.venv/bin/python -m pytest tests/test_main.py -k replay -v` — expect 404 (route doesn't exist yet).

Add to `main.py`'s imports:

```python
from vor_agents.tracing import replay_pending_traces
```

Add the route, after `/blast-radius/commit` (or after `/audit` if the blast-radius plan hasn't landed yet):

```python
@app.post("/replay-traces")
async def replay_traces(request: Request) -> dict[str, int]:
    """
    Cloud Scheduler hits this every 15 minutes (see DEPLOY.md) to drain
    vor_agents.tracing's pending_traces fallback queue -- traces that
    failed to log directly to MLflow (server unreachable at the time)
    get retried here. OIDC-gated the same way /sweep is -- Cloud Run
    itself rejects unauthenticated requests, no manual auth check needed.
    """
    client = get_firestore_client()
    replayed = replay_pending_traces(client)
    return {"replayed": replayed}
```

Run: `.venv/bin/python -m pytest tests/test_main.py -v` — expect all PASS.

- [ ] **Step 6: Run the full suite and lint**

Run: `.venv/bin/python -m pytest -q && .venv/bin/python -m ruff check . && .venv/bin/python -m black --check . && .venv/bin/python -m mypy vor_agents/ main.py && .venv/bin/python -m bandit -r vor_agents/ main.py`
Expected: all pass, no regressions.

- [ ] **Step 7: Commit**

```bash
git add vor_agents/tracing.py tests/test_tracing.py main.py tests/test_main.py
git commit -m "Add replay_pending_traces() and POST /replay-traces"
```

---

## Task 5: `docs/DEPLOY.md` — MLflow env vars and the replay Scheduler job

**Files:**
- Modify: `docs/DEPLOY.md`

**Interfaces:**
- None (documentation only).

- [ ] **Step 1: Add a new section**

Insert after the Cloud Tasks/blast-radius/needs_attention sections (wherever this repo's `DEPLOY.md` currently ends its numbered steps):

```markdown
## 5. MLflow tracing

Set the tracking server URI on the Cloud Run service, alongside the
existing env vars:

```bash
gcloud run services update vor \
  --region us-central1 \
  --update-env-vars "MLFLOW_TRACKING_URI=https://YOUR_MLFLOW_SERVER"
```

If the managed server requires its own auth (API key, service-account
token), that credential goes in Secret Manager / `.env`, never
hardcoded, per CLAUDE.md's secrets rule -- consult whichever managed
MLflow offering you're using (Databricks-hosted or self-run) for its own
auth mechanism; this repo's code just reads `MLFLOW_TRACKING_URI` and
whatever auth env vars the `mlflow` client itself expects.

Scheduled replay job for the pending_traces fallback queue:

```bash
gcloud scheduler jobs create http vor-trace-replay \
  --location us-central1 \
  --schedule "*/15 * * * *" \
  --uri "https://YOUR_CLOUD_RUN_URL/replay-traces" \
  --http-method POST \
  --oidc-service-account-email "vor-scheduler@YOUR_PROJECT_ID.iam.gserviceaccount.com" \
  --oidc-token-audience "https://YOUR_CLOUD_RUN_URL"
```

Every 15 minutes -- unvalidated starting point, same posture as every
other unvalidated interval/threshold in this project. Reuses the
existing `vor-scheduler` service account, same as `/sweep`. `/replay-traces`
must never be deployed with `--allow-unauthenticated`.

**Not addressed here:** no cap on `pending_traces` growth during an
extended MLflow outage -- if the tracking server is down for days, this
collection grows unbounded. Worth a TTL/max-size policy if real outages
turn out to be long; revisit with real data.
```

- [ ] **Step 2: Commit**

```bash
git add docs/DEPLOY.md
git commit -m "Document MLflow tracking URI and the trace-replay Scheduler job"
```

---

## Final verification

- [ ] Run `.venv/bin/python -m pytest -v` — full suite passes.
- [ ] Run `.venv/bin/python -m ruff check . && .venv/bin/python -m black --check . && .venv/bin/python -m mypy vor_agents/ main.py && .venv/bin/python -m bandit -r vor_agents/ main.py` — all clean.
- [ ] Confirm `git log --oneline -5` shows one commit per task.
- [ ] Update `docs/TODO-Aug24.md` Task 4 checkbox to done, referencing the commits.
