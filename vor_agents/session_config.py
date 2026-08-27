"""
Vör -- ADK session-service selection.

One place for choosing which BaseSessionService backs every
classify_alert()/audit_pattern() call, mirroring model_config.py's
"resolve from env, single source of truth" shape.
"""

import os

from google.adk.sessions import BaseSessionService, DatabaseSessionService
from loguru import logger

SESSION_DB_URL_ENV_VAR = "SESSION_DB_URL"
# In-memory SQLite by default: zero setup for local dev and the test
# suite, while still exercising the REAL DatabaseSessionService class
# (not InMemorySessionService) -- see build_session_service()'s docstring
# for why that distinction matters. Production sets SESSION_DB_URL to a
# Cloud SQL Postgres connection string (see docs/DEPLOY.md's Cloud SQL
# section).
DEFAULT_SESSION_DB_URL = "sqlite+aiosqlite:///:memory:"


def _redact(db_url: str) -> str:
    """Strips credentials before a session DB URL ever reaches a log
    line -- production values embed a live DB password (see
    scripts/deploy.sh's SESSION_DB_URL construction)."""
    if "://" not in db_url or "@" not in db_url:
        return db_url
    scheme, rest = db_url.split("://", 1)
    _, host_part = rest.rsplit("@", 1)
    return f"{scheme}://***@{host_part}"


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

    A $SESSION_DB_URL that can't be used (malformed, unsupported dialect)
    does NOT raise: it's logged at ERROR and degrades to the in-memory
    default, matching how every other environment-driven config in this
    package behaves (model_config, firestore_config, env_config all fall
    back and log rather than raising). That matters more here than
    elsewhere because this runs at import time in orchestrator.py -- an
    exception would propagate through `import main` and take /classify,
    /sweep, /audit, and /replay-traces down together over one bad env
    var. See final-review.md Important #6.
    """
    db_url = os.environ.get(SESSION_DB_URL_ENV_VAR, "").strip() or DEFAULT_SESSION_DB_URL
    try:
        service = DatabaseSessionService(db_url=db_url)
    except Exception as exc:  # noqa: BLE001 — any construction failure
        # (malformed URL, unsupported dialect) degrades to the safe
        # in-memory default instead of taking the whole process down --
        # this runs at import time, so an uncaught exception here would
        # take every route down together over one bad env var. See
        # final-review.md Important #6.
        logger.bind(db_url=_redact(db_url), error=str(exc)).error(
            "Failed to construct DatabaseSessionService, falling back to the "
            "in-memory default -- sessions will not persist across restarts"
        )
        return DatabaseSessionService(db_url=DEFAULT_SESSION_DB_URL)
    logger.bind(db_url=_redact(db_url)).info("Session store initialized")
    return service
