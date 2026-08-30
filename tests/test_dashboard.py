"""
Vör — tests for the Streamlit dashboard.

The dashboard had no tests at all, which is how two bugs that a single
render pass would have caught reached main: an auto-refresh rerun loop
that hung two pages on any interaction, and a staleness column reading a
Firestore field that is never written.

Streamlit's own `AppTest` runs a page script headlessly — no browser, no
server — so these are ordinary pytest tests. Firestore is unreachable in
the test environment, so every page renders from the demo fallback; that
is deliberate here, since the demo path is what an operator sees when
credentials are missing and it should render cleanly too.

Kept deliberately small: these are smoke tests over the render path plus
regressions for the two specific bugs, not a widget-by-widget UI suite.
"""

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.conftest import FakeFirestoreClient
from vor_agents.schemas import AuditorAction, AuditorOutput, ClassifierOutput, Decision
from vor_agents.tracing import log_audit_trace, log_classification_trace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard"))

from streamlit.testing.v1 import AppTest

PAGES = Path(__file__).resolve().parent.parent / "dashboard" / "pages"
ALL_PAGES = ["home", "patterns", "detail", "pipeline", "traces"]

# Pages that call inject_auto_refresh(). These are the ones the rerun-loop
# bug hung; the others reran fine, which is what localised it.
AUTO_REFRESH_PAGES = ["home", "traces"]


def _app(page: str, timeout: int = 15) -> AppTest:
    # A healthy page renders in about a second. The generous-but-bounded
    # timeout is what turns the rerun loop into a fast failure instead of
    # a hung CI job — the loop never completes, so only the clock stops it.
    return AppTest.from_file(str(PAGES / f"{page}.py"), default_timeout=timeout)


class TestPagesRender:
    @pytest.mark.parametrize("page", ALL_PAGES)
    def test_page_renders_without_exception(self, page):
        app = _app(page).run()
        assert not app.exception


class TestStalenessIsComputed:
    """Regression: load_patterns() read `days_since_last_review` as a
    stored Firestore field. Nothing writes it — only `last_reviewed_at` is
    persisted — so it was None for every real pattern and coerced to 0.

    These use the real FakeFirestoreClient and the real loader, because
    the bug lived precisely in the gap between what the dashboard assumed
    a confidence_doc contains and what one actually contains. Asserting
    against a hand-built dict would have reproduced the same wrong
    assumption and passed.
    """

    @staticmethod
    def _load(monkeypatch, docs):
        import shared

        client = FakeFirestoreClient()
        for doc_id, data in docs.items():
            client.collection("confidence_docs").document(doc_id).set(data)
        monkeypatch.setattr(shared, "_get_firestore_client", lambda: client)
        shared.load_patterns.clear()
        return shared.load_patterns()

    def test_never_reviewed_pattern_reads_as_maximally_stale(self, monkeypatch):
        # The bug's worst case: no last_reviewed_at at all previously
        # displayed as 0 — "audited today" — for a pattern never audited.
        frame = self._load(monkeypatch, {"d1": {"identity_key": ["r", "p", "c", "e"]}})
        assert frame.iloc[0]["days_since_last_review"] == 9999

    def test_recent_review_reads_as_recent(self, monkeypatch):
        recent = (datetime.now(UTC) - timedelta(days=3)).isoformat()
        frame = self._load(monkeypatch, {"d1": {"last_reviewed_at": recent}})
        assert frame.iloc[0]["days_since_last_review"] == 3

    def test_a_stored_days_field_is_ignored_in_favour_of_the_timestamp(self, monkeypatch):
        # If a doc ever did carry the field, the timestamp still wins —
        # otherwise the dashboard could disagree with enrich() and the
        # sweep about the same pattern's staleness.
        frame = self._load(
            monkeypatch,
            {"d1": {"days_since_last_review": 0, "last_reviewed_at": ""}},
        )
        assert frame.iloc[0]["days_since_last_review"] == 9999

    def test_malformed_timestamp_is_treated_as_never_reviewed(self, monkeypatch):
        frame = self._load(monkeypatch, {"d1": {"last_reviewed_at": "not-a-date"}})
        assert frame.iloc[0]["days_since_last_review"] == 9999


