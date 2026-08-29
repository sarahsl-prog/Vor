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
from pathlib import Path

import pytest

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
