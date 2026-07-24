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
