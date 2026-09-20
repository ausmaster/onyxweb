"""Test doubles for code that uses onyxweb, so it can be tested without Chrome.

``FakeClient`` stands in for `AsyncClient`: it serves canned pages, records what it was asked
for, and can be told to fail, close or die. It offers the methods a caller needs to fetch and
shut down (``fetch``, ``aclose``, ``alive``), each with the real client's signature. Import it by
full module path::

    from onyxweb.testing import FakeClient, FakeClientFactory

    fake = FakeClient({"https://example.com/": "<h1>Example</h1>"})
    page = await fake.fetch("https://example.com/")   # a RenderResult, no browser involved
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Self

from onyxweb import ChromeExitedError, OnyxwebError, RenderResult, _merge_fetch_config
from onyxweb.config import FetchConfig


class FakeClient:
    """An `AsyncClient` that serves canned pages and never launches Chrome.

    Attributes:
        pages: URL -> the page to serve, as html or a ready `RenderResult`. A URL not listed
            serves a small page whose html is the URL itself.
        error: When set, every fetch raises it; clear it to let fetches succeed again.
        closed: True once `aclose` was called.
        fetched: Every ``fetch`` call as ``(url, overrides)``, in order.
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
        """Make Chrome "exit": `alive` turns False and every fetch raises `ChromeExitedError`."""
        self._alive = False

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
        if self.closed:
            raise OnyxwebError("internal: Client is closed")
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

    async def aclose(self) -> None:
        """Close the client; closing one whose Chrome died is fine."""
        self.closed = True

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()


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
