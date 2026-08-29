"""
Vör -- synthetic alert stream generation.

`datasets.py` answers "what are the 6 cases?" -- six fixed, deterministic
snapshots, each a history plus one probe alert. It deliberately does not
answer "what does a *stream* of alerts arriving at /classify look like?",
and that is the gap this module fills.

The difference matters for anything that runs against a live service. A
deployment fed only the 6 probes sees six requests against six known
patterns and exercises none of the conditions that make triage hard:
volume, unfamiliar patterns showing up mid-stream, the same pattern
recurring across different hosts over time, and a deviation buried in
traffic rather than handed over labelled.

So the stream is built as background traffic with the canonical cases
*injected into it* at an interval:

  - **Background noise** -- a pool of recurring, plausibly-benign Windows
    patterns (`BACKGROUND_PATTERNS`), each with its own invariant
    structure and its own host/user population. These are what a real
    estate mostly emits: the same handful of management-agent and
    servicing patterns, over and over, across many machines.
  - **Deviations** -- a configurable fraction of noise events break one
    or two diffable fields rather than all five. Case #6 inverts every
    field at once because it is the maximum-signal case; real deviations
    are usually a single field, which is the harder detection.
  - **Novel patterns** -- a fraction of events carry an identity key
    never seen before (new software, a new rule firing). These have no
    history at all, so `UNCERTAIN`/`no_history` is the correct outcome,
    and a stream without them never tests that.
  - **Canonical probes** -- every `case_interval` events, one of the 6
    `datasets.py` probe alerts is injected, cycling in order.

Every event carries its own label (`GeneratedEvent.kind`, `.case`,
`.expected_outcome`) *alongside* the alert rather than inside it. The
alert dict published to Pub/Sub stays a clean alert: nothing downstream
can accidentally read a synthetic answer key out of the payload, which
would quietly invalidate any result the stream produced.

Determinism is preserved on the generation side -- the same `seed`
produces the same sequence of patterns, hosts, users and deviations.
Timestamps are the deliberate exception: they come from an injected
`now_fn` so a live run stamps real wall-clock time (what a real ingest
does) while tests pin it. Nothing here touches Pub/Sub, Firestore or the
model; publishing is `scripts/generate_events.py`'s job.
"""

import random
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .datasets import DatasetCase, generate_case
from .identity import DIFFABLE_FIELDS, IDENTITY_KEY_FIELDS

# Defaults, all overridable from the CLI. Chosen to make a short run
# (a few hundred events) actually contain each interesting condition at
# least once, rather than to model any measured real-world base rate --
# no production Hayabusa/EVTX volume exists to calibrate against, the
# same open gap flagged on the graduation thresholds in identity.py.
DEFAULT_CASE_INTERVAL = 25
DEFAULT_DEVIATION_RATE = 0.05
DEFAULT_NOVEL_RATE = 0.03


class EventStreamConfigError(ValueError):
    """
    Raised when stream parameters are outside their valid ranges (rates
    not in [0, 1], a non-positive case interval).

    Validated up front rather than left to produce a subtly wrong stream:
    a run configured with `--deviation-rate 5` (meaning "5 percent") would
    otherwise silently emit *every* event as a deviation, and the operator
    would read the resulting flood of ESCALATEs as a finding about Vör
    rather than a typo in their own command.
    """


@dataclass(frozen=True)
class BackgroundPattern:
    """
    One recurring benign pattern in the simulated estate.

    `hosts` and `users` are per-pattern rather than global on purpose. A
    backup agent runs as SYSTEM across the server fleet; a software
    inventory agent runs as many different users across workstations.
    Drawing both from one shared pool would give every pattern the same
    synthetic diversity profile, which is precisely the signal the
    graduation gate reads -- see evidence_diversity.py.
    """

    detection_rule_id: str
    parent_image: str
    child_image: str
    endpoint_family: str
    structure: dict[str, Any]
    hosts: tuple[str, ...]
    users: tuple[str, ...]

    def identity(self) -> dict[str, str]:
        return {
            "detection_rule_id": self.detection_rule_id,
            "parent_image": self.parent_image,
            "child_image": self.child_image,
            "endpoint_family": self.endpoint_family,
        }


