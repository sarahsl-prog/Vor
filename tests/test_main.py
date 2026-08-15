"""
Tests for main.py — the Cloud Run FastAPI service. Focused on the
Cloud Tasks wiring, since that's the one piece of real logic living in
this file rather than in vor_agents/.
"""

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import main
from vor_agents.schemas import (
    AuditorAction,
    AuditorOutput,
    ClassifierOutput,
    Decision,
    UncertainReason,
)

TASK_ENV = {
    "GCP_PROJECT": "test-project",
    "TASKS_LOCATION": "us-central1",
    "TASKS_QUEUE": "vor-audit-queue",
    "TASKS_OIDC_SA_EMAIL": "vor-scheduler@test-project.iam.gserviceaccount.com",
    "SERVICE_URL": "https://vor-test.a.run.app",
}


def _full_alert():
    """Minimal alert satisfying ClassifierRequest's four required
    identity fields. classify_alert is mocked in every test below, so
    only the shape (not the content) needs to pass validation."""
    return {
        "detection_rule_id": "rule",
        "parent_image": "w3wp.exe",
        "child_image": "csc.exe",
        "endpoint_family": "family",
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


def test_healthz():
    client = TestClient(main.app)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_classify_enqueues_audit_task_on_suppress(fake_firestore, fake_tasks_client, monkeypatch):
    for key, value in TASK_ENV.items():
        monkeypatch.setenv(key, value)
    identity_key = ("rule", "w3wp.exe", "csc.exe", "family")

    with patch("main.get_firestore_client", return_value=fake_firestore), \
         patch("main.get_tasks_client", return_value=fake_tasks_client), \
         patch("main.classify_alert", new=AsyncMock(return_value=(_suppress_result(), identity_key))):
        client = TestClient(main.app)
        resp = client.post("/classify", json=_full_alert())

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
        client.post("/classify", json=_full_alert())
        client.post("/classify", json=_full_alert())

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
        resp = client.post("/classify", json=_full_alert())

    assert resp.status_code == 200
    assert resp.json()["decision"] == "SUPPRESS"


def test_classify_returns_result_even_if_task_env_var_missing(
    fake_firestore, fake_tasks_client, monkeypatch
):
    """_enqueue() must never raise, even on a deploy misconfiguration —
    a missing env var is not the caller's fault and must not cost them
    their classification result.

    get_tasks_client is patched with fake_tasks_client, same as every
    sibling test in this file — otherwise _enqueue's left-to-right
    argument evaluation constructs a real tasks_v2.CloudTasksClient()
    first, which performs an ADC lookup and raises DefaultCredentialsError
    in any environment without ambient GCP credentials (CI, a clean
    container, a contributor machine), for a reason unrelated to what
    this test asserts."""
    for key, value in TASK_ENV.items():
        if key != "TASKS_OIDC_SA_EMAIL":
            monkeypatch.setenv(key, value)
    monkeypatch.delenv("TASKS_OIDC_SA_EMAIL", raising=False)
    identity_key = ("rule", "w3wp.exe", "csc.exe", "family")

    with patch("main.get_firestore_client", return_value=fake_firestore), \
         patch("main.get_tasks_client", return_value=fake_tasks_client), \
         patch("main.classify_alert", new=AsyncMock(return_value=(_suppress_result(), identity_key))):
        client = TestClient(main.app)
        resp = client.post("/classify", json=_full_alert())

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


def test_audit_endpoint_rejects_missing_identity_key(fake_firestore):
    with patch("main.get_firestore_client", return_value=fake_firestore):
        client = TestClient(main.app)
        resp = client.post("/audit", json={"pattern_data": {"triggered_by": "test"}})

    assert resp.status_code == 422


def test_audit_endpoint_rejects_missing_pattern_data(fake_firestore):
    with patch("main.get_firestore_client", return_value=fake_firestore):
        client = TestClient(main.app)
        resp = client.post("/audit", json={"identity_key": ["a", "b"]})

    assert resp.status_code == 422


def test_audit_endpoint_rejects_non_json_body(fake_firestore):
    with patch("main.get_firestore_client", return_value=fake_firestore):
        client = TestClient(main.app)
        resp = client.post("/audit", content=b"not json", headers={"content-type": "application/json"})

    assert resp.status_code == 422


def test_classify_rejects_missing_identity_fields(fake_firestore):
    """Regression coverage: pattern_identity_key() indexes an alert dict
    directly (alert["field"]), so a request missing one of the four
    identity fields previously raised a raw KeyError-turned-500. Must now
    be a clean 422 from Pydantic/FastAPI request validation, before
    classify_alert() ever runs."""
    with patch("main.get_firestore_client", return_value=fake_firestore):
        client = TestClient(main.app)
        resp = client.post("/classify", json={"detection_rule_id": "rule"})

    assert resp.status_code == 422


def test_classify_rejects_invalid_json_body(fake_firestore):
    """Regression coverage: a non-JSON body previously raised an
    unhandled json.decoder.JSONDecodeError out of `await request.json()`,
    surfacing as a 500. Switching /classify to a Pydantic body param
    means FastAPI's own request-parsing layer rejects this before main.py
    code runs at all."""
    with patch("main.get_firestore_client", return_value=fake_firestore):
        client = TestClient(main.app)
        resp = client.post(
            "/classify", content=b"not json", headers={"content-type": "application/json"}
        )

    assert resp.status_code == 422


def test_classify_allows_extra_context_fields(
    fake_firestore, fake_tasks_client, monkeypatch
):
    """extra="allow" on ClassifierRequest: fields beyond the four required
    identity ones (DIFFABLE_FIELDS, host/user/timestamp, or anything an
    alert schema might add later) must pass through to classify_alert(),
    not get silently stripped by validation."""
    for key, value in TASK_ENV.items():
        monkeypatch.setenv(key, value)
    identity_key = ("rule", "w3wp.exe", "csc.exe", "family")
    alert_with_extras = {
        **_full_alert(),
        "integrity_level": "Medium",
        "host": "SRV-01",
    }

    captured_alert = {}

    async def _fake_classify_alert(alert, client):
        captured_alert.update(alert)
        return _suppress_result(), identity_key

    with patch("main.get_firestore_client", return_value=fake_firestore), \
         patch("main.get_tasks_client", return_value=fake_tasks_client), \
         patch("main.classify_alert", new=_fake_classify_alert):
        client = TestClient(main.app)
        resp = client.post("/classify", json=alert_with_extras)

    assert resp.status_code == 200
    assert captured_alert["integrity_level"] == "Medium"
    assert captured_alert["host"] == "SRV-01"
