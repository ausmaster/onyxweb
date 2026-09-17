"""Waiting out a challenge ends when the real page arrives, not at the 12 s cap.

One size constant used to mean both "small enough to be a stub" (detection) and
"big enough to be the resolved page" (the bypass wait). Raising it for detection
precision made every resolved page between 15 KB and 30 KB wait the full 12 s.
"""

from __future__ import annotations

import time

import onyxweb
from pytest_httpserver import HTTPServer
from werkzeug import Request, Response

REAL_PAGE_BYTES = 20_000  # between the gates: above 15 KB resolved, below 30 KB stub
CHALLENGE_MAX_WAIT_S = 12.0  # mirrors CHALLENGE_MAX_WAIT_MS in src/engine.rs


def _challenge_then_real(request: Request) -> Response:
    """An Akamai interstitial that sets a cookie and reloads into the real page."""
    if "solved=1" in request.headers.get("Cookie", ""):
        body = (
            "<html><head><title>Real Page</title></head><body>"
            + "x" * REAL_PAGE_BYTES
            + "</body></html>"
        )
    else:
        body = (
            '<html><body><div id="sec-if-cpt-container"></div>'
            '<script>document.cookie="solved=1;path=/";'
            "setTimeout(function(){location.reload();},800);</script></body></html>"
        )
    return Response(body, status=200, content_type="text/html")


def test_wait_ends_when_a_mid_sized_page_resolves(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/").respond_with_handler(_challenge_then_real)
    with onyxweb.Client(concurrency=1, bypass_anti_bot=True) as client:
        started = time.perf_counter()
        r = client.fetch(httpserver.url_for("/"), timeout_ms=30_000)
        elapsed = time.perf_counter() - started
    assert "Real Page" in r
    assert r.anti_bot == onyxweb.AntiBot(vendor="akamai", kind="challenge", resolved=True)
    assert elapsed < CHALLENGE_MAX_WAIT_S / 2, f"waited {elapsed:.1f} s for a page that resolved"
