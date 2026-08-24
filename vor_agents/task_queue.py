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

    Hashes the JSON-encoded tuple, not "_".join(identity_key) — the same
    collision risk flagged for enrichment._doc_id applies here too:
    ("a", "b_c") and ("a_b", "c") would otherwise hash to the same task
    name and silently dedup against each other. usedforsecurity=False:
    this hash is for deterministic naming/dedup, not a security boundary
    (see BLAST_RADIUS_PLAYBOOK.md's threat model — task names aren't
    secret or trust-bearing), which also satisfies Bandit's B324 check.
    """
    encoded = json.dumps(list(identity_key), separators=(",", ":"))
    key_hash = hashlib.sha1(encoded.encode(), usedforsecurity=False).hexdigest()
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
        raise AuditEnqueueError(f"Failed to enqueue audit for {identity_key}: {exc}") from exc

    return True