# The benign structure shared by most well-behaved patterns: an
# authenticated, cookie-bearing, medium-integrity read that is not
# followed by egress. Deviating from it is what ESCALATE exists for.
_BENIGN_STRUCTURE: dict[str, Any] = {
    "auth_method_present": True,
    "session_cookie_present": True,
    "integrity_level": "Medium",
    "file_access_mode": "read",
    "egress_follows_access": False,
}

_SERVERS = ("SRV-SP-01", "SRV-SP-02", "SRV-SP-03", "SRV-SP-04", "SRV-SP-05")
_WORKSTATIONS = tuple(f"WKS-{index:03d}" for index in range(1, 41))
_BACKUP_SERVERS = ("SRV-BKP-01", "SRV-BKP-02", "SRV-FILE-01", "SRV-FILE-02")
_HUMAN_USERS = (
    "CONTOSO\\jsmith",
    "CONTOSO\\mjones",
    "CONTOSO\\kwhite",
    "CONTOSO\\abrown",
    "CONTOSO\\rpatel",
    "CONTOSO\\tnguyen",
)
_MACHINE_USERS = ("NT AUTHORITY\\SYSTEM", "NT AUTHORITY\\NETWORK SERVICE")

# The estate's recurring traffic. The first entry is the SharePoint
# ToolPane pattern datasets.py builds all 6 cases around -- keeping it in
# the noise pool is what makes an injected canonical probe arrive as one
# more instance of an already-busy pattern rather than as an obvious
# outlier, which is the realism the injection is for.
BACKGROUND_PATTERNS: tuple[BackgroundPattern, ...] = (
    BackgroundPattern(
        detection_rule_id="SharePoint_ToolPane_Rule",
        parent_image="w3wp.exe",
        child_image="csc.exe",
        endpoint_family="ToolPane_admin",
        structure=_BENIGN_STRUCTURE,
        hosts=_SERVERS,
        users=_HUMAN_USERS,
    ),
    BackgroundPattern(
        detection_rule_id="SCCM_Software_Inventory",
        parent_image="ccmexec.exe",
        child_image="wmiprvse.exe",
        endpoint_family="workstation_managed",
        structure=_BENIGN_STRUCTURE,
        hosts=_WORKSTATIONS,
        users=_MACHINE_USERS,
    ),
    BackgroundPattern(
        detection_rule_id="Windows_Servicing_Stack",
        parent_image="svchost.exe",
        child_image="tiworker.exe",
        endpoint_family="workstation_managed",
        structure=_BENIGN_STRUCTURE,
        hosts=_WORKSTATIONS,
        users=_MACHINE_USERS,
    ),
    BackgroundPattern(
        detection_rule_id="Backup_Agent_Shadow_Copy",
        parent_image="services.exe",
        child_image="vssadmin.exe",
        endpoint_family="server_backup",
        # A backup agent legitimately runs High-integrity and writes.
        # Included precisely because "High + write" is not universally
        # suspicious -- a per-pattern template is the only thing that can
        # tell this apart from the same fields on a ToolPane alert.
        structure={
            **_BENIGN_STRUCTURE,
            "integrity_level": "High",
            "file_access_mode": "write",
        },
        hosts=_BACKUP_SERVERS,
        users=_MACHINE_USERS,
    ),
    BackgroundPattern(
        detection_rule_id="Monitoring_Agent_Collector",
        parent_image="winlogbeat.exe",
        child_image="powershell.exe",
        endpoint_family="server_monitoring",
        # Telemetry shipping means egress genuinely does follow access
        # here. Same point as the backup agent, on a different field.
        structure={**_BENIGN_STRUCTURE, "egress_follows_access": True},
        hosts=_SERVERS + _BACKUP_SERVERS,
        users=_MACHINE_USERS,
    ),
    BackgroundPattern(
        detection_rule_id="Helpdesk_Remote_Assist",
        parent_image="explorer.exe",
        child_image="msra.exe",
        endpoint_family="workstation_managed",
        structure=_BENIGN_STRUCTURE,
        hosts=_WORKSTATIONS,
        users=_HUMAN_USERS,
    ),
)

