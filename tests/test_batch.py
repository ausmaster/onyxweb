"""Client.batch() — parallel inside Rust, returns when all complete."""

from __future__ import annotations

import onyxweb
import pytest

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
URLS = [
    "https://example.com",
    "https://example.org",
    "https://example.net",
]


class TestBatchHtml:
    def test_returns_render_results(self) -> None:
        with onyxweb.Client(concurrency=4) as c:
            results = c.batch(URLS, capture="html")
        assert len(results) == len(URLS)
        for r in results:
            assert isinstance(r, onyxweb.RenderResult)
            assert len(r) > 0


class TestBatchPng:
    def test_returns_bytes(self) -> None:
        with onyxweb.Client(concurrency=4) as c:
            results = c.batch(URLS, capture="png")
        assert len(results) == len(URLS)
        for png in results:
            assert isinstance(png, bytes)
            assert png.startswith(PNG_MAGIC)


class TestBatchBoth:
    def test_returns_fetch_results(self) -> None:
        with onyxweb.Client(concurrency=4) as c:
            results = c.batch(URLS, capture="both")
        assert len(results) == len(URLS)
        for fr in results:
            assert isinstance(fr, onyxweb.FetchResult)
            assert isinstance(fr.html, onyxweb.RenderResult)
            assert fr.png.startswith(PNG_MAGIC)


class TestBatchEmpty:
    def test_empty_urls_returns_empty_list(self) -> None:
        with onyxweb.Client() as c:
            results = c.batch([], capture="html")
        assert results == []


class TestBatchInvalidCapture:
    def test_bad_capture_mode_raises(self) -> None:
        with onyxweb.Client() as c, pytest.raises(ValueError, match="capture must"):
            c.batch(["https://example.com"], capture="invalid")  # type: ignore[arg-type]


class TestBatchAcceptsIterables:
    """batch() should accept any iterable of URLs, not only lists."""

    def test_generator_works(self) -> None:
        from collections.abc import Iterator

        def gen() -> Iterator[str]:
            yield from URLS
        with onyxweb.Client() as c:
            results = c.batch(gen(), capture="html")
        assert len(results) == len(URLS)

    def test_tuple_works(self) -> None:
        with onyxweb.Client() as c:
            results = c.batch(tuple(URLS), capture="html")
        assert len(results) == len(URLS)
