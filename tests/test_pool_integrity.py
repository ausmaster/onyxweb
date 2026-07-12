"""Pool integrity soak — N sequential fetches with mixed per-call config,
then one control fetch must be clean of any prior fetch's residue.

Guards the cleanup discipline (script removal + block_urls restoration)
against future regressions. Forward-looking by design: this test should
pass today and continue passing as Phase 3+ adds more per-call mutations
on top of the same cleanup phase.
"""

from __future__ import annotations

import onyxweb
from pytest_httpserver import HTTPServer


def test_pool_integrity_mixed_config_soak(httpserver: HTTPServer) -> None:
    """30 mixed-config fetches on a 1-tab pool, then 1 control fetch must
    show NO residue: no leftover scripts, no leftover URL blocks."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body><script>"
        "fetch('/track', {mode: 'no-cors'}).catch(() => {})"
        "</script></body></html>",
        content_type="text/html",
    )
    httpserver.expect_request("/track").respond_with_data("hit")
    page = httpserver.url_for("/")
    track_url = httpserver.url_for("/track")

    # concurrency=1 forces every fetch onto the same pooled tab — maximum
    # pressure on the cleanup phase. ``wait_after_ms`` lets the page's
    # async fetch() complete (or get blocked) within the same call.
    with onyxweb.Client(concurrency=1) as c:
        for i in range(30):
            mode = i % 4
            if mode == 0:
                # no per-call config
                c.fetch(page, wait_after_ms=150)
            elif mode == 1:
                # per-call script only
                c.fetch(page, scripts=[f"console.error('SOAK_S_{i}')"], wait_after_ms=150)
            elif mode == 2:
                # per-call block_urls only
                c.fetch(page, block_urls=[track_url], wait_after_ms=150)
            else:
                # both
                c.fetch(
                    page,
                    scripts=[f"console.error('SOAK_B_{i}')"],
                    block_urls=[track_url],
                    wait_after_ms=150,
                )

        # Reset server log; the assertion below only counts the final fetch.
        httpserver.clear_log()
        # Control fetch — no per-call config. Cleanup of all prior fetches
        # must have left this tab clean.
        r_final = c.fetch(page, wait_after_ms=300)

    # No leftover scripts firing on this fetch.
    final_texts = " ".join(m.text for m in r_final.console_messages)
    assert "SOAK_S_" not in final_texts, f"script residue: {final_texts}"
    assert "SOAK_B_" not in final_texts, f"script residue: {final_texts}"

    # /track must be reachable — no leftover block_urls.
    track_hits = [req for req, _ in httpserver.log if req.path == "/track"]
    assert len(track_hits) >= 1, (
        f"block_urls residue prevented /track in control fetch: hits={len(track_hits)}"
    )


def test_pool_integrity_with_same_doc_navs(httpserver: HTTPServer) -> None:
    """Mix full and same-document navs on a 1-tab pool. Each nav must
    succeed; same-doc navs must propagate the prior document's status."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body>x</body></html>", content_type="text/html"
    )
    base = httpserver.url_for("/")

    with onyxweb.Client(concurrency=1) as c:
        r1 = c.fetch(base)
        r2 = c.fetch(base + "#a")  # same-doc
        r3 = c.fetch(base + "#b")  # same-doc again
        r4 = c.fetch(base + "?q=1")  # full nav (different query)
        r5 = c.fetch(base + "?q=1#c")  # same-doc from r4
        r6 = c.fetch(base)  # full nav (path again)

    for r, label in [(r1, "r1"), (r2, "r2"), (r3, "r3"), (r4, "r4"), (r5, "r5"), (r6, "r6")]:
        assert r.status_code == 200, f"{label}: status={r.status_code}"
