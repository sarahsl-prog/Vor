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


def test_classify_returns_result_even_if_task_env_var_missing(fake_firestore, monkeypatch):
    """_enqueue() must never raise, even on a deploy misconfiguration —
    a missing env var is not the caller's fault and must not cost them
    their classification result."""
    for key, value in TASK_ENV.items():
        if key != "TASKS_OIDC_SA_EMAIL":
            monkeypatch.setenv(key, value)
    monkeypatch.delenv("TASKS_OIDC_SA_EMAIL", raising=False)
    identity_key = ("rule", "w3wp.exe", "csc.exe", "family")

    with patch("main.get_firestore_client", return_value=fake_firestore), \
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
