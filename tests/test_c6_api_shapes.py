"""C6 API shapes — one input through any call shape gives the same result.

Sync and async methods share one Rust implementation per operation, and the
module-level functions, ``batch`` and ``fetch_all`` wrap the same capture. So
``SHAPES`` fetches one local page through every shape and compares each result
to ``Client.fetch``'s: HTML, title, status, headers, cookies, console,
post-load results, body hashes and anti-bot verdict. A shape that returns a
``FetchResult`` must also forward its fields and carry a viewport-sized image.
A result saved to a snapshot and loaded again is one more shape: it reads the same
without Chrome. The fixture keeps a ``Client``, an ``AsyncClient`` and the module's
default clients open together, so every row also runs beside other clients.

``PARALLEL`` drives one slow page through threads, ``asyncio.gather`` and
``batch``; the server counts requests in flight, which must reach, and never
pass, the client's ``concurrency``.
"""

from __future__ import annotations

import asyncio
import struct
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Literal

import onyxweb
import pytest
from conftest import PNG_MAGIC, reloaded
from onyxweb.records import PAGE_BUCKETS
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Request, Response

VIEWPORT = (1200, 800)  # ViewportConfig default, so every screenshot is this size
SLOW_S = 0.3  # server delay on the parallel page, so requests overlap
DEFAULT_CONCURRENCY = 16  # ClientConfig default, used by the module-level clients

# The console text is assembled at runtime, so the script source can't supply it.
_PAGE = (
    "<html><head><title>Shape Page</title></head><body><p>one</p><p>two</p>"
    "<script>console.error('SHAPE_' + 'ERROR')</script></body></html>"
)
CONFIG = onyxweb.FetchConfig(
    post_load_scripts=["document.title + '/' + document.querySelectorAll('p').length"]
)

Result = onyxweb.RenderResult | onyxweb.FetchResult | bytes | Exception


@dataclass(frozen=True)
class Clients:
    """The two client classes, open side by side."""

    sync: onyxweb.Client
    aio: onyxweb.AsyncClient


@dataclass
class Gauge:
    """Counts requests the slow page is serving at once."""

    now: int = 0
    peak: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def handle(self, _request: Request) -> Response:
        with self.lock:
            self.now += 1
            self.peak = max(self.peak, self.now)
        try:
            time.sleep(SLOW_S)
        finally:
            with self.lock:
                self.now -= 1
        return Response("<html><body>SLOW_PAGE</body></html>", content_type="text/html")


@pytest.fixture(scope="module")
def gauge() -> Gauge:
    return Gauge()


@pytest.fixture(scope="module")
def server(gauge: Gauge) -> Iterator[HTTPServer]:
    # Threaded, so slow requests from parallel tabs overlap on the server.
    with HTTPServer(threaded=True) as s:
        s.expect_request("/shape").respond_with_response(
            Response(
                _PAGE,
                content_type="text/html",
                headers=[("X-Bw", "shape"), ("Set-Cookie", "sid=xyz; Path=/")],
            )
        )
        s.expect_request("/slow").respond_with_handler(gauge.handle)
        yield s


@pytest.fixture(scope="module")
def clients() -> Iterator[Clients]:
    sync = onyxweb.Client(concurrency=1)
    aio = onyxweb.AsyncClient(concurrency=1)
    try:
        yield Clients(sync, aio)
    finally:
        sync.close()
        asyncio.run(aio.aclose())


def _signature(r: onyxweb.RenderResult) -> dict[str, object]:
    """Everything a fetch reports that doesn't vary between two fetches of one page."""
    m = r.metadata
    return {
        "html": r.html,
        "title": r.title,
        "status": (r.status_code, m.status_code, m.status_text),
        "urls": (r.final_url, m.final_url, m.request_url),
        "headers": {k: v for k, v in r.headers.items() if k.lower() != "date"},
        "cookies": r.headers.cookies,
        "console": [(msg.type, msg.text) for msg in r.console_messages],
        "errors": r.errors,
        "post_load_results": r.post_load_results,
        "body": (m.content_length, m.body_hashes),
        "anti_bot": r.anti_bot,
        # What the page holds, read through every bucket, the text and search.
        "text": r.text,
        "overview": repr(r.overview()),
        "buckets": {name: getattr(r, name).asdict() for name in PAGE_BUCKETS},
        "search": {name: b.asdict() for name, b in r.search("SHAPE").items()},
    }


@pytest.fixture(scope="module")
def baseline(clients: Clients, server: HTTPServer) -> dict[str, object]:
    """``Client.fetch`` of the page — the result every shape must reproduce."""
    r = clients.sync.fetch(server.url_for("/shape"), config=CONFIG)
    # Positive controls: the signature carries each thing a shape could drop.
    assert r.title == "Shape Page"
    assert r.headers["x-bw"] == "shape"
    assert r.headers.cookies == {"sid": "xyz"}
    assert [(m.type, m.text) for m in r.console_messages] == [("error", "SHAPE_ERROR")]
    assert r.post_load_results == ["Shape Page/2"]
    return _signature(r)


