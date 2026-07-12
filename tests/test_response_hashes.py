"""Rust-computed body + header hashes, byte-exact with Python's ``mmh3`` /
``hashlib`` so BBOT's ``HTTP_RESPONSE`` hashes line up across tools.

``result.metadata.body_hashes`` — md5 / mmh3 / sha256 over the rendered body.
``result.headers.hashes`` — the same over the canonical ``.raw`` header block
(``Name: Value\\r\\n…``), present on every protocol.

``mmh3`` is a dev-only dependency used purely as the parity oracle here.
"""

from __future__ import annotations

import hashlib

import onyxweb
import mmh3
from pytest_httpserver import HTTPServer


def test_body_hashes_match_python(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/").respond_with_data(
        "<html><body>hash me — éèê</body></html>", content_type="text/html"
    )
    with onyxweb.Client() as c:
        r = c.fetch(httpserver.url_for("/"))

    body = str(r).encode("utf-8")
    h = r.metadata.body_hashes
    assert h.md5 == hashlib.md5(body).hexdigest()
    assert h.sha256 == hashlib.sha256(body).hexdigest()
    assert h.mmh3 == mmh3.hash(body)  # signed 32-bit, matches BBOT


def test_body_hashes_content_length_consistent(httpserver: HTTPServer) -> None:
    """The hashed body and ``content_length`` are the same bytes as ``str(r)``."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body>abc</body></html>", content_type="text/html"
    )
    with onyxweb.Client() as c:
        r = c.fetch(httpserver.url_for("/"))
    body = str(r).encode("utf-8")
    assert r.metadata.content_length == len(body)
    assert r.metadata.body_hashes.sha256 == hashlib.sha256(body).hexdigest()


def test_header_hashes_over_canonical_raw(httpserver: HTTPServer) -> None:
    """Header hashes are over the canonical ``.raw`` (Name: Value) string —
    byte-exact with hashing ``.raw`` yourself. Matches blasthttp's header_*."""
    httpserver.expect_request("/").respond_with_data(
        "x", content_type="text/html", headers={"X-Marker": "present"}
    )
    with onyxweb.Client() as c:
        r = c.fetch(httpserver.url_for("/"))

    raw = r.headers.raw
    assert raw is not None
    raw_bytes = raw.encode("utf-8")
    h = r.headers.hashes
    assert h is not None
    assert h.md5 == hashlib.md5(raw_bytes).hexdigest()
    assert h.sha256 == hashlib.sha256(raw_bytes).hexdigest()
    assert h.mmh3 == mmh3.hash(raw_bytes)


def test_header_hashes_always_present() -> None:
    """The canonical raw form exists on every protocol, so header hashes are
    never None (unlike the dropped verbatim-only approach)."""
    with onyxweb.Client() as c:
        r = c.fetch("data:text/html,<html><body>x</body></html>")
    h = r.headers.hashes
    assert h is not None
    assert h.mmh3 == mmh3.hash(r.headers.raw.encode("utf-8"))
