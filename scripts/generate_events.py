"""
Publish a stream of alerts to Vör's Pub/Sub ingest topic.

`seed_firestore.py` loads *history* -- the confirmed-negative evidence a
pattern is judged against. This is the other half: the live traffic that
gets judged. It publishes to the `vor-alerts` topic, whose push
subscription calls `POST /classify` (docs/DEPLOY.md step 4), so events
travel the real production ingest path rather than a test shortcut --
including the base64 Pub/Sub envelope `main.py::_decode_classify_body()`
unwraps.

Two sources, mirroring seed_firestore.py's split:

  (default)      synthetic traffic from vor_agents/event_stream.py --
                 recurring benign patterns, occasional single-field
                 deviations and never-seen identity keys, with the 6
                 canonical dataset cases injected at an interval.

  --file <path>  a JSON list of real alerts, published in order. This is
                 the path for a Hayabusa/EVTX export once it has been
                 mapped onto Vör's alert schema. Note that the mapping is
                 yours to do: Hayabusa emits nothing equivalent to
                 auth_method_present, session_cookie_present,
                 file_access_mode or egress_follows_access, and this
                 script validates that all five DIFFABLE_FIELDS are
                 present rather than guessing them (README, "No
                 Hayabusa/EVTX exporter").

Usage:
    # See exactly what would be published, without publishing anything
    .venv/bin/python scripts/generate_events.py --count 20 --dry-run

    # 500 events at 5/sec to the default topic
    .venv/bin/python scripts/generate_events.py --count 500 --rate 5

    # Soak: run for 10 minutes at 2/sec
    .venv/bin/python scripts/generate_events.py --duration 600 --rate 2

    # Replay a mapped export
    .venv/bin/python scripts/generate_events.py --file alerts.json

Publishing costs money and drives real Gemini calls downstream -- one
model call per event, plus an audit enqueue per SUPPRESS. Run --dry-run
first; the summary it prints is identical to a real run's.
"""

import sys
from pathlib import Path

# Running this file directly puts scripts/ on sys.path, not the repo
# root, so `import vor_agents` would fail for the documented invocation.
# Same shim, same reason, as scripts/seed_firestore.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import json
import os
import signal
import time
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from types import FrameType
from typing import Any, Protocol

from loguru import logger

from vor_agents.event_stream import (
    DEFAULT_CASE_INTERVAL,
    DEFAULT_DEVIATION_RATE,
    DEFAULT_NOVEL_RATE,
    EventStreamConfigError,
    GeneratedEvent,
    generate_events,
    validate_alert,
)

DEFAULT_TOPIC = "vor-alerts"

# Publishing is fire-and-forget by default; this bounds how long the run
# waits at the end for outstanding publish futures to resolve so a
# transient Pub/Sub problem surfaces as a reported failure rather than a
# process that never exits.
PUBLISH_TIMEOUT_SECONDS = 30.0


class EventSourceError(Exception):
    """
    Raised on malformed --file input: unreadable, not JSON, not a list,
    or records missing required fields.

    Validated in full before the first publish, exactly as
    seed_firestore.py validates before the first write. A partially
    published run is materially worse than a refused one: the events that
    did land have already been classified, may have graduated a pattern,
    and cannot be recalled.
    """


class PublishError(Exception):
    """
    Raised when the Pub/Sub client cannot be constructed or the topic
    cannot be resolved -- a broken deployment or a wrong --project, not a
    per-event condition. Per-event publish failures are counted and
    reported instead (see `_Stats.failed`), because one rejected message
    in a thousand-event soak should not abort the run.
    """


class Publisher(Protocol):
    """
    The narrow slice of google.cloud.pubsub_v1.PublisherClient this
    script uses. Declared as a Protocol so tests can substitute a fake
    without a Pub/Sub client, credentials or network -- the same pattern
    the rest of this codebase's tests use for Firestore.
    """

    def publish(self, topic: str, data: bytes) -> Any: ...


@dataclass
class _Stats:
    """Counts for the end-of-run summary."""

    published: int = 0
    failed: int = 0
    by_kind: Counter[str] = field(default_factory=Counter)
    by_case: Counter[str] = field(default_factory=Counter)