def _png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == PNG_MAGIC
    width, height = struct.unpack(">II", data[16:24])  # IHDR
    return width, height


async def _ready(value: Result) -> Result:
    return value


async def _only(results: Awaitable[list[Result]]) -> Result:
    [one] = await results
    return one


# Shape -> (call, result type). Each call takes the clients, the page URL and CONFIG.
Call = Callable[[Clients, str, onyxweb.FetchConfig], Awaitable[Result]]
SHAPES: dict[str, tuple[Call, type]] = {
    "client_fetch": (
        lambda c, url, cfg: _ready(c.sync.fetch(url, config=cfg)),
        onyxweb.RenderResult,
    ),
    "client_fetch_all": (
        lambda c, url, cfg: _ready(c.sync.fetch_all(url, config=cfg)),
        onyxweb.FetchResult,
    ),
    "client_batch_html": (
        lambda c, url, cfg: _ready(c.sync.batch([url], config=cfg)[0]),
        onyxweb.RenderResult,
    ),
    "client_batch_both": (
        lambda c, url, cfg: _ready(c.sync.batch([url], capture="both", config=cfg)[0]),
        onyxweb.FetchResult,
    ),
    "client_batch_generator": (
        lambda c, url, cfg: _ready(c.sync.batch((u for u in [url]), config=cfg)[0]),
        onyxweb.RenderResult,
    ),
    "client_batch_tuple": (
        lambda c, url, cfg: _ready(c.sync.batch((url,), config=cfg)[0]),
        onyxweb.RenderResult,
    ),
    "snapshot_round_trip": (
        lambda c, url, cfg: _ready(reloaded(c.sync.fetch(url, config=cfg))),
        onyxweb.RenderResult,
    ),
    "async_client_fetch": (
        lambda c, url, cfg: c.aio.fetch(url, config=cfg),
        onyxweb.RenderResult,
    ),
    "async_client_fetch_all": (
        lambda c, url, cfg: c.aio.fetch_all(url, config=cfg),
        onyxweb.FetchResult,
    ),
    "async_client_batch_html": (
        lambda c, url, cfg: _only(c.aio.batch([url], config=cfg)),
        onyxweb.RenderResult,
    ),
    "async_client_batch_both": (
        lambda c, url, cfg: _only(c.aio.batch([url], capture="both", config=cfg)),
        onyxweb.FetchResult,
    ),
    "module_fetch": (
        lambda c, url, cfg: _ready(onyxweb.fetch(url, config=cfg)),
        onyxweb.RenderResult,
    ),
    "module_fetch_all": (
        lambda c, url, cfg: _ready(onyxweb.fetch_all(url, config=cfg)),
        onyxweb.FetchResult,
    ),
    "module_afetch": (
        lambda c, url, cfg: onyxweb.afetch(url, config=cfg),
        onyxweb.RenderResult,
    ),
    "module_afetch_all": (
        lambda c, url, cfg: onyxweb.afetch_all(url, config=cfg),
        onyxweb.FetchResult,
    ),
}


@pytest.mark.parametrize("name", list(SHAPES))
async def test_shape_matches_client_fetch(
    clients: Clients, server: HTTPServer, baseline: dict[str, object], name: str
) -> None:
    call, returns = SHAPES[name]
    out = await call(clients, server.url_for("/shape"), CONFIG)
    assert isinstance(out, returns), repr(out)
    if isinstance(out, onyxweb.FetchResult):
        page = out.html
        # A FetchResult forwards its page's fields rather than keeping copies.
        assert (out.errors, out.console_messages) == (page.errors, page.console_messages)
        assert (out.final_url, out.status_code) == (page.final_url, page.status_code)
        assert out.elapsed_s == page.elapsed_s
        assert out.metadata is page.metadata and out.headers is page.headers
        assert out.anti_bot == page.anti_bot
        assert _png_size(out.png) == VIEWPORT
        out = page
    assert isinstance(out, onyxweb.RenderResult)
    assert _signature(out) == baseline


# Image-only shapes -> call; each returns the image bytes.
ImageCall = Callable[[Clients, str], Awaitable[Result]]
IMAGE_SHAPES: dict[str, ImageCall] = {
    "client_screenshot": lambda c, url: _ready(c.sync.screenshot(url)),
    "client_batch_png": lambda c, url: _ready(c.sync.batch([url], capture="png")[0]),
    "async_client_screenshot": lambda c, url: c.aio.screenshot(url),
    "async_client_batch_png": lambda c, url: _only(c.aio.batch([url], capture="png")),
    "module_screenshot": lambda c, url: _ready(onyxweb.screenshot(url)),
    "module_ascreenshot": lambda c, url: onyxweb.ascreenshot(url),
}


@pytest.mark.parametrize("name", list(IMAGE_SHAPES))
async def test_image_shape_returns_a_viewport_png(
    clients: Clients, server: HTTPServer, name: str
) -> None:
    out = await IMAGE_SHAPES[name](clients, server.url_for("/shape"))
    assert isinstance(out, bytes), repr(out)
    assert _png_size(out) == VIEWPORT


