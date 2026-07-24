"""Async parity + FetchResult delegation + edge cases for the response
metadata / headers / hashes surface (slices 1-3c).

The async path shares the same ``do_*_inner`` helpers and
``_make_render_result`` as sync, so parity on one comprehensive case plus the
FetchResult / batch / same-doc edges covers the surface.
"""

from __future__ import annotations

import onyxweb
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Response

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


async def test_async_metadata_headers_hashes_parity(httpserver: HTTPServer) -> None:
    resp = Response(
        "<html><body>async</body></html>",
        content_type="text/html",
        headers={"X-Bw": "1"},
    )
    resp.headers.add("Set-Cookie", "sid=xyz; Path=/; HttpOnly")
    httpserver.expect_request("/").respond_with_response(resp)

    async with onyxweb.AsyncClient() as ac:
        r = await ac.fetch(httpserver.url_for("/"))

    assert r.metadata.status_code == 200
    assert r.metadata.mime_type == "text/html"
    assert r.headers["x-bw"] == "1"
    assert r.headers.cookies == {"sid": "xyz"}
    assert r.headers.raw and not r.headers.raw.lower().startswith("http/")
    assert r.headers.hashes is not None
    assert r.metadata.body_hashes.md5
    assert r.metadata.content_length == len(str(r).encode("utf-8"))
    assert r.metadata.request_method == "GET"


def test_fetch_all_result_has_metadata_and_headers(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/").respond_with_data(
        "<html><body>x</body></html>", content_type="text/html", headers={"X-Bw": "fa"}
    )
    with onyxweb.Client() as c:
        fr = c.fetch_all(httpserver.url_for("/"))

    assert isinstance(fr, onyxweb.FetchResult)
    assert fr.metadata.status_code == 200
    assert fr.headers["x-bw"] == "fa"
    assert fr.metadata.content_length == len(str(fr.html).encode("utf-8"))
    assert fr.png[:8] == PNG_MAGIC


async def test_async_fetch_all_delegation(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/").respond_with_data(
        "<html><body>x</body></html>", content_type="text/html", headers={"X-Bw": "afa"}
    )
    async with onyxweb.AsyncClient() as ac:
        fr = await ac.fetch_all(httpserver.url_for("/"))
    assert fr.metadata.status_code == 200
    assert fr.headers["x-bw"] == "afa"


def test_batch_results_carry_metadata_headers(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/").respond_with_data(
        "<html><body>x</body></html>", content_type="text/html", headers={"X-Bw": "b"}
    )
    with onyxweb.Client(concurrency=2) as c:
        results = c.batch(
            [httpserver.url_for("/"), httpserver.url_for("/")], capture="html"
        )
    assert len(results) == 2
    for r in results:
        assert not isinstance(r, (bytes, Exception))
        assert r.metadata.status_code == 200
        assert r.headers["x-bw"] == "b"
        assert r.headers.hashes is not None


def test_same_doc_nav_preserves_metadata(httpserver: HTTPServer) -> None:
    """A same-document nav carries the prior document's metadata + headers
    (no new HTTP response), via the prev-response fallback."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body>x</body></html>", content_type="text/html", headers={"X-Bw": "sd"}
    )
    base = httpserver.url_for("/")
    with onyxweb.Client(concurrency=1) as c:
        r1 = c.fetch(base)
        r2 = c.fetch(base + "#frag")  # same-doc

    assert r2.status_code == 200
    assert r2.metadata.protocol == r1.metadata.protocol
    assert r2.headers.get("x-bw") == "sd"
