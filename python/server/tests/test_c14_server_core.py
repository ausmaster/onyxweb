"""C14 server core — a core call, on any input and any limits, gives one outcome and every effect
of it on any front-end.

``CALLS`` is the contract: a row is one call (``fetch``, ``screenshot``, ``fetch_all`` or
``batch``) with its options, the limits in force and how the browser behaves, and it says what
comes back (a result, or a ``Refused`` with a stable code, or the browser's own failure). Every
row is also held to the effects any call has: which clients were built and which URLs reached them
with which overrides, that each call is counted once and each failure by its kind, that one line
is logged without a query string, and that nothing is held. ``SEQUENCES`` pin how clients are built
per engine and replaced when dead, ``EVICTIONS`` how held pages leave the store (by count and by
bytes), and ``ENV`` how ``ONYXWEB_SERVER_*`` sets the limits. A fake client stands in for the
browser, so nothing here launches Chrome.
"""

from __future__ import annotations

import logging
import os
from collections import Counter
from dataclasses import dataclass, field
from functools import partial
from typing import Any

import onyxweb
import pytest
from conftest import PUBLIC
from onyxweb.testing import FakeClient, FakeClientFactory
from onyxweb_server.core import (
    MAX_WAIT_MS,
    CoreConfig,
    FetchOptions,
    Refused,
    ServerCore,
    ShotOptions,
)

_SCHEME = "only http and https"
_ELSEWHERE = "http://93.184.216.35/"
_PRIVATE = "http://127.0.0.1/"
_ABSENT = object()  # a kwarg the client must not receive
_IMAGE_OF = {"fetch": "page", "screenshot": "image", "fetch_all": "both"}
# What a failure is counted under: a `Refused` by its code, a browser error by its kind.
_KEYS = {"ChromeExitedError": "chrome_exited", "OnyxwebError": "cdp"}
_OK = ("ok", "page", "image", "both")


def _body(size: int, char: str = "x") -> str:
    """Html whose body is `size` characters, so a page is `size` + 26 bytes of ASCII."""
    return f"<html><body>{char * size}</body></html>"


CDP_FAILED = onyxweb.OnyxwebError("CDP: boom")
CDP_FAILED.kind = "cdp"  # the browser tags its errors; the core counts failures by this


@dataclass(frozen=True)
class Call:
    """One core call, the world it runs in, and what must come of it."""

    op: str = "fetch"  # fetch | screenshot | fetch_all | batch
    urls: tuple[str, ...] = (PUBLIC,)  # one URL, or a batch's
    options: dict[str, Any] = field(default_factory=dict)  # FetchOptions
    shot: dict[str, Any] = field(default_factory=dict)  # ShotOptions
    config: dict[str, Any] = field(default_factory=dict)  # CoreConfig overrides
    pages: dict[str, str] = field(default_factory=dict)  # canned html by URL
    dies: int = 0  # Chrome deaths as clients start serving, across the whole call
    error: BaseException | None = None  # every browser call fails with this
    client_kwargs: dict[str, Any] | None = None  # build the real way; what the client is built with
    # What must come back: "ok", or a `Refused` code, or the class name of the browser's failure.
    expect: str = "ok"
    items: tuple[str, ...] = ()  # a batch's items in order: "page", a code or a class name
    says: tuple[str, ...] = ()  # fragments of a failure's message
    # Effects. `None` means the value follows from the outcome.
    builds: tuple[str, ...] | None = None  # engines built, in order
    fetched: tuple[str, ...] | None = None  # URLs that reached a browser
    overrides: dict[str, Any] | None = None  # what each of those calls carried; None: unchecked
    retries: int = 0
    log: tuple[str, ...] = ()  # fragments the log must carry
    silent: tuple[str, ...] = ()  # fragments it must never carry


def _refused(code: str, url: str = PUBLIC, **kw: Any) -> Call:
    return Call(urls=(url,), expect=code, **kw)


