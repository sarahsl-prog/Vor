"""
Vör -- ADK session-service selection.

One place for choosing which BaseSessionService backs every
classify_alert()/audit_pattern() call, mirroring model_config.py's
"resolve from env, single source of truth" shape.
"""

import os

from google.adk.sessions import BaseSessionService, DatabaseSessionService

SESSION_DB_URL_ENV_VAR = "SESSION_DB_URL"
# In-memory SQLite by default: zero setup for local dev and the test
# suite, while still exercising the REAL DatabaseSessionService class
# (not InMemorySessionService) -- see build_session_service()'s docstring
# for why that distinction matters. Production sets SESSION_DB_URL to a
# Cloud SQL Postgres connection string (see docs/DEPLOY.md's Cloud SQL
# section).
DEFAULT_SESSION_DB_URL = "sqlite+aiosqlite:///:memory:"


def build_session_service() -> BaseSessionService:
    """
    Builds the session store used for the life of the process. Unlike
    resolve_model()/env_int() (read per call, deliberately), this is
    constructed ONCE at import time in orchestrator.py and reused --
    DatabaseSessionService owns a SQLAlchemy connection pool, which is
    meant to be a long-lived singleton, not rebuilt per request.

    Reads $SESSION_DB_URL at call time (not bound at import into a
    default argument -- same reasoning as model_config.py) so a test can
    monkeypatch it before calling this function directly, even though
    orchestrator.py itself only calls it once.

    Every classify_alert()/audit_pattern() call already creates a fresh
    session and deletes it in a finally block before returning (see
    orchestrator._discard_session) -- no session is ever reused across
    requests today. A persistent backing store still matters for two
    reasons that are independent of that: (1) InMemorySessionService's
    entire state lives in one process's heap, so a Cloud Run instance
    recycled mid-request (autoscaling, deploy, OOM) silently drops any
    session created but not yet cleaned up; (2) it's the seam that lets a
    future feature reuse a session across calls (e.g. multi-turn audit
    review) without a second migration.
    """
    db_url = os.environ.get(SESSION_DB_URL_ENV_VAR, "").strip() or DEFAULT_SESSION_DB_URL
    return DatabaseSessionService(db_url=db_url)
