"""Server core: the policy every front-end shares.

Front-ends (MCP, HTTP) differ in how they present a page, not in what they refuse, which limits
apply, which pages they hold or how the browser is reached, so all of that lives here once: the
URL guard, the allowed options and their ceilings, the size and queue limits, the page store and
the browser clients. Every refusal is a `Refused`, whose `code` a front-end maps to its own
vocabulary.
"""

from __future__ import annotations

from asyncio import CancelledError, Lock, Task, create_task, gather, sleep, to_thread
from collections import Counter, OrderedDict
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass, field, replace
from hashlib import sha256
from json import dumps
from logging import getLogger
from os import environ
from re import compile
from time import monotonic
from types import TracebackType
from typing import Any, Final, Literal, Protocol, Self, TypeVar, cast, overload
from urllib.parse import urlsplit

import onyxweb
from onyxweb import FetchResult, RenderResult
from onyxweb.config import FetchConfig, ScreenshotConfig
from onyxweb.records import PAGE_BUCKETS
from pydantic import ValidationError

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
PAGE_ID: Final = compile(r"p[0-9a-f]{10}")
MB: Final = 1024 * 1024
log = getLogger("onyxweb_server")
T = TypeVar("T")


class Refused(ValueError):
    """A request the core will not serve. `code` says why, for a front-end to map.

    Catch this to catch every refusal, or one subclass for one cause.
    """

    code = "refused"


class RefusedUrl(Refused):
    """The URL guard, or the egress proxy, would not fetch that address."""

    code = "refused_url"


class RefusedOption(Refused):
    """An option the core does not allow, or one outside its ceiling."""

    code = "refused_option"


class TooLarge(Refused):
    """A page or image over a size limit."""

    code = "too_large"


class RefusedSession(Refused):
    """A session the core will not open: the cap is reached, or the id is not usable."""

    code = "refused_session"


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
    "max_sessions": ("ONYXWEB_SERVER_MAX_SESSIONS", 1),
    "session_ttl_ms": ("ONYXWEB_SERVER_SESSION_TTL_MS", 1),
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
    max_sessions: int = 16  # sessions held at once; each leases one tab
    session_ttl_ms: int = 120_000  # longest a session may idle before its tab goes back
    egress: bool = True  # route the browser through the egress proxy

    def __post_init__(self) -> None:
        for name, (_, smallest) in _LIMITS.items():
            if (value := getattr(self, name)) < smallest:
                raise ValueError(
                    f"{name} must be at least {smallest}, got {value}; "
                    "set it to how many the server should allow."
                )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> CoreConfig:
        """Read the limits from ``ONYXWEB_SERVER_*``, keeping a default for each one not set.

        Raises:
            ValueError: If a variable is not a whole number, is below its minimum, or
                ``ONYXWEB_SERVER_EGRESS`` is not 1 or 0.
        """
        env = environ if env is None else env
        values: dict[str, Any] = {}
        for name, (var, smallest) in _LIMITS.items():
            if var not in env:
                continue
            try:
                number = int(env[var])
            except ValueError as ve:
                raise ValueError(
                    f"{var} must be a whole number, got {env[var]!r}; set it to a count."
                ) from ve
            if number < smallest:
                raise ValueError(
                    f"{var} must be at least {smallest}, got {number}; "
                    "set it to how many the server should allow."
                )
            values[name] = number
        if EGRESS_VAR in env:
            flag = env[EGRESS_VAR].strip().lower()
            if flag not in ("0", "1", "true", "false", "yes", "no", "on", "off"):
                raise ValueError(f"{EGRESS_VAR} must be 1 or 0, got {env[EGRESS_VAR]!r}.")
            values["egress"] = flag in ("1", "true", "yes", "on")
        return cls(**values)