CALLS: dict[str, Call] = {
    # --- the guard, applied by the core before any browser work --------------------------------
    "a_local_server": _refused("refused_url", "http://127.0.0.1:8000/", says=("private",)),
    "a_file": _refused("refused_url", "file:///etc/passwd", says=(_SCHEME,)),
    "credentials_in_the_url": _refused(
        "refused_url", "http://user:pw@93.184.216.34/", says=("credentials",)
    ),
    # --- options outside their ceilings ---------------------------------------------------------
    "wait_over_the_ceiling": _refused(
        "refused_option", options={"wait_ms": 999_999}, says=("wait_ms", str(MAX_WAIT_MS))
    ),
    "wait_below_zero": _refused("refused_option", options={"wait_ms": -1}, says=("wait_ms", "0")),
    "unknown_engine": _refused(
        "refused_option", options={"engine": "turbo"}, says=("'shell'", "'full'")
    ),
    "timeout_over_the_ceiling": _refused(
        "refused_option", options={"timeout_ms": 999_999}, says=("timeout_ms", "60000")
    ),
    "timeout_too_short": _refused(
        "refused_option", options={"timeout_ms": 10}, says=("timeout_ms", "100")
    ),
    "unknown_wait_until": _refused(
        "refused_option",
        options={"wait_until": "idle"},
        says=("wait_until", "'load'", "'domcontentloaded'"),
    ),
    "too_many_headers": _refused(
        "refused_option",
        options={"headers": {f"X-{n}": "v" for n in range(21)}},
        says=("headers", "20"),
    ),
    "headers_too_long": _refused(
        "refused_option", options={"headers": {"X-Big": "v" * 9000}}, says=("headers", "8192")
    ),
    "a_forbidden_header": _refused(
        "refused_option", options={"headers": {"Cookie": "a=b"}}, says=("Cookie",)
    ),
    "too_many_blocks": _refused(
        "refused_option",
        options={"block_urls": [f"*://h{n}/*" for n in range(51)]},
        says=("block_urls", "50"),
    ),
    "a_block_without_a_scheme": _refused(
        "refused_option", options={"block_urls": ["*doubleclick*"]}, says=("block_urls", "scheme")
    ),
    # --- image options outside theirs -----------------------------------------------------------
    "screenshot_takes_no_blocks": Call(
        "screenshot",
        expect="refused_option",
        options={"block_urls": ["*://*.ads.test/*"]},
        says=("block_urls", "screenshot"),
    ),
    "screenshot_takes_no_anti_bot_switch": Call(
        "screenshot",
        expect="refused_option",
        options={"bypass_anti_bot": True},
        says=("bypass_anti_bot", "screenshot"),
    ),
    "an_unknown_format": Call(
        "screenshot",
        expect="refused_option",
        shot={"format": "tiff"},
        says=("'png', 'jpeg' or 'webp'",),
    ),
    "the_same_for_fetch_all": Call(
        "fetch_all",
        expect="refused_option",
        shot={"format": "tiff"},
        says=("'png', 'jpeg' or 'webp'",),
    ),
    "quality_over_100": Call(
        "screenshot",
        expect="refused_option",
        shot={"format": "jpeg", "quality": 101},
        says=("quality", "100"),
    ),
    "a_viewport_over_the_ceiling": Call(
        "screenshot",
        expect="refused_option",
        shot={"viewport": (5000, 100)},
        says=("viewport", "4096"),
    ),
    "a_viewport_of_zero": Call(
        "screenshot", expect="refused_option", shot={"viewport": (0, 100)}, says=("viewport", "1")
    ),
    "fetch_all_takes_no_viewport": Call(
        "fetch_all",
        expect="refused_option",
        shot={"viewport": (800, 600)},
        says=("fetch_all", "viewport"),
    ),
    "options_are_checked_for_an_image_too": Call(
        "screenshot", expect="refused_option", options={"wait_ms": -1}, says=("wait_ms",)
    ),
    # --- exactly the options a caller set reach the browser ------------------------------------
    "nothing_set_passes_nothing": Call(overrides={}),
    "the_settle": Call(options={"wait_ms": 700}, overrides={"wait_after_ms": 700}),
    "the_timeout": Call(options={"timeout_ms": 5000}, overrides={"timeout_ms": 5000}),
    "the_wait_mode": Call(
        options={"wait_until": "domcontentloaded"}, overrides={"wait_until": "domcontentloaded"}
    ),
    "headers_as_extra_headers": Call(
        options={"headers": {"X-A": "1"}}, overrides={"extra_headers": {"X-A": "1"}}
    ),
    "blocks": Call(
        options={"block_urls": ["*://*.ads.test/*"]}, overrides={"block_urls": ["*://*.ads.test/*"]}
    ),
    "anti_bot_off_is_still_a_choice": Call(
        options={"bypass_anti_bot": False}, overrides={"bypass_anti_bot": False}
    ),
    "everything_at_once": Call(
        options={
            "wait_ms": 1,
            "timeout_ms": 2000,
            "bypass_anti_bot": True,
            "headers": {"X-B": "2"},
        },
        overrides={
            "wait_after_ms": 1,
            "timeout_ms": 2000,
            "bypass_anti_bot": True,
            "extra_headers": {"X-B": "2"},
        },
    ),
    "a_screenshot_with_defaults": Call("screenshot", overrides={}),
    "a_screenshot_with_everything": Call(
        "screenshot",
        options={"wait_ms": 5, "timeout_ms": 900, "wait_until": "load", "headers": {"X-A": "1"}},
        shot={"full_page": True, "format": "jpeg", "quality": 40, "viewport": (800, 600)},
        overrides={
            "wait_after_ms": 5,
            "timeout_ms": 900,
            "wait_until": "load",
            "extra_headers": {"X-A": "1"},
            "full_page": True,
            "format": "jpeg",
            "quality": 40,
            "viewport": (800, 600),
        },
    ),
    "fetch_all_with_defaults": Call("fetch_all", overrides={}),
    "fetch_all_with_everything": Call(
        "fetch_all",
        options={"block_urls": ["*://*.ads.test/*"], "bypass_anti_bot": True, "wait_ms": 5},
        shot={"full_page": True, "format": "webp", "quality": 30},
        overrides={
            "block_urls": ["*://*.ads.test/*"],
            "bypass_anti_bot": True,
            "wait_after_ms": 5,
            "full_page": True,
            "format": "webp",
            "quality": 30,
        },
    ),
    # --- the size limit: a page or an image over it is refused ---------------------------------
    "a_page_under_the_cap": Call(config={"max_page_bytes": 1000}, pages={PUBLIC: _body(900)}),
    "a_page_over_the_cap": Call(
        config={"max_page_bytes": 1000},
        pages={PUBLIC: _body(1500)},
        expect="too_large",
        says=("ONYXWEB_SERVER_MAX_PAGE_BYTES", "page"),
    ),
    # Characters are not bytes: 400 of these are 1200 bytes.
    "the_cap_counts_bytes_not_characters": Call(
        config={"max_page_bytes": 1000},
        pages={PUBLIC: _body(400, "€")},
        expect="too_large",
        says=("ONYXWEB_SERVER_MAX_PAGE_BYTES",),
    ),
    # The fake's image is 18 bytes.
    "a_screenshot_under_the_cap": Call("screenshot", config={"max_page_bytes": 100}),
    "a_screenshot_over_the_cap": Call(
        "screenshot",
        config={"max_page_bytes": 10},
        expect="too_large",
        says=("ONYXWEB_SERVER_MAX_PAGE_BYTES", "image"),
    ),
    "fetch_all_under_the_cap": Call("fetch_all", config={"max_page_bytes": 100_000}),
    "fetch_alls_image_over_the_cap": Call(
        "fetch_all",
        config={"max_page_bytes": 10},
        pages={PUBLIC: "x"},
        expect="too_large",
        says=("image",),
    ),
    "fetch_alls_page_over_the_cap": Call(
        "fetch_all",
        config={"max_page_bytes": 30},
        pages={PUBLIC: "x" * 100},
        expect="too_large",
        says=("page",),
    ),
    # --- a batch: each URL in its place, the rest unaffected ------------------------------------
    "batch_results_come_back_in_order": Call(
        "batch", urls=(PUBLIC + "a", _ELSEWHERE, PUBLIC + "c"), items=("page",) * 3
    ),
    "a_refused_url_is_returned_in_place_and_never_fetched": Call(
        "batch",
        urls=(PUBLIC + "a", _PRIVATE + "x", PUBLIC + "c"),
        items=("page", "refused_url", "page"),
    ),
    # Not symmetric, so a result placed from the wrong end shows.
    "a_refusal_up_front_does_not_shift_the_rest": Call(
        "batch",
        urls=(_PRIVATE + "x", PUBLIC + "a", PUBLIC + "b"),
        items=("refused_url", "page", "page"),
    ),
    "a_page_over_the_cap_is_refused_in_place": Call(
        "batch",
        urls=(PUBLIC + "a", PUBLIC + "big"),
        items=("page", "too_large"),
        config={"max_page_bytes": 500},
        pages={PUBLIC + "big": _body(1000)},
    ),
    "the_options_reach_every_url": Call(
        "batch",
        urls=(PUBLIC + "a", PUBLIC + "b"),
        items=("page", "page"),
        options={"timeout_ms": 900, "headers": {"X-A": "1"}, "bypass_anti_bot": True},
        overrides={"timeout_ms": 900, "extra_headers": {"X-A": "1"}, "bypass_anti_bot": True},
    ),
    "every_url_refused_builds_no_client": Call(
        "batch", urls=(_PRIVATE + "a", "file:///etc/passwd"), items=("refused_url",) * 2
    ),
    "no_urls": Call("batch", urls=(), expect="refused_option", says=("urls", "at least 1")),
    "more_urls_than_allowed": Call(
        "batch",
        urls=tuple(PUBLIC + str(n) for n in range(4)),
        config={"max_batch": 3},
        expect="refused_option",
        says=("urls", "at most 3", "ONYXWEB_SERVER_MAX_BATCH"),
    ),
    "a_batch_option_outside_its_ceiling": Call(
        "batch", options={"timeout_ms": 999_999}, expect="refused_option", says=("timeout_ms",)
    ),
    # --- a Chrome that dies mid-call is replaced and the call retried once ----------------------
    **{
        f"a_chrome_that_dies_mid_{op}_is_retried_once": Call(
            op,
            urls=(PUBLIC + "a", PUBLIC + "b") if op == "batch" else (PUBLIC,),
            items=("page", "page") if op == "batch" else (),
            dies=1,
            builds=("shell", "shell"),
            retries=1,
        )
        for op in ("fetch", "screenshot", "fetch_all", "batch")
    },
    # One retry: a Chrome that keeps dying fails the call as `ChromeExitedError`.
    **{
        f"a_second_death_in_{op}_is_reported_not_retried_again": Call(
            op,
            urls=(PUBLIC + "a", PUBLIC + "b") if op == "batch" else (PUBLIC,),
            items=("ChromeExitedError",) * 2 if op == "batch" else (),
            expect="ok" if op == "batch" else "ChromeExitedError",
            dies=2,
            builds=("shell", "shell"),
            retries=1,
        )
        for op in ("fetch", "screenshot", "fetch_all", "batch")
    },
    # Only a dead Chrome is retried: a page that timed out would only time out again.
    "a_timeout_is_not_retried": Call(error=TimeoutError("slow"), expect="TimeoutError"),
    "a_browser_failure_is_counted_by_its_kind": Call(error=CDP_FAILED, expect="OnyxwebError"),
    # --- the log: one line, its operation, engine and outcome, and never a secret --------------
    "a_good_fetch_is_logged": Call(
        urls=(PUBLIC + "ok",), log=("fetch", "93.184.216.34/ok", "shell", "ok")
    ),
    "the_query_string_is_never_logged": Call(
        urls=(PUBLIC + "p?token=SECRET#frag",),
        log=("93.184.216.34/p",),
        silent=("SECRET", "token", "frag"),
    ),
    "credentials_are_never_logged": Call(
        urls=("http://bob:hunter2@93.184.216.34/x",),
        expect="refused_url",
        log=("refused_url", "93.184.216.34/x"),
        silent=("hunter2", "bob"),
    ),
    "a_failure_logs_its_kind_not_its_message": Call(
        urls=("http://127.0.0.1/secret?k=v",),
        expect="refused_url",
        log=("fetch", "refused_url"),
        silent=("k=v", "points at"),
    ),
    "the_engine_is_named": Call(
        "screenshot", options={"engine": "full"}, builds=("full",), log=("screenshot", "full")
    ),
    # --- built the real way: the client gets the core's limits and the proxy --------------------
    "the_default_limits": Call(
        options={"engine": "full"},
        config={"egress": False},
        client_kwargs={"engine": "full", "concurrency": 4, "queue_timeout_ms": 10_000},
    ),
    "a_tighter_queue": Call(
        config={"egress": False, "queue_ms": 250}, client_kwargs={"queue_timeout_ms": 250}
    ),
    "fewer_tabs": Call(
        config={"egress": False, "concurrency": 2}, client_kwargs={"concurrency": 2}
    ),
    # Loopback and link-local skip a proxy unless told not to, so the bypass list is mandatory.
    "egress_points_the_browser_at_the_proxy": Call(
        config={"egress": True},
        client_kwargs={
            "proxy": lambda url: url.startswith("http://127.0.0.1:") and url[-1].isdigit(),
            "proxy_bypass_list": "<-loopback>",
        },
    ),
    "no_egress_no_proxy": Call(
        config={"egress": False}, client_kwargs={"proxy": _ABSENT, "proxy_bypass_list": _ABSENT}
    ),
}


