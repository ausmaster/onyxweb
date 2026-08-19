"""``include.shadow_dom`` / ``include.iframes`` — reach past outerHTML's blind spots.

``outerHTML`` never serializes shadow roots (open or closed), so component
internals are missing from ``.dom`` by default. Enabling the knob registers an
init script forcing roots open + serializable, then captures via ``getHTML()``.
Both halves are required: ``serializable`` is what makes ``getHTML`` emit the
subtree, and forcing open is what reaches closed roots at all.
"""

from __future__ import annotations

from collections.abc import Callable

import onyxweb
from pytest_httpserver import HTTPServer

DataUrl = Callable[[bytes], str]

# Markers are assembled at runtime so the literal never appears in the script
# source — otherwise `in html` matches the <script> text, not rendered content.
_OPEN = (
    b"<html><body><div id='host'></div><script>"
    b"document.getElementById('host').attachShadow({mode:'open'})"
    b".innerHTML='<span>'+'OPEN'+'MARKER'+'</span>';"
    b"</script></body></html>"
)
_CLOSED = (
    b"<html><body><div id='host'></div><script>"
    b"document.getElementById('host').attachShadow({mode:'closed'})"
    b".innerHTML='<span>'+'CLOSED'+'MARKER'+'</span>';"
    b"</script></body></html>"
)


def _serve_frames(httpserver: HTTPServer) -> None:
    """A page whose iframe holds text found nowhere in the parent document."""
    httpserver.expect_request("/inner.html").respond_with_data(
        "<html><body><p>IFRAME_INNER</p></body></html>", content_type="text/html"
    )
    httpserver.expect_request("/outer.html").respond_with_data(
        "<html><body><h1>PARENT_HEADING</h1><iframe src='inner.html'></iframe></body></html>",
        content_type="text/html",
    )


def test_open_shadow_content_missing_by_default(data_url: DataUrl) -> None:
    with onyxweb.Client() as c:
        r = c.fetch(data_url(_OPEN))
    assert "OPENMARKER" not in r.dom.html()


def test_open_shadow_content_captured_when_enabled(data_url: DataUrl) -> None:
    with onyxweb.Client(include_shadow_dom=True) as c:
        r = c.fetch(data_url(_OPEN))
    assert "OPENMARKER" in r.dom.html()
    assert r.dom.query_one("span") is not None


def test_closed_shadow_content_captured_when_enabled(data_url: DataUrl) -> None:
    """Closed roots need the forced-open half of the patch."""
    with onyxweb.Client(include_shadow_dom=True) as c:
        r = c.fetch(data_url(_CLOSED))
    assert "CLOSEDMARKER" in r.dom.html()


def test_closed_shadow_content_missing_by_default(data_url: DataUrl) -> None:
    with onyxweb.Client() as c:
        r = c.fetch(data_url(_CLOSED))
    assert "CLOSEDMARKER" not in r.dom.html()


def test_capture_preserves_light_dom_and_doctype(data_url: DataUrl) -> None:
    """The shadow-aware path must not lose ordinary content."""
    page = b"<!DOCTYPE html><html><body><h1>LIGHT_HEADING</h1></body></html>"
    with onyxweb.Client(include_shadow_dom=True) as c:
        r = c.fetch(data_url(page))
    assert r.dom.query_one("h1") is not None
    assert "LIGHT_HEADING" in r.dom.html()
    assert r.dom.html().lower().lstrip().startswith("<!doctype")


def test_config_defaults_off_and_is_settable() -> None:
    cfg = onyxweb.ClientConfig()
    assert cfg.include.shadow_dom is False
    assert cfg.include.iframes is False
    assert onyxweb.ClientConfig.from_flat(include_shadow_dom=True).include.shadow_dom is True
    assert onyxweb.ClientConfig.from_flat(include_iframes=True).include.iframes is True


# --- include.iframes -------------------------------------------------------


def test_same_origin_iframe_content_missing_by_default(httpserver: HTTPServer) -> None:
    _serve_frames(httpserver)
    with onyxweb.Client() as c:
        r = c.fetch(httpserver.url_for("/outer.html"), wait_after_ms=800)
    assert "PARENT_HEADING" in r.dom.html()
    assert "IFRAME_INNER" not in r.dom.html()


def test_same_origin_iframe_content_included_when_enabled(httpserver: HTTPServer) -> None:
    _serve_frames(httpserver)
    with onyxweb.Client(include_iframes=True) as c:
        r = c.fetch(httpserver.url_for("/outer.html"), wait_after_ms=800)
    assert "PARENT_HEADING" in r.dom.html()
    assert "IFRAME_INNER" in r.dom.html()


def test_cross_origin_iframe_is_skipped_not_fatal(httpserver: HTTPServer) -> None:
    """An unreadable frame must be skipped silently, leaving the page captured."""
    httpserver.expect_request("/x.html").respond_with_data(
        "<html><body><h1>PARENT_HEADING</h1>"
        "<iframe src='https://example.com/'></iframe></body></html>",
        content_type="text/html",
    )
    with onyxweb.Client(include_iframes=True) as c:
        r = c.fetch(httpserver.url_for("/x.html"), wait_after_ms=1500)
    assert "PARENT_HEADING" in r.dom.html()


def test_both_includes_together(httpserver: HTTPServer) -> None:
    _serve_frames(httpserver)
    with onyxweb.Client(include_iframes=True, include_shadow_dom=True) as c:
        r = c.fetch(httpserver.url_for("/outer.html"), wait_after_ms=800)
    assert "IFRAME_INNER" in r.dom.html()


async def test_async_client_parity(data_url: DataUrl) -> None:
    async with onyxweb.AsyncClient(include_shadow_dom=True) as ac:
        r = await ac.fetch(data_url(_OPEN))
    assert "OPENMARKER" in r.dom.html()
