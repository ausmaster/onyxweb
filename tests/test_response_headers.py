"""``result.headers`` — a case-insensitive Mapping over the main-document
response headers, plus ``.raw`` / ``.cookies`` / ``.set_cookie`` / ``.hashes``.

Headers (incl. Set-Cookie, which ``Network.responseReceived`` strips) come from
``Network.responseReceivedExtraInfo``, correlated to the main-document response
by ``(request_id, status)`` so a redirect hop can't bind to the final response.

``.raw`` is the canonical ``Name: Value\\r\\nName: Value`` form (no status line,
no pseudo-headers, no trailing CRLF) — matching blacklanternsecurity/blasthttp's
``raw_headers`` so onyxweb's rendered ``HTTP_RESPONSE`` is a drop-in for BBOT.
It's protocol-agnostic: present and identically shaped for HTTP/1.x and h2/h3.
"""

from __future__ import annotations

import onyxweb
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Response


def test_headers_case_insensitive_mapping(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/").respond_with_data(
        "<html><body>hi</body></html>",
        content_type="text/html",
        headers={"X-Custom-Header": "onyxweb", "Server": "test-srv"},
    )
    with onyxweb.Client() as c:
        r = c.fetch(httpserver.url_for("/"))

    assert r.headers["content-type"].startswith("text/html")
    assert r.headers["Content-Type"].startswith("text/html")
    assert r.headers["x-custom-header"] == "onyxweb"
    assert "X-Custom-Header" in r.headers
    assert "server" in r.headers
    assert r.headers.get("nonexistent-header") is None
    assert r.headers.get("x-custom-header", "d") == "onyxweb"
    assert isinstance(dict(r.headers), dict)


def test_set_cookie_captured_and_split(httpserver: HTTPServer) -> None:
    """Set-Cookie (stripped by responseReceived) is present via extraInfo, and
    multiple cookies split into a list (CDP joins them with ``\\n``)."""
    resp = Response("<html><body>ok</body></html>", content_type="text/html")
    resp.headers.add("Set-Cookie", "sid=abc; Path=/; HttpOnly")
    resp.headers.add("Set-Cookie", "theme=dark; Path=/")
    httpserver.expect_request("/").respond_with_response(resp)

    with onyxweb.Client() as c:
        r = c.fetch(httpserver.url_for("/"))

    sc = r.headers.set_cookie
    assert isinstance(sc, list)
    assert any("sid=abc" in cookie for cookie in sc)
    assert any("theme=dark" in cookie for cookie in sc)


def test_cookies_parsed_name_value(httpserver: HTTPServer) -> None:
    """``.cookies`` parses Set-Cookie into name->value (attributes stripped,
    last-wins) — matching blasthttp's ``cookies``."""
    resp = Response("<html><body>ok</body></html>", content_type="text/html")
    resp.headers.add("Set-Cookie", "sid=abc123; Path=/; HttpOnly; Secure")
    resp.headers.add("Set-Cookie", "theme=dark; Path=/; Max-Age=3600")
    httpserver.expect_request("/").respond_with_response(resp)

    with onyxweb.Client() as c:
        r = c.fetch(httpserver.url_for("/"))

    assert r.headers.cookies == {"sid": "abc123", "theme": "dark"}


def test_raw_headers_canonical_form(httpserver: HTTPServer) -> None:
    """``.raw`` is the ``Name: Value\\r\\nName: Value`` form — NO status line,
    no pseudo-headers, no trailing CRLF."""
    httpserver.expect_request("/").respond_with_data(
        "x", content_type="text/html", headers={"X-Marker": "present"}
    )
    with onyxweb.Client() as c:
        r = c.fetch(httpserver.url_for("/"))

    raw = r.headers.raw
    assert raw  # always present
    assert not raw.lower().startswith("http/")  # NO status line
    assert not raw.endswith("\r\n")  # no trailing CRLF
    assert "content-type: text/html" in raw.lower()
    assert "x-marker: present" in raw.lower()
    # Every line is a "name: value" header line.
    for line in raw.split("\r\n"):
        assert ": " in line, f"non-header line in raw: {line!r}"


def test_raw_and_hashes_present_for_h2_style_response() -> None:
    """A data: URL (no wire header text) still yields a canonical ``.raw`` and
    header ``.hashes`` — the canonical form is protocol-agnostic, never None."""
    with onyxweb.Client() as c:
        r = c.fetch("data:text/html,<html><body>x</body></html>")
    assert r.headers.raw is not None
    assert r.headers.hashes is not None


def test_redirect_hop_headers_do_not_bind_to_final(httpserver: HTTPServer) -> None:
    """Redirects reuse the CDP request_id. The intermediate hop's headers must
    NOT attach to the final response. Regression for the (request_id, status)
    correlation.
    """
    httpserver.expect_request("/hop").respond_with_response(
        Response(
            status=302,
            headers={"Location": httpserver.url_for("/final"), "X-Hop": "redirect"},
        )
    )
    httpserver.expect_request("/final").respond_with_data(
        "<html><body>final</body></html>",
        content_type="text/html",
        headers={"X-Hop": "final"},
    )

    with onyxweb.Client() as c:
        r = c.fetch(httpserver.url_for("/hop"))

    assert r.status_code == 200
    assert r.final_url.endswith("/final")
    assert r.headers["x-hop"] == "final"
    assert "location" not in r.headers  # the 302's Location must not leak in
    assert "x-hop: final" in r.headers.raw.lower()
    assert "x-hop: redirect" not in r.headers.raw.lower()
