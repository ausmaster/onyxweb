"""A timed-out fetch fails close to its timeout, and the tab is usable afterwards.

After a timeout the engine resets the tab to ``about:blank``. When the navigation
itself is stuck that reset can never finish, and the tab is recreated instead —
so waiting long on the reset only delays the error the caller is owed.
"""

from __future__ import annotations

import time
from collections.abc import Iterator

import onyxweb
import pytest
from pytest_httpserver import HTTPServer
from werkzeug import Request, Response

TIMEOUT_MS = 500
OVERRUN_S = 0.75  # slack past the timeout for the reset and error path; was 2 s+


def _slow(_request: Request) -> Response:
    time.sleep(3)  # far past TIMEOUT_MS, so the navigation is still stuck when it fires
    return Response("<p>slow</p>", content_type="text/html")


@pytest.fixture
def slow_server() -> Iterator[HTTPServer]:
    # Threaded, so the stuck request doesn't block the recovery fetch behind it.
    with HTTPServer(threaded=True) as server:
        server.expect_request("/slow").respond_with_handler(_slow)
        server.expect_request("/ok").respond_with_data("<p>ok</p>", content_type="text/html")
        yield server


def test_stuck_navigation_fails_near_its_timeout(slow_server: HTTPServer) -> None:
    with onyxweb.Client(concurrency=1) as client:
        started = time.perf_counter()
        with pytest.raises(TimeoutError):
            client.fetch(slow_server.url_for("/slow"), timeout_ms=TIMEOUT_MS)
        elapsed = time.perf_counter() - started
        # The same (only) tab must serve the next fetch.
        assert "ok" in client.fetch(slow_server.url_for("/ok"))
    assert elapsed < TIMEOUT_MS / 1000 + OVERRUN_S, f"timeout surfaced after {elapsed:.2f} s"


def test_timeout_after_navigation_keeps_a_working_tab(slow_server: HTTPServer) -> None:
    """A slow post-load script times out on a healthy tab; recovery stays quick."""
    stall = "new Promise(resolve => setTimeout(resolve, 4000))"
    with onyxweb.Client(concurrency=1) as client:
        started = time.perf_counter()
        with pytest.raises(TimeoutError):
            client.fetch(
                slow_server.url_for("/ok"), timeout_ms=TIMEOUT_MS, post_load_scripts=[stall]
            )
        elapsed = time.perf_counter() - started
        assert "ok" in client.fetch(slow_server.url_for("/ok"))
    assert elapsed < TIMEOUT_MS / 1000 + OVERRUN_S, f"timeout surfaced after {elapsed:.2f} s"
