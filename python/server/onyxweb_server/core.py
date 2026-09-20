"""Server core: the policy every front-end shares.

Front-ends (MCP, HTTP) differ in how they present a page, not in what they refuse, which limits
apply, which pages they hold or how the browser is reached, so all of that lives here once: the
URL guard, the allowed options and their ceilings, the size and queue limits, the page store and
the browser clients. Every refusal is a `Refused` with a stable `code`.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
from collections import Counter, OrderedDict
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Final, Literal, Protocol, TypeVar
from urllib.parse import urlsplit

import onyxweb
import pydantic
from onyxweb import FetchResult, RenderResult
from onyxweb.config import FetchConfig, ScreenshotConfig

from onyxweb_server.egress import REFUSED_HEADER, EgressProxy, is_public, resolve_host

DEFAULT_MAX_PAGES: Final = 50  # pages held before the least recently used is dropped
CLIENT_CONCURRENCY: Final = 4  # tabs per engine
ENGINES: Final = ("shell", "full")
MAX_WAIT_MS: Final = 30_000  # longest settle a caller may ask of one fetch
MIN_TIMEOUT_MS: Final = 100  # shortest navigation budget a caller may ask for
MAX_HEADERS: Final = 20  # extra request headers per fetch
MAX_HEADER_BYTES: Final = 8192  # names and values of those headers together
MAX_BLOCKS: Final = 50  # URL patterns a caller may block per fetch
WAIT_MODES: Final = ("load", "domcontentloaded")
IMAGE_FORMATS: Final = ("png", "jpeg", "webp")
MAX_VIEWPORT: Final = 4096  # widest and tallest screenshot viewport, in pixels
PAGE_ID: Final = re.compile(r"p[0-9a-f]{10}")
MB: Final = 1024 * 1024
log = logging.getLogger("onyxweb_server")
T = TypeVar("T")


class Refused(ValueError):
    """A request the core will not serve. `code` says why, for a front-end to map.

    Codes: ``refused_url`` (the guard), ``refused_option`` (an option outside what the core
    allows), ``too_large`` (a page over a size limit).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# --- limits ------------------------------------------------------------------------------

# CoreConfig field -> (environment variable, smallest allowed value).
_LIMITS: Final = {
    "max_pages": ("ONYXWEB_SERVER_MAX_PAGES", 1),
    "max_store_bytes": ("ONYXWEB_SERVER_MAX_STORE_BYTES", 1),
    "max_page_bytes": ("ONYXWEB_SERVER_MAX_PAGE_BYTES", 1),
    "max_batch": ("ONYXWEB_SERVER_MAX_BATCH", 1),
    "max_wait_ms": ("ONYXWEB_SERVER_MAX_WAIT_MS", 0),
    "max_timeout_ms": ("ONYXWEB_SERVER_MAX_TIMEOUT_MS", MIN_TIMEOUT_MS),
    "queue_ms": ("ONYXWEB_SERVER_QUEUE_MS", 1),
    "concurrency": ("ONYXWEB_SERVER_CONCURRENCY", 1),
}
EGRESS_VAR: Final = "ONYXWEB_SERVER_EGRESS"


