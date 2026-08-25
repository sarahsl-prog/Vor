"""
Vör -- Firestore database selection.

One place for the database name and the env var that overrides it, so
every entrypoint that constructs a client agrees on both. Same reasoning
as model_config.py: with the literal duplicated per call site, a new
script is one forgotten line away from silently reading and writing the
wrong database.

That failure is quiet and expensive: a one-shot migration
(scripts/backfill_identity_key.py) pointed at "(default)" while the real
data lives elsewhere reports "0 docs migrated" and looks like a clean run.
"""

import os

# Firestore's own name for the unnamed default database. Not a Vör
# convention -- passing this is equivalent to passing nothing.
DEFAULT_FIRESTORE_DATABASE = "(default)"

FIRESTORE_DATABASE_ENV_VAR = "FIRESTORE_DATABASE"


def firestore_database() -> str:
    """
    The database name every firestore.Client() in this repo should be
    constructed with: $FIRESTORE_DATABASE, else Firestore's default.

    Read at call time, not at import. A blank value is treated as unset --
    an env var a deploy script didn't populate usually arrives as "", and
    an empty database name fails deep inside the client rather than at the
    call site.
    """
    configured = os.environ.get(FIRESTORE_DATABASE_ENV_VAR, "")
    return configured.strip() or DEFAULT_FIRESTORE_DATABASE
