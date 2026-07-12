"""Per-fetch navigation blocking: ``FetchConfig.block_navigation`` keeps
the page on its original URL even when JS or actions try to navigate
away. Implemented via CDP ``Fetch.enable`` + ``Fetch.requestPaused``,
filtered to navigation-type requests.

Use case (DOMino's ``ClickJSElements``): click ``[href^="javascript:"]``
links sequentially on the same page state; without nav-blocking, the
first click that triggers ``window.location = ...`` scrambles the page
for subsequent clicks.

Rules:
- Initial page load is NOT blocked (the listener arms AFTER the
  lifecycle event).
- Cleanup runs unconditionally — Fetch domain disabled before pool
  return so the next fetch on the same tab is unaffected.
"""

from __future__ import annotations

import onyxweb
from onyxweb import Click
from pytest_httpserver import HTTPServer

# ----------------------------------------------------------------------------
# TDD #1: block_navigation prevents JS-triggered nav during actions
# ----------------------------------------------------------------------------


def test_block_navigation_doesnt_block_initial_page_load(httpserver: HTTPServer) -> None:
    """The initial fetch must still load the page; block_navigation only
    affects post-load navigation."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body>initial-loaded</body></html>",
        content_type="text/html",
    )

    with onyxweb.Client() as c:
        r = c.fetch(httpserver.url_for("/"), block_navigation=True)

    assert "initial-loaded" in r, f"initial load was blocked? html={r[:200]}"
    assert r.status_code == 200


def test_without_block_navigation_click_redirect_navigates(
    httpserver: HTTPServer,
) -> None:
    """Sanity baseline: without block_navigation, a click that does
    ``window.location.href = '/elsewhere'`` navigates the page; final_url
    moves."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body>"
        "<button id='go' onclick='window.location.href=\"/elsewhere\"'>x</button>"
        "</body></html>",
        content_type="text/html",
    )
    httpserver.expect_request("/elsewhere").respond_with_data(
        "<html><body>ELSEWHERE</body></html>",
        content_type="text/html",
    )

    with onyxweb.Client() as c:
        r = c.fetch(
            httpserver.url_for("/"),
            actions=[Click(type="click", selector="#go", wait_after_ms=500)],
        )

    assert "/elsewhere" in r.final_url, (
        f"sanity broken: click should have navigated; final_url={r.final_url}"
    )


def test_block_navigation_prevents_js_redirect_from_click(
    httpserver: HTTPServer,
) -> None:
    """With block_navigation=True, a click that triggers JS-initiated
    navigation is intercepted; final_url stays at the original page."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body>"
        "<button id='go' onclick='window.location.href=\"/elsewhere\"'>x</button>"
        "<div id='marker'>ORIGINAL_PAGE</div>"
        "</body></html>",
        content_type="text/html",
    )
    httpserver.expect_request("/elsewhere").respond_with_data(
        "<html><body>ELSEWHERE</body></html>",
        content_type="text/html",
    )

    with onyxweb.Client() as c:
        r = c.fetch(
            httpserver.url_for("/"),
            actions=[Click(type="click", selector="#go", wait_after_ms=500)],
            block_navigation=True,
        )

    assert "/elsewhere" not in r.final_url, (
        f"block_navigation didn't prevent nav; final_url={r.final_url}"
    )
    # The captured HTML reflects the original page (we never left).
    assert "ORIGINAL_PAGE" in r, f"page changed despite block_navigation; html={r[:300]}"


# ----------------------------------------------------------------------------
# TDD #2: cleanup — Fetch.disable runs unconditionally so the next fetch on
# the same pooled tab isn't intercepted (which would hang its initial goto).
# ----------------------------------------------------------------------------


def test_block_navigation_doesnt_leak_to_next_fetch(httpserver: HTTPServer) -> None:
    """concurrency=1: fetch #1 with block_navigation=True; fetch #2 without
    block_navigation must complete its initial load normally (the Fetch
    domain must have been disabled in cleanup)."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body>page-1</body></html>",
        content_type="text/html",
    )
    httpserver.expect_request("/two").respond_with_data(
        "<html><body>PAGE_TWO_LOADED</body></html>",
        content_type="text/html",
    )

    with onyxweb.Client(concurrency=1) as c:
        # Fetch #1 with block_navigation arms the listener.
        r1 = c.fetch(httpserver.url_for("/"), block_navigation=True)
        assert "page-1" in r1
        # Fetch #2 with NO block_navigation — initial load must succeed.
        # If cleanup didn't disable Fetch domain, this would hang on
        # navigation paused-without-handler.
        r2 = c.fetch(httpserver.url_for("/two"))

    assert "PAGE_TWO_LOADED" in r2, (
        f"second fetch didn't load (Fetch domain leak from #1); html={r2[:300]}"
    )
    assert r2.status_code == 200


