# Cloud Tasks Audit Queue Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move all audit execution (both the `/classify`-triggered and `/sweep`-triggered paths) off in-process background work and onto a Cloud Tasks queue, so audits always run inside their own fully-CPU-allocated HTTP request, get automatic retries, and get real server-side dedup.

**Architecture:** A new `vor_agents/task_queue.py` module owns deterministic task naming and the single `enqueue_audit()` call. `orchestrator.py`'s `run_scheduled_sweep()` switches from awaiting `audit_pattern()` in a loop to enqueueing one task per target via a dependency-injected callable. `main.py` gains a new `POST /audit` endpoint (the one place `audit_pattern()` is still actually invoked), and `/classify`/`/sweep` both enqueue instead of executing directly.

**Tech Stack:** Python 3.13, `google-cloud-tasks` (Cloud Tasks client), `loguru` (adopted for the first time in this codebase), FastAPI, pytest/pytest-asyncio, existing `FakeFirestoreClient` fake-client pattern extended with a new `FakeTasksClient`.

**Spec:** `docs/superpowers/specs/2026-08-14-cloud-tasks-audit-queue-design.md`

## Global Constraints

- Never let a raw Cloud Tasks/GCP SDK exception escape `task_queue.py` — wrap in `AuditEnqueueError`, per the project's existing standard (`identity.py`'s `MalformedAlertError` is the precedent).
- An audit **enqueue** failure must never fail the caller's own response — `/classify` still returns the classification result, `/sweep` still enqueues its remaining targets, even if one enqueue call fails.
- Task names are deterministic (derived from `identity_key` via sha1), so a duplicate enqueue attempt for the same pattern within Cloud Tasks' dedup window is rejected by Cloud Tasks itself (`AlreadyExists`), not by an app-level check. This replaces `main.py`'s old `under_review` read-then-act guard, which is removed entirely.
- `run_scheduled_sweep()` becomes a plain synchronous function (no more `await audit_pattern()` inside it) — this is a real signature/behavior change from today's `async def run_scheduled_sweep(...)`.
- `loguru` is used only at the new failure/dedup points this change introduces (`task_queue.py`, `main.py`'s enqueue-failure catch site) — not retrofitted onto existing modules.
- Test doubles only — no real GCP credentials or network access required to run the test suite, matching the existing `FakeFirestoreClient` convention.

---

## Task 1: `FakeTasksClient` test double + `google-cloud-tasks` dependency

**Files:**
- Modify: `requirements.txt`
- Modify: `tests/conftest.py`

**Interfaces:**
- Produces: `FakeTasksClient` class and `fake_tasks_client` pytest fixture, both used by Task 2's tests onward. `FakeTasksClient.created_tasks: dict[str, dict]` (task name → task dict), `.create_task(parent: str, task: dict) -> dict`, `.queue_path(project: str, location: str, queue: str) -> str`, `.task_path(project: str, location: str, queue: str, task: str) -> str`.

- [ ] **Step 1: Add the dependency**

Edit `requirements.txt`:

```
google-adk
google-cloud-firestore
google-cloud-tasks
pydantic
fastapi
uvicorn[standard]
```

- [ ] **Step 2: Install it**

Run: `uv pip install -r requirements.txt -r requirements-dev.txt --python .venv/bin/python`
Expected: `google-cloud-tasks` (and its transitive deps) installed with no errors.

- [ ] **Step 3: Add `FakeTasksClient` to `tests/conftest.py`**

Append to the end of `tests/conftest.py` (after the existing `fake_firestore` fixture):

```python
from google.api_core.exceptions import AlreadyExists


class FakeTasksClient:
    """
    In-memory stand-in for google.cloud.tasks_v2.CloudTasksClient.
    Supports exactly what vor_agents.task_queue uses: create_task() with
    a task name that collides raises the SAME AlreadyExists exception
    the real client raises (imported from google.api_core.exceptions,
    not a fake stand-in type), so enqueue_audit()'s dedup handling is
    exercised against the real error type. Deliberately not a full Cloud
    Tasks emulator — tests should never need real GCP credentials or
    network access to run.
    """
    def __init__(self):
        self.created_tasks: dict[str, dict] = {}

    def create_task(self, parent: str, task: dict) -> dict:
        name = task["name"]
        if name in self.created_tasks:
            raise AlreadyExists(f"Task already exists: {name}")
        self.created_tasks[name] = task
        return task

    def queue_path(self, project: str, location: str, queue: str) -> str:
        return f"projects/{project}/locations/{location}/queues/{queue}"

    def task_path(self, project: str, location: str, queue: str, task: str) -> str:
        return f"{self.queue_path(project, location, queue)}/tasks/{task}"


@pytest.fixture
def fake_tasks_client():
    return FakeTasksClient()
```

- [ ] **Step 4: Verify the test suite still collects and passes (no behavior changed yet)**

Run: `.venv/bin/python -m pytest -q`
Expected: same pass count as before this task (no new tests reference the fixture yet, so nothing new runs — this just confirms the fixture doesn't break collection).

- [ ] **Step 5: Commit**

```bash
git add requirements.txt tests/conftest.py
git commit -m "Add FakeTasksClient test double and google-cloud-tasks dependency"
```

---

## Task 2: `vor_agents/task_queue.py` — deterministic naming + enqueue

**Files:**
- Create: `vor_agents/task_queue.py`
- Create: `tests/test_task_queue.py`
- Modify: `requirements.txt`

**Interfaces:**
- Consumes: `FakeTasksClient` / `fake_tasks_client` fixture (Task 1).
- Produces: `AuditEnqueueError(Exception)`, `_task_name(queue_path: str, identity_key: tuple) -> str`, `enqueue_audit(identity_key: tuple, pattern_data: dict, tasks_client, queue_path: str, audit_url: str, oidc_service_account_email: str) -> bool` — all consumed by Task 3 (`orchestrator.py`) and Tasks 4–6 (`main.py`).

- [ ] **Step 1: Add `loguru` dependency**

Edit `requirements.txt`, add after `google-cloud-tasks`:

```
google-adk
google-cloud-firestore
google-cloud-tasks
loguru
pydantic
fastapi
uvicorn[standard]
```

Run: `uv pip install -r requirements.txt -r requirements-dev.txt --python .venv/bin/python`

- [ ] **Step 2: Write the failing tests**

Create `tests/test_task_queue.py`:

```python
"""
Tests for vor_agents.task_queue -- deterministic task naming and the
enqueue/dedup boundary with Cloud Tasks. Uses FakeTasksClient (see
conftest.py), never a real Cloud Tasks connection.
"""

import pytest

from vor_agents.task_queue import AuditEnqueueError, _task_name, enqueue_audit

QUEUE_PATH = "projects/test-project/locations/us-central1/queues/vor-audit-queue"
AUDIT_URL = "https://vor-example.a.run.app/audit"
OIDC_SA = "vor-scheduler@test-project.iam.gserviceaccount.com"

IDENTITY_KEY = ("SharePoint_ToolPane_Rule", "w3wp.exe", "csc.exe", "ToolPane_admin")
OTHER_IDENTITY_KEY = ("SharePoint_ToolPane_Rule", "w3wp.exe", "cmd.exe", "ToolPane_admin")


class TestTaskName:
    def test_same_identity_key_produces_same_name(self):
        assert _task_name(QUEUE_PATH, IDENTITY_KEY) == _task_name(QUEUE_PATH, IDENTITY_KEY)

    def test_different_identity_key_produces_different_name(self):
        assert _task_name(QUEUE_PATH, IDENTITY_KEY) != _task_name(QUEUE_PATH, OTHER_IDENTITY_KEY)

    def test_name_is_scoped_under_the_queue_path(self):
        assert _task_name(QUEUE_PATH, IDENTITY_KEY).startswith(f"{QUEUE_PATH}/tasks/audit-")


class TestEnqueueAudit:
    def test_new_task_returns_true_and_is_recorded(self, fake_tasks_client):
        result = enqueue_audit(
            IDENTITY_KEY, {"triggered_by": "test"}, fake_tasks_client,
            QUEUE_PATH, AUDIT_URL, OIDC_SA,
        )
        assert result is True
        assert len(fake_tasks_client.created_tasks) == 1

    def test_duplicate_identity_key_returns_false_without_a_second_task(self, fake_tasks_client):
        enqueue_audit(
            IDENTITY_KEY, {"triggered_by": "test"}, fake_tasks_client,
            QUEUE_PATH, AUDIT_URL, OIDC_SA,
        )
        result = enqueue_audit(
            IDENTITY_KEY, {"triggered_by": "test"}, fake_tasks_client,
            QUEUE_PATH, AUDIT_URL, OIDC_SA,
        )
        assert result is False
        assert len(fake_tasks_client.created_tasks) == 1

    def test_different_pattern_gets_its_own_task(self, fake_tasks_client):
        enqueue_audit(
            IDENTITY_KEY, {"triggered_by": "test"}, fake_tasks_client,
            QUEUE_PATH, AUDIT_URL, OIDC_SA,
        )
        enqueue_audit(
            OTHER_IDENTITY_KEY, {"triggered_by": "test"}, fake_tasks_client,
            QUEUE_PATH, AUDIT_URL, OIDC_SA,
        )
        assert len(fake_tasks_client.created_tasks) == 2

    def test_non_dedup_client_errors_are_wrapped(self, fake_tasks_client):
        def _boom(parent, task):
            raise RuntimeError("quota exceeded")
        fake_tasks_client.create_task = _boom

        with pytest.raises(AuditEnqueueError):
            enqueue_audit(
                IDENTITY_KEY, {"triggered_by": "test"}, fake_tasks_client,
                QUEUE_PATH, AUDIT_URL, OIDC_SA,
            )
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_task_queue.py -v`
Expected: `ModuleNotFoundError: No module named 'vor_agents.task_queue'` (collection error) — fails because the module doesn't exist yet.

- [ ] **Step 4: Write `vor_agents/task_queue.py`**

```python
"""
Vör — Cloud Tasks audit-enqueue path.

Deterministic task-name construction plus the single enqueue call. No
LLM, no scoring logic — this module's only job is getting an audit
request onto Cloud Tasks reliably, with real dedup, replacing the
best-effort under_review read-then-act check that used to live in
main.py. See docs/superpowers/specs/2026-08-14-cloud-tasks-audit-queue-design.md.
"""

import hashlib
import json

from google.api_core.exceptions import AlreadyExists
from google.cloud.tasks_v2 import HttpMethod
from loguru import logger


class AuditEnqueueError(Exception):
    """
    Raised when enqueueing an audit task fails for any reason OTHER than
    the task already existing (that case is dedup working as intended,
    not a failure — see enqueue_audit()). Wraps the underlying Cloud
    Tasks client exception so callers never see a raw GCP SDK exception,
    same "never surface raw exceptions" standard as MalformedAlertError
    in identity.py.
    """


def _task_name(queue_path: str, identity_key: tuple) -> str:
    """
    Deterministic task name derived from identity_key: the same pattern
    always maps to the same task name. Cloud Tasks rejects a second task
    with a name already present in its dedup window (~1hr after
    completion/deletion) with AlreadyExists — this is what gives real,
    server-side dedup instead of an app-level read-then-act check.

    Hashed (not the raw identity_key) because Cloud Tasks task names are
    restricted to [A-Za-z0-9_-] and a fixed max length, and identity_key
    components (rule IDs, process names) aren't guaranteed to fit either
    constraint.
    """
    key_hash = hashlib.sha1("_".join(identity_key).encode()).hexdigest()
    return f"{queue_path}/tasks/audit-{key_hash}"


def enqueue_audit(
    identity_key: tuple,
    pattern_data: dict,
    tasks_client,
    queue_path: str,
    audit_url: str,
    oidc_service_account_email: str,
) -> bool:
    """
    Enqueues a POST /audit task for this pattern. Returns True if a new
    task was created, False if an identical task was already queued
    (dedup hit — expected, not an error).

    Any other failure (auth, quota, queue missing) is logged and
    re-raised as AuditEnqueueError — callers decide for themselves
    whether an enqueue failure should affect their own response (see
    main.py's /classify, which deliberately does not let this fail the
    classification response).
    """
    task_name = _task_name(queue_path, identity_key)
    task = {
        "name": task_name,
        "http_request": {
            "http_method": HttpMethod.POST,
            "url": audit_url,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(
                {"identity_key": list(identity_key), "pattern_data": pattern_data}
            ).encode(),
            "oidc_token": {
                "service_account_email": oidc_service_account_email,
                "audience": audit_url,
            },
        },
    }

    try:
        tasks_client.create_task(parent=queue_path, task=task)
    except AlreadyExists:
        logger.bind(identity_key=identity_key, task_name=task_name).warning(
            "Audit already queued for this pattern, skipping duplicate enqueue"
        )
        return False
    except Exception as exc:
        logger.bind(identity_key=identity_key, task_name=task_name, error=str(exc)).error(
            "Failed to enqueue audit task"
        )
        raise AuditEnqueueError(
            f"Failed to enqueue audit for {identity_key}: {exc}"
        ) from exc

    return True
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_task_queue.py -v`
Expected: all 7 tests PASS.

- [ ] **Step 6: Run the full suite and lint**

Run: `.venv/bin/python -m pytest -q && .venv/bin/python -m ruff check .`
Expected: all pass, no regressions, no lint errors.

- [ ] **Step 7: Commit**

```bash
git add requirements.txt vor_agents/task_queue.py tests/test_task_queue.py
git commit -m "Add vor_agents.task_queue: deterministic naming + Cloud Tasks enqueue"
```

---

## Task 3: `orchestrator.py` — `run_scheduled_sweep()` switches to enqueue

**Files:**
- Modify: `vor_agents/orchestrator.py:186-204` (the `run_scheduled_sweep` function)
- Modify: `tests/test_orchestrator.py`

**Interfaces:**
- Consumes: nothing new imported — takes an `enqueue_audit_fn: Callable[[tuple, dict], bool]` parameter injected by the caller (Task 4 wires the real one; this task's tests use a plain local fake function). Orchestrator stays decoupled from Cloud Tasks config specifics, same as it already receives `firestore_client` generically rather than constructing one itself.
- Produces: `run_scheduled_sweep(firestore_client, enqueue_audit_fn, max_targets: int = 10) -> list[tuple]` — **no longer `async`**, returns the list of identity_keys that were newly enqueued (dedup hits excluded). Consumed by Task 4 (`main.py`'s `/sweep`).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_orchestrator.py` (new top-level import and new test class — this class is NOT `@pytest.mark.asyncio` since `run_scheduled_sweep` is now synchronous):

Add to the imports at the top of the file:

```python
from vor_agents.enrichment import record_confirmed_negative
from vor_agents.orchestrator import classify_alert, run_scheduled_sweep
```

(This replaces the existing `from vor_agents.orchestrator import classify_alert` line — `record_confirmed_negative` is already imported separately below it in the file; just add `run_scheduled_sweep` to the existing `orchestrator` import.)

Append a new test class:

```python
class TestRunScheduledSweep:
    """
    run_scheduled_sweep() no longer awaits audit_pattern() directly --
    it selects targets (unchanged select_audit_targets() logic) and
    enqueues one task per target via a dependency-injected callable,
    same shape as classify_alert() receiving firestore_client rather
    than constructing one. Not async: enqueueing is a synchronous Cloud
    Tasks client call, same as the existing synchronous Firestore calls
    already used inside async FastAPI handlers elsewhere in this repo.
    """

    def test_enqueues_each_selected_target_and_returns_their_identity_keys(
        self, fake_firestore, diverse_confirmed_instances
    ):
        for instance in diverse_confirmed_instances:
            record_confirmed_negative(instance, fake_firestore)

        enqueued_calls = []

        def fake_enqueue(identity_key, pattern_data):
            enqueued_calls.append(identity_key)
            return True

        result = run_scheduled_sweep(fake_firestore, fake_enqueue)

        expected_key = ("SharePoint_ToolPane_Rule", "w3wp.exe", "csc.exe", "ToolPane_admin")
        assert result == [expected_key]
        assert enqueued_calls == [expected_key]

    def test_dedup_hits_are_not_counted_in_the_returned_list(
        self, fake_firestore, diverse_confirmed_instances
    ):
        for instance in diverse_confirmed_instances:
            record_confirmed_negative(instance, fake_firestore)

        result = run_scheduled_sweep(fake_firestore, lambda identity_key, pattern_data: False)

        assert result == []

    def test_no_confirmed_patterns_enqueues_nothing(self, fake_firestore):
        result = run_scheduled_sweep(fake_firestore, lambda identity_key, pattern_data: True)
        assert result == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_orchestrator.py::TestRunScheduledSweep -v`
Expected: `TypeError: run_scheduled_sweep() missing 1 required positional argument: 'enqueue_audit_fn'` (current signature is `(firestore_client, max_targets=10)`).

- [ ] **Step 3: Replace `run_scheduled_sweep()` in `vor_agents/orchestrator.py`**

Replace the existing function (currently at lines 186-204):

```python
def run_scheduled_sweep(
    firestore_client, enqueue_audit_fn, max_targets: int = 10
) -> list[tuple]:
    """
    Safety-net path — invoked on a timer (e.g. weekly Cloud Scheduler job
    hitting a Cloud Run endpoint that calls this function). Reuses the
    same select_audit_targets() priority scoring the event-triggered path
    would use if it fired for these patterns, which it may never do for
    quiet, low-volume ones — that's the coverage gap this sweep exists to
    close.

    No longer runs audits itself — enqueues one Cloud Tasks task per
    selected target via enqueue_audit_fn (identity_key, pattern_data) ->
    bool, and returns the identity_keys that were newly enqueued (a
    dedup hit — the pattern already has an audit in flight — returns
    False from enqueue_audit_fn and is excluded from the result). The
    actual audit runs later, in its own request, when Cloud Tasks
    dispatches to POST /audit — see task_queue.py and main.py.

    enqueue_audit_fn is dependency-injected rather than imported and
    called directly: this function doesn't need to know Cloud Tasks
    config specifics (queue path, audit URL, OIDC service account) any
    more than it needs to know how firestore_client was constructed.
    """
    all_suppressed = _fetch_all_suppressed_patterns(firestore_client)
    targets = select_audit_targets(all_suppressed, max_targets=max_targets)

    enqueued = []
    for pattern in targets:
        was_new = enqueue_audit_fn(pattern["identity_key"], pattern)
        if was_new:
            enqueued.append(pattern["identity_key"])
    return enqueued
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_orchestrator.py -v`
Expected: all tests in the file PASS, including the 3 new ones.

- [ ] **Step 5: Run the full suite and lint**

Run: `.venv/bin/python -m pytest -q && .venv/bin/python -m ruff check .`
Expected: all pass except `test_main.py`'s `test_sweep_returns_audited_count`, which will FAIL at this point — `main.py`'s existing call to `run_scheduled_sweep(firestore_client)` doesn't match the new `(firestore_client, enqueue_audit_fn, max_targets=10)` signature yet. Confirm it's the only failure, then proceed — it gets fixed (test replaced, `main.py` rewritten) in Task 4.

- [ ] **Step 6: Commit**

```bash
git add vor_agents/orchestrator.py tests/test_orchestrator.py
git commit -m "run_scheduled_sweep(): enqueue audits via Cloud Tasks instead of awaiting them"
```

---

## Task 4: `main.py` — Cloud Tasks wiring for `/classify`, `/sweep`, `/audit`

`main.py` is one small file, and all three endpoint changes are part of
the same coherent rewrite (wiring helpers `/classify`, `/sweep`, and the
new `/audit` all share `get_tasks_client()`/`_enqueue()`) — so this task
writes the full test coverage for all three endpoints first, then makes
one rewrite that satisfies all of them, rather than splitting into
several tasks where later ones would just re-confirm behavior an earlier
one already implemented.

**Files:**
- Modify: `main.py`
- Modify: `tests/test_main.py`

**Interfaces:**
- Consumes: `enqueue_audit`, `AuditEnqueueError` from `vor_agents.task_queue` (Task 2); `run_scheduled_sweep` from `vor_agents.orchestrator` (Task 3).
- Produces: `get_tasks_client()`, `_enqueue(identity_key: tuple, pattern_data: dict) -> bool` (module-private helper in `main.py`). `_enqueue` never raises — it catches `AuditEnqueueError` internally and returns `False`.

- [ ] **Step 1: Write the failing tests**

Rewrite `tests/test_main.py` in full (small file, easier than a partial
patch): keep `test_healthz` unchanged, drop the two `under_review`-guard
tests (`test_classify_fires_audit_background_task_on_suppress` and
`test_classify_skips_audit_if_already_under_review` — the guard they
test no longer exists, Cloud Tasks dedup replaces it) and the existing
`test_sweep_returns_audited_count`, and add the full set below covering
`/classify`, `/sweep`, and the new `/audit`. The `import os` and the
expanded `vor_agents.schemas` import in the block below **replace** the
file's existing top-of-file imports — don't leave both the old and new
import lines in the file:

```python
import os

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import main
from vor_agents.schemas import AuditorAction, AuditorOutput, ClassifierOutput, Decision, UncertainReason

TASK_ENV = {
    "GCP_PROJECT": "test-project",
    "TASKS_LOCATION": "us-central1",
    "TASKS_QUEUE": "vor-audit-queue",
    "TASKS_OIDC_SA_EMAIL": "vor-scheduler@test-project.iam.gserviceaccount.com",
    "SERVICE_URL": "https://vor-test.a.run.app",
}


def _suppress_result():
    return ClassifierOutput(
        decision=Decision.SUPPRESS,
        matched_pattern_id="test",
        uncertain_reason=UncertainReason.NOT_APPLICABLE,
        structural_deviations_found=[],
        reasoning="matches template",
        confidence_used=0.9,
    )


def test_classify_enqueues_audit_task_on_suppress(fake_firestore, fake_tasks_client, monkeypatch):
    for key, value in TASK_ENV.items():
        monkeypatch.setenv(key, value)
    identity_key = ("rule", "w3wp.exe", "csc.exe", "family")

    with patch("main.get_firestore_client", return_value=fake_firestore), \
         patch("main.get_tasks_client", return_value=fake_tasks_client), \
         patch("main.classify_alert", new=AsyncMock(return_value=(_suppress_result(), identity_key))):
        client = TestClient(main.app)
        resp = client.post("/classify", json={"detection_rule_id": "rule"})

    assert resp.status_code == 200
    assert resp.json()["decision"] == "SUPPRESS"
    assert len(fake_tasks_client.created_tasks) == 1


def test_classify_does_not_enqueue_second_task_for_same_pattern(
    fake_firestore, fake_tasks_client, monkeypatch
):
    """Replaces the old under_review app-level guard: dedup is now
    enforced by Cloud Tasks task naming, not a read-then-act check."""
    for key, value in TASK_ENV.items():
        monkeypatch.setenv(key, value)
    identity_key = ("rule", "w3wp.exe", "csc.exe", "family")

    with patch("main.get_firestore_client", return_value=fake_firestore), \
         patch("main.get_tasks_client", return_value=fake_tasks_client), \
         patch("main.classify_alert", new=AsyncMock(return_value=(_suppress_result(), identity_key))):
        client = TestClient(main.app)
        client.post("/classify", json={"detection_rule_id": "rule"})
        client.post("/classify", json={"detection_rule_id": "rule"})

    assert len(fake_tasks_client.created_tasks) == 1


def test_classify_returns_result_even_if_enqueue_fails(fake_firestore, monkeypatch):
    """A failed audit *trigger* must never fail the classification
    response."""
    for key, value in TASK_ENV.items():
        monkeypatch.setenv(key, value)
    identity_key = ("rule", "w3wp.exe", "csc.exe", "family")

    class _BoomTasksClient:
        def queue_path(self, project, location, queue):
            return f"projects/{project}/locations/{location}/queues/{queue}"

        def create_task(self, parent, task):
            raise RuntimeError("Cloud Tasks unavailable")

    with patch("main.get_firestore_client", return_value=fake_firestore), \
         patch("main.get_tasks_client", return_value=_BoomTasksClient()), \
         patch("main.classify_alert", new=AsyncMock(return_value=(_suppress_result(), identity_key))):
        client = TestClient(main.app)
        resp = client.post("/classify", json={"detection_rule_id": "rule"})

    assert resp.status_code == 200
    assert resp.json()["decision"] == "SUPPRESS"


def test_sweep_returns_enqueued_count(fake_firestore):
    with patch("main.get_firestore_client", return_value=fake_firestore), \
         patch("main.run_scheduled_sweep", return_value=[("a",), ("b",), ("c",)]):
        client = TestClient(main.app)
        resp = client.post("/sweep", json={})

    assert resp.status_code == 200
    assert resp.json() == {"enqueued": 3}


def test_audit_endpoint_invokes_audit_pattern(fake_firestore):
    identity_key = ["rule", "w3wp.exe", "csc.exe", "family"]
    fake_decision = AuditorOutput(
        action=AuditorAction.NO_ACTION,
        invalidated_instance_ids=[],
        concerns_found=[],
        reasoning="clean",
    )

    with patch("main.get_firestore_client", return_value=fake_firestore), \
         patch("main.audit_pattern", new=AsyncMock(return_value=fake_decision)) as mock_audit:
        client = TestClient(main.app)
        resp = client.post(
            "/audit", json={"identity_key": identity_key, "pattern_data": {"triggered_by": "test"}}
        )

    assert resp.status_code == 200
    assert resp.json()["action"] == "NO_ACTION"
    mock_audit.assert_called_once()
    assert mock_audit.call_args[0][0] == tuple(identity_key)
    assert mock_audit.call_args[0][1] == {"triggered_by": "test"}
```

`run_scheduled_sweep` is patched with a plain `return_value`, not
`AsyncMock` — it's synchronous now, per Task 3. Leave `test_healthz` in
place unchanged.

- [ ] **Step 2: Run the new/changed tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_main.py -v`
Expected: the `/classify` enqueue tests fail with `AttributeError`/`ImportError`-style failures (`main.get_tasks_client` doesn't exist yet); `test_sweep_returns_enqueued_count` fails (old `main.py` still returns `audited_count`); `test_audit_endpoint_invokes_audit_pattern` fails with a 404 (no `/audit` route exists yet). `test_healthz` still passes.

- [ ] **Step 3: Rewrite `main.py`**

Replace the full file contents:

```python
"""
Vör — Cloud Run entrypoint.

Exposes the trigger paths from the hybrid cadence decision:
  POST /classify — event-triggered primary path. A SUPPRESS decision means
                   this pattern's identity key just matched an incoming
                   alert again — exactly the trigger condition the auditor
                   was designed around. Enqueues the audit onto Cloud
                   Tasks so it runs in its own fully-CPU-allocated request
                   rather than as in-process background work.
  POST /sweep    — scheduled safety-net path, invoked by Cloud Scheduler
                   for the quiet, low-volume patterns event-triggering
                   would otherwise never revisit. Enqueues one audit task
                   per selected target and returns immediately.
  POST /audit    — the only place audit_pattern() actually runs. Reached
                   exclusively via Cloud Tasks (OIDC-authenticated), never
                   called directly by /classify or /sweep.
  GET  /healthz  — Cloud Run health check

See DEPLOY.md for how this actually gets deployed and secured.
"""

import os

from fastapi import FastAPI, Request
from google.cloud import firestore, tasks_v2
from loguru import logger

from vor_agents.orchestrator import audit_pattern, classify_alert, run_scheduled_sweep
from vor_agents.task_queue import AuditEnqueueError, enqueue_audit

app = FastAPI(title="Vör")
_firestore_client = None
_tasks_client = None


def get_firestore_client():
    # Lazy singleton — avoids paying Firestore client init cost on every
    # cold start path that doesn't need it (e.g. /healthz).
    global _firestore_client
    if _firestore_client is None:
        _firestore_client = firestore.Client()
    return _firestore_client


def get_tasks_client():
    # Lazy singleton, same shape as get_firestore_client().
    global _tasks_client
    if _tasks_client is None:
        _tasks_client = tasks_v2.CloudTasksClient()
    return _tasks_client


def _queue_path() -> str:
    return get_tasks_client().queue_path(
        os.environ["GCP_PROJECT"], os.environ["TASKS_LOCATION"], os.environ["TASKS_QUEUE"]
    )


def _audit_url() -> str:
    return f"{os.environ['SERVICE_URL']}/audit"


def _enqueue(identity_key: tuple, pattern_data: dict) -> bool:
    """
    Shared enqueue path for both /classify and /sweep (passed into
    run_scheduled_sweep as its enqueue_audit_fn). Never raises — an
    enqueue failure must never fail the caller's own response; it's
    logged and treated as "not enqueued" (False) so callers can still
    react to that if they care (today, neither does).
    """
    try:
        return enqueue_audit(
            identity_key,
            pattern_data,
            get_tasks_client(),
            _queue_path(),
            _audit_url(),
            os.environ["TASKS_OIDC_SA_EMAIL"],
        )
    except AuditEnqueueError:
        logger.bind(identity_key=identity_key).error(
            "Audit enqueue failed; caller's response is unaffected"
        )
        return False


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.post("/classify")
async def classify(request: Request):
    alert = await request.json()
    client = get_firestore_client()
    result, identity_key = await classify_alert(alert, client)

    if result.decision == "SUPPRESS":
        _enqueue(identity_key, {"triggered_by": "classify_suppress"})

    return result.model_dump()


@app.post("/sweep")
async def sweep(request: Request):
    """
    Cloud Scheduler hits this on a weekly cadence (see DEPLOY.md). The
    scheduler job is configured with an OIDC token bound to a service
    account with run.invoker on this service — Cloud Run itself rejects
    unauthenticated requests, so no manual auth check is needed here. Do
    NOT deploy this endpoint with --allow-unauthenticated in production.
    """
    client = get_firestore_client()
    enqueued = run_scheduled_sweep(client, _enqueue)
    return {"enqueued": len(enqueued)}


@app.post("/audit")
async def audit(request: Request):
    """
    Reached exclusively via a Cloud Tasks dispatch — never called
    directly by /classify or /sweep. This is the one place
    audit_pattern() actually runs, inside its own fully-CPU-allocated
    request. Gated by Cloud Run IAM the same way /sweep is (OIDC,
    never --allow-unauthenticated) — no manual token check needed here.
    """
    payload = await request.json()
    identity_key = tuple(payload["identity_key"])
    pattern_data = payload["pattern_data"]
    client = get_firestore_client()
    decision = await audit_pattern(identity_key, pattern_data, client)
    return decision.model_dump()
```

- [ ] **Step 4: Run all the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_main.py -v`
Expected: all tests PASS, including `test_healthz` (untouched) and all 5 new/changed tests.

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all tests PASS, zero failures anywhere in the suite.

- [ ] **Step 6: Lint**

Run: `.venv/bin/python -m ruff check .`
Expected: passes (no unused imports — `BackgroundTasks`, `CONFIDENCE_COLLECTION`, `_doc_id` are no longer imported in `main.py`).

- [ ] **Step 7: Commit**

```bash
git add main.py tests/test_main.py
git commit -m "Route /classify, /sweep, and a new /audit endpoint through Cloud Tasks"
```

---

## Task 5: `docs/DEPLOY.md` — Cloud Tasks deployment steps

**Files:**
- Modify: `docs/DEPLOY.md`

**Interfaces:**
- None (documentation only).

- [ ] **Step 1: Insert a new section after step 3 (the weekly sweep job) and before step 4 (`/classify`'s trigger source)**

```markdown
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
```

- [ ] **Step 2: Verify the doc renders sensibly**

Read the file back and confirm the new section sits between the
existing step 3 (weekly sweep job) and step 4 (`/classify`'s trigger
source), with consistent heading levels and no broken code fences.

- [ ] **Step 3: Commit**

```bash
git add docs/DEPLOY.md
git commit -m "Document Cloud Tasks queue setup and IAM for the audit path"
```

---

## Final verification

- [ ] Run `.venv/bin/python -m pytest -v` — full suite passes, including the pre-existing `xfail` (identity-key underscore round-trip, untouched by this plan).
- [ ] Run `.venv/bin/python -m ruff check .` — clean.
- [ ] Confirm `git log --oneline -5` shows one commit per task, each with a passing state at the time it was made.