@dataclass(frozen=True)
class FetchOptions:
    """The knobs a caller may set on one fetch, and only those.

    There is no field for scripts, post-load scripts, actions or navigation blocking: the
    server never runs caller-supplied JavaScript.
    """

    engine: str = "full"  # a real Chrome that passes more bot checks; "shell" is lighter and faster
    wait_ms: int = 0  # settle after the page loads
    timeout_ms: int | None = None  # navigation budget; None keeps the client's
    wait_until: str | None = None  # "load" or "domcontentloaded"; None keeps the client's
    headers: Mapping[str, str] = field(default_factory=dict)  # extra request headers
    block_urls: Sequence[str] = ()  # URLPattern strings to block, such as "*://*.ads.test/*"
    bypass_anti_bot: bool | None = None  # None keeps the client's

    def overrides(self, config: CoreConfig) -> dict[str, Any]:
        """Check every option against its ceiling; return what to pass the client.

        Only what differs from the client's own default is returned.

        Raises:
            RefusedOption: Naming the option and the fix.
        """
        if self.engine not in ENGINES:
            raise RefusedOption(f"engine must be 'shell' or 'full', got {self.engine!r}.")
        if not 0 <= self.wait_ms <= config.max_wait_ms:
            raise RefusedOption(
                f"wait_ms must be between 0 and {config.max_wait_ms}, got {self.wait_ms}."
            )
        if self.timeout_ms is not None and not (
            MIN_TIMEOUT_MS <= self.timeout_ms <= config.max_timeout_ms
        ):
            raise RefusedOption(
                f"timeout_ms must be between {MIN_TIMEOUT_MS} and {config.max_timeout_ms}, "
                f"got {self.timeout_ms}."
            )
        if self.wait_until is not None and self.wait_until not in WAIT_MODES:
            raise RefusedOption(
                f"wait_until must be 'load' or 'domcontentloaded', got {self.wait_until!r}."
            )
        if len(self.headers) > MAX_HEADERS:
            raise RefusedOption(
                f"headers holds {len(self.headers)}; send at most {MAX_HEADERS} extra headers."
            )
        if sum(len(k) + len(v) for k, v in self.headers.items()) > MAX_HEADER_BYTES:
            raise RefusedOption(f"headers are over {MAX_HEADER_BYTES} bytes together; send fewer.")
        if len(self.block_urls) > MAX_BLOCKS:
            raise RefusedOption(
                f"block_urls holds {len(self.block_urls)}; block at most {MAX_BLOCKS} patterns."
            )
        try:
            # The library's own validators: forbidden headers, and URLPatterns Chrome can parse.
            FetchConfig(extra_headers=dict(self.headers), block_urls=list(self.block_urls))
        except ValidationError as ve:
            raise RefusedOption(
                "; ".join(e["msg"].removeprefix("Value error, ") for e in ve.errors())
            ) from ve
        wanted: dict[str, Any] = {}
        if self.wait_ms:
            wanted["wait_after_ms"] = self.wait_ms
        if self.timeout_ms is not None:
            wanted["timeout_ms"] = self.timeout_ms
        if self.wait_until is not None:
            wanted["wait_until"] = self.wait_until
        if self.headers:
            wanted["extra_headers"] = dict(self.headers)
        if self.block_urls:
            wanted["block_urls"] = list(self.block_urls)
        if self.bypass_anti_bot is not None:
            wanted["bypass_anti_bot"] = self.bypass_anti_bot
        return wanted


@dataclass(frozen=True)
class ShotOptions:
    """The image knobs a caller may set on `ServerCore.screenshot` and `fetch_all`."""

    full_page: bool = False  # capture the whole scrollable page, not just the viewport
    format: str = "png"  # "png", "jpeg" or "webp"
    quality: int | None = None  # 0-100, for jpeg and webp
    viewport: tuple[int, int] | None = None  # (width, height); `screenshot` only

    def overrides(self, *, viewport_allowed: bool) -> dict[str, Any]:
        """Check every image option; return what to pass the client.

        Args:
            viewport_allowed: Whether this call takes a viewport. `fetch_all` does not, since
                the page it returns would be captured at that size too.

        Raises:
            RefusedOption: Naming the option and the fix.
        """
        if self.format not in IMAGE_FORMATS:
            raise RefusedOption(f"format must be 'png', 'jpeg' or 'webp', got {self.format!r}.")
        if self.quality is not None and not 0 <= self.quality <= 100:
            raise RefusedOption(f"quality must be between 0 and 100, got {self.quality}.")
        if self.viewport is not None:
            if not viewport_allowed:
                raise RefusedOption(
                    "fetch_all takes no viewport; use screenshot for a chosen size."
                )
            if not all(1 <= side <= MAX_VIEWPORT for side in self.viewport):
                raise RefusedOption(
                    f"viewport must be (width, height), each between 1 and {MAX_VIEWPORT}, "
                    f"got {self.viewport}."
                )
        wanted: dict[str, Any] = {}
        if self.full_page:
            wanted["full_page"] = True
        if self.format != "png":
            wanted["format"] = self.format
        if self.quality is not None:
            wanted["quality"] = self.quality
        if self.viewport is not None:
            wanted["viewport"] = tuple(self.viewport)
        return wanted


PAGE_KEY: Final = "onyxweb_page"
PAGE_VERSION: Final = 1  # what a selection answers with; bump when a view changes meaning
ALWAYS: Final = ("final_url", "status_code", "anti_bot")  # in every selection, named or not


