"""
Tests for main.py — the Cloud Run FastAPI service. Focused on the
Cloud Tasks wiring, since that's the one piece of real logic living in
this file rather than in vor_agents/.
"""

import base64
import json as _json
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from google.cloud.tasks_v2 import HttpMethod

import main
from vor_agents.blast_radius import ProposalAlreadyResolvedError, ProposalNotFoundError
from vor_agents.schemas import (
    AuditorAction,
    AuditorOutput,
    AuditRequest,
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

    with (
        patch("main.get_firestore_client", return_value=fake_firestore),
        patch("main.get_tasks_client", return_value=fake_tasks_client),
        patch(
            "main.classify_alert", new=AsyncMock(return_value=(_suppress_result(), identity_key))
        ),
    ):
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

    with (
        patch("main.get_firestore_client", return_value=fake_firestore),
        patch("main.get_tasks_client", return_value=fake_tasks_client),
        patch(
            "main.classify_alert", new=AsyncMock(return_value=(_suppress_result(), identity_key))
        ),
    ):
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

    with (
        patch("main.get_firestore_client", return_value=fake_firestore),
        patch("main.get_tasks_client", return_value=_BoomTasksClient()),
        patch(
            "main.classify_alert", new=AsyncMock(return_value=(_suppress_result(), identity_key))
        ),
    ):
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

    with (
        patch("main.get_firestore_client", return_value=fake_firestore),
        patch("main.get_tasks_client", return_value=fake_tasks_client),
        patch(
            "main.classify_alert", new=AsyncMock(return_value=(_suppress_result(), identity_key))
        ),
    ):
        client = TestClient(main.app)
        resp = client.post("/classify", json=_full_alert())

    assert resp.status_code == 200
    assert resp.json()["decision"] == "SUPPRESS"


def test_sweep_returns_enqueued_count(fake_firestore):
    with (
        patch("main.get_firestore_client", return_value=fake_firestore),
        patch("main.run_scheduled_sweep", return_value=[("a",), ("b",), ("c",)]),
    ):
        client = TestClient(main.app)
        resp = client.post("/sweep", json={})

    assert resp.status_code == 200
    assert resp.json() == {"enqueued": 3}


def test_sweep_returns_result_if_enqueue_misconfigured(
    fake_firestore, fake_tasks_client, monkeypatch, diverse_confirmed_instances
):
    """/sweep must never 500 on a deploy misconfiguration (missing env
    var) — same guarantee /classify already has via _enqueue's own
    try/except, exercised here through the real run_scheduled_sweep ->
    _enqueue path instead of a mock, with an actual confirmed pattern in
    Firestore so there's a real target to (fail to) enqueue."""
    from vor_agents.enrichment import record_confirmed_negative

    for key, value in TASK_ENV.items():
        if key != "TASKS_OIDC_SA_EMAIL":
            monkeypatch.setenv(key, value)
    monkeypatch.delenv("TASKS_OIDC_SA_EMAIL", raising=False)

    for instance in diverse_confirmed_instances:
        record_confirmed_negative(instance, fake_firestore)

    with (
        patch("main.get_firestore_client", return_value=fake_firestore),
        patch("main.get_tasks_client", return_value=fake_tasks_client),
    ):
        client = TestClient(main.app)
        resp = client.post("/sweep", json={})

    assert resp.status_code == 200
    assert resp.json() == {"enqueued": 0}
    assert len(fake_tasks_client.created_tasks) == 0


def test_replay_traces_returns_replayed_count(fake_firestore):
    with (
        patch("main.get_firestore_client", return_value=fake_firestore),
        patch("main.replay_pending_traces", return_value=4),
    ):
        client = TestClient(main.app)
        resp = client.post("/replay-traces", json={})

    assert resp.status_code == 200
    assert resp.json() == {"replayed": 4}


def test_audit_endpoint_invokes_audit_pattern(fake_firestore):
    identity_key = ["rule", "w3wp.exe", "csc.exe", "family"]
    fake_decision = AuditorOutput(
        action=AuditorAction.NO_ACTION,
        invalidated_instance_ids=[],
        concerns_found=[],
        reasoning="clean",
    )

    with (
        patch("main.get_firestore_client", return_value=fake_firestore),
        patch("main.audit_pattern", new=AsyncMock(return_value=fake_decision)) as mock_audit,
    ):
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
        resp = client.post(
            "/audit", content=b"not json", headers={"content-type": "application/json"}
        )

    assert resp.status_code == 422


def test_audit_endpoint_returns_422_on_malformed_stored_data(fake_firestore):
    """Regression coverage: audit_pattern()'s own try/except (Task 1)
    covers model/parsing failures, but NOT mark_under_review() (before
    that try block) or the invalidate_instances() rebuild inside
    clear_under_review() (in its finally block) — a MalformedAlertError
    raised from stored data missing a DIFFABLE_FIELDS key can still
    escape audit_pattern(). Must be a 422 (permanent, not worth
    retrying), not a bare 500."""
    from vor_agents.identity import MalformedAlertError

    with (
        patch("main.get_firestore_client", return_value=fake_firestore),
        patch(
            "main.audit_pattern",
            new=AsyncMock(side_effect=MalformedAlertError("missing field: integrity_level")),
        ),
    ):
        client = TestClient(main.app)
        resp = client.post(
            "/audit", json={"identity_key": ["a", "b", "c", "d"], "pattern_data": {}}
        )

    assert resp.status_code == 422
    assert "integrity_level" in resp.json()["detail"]


def test_audit_endpoint_returns_500_on_unexpected_failure(fake_firestore):
    """A truly unexpected failure (Firestore unavailable, network) is the
    retryable direction — must stay a 500 so Cloud Tasks retries it,
    unlike the malformed-data case above."""
    with (
        patch("main.get_firestore_client", return_value=fake_firestore),
        patch(
            "main.audit_pattern",
            new=AsyncMock(side_effect=RuntimeError("Firestore unavailable")),
        ),
    ):
        client = TestClient(main.app)
        resp = client.post(
            "/audit", json={"identity_key": ["a", "b", "c", "d"], "pattern_data": {}}
        )

    assert resp.status_code == 500


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
    surfacing as a 500. _decode_classify_body()'s own manual json.loads()
    call now rejects this and turns it into a clean 422 before any
    envelope/raw-alert detection or ClassifierRequest validation runs."""
    with patch("main.get_firestore_client", return_value=fake_firestore):
        client = TestClient(main.app)
        resp = client.post(
            "/classify", content=b"not json", headers={"content-type": "application/json"}
        )

    assert resp.status_code == 422


def test_classify_allows_extra_context_fields(fake_firestore, fake_tasks_client, monkeypatch):
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

    with (
        patch("main.get_firestore_client", return_value=fake_firestore),
        patch("main.get_tasks_client", return_value=fake_tasks_client),
        patch("main.classify_alert", new=_fake_classify_alert),
    ):
        client = TestClient(main.app)
        resp = client.post("/classify", json=alert_with_extras)

    assert resp.status_code == 200
    assert captured_alert["integrity_level"] == "Medium"
    assert captured_alert["host"] == "SRV-01"


def _pubsub_envelope(alert: dict) -> dict:
    encoded = base64.b64encode(_json.dumps(alert).encode()).decode()
    return {
        "message": {"data": encoded, "messageId": "1"},
        "subscription": "projects/p/subscriptions/s",
    }


def test_classify_accepts_pubsub_envelope(fake_firestore, fake_tasks_client, monkeypatch):
    identity_key = ("rule", "w3wp.exe", "csc.exe", "family")
    alert = {
        "detection_rule_id": "rule",
        "parent_image": "w3wp.exe",
        "child_image": "csc.exe",
        "endpoint_family": "family",
    }

    with (
        patch("main.get_firestore_client", return_value=fake_firestore),
        patch("main.get_tasks_client", return_value=fake_tasks_client),
        patch(
            "main.classify_alert", new=AsyncMock(return_value=(_suppress_result(), identity_key))
        ),
    ):
        client = TestClient(main.app)
        resp = client.post("/classify", content=_json.dumps(_pubsub_envelope(alert)))

    assert resp.status_code == 200
    assert resp.json()["decision"] == "SUPPRESS"


def test_classify_rejects_malformed_pubsub_envelope(fake_firestore):
    body = {"message": {"data": "not-valid-base64!!!"}, "subscription": "s"}

    with patch("main.get_firestore_client", return_value=fake_firestore):
        client = TestClient(main.app)
        resp = client.post("/classify", content=_json.dumps(body))

    assert resp.status_code == 422


def test_classify_rejects_envelope_whose_decoded_data_is_not_json(fake_firestore):
    encoded = base64.b64encode(b"not json").decode()
    body = {"message": {"data": encoded}, "subscription": "s"}

    with patch("main.get_firestore_client", return_value=fake_firestore):
        client = TestClient(main.app)
        resp = client.post("/classify", content=_json.dumps(body))

    assert resp.status_code == 422


def test_classify_rejects_envelope_whose_decoded_data_is_not_a_json_object(fake_firestore):
    """message.data decodes to valid JSON that isn't a dict (a list here) --
    must still 422, not proceed to ClassifierRequest validation."""
    encoded = base64.b64encode(_json.dumps([1, 2, 3]).encode()).decode()
    body = {"message": {"data": encoded}, "subscription": "s"}

    with patch("main.get_firestore_client", return_value=fake_firestore):
        client = TestClient(main.app)
        resp = client.post("/classify", content=_json.dumps(body))

    assert resp.status_code == 422


def test_classify_rejects_non_object_raw_body(fake_firestore):
    """A non-envelope raw body that IS valid JSON but isn't a JSON object
    at all (no `message` wrapper) -- must 422 via the final catch-all in
    _decode_classify_body(), not raise unhandled out of model_validate."""
    with patch("main.get_firestore_client", return_value=fake_firestore):
        client = TestClient(main.app)
        resp = client.post("/classify", content=_json.dumps([1, 2, 3]))

    assert resp.status_code == 422


def test_classify_raw_alert_with_message_field_not_mistaken_for_envelope(
    fake_firestore, fake_tasks_client, monkeypatch
):
    """A legitimate raw alert can carry its own top-level `message` field
    (Windows Event Log records commonly do) with a nested `data` key that
    happens to collide with the Pub/Sub envelope shape. Pub/Sub push
    always includes a top-level `subscription` field -- without it, this
    must still be classified as a raw alert, not misread as an envelope
    (which would either 422 a legitimate alert or, worse, silently decode
    and classify the wrong object)."""
    identity_key = ("rule", "w3wp.exe", "csc.exe", "family")
    alert = {
        **_full_alert(),
        "message": {"data": "some evtx payload", "level": "info"},
    }

    captured_alert = {}

    async def _fake_classify_alert(alert, client):
        captured_alert.update(alert)
        return _suppress_result(), identity_key

    with (
        patch("main.get_firestore_client", return_value=fake_firestore),
        patch("main.get_tasks_client", return_value=fake_tasks_client),
        patch("main.classify_alert", new=_fake_classify_alert),
    ):
        client = TestClient(main.app)
        resp = client.post("/classify", json=alert)

    assert resp.status_code == 200
    assert captured_alert["message"] == {"data": "some evtx payload", "level": "info"}


def test_classify_still_accepts_raw_alert_body(fake_firestore, fake_tasks_client, monkeypatch):
    """Direct/test callers posting raw alert JSON (no Pub/Sub envelope)
    keep working exactly as before this change."""
    identity_key = ("rule", "w3wp.exe", "csc.exe", "family")
    alert = {
        "detection_rule_id": "rule",
        "parent_image": "w3wp.exe",
        "child_image": "csc.exe",
        "endpoint_family": "family",
    }

    with (
        patch("main.get_firestore_client", return_value=fake_firestore),
        patch("main.get_tasks_client", return_value=fake_tasks_client),
        patch(
            "main.classify_alert", new=AsyncMock(return_value=(_suppress_result(), identity_key))
        ),
    ):
        client = TestClient(main.app)
        resp = client.post("/classify", json=alert)

    assert resp.status_code == 200
    assert resp.json()["decision"] == "SUPPRESS"


def test_blast_radius_commit_commits_a_pending_proposal(fake_firestore):
    with (
        patch("main.get_firestore_client", return_value=fake_firestore),
        patch(
            "main.commit_blast_radius_proposal",
            return_value={"status": "committed", "proposal_id": "p1"},
        ),
    ):
        client = TestClient(main.app)
        resp = client.post("/blast-radius/commit", json={"proposal_id": "p1"})

    assert resp.status_code == 200
    assert resp.json()["status"] == "committed"


def test_blast_radius_commit_returns_404_for_unknown_proposal(fake_firestore):
    with (
        patch("main.get_firestore_client", return_value=fake_firestore),
        patch(
            "main.commit_blast_radius_proposal",
            side_effect=ProposalNotFoundError("no such proposal"),
        ),
    ):
        client = TestClient(main.app)
        resp = client.post("/blast-radius/commit", json={"proposal_id": "missing"})

    assert resp.status_code == 404


def test_blast_radius_commit_returns_409_for_already_resolved_proposal(fake_firestore):
    with (
        patch("main.get_firestore_client", return_value=fake_firestore),
        patch(
            "main.commit_blast_radius_proposal",
            side_effect=ProposalAlreadyResolvedError("already committed"),
        ),
    ):
        client = TestClient(main.app)
        resp = client.post("/blast-radius/commit", json={"proposal_id": "p1"})

    assert resp.status_code == 409


def _uncertain_result():
    return ClassifierOutput(
        decision=Decision.UNCERTAIN,
        matched_pattern_id="test",
        uncertain_reason=UncertainReason.NO_HISTORY,
        structural_deviations_found=[],
        reasoning="no history for this pattern",
    )


def _escalate_result():
    return ClassifierOutput(
        decision=Decision.ESCALATE,
        matched_pattern_id="test",
        uncertain_reason=UncertainReason.NOT_APPLICABLE,
        structural_deviations_found=[
            {"field": "integrity_level", "template": "Medium", "observed": "High"}
        ],
        reasoning="structural deviation found",
    )


def _classify_with(result, fake_firestore, fake_tasks_client, monkeypatch):
    for key, value in TASK_ENV.items():
        monkeypatch.setenv(key, value)
    identity_key = ("rule", "w3wp.exe", "csc.exe", "family")

    with (
        patch("main.get_firestore_client", return_value=fake_firestore),
        patch("main.get_tasks_client", return_value=fake_tasks_client),
        patch("main.classify_alert", new=AsyncMock(return_value=(result, identity_key))),
    ):
        client = TestClient(main.app)
        return client.post("/classify", json=_full_alert())


def test_classify_no_enqueue_on_non_suppress(fake_firestore, fake_tasks_client, monkeypatch):
    """The audit queue exists to re-verify patterns Vör suppressed
    AUTONOMOUSLY -- a SUPPRESS is the trigger condition the auditor was
    designed around (see main.py's module docstring). UNCERTAIN and
    ESCALATE both already put a human in the loop, so enqueueing an audit
    for them would burn model spend re-checking a decision nobody acted
    on unreviewed.

    Named in docs/Code-review-Aug15.md's Test Gaps table; only the
    SUPPRESS-path enqueue was covered before this.
    """
    for result in (_uncertain_result(), _escalate_result()):
        resp = _classify_with(result, fake_firestore, fake_tasks_client, monkeypatch)
        assert resp.status_code == 200

    assert fake_tasks_client.created_tasks == {}


def test_enqueued_task_body_shape(fake_firestore, fake_tasks_client, monkeypatch):
    """Asserts the actual Task the Cloud Tasks client receives, not just
    that one was created: the callback URL, the OIDC service account and
    audience, and the JSON body /audit will have to parse back out.

    This is the contract between _enqueue() and the /audit endpoint, and
    it is only ever exercised for real in production -- a silent change
    to the payload shape (a renamed key, a tuple where /audit expects a
    list) would otherwise surface as audits failing after deploy, not as
    a failing test. Named in docs/Code-review-Aug15.md's Test Gaps table.
    """
    resp = _classify_with(_suppress_result(), fake_firestore, fake_tasks_client, monkeypatch)
    assert resp.status_code == 200

    (task,) = fake_tasks_client.created_tasks.values()

    assert task.name.startswith(
        "projects/test-project/locations/us-central1/queues/vor-audit-queue/tasks/audit-"
    )

    http_request = task.http_request
    assert http_request.url == "https://vor-test.a.run.app/audit"
    assert http_request.http_method == HttpMethod.POST
    assert http_request.headers["Content-Type"] == "application/json"

    assert (
        http_request.oidc_token.service_account_email
        == "vor-scheduler@test-project.iam.gserviceaccount.com"
    )
    # Audience must be the /audit URL itself -- an OIDC token minted for
    # any other audience is rejected by Cloud Run's IAM check.
    assert http_request.oidc_token.audience == "https://vor-test.a.run.app/audit"

    body = _json.loads(http_request.body.decode())
    # A JSON array, not a tuple: /audit validates this against
    # AuditRequest, and tuples do not survive a JSON round-trip.
    assert body["identity_key"] == ["rule", "w3wp.exe", "csc.exe", "family"]
    assert body["pattern_data"] == {"triggered_by": "classify_suppress"}


def test_enqueued_task_body_parses_back_into_an_audit_request(
    fake_firestore, fake_tasks_client, monkeypatch
):
    """The other half of that contract: the enqueued body is not merely
    well-shaped, it actually validates against the model /audit parses it
    with. Catches a drift between task_queue.py and AuditRequest that
    matching literals by hand would not."""
    _classify_with(_suppress_result(), fake_firestore, fake_tasks_client, monkeypatch)

    (task,) = fake_tasks_client.created_tasks.values()
    parsed = AuditRequest.model_validate(_json.loads(task.http_request.body.decode()))

    assert parsed.identity_key == ["rule", "w3wp.exe", "csc.exe", "family"]