# Components for synthesising a never-before-seen identity key. Combined
# randomly, so a novel event is unlikely to collide with a background
# pattern or with a previous novel one -- the point is an identity key
# with no Firestore history behind it.
_NOVEL_RULES = (
    "Unsigned_Binary_Spawn",
    "LOLBin_Proxy_Execution",
    "Rare_Parent_Child_Pair",
    "Script_Interpreter_Chain",
    "Unexpected_Service_Install",
)
_NOVEL_PARENTS = ("outlook.exe", "excel.exe", "chrome.exe", "mshta.exe", "wscript.exe")
_NOVEL_CHILDREN = ("rundll32.exe", "regsvr32.exe", "certutil.exe", "bitsadmin.exe", "curl.exe")
_NOVEL_FAMILIES = ("workstation_unmanaged", "kiosk", "contractor_laptop", "lab_vm")

# How a single diffable field is broken when a noise event is chosen to
# deviate. Each maps a field to the value that is *not* its benign one --
# for the two boolean-ish fields this is computed from the pattern's own
# template rather than hardcoded, since (see Backup_Agent_Shadow_Copy and
# Monitoring_Agent_Collector above) the benign value differs per pattern.
_DEVIATED_VALUES: dict[str, Any] = {
    "integrity_level": {"Medium": "High", "High": "System", "Low": "Medium"},
    "file_access_mode": {"read": "write", "write": "read"},
}


@dataclass(frozen=True)
class GeneratedEvent:
    """
    One generated alert plus the labels describing what it is.

    The labels live here rather than inside `alert` so the payload
    published to Vör stays indistinguishable from a real alert. A
    `_synthetic_case` key riding along in the alert dict would be visible
    to enrichment, to the prompt, and to anything reading Firestore
    afterwards -- an answer key leaking into the thing being measured.

    `expected_outcome` is documentation, exactly as in datasets.py: it
    records designed intent so a surprising decision can be recognised as
    surprising. It is never asserted on, because a background event's
    real outcome legitimately depends on what history Firestore happens
    to hold at that moment.
    """

    alert: dict[str, Any]
    kind: str  # "background" | "deviation" | "novel" | "canonical"
    case: str | None = None  # DatasetCase value, canonical events only
    expected_outcome: str | None = None
    deviated_fields: tuple[str, ...] = field(default=())


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _timestamp(now_fn: Callable[[], datetime]) -> str:
    """
    ISO-8601 with a `Z` suffix, matching datasets.py's format exactly so
    a canonical probe and a generated event are indistinguishable in
    shape. `now_fn` is injected rather than called directly -- see the
    module docstring on why timestamps are the one non-deterministic part.
    """
    return now_fn().isoformat().replace("+00:00", "Z")