def _records(bucket: str) -> Callable[[RenderResult], Any]:
    """A bucket as a view: every record as a plain dict, which is `Bucket.asdict`."""
    return lambda page: getattr(page, bucket).asdict()


# Name -> that view of a page as JSON-ready data, encoded as `RenderResult.snapshot` encodes it
# where the snapshot has it. Every public view of `RenderResult` is here but those C14 names in
# `NOT_SELECTABLE`, so a view the library gains is one row. A view is read only when named.
VIEWS: Final[Mapping[str, Callable[[RenderResult], Any]]] = {
    "final_url": lambda page: page.final_url,
    "status_code": lambda page: page.status_code,
    "anti_bot": lambda page: asdict(page.anti_bot) if page.anti_bot else None,
    "title": lambda page: page.title,
    "published": lambda page: asdict(found) if (found := page.published) else None,
    "main_text": lambda page: page.main_text,
    "text": lambda page: page.text,
    "html": lambda page: page.html,
    "elapsed_s": lambda page: page.elapsed_s,
    "errors": lambda page: page.errors,
    "console_messages": lambda page: [asdict(m) for m in page.console_messages],
    "metadata": lambda page: asdict(page.metadata),
    "headers": lambda page: {
        "pairs": [list(pair) for pair in page.headers.pairs],
        "raw": page.headers.raw,
        "hashes": asdict(page.headers.hashes),
    },
    **{bucket: _records(bucket) for bucket in PAGE_BUCKETS},
}


def _selection(select: Sequence[str]) -> tuple[str, ...]:
    """The views a selection answers with: `ALWAYS` and those named, in `VIEWS` order.

    The order is the registry's, not the caller's, so the same names give the same bytes.

    Raises:
        RefusedOption: Naming each name that is no view, and every name that is.
    """
    unknown = [name for name in dict.fromkeys(select) if name not in VIEWS]
    if unknown:
        raise RefusedOption(
            f"select holds {', '.join(map(repr, unknown))}, which name no view of a page; "
            f"choose from {', '.join(VIEWS)}."
        )
    named = {*ALWAYS, *select}
    return tuple(name for name in VIEWS if name in named)


@dataclass(frozen=True)
class Selected:
    """The views of one page a caller selected: what `fetch` and `batch` answer with `select`.

    Plain data rather than a page, so it cannot be loaded back as one; it is measured, and sent,
    as the JSON it is.

    Attributes:
        data: `PAGE_KEY`, then each view by name in `VIEWS` order, `ALWAYS` among them.
        size: Bytes of `data` as JSON, which is what the size limit measures.
    """

    data: dict[str, Any]
    size: int

    @classmethod
    def of(cls, page: RenderResult, views: Sequence[str]) -> Selected:
        """Read `views` of `page`; a view not named is never computed."""
        data = {PAGE_KEY: PAGE_VERSION, **{name: VIEWS[name](page) for name in views}}
        return cls(data, len(dumps(data, ensure_ascii=False).encode()))


def _select_each(items: Sequence[RenderResult | Exception], views: Sequence[str]) -> list[Any]:
    """A batch's pages as their selections; a failure stays in its place."""
    return [Selected.of(i, views) if isinstance(i, RenderResult) else i for i in items]


