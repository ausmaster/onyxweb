"""An invalid URL fails at once with ``kind="invalid_url"``, not after a timeout.

Chrome rejects such a ``Page.navigate`` in under a millisecond ("Cannot navigate
to invalid URL"), but chromiumoxide holds that error waiting for a navigation
that never starts, so the fetch used to sit out the whole navigation timeout.
"""

from __future__ import annotations

import time

import onyxweb
import pytest
from pytest_httpserver import HTTPServer

FAST_S = 1.0  # a rejected URL costs no navigation; the timeout below is 5 s
TIMEOUT_MS = 5_000  # what an unrejected invalid URL would wait out


@pytest.mark.parametrize("url", ["not-a-url", "example.com", "http://"])
def test_invalid_url_fails_fast(url: str) -> None:
    started = time.perf_counter()
    with pytest.raises(onyxweb.OnyxwebError) as exc:
        onyxweb.fetch(url, timeout_ms=TIMEOUT_MS)
    assert time.perf_counter() - started < FAST_S
    assert exc.value.kind == "invalid_url"
    assert exc.value.url == url


def test_invalid_url_error_says_how_to_fix_it() -> None:
    with pytest.raises(onyxweb.OnyxwebError, match="https://"):
        onyxweb.fetch("example.com", timeout_ms=TIMEOUT_MS)


def test_screenshot_rejects_an_invalid_url_fast() -> None:
    started = time.perf_counter()
    with pytest.raises(onyxweb.OnyxwebError):
        onyxweb.screenshot("not-a-url", timeout_ms=TIMEOUT_MS)
    assert time.perf_counter() - started < FAST_S


def test_batch_returns_the_invalid_url_in_place(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/ok.html").respond_with_data("<p>ok</p>", content_type="text/html")
    good = httpserver.url_for("/ok.html")
    with onyxweb.Client(concurrency=2) as client:
        started = time.perf_counter()
        results = client.batch(
            [good, "not-a-url", good], config=onyxweb.FetchConfig(timeout_ms=TIMEOUT_MS)
        )
        elapsed = time.perf_counter() - started
    assert elapsed < FAST_S
    rejected = results[1]
    assert isinstance(rejected, onyxweb.OnyxwebError)
    assert rejected.kind == "invalid_url"
    assert not isinstance(results[0], Exception)
    assert not isinstance(results[2], Exception)


def test_tab_is_usable_right_after_an_invalid_url(httpserver: HTTPServer) -> None:
    """Rejected before navigating, so the only pooled tab is never wedged."""
    httpserver.expect_request("/ok.html").respond_with_data("<p>ok</p>", content_type="text/html")
    with onyxweb.Client(concurrency=1) as client:
        with pytest.raises(onyxweb.OnyxwebError):
            client.fetch("not-a-url", timeout_ms=TIMEOUT_MS)
        started = time.perf_counter()
        page = client.fetch(httpserver.url_for("/ok.html"))
        elapsed = time.perf_counter() - started
    assert "ok" in page
    assert elapsed < FAST_S
