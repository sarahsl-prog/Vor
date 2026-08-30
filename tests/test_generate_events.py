"""
Vör — tests for scripts/generate_events.py.

No Pub/Sub client, no credentials, no network: the publisher is a fake
satisfying the script's `Publisher` protocol, which is exactly why that
protocol is declared as narrowly as it is.

What matters here is the operator-facing contract — a bad export is
refused before anything is published, per-event failures are counted
rather than fatal, publishes are actually confirmed, and --dry-run emits
re-ingestable JSON — rather than the generation logic, which
test_event_stream.py covers.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.generate_events import (
    EventSourceError,
    _Stats,
    load_events_from_file,
    main,
    run,
)
from vor_agents.event_stream import GeneratedEvent, generate_events


class FakeFuture:
    def __init__(self, exc=None):
        self._exc = exc

    def result(self, timeout=None):
        if self._exc:
            raise self._exc
        return "message-id"


class FakePublisher:
    """Records what was published. `fail_on` makes publish() raise for
    the given call indices; `unconfirmed_on` makes the call succeed but
    its future fail — the two distinct failure modes the async client
    actually has."""

    def __init__(self, fail_on=(), unconfirmed_on=()):
        self.published = []
        self._fail_on = set(fail_on)
        self._unconfirmed_on = set(unconfirmed_on)
        self._calls = 0

    def publish(self, topic, data):
        index = self._calls
        self._calls += 1
        if index in self._fail_on:
            raise RuntimeError("publish rejected")
        self.published.append((topic, data))
        if index in self._unconfirmed_on:
            return FakeFuture(RuntimeError("not confirmed"))
        return FakeFuture()


def _events(count=5):
    return generate_events(count, seed=0)


class TestRun:
    def test_publishes_every_event_as_json_bytes(self):
        publisher = FakePublisher()
        stats = run(_events(5), publisher, "projects/p/topics/t", rate=0)

        assert stats.published == 5
        assert stats.failed == 0
        assert len(publisher.published) == 5
        for topic, data in publisher.published:
            assert topic == "projects/p/topics/t"
            # Decoded rather than eyeballed: the push subscription
            # base64-decodes and json.loads() this exact payload.
            assert "detection_rule_id" in json.loads(data.decode("utf-8"))

    def test_counts_kinds_and_cases(self):
        stats = run(generate_events(60, seed=0, case_interval=10), FakePublisher(), "t", rate=0)
        assert sum(stats.by_kind.values()) == 60
        assert stats.by_kind["canonical"] == 6
        assert len(stats.by_case) == 6

    def test_a_rejected_publish_is_counted_not_fatal(self):
        publisher = FakePublisher(fail_on={2})
        stats = run(_events(5), publisher, "t", rate=0)

        # The run continues: a single rejected message must not abandon
        # a long soak.
        assert stats.published == 4
        assert stats.failed == 1
        assert len(publisher.published) == 4

    def test_an_unconfirmed_publish_is_not_reported_as_published(self):
        # publish() returning without raising is not delivery — the
        # future is where a Pub/Sub-side failure actually surfaces.
        stats = run(_events(5), FakePublisher(unconfirmed_on={1}), "t", rate=0)
        assert stats.published == 4
        assert stats.failed == 1

    def test_dry_run_publishes_nothing_and_prints_json_lines(self, capsys):
        stats = run(_events(4), None, "(dry-run)", rate=0)
        lines = capsys.readouterr().out.strip().splitlines()

        assert stats.published == 4
        assert len(lines) == 4
        for line in lines:
            assert json.loads(line)["detection_rule_id"]

    def test_dry_run_output_round_trips_back_into_the_file_path(self, capsys, tmp_path):
        run(_events(6), None, "(dry-run)", rate=0)
        alerts = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
        path = tmp_path / "replay.json"
        path.write_text(json.dumps(alerts))

        # The documented workflow: inspect a dry run, then replay it.
        assert len(load_events_from_file(path)) == 6

    def test_stop_requested_ends_the_run_early(self):
        publisher = FakePublisher()
        calls = {"n": 0}

        def stop():
            calls["n"] += 1
            return calls["n"] > 3

        stats = run(generate_events(100, seed=0), publisher, "t", rate=0, stop_requested=stop)
        assert stats.published == 3

    def test_duration_bounds_the_run(self):
        # rate=0 means the loop is limited only by the clock; with a
        # duration of 0 it must stop immediately rather than draining a
        # sys.maxsize-length generator.
        stats = run(generate_events(1_000_000, seed=0), FakePublisher(), "t", rate=0, duration=0)
        assert stats.published == 0

    def test_rate_paces_the_stream(self):
        import time

        start = time.monotonic()
        run(_events(3), FakePublisher(), "t", rate=20)
        # 3 events at 20/sec => targets at 0.00, 0.05, 0.10s. Asserting a
        # lower bound only; an upper bound would be flaky under load.
        assert time.monotonic() - start >= 0.10


class TestLoadEventsFromFile:
    def test_reads_a_valid_list(self, tmp_path, baseline_alert):
        path = tmp_path / "alerts.json"
        path.write_text(json.dumps([baseline_alert, baseline_alert]))

        events = load_events_from_file(path)
        assert len(events) == 2
        assert all(event.kind == "replay" for event in events)

    def test_missing_file_is_reported_clearly(self, tmp_path):
        with pytest.raises(EventSourceError, match="Cannot read"):
            load_events_from_file(tmp_path / "nope.json")

    def test_invalid_json_is_reported_clearly(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{not json")
        with pytest.raises(EventSourceError, match="not valid JSON"):
            load_events_from_file(path)

    def test_non_list_is_rejected(self, tmp_path, baseline_alert):
        path = tmp_path / "obj.json"
        path.write_text(json.dumps(baseline_alert))
        with pytest.raises(EventSourceError, match="must contain a JSON list"):
            load_events_from_file(path)

    def test_empty_list_is_rejected(self, tmp_path):
        path = tmp_path / "empty.json"
        path.write_text("[]")
        with pytest.raises(EventSourceError, match="no alerts"):
            load_events_from_file(path)

    def test_non_object_record_is_rejected(self, tmp_path, baseline_alert):
        path = tmp_path / "mixed.json"
        path.write_text(json.dumps([baseline_alert, "not an object"]))
        with pytest.raises(EventSourceError, match=r"\[1\] is not a JSON object"):
            load_events_from_file(path)

    def test_missing_fields_name_the_record_and_the_fields(self, tmp_path, baseline_alert):
        incomplete = {k: v for k, v in baseline_alert.items() if k != "egress_follows_access"}
        path = tmp_path / "incomplete.json"
        path.write_text(json.dumps([baseline_alert, incomplete]))

        with pytest.raises(EventSourceError) as exc:
            load_events_from_file(path)
        assert "[1]" in str(exc.value)
        assert "egress_follows_access" in str(exc.value)

    def test_nothing_is_returned_when_any_record_is_bad(self, tmp_path, baseline_alert):
        # Validation is all-or-nothing on purpose: a partially published
        # run cannot be recalled, because those events were classified.
        path = tmp_path / "partial.json"
        path.write_text(json.dumps([baseline_alert] * 46 + [{"detection_rule_id": "R"}]))
        with pytest.raises(EventSourceError):
            load_events_from_file(path)


class TestMain:
    def test_dry_run_needs_no_project(self, capsys):
        assert main(["--count", "3", "--rate", "0", "--dry-run"]) == 0
        assert len(capsys.readouterr().out.strip().splitlines()) == 3

    def test_publishing_without_a_project_is_refused(self, capsys, monkeypatch):
        monkeypatch.delenv("GCP_PROJECT", raising=False)
        assert main(["--count", "1"]) == 2
        assert "--project is required" in capsys.readouterr().err

    def test_negative_rate_is_refused(self, capsys):
        assert main(["--rate", "-1", "--dry-run"]) == 2
        assert "--rate must not be negative" in capsys.readouterr().err

    def test_bad_stream_config_is_reported_not_raised(self, capsys):
        assert main(["--deviation-rate", "5", "--dry-run"]) == 2
        assert "deviation_rate" in capsys.readouterr().err

    def test_bad_file_is_reported_not_raised(self, capsys, tmp_path):
        assert main(["--file", str(tmp_path / "missing.json"), "--dry-run"]) == 2
        assert "Cannot read" in capsys.readouterr().err

    def test_summary_reports_counts_on_stderr(self, capsys):
        main(["--count", "20", "--case-interval", "10", "--rate", "0", "--dry-run"])
        err = capsys.readouterr().err
        assert "would publish 20 event(s)" in err
        assert "canonical cases injected" in err

    def test_summary_goes_to_stderr_so_stdout_stays_pipeable(self, capsys):
        main(["--count", "5", "--rate", "0", "--dry-run"])
        captured = capsys.readouterr()
        # Every stdout line must be a bare alert, so `| jq` and
        # redirection into --file both work.
        for line in captured.out.strip().splitlines():
            assert json.loads(line)
        assert "would publish" in captured.err


class TestStats:
    def test_counters_are_independent_between_instances(self):
        first, second = _Stats(), _Stats()
        first.by_kind["background"] += 1
        assert second.by_kind == {}

    def test_generated_event_defaults(self):
        event = GeneratedEvent(alert={}, kind="background")
        assert event.case is None
        assert event.deviated_fields == ()
