"""
Vör -- synthetic dataset generation for the 6 canonical cases.

Why this exists: every test fixture so far modeled one case ad hoc in
conftest.py, and only cases #1, #3 and #6 were ever named anywhere (see
enrichment.seed_template, conftest.drift_alert_cve_model,
test_identity.py). That left "the 6 synthetic cases" as a phrase in the
README with no definition behind it. This module is that definition, in
code, so the dataset and the docs cannot drift apart.

The six cases are chosen to span the actual decision surface, not to
enumerate alert types. Two patterns that SHOULD be trustworthy, arriving
by the two different provenances (#1 seeded, #2 live-graduated); two
kinds of deviation against a trusted pattern (#3 whole-identity drift,
#6 field-level); and the two distinct reasons graduation is withheld
(#4 evidence too uniform, #5 evidence too scarce).

    1. SEEDED_CONFIRMED     -- bulk-imported history, enters at confirmed
                               tier, provenance "seeded"
    2. LIVE_CONFIRMED       -- same pattern earned one alert at a time,
                               provenance "live"
    3. IDENTITY_DRIFT       -- CVE-2026-56164-modeled: different
                               child_image, so a different identity key
                               entirely; never matches the template
    4. LOW_DIVERSITY        -- meets GRADUATION_THRESHOLD by count, fails
                               MIN_DIVERSITY (same host/user/hour)
    5. INSUFFICIENT_HISTORY -- below GRADUATION_THRESHOLD; provisional
    6. FIELD_DEVIATION      -- same identity key as #1/#2, every diffable
                               field deviates

Determinism is the point: generation takes an explicit `seed` and the
same seed always produces byte-identical output. A synthetic dataset that
shifts between runs would make any downstream result -- a seeded
Firestore, a demo, a graduation threshold sanity check -- unreproducible.

Nothing here touches Firestore or the model. Persisting a generated case
is scripts/seed_firestore.py's job; see docs/DATASET_RUNBOOK.md.
"""

import random
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

from .identity import DIFFABLE_FIELDS

# Fixed epoch so a given seed produces the same timestamps forever --
# datetime.now() here would silently break reproducibility.
_EPOCH = datetime(2026, 8, 1, 9, 0, 0, tzinfo=UTC)

_HOSTS = ["SRV-SP-01", "SRV-SP-02", "SRV-SP-03", "SRV-SP-04", "SRV-SP-05"]
_USERS = ["CONTOSO\\jsmith", "CONTOSO\\mjones", "CONTOSO\\kwhite", "CONTOSO\\abrown"]

# The trusted pattern used throughout the design docs: a SharePoint
# ToolPane worker process compiling a template. Benign and recurring --
# exactly the shape Vör is meant to learn to stop paging humans about.
_BASELINE_IDENTITY = {
    "detection_rule_id": "SharePoint_ToolPane_Rule",
    "parent_image": "w3wp.exe",
    "child_image": "csc.exe",
    "endpoint_family": "ToolPane_admin",
}

# The invariant structural fields that make the baseline benign. A
# deviation in any of these is what ESCALATE exists for.
_BASELINE_STRUCTURE = {
    "auth_method_present": True,
    "session_cookie_present": True,
    "integrity_level": "Medium",
    "file_access_mode": "read",
    "egress_follows_access": False,
}

# Case #6 inverts every one of them at once -- the maximum-signal
# field-level deviation, and the case classifier_agent.py's docstring
# names as the bar a model must clear.
_DEVIATED_STRUCTURE = {
    "auth_method_present": False,
    "session_cookie_present": False,
    "integrity_level": "High",
    "file_access_mode": "write",
    "egress_follows_access": True,
}


class DatasetCase(str, Enum):
    """The 6 canonical cases. String-valued so a CLI can take one by name
    (`--case low_diversity`) without a lookup table."""

    SEEDED_CONFIRMED = "seeded_confirmed"
    LIVE_CONFIRMED = "live_confirmed"
    IDENTITY_DRIFT = "identity_drift"
    LOW_DIVERSITY = "low_diversity"
    INSUFFICIENT_HISTORY = "insufficient_history"
    FIELD_DEVIATION = "field_deviation"


class UnknownDatasetCaseError(ValueError):
    """
    Raised when a case name doesn't match any DatasetCase. Its message
    lists the valid names -- this is reached from a CLI argument, and a
    bare KeyError/ValueError would tell the operator nothing about what
    they should have typed.
    """


