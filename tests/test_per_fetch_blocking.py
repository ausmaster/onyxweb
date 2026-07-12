"""Per-fetch URL blocking: ``FetchConfig.block_urls`` prevents requests at
the network layer. Additive over Client-level ``NetworkConfig.block_urls``.

TDD-built across the per-fetch-extensions phase:
1. Block requests at the network layer (this file's first tests).
2. Cleanup — block_urls don't leak between fetches on same pool tab.

Tests use a real ``pytest-httpserver`` for both the parent page and the
tracking URLs. data: URLs would have null-origin and chromium restricts
cross-origin requests from null origins — same-origin via the httpserver
matches the real-world use case.
"""

from __future__ import annotations

import onyxweb
from pytest_httpserver import HTTPServer

# ----------------------------------------------------------------------------
# TDD #3: per-fetch block_urls execute (additive over Client-level)
# ----------------------------------------------------------------------------


def test_per_fetch_block_urls_baseline_no_block(httpserver: HTTPServer) -> None:
    """Baseline: without block_urls, the page-side fetch DOES hit the server.
    Sanity-checks the test setup so the blocking test below is meaningful."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body><script>"
        "fetch('/track', {mode: 'no-cors'}).catch(() => {})"
        "</script></body></html>",
        content_type="text/html",
    )
    httpserver.expect_request("/track").respond_with_data("hit")

    with onyxweb.Client() as c:
        c.fetch(httpserver.url_for("/"), wait_after_ms=300)

    track_hits = [req for req, _ in httpserver.log if req.path == "/track"]
    assert len(track_hits) >= 1, (
        f"baseline broken: server didn't receive /track, log={[r.path for r, _ in httpserver.log]}"
    )


def test_per_fetch_block_urls_blocks_request(httpserver: HTTPServer) -> None:
    """A URL in per-call block_urls prevents the request from reaching the server."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body><script>"
        "fetch('/track', {mode: 'no-cors'}).catch(() => {})"
        "</script></body></html>",
        content_type="text/html",
    )
    httpserver.expect_request("/track").respond_with_data("hit")
    track_url = httpserver.url_for("/track")

    with onyxweb.Client() as c:
        c.fetch(httpserver.url_for("/"), block_urls=[track_url], wait_after_ms=300)

    track_hits = [req for req, _ in httpserver.log if req.path == "/track"]
    assert len(track_hits) == 0, (
        f"block_urls didn't prevent /track: hits={[r.path for r, _ in httpserver.log]}"
    )


def test_per_fetch_block_urls_additive_over_client_level(httpserver: HTTPServer) -> None:
    """``FetchConfig.block_urls`` stacks on top of ``NetworkConfig.block_urls``."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body><script>"
        "fetch('/api1', {mode: 'no-cors'}).catch(() => {});"
        "fetch('/api2', {mode: 'no-cors'}).catch(() => {})"
        "</script></body></html>",
        content_type="text/html",
    )
    httpserver.expect_request("/api1").respond_with_data("a")
    httpserver.expect_request("/api2").respond_with_data("b")
    api1 = httpserver.url_for("/api1")
    api2 = httpserver.url_for("/api2")

    # Client blocks /api1; per-call adds /api2.
    with onyxweb.Client(block_urls=[api1]) as c:
        c.fetch(httpserver.url_for("/"), block_urls=[api2], wait_after_ms=400)

    api1_hits = [req for req, _ in httpserver.log if req.path == "/api1"]
    api2_hits = [req for req, _ in httpserver.log if req.path == "/api2"]
    assert len(api1_hits) == 0, f"client-level block didn't apply: {api1_hits}"
    assert len(api2_hits) == 0, f"per-call block didn't apply: {api2_hits}"


# ----------------------------------------------------------------------------
# TDD #4: cleanup — per-call block_urls must NOT leak to subsequent fetches.
# ``concurrency=1`` forces tab reuse so the leak is observable.
# ----------------------------------------------------------------------------


def test_per_fetch_block_urls_do_not_leak_to_next_fetch(httpserver: HTTPServer) -> None:
    """Fetch #1 blocks /track; fetch #2 (no per-call block) must still
    receive the response from /track."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body><script>"
        "fetch('/track', {mode: 'no-cors'}).catch(() => {})"
        "</script></body></html>",
        content_type="text/html",
    )
    httpserver.expect_request("/track").respond_with_data("hit")
    page = httpserver.url_for("/")
    track_url = httpserver.url_for("/track")

    with onyxweb.Client(concurrency=1) as c:
        # Fetch #1: per-call block_urls includes /track.
        c.fetch(page, block_urls=[track_url], wait_after_ms=300)
        # Sanity: fetch #1 should have blocked /track (zero hits so far).
        hits_after_1 = sum(1 for req, _ in httpserver.log if req.path == "/track")
        assert hits_after_1 == 0, f"sanity broken — fetch #1 didn't block: {hits_after_1}"

        # Fetch #2: no per-call block_urls. The previous block must NOT leak.
        c.fetch(page, wait_after_ms=300)

    # After fetch #2 (no per-call block), /track must have received its request.
    hits_after_2 = sum(1 for req, _ in httpserver.log if req.path == "/track")
    assert hits_after_2 >= 1, (
        f"per-call block_urls leaked from fetch #1 to fetch #2: /track hits={hits_after_2}"
    )


