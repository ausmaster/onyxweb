"""C2 response metadata — a server response maps to the page's status, headers and hashes.

``ROWS`` serves a small site per row and names the metadata the fetch must report.
Every row also checks the invariants that hold for any response: the hashes match
Python's ``hashlib`` / ``mmh3`` over the same bytes, ``content_length`` is the
rendered body's UTF-8 length, the raw header block has BBOT's canonical shape, and
only the main frame speaks for the page. These feed BBOT's ``HTTP_RESPONSE``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlparse

import mmh3
import onyxweb
import pytest
from conftest import reloaded
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Response

LOOPBACK = ("127.0.0.1", "::1")


@dataclass(frozen=True)
class Page:
    """One response a row's site serves."""

    status: int = 200
    body: str = "<html><body>ok</body></html>"
    headers: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Row:
    """A site (path → page; the fetch starts at the first) and what the fetch reports."""

    site: dict[str, Page]
    status: int = 200
    status_text: str = "OK"
    final: str | None = None  # final path; the start path when None
    chain: tuple[tuple[str, int], ...] = ()  # (path, status) of each redirect hop
    present: dict[str, str] = field(default_factory=dict)  # header → value, any case
    absent: tuple[str, ...] = ()  # headers the page must not report
    cookies: dict[str, str] = field(default_factory=dict)
    shows: str | None = None  # text the rendered page contains
    wait_after_ms: int = 0


_FRAME = "<iframe src='{}'></iframe>"
ROWS: dict[str, Row] = {
    "plain": Row(
        # Non-ASCII, so hashes and content_length must count UTF-8 bytes.
        {
            "/": Page(
                body="<html><body>hello — éèê</body></html>",
                headers=(("X-Custom-Header", "onyxweb"), ("Server", "test-srv")),
            )
        },
        present={"X-Custom-Header": "onyxweb", "Server": "test-srv"},
        absent=("Nonexistent-Header",),
        shows="hello — éèê",
    ),
    # content_length and hashes cover the rendered body, not the bytes served.
    "js_changed_body": Row(
        {
            "/": Page(
                body="<html><body><script>document.body.innerHTML += '<p>added</p>'"
                "</script></body></html>"
            )
        },
        shows="<p>added</p>",
        wait_after_ms=100,
    ),
    "not_found": Row({"/missing": Page(status=404)}, status=404, status_text="NOT FOUND"),
    # Set-Cookie arrives only via extraInfo; CDP joins several with "\n".
    "cookies": Row(
        {
            "/": Page(
                headers=(
                    ("Set-Cookie", "sid=abc123; Path=/; HttpOnly; Secure"),
                    ("Set-Cookie", "theme=dark; Path=/; Max-Age=3600"),
                )
            )
        },
        cookies={"sid": "abc123", "theme": "dark"},
    ),
    "redirect_chain": Row(
        {
            "/a": Page(status=302, headers=(("Location", "/b"),)),
            "/b": Page(status=301, headers=(("Location", "/c"),)),
            "/c": Page(body="<html><body>done</body></html>"),
        },
        final="/c",
        chain=(("/a", 302), ("/b", 301)),
        shows="done",
    ),
    # Hops share a CDP request id; a hop's headers must not bind to the final response.
    "redirect_hop_headers": Row(
        {
            "/hop": Page(status=302, headers=(("Location", "/final"), ("X-Hop", "redirect"))),
            "/final": Page(headers=(("X-Hop", "final"),)),
        },
        final="/final",
        chain=(("/hop", 302),),
        present={"X-Hop": "final"},
        absent=("Location",),
    ),
    # Subframe documents report ResourceType::Document too; none may speak for the page.
    "iframes_cannot_speak_for_the_page": Row(
        {
            "/": Page(
                body="<html><body><h1>MAIN</h1>"
                + _FRAME.format("/broken")
                + _FRAME.format("/frame")
                + _FRAME.format("/frame-hop")
                + "</body></html>",
                headers=(("X-Main", "yes"),),
            ),
            "/broken": Page(status=500, body="nope"),
            "/frame": Page(headers=(("X-Frame-Only", "leaked"),)),
            "/frame-hop": Page(status=302, headers=(("Location", "/land"),)),
            "/land": Page(body="<html><body>landed</body></html>"),
        },
        present={"X-Main": "yes"},
        absent=("X-Frame-Only",),
        shows="MAIN",
        wait_after_ms=500,
    ),
}


