"""Async API end-to-end smoke tests — AsyncClient.fetch / screenshot /
fetch_all / batch / aclose, async context manager. Mirrors test_smoke.py for
the sync surface."""

from __future__ import annotations

import onyxweb
import pytest

URL = "https://example.com"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


async def test_async_client_fetch() -> None:
    async with onyxweb.AsyncClient() as ac:
        r = await ac.fetch(URL)
    assert isinstance(r, onyxweb.RenderResult)
    assert "Example Domain" in r
    assert r.dom.title() == "Example Domain"


async def test_async_client_screenshot() -> None:
    async with onyxweb.AsyncClient() as ac:
        png = await ac.screenshot(URL)
    assert isinstance(png, bytes)
    assert png.startswith(PNG_MAGIC)
    assert len(png) > 1000


async def test_async_client_fetch_all() -> None:
    async with onyxweb.AsyncClient() as ac:
        fr = await ac.fetch_all(URL)
    assert isinstance(fr, onyxweb.FetchResult)
    assert isinstance(fr.html, onyxweb.RenderResult)
    assert fr.png.startswith(PNG_MAGIC)
    assert "Example Domain" in fr.html


async def test_async_client_batch_html() -> None:
    async with onyxweb.AsyncClient(concurrency=2) as ac:
        results = await ac.batch([URL, URL], capture="html")
    assert len(results) == 2
    for r in results:
        assert isinstance(r, onyxweb.RenderResult)
        assert "Example Domain" in r


async def test_async_client_batch_png() -> None:
    async with onyxweb.AsyncClient(concurrency=2) as ac:
        results = await ac.batch([URL], capture="png")
    assert len(results) == 1
    assert isinstance(results[0], bytes)
    assert results[0].startswith(PNG_MAGIC)


async def test_async_client_batch_both() -> None:
    async with onyxweb.AsyncClient(concurrency=2) as ac:
        results = await ac.batch([URL], capture="both")
    assert len(results) == 1
    assert isinstance(results[0], onyxweb.FetchResult)


async def test_async_client_context_manager_closes_cleanly() -> None:
    """Using AsyncClient as ctx manager runs aclose() on exit."""
    async with onyxweb.AsyncClient() as ac:
        r = await ac.fetch(URL)
        assert len(r) > 0
    # After aclose, further calls raise RuntimeError.
    with pytest.raises(RuntimeError):
        await ac.fetch(URL)


async def test_async_client_explicit_aclose_idempotent() -> None:
    """aclose() on an already-closed client is a no-op."""
    ac = onyxweb.AsyncClient()
    await ac.aclose()
    await ac.aclose()  # second call must not error


async def test_async_client_update_config_runtime_field() -> None:
    """Runtime-mutable config flows through AsyncClient.update_config."""
    async with onyxweb.AsyncClient() as ac:
        ac.update_config(extra_headers={"X-Test": "value"})
        # Just verify it didn't error and config snapshot reflects it.
        snap = ac.config.snapshot()
        assert snap.network.extra_headers == {"X-Test": "value"}


async def test_async_client_update_config_launch_only_rejected() -> None:
    """Launch-only fields raise ValueError on update_config."""
    async with onyxweb.AsyncClient() as ac:
        with pytest.raises(ValueError, match="launch-only"):
            ac.update_config(concurrency=8)


async def test_async_and_sync_clients_coexist() -> None:
    """A sync Client and an AsyncClient can run side by side."""
    async with onyxweb.AsyncClient() as ac:
        with onyxweb.Client() as c:
            sync_r = c.fetch(URL)
            async_r = await ac.fetch(URL)
    assert "Example Domain" in sync_r
    assert "Example Domain" in async_r