class TestLoadTraces:
    """The trace pages read MLflow, which is where traces actually live.

    These write with the *real* tracing.py and read with the *real*
    dashboard loader, against a real MLflow sqlite store. Mocking the
    query would test the loader against my own assumption about what
    tracing.py writes -- and the whole bug being fixed here was the two
    sides disagreeing about where traces are. Nothing is asserted that a
    round trip doesn't actually produce.
    """

    @staticmethod
    def _write_and_load(tmp_path, monkeypatch, writes):
        monkeypatch.setenv("MLFLOW_TRACKING_URI", f"sqlite:///{tmp_path}/mlflow.db")
        monkeypatch.setenv("MLFLOW_EXPERIMENT_NAME", f"vor-test-{tmp_path.name}")
        import shared

        for write in writes:
            write()
        shared.load_traces.clear()
        return shared.load_traces()

    def _classification(self, decision, overrides=(), reasoning="ok"):
        return lambda: log_classification_trace(
            {"detection_rule_id": "R"},
            {"pattern_identity_key": ("SharePoint_ToolPane_Rule", "w3wp.exe", "csc.exe", "adm")},
            ClassifierOutput(decision=decision, reasoning=reasoning),
            list(overrides),
            None,
        )

    def _audit(self, action):
        return lambda: log_audit_trace(
            ("R", "p", "c", "e"),
            {},
            AuditorOutput(action=action, reasoning="checked", concerns_found=[]),
            False,
            None,
        )

    def test_round_trip_reads_back_what_the_service_wrote(self, tmp_path, monkeypatch):
        frame = self._write_and_load(
            tmp_path,
            monkeypatch,
            [
                self._classification(Decision.SUPPRESS),
                self._classification(Decision.ESCALATE, ["ground_truth_missed"]),
                self._audit(AuditorAction.DOWNGRADE),
            ],
        )
        assert len(frame) == 3
        assert frame["run_type"].value_counts().to_dict() == {"classification": 2, "audit": 1}

    def test_decisions_are_the_values_the_badges_key_on(self, tmp_path, monkeypatch):
        # If these came back as "Decision.SUPPRESS" every badge lookup in
        # the pages would miss and fall through to a default -- silently.
        frame = self._write_and_load(
            tmp_path, monkeypatch, [self._classification(Decision.SUPPRESS)]
        )
        assert frame.iloc[0]["decision"] == "SUPPRESS"

    def test_audit_rows_carry_action_and_the_decision_placeholder(self, tmp_path, monkeypatch):
        # The pages render `decision if decision != "—" else action`, so a
        # missing param has to arrive as that placeholder, not NaN.
        frame = self._write_and_load(tmp_path, monkeypatch, [self._audit(AuditorAction.NO_ACTION)])
        row = frame.iloc[0]
        assert row["action"] == "NO_ACTION"
        assert row["decision"] == "—"

    def test_identity_key_renders_with_arrows(self, tmp_path, monkeypatch):
        frame = self._write_and_load(
            tmp_path, monkeypatch, [self._classification(Decision.SUPPRESS)]
        )
        assert frame.iloc[0]["identity_key"] == (
            "SharePoint_ToolPane_Rule → w3wp.exe → csc.exe → adm"
        )

    def test_overrides_distinguish_none_fired_from_fired(self, tmp_path, monkeypatch):
        frame = self._write_and_load(
            tmp_path,
            monkeypatch,
            [
                self._classification(Decision.SUPPRESS),
                self._classification(Decision.ESCALATE, ["under_review"]),
            ],
        )
        assert set(frame["overrides_fired"]) == {"", "under_review"}

    def test_reasoning_is_truncated_for_display(self, tmp_path, monkeypatch):
        frame = self._write_and_load(
            tmp_path, monkeypatch, [self._classification(Decision.SUPPRESS, reasoning="x" * 500)]
        )
        assert len(frame.iloc[0]["reasoning"]) == 200

    def test_no_runs_yields_an_empty_frame_that_still_has_columns(self, tmp_path, monkeypatch):
        # A healthy, quiet deployment has zero runs. pd.DataFrame([]) has
        # no columns, so pages that filter on traces["run_type"] after an
        # .empty guard would raise KeyError instead of saying "none yet".
        frame = self._write_and_load(tmp_path, monkeypatch, [])
        assert frame.empty
        for column in ("run_type", "decision", "action", "overrides_fired"):
            assert column in frame

    def test_the_traces_page_renders_runs_from_mlflow(self, tmp_path, monkeypatch):
        # The end of the chain: not just that the loader can read MLflow,
        # but that the page an analyst opens shows what the service
        # logged there. Previously this page rendered the pending_traces
        # outage queue, so it was blank whenever the system was healthy.
        monkeypatch.setenv("MLFLOW_TRACKING_URI", f"sqlite:///{tmp_path}/mlflow.db")
        monkeypatch.setenv("MLFLOW_EXPERIMENT_NAME", f"vor-page-{tmp_path.name}")
        self._classification(Decision.ESCALATE, ["ground_truth_missed"])()

        import shared

        shared.load_traces.clear()
        app = _app("traces").run()

        assert not app.exception
        rendered = " ".join(block.value for block in app.markdown)
        assert "ESCALATE" in rendered
        assert "ground_truth_missed" in rendered

    def test_unconfigured_mlflow_falls_back_to_demo_data(self, monkeypatch):
        monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)
        import shared

        shared.load_traces.clear()
        frame = shared.load_traces()
        assert not frame.empty


