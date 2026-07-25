"""``wait_until`` lifecycle timing on full navigations.

``domcontentloaded`` must return at DCL (main document parsed), not wait for the
full ``load``. Regression for chromiumoxide's ``goto()`` blocking until *all*
frames fire ``load`` — a page whose only slow thing is a subframe returns fast
under ``domcontentloaded`` and slow under ``load``.
"""

from __future__ import annotations

import time
from collections.abc import Iterator

import onyxweb
import pytest
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Request, Response

_SLOW_S = 1.5


@pytest.fixture
def tserver() -> Iterator[HTTPServer]:
    """Threaded so the sleeping subframe request doesn't block the main doc."""
    with HTTPServer(threaded=True) as server:
        yield server


def _serve_iframe_page(server: HTTPServer) -> str:
    server.expect_request("/iframe").respond_with_data(
        "<html><body>main<iframe src='/slow'></iframe></body></html>",
        content_type="text/html",
    )

    def slow(_r: Request) -> Response:
        time.sleep(_SLOW_S)
        return Response("<html><body>slow-done</body></html>", content_type="text/html")

    server.expect_request("/slow").respond_with_handler(slow)
    return server.url_for("/iframe")


def test_domcontentloaded_returns_before_slow_subframe(tserver: HTTPServer) -> None:
    url = _serve_iframe_page(tserver)
    with onyxweb.Client(concurrency=1) as c:
        c.fetch("data:text/html,<html></html>")  # warm the pooled tab
        t = time.perf_counter()
        r = c.fetch(url, wait_until="domcontentloaded")
        elapsed = time.perf_counter() - t
    assert r.status_code == 200
    assert elapsed < 0.8, f"DCL waited for the {_SLOW_S}s subframe: {elapsed:.2f}s"


def test_load_waits_for_slow_subframe(tserver: HTTPServer) -> None:
    url = _serve_iframe_page(tserver)
    with onyxweb.Client(concurrency=1) as c:
        c.fetch("data:text/html,<html></html>")
        t = time.perf_counter()
        r = c.fetch(url, wait_until="load")
        elapsed = time.perf_counter() - t
    assert r.status_code == 200
    assert elapsed >= _SLOW_S - 0.3, f"load returned before the subframe: {elapsed:.2f}s"


def test_capture_returns_new_page_despite_prior_pushstate(tserver: HTTPServer) -> None:
    """A fetch must return ITS page, not the prior page's DOM.

    The prior page keeps firing ``pushState`` and the next page is slow to
    respond; the goto/lifecycle race must not resolve on the outgoing page's
    events while the new nav is in flight. Regression for the gauntlet finding.
    """
    tserver.expect_request("/spa").respond_with_data(
        "<html><body>SPA_PAGE_MARKER"
        "<script>setInterval(function(){"
        "history.pushState({}, '', '#' + Date.now());}, 100);</script>"
        "</body></html>",
        content_type="text/html",
    )

    def slow_next(_r: Request) -> Response:
        time.sleep(1.2)  # keeps the prior SPA page live + firing pushState meanwhile
        return Response("<html><body>NEXT_PAGE_MARKER</body></html>", content_type="text/html")

    tserver.expect_request("/next").respond_with_handler(slow_next)

    with onyxweb.Client(concurrency=1) as c:
        c.fetch(tserver.url_for("/spa"))  # prime the pooled tab (pushState running)
        r = c.fetch(tserver.url_for("/next"))  # slow to respond → must still be NEXT
    assert r.status_code == 200
    assert "NEXT_PAGE_MARKER" in r, f"captured stale/wrong content: {str(r)[:200]!r}"
    assert "SPA_PAGE_MARKER" not in r, "captured the PRIOR page's DOM"


def test_referer_preserved_under_race(tserver: HTTPServer) -> None:
    """The goto/lifecycle race must keep the Page.navigate referrer param."""
    tserver.expect_request("/").respond_with_data(
        "<html><body>ok</body></html>", content_type="text/html"
    )
    with onyxweb.Client(concurrency=1) as c:
        c.fetch(
            tserver.url_for("/"),
            wait_until="domcontentloaded",
            extra_headers={"Referer": "http://ref.example/x"},
        )
    reqs = [r for r, _ in tserver.log if r.path == "/"]
    assert reqs and reqs[-1].headers.get("Referer") == "http://ref.example/x"