def _instance(
    index: int,
    rng: random.Random,
    structure: dict[str, Any],
    identity: dict[str, Any],
    vary_context: bool = True,
) -> dict[str, Any]:
    """
    One synthetic alert/instance.

    `vary_context` controls host/user/timestamp spread only -- never the
    structural fields. That separation is what makes case #4 possible:
    low-diversity evidence is *structurally identical* to high-diversity
    evidence, and differs only in whether the surrounding context repeats.
    Collapsing the two would make the two-part graduation gate
    untestable.
    """
    if vary_context:
        host = rng.choice(_HOSTS)
        user = rng.choice(_USERS)
        offset = timedelta(days=rng.randint(0, 20), hours=rng.randint(0, 23))
    else:
        host = _HOSTS[0]
        user = _USERS[0]
        # Minutes apart, same hour: enough to be distinct events, not
        # enough to count as diverse evidence.
        offset = timedelta(minutes=index * 5)

    return {
        **identity,
        **structure,
        "host": host,
        "user": user,
        "timestamp": (_EPOCH + offset).isoformat().replace("+00:00", "Z"),
        "instance_id": f"synthetic-{index:03d}",
    }


def generate_case(case: DatasetCase | str, seed: int = 0) -> dict[str, Any]:
    """
    Builds one dataset case.

    Returns:
        {
            "case": str,                     # the DatasetCase value
            "description": str,              # what it models, in prose
            "identity_key": tuple[str, ...], # this case's pattern
            "instances": list[dict],         # confirmed-negative history
            "probe_alert": dict,             # the alert to classify against it
            "expected_outcome": str,         # what SHOULD happen, and why
        }

    `instances` is the history to seed; `probe_alert` is what you then
    classify against it. For cases #3 and #6 the probe deliberately does
    NOT match the seeded history -- that mismatch is the case.

    `expected_outcome` is documentation, not an assertion: it records the
    designed intent of each case so a reader can tell whether a surprising
    result is a bug or a misunderstanding. Tests assert against real
    behavior, never against this string.
    """
    try:
        case = DatasetCase(case)
    except ValueError as exc:
        valid = ", ".join(member.value for member in DatasetCase)
        raise UnknownDatasetCaseError(
            f"Unknown dataset case '{case}'. Valid cases: {valid}"
        ) from exc

    # nosec B311 -- random is exactly right here: this generates
    # synthetic TEST data and reproducibility is a hard requirement (see
    # this module's docstring). A CSPRNG cannot be seeded to produce
    # identical output across runs, which is the whole point. Suppressed
    # at this line rather than skipped project-wide so a genuinely
    # security-relevant random call elsewhere still trips the check.
    rng = random.Random(seed)  # nosec B311

    if case is DatasetCase.SEEDED_CONFIRMED:
        instances = [_instance(i, rng, _BASELINE_STRUCTURE, _BASELINE_IDENTITY) for i in range(5)]
        return {
            "case": case.value,
            "description": (
                "Bulk-imported historical evidence for the baseline pattern. "
                "Enters directly at confirmed tier via seed_template() because "
                "the batch already meets GRADUATION_THRESHOLD; provenance "
                "'seeded', verified_by 'bulk' -- no human signed off per-alert."
            ),
            "identity_key": tuple(_BASELINE_IDENTITY.values()),
            "instances": instances,
            "probe_alert": _instance(99, rng, _BASELINE_STRUCTURE, _BASELINE_IDENTITY),
            "expected_outcome": (
                "SUPPRESS is permitted: confirmed tier, diverse evidence, probe "
                "matches the template on every diffable field."
            ),
        }

    if case is DatasetCase.LIVE_CONFIRMED:
        instances = [_instance(i, rng, _BASELINE_STRUCTURE, _BASELINE_IDENTITY) for i in range(5)]
        return {
            "case": case.value,
            "description": (
                "The same baseline pattern, but earned one alert at a time "
                "through record_confirmed_negative() rather than bulk import. "
                "Provenance 'live'. Exists to keep the two provenances "
                "distinguishable end-to-end -- they reach confirmed tier by "
                "different paths and should not be silently conflated."
            ),
            "identity_key": tuple(_BASELINE_IDENTITY.values()),
            "instances": instances,
            "probe_alert": _instance(99, rng, _BASELINE_STRUCTURE, _BASELINE_IDENTITY),
            "expected_outcome": "SUPPRESS is permitted, same as case #1.",
        }

    if case is DatasetCase.IDENTITY_DRIFT:
        drift_identity = {**_BASELINE_IDENTITY, "child_image": "cmd.exe"}
        return {
            "case": case.value,
            "description": (
                "CVE-2026-56164-modeled drift: w3wp.exe spawns cmd.exe rather "
                "than csc.exe. Because child_image is part of the identity key, "
                "this is a DIFFERENT PATTERN, not a deviating instance of the "
                "trusted one -- it never reaches field-level diffing at all."
            ),
            "identity_key": tuple(drift_identity.values()),
            "instances": [
                _instance(i, rng, _BASELINE_STRUCTURE, _BASELINE_IDENTITY) for i in range(5)
            ],
            "probe_alert": _instance(99, rng, _DEVIATED_STRUCTURE, drift_identity),
            "expected_outcome": (
                "Never SUPPRESS. The probe's identity key has no confirmed "
                "history, so enrich() returns NO_HISTORY -- the trusted "
                "pattern's evidence is not transferable to it."
            ),
        }

    if case is DatasetCase.LOW_DIVERSITY:
        instances = [
            _instance(i, rng, _BASELINE_STRUCTURE, _BASELINE_IDENTITY, vary_context=False)
            for i in range(3)
        ]
        return {
            "case": case.value,
            "description": (
                "Three confirmed instances -- meeting GRADUATION_THRESHOLD by "
                "raw count -- all from the same host, same user, same hour. "
                "This is the exact case the two-part graduation gate was built "
                "for: repetition on one machine is not independent evidence."
            ),
            "identity_key": tuple(_BASELINE_IDENTITY.values()),
            "instances": instances,
            "probe_alert": _instance(99, rng, _BASELINE_STRUCTURE, _BASELINE_IDENTITY),
            "expected_outcome": (
                "Never autonomously SUPPRESS. Count passes but "
                "evidence_diversity_score falls below MIN_DIVERSITY, so the "
                "pattern stays provisional."
            ),
        }

    if case is DatasetCase.INSUFFICIENT_HISTORY:
        instances = [_instance(i, rng, _BASELINE_STRUCTURE, _BASELINE_IDENTITY) for i in range(2)]
        return {
            "case": case.value,
            "description": (
                "Two confirmed instances -- diverse, but below "
                "GRADUATION_THRESHOLD. The other half of the graduation gate: "
                "case #4 has enough evidence of too-similar a kind, this one "
                "has too little evidence of a perfectly good kind."
            ),
            "identity_key": tuple(_BASELINE_IDENTITY.values()),
            "instances": instances,
            "probe_alert": _instance(99, rng, _BASELINE_STRUCTURE, _BASELINE_IDENTITY),
            "expected_outcome": (
                "Never autonomously SUPPRESS -- provisional tier until a third " "instance arrives."
            ),
        }

    # DatasetCase.FIELD_DEVIATION -- the enum is exhaustively handled, so
    # no fallback branch is reachable here.
    return {
        "case": case.value,
        "description": (
            "Same identity key as the trusted pattern, but every one of the 5 "
            "DIFFABLE_FIELDS deviates at once. Unlike case #3 this DOES reach "
            "field-level diffing -- it is a known pattern behaving wrongly, "
            "which is the harder and more dangerous case to get right."
        ),
        "identity_key": tuple(_BASELINE_IDENTITY.values()),
        "instances": [_instance(i, rng, _BASELINE_STRUCTURE, _BASELINE_IDENTITY) for i in range(5)],
        "probe_alert": _instance(99, rng, _DEVIATED_STRUCTURE, _BASELINE_IDENTITY),
        "expected_outcome": (
            f"ESCALATE. All {len(DIFFABLE_FIELDS)} diffable fields deviate from "
            "the template. If the model reports SUPPRESS anyway, "
            "classify_alert()'s deterministic reconciliation overrides it -- "
            "the model missing a real deviation must never produce an "
            "autonomous suppression."
        ),
    }


def generate_all(seed: int = 0) -> dict[str, dict[str, Any]]:
    """Every case, keyed by its case name. Same seed => same output."""
    return {case.value: generate_case(case, seed=seed) for case in DatasetCase}
