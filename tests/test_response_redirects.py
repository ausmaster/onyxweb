"""``metadata.redirect_chain`` / ``request_url`` / ``request_method`` — the
redirect hops taken to reach the final response, plus the original request.

Mirrors blasthttp's ``redirect_chain`` (list of hops with url/status/ip) and
``request_url`` / ``request_method``.
"""

from __future__ import annotations

import onyxweb
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Response


def test_redirect_chain_captured(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/a").respond_with_response(
        Response(status=302, headers={"Location": httpserver.url_for("/b")})
    )
    httpserver.expect_request("/b").respond_with_response(
        Response(status=301, headers={"Location": httpserver.url_for("/c")})
    )
    httpserver.expect_request("/c").respond_with_data(
        "<html><body>done</body></html>", content_type="text/html"
    )

    with onyxweb.Client() as c:
        r = c.fetch(httpserver.url_for("/a"))

    m = r.metadata
    assert m.status_code == 200
    assert m.final_url.endswith("/c")
    assert m.request_url.endswith("/a")
    assert m.request_method == "GET"

    chain = m.redirect_chain
    assert len(chain) == 2
    assert chain[0].url.endswith("/a") and chain[0].status == 302
    assert chain[1].url.endswith("/b") and chain[1].status == 301
    assert all(h.remote_ip in ("127.0.0.1", "::1") for h in chain)


def test_no_redirect_empty_chain(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/").respond_with_data(
        "<html><body>x</body></html>", content_type="text/html"
    )
    with onyxweb.Client() as c:
        r = c.fetch(httpserver.url_for("/"))
    assert r.metadata.redirect_chain == []
    assert r.metadata.request_url == httpserver.url_for("/")
    assert r.metadata.final_url == httpserver.url_for("/")


def test_redirect_chain_no_leak_between_fetches(httpserver: HTTPServer) -> None:
    """concurrency=1 forces tab reuse — the chain must reset each fetch."""
    httpserver.expect_request("/a").respond_with_response(
        Response(status=302, headers={"Location": httpserver.url_for("/c")})
    )
    httpserver.expect_request("/c").respond_with_data(
        "<html><body>done</body></html>", content_type="text/html"
    )
    httpserver.expect_request("/plain").respond_with_data(
        "<html><body>plain</body></html>", content_type="text/html"
    )
    with onyxweb.Client(concurrency=1) as c:
        r1 = c.fetch(httpserver.url_for("/a"))
        r2 = c.fetch(httpserver.url_for("/plain"))
    assert len(r1.metadata.redirect_chain) == 1
    assert r2.metadata.redirect_chain == []