def test_block_navigation_cleanup_after_action_triggered_nav_attempt(
    httpserver: HTTPServer,
) -> None:
    """concurrency=1: fetch #1 blocks a JS-redirect. Fetch #2 has no
    block_navigation and CAN navigate normally — cleanup didn't leave
    intercepted state."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body>"
        "<button id='go' onclick='window.location.href=\"/elsewhere\"'>x</button>"
        "</body></html>",
        content_type="text/html",
    )
    httpserver.expect_request("/elsewhere").respond_with_data(
        "<html><body>ELSEWHERE_REACHED</body></html>",
        content_type="text/html",
    )

    with onyxweb.Client(concurrency=1) as c:
        # Fetch #1 with block_navigation — click attempted, blocked.
        c.fetch(
            httpserver.url_for("/"),
            actions=[Click(type="click", selector="#go", wait_after_ms=300)],
            block_navigation=True,
        )
        # Fetch #2 — direct goto to /elsewhere should work normally.
        r2 = c.fetch(httpserver.url_for("/elsewhere"))

    assert "ELSEWHERE_REACHED" in r2, (
        f"normal navigation broken after block_navigation cleanup; html={r2[:300]}"
    )


# ----------------------------------------------------------------------------
# TDD #3: AsyncClient parity — block_navigation works the same via async.
# ----------------------------------------------------------------------------


async def test_async_block_navigation_prevents_js_redirect(
    httpserver: HTTPServer,
) -> None:
    """``await ac.fetch(block_navigation=True)`` keeps the page on its
    original URL despite a JS-driven redirect attempt."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body>"
        "<button id='go' onclick='window.location.href=\"/elsewhere\"'>x</button>"
        "</body></html>",
        content_type="text/html",
    )
    httpserver.expect_request("/elsewhere").respond_with_data(
        "<html><body>ELSEWHERE</body></html>",
        content_type="text/html",
    )

    async with onyxweb.AsyncClient() as ac:
        r = await ac.fetch(
            httpserver.url_for("/"),
            actions=[Click(type="click", selector="#go", wait_after_ms=500)],
            block_navigation=True,
        )

    assert "/elsewhere" not in r.final_url


async def test_async_block_navigation_cleanup(httpserver: HTTPServer) -> None:
    """Cleanup runs on the async path; second fetch unaffected."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body>page-1</body></html>",
        content_type="text/html",
    )
    httpserver.expect_request("/two").respond_with_data(
        "<html><body>PAGE_TWO_ASYNC</body></html>",
        content_type="text/html",
    )

    async with onyxweb.AsyncClient(concurrency=1) as ac:
        await ac.fetch(httpserver.url_for("/"), block_navigation=True)
        r2 = await ac.fetch(httpserver.url_for("/two"))

    assert "PAGE_TWO_ASYNC" in r2


# ----------------------------------------------------------------------------
# Behavioral guarantee surfaced by the Phase 4 adversarial review:
# block_navigation must intercept ONLY the navigation request — every
# subresource (image, stylesheet, script, fetch/XHR) must still reach the
# server. The implementation uses a Document-only Fetch.enable pattern; the
# listener never sees subresource requests at all.
# ----------------------------------------------------------------------------


def test_block_navigation_doesnt_block_subresources(httpserver: HTTPServer) -> None:
    """With ``block_navigation=True``, image / script / fetch / CSS-bg
    subresources MUST still reach the server — only navigation is aborted."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body>"
        "<img id='i' src='/asset.png' />"
        "<script src='/asset.js'></script>"
        "<style>body { background: url('/bg.png'); }</style>"
        "<script>fetch('/api/data', {mode: 'no-cors'}).catch(() => {});</script>"
        "</body></html>",
        content_type="text/html",
    )
    httpserver.expect_request("/asset.png").respond_with_data(b"\x89PNG")
    httpserver.expect_request("/asset.js").respond_with_data("/* js */")
    httpserver.expect_request("/bg.png").respond_with_data(b"\x89PNG")
    httpserver.expect_request("/api/data").respond_with_data('{"ok": true}')

    with onyxweb.Client() as c:
        c.fetch(httpserver.url_for("/"), block_navigation=True, wait_after_ms=400)

    paths = {req.path for req, _ in httpserver.log}
    # All subresources must have hit the server.
    for required in ("/asset.png", "/asset.js", "/bg.png", "/api/data"):
        assert required in paths, (
            f"subresource {required} blocked by block_navigation; got paths={paths}"
        )