def check_url(url: str) -> None:
    """Refuse a URL the server must not fetch.

    Args:
        url: The URL an agent asked for.

    Raises:
        RefusedUrl: If the scheme is not http or https, the URL has no host or carries
            credentials, or the host resolves to any address that is not public.

    Security:
        Redirects and DNS rebinding are not covered; a public URL may redirect to a private
        one. `egress.EgressProxy` closes both, by checking every address the browser reaches.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise RefusedUrl(f"{url!r}: only http and https URLs are fetched; pass a public URL.")
    if not parts.hostname:
        raise RefusedUrl(f"{url!r} has no host; pass a full URL such as https://example.com/.")
    if parts.username is not None or parts.password is not None:
        raise RefusedUrl(f"{url!r} carries credentials; remove user:pass@ from the URL.")
    try:
        addresses = resolve_host(parts.hostname)
    except OSError as oe:
        raise RefusedUrl(
            f"cannot resolve host {parts.hostname!r}; pass a reachable public host."
        ) from oe
    for address in addresses:
        if not is_public(address):
            raise RefusedUrl(
                f"{url!r} points at a private or internal address ({address}); "
                "only public addresses are fetched."
            )


def _where(url: str) -> str:
    """Host and path of `url`, for a log line or a message: no credentials, query or fragment."""
    try:
        parts = urlsplit(url)
        return f"{parts.hostname or '?'}{parts.path}"
    except ValueError:
        return "?"


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
            TooLarge: If the page alone is over the store's byte limit.
        """
        size = len(page.html.encode())
        if size > self._max_bytes:
            raise TooLarge(
                f"the page is {size} bytes, over the store's {self._max_bytes}; "
                "raise ONYXWEB_SERVER_MAX_STORE_BYTES, or fetch a smaller page."
            )
        digest = sha256(f"{page.final_url}\n{page.html}".encode()).hexdigest()
        page_id = f"p{digest[:10]}"
        self._pages.pop(page_id, None)
        self._pages[page_id] = (page, monotonic(), size)
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
        now = monotonic()
        return [(i, p, now - t) for i, (p, t, _) in reversed(self._pages.items())]


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

    def session(self) -> Any:
        """Open a session: fetches that share one tab, and so share what the first left in it."""

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
        self._config = config or CoreConfig()
        self._make = make
        # Only clients the pool builds itself go through the proxy; a caller's own are theirs.
        self._egress = egress if make is None else None
        self._clients: dict[str, BrowserClient] = {}
        self._building = Lock()
        self.restarts = 0  # clients replaced because their Chrome died

    def _build(self, engine: str) -> BrowserClient:
        """A real client for `engine`, pointed at the egress proxy when there is one."""
        proxied: dict[str, Any] = {}
        if self._egress is not None:
            # Chrome skips a proxy for loopback and link-local unless told not to. A proxy
            # carries TCP only, so WebRTC's own UDP would go around it to any address a page
            # names: Chrome is told to send no UDP it cannot proxy, which is none. Full Chrome
            # reads the first switch and the headless shell the second (measured); each
            # ignores the one it does not know.
            proxied = {
                "proxy": self._egress.url,
                "proxy_bypass_list": "<-loopback>",
                "chrome_args": [
                    "--webrtc-ip-handling-policy=disable_non_proxied_udp",
                    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                ],
            }
        return onyxweb.AsyncClient(
            concurrency=self._config.concurrency,
            queue_timeout_ms=self._config.queue_ms,
            engine=engine,
            bypass_anti_bot=True,  # wait out a challenge page; a call can pass False to skip it
            **proxied,
        )

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
                self._clients[engine] = await to_thread(self._make or self._build, engine)
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


class _EgressWatch:
    """Whether the egress proxy refused a navigation since this watch was taken.

    A refused plain-HTTP navigation comes back as the proxy's own marked 403; a refused HTTPS
    one only as a tunnel failure, which any broken connection also gives, so that counts only
    when the proxy has refused something in the meantime.
    """

    def __init__(self, proxy: EgressProxy | None) -> None:
        self._proxy = proxy
        self._before = proxy.refusals if proxy is not None else 0

    def refused(self, item: object) -> bool:
        """Whether the proxy refused the navigation behind `item`, a result or an error."""
        if isinstance(item, FetchResult):
            item = item.html
        if isinstance(item, RenderResult):
            return item.headers.get(REFUSED_HEADER) == "refused"
        return (
            isinstance(item, onyxweb.OnyxwebError)
            and self._proxy is not None
            and self._proxy.refusals > self._before
            and "ERR_TUNNEL_CONNECTION_FAILED" in str(item)
        )

    @staticmethod
    def refusal(url: str) -> RefusedUrl:
        """The refusal to raise for a navigation the proxy stopped."""
        return RefusedUrl(
            f"fetching {_where(url)} led the browser to a private or internal address; "
            "only public addresses are fetched."
        )


