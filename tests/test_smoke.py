"""Basic end-to-end smoke tests — fetch, screenshot, fetch_all, the module
convenience wrappers. If any of these fail, the engine is broken."""

from __future__ import annotations

import onyxweb
import pytest

URL = "https://example.com"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def test_module_fetch() -> None:
    r = onyxweb.fetch(URL)
    assert isinstance(r, onyxweb.RenderResult)
    assert "Example Domain" in r


def test_module_screenshot() -> None:
    png = onyxweb.screenshot(URL)
    assert isinstance(png, bytes)
    assert png.startswith(PNG_MAGIC)
    assert len(png) > 1000  # tiny sanity: real image, not just header


def test_module_fetch_all() -> None:
    fr = onyxweb.fetch_all(URL)
    assert isinstance(fr, onyxweb.FetchResult)
    assert isinstance(fr.html, onyxweb.RenderResult)
    assert fr.png.startswith(PNG_MAGIC)
    assert "Example Domain" in fr.html
    assert fr.html.dom.title() == "Example Domain"


def test_client_context_manager_closes_cleanly() -> None:
    """Using Client as context manager runs close() on exit — no dangling chrome."""
    with onyxweb.Client() as c:
        r = c.fetch(URL)
        assert len(r) > 0
    # After close(), further calls raise RuntimeError
    with pytest.raises(RuntimeError):
        c.fetch(URL)


def test_multiple_clients_coexist() -> None:
    """Two Clients run simultaneously — each owns its own chromium process."""
    with onyxweb.Client() as a, onyxweb.Client() as b:
        ra = a.fetch(URL)
        rb = b.fetch(URL)
    assert len(ra) > 0 and len(rb) > 0
