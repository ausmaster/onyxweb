"""``result.metadata`` — structured response metadata alongside the existing
``status_code`` / ``final_url`` / ``elapsed_s``.

Feeds BBOT's ``HTTP_RESPONSE`` event (status_text, mime_type, protocol,
remote_ip/port, content_length). All fields come off the
``Network.responseReceived`` event onyxweb already subscribes to.

pytest-httpserver serves HTTP/1.1 so protocol / remote_ip assertions are
deterministic.
"""

from __future__ import annotations

import onyxweb
from pytest_httpserver import HTTPServer


def test_metadata_core_fields(httpserver: HTTPServer) -> None:
    body = "<html><body>hello metadata</body></html>"
    httpserver.expect_request("/").respond_with_data(body, content_type="text/html")
    with onyxweb.Client() as c:
        r = c.fetch(httpserver.url_for("/"))

    m = r.metadata
    assert m.status_code == 200
    assert m.status_text == "OK"
    assert m.mime_type == "text/html"
    assert m.protocol in ("http/1.1", "http/1.0")
    assert m.remote_ip in ("127.0.0.1", "::1")
    assert isinstance(m.remote_port, int) and m.remote_port > 0
    assert m.final_url == httpserver.url_for("/")
    assert m.elapsed_s > 0


def test_metadata_content_length_matches_rendered_body(httpserver: HTTPServer) -> None:
    """``content_length`` is the byte length of the rendered (post-JS) body —
    the same bytes BBOT hashes — not the HTTP Content-Length header."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body><script>document.body.innerHTML += '<p>added</p>'</script></body></html>",
        content_type="text/html",
    )
    with onyxweb.Client() as c:
        r = c.fetch(httpserver.url_for("/"), wait_after_ms=100)

    assert r.metadata.content_length == len(str(r).encode("utf-8"))
    assert "added" in r  # sanity: rendered body includes the JS-added node


def test_metadata_status_code_mirrors_top_level(httpserver: HTTPServer) -> None:
    """``metadata.status_code`` and the backward-compat ``result.status_code``
    agree."""
    httpserver.expect_request("/").respond_with_data("x", content_type="text/html")
    with onyxweb.Client() as c:
        r = c.fetch(httpserver.url_for("/"))
    assert r.metadata.status_code == r.status_code == 200
