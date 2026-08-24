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
from vor_agents.tracing import (
    PENDING_TRACES_COLLECTION,
    log_audit_trace,
    log_classification_trace,
    replay_pending_traces,
)


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
            {"detection_rule_id": "r"},
            {"status": "NO_HISTORY"},
            _classifier_output(),
            [],
            fake_firestore,
        )

        assert list(fake_firestore.collection(PENDING_TRACES_COLLECTION).stream()) == []

    def test_mlflow_failure_falls_back_to_pending_traces(self, fake_firestore, monkeypatch):
        monkeypatch.setattr("vor_agents.tracing.mlflow", _FakeMlflowAlwaysFails())

        log_classification_trace(
            {"detection_rule_id": "r"},
            {"status": "NO_HISTORY"},
            _classifier_output(),
            ["under_review"],
            fake_firestore,
        )

        docs = list(fake_firestore.collection(PENDING_TRACES_COLLECTION).stream())
        assert len(docs) == 1
        assert docs[0].to_dict()["run_type"] == "classification"
        assert docs[0].to_dict()["run_data"]["overrides_fired"] == ["under_review"]

    def test_never_raises_even_if_firestore_fallback_also_fails(self, monkeypatch):
        monkeypatch.setattr("vor_agents.tracing.mlflow", _FakeMlflowAlwaysFails())

        # Must not raise -- this is the whole point of the fallback design.
        log_classification_trace(
            {"detection_rule_id": "r"},
            {"status": "NO_HISTORY"},
            _classifier_output(),
            [],
            _BoomFirestoreClient(),
        )


class TestLogAuditTrace:
    def test_success_does_not_write_to_firestore(self, fake_firestore, monkeypatch):
        monkeypatch.setattr("vor_agents.tracing.mlflow", _FakeMlflowSuccess())
        identity_key = ("rule", "w3wp.exe", "csc.exe", "family")

        log_audit_trace(
            identity_key, {"triggered_by": "test"}, _auditor_output(), False, fake_firestore
        )

        assert list(fake_firestore.collection(PENDING_TRACES_COLLECTION).stream()) == []

    def test_mlflow_failure_falls_back_to_pending_traces(self, fake_firestore, monkeypatch):
        monkeypatch.setattr("vor_agents.tracing.mlflow", _FakeMlflowAlwaysFails())
        identity_key = ("rule", "w3wp.exe", "csc.exe", "family")

        log_audit_trace(
            identity_key, {"triggered_by": "test"}, _auditor_output(), True, fake_firestore
        )

        docs = list(fake_firestore.collection(PENDING_TRACES_COLLECTION).stream())
        assert len(docs) == 1
        assert docs[0].to_dict()["run_type"] == "audit"
        assert docs[0].to_dict()["run_data"]["audit_failed"] is True


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