class _Call:
    """One core operation: it counts the call, times it, sizes its result and logs one line.

    A failure raised inside the ``with`` is counted and logged by `__exit__`; `done` sizes the
    result, and for a batch counts the failures it returns in place rather than raises. Only
    host and path reach the log (`label`), never a query string, credentials or a message.
    """

    def __init__(self, core: ServerCore, op: str, label: str, engine: str, count: int = 1) -> None:
        self._core = core
        self._op = op
        self._label = label
        self._engine = engine
        self._count = count
        self._started = 0.0
        self._summary = ""

    def __enter__(self) -> Self:
        self._started = monotonic()
        self._core._requests += self._count
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        failure: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        elapsed = f"{(monotonic() - self._started) * 1000:.0f}ms"
        head = f"{self._op} {self._label} engine={self._engine}"
        if failure is None:
            log.info(f"{head} ok in {elapsed}{self._summary}")
            return
        if not isinstance(failure, Exception):
            return  # a cancellation is not the call failing, and is nobody's to count
        why = self._why(failure)
        self._core._failures[why] += 1
        log.warning(f"{head} failed {why} in {elapsed}")

    @staticmethod
    def _why(failure: BaseException) -> str:
        """What a failure is counted under: a `Refused` code, a browser kind, else its type."""
        return str(
            getattr(failure, "code", None)
            or getattr(failure, "kind", None)
            or type(failure).__name__
        )

    def note(self, detail: str) -> None:
        """Say something more in this call's log line, when its result is not a page or image."""
        self._summary = detail

    def done(self, result: T) -> T:
        """Size-check `result`, note what the log line says, and return it.

        A batch is checked item by item: one too large becomes its `TooLarge` in place, and
        every failure it carries is counted here, since none of them reaches `__exit__`.

        Raises:
            TooLarge: If a single result's page or image is over the limit.
        """
        one: object = result  # narrows where the TypeVar cannot
        if not isinstance(one, list):
            self._check(one)
            if isinstance(one, bytes):
                self._summary = f" {len(one)} bytes"
            elif isinstance(one, RenderResult | FetchResult):
                self._summary = f" status={one.status_code}"
            elif isinstance(one, Selected):
                self._summary = f" status={one.data['status_code']}"
            return result
        items: list[Any] = []
        for item in one:
            try:
                self._check(item)
            except TooLarge as big:
                item = big
            if isinstance(item, Exception):
                self._core._failures[self._why(item)] += 1
            items.append(item)
        self._summary = f" {sum(not isinstance(i, Exception) for i in items)}/{len(items)} ok"
        return cast(T, items)

    def _check(self, result: object) -> None:
        """Refuse a result over the limit, measured as it is sent.

        A page is its html, an image its bytes, and `fetch_all` carries both, so both are
        checked; a selection is its JSON, whatever the page it came from weighs.
        """
        if isinstance(result, FetchResult):
            self._under(len(result.html.html.encode()), "page")
            self._under(len(result.png), "image")
        elif isinstance(result, RenderResult):
            self._under(len(result.html.encode()), "page")
        elif isinstance(result, bytes):
            self._under(len(result), "image")
        elif isinstance(result, Selected):
            self._under(result.size, "answer", "select fewer views")

    def _under(self, size: int, what: str, fix: str = "fetch a smaller page") -> None:
        if size > (limit := self._core._config.max_page_bytes):
            raise TooLarge(
                f"the {what} is {size} bytes, over the {limit} limit; "
                f"{fix}, or raise ONYXWEB_SERVER_MAX_PAGE_BYTES."
            )


@dataclass
class _HeldSession:
    """One caller's session id: the tab it is holding, and the browser that tab lives in."""

    session: Any  # a `Session` from the browser client: an `AsyncSession`, or a fake in tests
    client: BrowserClient  # the browser that built it; the session ends when this one does
    engine: str  # which engine built it; a session cannot move between engines
    last_used: float  # monotonic seconds, for the idle TTL


def _session_ended(url: str) -> onyxweb.ChromeExitedError:
    """What a session's call for `url` is answered with once its browser is gone.

    The library names a browser's exit itself when it happens under a fetch. One that exits while a
    session idles is replaced by the pool before the session's tab is tried again, and all the
    library can then say of that tab is "Client is closed", with no cause. The cause is known here.
    """
    exited = onyxweb.ChromeExitedError(
        "the browser this session's tab lived in exited, so the session is closed; "
        "its id opens a new session on the next call"
    )
    exited.kind = "chrome_exited"
    exited.url = url
    return exited


