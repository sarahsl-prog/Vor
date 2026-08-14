"""
Tests for main.py — the Cloud Run FastAPI service. Focused on the
duplicate-audit guard, since that's the one piece of real logic living in
this file rather than in vor_agents/.
"""

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import main
from vor_agents.schemas import ClassifierOutput, Decision, UncertainReason


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


def test_classify_fires_audit_background_task_on_suppress(fake_firestore):
    identity_key = ("rule", "w3wp.exe", "csc.exe", "family")

    with patch("main.get_firestore_client", return_value=fake_firestore), \
         patch("main.classify_alert", new=AsyncMock(return_value=(_suppress_result(), identity_key))), \
         patch("main.audit_pattern", new=AsyncMock()) as mock_audit:
        client = TestClient(main.app)
        resp = client.post("/classify", json={"detection_rule_id": "rule"})

    assert resp.status_code == 200
    assert resp.json()["decision"] == "SUPPRESS"
    mock_audit.assert_called_once()
    assert mock_audit.call_args[0][0] == identity_key


def test_classify_skips_audit_if_already_under_review(fake_firestore):
    """The duplicate-audit guard: if under_review is already True for
    this identity_key, /classify must NOT schedule a second concurrent
    audit for it."""
    identity_key = ("rule", "w3wp.exe", "csc.exe", "family")
    from vor_agents.enrichment import CONFIDENCE_COLLECTION, _doc_id
    fake_firestore.collection(CONFIDENCE_COLLECTION).document(_doc_id(identity_key)).set(
        {"under_review": True}
    )

    with patch("main.get_firestore_client", return_value=fake_firestore), \
         patch("main.classify_alert", new=AsyncMock(return_value=(_suppress_result(), identity_key))), \
         patch("main.audit_pattern", new=AsyncMock()) as mock_audit:
        client = TestClient(main.app)
        client.post("/classify", json={"detection_rule_id": "rule"})

    mock_audit.assert_not_called()


def test_sweep_returns_audited_count(fake_firestore):
    with patch("main.get_firestore_client", return_value=fake_firestore), \
         patch("main.run_scheduled_sweep", new=AsyncMock(return_value=[1, 2, 3])):
        client = TestClient(main.app)
        resp = client.post("/sweep", json={})

    assert resp.status_code == 200
    assert resp.json() == {"audited_count": 3}