@pytest.mark.parametrize("aio", [False, True], ids=["client", "async_client"])
async def test_batch_edges(clients: Clients, server: HTTPServer, aio: bool) -> None:
    """An empty batch is an empty list; an unknown capture mode names the valid ones."""
    url = server.url_for("/shape")
    names_the_modes = r"capture must be 'html'\|'png'\|'both'"
    if aio:
        assert await clients.aio.batch([]) == []
        with pytest.raises(ValueError, match=names_the_modes):
            await clients.aio.batch([url], capture="invalid")  # type: ignore[arg-type]
    else:
        assert clients.sync.batch([]) == []
        with pytest.raises(ValueError, match=names_the_modes):
            clients.sync.batch([url], capture="invalid")  # type: ignore[arg-type]


@dataclass(frozen=True)
class Parallel:
    """Calls driven at once through one client, and how."""

    drive: Literal["threads", "gather", "batch", "abatch", "module_gather"]
    calls: int
    concurrency: int = DEFAULT_CONCURRENCY
    workers: int = 0  # Python threads, for the "threads" drive
    one_fails: bool = False  # one call goes to a refused port

    @property
    def peak(self) -> int:
        """Requests in flight at once: the callers, capped by the pool."""
        callers = self.workers if self.drive == "threads" else self.calls
        return min(callers, self.calls - self.one_fails, self.concurrency)


PARALLEL: dict[str, Parallel] = {
    "threads_1_on_1": Parallel("threads", calls=2, concurrency=1, workers=1),
    "threads_4_on_4": Parallel("threads", calls=8, concurrency=4, workers=4),
    # Eight overlapping fetches from eight threads: the GIL is released while one runs.
    "threads_8_on_8": Parallel("threads", calls=8, concurrency=8, workers=8),
    "threads_16_capped_at_4": Parallel("threads", calls=16, concurrency=4, workers=16),
    "threads_one_failure": Parallel("threads", 12, concurrency=4, workers=12, one_fails=True),
    "gather_4": Parallel("gather", calls=4, concurrency=4),
    "batch_4": Parallel("batch", calls=4, concurrency=4),
    "abatch_4": Parallel("abatch", calls=4, concurrency=4),
    "abatch_8_capped_at_4": Parallel("abatch", calls=8, concurrency=4),
    "module_afetch_gather_3": Parallel("module_gather", calls=3),
}


def _settle(fetch: Callable[[str], onyxweb.RenderResult], url: str) -> Result:
    try:
        return fetch(url)
    except Exception as e:  # a failed call is its exception, as in a batch
        return e


async def _drive(row: Parallel, urls: list[str]) -> list[Any]:
    if row.drive == "module_gather":
        return list(await asyncio.gather(*map(onyxweb.afetch, urls), return_exceptions=True))
    if row.drive in ("gather", "abatch"):
        async with onyxweb.AsyncClient(concurrency=row.concurrency) as ac:
            if row.drive == "abatch":
                return await ac.batch(urls)
            return list(await asyncio.gather(*map(ac.fetch, urls), return_exceptions=True))
    with onyxweb.Client(concurrency=row.concurrency) as c:
        if row.drive == "batch":
            return c.batch(urls)
        with ThreadPoolExecutor(max_workers=row.workers) as pool:
            return list(pool.map(lambda u: _settle(c.fetch, u), urls))


@pytest.mark.parametrize("name", list(PARALLEL))
async def test_parallel_calls_fill_the_pool_and_no_more(
    server: HTTPServer, gauge: Gauge, refused_url: str, name: str
) -> None:
    row = PARALLEL[name]
    urls = [server.url_for("/slow")] * row.calls
    if row.one_fails:
        urls[row.calls // 2] = refused_url
    gauge.peak = 0
    results = await _drive(row, urls)
    assert len(results) == row.calls
    for url, r in zip(urls, results, strict=True):
        if url == refused_url:
            assert isinstance(r, onyxweb.OnyxwebError), repr(r)
            assert r.kind == "cdp"
        else:
            assert isinstance(r, onyxweb.RenderResult), repr(r)
            assert "SLOW_PAGE" in r
    assert gauge.peak == row.peak


async def test_a_fetch_leaves_the_event_loop_free(clients: Clients, server: HTTPServer) -> None:
    """An awaited fetch yields the loop: other coroutines keep running meanwhile.

    Not a result comparison, so no table above can express it.
    """
    stamps: list[float] = []

    async def ticker() -> None:
        for _ in range(10):
            await asyncio.sleep(0.05)
            stamps.append(time.perf_counter())

    async def timed_fetch() -> float:
        await clients.aio.fetch(server.url_for("/slow"), wait_after_ms=300)
        return time.perf_counter()

    _, done = await asyncio.gather(ticker(), timed_fetch())
    # A blocked loop would let the ticks run only after the fetch returned.
    during = sum(stamp < done for stamp in stamps)
    assert during >= 5, f"only {during} of 10 ticks ran while the fetch was in flight"