@dataclass(frozen=True)
class CoreConfig:
    """Every limit the core enforces; `from_env` reads them from ``ONYXWEB_SERVER_*``."""

    max_pages: int = DEFAULT_MAX_PAGES  # pages held
    max_store_bytes: int = 256 * MB  # bytes of html held
    max_page_bytes: int = 20 * MB  # largest page or image one fetch may return
    max_batch: int = 50  # URLs in one batch
    max_wait_ms: int = MAX_WAIT_MS  # longest settle after the page loads
    max_timeout_ms: int = 60_000  # longest navigation budget
    queue_ms: int = 10_000  # longest a request waits for a free tab
    concurrency: int = CLIENT_CONCURRENCY  # tabs per engine
    egress: bool = True  # route the browser through the egress proxy

    def __post_init__(self) -> None:
        for name, (_, smallest) in _LIMITS.items():
            if (value := getattr(self, name)) < smallest:
                raise ValueError(
                    f"{name} must be at least {smallest}, got {value}; "
                    "set it to how many the server should allow."
                )

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> CoreConfig:
        """Read the limits from ``ONYXWEB_SERVER_*``, keeping a default for each one not set.

        Raises:
            ValueError: If a variable is not a whole number, is below its minimum, or
                ``ONYXWEB_SERVER_EGRESS`` is not 1 or 0.
        """
        environ = os.environ if environ is None else environ
        values: dict[str, Any] = {}
        for name, (var, smallest) in _LIMITS.items():
            if var not in environ:
                continue
            try:
                number = int(environ[var])
            except ValueError as ve:
                raise ValueError(
                    f"{var} must be a whole number, got {environ[var]!r}; set it to a count."
                ) from ve
            if number < smallest:
                raise ValueError(
                    f"{var} must be at least {smallest}, got {number}; "
                    "set it to how many the server should allow."
                )
            values[name] = number
        if EGRESS_VAR in environ:
            flag = environ[EGRESS_VAR].strip().lower()
            if flag not in ("0", "1", "true", "false", "yes", "no", "on", "off"):
                raise ValueError(f"{EGRESS_VAR} must be 1 or 0, got {environ[EGRESS_VAR]!r}.")
            values["egress"] = flag in ("1", "true", "yes", "on")
        return cls(**values)


# --- what a caller may ask for -----------------------------------------------------------


@dataclass(frozen=True)
class FetchOptions:
    """The knobs a caller may set on one fetch, and only those.

    There is no field for scripts, post-load scripts, actions or navigation blocking: the
    server never runs caller-supplied JavaScript.
    """

    engine: str = "shell"  # "shell" is fast; "full" is a real Chrome that passes more bot checks
    wait_ms: int = 0  # settle after the page loads
    timeout_ms: int | None = None  # navigation budget; None keeps the client's
    wait_until: str | None = None  # "load" or "domcontentloaded"; None keeps the client's
    headers: Mapping[str, str] = field(default_factory=dict)  # extra request headers
    block_urls: Sequence[str] = ()  # URLPattern strings to block, such as "*://*.ads.test/*"
    bypass_anti_bot: bool | None = None  # None keeps the client's


@dataclass(frozen=True)
class ShotOptions:
    """The image knobs a caller may set on `ServerCore.screenshot` and `fetch_all`."""

    full_page: bool = False  # capture the whole scrollable page, not just the viewport
    format: str = "png"  # "png", "jpeg" or "webp"
    quality: int | None = None  # 0-100, for jpeg and webp
    viewport: tuple[int, int] | None = None  # (width, height); `screenshot` only


def _refuse_option(message: str) -> Refused:
    return Refused("refused_option", message)


def _fetch_overrides(options: FetchOptions, config: CoreConfig) -> dict[str, Any]:
    """Validate `options` against the ceilings; return the overrides to pass the client.

    Only what differs from the client's own default is returned.

    Raises:
        Refused: ``refused_option``, naming the option and the fix.
    """
    if options.engine not in ENGINES:
        raise _refuse_option(f"engine must be 'shell' or 'full', got {options.engine!r}.")
    if not 0 <= options.wait_ms <= config.max_wait_ms:
        raise _refuse_option(
            f"wait_ms must be between 0 and {config.max_wait_ms}, got {options.wait_ms}."
        )
    timeout = options.timeout_ms
    if timeout is not None and not MIN_TIMEOUT_MS <= timeout <= config.max_timeout_ms:
        raise _refuse_option(
            f"timeout_ms must be between {MIN_TIMEOUT_MS} and {config.max_timeout_ms}, "
            f"got {timeout}."
        )
    if options.wait_until is not None and options.wait_until not in WAIT_MODES:
        raise _refuse_option(
            f"wait_until must be 'load' or 'domcontentloaded', got {options.wait_until!r}."
        )
    if len(options.headers) > MAX_HEADERS:
        raise _refuse_option(
            f"headers holds {len(options.headers)}; send at most {MAX_HEADERS} extra headers."
        )
    if sum(len(k) + len(v) for k, v in options.headers.items()) > MAX_HEADER_BYTES:
        raise _refuse_option(f"headers are over {MAX_HEADER_BYTES} bytes together; send fewer.")
    if len(options.block_urls) > MAX_BLOCKS:
        raise _refuse_option(
            f"block_urls holds {len(options.block_urls)}; block at most {MAX_BLOCKS} patterns."
        )
    try:
        # The library's own validators: forbidden headers, and URLPatterns Chrome can parse.
        FetchConfig(extra_headers=dict(options.headers), block_urls=list(options.block_urls))
    except pydantic.ValidationError as ve:
        raise _refuse_option(
            "; ".join(e["msg"].removeprefix("Value error, ") for e in ve.errors())
        ) from ve
    overrides: dict[str, Any] = {}
    if options.wait_ms:
        overrides["wait_after_ms"] = options.wait_ms
    if timeout is not None:
        overrides["timeout_ms"] = timeout
    if options.wait_until is not None:
        overrides["wait_until"] = options.wait_until
    if options.headers:
        overrides["extra_headers"] = dict(options.headers)
    if options.block_urls:
        overrides["block_urls"] = list(options.block_urls)
    if options.bypass_anti_bot is not None:
        overrides["bypass_anti_bot"] = options.bypass_anti_bot
    return overrides