class _Mortal(FakeClient):
    """A fake whose Chrome dies as it starts serving, while its factory has deaths left."""

    def __init__(self, factory: _MortalFactory, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._factory = factory

    def _maybe_die(self) -> None:
        if self._factory.left > 0 and not self.fetched:
            self._factory.left -= 1
            self.die()

    async def fetch(self, url: str, **kwargs: Any) -> onyxweb.RenderResult:
        self._maybe_die()
        return await super().fetch(url, **kwargs)

    async def screenshot(self, url: str, **kwargs: Any) -> bytes:
        self._maybe_die()
        return await super().screenshot(url, **kwargs)

    async def fetch_all(self, url: str, **kwargs: Any) -> onyxweb.FetchResult:
        self._maybe_die()
        return await super().fetch_all(url, **kwargs)

    async def batch(self, urls: Any, **kwargs: Any) -> Any:
        urls = list(urls)
        self._maybe_die()
        return await super().batch(urls, **kwargs)


class _MortalFactory(FakeClientFactory):
    """Builds `_Mortal` clients that share one count of deaths."""

    def __init__(self, deaths: int, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.left = deaths  # deaths still to hand out, across every client it builds

    def __call__(self, engine: str) -> FakeClient:
        client = _Mortal(self, pages=self.pages, error=self.error)
        self.built.append((engine, client))
        return client


class _Recorder(FakeClient):
    """Stands in for ``onyxweb.AsyncClient``, appending itself to `built` with its kwargs."""

    def __init__(self, built: list[_Recorder], **kwargs: Any) -> None:
        super().__init__()
        self.kwargs = kwargs
        built.append(self)


def _token(item: object) -> str:
    """What an outcome is called in a row: a page, an image, both, a `Refused` code or a class."""
    if isinstance(item, Refused):
        return item.code
    if isinstance(item, Exception):
        return type(item).__name__
    return {
        onyxweb.RenderResult: "page",
        bytes: "image",
        onyxweb.FetchResult: "both",
    }.get(type(item), "list")


@pytest.mark.parametrize("name", list(CALLS))
async def test_a_core_call_gives_its_outcome_and_every_effect(
    name: str, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = CALLS[name]
    recorded: list[_Recorder] = []
    factory: FakeClientFactory | None = None
    if row.client_kwargs is not None:
        monkeypatch.setattr(onyxweb, "AsyncClient", partial(_Recorder, recorded))
    else:
        factory = (
            _MortalFactory(row.dies, pages=row.pages, error=row.error)
            if row.dies
            else FakeClientFactory(pages=row.pages, error=row.error)
        )
    core = ServerCore(factory, config=CoreConfig(**row.config))
    try:
        options, shot = FetchOptions(**row.options), ShotOptions(**row.shot)
        got: Any
        with caplog.at_level(logging.INFO, logger="onyxweb_server"):
            try:
                if row.op == "batch":
                    got = await core.batch(list(row.urls), options)
                else:
                    got = await getattr(core, row.op)(
                        row.urls[0], *([options] if row.op == "fetch" else [options, shot])
                    )
            except Exception as failure:  # the row says which failure it must be
                got = failure
        clients = (
            factory.built if factory is not None else [(c.kwargs["engine"], c) for c in recorded]
        )

        # The outcome.
        batch = row.op == "batch"
        if row.expect == "ok":
            assert isinstance(got, list) if batch else _token(got) == _IMAGE_OF[row.op], got
            tokens = tuple(_token(i) for i in got) if batch else ()
            assert tokens == row.items
        else:
            assert isinstance(got, Exception) and _token(got) == row.expect, got
            for fragment in row.says:
                assert fragment in str(got), str(got)
        if batch and row.expect == "ok":
            assert [getattr(i, "final_url", None) for i in got if _token(i) == "page"] == [
                u for u, t in zip(row.urls, row.items, strict=True) if t == "page"
            ], "a result landed in another URL's place"

        # Which clients were built, and which URLs reached them with what.
        tokens_for_work = row.items or (row.expect,)
        early = all(t in ("refused_url", "refused_option") for t in tokens_for_work)
        engine = row.options.get("engine", "shell")
        builds = row.builds if row.builds is not None else (() if early else (engine,))
        assert tuple(e for e, _ in clients) == builds, "clients built beyond what the row says"
        if row.fetched is not None:
            reached = row.fetched
        elif batch:
            reached = tuple(
                u for u, t in zip(row.urls, row.items, strict=False) if t != "refused_url"
            )
        else:
            reached = () if early else (row.urls[0],)
        calls = [entry for _, c in clients for entry in c.fetched]
        # A retry asks a browser for every one of them again.
        assert tuple(u for u, _ in calls) == reached * (1 + row.retries)
        if row.overrides is not None:
            assert all(o == row.overrides for _, o in calls), calls

        # Every call is counted once, and each failure under its kind.
        stats = core.stats()
        failing = row.items if batch and row.expect == "ok" else (row.expect,)
        assert stats["requests"] == (len(row.urls) if batch else 1)
        assert stats["failures"] == dict(Counter(_KEYS.get(t, t) for t in failing if t not in _OK))
        assert (stats["retries"], stats["egress_refusals"]) == (row.retries, 0)
        assert core.pages() == [], "a call held a page"

        # One or more lines are logged, with what the row wants and never what it forbids.
        text = "\n".join(r.getMessage() for r in caplog.records if r.name == "onyxweb_server")
        assert text, "nothing was logged"
        for fragment in row.log:
            assert fragment in text, text
        for fragment in row.silent:
            assert fragment not in text, text

        # How the real client is built.
        for key, value in (row.client_kwargs or {}).items():
            kwargs = clients[0][1].kwargs  # type: ignore[attr-defined]
            if value is _ABSENT:
                assert key not in kwargs, key
            elif callable(value):
                assert value(kwargs[key]), (key, kwargs[key])
            else:
                assert kwargs[key] == value, key
    finally:
        await core.aclose()


# --- clients across calls ---------------------------------------------------------------------

# Steps (an engine is a fetch through it; "die" kills the newest client) -> the engines built in
# order, the restarts, which clients ended closed, and the health after each step.
SEQUENCES: dict[
    str, tuple[tuple[str, ...], tuple[str, ...], int, tuple[bool, ...], list[dict[str, bool]]]
] = {
    "nothing_is_built_before_a_fetch": ((), (), 0, (), []),
    "one_client_per_engine_built_when_first_used": (
        ("shell", "shell", "full"),
        ("shell", "full"),
        0,
        (False, False),
        [{"shell": True}, {"shell": True}, {"shell": True, "full": True}],
    ),
    # A Chrome that died leaves its client useless; the next fetch gets a fresh one.
    "a_dead_client_is_replaced_and_closed_on_the_next_fetch": (
        ("shell", "die", "shell"),
        ("shell", "shell"),
        1,
        (True, False),
        [{"shell": True}, {"shell": False}, {"shell": True}],
    ),
}


@pytest.mark.parametrize("name", list(SEQUENCES))
async def test_clients_are_built_once_per_engine_and_replaced_when_dead(name: str) -> None:
    steps, built, restarts, closed, healths = SEQUENCES[name]
    factory = FakeClientFactory()
    core = ServerCore(factory)
    seen: list[dict[str, bool]] = []
    for step in steps:
        if step == "die":
            factory.built[-1][1].die()
        else:
            await core.fetch(PUBLIC, FetchOptions(engine=step))
        seen.append(core.health())
    assert tuple(factory.engines) == built
    assert core.stats()["restarts"] == restarts
    assert tuple(c.closed for _, c in factory.built) == closed
    assert seen == healths


# --- the pages held ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Eviction:
    """Pages held (or used) in order on a store, and the pages left."""

    cap: int
    ops: tuple[tuple[str, str], ...]  # ("hold" | "use", page name; "name@v2" is another capture)
    held: tuple[str, ...]
    max_bytes: int = 10**9
    sizes: dict[str, int] = field(default_factory=dict)  # page name -> bytes of padding
    refused: tuple[str, ...] = ()  # pages that must be refused as too large for the store
    by_argument: bool = False  # set the cap with `ServerCore(max_pages=...)`, not the config


EVICTIONS: dict[str, Eviction] = {
    "under_the_cap_keeps_everything": Eviction(
        3, (("hold", "alpha"), ("hold", "beta")), ("alpha", "beta")
    ),
    "the_oldest_goes_first": Eviction(
        2, (("hold", "alpha"), ("hold", "beta"), ("hold", "gamma")), ("beta", "gamma")
    ),
    "the_cap_can_be_an_argument": Eviction(
        2,
        (("hold", "alpha"), ("hold", "beta"), ("hold", "gamma")),
        ("beta", "gamma"),
        by_argument=True,
    ),
    "using_a_page_saves_it": Eviction(
        2,
        (("hold", "alpha"), ("hold", "beta"), ("use", "alpha"), ("hold", "gamma")),
        ("alpha", "gamma"),
    ),
    # One page held twice is one entry with one id, not two.
    "a_second_hold_is_a_use_not_growth": Eviction(
        2,
        (("hold", "alpha"), ("hold", "beta"), ("hold", "alpha"), ("hold", "gamma")),
        ("alpha", "gamma"),
    ),
    # A page whose content changed is a new capture with a new id; the old id keeps the old content.
    "an_id_names_one_capture": Eviction(
        5, (("hold", "alpha"), ("hold", "alpha@v2")), ("alpha", "alpha@v2")
    ),
    # The byte cap evicts as the count cap does: least recently used first.
    "bytes_evict_the_oldest": Eviction(
        10,
        (("hold", "alpha"), ("hold", "beta"), ("hold", "gamma")),
        ("beta", "gamma"),
        max_bytes=2500,
        sizes={"alpha": 1000, "beta": 1000, "gamma": 1000},
    ),
    "using_a_page_saves_it_from_the_byte_cap": Eviction(
        10,
        (("hold", "alpha"), ("hold", "beta"), ("use", "alpha"), ("hold", "gamma")),
        ("alpha", "gamma"),
        max_bytes=2500,
        sizes={"alpha": 1000, "beta": 1000, "gamma": 1000},
    ),
    "one_large_page_can_push_out_several": Eviction(
        10,
        (("hold", "alpha"), ("hold", "beta"), ("hold", "gamma")),
        ("gamma",),
        max_bytes=2500,
        sizes={"alpha": 500, "beta": 500, "gamma": 2000},
    ),
    "a_page_bigger_than_the_store_is_refused": Eviction(
        10,
        (("hold", "alpha"), ("hold", "beta")),
        ("alpha",),
        max_bytes=1500,
        sizes={"alpha": 500, "beta": 2000},
        refused=("beta",),
    ),
}


def _capture(name: str, pad: int = 0) -> onyxweb.RenderResult:
    return onyxweb.RenderResult(
        f"<html><body>{name.upper()}_BODY{'x' * pad}</body></html>",
        final_url=PUBLIC + name.split("@")[0],
    )


@pytest.mark.parametrize("name", list(EVICTIONS))
def test_the_store_holds_at_most_its_caps(name: str) -> None:
    row = EVICTIONS[name]
    limits = CoreConfig(max_pages=row.cap, max_store_bytes=row.max_bytes)
    core = (
        ServerCore(max_pages=row.cap, config=CoreConfig(max_store_bytes=row.max_bytes))
        if row.by_argument
        else ServerCore(config=limits)
    )
    ids: dict[str, str] = {}
    for op, page in row.ops:
        if op == "hold" and page in row.refused:
            with pytest.raises(Refused, match="ONYXWEB_SERVER_MAX_STORE_BYTES") as exc:
                core.hold(_capture(page, row.sizes.get(page, 0)))
            assert exc.value.code == "too_large"
        elif op == "hold":
            got = core.hold(_capture(page, row.sizes.get(page, 0)))
            assert ids.setdefault(page, got) == got, "the same page got a new id"
        else:
            core.page(ids[page])
    assert len(set(ids.values())) == len(ids), "two captures shared an id"
    assert not set(ids) & set(row.refused), "a refused page was held"
    listed = {page_id for page_id, _, _ in core.pages()}
    stats = core.stats()
    assert stats["held_pages"] == len(row.held)
    assert stats["held_bytes"] == sum(
        len(_capture(name, row.sizes.get(name, 0)).html.encode()) for name in row.held
    )
    for page, page_id in ids.items():
        assert (page_id in listed) == (page in row.held), (page, listed)
        if page in row.held:
            assert page.upper() in core.page(page_id).html
        else:
            with pytest.raises(ValueError, match="fetch the URL again"):
                core.page(page_id)


# --- limits through their entry paths ---------------------------------------------------------

# Environment (or, when empty, keyword arguments) -> the limits it sets, or the message refusing it.
ENV: dict[str, tuple[dict[str, str], dict[str, Any], dict[str, Any] | str]] = {
    "defaults": (
        {},
        {},
        {
            "max_pages": 50,
            "max_store_bytes": 256 * 1024 * 1024,
            "max_page_bytes": 20 * 1024 * 1024,
            "max_batch": 50,
            "max_wait_ms": 30_000,
            "max_timeout_ms": 60_000,
            "queue_ms": 10_000,
            "concurrency": 4,
            "egress": True,
        },
    ),
    "every_limit_can_be_set": (
        {
            "ONYXWEB_SERVER_MAX_PAGES": "7",
            "ONYXWEB_SERVER_MAX_STORE_BYTES": "1000",
            "ONYXWEB_SERVER_MAX_PAGE_BYTES": "500",
            "ONYXWEB_SERVER_MAX_BATCH": "3",
            "ONYXWEB_SERVER_MAX_WAIT_MS": "2000",
            "ONYXWEB_SERVER_MAX_TIMEOUT_MS": "9000",
            "ONYXWEB_SERVER_QUEUE_MS": "250",
            "ONYXWEB_SERVER_CONCURRENCY": "2",
        },
        {},
        {
            "max_pages": 7,
            "max_store_bytes": 1000,
            "max_page_bytes": 500,
            "max_batch": 3,
            "max_wait_ms": 2000,
            "max_timeout_ms": 9000,
            "queue_ms": 250,
            "concurrency": 2,
        },
    ),
    "egress_can_be_turned_off": ({"ONYXWEB_SERVER_EGRESS": "0"}, {}, {"egress": False}),
    "egress_says_what_it_takes": ({"ONYXWEB_SERVER_EGRESS": "maybe"}, {}, "ONYXWEB_SERVER_EGRESS"),
    "a_limit_of_zero": (
        {"ONYXWEB_SERVER_MAX_BATCH": "0"},
        {},
        "ONYXWEB_SERVER_MAX_BATCH must be at least 1, got 0",
    ),
    "a_store_needs_room_for_a_page": (
        {"ONYXWEB_SERVER_MAX_PAGES": "0"},
        {},
        "ONYXWEB_SERVER_MAX_PAGES must be at least 1",
    ),
    "not_a_number": (
        {"ONYXWEB_SERVER_QUEUE_MS": "soon"},
        {},
        "ONYXWEB_SERVER_QUEUE_MS must be a whole number, got 'soon'",
    ),
    "a_direct_limit_of_zero": ({}, {"max_pages": 0}, "max_pages must be at least 1, got 0"),
}


@pytest.mark.parametrize("name", list(ENV))
def test_the_limits_come_from_the_environment_or_arguments(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    variables, kwargs, expected = ENV[name]
    for var in [v for v in os.environ if v.startswith("ONYXWEB_SERVER_")]:
        monkeypatch.delenv(var)
    for var, value in variables.items():
        monkeypatch.setenv(var, value)

    def build() -> CoreConfig:
        return CoreConfig(**kwargs) if kwargs else CoreConfig.from_env()

    if isinstance(expected, str):
        with pytest.raises(ValueError, match=expected):
            build()
        return
    config = build()
    for attr, value in expected.items():
        assert getattr(config, attr) == value, attr
