"""Test doubles for code that uses onyxweb, so it can be tested without Chrome.

``FakeClient`` stands in for `AsyncClient`: it serves canned pages, records what it was asked
for, and can be told to fail, close or die. It offers the calls a caller needs (``fetch``,
``screenshot``, ``fetch_all``, ``batch``, ``aclose``, ``alive``), each with the real client's
signature. Import it by full module path::

    from onyxweb.testing import FakeClient, FakeClientFactory

    fake = FakeClient({"https://example.com/": "<h1>Example</h1>"})
    page = await fake.fetch("https://example.com/")   # a RenderResult, no browser involved
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Final, Literal, Self, cast

from onyxweb import (
    ChromeExitedError,
    FetchResult,
    OnyxwebError,
    RenderResult,
    _merge_fetch_config,
    _merge_screenshot_config,
)
from onyxweb._onyxweb import _FetchOutput
from onyxweb.config import FetchConfig, ScreenshotConfig

# Each format's opening bytes and a tail: enough for code that sniffs an image, not a real picture.
_IMAGES: Final = {
    "png": b"\x89PNG\r\n\x1a\n" + b"fake-image",
    "jpeg": b"\xff\xd8\xff\xe0" + b"fake-image",
    "webp": b"RIFF\x0c\x00\x00\x00WEBP" + b"fake-image",
}


class FakeClient:
    """An `AsyncClient` that serves canned pages and never launches Chrome.

    Attributes:
        pages: URL -> the page to serve, as html or a ready `RenderResult`. A URL not listed
            serves a small page whose html is the URL itself.
        error: When set, every call raises it (a `batch` returns it in place); clear it to let
            calls succeed again.
        closed: True once `aclose` was called.
        fetched: Every call as ``(url, overrides)``, in order: the overrides the caller set,
            leaving out what it left at the default.
    """

    def __init__(
        self,
        pages: Mapping[str, str | RenderResult] | None = None,
        *,
        error: BaseException | None = None,
    ) -> None:
        self.pages = dict(pages or {})
        self.error = error
        self.closed = False
        self.fetched: list[tuple[str, dict[str, Any]]] = []
        self._alive = True

    @property
    def alive(self) -> bool:
        """False once the client is closed or `die` was called."""
        return self._alive and not self.closed

    def die(self) -> None:
        """Make Chrome "exit": `alive` turns False and every call raises `ChromeExitedError`."""
        self._alive = False

    def _check_open(self) -> None:
        if self.closed:
            raise OnyxwebError("internal: Client is closed")

    def _serve(self, url: str) -> RenderResult:
        """The page for `url`, or what a real client raises in the fake's current state."""
        self._check_open()
        if not self._alive:
            exited = ChromeExitedError(
                "Chrome exited (fake); every later call on this client fails, "
                "create a new Client (or AsyncClient) to continue"
            )
            exited.kind = "chrome_exited"
            exited.url = url
            raise exited
        if self.error is not None:
            raise self.error
        page = self.pages.get(url)
        if isinstance(page, RenderResult):
            return page
        return RenderResult(
            page if page is not None else f"<html><body>{url}</body></html>", final_url=url
        )

    async def fetch(
        self,
        url: str,
        *,
        config: FetchConfig | None = None,
        **overrides: Any,
    ) -> RenderResult:
        """Serve the page for `url`; raise what a real client would raise in the same state.

        Args:
            url: The URL to fetch.
            config: Validated with `overrides` as the real client does, then ignored.
            **overrides: Per-call overrides; an unknown one raises TypeError as it does for real.

        Raises:
            OnyxwebError: If the client is closed.
            ChromeExitedError: If `die` was called.
        """
        _merge_fetch_config(config, overrides)
        self.fetched.append((url, overrides))
        return self._serve(url)

    async def screenshot(
        self,
        url: str,
        *,
        config: ScreenshotConfig | None = None,
        **overrides: Any,
    ) -> bytes:
        """Serve a fake image of the ``format`` asked for (PNG by default).

        Raises:
            OnyxwebError: If the client is closed.
            ChromeExitedError: If `die` was called.
        """
        shot = _merge_screenshot_config(config, overrides)
        self.fetched.append((url, overrides))
        self._serve(url)
        return _IMAGES[shot.format]

    async def fetch_all(
        self,
        url: str,
        *,
        config: FetchConfig | None = None,
        full_page: bool = False,
        format: Literal["png", "jpeg", "webp"] = "png",
        quality: int | None = None,
        **overrides: Any,
    ) -> FetchResult:
        """Serve the page for `url` with a fake image of `format`, as one `FetchResult`."""
        _merge_fetch_config(config, overrides)
        ScreenshotConfig(full_page=full_page, format=format, quality=quality)  # validates them
        recorded = dict(overrides)
        if full_page:
            recorded["full_page"] = True
        if format != "png":
            recorded["format"] = format
        if quality is not None:
            recorded["quality"] = quality
        self.fetched.append((url, recorded))
        return _fetch_result(self._serve(url), _IMAGES[format])

    async def batch(
        self,
        urls: Iterable[str],
        *,
        capture: Literal["html", "png", "both"] = "html",
        config: FetchConfig | None = None,
    ) -> list[RenderResult | FetchResult | bytes | Exception]:
        """Serve every URL, in order; a failure is returned in its place, as for the real client.

        Raises:
            OnyxwebError: If the client is closed.
            ValueError: If `capture` is not ``"html"``, ``"png"`` or ``"both"``.
        """
        self._check_open()
        if capture not in ("html", "png", "both"):
            raise ValueError(f"capture must be 'html'|'png'|'both', got {capture!r}")
        overrides = config.model_dump(exclude_unset=True) if config else {}
        items: list[RenderResult | FetchResult | bytes | Exception] = []
        for url in urls:
            self.fetched.append((url, dict(overrides)))
            try:
                page = self._serve(url)
            except Exception as failure:  # a failed URL is its exception, as in a real batch
                items.append(failure)
                continue
            if capture == "html":
                items.append(page)
            elif capture == "png":
                items.append(_IMAGES["png"])
            else:
                items.append(_fetch_result(page, _IMAGES["png"]))
        return items

    async def aclose(self) -> None:
        """Close the client; closing one whose Chrome died is fine."""
        self.closed = True

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()


def _fetch_result(page: RenderResult, image: bytes) -> FetchResult:
    """A `FetchResult` over `page` and `image`, built without the Rust output it usually wraps.

    `FetchResult` forwards three fields to that output, so a stand-in carries just those.
    """
    result = FetchResult.__new__(FetchResult)
    result.html = page
    result.png = image
    result._raw = cast(
        _FetchOutput,
        SimpleNamespace(
            final_url=page.final_url, status_code=page.status_code, elapsed_s=page.elapsed_s
        ),
    )
    return result


@dataclass
class FakeClientFactory:
    """Builds a `FakeClient` per engine, for code that takes a ``make_client(engine)`` callable.

    Attributes:
        pages: Canned pages every client it builds serves.
        error: Set on every client it builds.
        built: Each client built, as ``(engine, client)``, in order.
    """

    pages: Mapping[str, str | RenderResult] | None = None
    error: BaseException | None = None
    built: list[tuple[str, FakeClient]] = field(default_factory=list)

    def __call__(self, engine: str) -> FakeClient:
        """Build a `FakeClient` for `engine` and remember it."""
        client = FakeClient(self.pages, error=self.error)
        self.built.append((engine, client))
        return client

    @property
    def engines(self) -> list[str]:
        """The engine of each client built, in order."""
        return [engine for engine, _ in self.built]
