"""
Tests for vor_agents.firestore_config -- which database every
firestore.Client() in this repo is constructed against.

Small surface, but the failure it guards is quiet: a client pointed at
"(default)" while the real data lives in a named database reads and writes
the wrong place without erroring. For scripts/backfill_identity_key.py
that means a one-shot migration reporting a clean run having migrated
nothing.
"""

import pytest

from vor_agents.firestore_config import (
    DEFAULT_FIRESTORE_DATABASE,
    FIRESTORE_DATABASE_ENV_VAR,
    firestore_database,
)


def test_default_when_env_var_unset(monkeypatch):
    monkeypatch.delenv(FIRESTORE_DATABASE_ENV_VAR, raising=False)

    assert firestore_database() == DEFAULT_FIRESTORE_DATABASE


def test_env_var_is_read_at_call_time(monkeypatch):
    monkeypatch.setenv(FIRESTORE_DATABASE_ENV_VAR, "vor-prod")

    assert firestore_database() == "vor-prod"


def test_change_takes_effect_between_calls(monkeypatch):
    monkeypatch.setenv(FIRESTORE_DATABASE_ENV_VAR, "db-a")
    first = firestore_database()
    monkeypatch.setenv(FIRESTORE_DATABASE_ENV_VAR, "db-b")

    assert (first, firestore_database()) == ("db-a", "db-b")


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_env_var_falls_back_to_the_default(monkeypatch, blank):
    """An env var a deploy script didn't populate usually arrives as "".
    An empty database name fails deep inside the client rather than here."""
    monkeypatch.setenv(FIRESTORE_DATABASE_ENV_VAR, blank)

    assert firestore_database() == DEFAULT_FIRESTORE_DATABASE
