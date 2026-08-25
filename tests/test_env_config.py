"""
Tests for vor_agents.env_config -- integer settings read from the
environment.

Weighted heavily toward malformed input, because that is the whole
justification for the helper existing: these values come from a deploy
flag typed by a human and are read on request paths. `SWEEP_MAX_TARGETS=ten`
must degrade to the documented default with a loud log, never raise
ValueError inside a Cloud Run request.
"""

import pytest

from vor_agents.blast_radius import (
    DEFAULT_TABLE_CACHE_TTL_SECONDS,
    TABLE_CACHE_TTL_ENV_VAR,
    _table_cache_ttl_seconds,
)
from vor_agents.env_config import env_int
from vor_agents.orchestrator import (
    DEFAULT_SWEEP_MAX_TARGETS,
    SWEEP_MAX_TARGETS_ENV_VAR,
)

VAR = "VOR_TEST_INT"


class TestEnvInt:
    def test_unset_returns_the_default(self, monkeypatch):
        monkeypatch.delenv(VAR, raising=False)

        assert env_int(VAR, 10, minimum=1) == 10

    def test_valid_value_is_used(self, monkeypatch):
        monkeypatch.setenv(VAR, "25")

        assert env_int(VAR, 10, minimum=1) == 25

    def test_read_at_call_time(self, monkeypatch):
        """Same per-call contract as resolve_model()/firestore_database():
        a change between two calls in one process must take effect."""
        monkeypatch.setenv(VAR, "5")
        first = env_int(VAR, 10, minimum=1)
        monkeypatch.setenv(VAR, "7")

        assert (first, env_int(VAR, 10, minimum=1)) == (5, 7)

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_blank_returns_the_default(self, monkeypatch, blank):
        monkeypatch.setenv(VAR, blank)

        assert env_int(VAR, 10, minimum=1) == 10

    def test_surrounding_whitespace_is_tolerated(self, monkeypatch):
        monkeypatch.setenv(VAR, "  25  ")

        assert env_int(VAR, 10, minimum=1) == 25

    @pytest.mark.parametrize("bad", ["ten", "1.5", "10x", "--5", "1,000"])
    def test_non_integer_falls_back_without_raising(self, monkeypatch, bad):
        """The failure that matters: a typo in a deploy flag must not take
        the service down on a request path."""
        monkeypatch.setenv(VAR, bad)

        assert env_int(VAR, 10, minimum=1) == 10

    @pytest.mark.parametrize("below", ["0", "-1", "-100"])
    def test_below_minimum_falls_back(self, monkeypatch, below):
        monkeypatch.setenv(VAR, below)

        assert env_int(VAR, 10, minimum=1) == 10

    def test_value_exactly_at_the_minimum_is_accepted(self, monkeypatch):
        monkeypatch.setenv(VAR, "1")

        assert env_int(VAR, 10, minimum=1) == 1

    def test_zero_is_allowed_when_the_minimum_permits_it(self, monkeypatch):
        """minimum is per-setting: 0 is meaningless for max_targets but
        legitimate for a cache TTL ("never serve from cache")."""
        monkeypatch.setenv(VAR, "0")

        assert env_int(VAR, 300, minimum=0) == 0


class TestSweepMaxTargets:
    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv(SWEEP_MAX_TARGETS_ENV_VAR, raising=False)

        assert env_int(SWEEP_MAX_TARGETS_ENV_VAR, DEFAULT_SWEEP_MAX_TARGETS, minimum=1) == 10

    def test_zero_is_rejected(self, monkeypatch):
        """0 would silently disable the entire safety-net sweep while
        looking identical to "ran, nothing to audit" from the outside."""
        monkeypatch.setenv(SWEEP_MAX_TARGETS_ENV_VAR, "0")

        assert (
            env_int(SWEEP_MAX_TARGETS_ENV_VAR, DEFAULT_SWEEP_MAX_TARGETS, minimum=1)
            == DEFAULT_SWEEP_MAX_TARGETS
        )


class TestTableCacheTtl:
    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv(TABLE_CACHE_TTL_ENV_VAR, raising=False)

        assert _table_cache_ttl_seconds() == DEFAULT_TABLE_CACHE_TTL_SECONDS

    def test_env_var_is_honored(self, monkeypatch):
        monkeypatch.setenv(TABLE_CACHE_TTL_ENV_VAR, "60")

        assert _table_cache_ttl_seconds() == 60

    def test_zero_is_allowed(self, monkeypatch):
        """0 means "never serve from cache" -- valid for debugging a stale
        table, or a deployment happy to pay the Firestore reads."""
        monkeypatch.setenv(TABLE_CACHE_TTL_ENV_VAR, "0")

        assert _table_cache_ttl_seconds() == 0

    def test_negative_falls_back(self, monkeypatch):
        monkeypatch.setenv(TABLE_CACHE_TTL_ENV_VAR, "-1")

        assert _table_cache_ttl_seconds() == DEFAULT_TABLE_CACHE_TTL_SECONDS

    def test_garbage_falls_back(self, monkeypatch):
        monkeypatch.setenv(TABLE_CACHE_TTL_ENV_VAR, "five minutes")

        assert _table_cache_ttl_seconds() == DEFAULT_TABLE_CACHE_TTL_SECONDS
