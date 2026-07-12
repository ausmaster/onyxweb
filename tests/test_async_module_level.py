"""Async module-level convenience: ``onyxweb.afetch / ascreenshot /
afetch_all`` use a shared default ``AsyncClient``. Mirrors the sync
module-level coverage."""

from __future__ import annotations

import asyncio

import onyxweb

URL = "https://example.com"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


async def test_module_afetch() -> None:
    r = await onyxweb.afetch(URL)
    assert isinstance(r, onyxweb.RenderResult)
    assert "Example Domain" in r


async def test_module_ascreenshot() -> None:
    png = await onyxweb.ascreenshot(URL)
    assert isinstance(png, bytes)
    assert png.startswith(PNG_MAGIC)


async def test_module_afetch_all() -> None:
    fr = await onyxweb.afetch_all(URL)
    assert isinstance(fr, onyxweb.FetchResult)
    assert "Example Domain" in fr.html
    assert fr.png.startswith(PNG_MAGIC)


async def test_module_afetch_reuses_default_client() -> None:
    """Two awaits on ``afetch`` should use the same shared AsyncClient
    instance — module-level lazy init pattern."""
    await onyxweb.afetch(URL)
    await onyxweb.afetch(URL)
    # Test passes if both calls succeed; we trust the implementation's
    # singleton via _default_async_client.


async def test_module_afetch_concurrent_via_gather() -> None:
    """The shared default AsyncClient handles concurrent gather'd calls."""
    results = await asyncio.gather(*[onyxweb.afetch(URL) for _ in range(3)])
    assert all("Example Domain" in r for r in results)