class ServerCore:
    """What every front-end shares: guarded fetching, held pages and browser clients.

    A front-end reaches the browser only through these calls, so the URL guard, the option
    ceilings and the size limits cannot be skipped. None of them holds a page; a front-end that
    wants one kept under an id calls `hold`. The caller can never pass scripts or actions:
    `FetchOptions` has no field for them.
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
        self._requests = 0  # calls made, counting one per URL of a batch
        self._failures: Counter[str] = Counter()  # by cause, as `_Call` names it
        self._guard = url_guard
        self._store = PageStore(config.max_pages, config.max_store_bytes)
        if egress is None and config.egress and make_client is None:
            egress = EgressProxy()
        self._egress = egress if make_client is None else None
        self._pool = ClientPool(make_client, config, self._egress)
        self._retries = 0
        # Session id -> the tab it holds. A session leases a tab from the pool for as long as it
        # lives, so it is bounded by both `max_sessions` and the pool's own concurrency.
        self._sessions: dict[str, _HeldSession] = {}
        # One call at a time through admission: opening a session awaits a Chrome launch, and the
        # count it checked must still be true when it inserts, or the cap and the id both race.
        self._admission = Lock()
        self._reaper: Task[None] | None = None

    async def _expire_sessions(self, now: float) -> None:
        """Close every session that has idled past the TTL, releasing its tab."""
        idle = self._config.session_ttl_ms / 1000
        for sid in [s for s, held in self._sessions.items() if now - held.last_used > idle]:
            await self._close_session(sid)

    async def _close_session(self, sid: str) -> bool:
        """Close `sid` and release its tab; False when it was not open."""
        held = self._sessions.pop(sid, None)
        if held is None:
            return False
        await held.session.close()
        return True

    async def close_session(self, sid: str) -> bool:
        """Close the session `sid`, returning its tab to the pool.

        False means no such session was open, which a caller may read as already closed.
        """
        return await self._close_session(sid)

    async def _session_for(self, sid: str, engine: str) -> _HeldSession:
        """The session `sid` owns, opened on first use and refreshed on every call.

        Admission is one call at a time, because opening a session launches Chrome: the count
        checked before that await must still hold when the session is inserted, or four concurrent
        first requests open four sessions past a cap of two, and two first uses of one id open two
        tabs under it.

        Raises:
            RefusedSession: When `max_sessions` are already open, or the id is unusable. The
                message names the way out, because the caller has to close one to get in.
        """
        if not sid:
            raise RefusedOption("session must not be empty; give it an id of your own.")
        if len(sid) > 200:
            raise RefusedOption(f"session is {len(sid)} characters; use at most 200.")
        await self._expire_sessions(monotonic())
        async with self._admission:
            held = self._sessions.get(sid)
            if held is not None and held.engine != engine:
                # A tab belongs to the engine that built it, so this id starts over on the new one.
                await self._close_session(sid)
                held = None
            if held is None:
                holding = self.session_count()
                if holding >= self._config.max_sessions:
                    raise RefusedSession(
                        f"{holding} sessions are open, the most this server keeps "
                        f"(ONYXWEB_SERVER_MAX_SESSIONS). Close one, or wait "
                        f"{self._config.session_ttl_ms} ms for an idle one to expire."
                    )
                self._start_reaper()
                client = await self._pool.get(engine)
                held = _HeldSession(client.session(), client, engine, last_used=monotonic())
                self._sessions[sid] = held
            held.last_used = monotonic()
            return held

    def _start_reaper(self) -> None:
        """Start the task that expires idle sessions: once per core, with the first session."""
        if self._reaper is None:
            # A quarter of the TTL, so a session is gone within a quarter-TTL of idling out; a
            # 50 ms floor keeps a tiny TTL from spinning, a 5 s ceiling keeps a long one prompt.
            interval = max(0.05, min(self._config.session_ttl_ms / 4000, 5.0))
            self._reaper = create_task(self._reap(interval))

    async def _reap(self, interval: float) -> None:
        """Close sessions idle past the TTL, whether or not a caller comes back.

        Expiring only when a session request arrives lets a caller that dies mid-search pin its
        tab forever, because sessionless traffic never runs the sweep.
        """
        while True:
            await sleep(interval)
            await self._expire_sessions(monotonic())

    def session_count(self) -> int:
        """How many sessions hold a tab.

        A session whose browser is gone holds none, so it is not counted, though its id is kept
        until its caller is told (on its next call), it is closed, or it idles out.
        """
        return sum(1 for held in self._sessions.values() if held.client.alive)

    @overload
    async def fetch(
        self,
        url: str,
        options: FetchOptions | None = None,
        select: None = None,
        session: str | None = None,
    ) -> RenderResult: ...

    @overload
    async def fetch(
        self,
        url: str,
        options: FetchOptions | None = None,
        *,
        select: Sequence[str],
        session: str | None = None,
    ) -> Selected: ...

    async def fetch(
        self,
        url: str,
        options: FetchOptions | None = None,
        select: Sequence[str] | None = None,
        session: str | None = None,
    ) -> RenderResult | Selected:
        """Fetch `url` in a real browser and return the page, or the views of it selected.

        Holds nothing either way.

        Args:
            url: The URL to fetch; the guard refuses anything that is not public http(s).
            options: What the caller may ask of the fetch; see `FetchOptions`.
            select: Names in `VIEWS` to answer with instead of the page, as `Selected`. The
                `ALWAYS` views come with any selection, so an empty one is the verdict alone.
            session: An id of the caller's own. Naming one fetches on that session's tab, which
                carries what the first fetch left in it — a cookie, a `localStorage` entry — so a
                link whose next step depends on the page above it can be followed. It leases a tab
                out of the pool for as long as it lives, so it costs one of the tabs.

        Raises:
            Refused: `RefusedOption` for an option outside its ceiling or a name that is no
                view, `RefusedUrl` from the guard (nothing is built or requested first),
                `RefusedSession` for a session over the cap, or `TooLarge` for a page, or a
                selection, over the size limit.
        """
        options = options or FetchOptions()
        with _Call(self, "fetch", _where(url), options.engine) as call:
            wanted = options.overrides(self._config)
            views = None if select is None else _selection(select)
            if session is None:
                page = await self._run(url, options, lambda c: c.fetch(url, **wanted))
            else:
                held = await self._session_for(session, options.engine)
                page = await self._session_fetch(session, held, url, options, wanted)
            if views is None:
                return call.done(page)
            return call.done(await to_thread(Selected.of, page, views))

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
            Refused: `RefusedOption`, `RefusedUrl` or `TooLarge`, as for `fetch`.
        """
        options = options or FetchOptions()
        with _Call(self, "screenshot", _where(url), options.engine) as call:
            wanted = options.overrides(self._config)
            for name in ("block_urls", "bypass_anti_bot"):
                if wanted.pop(name, None) is not None:
                    raise RefusedOption(f"screenshot takes no {name}; use fetch_all, or remove it.")
            wanted |= (shot or ShotOptions()).overrides(viewport_allowed=True)
            return call.done(await self._run(url, options, lambda c: c.screenshot(url, **wanted)))

    async def fetch_all(
        self, url: str, options: FetchOptions | None = None, shot: ShotOptions | None = None
    ) -> FetchResult:
        """Fetch `url` and screenshot it from one visit; the page and the image, holding nothing.

        Raises:
            Refused: `RefusedOption`, `RefusedUrl` or `TooLarge`, as for `fetch`.
        """
        options = options or FetchOptions()
        with _Call(self, "fetch_all", _where(url), options.engine) as call:
            wanted = options.overrides(self._config)
            wanted |= (shot or ShotOptions()).overrides(viewport_allowed=False)
            return call.done(await self._run(url, options, lambda c: c.fetch_all(url, **wanted)))

    @overload
    async def batch(
        self,
        urls: Sequence[str],
        options: FetchOptions | None = None,
        select: None = None,
        *,
        session: str | None = None,
    ) -> list[RenderResult | Exception]: ...

    @overload
    async def batch(
        self,
        urls: Sequence[str],
        options: FetchOptions | None = None,
        *,
        select: Sequence[str],
        session: str | None = None,
    ) -> list[Selected | Exception]: ...

    async def batch(
        self,
        urls: Sequence[str],
        options: FetchOptions | None = None,
        select: Sequence[str] | None = None,
        *,
        session: str | None = None,
    ) -> list[Any]:
        """Fetch many URLs at once; each result is its page, or the failure in its place.

        A URL the guard refuses is returned as its `RefusedUrl` and never reaches the browser.

        Args:
            urls: 1 to `CoreConfig.max_batch` URLs.
            options: Applied to every URL.
            select: As for `fetch`: each page becomes its `Selected`, measured on its own.
            session: As for `fetch`. Every URL of the batch runs on that one session's tab, so
                they take turns rather than fanning out across the pool.

        Raises:
            RefusedOption: If there are too few or too many URLs, an option is outside its
                ceiling, or a name is no view. Nothing is built or requested first.
            RefusedSession: If the session cap is reached, naming the way out.
        """
        options = options or FetchOptions()
        with _Call(self, "batch", f"{len(urls)} urls", options.engine, len(urls)) as call:
            wanted = options.overrides(self._config)
            views = None if select is None else _selection(select)
            held = None if session is None else await self._session_for(session, options.engine)
            if not 1 <= len(urls) <= self._config.max_batch:
                raise RefusedOption(
                    f"urls holds {len(urls)}; send at least 1 and at most "
                    f"{self._config.max_batch} (ONYXWEB_SERVER_MAX_BATCH)."
                )

            def verdict(url: str) -> Refused | None:
                """The refusal this URL earns, or None; a caller's guard may raise any Refused."""
                try:
                    self._guard(url)
                except Refused as refusal:
                    return refusal
                return None

            watch = _EgressWatch(self._egress)
            verdicts = await gather(*(to_thread(verdict, url) for url in urls))
            allowed = [url for url, refusal in zip(urls, verdicts, strict=True) if refusal is None]
            fetched = await self._batch(allowed, options, wanted, held, session) if allowed else []
            pending = iter(fetched)
            results: list[RenderResult | Exception] = []
            for url, refusal in zip(urls, verdicts, strict=True):
                item = refusal if refusal is not None else next(pending)
                results.append(watch.refusal(url) if watch.refused(item) else item)
            if views is None:
                return call.done(results)
            return call.done(await to_thread(_select_each, results, views))

    async def _batch(
        self,
        urls: list[str],
        options: FetchOptions,
        wanted: dict[str, Any],
        held: _HeldSession | None = None,
        sid: str | None = None,
    ) -> list[Any]:
        """Fetch every allowed URL, giving each one whose Chrome died a second try.

        With `held`, every URL runs on that session's tab, one after another: a session is a run of
        fetches that share one tab, so a batch cannot fan out across the pool within it. When that
        tab's browser is gone the session ends, and each URL left is answered with the exit under
        its own URL rather than tried on a fresh tab, which would hold none of what the session's
        pages stored and could answer a page that looks right and is not.
        """
        config = FetchConfig(**wanted)
        if held is not None and sid is not None:
            out: list[Any] = []
            for url in urls:
                if not held.client.alive:
                    out.append(await self._session_failure(sid, held, url, None))
                    continue
                try:
                    out.append(await held.session.fetch(url, config=config))
                except (onyxweb.OnyxwebError, TimeoutError) as err:
                    # A failure is returned in its place, as a batch always does.
                    out.append(await self._session_failure(sid, held, url, err))
            return out
        client = await self._pool.get(options.engine)
        fetched = await client.batch(urls, capture="html", config=config)
        died = [i for i, item in enumerate(fetched) if isinstance(item, onyxweb.ChromeExitedError)]
        if died:
            self._retries += 1
            client = await self._pool.get(options.engine)
            again = await client.batch([urls[i] for i in died], capture="html", config=config)
            for i, item in zip(died, again, strict=True):
                fetched[i] = item
        return fetched

    async def _run(
        self,
        url: str,
        options: FetchOptions,
        do: Callable[[BrowserClient], Awaitable[T]],
        retry: bool = True,
    ) -> T:
        """Run `do` on the engine's client once the guard accepts `url`.

        A Chrome that dies mid-call is replaced and the call made once more; nothing else is
        retried, since a fetch is idempotent but a timeout would only repeat. `retry=False` is for
        a session fetch, whose tab cannot come back: a replacement tab has none of the state the
        session was holding, so a retry could answer a page that looks right and is not.
        A navigation the egress proxy refused (a redirect, or a start URL on a private address)
        is `RefusedUrl`.
        """
        await to_thread(self._guard, url)
        watch = _EgressWatch(self._egress)
        try:
            client = await self._pool.get(options.engine)
            try:
                result = await do(client)
            except onyxweb.ChromeExitedError:
                if not retry:
                    raise
                self._retries += 1
                result = await do(await self._pool.get(options.engine))
        except onyxweb.OnyxwebError as err:
            if watch.refused(err):
                raise watch.refusal(url) from err
            raise
        if watch.refused(result):
            raise watch.refusal(url)
        return result

    async def _session_fetch(
        self, sid: str, held: _HeldSession, url: str, options: FetchOptions, wanted: dict[str, Any]
    ) -> RenderResult:
        """Fetch `url` on a session's tab; a session whose browser is gone ends, and says so.

        There is no retry on a fresh tab: it holds none of what the session's pages stored, so a
        retry could answer a page that looks right and is not. The caller decides whether to start
        over, and the id opens a new session when it does.
        """
        try:
            return await self._run(
                url, options, lambda _client: held.session.fetch(url, **wanted), retry=False
            )
        except (onyxweb.OnyxwebError, TimeoutError) as err:
            failure = await self._session_failure(sid, held, url, err)
            if failure is err:
                raise
            raise failure from err

    async def _session_failure(
        self, sid: str, held: _HeldSession, url: str, failure: Exception | None
    ) -> Exception:
        """What a session's failed call is answered with, ending it if its browser is gone.

        While the browser lives the failure is the page's own, and the session goes on. Once it is
        gone the session is closed and the answer is the browser's exit: the library's own when it
        named one, otherwise the one this core can name (`failure` is None for a URL never tried).
        """
        if failure is not None and held.client.alive:
            return failure
        if self._sessions.get(sid) is held:  # not a newer session a concurrent call opened
            await self._close_session(sid)
        if failure is not None and isinstance(failure, onyxweb.ChromeExitedError):
            return failure
        return _session_ended(url)

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
            "egress_refusals": self._egress.refusals if self._egress is not None else 0,
            "sessions": self.session_count(),
            "max_sessions": self._config.max_sessions,
        }

    async def aclose(self) -> None:
        """Stop reaping, close every session, then every browser client."""
        if self._reaper is not None:
            self._reaper.cancel()
            with suppress(CancelledError):
                await self._reaper
            self._reaper = None
        for sid in list(self._sessions):
            await self._close_session(sid)
        await self._pool.aclose()