def _shot_overrides(shot: ShotOptions, *, viewport_allowed: bool) -> dict[str, Any]:
    """Validate `shot`; return the image overrides to pass the client.

    Raises:
        Refused: ``refused_option``, naming the option and the fix.
    """
    if shot.format not in IMAGE_FORMATS:
        raise _refuse_option(f"format must be 'png', 'jpeg' or 'webp', got {shot.format!r}.")
    if shot.quality is not None and not 0 <= shot.quality <= 100:
        raise _refuse_option(f"quality must be between 0 and 100, got {shot.quality}.")
    if shot.viewport is not None:
        if not viewport_allowed:
            raise _refuse_option("fetch_all takes no viewport; use screenshot for a chosen size.")
        if not all(1 <= side <= MAX_VIEWPORT for side in shot.viewport):
            raise _refuse_option(
                f"viewport must be (width, height), each between 1 and {MAX_VIEWPORT}, "
                f"got {shot.viewport}."
            )
    overrides: dict[str, Any] = {}
    if shot.full_page:
        overrides["full_page"] = True
    if shot.format != "png":
        overrides["format"] = shot.format
    if shot.quality is not None:
        overrides["quality"] = shot.quality
    if shot.viewport is not None:
        overrides["viewport"] = tuple(shot.viewport)
    return overrides


def _page_bytes(page: RenderResult) -> int:
    return len(page.html.encode())


# --- URL guard ---------------------------------------------------------------------------


