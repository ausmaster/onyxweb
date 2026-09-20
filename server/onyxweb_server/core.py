"""Server core: the URL guard, the page store and the browser clients every front-end shares.

Front-ends (MCP, HTTP) differ in how they present a page, not in what they refuse to fetch,
which pages they hold or how the browser is reached, so those three live here once.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import os
import re
import socket
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Final
from urllib.parse import urlsplit

import onyxweb
from onyxweb import RenderResult

DEFAULT_MAX_PAGES: Final = 50  # pages held before the least recently used is dropped
CLIENT_CONCURRENCY: Final = 4  # tabs per engine
ENGINES: Final = ("shell", "full")
MAX_WAIT_MS: Final = 30_000  # longest settle a caller may ask of one fetch
PAGE_ID: Final = re.compile(r"p[0-9a-f]{10}")


# --- URL guard ---------------------------------------------------------------------------


def _addresses(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Every address `host` names; a name that is not an IP literal is resolved."""
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    # Decimal (2130706433) and octal (0177.0.0.1) forms are names to Python but 127.0.0.1
    # to Chrome, so anything that is not a plain literal goes through the resolver.
    try:
        found = socket.getaddrinfo(host, None)
    except OSError as oe:
        raise ValueError(f"cannot resolve host {host!r}; pass a reachable public host.") from oe
    return [ipaddress.ip_address(str(info[4][0]).split("%")[0]) for info in found]