@pytest.fixture(scope="module")
def client() -> Iterator[onyxweb.Client]:
    with onyxweb.Client(concurrency=1) as c:
        yield c


def _check_invariants(r: onyxweb.RenderResult) -> None:
    """What holds for any response, served locally or not."""
    m = r.metadata
    body = r.html.encode()
    assert m.status_code == r.status_code
    assert m.content_length == len(body)
    assert (m.body_hashes.md5, m.body_hashes.sha256, m.body_hashes.mmh3) == (
        hashlib.md5(body).hexdigest(),
        hashlib.sha256(body).hexdigest(),
        mmh3.hash(body),  # signed 32-bit, as BBOT computes it
    )
    raw = r.headers.raw
    assert not raw.lower().startswith("http/"), "no status line"
    assert not raw.endswith("\r\n"), "no trailing CRLF"
    assert all(": " in line for line in (raw.split("\r\n") if raw else []))
    raw_bytes = raw.encode()
    assert (r.headers.hashes.md5, r.headers.hashes.sha256, r.headers.hashes.mmh3) == (
        hashlib.md5(raw_bytes).hexdigest(),
        hashlib.sha256(raw_bytes).hexdigest(),
        mmh3.hash(raw_bytes),
    )
    # A saved and loaded result reports the same response: redirects, cert, cookies too.
    again = reloaded(r)
    assert again.metadata == m
    assert (again.headers.raw, again.headers.hashes) == (r.headers.raw, r.headers.hashes)
    assert dict(again.headers) == dict(r.headers)
    assert again.headers.set_cookie == r.headers.set_cookie


@pytest.mark.parametrize("name", list(ROWS))
def test_response(client: onyxweb.Client, httpserver: HTTPServer, name: str) -> None:
    """Each local site maps to the metadata the fetch reports."""
    row = ROWS[name]
    for path, page in row.site.items():
        httpserver.expect_request(path).respond_with_response(
            Response(
                page.body,
                status=page.status,
                headers=list(page.headers),
                content_type="text/html; charset=utf-8",  # else Chrome decodes as Windows-1252
            )
        )
    start = next(iter(row.site))
    r = client.fetch(httpserver.url_for(start), wait_after_ms=row.wait_after_ms)
    m = r.metadata
    assert (r.status_code, m.status_text) == (row.status, row.status_text)
    assert m.request_url == httpserver.url_for(start)
    assert m.final_url == r.final_url == httpserver.url_for(row.final or start)
    assert [(urlparse(hop.url).path, hop.status) for hop in m.redirect_chain] == list(row.chain)
    for header, value in row.present.items():
        assert r.headers[header] == r.headers[header.lower()] == value
        assert f"{header.lower()}: {value}" in r.headers.raw.lower()
    for header in row.absent:
        assert header not in r.headers
        assert r.headers.get(header) is None
        assert f"{header.lower()}: " not in r.headers.raw.lower()
    assert r.headers.cookies == row.cookies
    for cookie, value in row.cookies.items():
        assert any(f"{cookie}={value}" in line for line in r.headers.set_cookie)
    if row.shows is not None:
        assert row.shows in r
    # Facts every local HTTP/1.1 response shares.
    assert r.headers["Content-Type"].startswith("text/html")
    assert m.mime_type == "text/html"
    assert m.protocol in ("http/1.1", "http/1.0")
    assert m.remote_ip in LOOPBACK
    assert m.remote_port is not None and m.remote_port > 0
    assert all(hop.remote_ip in LOOPBACK for hop in m.redirect_chain)
    assert m.cert_info is None
    assert m.elapsed_s > 0
    _check_invariants(r)


def test_data_url_response(client: onyxweb.Client) -> None:
    """A ``data:`` URL has no server: no certificate, but the invariants still hold."""
    r = client.fetch("data:text/html,<html><body>x</body></html>")
    assert r.metadata.cert_info is None
    _check_invariants(r)


def test_https_response_carries_its_certificate(client: onyxweb.Client) -> None:
    """``cert_info`` comes from CDP ``securityDetails`` — real network, example.com."""
    r = client.fetch("https://example.com/")
    ci = r.metadata.cert_info
    assert ci is not None
    assert any("example" in san.lower() for san in ci.sans)
    assert ci.common_name and ci.issuer
    assert datetime.fromisoformat(ci.not_before) < datetime.fromisoformat(ci.not_after)
    assert ci.emails == [san for san in ci.sans if "@" in san]
    assert ci.fingerprint_sha256 is None  # securityDetails doesn't expose one
    _check_invariants(r)
