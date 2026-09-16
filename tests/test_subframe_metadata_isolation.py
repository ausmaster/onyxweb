"""A subframe must not supply the page's response metadata.

`Network.responseReceived`, `requestWillBeSent` and `loadingFailed` all report
`ResourceType::Document` for subframe documents. Without a frame check, an
iframe's status/headers/redirects overwrite the main document's — so a page
served 200 reports its ad frame's 500.
"""

from __future__ import annotations

import onyxweb
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Response

_MAIN = "MAIN_DOCUMENT_CONTENT"


def _serve(httpserver: HTTPServer, iframe_path: str) -> str:
    httpserver.expect_request("/").respond_with_data(
        f"<html><body><h1>{_MAIN}</h1><iframe src='{iframe_path}'></iframe></body></html>",
        content_type="text/html",
        headers={"X-Main": "yes"},
    )
    return httpserver.url_for("/")


def test_iframe_status_does_not_override_page_status(httpserver: HTTPServer) -> None:
    """A 500 inside an iframe leaves the page's own 200 intact."""
    httpserver.expect_request("/broken").respond_with_data("nope", status=500)
    url = _serve(httpserver, "/broken")
    with onyxweb.Client(concurrency=1) as c:
        r = c.fetch(url, wait_after_ms=500)
    assert _MAIN in r
    assert r.status_code == 200
    assert r.metadata.status_code == 200


def test_iframe_headers_do_not_override_page_headers(httpserver: HTTPServer) -> None:
    """Header capture keeps the main document's headers, not the frame's."""
    httpserver.expect_request("/frame").respond_with_response(
        Response("<html><body>f</body></html>", content_type="text/html",
                 headers={"X-Frame-Only": "leaked"})
    )
    url = _serve(httpserver, "/frame")
    with onyxweb.Client(concurrency=1) as c:
        r = c.fetch(url, wait_after_ms=500)
    assert r.headers.get("x-main") == "yes"
    assert "x-frame-only" not in r.headers


def test_iframe_redirect_not_in_page_redirect_chain(httpserver: HTTPServer) -> None:
    """A redirect inside an iframe is not a hop of the page's own navigation."""
    httpserver.expect_request("/hop").respond_with_response(
        Response(status=302, headers={"Location": httpserver.url_for("/land")})
    )
    httpserver.expect_request("/land").respond_with_data(
        "<html><body>landed</body></html>", content_type="text/html"
    )
    url = _serve(httpserver, "/hop")
    with onyxweb.Client(concurrency=1) as c:
        r = c.fetch(url, wait_after_ms=500)
    assert r.metadata.redirect_chain == []


def test_csp_blocked_iframe_does_not_fail_the_page(httpserver: HTTPServer) -> None:
    """A frame blocked by CSP never issues a request; the page still returns."""
    httpserver.expect_request("/inner").respond_with_data(
        "<html><body>INNER</body></html>", content_type="text/html"
    )
    httpserver.expect_request("/").respond_with_data(
        f"<html><body><h1>{_MAIN}</h1><iframe src='/inner'></iframe></body></html>",
        content_type="text/html",
        headers={"Content-Security-Policy": "frame-src 'none'"},
    )
    with onyxweb.Client(concurrency=1) as c:
        r = c.fetch(httpserver.url_for("/"), wait_after_ms=500)
    assert _MAIN in r