def check_url(url: str) -> None:
    """Refuse a URL the server must not fetch.

    Args:
        url: The URL an agent asked for.

    Raises:
        ValueError: If the scheme is not http or https, the URL has no host or carries
            credentials, or the host resolves to any address that is not public.

    Security:
        Redirects and DNS rebinding are not covered; a public URL may redirect to a private
        one. Close both with a network that has no route to private ranges.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"{url!r}: only http and https URLs are fetched; pass a public URL.")
    if not parts.hostname:
        raise ValueError(f"{url!r} has no host; pass a full URL such as https://example.com/.")
    if parts.username is not None or parts.password is not None:
        raise ValueError(f"{url!r} carries credentials; remove user:pass@ from the URL.")
    for address in _addresses(parts.hostname):
        # Before Python 3.11.10 a mapped ::ffff:a.b.c.d escaped the IPv4 private ranges; judge
        # the embedded IPv4. No test can show it here, because this interpreter already does.
        address = getattr(address, "ipv4_mapped", None) or address
        if (
            not address.is_global
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
            or address.is_loopback
            or address.is_link_local
        ):
            raise ValueError(
                f"{url!r} points at a private or internal address ({address}); "
                "only public addresses are fetched."
            )


# --- the pages held ----------------------------------------------------------------------


class PageStore:
    """Pages fetched this session; the least recently used goes first once `max_pages` is full."""

    def __init__(self, max_pages: int) -> None:
        if max_pages < 1:
            raise ValueError(
                f"max_pages must be at least 1, got {max_pages}; set it to how many pages to keep."
            )
        self._max = max_pages
        self._pages: OrderedDict[str, tuple[RenderResult, float]] = OrderedDict()

    def add(self, page: RenderResult) -> str:
        """Hold `page`, counting it as used; return its id, the same for the same page."""
        digest = hashlib.sha256(f"{page.final_url}\n{page.html}".encode()).hexdigest()
        page_id = f"p{digest[:10]}"
        self._pages.pop(page_id, None)
        self._pages[page_id] = (page, time.monotonic())
        while len(self._pages) > self._max:
            self._pages.popitem(last=False)
        return page_id

    def get(self, page_id: str) -> RenderResult:
        """Return the page for `page_id`, counting it as used."""
        if not PAGE_ID.fullmatch(page_id):
            raise ValueError(
                f"{page_id!r} is not a page id; ids look like p1a2b3c4d5, "
                "from fetch or pages. Fetch a URL first."
            )
        if page_id not in self._pages:
            raise ValueError(
                f"no page {page_id} in this session (the server restarted, or it was evicted); "
                "fetch the URL again."
            )
        self._pages.move_to_end(page_id)
        return self._pages[page_id][0]

    def newest_first(self) -> list[tuple[str, RenderResult, float]]:
        """Every page held as (id, page, age in seconds), without counting a use."""
        now = time.monotonic()
        return [(i, p, now - t) for i, (p, t) in reversed(self._pages.items())]


# --- browser clients ---------------------------------------------------------------------


class ClientPool:
    """One browser client per engine, built on first use and closed together."""

    def __init__(self, make: Callable[[str], onyxweb.AsyncClient] | None = None) -> None:
        self._make = make or (
            lambda engine: onyxweb.AsyncClient(concurrency=CLIENT_CONCURRENCY, engine=engine)
        )
        self._clients: dict[str, onyxweb.AsyncClient] = {}
        self._building = asyncio.Lock()

    async def get(self, engine: str) -> onyxweb.AsyncClient:
        """Return a live client for `engine`, building one on first use or after its Chrome died."""
        async with self._building:
            client = self._clients.get(engine)
            if client is None or not client.alive:
                if client is not None:
                    await client.aclose()
                self._clients[engine] = await asyncio.to_thread(self._make, engine)
            return self._clients[engine]

    def health(self) -> dict[str, bool]:
        """Whether the Chrome of each engine built so far is alive, without a fetch."""
        return {engine: client.alive for engine, client in self._clients.items()}

    async def aclose(self) -> None:
        """Close every client built so far."""
        for client in self._clients.values():
            await client.aclose()


# --- the core ----------------------------------------------------------------------------


class ServerCore:
    """What every front-end shares: guarded fetching, held pages and browser clients.

    A front-end reaches the browser only through `fetch`, so the URL guard and the request
    ceilings cannot be skipped. `fetch` holds nothing; a front-end that wants a page kept
    under an id calls `hold`. The caller can never pass scripts or actions: `fetch` has no
    parameter for them.
    """

    def __init__(
        self,
        make_client: Callable[[str], onyxweb.AsyncClient] | None = None,
        *,
        url_guard: Callable[[str], None] = check_url,
        max_pages: int | None = None,
    ) -> None:
        """Build a core with an empty page store and no browser yet.

        Args:
            make_client: Builds the browser client for an engine; each is built on first use.
                Default: a client of `CLIENT_CONCURRENCY` tabs.
            url_guard: Raises ValueError for a URL that must not be fetched. Tests pass a
                permissive one because their server listens on 127.0.0.1; a real front-end
                never does.
            max_pages: Pages held before the least recently used goes. Default:
                ``ONYXWEB_SERVER_MAX_PAGES``, else `DEFAULT_MAX_PAGES`.
        """
        if max_pages is None:
            max_pages = int(os.environ.get("ONYXWEB_SERVER_MAX_PAGES", DEFAULT_MAX_PAGES))
            if max_pages < 1:
                raise ValueError(
                    f"ONYXWEB_SERVER_MAX_PAGES must be at least 1, got {max_pages}; "
                    "set it to how many pages to keep."
                )
        self._guard = url_guard
        self._store = PageStore(max_pages)
        self._pool = ClientPool(make_client)

    async def fetch(self, url: str, *, engine: str = "shell", wait_ms: int = 0) -> RenderResult:
        """Fetch `url` in a real browser and return the page, holding nothing.

        Args:
            url: The URL to fetch; the guard refuses anything that is not public http(s).
            engine: ``"shell"`` (fast) or ``"full"`` (a real Chrome that passes more bot checks).
            wait_ms: Milliseconds to wait after the page loads, at most `MAX_WAIT_MS`.

        Raises:
            ValueError: If the engine is unknown, `wait_ms` is out of range, or the guard
                refuses the URL. Nothing is built or requested first.
        """
        if engine not in ENGINES:
            raise ValueError(f"engine must be 'shell' or 'full', got {engine!r}.")
        if not 0 <= wait_ms <= MAX_WAIT_MS:
            raise ValueError(f"wait_ms must be between 0 and {MAX_WAIT_MS}, got {wait_ms}.")
        await asyncio.to_thread(self._guard, url)
        client = await self._pool.get(engine)
        return await client.fetch(url, wait_after_ms=wait_ms)

    def hold(self, page: RenderResult) -> str:
        """Keep `page` under an id, counting it as used; the same page gets the same id."""
        return self._store.add(page)

    def page(self, page_id: str) -> RenderResult:
        """Return the held page for `page_id`, counting it as used."""
        return self._store.get(page_id)

    def pages(self) -> list[tuple[str, RenderResult, float]]:
        """Every held page as (id, page, age in seconds), newest first, without counting a use."""
        return self._store.newest_first()

    def health(self) -> dict[str, bool]:
        """Whether the Chrome of each engine built so far is alive, without a fetch."""
        return self._pool.health()

    async def aclose(self) -> None:
        """Close every browser client."""
        await self._pool.aclose()