def load_events_from_file(path: Path) -> list[GeneratedEvent]:
    """
    Reads and validates a JSON list of real alerts for --file replay.

    Every record is checked before any is returned, and the error names
    the exact record index and the exact fields missing -- an operator
    mapping an export onto Vör's schema needs to fix the mapping, and
    "record 47 is missing egress_follows_access" is what makes that
    possible.
    """
    try:
        raw = json.loads(path.read_text())
    except OSError as exc:
        raise EventSourceError(f"Cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise EventSourceError(f"{path} is not valid JSON: {exc}") from exc

    if not isinstance(raw, list):
        raise EventSourceError(
            f"{path} must contain a JSON list of alerts, got {type(raw).__name__}"
        )
    if not raw:
        raise EventSourceError(f"{path} contains no alerts")

    events = []
    for index, alert in enumerate(raw):
        if not isinstance(alert, dict):
            raise EventSourceError(f"{path}[{index}] is not a JSON object")
        missing = validate_alert(alert)
        if missing:
            raise EventSourceError(
                f"{path}[{index}] is missing required field(s): {missing}. "
                "Vör needs the 4 identity fields and all 5 DIFFABLE_FIELDS; "
                "see the module docstring on mapping a Hayabusa export."
            )
        events.append(GeneratedEvent(alert=alert, kind="replay"))
    return events


def build_publisher(project: str, topic: str) -> tuple[Publisher, str]:
    """
    Constructs a real Pub/Sub publisher and resolves the topic path.

    Imported lazily rather than at module scope so --dry-run works on a
    machine with no credentials configured at all: inspecting what would
    be sent must never require the ability to send it.
    """
    try:
        # The ignore below is needed because google-cloud-pubsub ships
        # its py.typed marker under google/pubsub_v1/ but not under the
        # google/cloud/pubsub_v1/ shim imported here, so mypy cannot see
        # the attribute on the google.cloud namespace package. Narrowed to
        # this line rather than relaxed in pyproject.toml, so the fully
        # typed google.cloud packages this repo relies on (firestore,
        # tasks_v2) stay strictly checked.
        from google.cloud import pubsub_v1  # type: ignore[attr-defined]
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise PublishError(f"google-cloud-pubsub is not installed: {exc}. Run `uv sync`.") from exc

    try:
        client = pubsub_v1.PublisherClient()
        topic_path = str(client.topic_path(project, topic))
    except Exception as exc:
        # Broad by necessity: the client raises credential, transport and
        # argument errors from several unrelated libraries here. Wrapped
        # rather than surfaced raw, per this project's error-handling
        # standard.
        raise PublishError(
            f"Cannot create a Pub/Sub publisher for {project}/{topic}: {exc!r}"
        ) from exc

    return client, topic_path


def _iter_source(args: argparse.Namespace) -> Iterator[GeneratedEvent]:
    """
    Resolves --file / --count / --duration into one event iterator.

    For --duration the count is left effectively unbounded and the pacing
    loop stops on the clock instead; sys.maxsize rather than an infinite
    generator keeps generate_events()'s deterministic indexing intact.
    """
    if args.file:
        return iter(load_events_from_file(args.file))
    count = sys.maxsize if args.duration else args.count
    return generate_events(
        count=count,
        seed=args.seed,
        case_interval=args.case_interval,
        deviation_rate=args.deviation_rate,
        novel_rate=args.novel_rate,
        inject_cases=not args.no_cases,
    )


def run(
    events: Iterator[GeneratedEvent],
    publisher: Publisher | None,
    topic_path: str,
    rate: float,
    duration: float | None = None,
    stop_requested: Any = None,
) -> _Stats:
    """
    Paces `events` at `rate` events/sec and publishes each one.

    `publisher` of None is dry-run: every event is printed as one JSON
    object per line (so the output pipes into `jq` or straight back into
    --file) and nothing is sent.

    Pacing is computed against a fixed start time rather than by sleeping
    a constant interval per event, so publish latency does not
    accumulate into a drifting, steadily-slower stream over a long soak.

    `stop_requested` is any zero-argument callable returning True when
    the run should end early -- SIGINT handling in main() passes one, so
    Ctrl-C stops between events and still prints a summary rather than
    losing the accounting to a traceback.
    """
    stats = _Stats()
    interval = 1.0 / rate if rate > 0 else 0.0
    start = time.monotonic()
    futures = []

    for index, event in enumerate(events):
        if stop_requested is not None and stop_requested():
            logger.info("Stop requested; ending after {} event(s)", index)
            break

        elapsed = time.monotonic() - start
        if duration is not None and elapsed >= duration:
            break

        target = index * interval
        if target > elapsed:
            time.sleep(target - elapsed)

        stats.by_kind[event.kind] += 1
        if event.case:
            stats.by_case[event.case] += 1

        if publisher is None:
            print(json.dumps(event.alert))
            stats.published += 1
            continue

        try:
            futures.append(publisher.publish(topic_path, json.dumps(event.alert).encode("utf-8")))
            stats.published += 1
        except Exception as exc:  # noqa: BLE001 — deliberate catch-all:
            # the Pub/Sub client raises transport, auth and serialization
            # errors from several unrelated libraries, and no per-event
            # failure is worth abandoning a long soak run over. Counted
            # and logged, then the stream continues.
            stats.failed += 1
            logger.bind(instance_id=event.alert.get("instance_id")).error(
                "Publish failed: {}", repr(exc)
            )

    # Pub/Sub's publish() is asynchronous. Without resolving the futures
    # the process can exit with messages still buffered, reporting a
    # success it never actually confirmed.
    for future in futures:
        try:
            future.result(timeout=PUBLISH_TIMEOUT_SECONDS)
        except Exception as exc:  # noqa: BLE001 — deliberate, same
            # rationale as the publish() catch above: a future can fail
            # with anything the transport raises, and one unconfirmed
            # message must not lose the accounting for the rest.
            stats.published -= 1
            stats.failed += 1
            logger.error("Publish did not confirm: {}", repr(exc))

    return stats


def _print_summary(stats: _Stats, dry_run: bool, topic_path: str) -> None:
    prefix = "[dry-run] would publish" if dry_run else "Published"
    target = "(nothing sent)" if dry_run else topic_path
    print(f"\n{prefix} {stats.published} event(s) to {target}", file=sys.stderr)
    if stats.failed:
        print(f"  {stats.failed} failed to publish", file=sys.stderr)
    for kind, count in sorted(stats.by_kind.items()):
        print(f"  {kind:<12} {count}", file=sys.stderr)
    if stats.by_case:
        print("  canonical cases injected:", file=sys.stderr)
        for case, count in sorted(stats.by_case.items()):
            print(f"    {case:<22} {count}", file=sys.stderr)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    volume = parser.add_mutually_exclusive_group()
    volume.add_argument(
        "--count", type=int, default=100, help="How many events to send (default 100)."
    )
    volume.add_argument(
        "--duration",
        type=float,
        help="Send continuously for this many seconds instead of a fixed count.",
    )
    parser.add_argument(
        "--rate",
        type=float,
        default=1.0,
        help="Events per second (default 1.0; 0 = as fast as possible).",
    )
    parser.add_argument(
        "--file",
        type=Path,
        help="Replay a JSON list of real alerts instead of generating synthetic ones.",
    )
    parser.add_argument(
        "--project",
        default=os.environ.get("GCP_PROJECT"),
        help="GCP project holding the topic (default: $GCP_PROJECT).",
    )
    parser.add_argument(
        "--topic", default=DEFAULT_TOPIC, help=f"Pub/Sub topic (default {DEFAULT_TOPIC})."
    )
    parser.add_argument("--seed", type=int, default=0, help="RNG seed (default 0).")
    parser.add_argument(
        "--case-interval",
        type=int,
        default=DEFAULT_CASE_INTERVAL,
        help=(
            "Inject one of the 6 canonical dataset cases every N events "
            f"(default {DEFAULT_CASE_INTERVAL})."
        ),
    )
    parser.add_argument(
        "--no-cases", action="store_true", help="Pure background noise; inject no canonical cases."
    )
    parser.add_argument(
        "--deviation-rate",
        type=float,
        default=DEFAULT_DEVIATION_RATE,
        help=(
            "Fraction of events breaking 1-2 diffable fields "
            f"(default {DEFAULT_DEVIATION_RATE})."
        ),
    )
    parser.add_argument(
        "--novel-rate",
        type=float,
        default=DEFAULT_NOVEL_RATE,
        help=f"Fraction of events with an unseen identity key (default {DEFAULT_NOVEL_RATE}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the alerts as JSON lines; publish nothing.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.rate < 0:
        print("error: --rate must not be negative", file=sys.stderr)
        return 2
    if not args.dry_run and not args.project:
        print(
            "error: --project is required (or set GCP_PROJECT) unless --dry-run",
            file=sys.stderr,
        )
        return 2

    try:
        events = _iter_source(args)
    except (EventSourceError, EventStreamConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    publisher: Publisher | None = None
    topic_path = "(dry-run)"
    if not args.dry_run:
        try:
            publisher, topic_path = build_publisher(args.project, args.topic)
        except PublishError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    # A soak run is normally ended with Ctrl-C. Flipping a flag the
    # pacing loop checks -- rather than letting KeyboardInterrupt unwind
    # it -- means outstanding publishes still get resolved and the
    # summary still gets printed.
    interrupted = False

    def _handle_sigint(signum: int, frame: FrameType | None) -> None:
        nonlocal interrupted
        interrupted = True

    signal.signal(signal.SIGINT, _handle_sigint)

    stats = run(
        events,
        publisher,
        topic_path,
        rate=args.rate,
        duration=args.duration,
        stop_requested=lambda: interrupted,
    )
    _print_summary(stats, args.dry_run, topic_path)
    return 1 if stats.failed else 0


if __name__ == "__main__":
    sys.exit(main())