def _deviate(
    structure: dict[str, Any], rng: random.Random, count: int
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """
    Breaks `count` randomly chosen diffable fields against the pattern's
    own benign structure, returning the deviated structure and the names
    of the fields changed.

    One or two fields, not all five: case #6 already covers the
    everything-at-once extreme, and a single-field deviation inside a
    stream of near-identical benign events is the harder and more
    realistic detection. Booleans invert; the two string fields step to a
    different value via `_DEVIATED_VALUES`, falling back to inversion-free
    substitution if the template value is unrecognised.
    """
    fields = rng.sample(DIFFABLE_FIELDS, k=min(count, len(DIFFABLE_FIELDS)))
    deviated = dict(structure)
    for name in fields:
        current = structure[name]
        if isinstance(current, bool):
            deviated[name] = not current
        else:
            deviated[name] = _DEVIATED_VALUES.get(name, {}).get(current, "UNEXPECTED")
    return deviated, tuple(sorted(fields))


def _background_event(
    pattern: BackgroundPattern,
    index: int,
    rng: random.Random,
    now_fn: Callable[[], datetime],
    deviated_field_count: int = 0,
) -> GeneratedEvent:
    structure = pattern.structure
    deviated_fields: tuple[str, ...] = ()
    if deviated_field_count:
        structure, deviated_fields = _deviate(pattern.structure, rng, deviated_field_count)

    alert = {
        **pattern.identity(),
        **structure,
        "host": rng.choice(pattern.hosts),
        "user": rng.choice(pattern.users),
        "timestamp": _timestamp(now_fn),
        "instance_id": f"gen-{index:06d}",
    }
    if deviated_fields:
        return GeneratedEvent(
            alert=alert,
            kind="deviation",
            expected_outcome=(
                "ESCALATE if this pattern has a confirmed template covering "
                f"{list(deviated_fields)}; UNCERTAIN if it has no history yet."
            ),
            deviated_fields=deviated_fields,
        )
    return GeneratedEvent(
        alert=alert,
        kind="background",
        expected_outcome=(
            "SUPPRESS once this pattern has graduated; UNCERTAIN until then. "
            "Matches its own template on every diffable field."
        ),
    )


def _novel_event(index: int, rng: random.Random, now_fn: Callable[[], datetime]) -> GeneratedEvent:
    identity = {
        "detection_rule_id": rng.choice(_NOVEL_RULES),
        "parent_image": rng.choice(_NOVEL_PARENTS),
        "child_image": rng.choice(_NOVEL_CHILDREN),
        "endpoint_family": rng.choice(_NOVEL_FAMILIES),
    }
    # A novel pattern is not automatically a deviating one -- an unknown
    # pattern behaving perfectly normally is the common case, and the
    # right answer for it is still "no history, don't suppress".
    structure, _ = _deviate(_BENIGN_STRUCTURE, rng, rng.randint(0, 2))
    return GeneratedEvent(
        alert={
            **identity,
            **structure,
            "host": rng.choice(_WORKSTATIONS),
            "user": rng.choice(_HUMAN_USERS),
            "timestamp": _timestamp(now_fn),
            "instance_id": f"gen-{index:06d}",
        },
        kind="novel",
        expected_outcome=(
            "Never SUPPRESS. This identity key has no confirmed history, so "
            "enrichment reports no_history and UNCERTAIN is correct."
        ),
    )


def _canonical_event(
    case: DatasetCase, index: int, seed: int, now_fn: Callable[[], datetime]
) -> GeneratedEvent:
    """
    One of the 6 canonical probe alerts, re-stamped for this stream.

    Only `timestamp` and `instance_id` are replaced -- every identity and
    diffable field is passed through from datasets.py untouched, so the
    case still means exactly what the dataset says it means. Re-stamping
    the timestamp is what keeps an injected probe from arriving with an
    August 2026 fixed-epoch date in the middle of live traffic; keeping
    the case name in `instance_id` makes it findable in Firestore
    afterwards without labelling the alert with its expected answer.
    """
    case_data = generate_case(case, seed=seed)
    alert = dict(case_data["probe_alert"])
    alert["timestamp"] = _timestamp(now_fn)
    alert["instance_id"] = f"gen-{index:06d}-{case.value}"
    return GeneratedEvent(
        alert=alert,
        kind="canonical",
        case=case.value,
        expected_outcome=str(case_data["expected_outcome"]),
    )


def _validate_config(case_interval: int, deviation_rate: float, novel_rate: float) -> None:
    if case_interval < 1:
        raise EventStreamConfigError(
            f"case_interval must be >= 1, got {case_interval}. Use inject_cases=False "
            "to disable canonical injection instead."
        )
    for name, rate in (("deviation_rate", deviation_rate), ("novel_rate", novel_rate)):
        if not 0.0 <= rate <= 1.0:
            raise EventStreamConfigError(
                f"{name} must be a fraction between 0.0 and 1.0, got {rate}"
            )
    if deviation_rate + novel_rate > 1.0:
        raise EventStreamConfigError(
            f"deviation_rate + novel_rate must not exceed 1.0, got "
            f"{deviation_rate} + {novel_rate} = {deviation_rate + novel_rate}"
        )


def generate_events(
    count: int,
    seed: int = 0,
    case_interval: int = DEFAULT_CASE_INTERVAL,
    deviation_rate: float = DEFAULT_DEVIATION_RATE,
    novel_rate: float = DEFAULT_NOVEL_RATE,
    inject_cases: bool = True,
    now_fn: Callable[[], datetime] | None = None,
) -> Iterator[GeneratedEvent]:
    """
    Yields `count` events as a stream of background traffic with the 6
    canonical cases injected every `case_interval` events.

    Args:
        count: how many events to yield. 0 yields nothing rather than
            raising -- a caller pacing by duration may legitimately ask
            for an empty batch.
        seed: RNG seed. The same seed yields the same patterns, hosts,
            users and deviations in the same order.
        case_interval: inject a canonical probe every N events, cycling
            through the 6 cases in DatasetCase order.
        deviation_rate: fraction of non-canonical events that break one
            or two diffable fields.
        novel_rate: fraction of non-canonical events carrying an
            unseen identity key.
        inject_cases: False turns the stream into pure background noise.
        now_fn: timestamp source, defaults to `datetime.now(UTC)`.

    Raises:
        EventStreamConfigError: on out-of-range rates or interval.

    A generator rather than a list: a soak run can stream for hours, and
    materialising that many alerts to send them one at a time would be
    pure waste. Note that this function is NOT itself a generator -- it
    validates and then returns one. A bare `yield` in this body would
    defer validation until the first iteration, so a caller that builds
    the stream inside a try/except and iterates it elsewhere (exactly
    what scripts/generate_events.py does) would see the config error
    escape its own error handling and surface as a traceback.
    """
    _validate_config(case_interval, deviation_rate, novel_rate)
    resolved_now = now_fn if now_fn is not None else _utcnow

    # nosec B311 -- same rationale as datasets.py: this is synthetic test
    # data whose reproducibility is a hard requirement, and a CSPRNG
    # cannot be seeded to replay an identical stream. Nothing generated
    # here is security-bearing.
    rng = random.Random(seed)  # nosec B311
    cases = list(DatasetCase)

    return _stream(
        count,
        seed,
        case_interval,
        inject_cases,
        rng,
        cases,
        resolved_now,
        deviation_rate,
        novel_rate,
    )


def _stream(
    count: int,
    seed: int,
    case_interval: int,
    inject_cases: bool,
    rng: random.Random,
    cases: list[DatasetCase],
    now_fn: Callable[[], datetime],
    deviation_rate: float,
    novel_rate: float,
) -> Iterator[GeneratedEvent]:
    """The actual generator behind generate_events(); split out purely so
    validation runs eagerly. See generate_events()'s docstring."""
    for index in range(count):
        # 1-based so the first injection lands at index case_interval-1
        # rather than at event 0 -- a stream should establish some
        # background before the first labelled probe arrives.
        position = index + 1
        if inject_cases and position % case_interval == 0:
            case = cases[(position // case_interval - 1) % len(cases)]
            yield _canonical_event(case, index, seed, now_fn)
            continue

        roll = rng.random()
        if roll < novel_rate:
            yield _novel_event(index, rng, now_fn)
        elif roll < novel_rate + deviation_rate:
            yield _background_event(
                rng.choice(BACKGROUND_PATTERNS),
                index,
                rng,
                now_fn,
                deviated_field_count=rng.randint(1, 2),
            )
        else:
            yield _background_event(rng.choice(BACKGROUND_PATTERNS), index, rng, now_fn)


def validate_alert(alert: dict[str, Any]) -> list[str]:
    """
    Returns the names of required fields missing from `alert`, empty if
    it is well-formed.

    Used by the replay path in scripts/generate_events.py to reject a
    malformed external export before anything is published. Deliberately
    returns the full list rather than raising on the first miss: an
    operator mapping a Hayabusa export onto Vör's schema needs to see
    every field they still have to map, not to rediscover them one run at
    a time. Returns names rather than raising because the caller decides
    whether a bad record aborts the run or is skipped.
    """
    required = list(IDENTITY_KEY_FIELDS) + DIFFABLE_FIELDS
    return [name for name in required if name not in alert]
