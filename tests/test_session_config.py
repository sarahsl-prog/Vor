"""
Tests for vor_agents.session_config -- which BaseSessionService backs the
ADK Runner, and that it's actually persistent (survives past one
in-process instance), not just "a different class with the same API."
"""

import pytest
from google.adk.sessions import DatabaseSessionService
from loguru import logger

from vor_agents.session_config import (
    SESSION_DB_URL_ENV_VAR,
    _redact,
    build_session_service,
)


class TestBuildSessionService:
    def test_defaults_to_in_memory_sqlite_when_env_var_unset(self, monkeypatch):
        monkeypatch.delenv(SESSION_DB_URL_ENV_VAR, raising=False)

        service = build_session_service()

        assert isinstance(service, DatabaseSessionService)

    def test_honors_the_env_var(self, monkeypatch, tmp_path):
        db_path = tmp_path / "sessions.db"
        monkeypatch.setenv(SESSION_DB_URL_ENV_VAR, f"sqlite+aiosqlite:///{db_path}")

        service = build_session_service()

        assert isinstance(service, DatabaseSessionService)

    def test_blank_env_var_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setenv(SESSION_DB_URL_ENV_VAR, "   ")

        service = build_session_service()

        assert isinstance(service, DatabaseSessionService)  # did not raise on a blank URL

    def test_malformed_url_falls_back_to_the_default_instead_of_raising(self, monkeypatch):
        """Regression for final-review.md Important #6:
        build_session_service() is called at import time -- a raised
        exception here would take /classify, /sweep, /audit, and
        /replay-traces down together over one bad env var.

        "not a valid url at all" makes SQLAlchemy's URL parser raise
        synchronously inside DatabaseSessionService.__init__, with no
        network or real database involved."""
        monkeypatch.setenv(SESSION_DB_URL_ENV_VAR, "not a valid url at all")

        # loguru doesn't propagate to stdlib logging, so pytest's caplog
        # never sees these records -- attach a real sink instead.
        messages: list[str] = []
        sink_id = logger.add(messages.append, level="ERROR")
        try:
            service = build_session_service()
        finally:
            logger.remove(sink_id)

        assert isinstance(service, DatabaseSessionService)
        # Proves the except branch actually ran, rather than the URL
        # happening to construct fine and this test passing vacuously.
        assert any("falling back to the in-memory default" in m for m in messages)

    def test_a_password_bearing_url_is_redacted_before_it_reaches_a_log(self):
        """Production SESSION_DB_URL values embed a live DB password (see
        scripts/deploy.sh) -- it must never be written to a log line."""
        redacted = _redact("postgresql+asyncpg://vor:hunter2@/vor_sessions?host=/cloudsql/x")

        assert "hunter2" not in redacted
        assert redacted.startswith("postgresql+asyncpg://***@")


@pytest.mark.asyncio
class TestSessionSurvivesAcrossInstances:
    async def test_a_session_created_by_one_instance_is_readable_by_another(
        self, monkeypatch, tmp_path
    ):
        """THE regression this task exists for: InMemorySessionService's
        state is process-local, so a session created by one instance is
        invisible to a second instance even pointed at 'the same' store
        -- there's no shared store at all. A real persistent backing
        store (a file-based SQLite DB standing in for Cloud SQL here)
        must let a SECOND, independently-constructed service instance
        see a session the FIRST instance created -- proving persistence
        isn't just "a different class," it's actually shared storage."""
        db_path = tmp_path / "sessions.db"
        monkeypatch.setenv(SESSION_DB_URL_ENV_VAR, f"sqlite+aiosqlite:///{db_path}")

        first_instance = build_session_service()
        session = await first_instance.create_session(app_name="vor", user_id="vor-system")

        second_instance = build_session_service()
        recovered = await second_instance.get_session(
            app_name="vor", user_id="vor-system", session_id=session.id
        )

        assert recovered is not None
        assert recovered.id == session.id