def test_per_fetch_block_urls_cleanup_preserves_client_level_block(
    httpserver: HTTPServer,
) -> None:
    """Cleanup restores the Client-level ``block_urls`` baseline. After a
    fetch with per-call additions, the Client-level block must still apply
    on the next fetch."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body><script>"
        "fetch('/banned', {mode: 'no-cors'}).catch(() => {});"
        "fetch('/temp', {mode: 'no-cors'}).catch(() => {})"
        "</script></body></html>",
        content_type="text/html",
    )
    httpserver.expect_request("/banned").respond_with_data("banned")
    httpserver.expect_request("/temp").respond_with_data("temp")
    page = httpserver.url_for("/")
    banned = httpserver.url_for("/banned")
    temp = httpserver.url_for("/temp")

    with onyxweb.Client(concurrency=1, block_urls=[banned]) as c:
        # Fetch #1: client-level blocks /banned, per-call ADDS /temp.
        c.fetch(page, block_urls=[temp], wait_after_ms=400)
        # Fetch #2: only client-level block applies (no per-call). /banned
        # still blocked, /temp now reachable.
        c.fetch(page, wait_after_ms=400)

    banned_hits = sum(1 for req, _ in httpserver.log if req.path == "/banned")
    temp_hits = sum(1 for req, _ in httpserver.log if req.path == "/temp")

    # /banned must remain blocked across both fetches (Client-level).
    assert banned_hits == 0, f"Client-level block lost after cleanup: {banned_hits} hits"
    # /temp blocked in #1, reachable in #2 (1 hit total).
    assert temp_hits == 1, f"per-call /temp block didn't restore: {temp_hits} hits (expected 1)"


# ----------------------------------------------------------------------------
# TDD #5: AsyncClient parity — same blocking + cleanup via async.
# ----------------------------------------------------------------------------


async def test_async_per_fetch_block_urls_blocks_request(httpserver: HTTPServer) -> None:
    """``await ac.fetch(block_urls=...)`` blocks identically to sync."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body><script>"
        "fetch('/track', {mode: 'no-cors'}).catch(() => {})"
        "</script></body></html>",
        content_type="text/html",
    )
    httpserver.expect_request("/track").respond_with_data("hit")
    track_url = httpserver.url_for("/track")

    async with onyxweb.AsyncClient() as ac:
        await ac.fetch(httpserver.url_for("/"), block_urls=[track_url], wait_after_ms=300)

    track_hits = [req for req, _ in httpserver.log if req.path == "/track"]
    assert len(track_hits) == 0


async def test_async_per_fetch_block_urls_cleanup_no_leak(httpserver: HTTPServer) -> None:
    """Async path honors the same cleanup discipline as sync."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body><script>"
        "fetch('/track', {mode: 'no-cors'}).catch(() => {})"
        "</script></body></html>",
        content_type="text/html",
    )
    httpserver.expect_request("/track").respond_with_data("hit")
    page = httpserver.url_for("/")
    track_url = httpserver.url_for("/track")

    async with onyxweb.AsyncClient(concurrency=1) as ac:
        await ac.fetch(page, block_urls=[track_url], wait_after_ms=300)
        hits_after_1 = sum(1 for req, _ in httpserver.log if req.path == "/track")
        assert hits_after_1 == 0, "sanity: fetch #1 should have blocked /track"

        await ac.fetch(page, wait_after_ms=300)

    hits_after_2 = sum(1 for req, _ in httpserver.log if req.path == "/track")
    assert hits_after_2 >= 1, f"per-call block leaked across async fetches: {hits_after_2}"
