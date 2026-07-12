"""Same-document navigation lifecycle: hash-only / query+hash navs that
chromium treats as same-document don't fire ``Page.loadEventFired`` or
``Page.domContentEventFired``, AND chromiumoxide's `Page.navigate`
command-future hangs on the response (verified via raw CDP probe —
chromium itself responds in <1ms; chromiumoxide drops the response on
the long-lived pool session).

onyxweb tracks the pool tab's current URL via long-lived listeners on
``Page.frameNavigated`` and ``Page.navigatedWithinDocument``. When a
fetch's URL is same-doc relative to current, the engine takes a
different code path:

  - No per-call init scripts → ``Runtime.evaluate("location.href = ...")``.
    Goes through a separate CDP command channel, doesn't hang.
  - Per-call init scripts → cache-buster query forces a full nav so
    chromium's ``addScriptToEvaluateOnNewDocument`` machinery fires.

``concurrency=1`` forces every fetch onto the same pool tab so the
optimization is exercised on every test.
"""

from __future__ import annotations

import time

import onyxweb
import pytest
from pytest_httpserver import HTTPServer


def _serve_simple(httpserver: HTTPServer) -> str:
    httpserver.expect_request("/").respond_with_data(
        "<html><body><h1>same-doc test</h1></body></html>",
        content_type="text/html",
    )
    return httpserver.url_for("/")


def test_hash_only_nav_completes_without_timeout(httpserver: HTTPServer) -> None:
    base = _serve_simple(httpserver)
    with onyxweb.Client(concurrency=1) as c:
        t0 = time.perf_counter()
        r1 = c.fetch(base)
        r2 = c.fetch(base + "#abc")
        r3 = c.fetch(base + "#xyz")
        elapsed = time.perf_counter() - t0
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r3.status_code == 200
    # Three fetches on a shared tab should be fast; ≥10s would only happen
    # if a same-doc nav timed out (default nav timeout 30s).
    assert elapsed < 8.0, f"3 hash fetches took {elapsed:.1f}s — same-doc nav likely timed out"


def test_query_only_nav_then_hash_change(httpserver: HTTPServer) -> None:
    base = _serve_simple(httpserver)
    with onyxweb.Client(concurrency=1) as c:
        r1 = c.fetch(base + "?q=1")
        r2 = c.fetch(base + "?q=1#abc")  # same-doc from r1
    assert r1.status_code == 200
    assert r2.status_code == 200


@pytest.mark.asyncio
async def test_async_client_parity(httpserver: HTTPServer) -> None:
    base = _serve_simple(httpserver)
    async with onyxweb.AsyncClient(concurrency=1) as ac:
        r1 = await ac.fetch(base)
        r2 = await ac.fetch(base + "#a")
        r3 = await ac.fetch(base + "#b")
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r3.status_code == 200


def test_dcl_mode_same_doc_nav(httpserver: HTTPServer) -> None:
    """``wait_until='domcontentloaded'`` mode also handles same-doc navs."""
    base = _serve_simple(httpserver)
    with onyxweb.Client(concurrency=1, wait_until="domcontentloaded") as c:
        r1 = c.fetch(base)
        r2 = c.fetch(base + "#abc")
    assert r1.status_code == 200
    assert r2.status_code == 200


def test_same_doc_with_init_scripts_forces_full_nav(httpserver: HTTPServer) -> None:
    """Per-call init scripts only fire on new-document navs in chromium.
    When a fetch URL would be same-doc but per_call.scripts is non-empty,
    onyxweb appends a cache-buster to force a full nav so the init
    scripts run as the user expects.
    """
    httpserver.expect_request("/").respond_with_data(
        "<html><body><div id='out'></div>"
        "<script>document.getElementById('out').textContent = "
        "(window.__hooked === true ? 'hooked' : 'not_hooked');</script>"
        "</body></html>",
        content_type="text/html",
    )
    base = httpserver.url_for("/")
    init_script = "window.__hooked = true;"

    with onyxweb.Client(concurrency=1) as c:
        # Fetch 1: full nav, no init script.
        r1 = c.fetch(base)
        assert r1.status_code == 200
        assert "not_hooked" in r1, "sanity: page sees __hooked=undefined"

        # Fetch 2: would be same-doc (`base#x` vs `base`) but with an init
        # script. onyxweb must force a full nav so the init_script fires.
        r2 = c.fetch(base + "#x", scripts=[init_script])
        assert r2.status_code == 200
        assert "hooked" in r2, (
            f"init_script didn't run on same-doc + init_scripts case; html: {r2[:300]!r}"
        )
        # final_url contains the cache-buster, marking the full-nav route.
        assert "__onyxweb_t=" in r2.final_url, (
            f"expected cache-buster in final_url: {r2.final_url!r}"
        )


def test_same_doc_without_init_scripts_uses_evaluate_path(httpserver: HTTPServer) -> None:
    """The complementary case: no per-call init scripts, onyxweb takes the
    fast evaluate-based same-doc path; no cache-buster appears."""
    base = _serve_simple(httpserver)
    with onyxweb.Client(concurrency=1) as c:
        c.fetch(base)
        r2 = c.fetch(base + "#abc")
    assert r2.status_code == 200
    assert "__onyxweb_t" not in r2.final_url, (
        f"unexpected cache-buster in final_url: {r2.final_url!r}"
    )
    assert r2.final_url.endswith("#abc")