def check_url(url: str) -> None:
    """Refuse a URL the server must not fetch.

    Args:
        url: The URL an agent asked for.

    Raises:
        Refused: ``refused_url``, if the scheme is not http or https, the URL has no host or
            carries credentials, or the host resolves to any address that is not public.

    Security:
        Redirects and DNS rebinding are not covered; a public URL may redirect to a private
        one. Close both with a network that has no route to private ranges.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise Refused(
            "refused_url", f"{url!r}: only http and https URLs are fetched; pass a public URL."
        )
    if not parts.hostname:
        raise Refused(
            "refused_url", f"{url!r} has no host; pass a full URL such as https://example.com/."
        )
    if parts.username is not None or parts.password is not None:
        raise Refused(
            "refused_url", f"{url!r} carries credentials; remove user:pass@ from the URL."
        )
    try:
        addresses = resolve_host(parts.hostname)
    except OSError as oe:
        raise Refused(
            "refused_url", f"cannot resolve host {parts.hostname!r}; pass a reachable public host."
        ) from oe
    for address in addresses:
        if not is_public(address):
            raise Refused(
                "refused_url",
                f"{url!r} points at a private or internal address ({address}); "
                "only public addresses are fetched.",
            )


# --- the pages held ----------------------------------------------------------------------


class PageStore:
    """Pages fetched this session; the least recently used goes first past either limit."""

    def __init__(self, max_pages: int, max_bytes: int) -> None:
        if max_pages < 1:
            raise ValueError(
                f"max_pages must be at least 1, got {max_pages}; set it to how many pages to keep."
            )
        self._max = max_pages
        self._max_bytes = max_bytes
        self._pages: OrderedDict[str, tuple[RenderResult, float, int]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._pages)

    @property
    def held_bytes(self) -> int:
        """Bytes of html held."""
        return sum(size for _, _, size in self._pages.values())

    def add(self, page: RenderResult) -> str:
        """Hold `page`, counting it as used; return its id, the same for the same page.

        Raises:
            Refused: ``too_large``, if the page alone is over the store's byte limit.
        """
        size = _page_bytes(page)
        if size > self._max_bytes:
            raise Refused(
                "too_large",
                f"the page is {size} bytes, over the store's {self._max_bytes}; "
                "raise ONYXWEB_SERVER_MAX_STORE_BYTES, or fetch a smaller page.",
            )
        digest = hashlib.sha256(f"{page.final_url}\n{page.html}".encode()).hexdigest()
        page_id = f"p{digest[:10]}"
        self._pages.pop(page_id, None)
        self._pages[page_id] = (page, time.monotonic(), size)
        while len(self._pages) > self._max or self.held_bytes > self._max_bytes:
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
        return [(i, p, now - t) for i, (p, t, _) in reversed(self._pages.items())]


# --- browser clients ---------------------------------------------------------------------


class BrowserClient(Protocol):
    """What the server needs of a browser client.

    `onyxweb.AsyncClient` fits it, and so does `onyxweb.testing.FakeClient`.
    """

    @property
    def alive(self) -> bool:
        """Whether the browser is still running."""

    async def fetch(
        self, url: str, *, config: FetchConfig | None = None, **overrides: Any
    ) -> RenderResult:
        """Fetch `url`, applying the per-call `overrides` of `FetchConfig`."""

    async def screenshot(
        self, url: str, *, config: ScreenshotConfig | None = None, **overrides: Any
    ) -> bytes:
        """Screenshot `url`, applying the per-call `overrides` of `ScreenshotConfig`."""

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
        """Fetch `url` and screenshot it from one visit."""

    async def batch(
        self,
        urls: Iterable[str],
        *,
        capture: Literal["html", "png", "both"] = "html",
        config: FetchConfig | None = None,
    ) -> list[RenderResult | FetchResult | bytes | Exception]:
        """Fetch every URL; a failure is returned in its place."""

    async def aclose(self) -> None:
        """Shut the browser down."""


class ClientPool:
    """One browser client per engine, built on first use and closed together."""

    def __init__(
        self,
        make: Callable[[str], BrowserClient] | None = None,
        config: CoreConfig | None = None,
        egress: EgressProxy | None = None,
    ) -> None:
        config = config or CoreConfig()
        # Only clients the pool builds itself go through the proxy; a caller's own are theirs.
        self._egress = egress if make is None else None

        def build(engine: str) -> BrowserClient:
            proxied: dict[str, Any] = {}
            if self._egress is not None:
                # Chrome skips a proxy for loopback and link-local unless told not to.
                proxied = {"proxy": self._egress.url, "proxy_bypass_list": "<-loopback>"}
            return onyxweb.AsyncClient(
                concurrency=config.concurrency,
                queue_timeout_ms=config.queue_ms,
                engine=engine,
                **proxied,
            )

        self._make = make or build
        self._clients: dict[str, BrowserClient] = {}
        self._building = asyncio.Lock()
        self.restarts = 0  # clients replaced because their Chrome died

    async def get(self, engine: str) -> BrowserClient:
        """Return a live client for `engine`, building one on first use or after its Chrome died."""
        async with self._building:
            if self._egress is not None:
                await self._egress.start()
            client = self._clients.get(engine)
            if client is None or not client.alive:
                if client is not None:
                    self.restarts += 1
                    log.warning(f"chrome for engine={engine} died; starting a new one")
                    await client.aclose()
                self._clients[engine] = await asyncio.to_thread(self._make, engine)
            return self._clients[engine]

    def health(self) -> dict[str, bool]:
        """Whether the Chrome of each engine built so far is alive, without a fetch."""
        return {engine: client.alive for engine, client in self._clients.items()}

    async def aclose(self) -> None:
        """Close every client built so far, then the egress proxy."""
        for client in self._clients.values():
            await client.aclose()
        if self._egress is not None:
            await self._egress.aclose()


# --- the core ----------------------------------------------------------------------------


def _where(url: str) -> str:
    """Host and path of `url`, for a log line: no credentials, query string or fragment."""
    try:
        parts = urlsplit(url)
        return f"{parts.hostname or '?'}{parts.path}"
    except ValueError:
        return "?"


def _egress_refusal(url: str) -> Refused:
    return Refused(
        "refused_url",
        f"fetching {_where(url)} led the browser to a private or internal address; "
        "only public addresses are fetched.",
    )


def _kind(failure: Exception) -> str:
    """Why a call failed: a `Refused` code, else the browser error's kind, else its type."""
    return str(
        getattr(failure, "code", None) or getattr(failure, "kind", None) or type(failure).__name__
    )


