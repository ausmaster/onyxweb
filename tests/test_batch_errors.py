"""`batch` returns errors in-list (not stubs); every fetch error carries `.url`/`.kind`.

A failed batch item IS the exception instance a single fetch would raise
(``TimeoutError`` for timeouts, ``onyxweb.OnyxwebError`` otherwise), enriched
with ``.url`` (which URL failed) and ``.kind`` (the cause category). Single
fetches raise the same enriched exception.
"""

from __future__ import annotations

import time
from collections.abc import Iterator

import onyxweb
import pytest
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Request, Response

_TIMEOUT_MS = 700


@pytest.fixture
def tserver() -> Iterator[HTTPServer]:
    """A threaded mock server so a slow handler doesn't head-of-line-block the
    concurrent fast requests a batch issues in parallel."""
    with HTTPServer(threaded=True) as server:
        yield server


def _ok(_request: Request) -> Response:
    return Response("<html><body>ok</body></html>", content_type="text/html")


def _slow(_request: Request) -> Response:
    time.sleep(3)  # far longer than _TIMEOUT_MS — the fetch times out first
    return Response("<html><body>late</body></html>", content_type="text/html")


def _cfg() -> onyxweb.FetchConfig:
    return onyxweb.FetchConfig(timeout_ms=_TIMEOUT_MS)


def test_batch_returns_exception_in_list_for_timeout(tserver: HTTPServer) -> None:
    tserver.expect_request("/ok").respond_with_handler(_ok)
    tserver.expect_request("/slow").respond_with_handler(_slow)
    ok, slow = tserver.url_for("/ok"), tserver.url_for("/slow")
    with onyxweb.Client(concurrency=2) as client:
        results = client.batch([ok, slow], capture="html", config=_cfg())
    assert isinstance(results[0], onyxweb.RenderResult)
    assert results[0].status_code == 200
    err = results[1]
    assert isinstance(err, TimeoutError)  # a real exception object, not a stub
    # .url / .kind are injected onto the exception by the Rust layer (into_py_err),
    # so they aren't statically visible on the builtin TimeoutError.
    assert err.url == slow  # type: ignore[attr-defined]
    assert err.kind in ("navigation_timeout", "timeout")  # type: ignore[attr-defined]


def test_batch_positional_alignment_mixed(tserver: HTTPServer) -> None:
    """Order is preserved: item i corresponds to urls[i], failures interleaved."""
    tserver.expect_request("/ok").respond_with_handler(_ok)
    tserver.expect_request("/slow").respond_with_handler(_slow)
    ok, slow = tserver.url_for("/ok"), tserver.url_for("/slow")
    with onyxweb.Client(concurrency=3) as client:
        results = client.batch([ok, slow, ok], capture="html", config=_cfg())
    assert isinstance(results[0], onyxweb.RenderResult)
    assert isinstance(results[1], Exception)
    assert isinstance(results[2], onyxweb.RenderResult)


def test_batch_png_and_both_modes_return_exceptions(tserver: HTTPServer) -> None:
    """A failed item is an exception in png/both modes too (not empty bytes)."""
    tserver.expect_request("/slow").respond_with_handler(_slow)
    slow = tserver.url_for("/slow")
    with onyxweb.Client(concurrency=1) as client:
        png = client.batch([slow], capture="png", config=_cfg())
        both = client.batch([slow], capture="both", config=_cfg())
    assert isinstance(png[0], TimeoutError)
    assert png[0].url == slow  # type: ignore[attr-defined]
    assert isinstance(both[0], TimeoutError)


def test_batch_all_success_has_no_exceptions(tserver: HTTPServer) -> None:
    tserver.expect_request("/ok").respond_with_handler(_ok)
    ok = tserver.url_for("/ok")
    with onyxweb.Client(concurrency=2) as client:
        results = client.batch([ok, ok], capture="html")
    assert all(isinstance(r, onyxweb.RenderResult) for r in results)
    assert not any(isinstance(r, Exception) for r in results)


def test_single_fetch_raises_enriched_timeout(tserver: HTTPServer) -> None:
    """A single fetch still RAISES, but the exception now carries .url / .kind."""
    tserver.expect_request("/slow").respond_with_handler(_slow)
    slow = tserver.url_for("/slow")
    with onyxweb.Client(concurrency=1) as client, pytest.raises(TimeoutError) as ei:
        client.fetch(slow, config=_cfg())
    assert ei.value.url == slow  # type: ignore[attr-defined]
    assert ei.value.kind in ("navigation_timeout", "timeout")  # type: ignore[attr-defined]
