"""
Vör — shared pytest fixtures.

Fixtures here model realistic alert/instance shapes rather than minimal
stubs, so tests exercise the actual field names the code depends on
(DIFFABLE_FIELDS, identity key components) instead of drifting from them.
"""

import pytest


@pytest.fixture
def baseline_alert():
    """A single 'normal' w3wp.exe -> csc.exe alert, matches the trusted
    pattern used throughout the design docs."""
    return {
        "detection_rule_id": "SharePoint_ToolPane_Rule",
        "parent_image": "w3wp.exe",
        "child_image": "csc.exe",
        "endpoint_family": "ToolPane_admin",
        "auth_method_present": True,
        "session_cookie_present": True,
        "integrity_level": "Medium",
        "file_access_mode": "read",
        "egress_follows_access": False,
        "host": "SRV-SP-01",
        "user": "CONTOSO\\jsmith",
        "timestamp": "2026-08-11T09:14:22Z",
    }


def _instance(overrides=None, instance_id="i0"):
    base = {
        "detection_rule_id": "SharePoint_ToolPane_Rule",
        "parent_image": "w3wp.exe",
        "child_image": "csc.exe",
        "endpoint_family": "ToolPane_admin",
        "auth_method_present": True,
        "session_cookie_present": True,
        "integrity_level": "Medium",
        "file_access_mode": "read",
        "egress_follows_access": False,
        "instance_id": instance_id,
    }
    if overrides:
        base.update(overrides)
    return base


@pytest.fixture
def diverse_confirmed_instances():
    """5 confirmed instances across different hosts/users/hours — should
    score high on evidence_diversity_score. Meets GRADUATION_THRESHOLD
    (3) with room to spare."""
    return [
        _instance(
            {"host": "SRV-SP-01", "user": "jsmith", "timestamp": "2026-08-01T09:00:00Z"}, "i1"
        ),
        _instance(
            {"host": "SRV-SP-02", "user": "mjones", "timestamp": "2026-08-03T14:00:00Z"}, "i2"
        ),
        _instance(
            {"host": "SRV-SP-03", "user": "kwhite", "timestamp": "2026-08-05T22:00:00Z"}, "i3"
        ),
        _instance(
            {"host": "SRV-SP-01", "user": "abrown", "timestamp": "2026-08-07T03:00:00Z"}, "i4"
        ),
        _instance(
            {"host": "SRV-SP-04", "user": "jsmith", "timestamp": "2026-08-09T09:00:00Z"}, "i5"
        ),
    ]


@pytest.fixture
def low_diversity_confirmed_instances():
    """3 confirmed instances, same host/user/hour repeated — meets raw
    GRADUATION_THRESHOLD count but should FAIL MIN_DIVERSITY. This is the
    exact case the two-part gate was built to catch."""
    return [
        _instance(
            {"host": "SRV-SP-01", "user": "jsmith", "timestamp": "2026-08-01T09:00:00Z"}, "i1"
        ),
        _instance(
            {"host": "SRV-SP-01", "user": "jsmith", "timestamp": "2026-08-01T09:05:00Z"}, "i2"
        ),
        _instance(
            {"host": "SRV-SP-01", "user": "jsmith", "timestamp": "2026-08-01T09:12:00Z"}, "i3"
        ),
    ]


@pytest.fixture
def drift_alert_cve_model():
    """CVE-2026-56164-modeled drift: different child_image than baseline
    (cmd.exe vs csc.exe) -> different identity key entirely, tests
    identity-key rejection rather than field-level diffing."""
    return {
        "detection_rule_id": "SharePoint_ToolPane_Rule",
        "parent_image": "w3wp.exe",
        "child_image": "cmd.exe",
        "endpoint_family": "ToolPane_admin",
        "auth_method_present": False,
        "session_cookie_present": False,
        "integrity_level": "High",
        "file_access_mode": "write",
        "egress_follows_access": True,
        "host": "SRV-SP-01",
        "user": None,
        "timestamp": "2026-08-11T04:12:51Z",
    }


@pytest.fixture
def field_level_drift_alert():
    """Dataset case #6: SAME identity key as baseline_alert (w3wp.exe ->
    csc.exe), but every diffable field deviates. Tests the actual
    field-level diffing path, not identity-key rejection."""
    return {
        "detection_rule_id": "SharePoint_ToolPane_Rule",
        "parent_image": "w3wp.exe",
        "child_image": "csc.exe",
        "endpoint_family": "ToolPane_admin",
        "auth_method_present": False,
        "session_cookie_present": False,
        "integrity_level": "High",
        "file_access_mode": "write",
        "egress_follows_access": True,
        "host": "SRV-SP-01",
        "user": None,
        "timestamp": "2026-08-11T04:12:51Z",
    }


@pytest.fixture
def confirmed_template_fields():
    """The invariant fields a graduated template would carry, matching
    baseline_alert's values."""
    return {
        "auth_method_present": True,
        "session_cookie_present": True,
        "integrity_level": "Medium",
        "file_access_mode": "read",
        "egress_follows_access": False,
    }


class _FakeDocSnapshot:
    def __init__(self, data):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data) if self._data else {}


class _FakeDocRef:
    def __init__(self, store, doc_id):
        self._store = store
        self._doc_id = doc_id

    def get(self):
        return _FakeDocSnapshot(self._store.get(self._doc_id))

    def set(self, data, merge=False):
        if merge and self._doc_id in self._store:
            self._store[self._doc_id].update(data)
        else:
            self._store[self._doc_id] = dict(data)

    def update(self, data):
        if self._doc_id not in self._store:
            raise KeyError(f"No document to update: {self._doc_id}")
        self._store[self._doc_id].update(data)

    def delete(self) -> None:
        self._store.pop(self._doc_id, None)


class _FakeCollection:
    def __init__(self, store):
        self._store = store

    def document(self, doc_id):
        return _FakeDocRef(self._store, doc_id)

    def where(self, field, op, value):
        # Minimal support for the one query orchestrator.py actually uses:
        # .where("tier", "==", "confirmed")
        assert op == "=="
        matches = {doc_id: data for doc_id, data in self._store.items() if data.get(field) == value}
        return _FakeQuery(matches)

    def stream(self):
        for doc_id, data in self._store.items():
            snap = _FakeDocSnapshot(data)
            snap.id = doc_id
            yield snap


class _FakeQuery:
    def __init__(self, matches):
        self._matches = matches

    def stream(self):
        for doc_id, data in self._matches.items():
            snap = _FakeDocSnapshot(data)
            snap.id = doc_id
            yield snap


class FakeFirestoreClient:
    """
    In-memory stand-in for google.cloud.firestore.Client. Supports exactly
    the operations vor_agents actually uses: collection().document().get/
    set/update, and collection().where(...).stream(). Deliberately not a
    full Firestore emulator — tests should never need real GCP credentials
    or network access to run.
    """

    def __init__(self):
        self._collections: dict[str, dict] = {}

    def collection(self, name):
        if name not in self._collections:
            self._collections[name] = {}
        return _FakeCollection(self._collections[name])


@pytest.fixture
def fake_firestore():
    return FakeFirestoreClient()


from google.api_core.exceptions import AlreadyExists
from google.cloud.tasks_v2 import Task


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

    create_task() takes a real Task (attribute access, task.name — not a
    bare dict) since task_queue.enqueue_audit() now constructs one
    explicitly rather than passing a plain dict, matching what the real
    CloudTasksClient.create_task() is typed to receive.
    """

    def __init__(self):
        self.created_tasks: dict[str, Task] = {}

    def create_task(self, parent: str, task: Task) -> Task:
        name = task.name
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