class TestCountPendingTraces:
    """pending_traces is an outage backlog, not the trace store. It is
    surfaced as a count so a non-zero value reads as "this feed is behind"
    rather than being mistaken for the runs themselves."""

    def test_counts_queued_docs(self, monkeypatch):
        import shared

        client = FakeFirestoreClient()
        for doc_id in ("t1", "t2"):
            client.collection("pending_traces").document(doc_id).set({"run_type": "classification"})
        monkeypatch.setattr(shared, "_get_firestore_client", lambda: client)
        shared.count_pending_traces.clear()
        assert shared.count_pending_traces() == 2

    def test_empty_queue_is_zero(self, monkeypatch):
        import shared

        monkeypatch.setattr(shared, "_get_firestore_client", lambda: FakeFirestoreClient())
        shared.count_pending_traces.clear()
        assert shared.count_pending_traces() == 0

    def test_no_firestore_is_zero_not_a_crash(self, monkeypatch):
        import shared

        monkeypatch.setattr(shared, "_get_firestore_client", lambda: None)
        shared.count_pending_traces.clear()
        assert shared.count_pending_traces() == 0


class TestAutoRefreshDoesNotLoop:
    """Regression: `_auto_refresh_armed` was armed once and never cleared,
    so every script run after the first re-entered the fragment already
    armed and called st.rerun() while rendering — an unbreakable loop.

    AppTest surfaces the loop as a run timeout rather than a raised
    exception, so these assert that a run *completes*. A regression makes
    them hang until the timeout and fail; it does not make them pass
    quietly."""

    @pytest.mark.parametrize("page", AUTO_REFRESH_PAGES)
    def test_consecutive_reruns_complete(self, page):
        app = _app(page)
        app.run()
        app.run()
        app.run()
        assert not app.exception

    def test_widget_interaction_completes(self):
        # The user-visible symptom: touching a filter on the Traces page
        # hung it, because a widget change is just another script run.
        app = _app("traces")
        app.run()
        app.text_input[0].input("SUPPRESS").run()
        assert not app.exception

    @pytest.mark.parametrize("page", AUTO_REFRESH_PAGES)
    def test_refresh_is_still_armed_after_a_render(self, page):
        # The counterpart to the tests above: they prove the loop is gone,
        # this proves the feature survived. `_tick` arms the flag at the
        # end of its render pass so the *timer* re-execution reruns the
        # app; the next script run disarms it again before re-entering.
        # Deleting the st.rerun() would also stop the loop — and stop the
        # dashboard refreshing. This fails if anyone does that.
        app = _app(page).run()
        assert app.session_state["_auto_refresh_armed"] is True
