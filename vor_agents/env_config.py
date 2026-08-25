"""
Vör -- integer settings read from the environment.

Companion to model_config.py and firestore_config.py, which do the same
for the model name and the Firestore database. Shared here because
integer settings need parsing and range-checking that a string lookup
doesn't, and getting that wrong the same way in two places is exactly
what a helper is for.

The governing rule: a malformed value must never take the service down.
These are read on request paths, and the value arrives from a deploy
flag typed by a human. `SWEEP_MAX_TARGETS=ten` should log loudly and fall
back to the documented default, not raise ValueError inside a Cloud Run
request. Silence would be worse than a crash for the opposite reason, so
every rejection is logged at WARNING with the offending value.
"""

import os

from loguru import logger


def env_int(name: str, default: int, minimum: int) -> int:
    """
    Reads an integer setting from the environment, per call.

    Falls back to `default` -- logging why -- when the variable is unset,
    blank, not an integer, or below `minimum`. `minimum` is not
    decoration: for a setting like the sweep's max_targets, 0 silently
    disables the entire safety-net audit path, which looks identical to
    "the sweep ran and found nothing" from the outside.

    Read at call time rather than bound at import, same as
    resolve_model() and firestore_database() -- see model_config.py for
    the full reasoning.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default

    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "{} is not an integer ({!r}); falling back to {}",
            name,
            raw,
            default,
        )
        return default

    if value < minimum:
        logger.warning(
            "{} is {}, below the minimum of {}; falling back to {}",
            name,
            value,
            minimum,
            default,
        )
        return default

    return value