def _ms(started: float) -> str:
    return f"{(time.monotonic() - started) * 1000:.0f}ms"


def _summary(result: object) -> str:
    """What a log line says of a result: a status, image bytes, or how many of a batch worked."""
    if isinstance(result, RenderResult):
        return f" status={result.status_code}"
    if isinstance(result, FetchResult):
        return f" status={result.status_code}"
    if isinstance(result, bytes):
        return f" {len(result)} bytes"
    if isinstance(result, list):
        return f" {sum(not isinstance(i, Exception) for i in result)}/{len(result)} ok"
    return ""


class ServerCore:
    """What every front-end shares: guarded fetching, held pages and browser clients.

    A front-end reaches the browser only through `fetch`, so the URL guard, the option ceilings
    and the size limits cannot be skipped. `fetch` holds nothing; a front-end that wants a page
    kept under an id calls `hold`. The caller can never pass scripts or actions: `FetchOptions`
    has no field for them.
    """

    def __init__(
        self,
        make_client: Callable[[str], BrowserClient] | None = None,
        *,
        url_guard: Callable[[str], None] = check_url,
        max_pages: int | None = None,
        config: CoreConfig | None = None,
        egress: EgressProxy | None = None,
    ) -> None:
        """Build a core with an empty page store and no browser yet.

        Args:
            make_client: Builds the browser client for an engine; each is built on first use.
                Default: a real client with the limits of `config`.
            url_guard: Raises `Refused` for a URL that must not be fetched. Tests pass a
                permissive one because their server listens on 127.0.0.1; a real front-end
                never does.
            max_pages: Pages held before the least recently used goes; overrides `config`.
            config: The limits. Default: `CoreConfig.from_env()`.
            egress: The proxy the default browser clients are pointed at. Default: a public-only
                one when `config.egress` is on. A `make_client` of your own is not proxied.
        """
        config = config or CoreConfig.from_env()
        if max_pages is not None:
            config = replace(config, max_pages=max_pages)
        self._config = config
        self._guard = url_guard
        self._store = PageStore(config.max_pages, config.max_store_bytes)
        if egress is None and config.egress and make_client is None:
            egress = EgressProxy()
        self._egress = egress if make_client is None else None
        self._pool = ClientPool(make_client, config, self._egress)
        self._requests = 0
        self._retries = 0
        self._failures: Counter[str] = Counter()

    async def fetch(self, url: str, options: FetchOptions | None = None) -> RenderResult:
        """Fetch `url` in a real browser and return the page, holding nothing.

        Args:
            url: The URL to fetch; the guard refuses anything that is not public http(s).
            options: What the caller may ask of the fetch; see `FetchOptions`.

        Raises:
            Refused: ``refused_option`` for an option outside its ceiling, ``refused_url`` from
                the guard (nothing is built or requested first), or ``too_large`` for a page
                over the size limit.
        """
        options = options or FetchOptions()
        return await self._observe("fetch", _where(url), options, self._fetch(url, options))

    async def _fetch(self, url: str, options: FetchOptions) -> RenderResult:
        overrides = _fetch_overrides(options, self._config)
        page = await self._call(url, options, lambda client: client.fetch(url, **overrides))
        self._check_size(_page_bytes(page), "page")
        return page

    async def screenshot(
        self, url: str, options: FetchOptions | None = None, shot: ShotOptions | None = None
    ) -> bytes:
        """Screenshot `url` and return the image bytes, holding nothing.

        Args:
            url: The URL to capture; the guard refuses anything that is not public http(s).
            options: Engine, settle, timeout, wait mode and headers. `block_urls` and
                `bypass_anti_bot` do not apply to a screenshot and are refused.
            shot: Format, quality, full page and viewport.

        Raises:
            Refused: ``refused_option``, ``refused_url`` or ``too_large``, as for `fetch`.
        """
        options = options or FetchOptions()
        return await self._observe(
            "screenshot",
            _where(url),
            options,
            self._screenshot(url, options, shot or ShotOptions()),
        )

    async def _screenshot(self, url: str, options: FetchOptions, shot: ShotOptions) -> bytes:
        overrides = _fetch_overrides(options, self._config)
        for name in ("block_urls", "bypass_anti_bot"):
            if overrides.pop(name, None) is not None:
                raise _refuse_option(f"screenshot takes no {name}; use fetch_all, or remove it.")
        overrides |= _shot_overrides(shot, viewport_allowed=True)
        image = await self._call(url, options, lambda client: client.screenshot(url, **overrides))
        self._check_size(len(image), "image")
        return image

    async def fetch_all(
        self, url: str, options: FetchOptions | None = None, shot: ShotOptions | None = None
    ) -> FetchResult:
        """Fetch `url` and screenshot it from one visit; the page and the image, holding nothing.

        Raises:
            Refused: ``refused_option``, ``refused_url`` or ``too_large``, as for `fetch`.
        """
        options = options or FetchOptions()
        return await self._observe(
            "fetch_all", _where(url), options, self._fetch_all(url, options, shot or ShotOptions())
        )

    async def _fetch_all(self, url: str, options: FetchOptions, shot: ShotOptions) -> FetchResult:
        overrides = _fetch_overrides(options, self._config)
        overrides |= _shot_overrides(shot, viewport_allowed=False)
        both = await self._call(url, options, lambda client: client.fetch_all(url, **overrides))
        self._check_size(_page_bytes(both.html), "page")
        self._check_size(len(both.png), "image")
        return both

    async def batch(
        self, urls: Sequence[str], options: FetchOptions | None = None
    ) -> list[RenderResult | Exception]:
        """Fetch many URLs at once; each result is its page, or the failure in its place.

        A URL the guard refuses is returned as its `Refused` and never reaches the browser.

        Args:
            urls: 1 to `CoreConfig.max_batch` URLs.
            options: Applied to every URL.

        Raises:
            Refused: ``refused_option``, if there are too few or too many URLs or an option is
                outside its ceiling. Nothing is built or requested first.
        """
        options = options or FetchOptions()
        return await self._observe(
            "batch", f"{len(urls)} urls", options, self._batch(urls, options), count=len(urls)
        )

    async def _batch(
        self, urls: Sequence[str], options: FetchOptions
    ) -> list[RenderResult | Exception]:
        overrides = _fetch_overrides(options, self._config)
        if not 1 <= len(urls) <= self._config.max_batch:
            raise _refuse_option(
                f"urls holds {len(urls)}; send at least 1 and at most {self._config.max_batch} "
                "(ONYXWEB_SERVER_MAX_BATCH)."
            )

        def verdict(url: str) -> Refused | None:
            try:
                self._guard(url)
            except Refused as refusal:
                return refusal
            return None

        before = self._refusals()
        verdicts = await asyncio.gather(*(asyncio.to_thread(verdict, url) for url in urls))
        allowed = [url for url, refusal in zip(urls, verdicts, strict=True) if refusal is None]
        fetched: list[Any] = []
        if allowed:
            config = FetchConfig(**overrides)
            client = await self._pool.get(options.engine)
            fetched = await client.batch(allowed, capture="html", config=config)
            # Each URL whose Chrome died gets one more try, on a client built after it.
            died = [
                i for i, item in enumerate(fetched) if isinstance(item, onyxweb.ChromeExitedError)
            ]
            if died:
                self._retries += 1
                client = await self._pool.get(options.engine)
                again = await client.batch(
                    [allowed[i] for i in died], capture="html", config=config
                )
                for i, item in zip(died, again, strict=True):
                    fetched[i] = item
        results: list[RenderResult | Exception] = []
        pending = iter(fetched)
        for url, refusal in zip(urls, verdicts, strict=True):
            item = refusal if refusal is not None else next(pending)
            if self._egress_refused(item, before):
                item = _egress_refusal(url)
            if isinstance(item, RenderResult):
                try:
                    self._check_size(_page_bytes(item), "page")
                except Refused as too_large:
                    item = too_large
            if isinstance(item, Exception):
                self._failures[_kind(item)] += 1
            results.append(item)
        return results

    async def _call(
        self, url: str, options: FetchOptions, do: Callable[[BrowserClient], Awaitable[T]]
    ) -> T:
        """Run `do` on the engine's client once the guard accepts `url`.

        A Chrome that dies mid-call is replaced and the call made once more; nothing else is
        retried, since a fetch is idempotent but a timeout would only repeat. A navigation the
        egress proxy refused (a redirect or a start URL on a private address) is `Refused`.
        """
        await asyncio.to_thread(self._guard, url)
        before = self._refusals()
        try:
            client = await self._pool.get(options.engine)
            try:
                result = await do(client)
            except onyxweb.ChromeExitedError:
                self._retries += 1
                result = await do(await self._pool.get(options.engine))
        except onyxweb.OnyxwebError as err:
            if self._egress_refused(err, before):
                raise _egress_refusal(url) from err
            raise
        if self._egress_refused(result, before):
            raise _egress_refusal(url)
        return result

    def _refusals(self) -> int:
        return self._egress.refusals if self._egress else 0

    def _egress_refused(self, item: object, before: int) -> bool:
        """Whether the egress proxy refused the navigation behind `item`, a result or an error.

        A refused plain-HTTP navigation comes back as the proxy's own marked 403; a refused
        HTTPS one as a tunnel failure, which counts only if the proxy refused something since
        `before`.
        """
        if isinstance(item, FetchResult):
            item = item.html
        if isinstance(item, RenderResult):
            return item.headers.get(REFUSED_HEADER) == "refused"
        return (
            isinstance(item, onyxweb.OnyxwebError)
            and self._refusals() > before
            and "ERR_TUNNEL_CONNECTION_FAILED" in str(item)
        )

    async def _observe(
        self, op: str, label: str, options: FetchOptions, work: Awaitable[T], count: int = 1
    ) -> T:
        """Count `work` and log one line for it: the operation, where, the engine and the outcome.

        Only host and path are logged (`label`), never a query string, credentials or a message.
        """
        started = time.monotonic()
        self._requests += count
        try:
            result = await work
        except Exception as failure:
            kind = _kind(failure)
            self._failures[kind] += 1  # a batch that returns counts its own failures in `_batch`
            log.warning(f"{op} {label} engine={options.engine} failed {kind} in {_ms(started)}")
            raise
        log.info(f"{op} {label} engine={options.engine} ok in {_ms(started)}{_summary(result)}")
        return result

    def _check_size(self, size: int, what: str) -> None:
        if size > self._config.max_page_bytes:
            raise Refused(
                "too_large",
                f"the {what} is {size} bytes, over the {self._config.max_page_bytes} limit; "
                "fetch a smaller page, or raise ONYXWEB_SERVER_MAX_PAGE_BYTES.",
            )

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

    def stats(self) -> dict[str, Any]:
        """Counters since the server started, and what the store holds now."""
        return {
            "requests": self._requests,
            "failures": dict(self._failures),
            "retries": self._retries,
            "restarts": self._pool.restarts,
            "held_pages": len(self._store),
            "held_bytes": self._store.held_bytes,
            "egress_refusals": self._refusals(),
        }

    async def aclose(self) -> None:
        """Close every browser client."""
        await self._pool.aclose()
