"""Real-browser tests for the hydration settle strategy in render_page.py.

Unlike test_render_page.py, which mocks Chromium, these launch a real headless
browser and load synthetic pages via ``page.set_content`` — no network, no URL,
so url_safety's SSRF gate is not involved. They verify that scroll-triggered
content, collapsed content, and racy mounts actually enter the DOM after the
new settle logic runs, which is the behavior the mocks cannot prove.

Skipped automatically when Playwright or its Chromium build is unavailable.
"""
import os
import sys

import pytest

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills", "seo", "scripts")
sys.path.insert(0, _SCRIPTS)

import render_page  # noqa: E402

try:
    from playwright.sync_api import sync_playwright
    _PW_IMPORT = True
except Exception:  # pragma: no cover
    _PW_IMPORT = False


def _chromium_available() -> bool:
    if not _PW_IMPORT:
        return False
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
        return True
    except Exception:  # pragma: no cover - environment without the browser build
        return False


pytestmark = pytest.mark.skipif(
    not _chromium_available(), reason="Playwright Chromium not available"
)


class _Browser:
    """Context manager yielding a fresh page in a real headless Chromium."""

    def __enter__(self):
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        self._page = self._browser.new_page(viewport={"width": 800, "height": 600})
        return self._page

    def __exit__(self, *exc):
        self._browser.close()
        self._pw.stop()


# A page whose below-the-fold content only mounts when scrolled into view via an
# IntersectionObserver — exactly the case a top-of-page snapshot misses.
LAZY_SCROLL_HTML = """
<!DOCTYPE html><html><body>
  <div style="height:2000px">tall spacer so the sentinel starts off-screen</div>
  <div id="sentinel"></div>
  <div id="lazy"></div>
  <script>
    const obs = new IntersectionObserver((entries) => {
      for (const e of entries) {
        if (e.isIntersecting) {
          document.getElementById('lazy').textContent =
            'LAZY_CONTENT_MOUNTED_ON_SCROLL ' + 'x'.repeat(120);
          obs.disconnect();
        }
      }
    });
    obs.observe(document.getElementById('sentinel'));
  </script>
</body></html>
"""

COLLAPSED_HTML = """
<!DOCTYPE html><html><body>
  <p>always visible text, padded to clear the stability floor %s</p>
  <details><summary>More</summary><p>DETAILS_HIDDEN_TEXT here</p></details>
  <button aria-expanded="false" id="tab">Show panel</button>
  <div id="panel" hidden></div>
  <a href="/nav" aria-expanded="false" id="link">a link that must not be clicked</a>
  <script>
    document.getElementById('tab').addEventListener('click', (e) => {
      const el = e.currentTarget;
      el.setAttribute('aria-expanded', 'true');
      const panel = document.getElementById('panel');
      panel.hidden = false;
      panel.textContent = 'TAB_PANEL_REVEALED_TEXT';
    });
    // If the link were ever clicked, this flag would flip.
    document.getElementById('link').addEventListener('click', () => {
      window.__linkClicked = true;
    });
  </script>
</body></html>
""" % ("y" * 120)


def test_scroll_triggers_intersection_observer_content():
    with _Browser() as page:
        page.set_content(LAZY_SCROLL_HTML)
        # Before scrolling, the lazy node is empty — a plain snapshot misses it.
        assert page.evaluate("() => document.getElementById('lazy').textContent") == ""
        passes = render_page._scroll_through_page(page, timeout_ms=8000)
        assert passes >= 1
        text = page.evaluate("() => document.getElementById('lazy').textContent")
        assert "LAZY_CONTENT_MOUNTED_ON_SCROLL" in text


def test_scroll_returns_viewport_to_top():
    with _Browser() as page:
        page.set_content(LAZY_SCROLL_HTML)
        render_page._scroll_through_page(page, timeout_ms=8000)
        # Above-the-fold analysis and screenshots need the header, not the tail.
        assert page.evaluate("() => window.scrollY") == 0


def test_settle_scroll_surfaces_lazy_text_in_content():
    """End to end through _settle_page: the rendered DOM contains lazy content."""
    with _Browser() as page:
        page.set_content(LAZY_SCROLL_HTML)
        diagnostics, info = render_page._settle_page(
            page, "scroll", timeout_ms=8000, reveal_hidden=False
        )
        assert info["scroll_passes"] >= 1
        text = page.evaluate("() => document.getElementById('lazy').textContent")
        assert "LAZY_CONTENT_MOUNTED_ON_SCROLL" in text


def test_dom_strategy_alone_misses_scroll_content():
    """Guards the premise: without scrolling, the lazy content stays absent, so
    the scroll strategy is doing real work rather than passing trivially."""
    with _Browser() as page:
        page.set_content(LAZY_SCROLL_HTML)
        render_page._settle_page(page, "dom", timeout_ms=4000, reveal_hidden=False)
        text = page.evaluate("() => document.getElementById('lazy').textContent")
        assert "LAZY_CONTENT_MOUNTED_ON_SCROLL" not in text


def test_reveal_hidden_opens_details_and_tabs():
    with _Browser() as page:
        page.set_content(COLLAPSED_HTML)
        before = page.evaluate("() => document.body.innerText")
        assert "TAB_PANEL_REVEALED_TEXT" not in before
        count = render_page._reveal_hidden_content(page)
        assert count >= 2  # the <details> and the tab button
        text = page.evaluate("() => document.body.innerText")
        assert "DETAILS_HIDDEN_TEXT" in text
        assert "TAB_PANEL_REVEALED_TEXT" in text


def test_reveal_hidden_never_clicks_links():
    with _Browser() as page:
        page.set_content(COLLAPSED_HTML)
        render_page._reveal_hidden_content(page)
        # The anchor carried aria-expanded=false but must be left untouched.
        assert page.evaluate("() => window.__linkClicked === true") is False


def test_reveal_off_by_default_leaves_collapsed_content_alone():
    with _Browser() as page:
        page.set_content(COLLAPSED_HTML)
        _, info = render_page._settle_page(page, "scroll", timeout_ms=4000, reveal_hidden=False)
        assert info["revealed_elements"] == 0
        text = page.evaluate("() => document.body.innerText")
        assert "TAB_PANEL_REVEALED_TEXT" not in text


def test_scroll_is_bounded_on_infinite_feed():
    """An infinite-scroll page keeps growing; the pass cap must still stop it."""
    infinite = """
    <!DOCTYPE html><html><body>
      <div id="feed" style="height:2000px">feed</div>
      <script>
        // Every scroll near the bottom appends more height, forever.
        window.addEventListener('scroll', () => {
          const f = document.getElementById('feed');
          if (window.scrollY + window.innerHeight >= f.scrollHeight - 10) {
            f.style.height = (f.offsetHeight + 2000) + 'px';
          }
        });
      </script>
    </body></html>
    """
    with _Browser() as page:
        page.set_content(infinite)
        passes = render_page._scroll_through_page(page, timeout_ms=8000)
        assert passes <= render_page._SCROLL_MAX_PASSES
