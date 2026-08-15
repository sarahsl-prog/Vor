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
