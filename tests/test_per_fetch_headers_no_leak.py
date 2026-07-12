"""Per-fetch ``extra_headers`` cleanup: per-call header overrides must not
leak to the next fetch on the same pool tab.

Mirrors the cleanup discipline already enforced for ``block_urls``, scripts,
and ``block_navigation``. Without this cleanup, a ``Client`` with a
long-lived pool tab would carry headers from any call into every subsequent
call until a different fetch overwrote them.
"""

from __future__ import annotations

import onyxweb
from pytest_httpserver import HTTPServer


def test_per_call_extra_headers_do_not_leak(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/").respond_with_data(
        "<html><body>x</body></html>", content_type="text/html"
    )
    url = httpserver.url_for("/")

    with onyxweb.Client(concurrency=1) as c:
        # Fetch 1 sets a per-call header.
        c.fetch(url, extra_headers={"X-Per-Call": "fetch1-value"})
        # Fetch 2 sets none.
        c.fetch(url)

    requests = [req for req, _ in httpserver.log if req.path == "/"]
    assert len(requests) >= 2
    # Fetch 1 should have the per-call header.
    assert requests[0].headers.get("X-Per-Call") == "fetch1-value"
    # Fetch 2 must NOT have it — the per-call override must have been
    # cleaned up after fetch 1.
    assert requests[1].headers.get("X-Per-Call") is None, (
        f"X-Per-Call leaked into fetch 2: {requests[1].headers.get('X-Per-Call')!r}"
    )


def test_per_call_overrides_baseline_then_cleans_up(httpserver: HTTPServer) -> None:
    """Client-level baseline header must persist; per-call override replaces
    it for that call only, then the baseline restores."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body>x</body></html>", content_type="text/html"
    )
    url = httpserver.url_for("/")

    with onyxweb.Client(concurrency=1, extra_headers={"X-Persist": "baseline"}) as c:
        c.fetch(url, extra_headers={"X-Persist": "override"})  # call overrides baseline
        c.fetch(url)  # baseline must be back

    requests = [req for req, _ in httpserver.log if req.path == "/"]
    assert len(requests) >= 2
    assert requests[0].headers.get("X-Persist") == "override"
    assert requests[1].headers.get("X-Persist") == "baseline", (
        f"baseline X-Persist not restored: {requests[1].headers.get('X-Persist')!r}"
    )


def test_extra_headers_cleanup_in_pool_soak(httpserver: HTTPServer) -> None:
    """Mixed-config 30-fetch soak with per-call extra_headers must not leak."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body>x</body></html>", content_type="text/html"
    )
    url = httpserver.url_for("/")

    with onyxweb.Client(concurrency=1) as c:
        for i in range(30):
            if i % 2 == 0:
                c.fetch(url, extra_headers={"X-Iter": f"v{i}"})
            else:
                c.fetch(url)

        httpserver.clear_log()
        c.fetch(url)  # control fetch — must have NO X-Iter header

    requests = [req for req, _ in httpserver.log if req.path == "/"]
    assert len(requests) >= 1
    assert requests[-1].headers.get("X-Iter") is None, (
        f"X-Iter leaked into control fetch: {requests[-1].headers.get('X-Iter')!r}"
    )
