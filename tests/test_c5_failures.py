"""C5 failures — a failure cause maps to one exception, soon, with a tab left working.

``FAILURES`` names a cause and the exception a fetch must raise: ``TimeoutError``
for a timeout (deliberately not a ``RuntimeError``), ``onyxweb.OnyxwebError``
(a ``RuntimeError``) for everything else, each carrying ``.url`` and an exact
``.kind`` plus a message that says what went wrong. Every case then fetches again
on the same one-tab Client, which must work at once: a failure never wedges the
pool. ``BATCH_FAILURES`` checks that ``batch`` returns the same exception in
place, in every capture mode. ``SUBFRAME_FAILURES`` checks the inverse: a frame
that fails is not the page failing.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Literal

import onyxweb
import pytest
from onyxweb import Click
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Request, Response

TIMEOUT_MS = 700  # per-fetch timeout for the timing-out causes
SLACK_S = 0.75  # past the timeout, for the reset and error path
FAST_S = 1.0  # a cause that fails before navigating, or a healthy recovery fetch
REFUSED = "{refused}"  # stands in for the refused_url fixture


def _slow(_request: Request) -> Response:
    time.sleep(3)  # far past TIMEOUT_MS, so the navigation is still stuck when it fires
    return Response("<html><body>late</body></html>", content_type="text/html")


@pytest.fixture(scope="module")
def server() -> Iterator[HTTPServer]:
    # Threaded, so a stuck request doesn't block the recovery fetch behind it.
    with HTTPServer(threaded=True) as s:
        s.expect_request("/slow").respond_with_handler(_slow)
        s.expect_request("/ok").respond_with_data(
            "<html><body>ok</body></html>", content_type="text/html"
        )
        yield s


@pytest.fixture(scope="module")
def client() -> Iterator[onyxweb.Client]:
    with onyxweb.Client(concurrency=1) as c:
        yield c


def _url(target: str, server: HTTPServer, refused_url: str) -> str:
    """A row's target: a server path, the refused port, or a literal URL."""
    if target == REFUSED:
        return refused_url
    return server.url_for(target) if target.startswith("/") else target


@dataclass(frozen=True)
class Failure:
    """A fetch that must fail, and how."""

    target: str
    error: type[BaseException]
    kind: str
    says: tuple[str, ...] = ()  # message fragments; "{url}" is the fetched URL
    within_s: float = FAST_S
    kwargs: dict[str, Any] = field(default_factory=dict)
    client: dict[str, Any] = field(default_factory=dict)  # update_config kwargs, undone after
    busy_ms: int = 0  # another thread holds the only tab this long while the fetch waits


_STUCK = {"timeout_ms": TIMEOUT_MS}
FAILURES: dict[str, Failure] = {
    # Rejected before the tab is touched; the message names the fix.
    "invalid_url_no_scheme_or_host": Failure(
        "not-a-url", onyxweb.OnyxwebError, "invalid_url", ("https://",)
    ),
    "invalid_url_bare_host": Failure(
        "example.com", onyxweb.OnyxwebError, "invalid_url", ("https://",)
    ),
    "invalid_url_empty_host": Failure("http://", onyxweb.OnyxwebError, "invalid_url"),
    # Reaches Chrome and fails in its network stack.
    "connection_refused": Failure(
        REFUSED, onyxweb.OnyxwebError, "cdp", ("ERR_CONNECTION_REFUSED",)
    ),
    # The message carries the URL, the awaited lifecycle event and the timeout.
    "navigation_stuck": Failure(
        "/slow",
        TimeoutError,
        "navigation_timeout",
        ("{url}", "load", str(TIMEOUT_MS)),
        within_s=TIMEOUT_MS / 1000 + SLACK_S,
        kwargs=_STUCK,
    ),
    # Loaded, but a post-load script outlives the timeout on a healthy tab.
    "post_load_script_stalls": Failure(
        "/ok",
        TimeoutError,
        "timeout",
        ("{url}", "reached load", "post_load_scripts", str(TIMEOUT_MS)),
        within_s=TIMEOUT_MS / 1000 + SLACK_S,
        kwargs={**_STUCK, "post_load_scripts": ["new Promise(r => setTimeout(r, 4000))"]},
    ),
    # A throwing script aborts the fetch and names its 0-based index.
    "post_load_script_throws": Failure(
        "/ok",
        onyxweb.OnyxwebError,
        "post_load_script",
        ("post_load_scripts[1]", "SECOND_THREW"),
        kwargs={"post_load_scripts": ["1 + 1", "throw new Error('SECOND_THREW')", "2 + 2"]},
    ),
    "post_load_script_throws_first": Failure(
        "/ok",
        onyxweb.OnyxwebError,
        "post_load_script",
        ("post_load_scripts[0]", "FIRST_THREW"),
        kwargs={"post_load_scripts": ["throw new Error('FIRST_THREW')"]},
    ),
    "post_load_script_rejects": Failure(
        "/ok",
        onyxweb.OnyxwebError,
        "post_load_script",
        ("ASYNC_THREW",),
        kwargs={"post_load_scripts": ["(async () => { throw new Error('ASYNC_THREW'); })()"]},
    ),
    "action_aborts": Failure(
        "/ok",
        onyxweb.OnyxwebError,
        "cdp",
        ("click(#nonexistent)",),
        kwargs={"actions": [Click(type="click", selector="#nonexistent", on_error="abort")]},
    ),
    # Has a scheme, so config validation passes; Chrome's parser refuses regexp groups.
    "block_url_chrome_cannot_parse": Failure(
        "/ok",
        onyxweb.OnyxwebError,
        "invalid_config",
        ("block_urls", "(\\d+)", "*://*.doubleclick.net/*"),
        kwargs={"block_urls": ["*://*/(\\d+)"]},
    ),
    # A client-level change forces the tab to be recreated, and Chrome refuses the pattern then.
    "tab_cannot_be_recreated": Failure(
        "/ok",
        onyxweb.OnyxwebError,
        "invalid_config",
        ("block_urls", "(\\d+)", "*://*.doubleclick.net/*"),
        client={"block_urls": ["*://*/(\\d+)"]},
    ),
    # The one tab is held longer than the queue timeout, so the fetch gives up waiting for it.
    "queue_wait_exceeds_the_timeout": Failure(
        "/ok",
        onyxweb.QueueTimeoutError,
        "queue_timeout",
        ("no tab was free", "300", "queue_timeout_ms"),
        within_s=0.3 + SLACK_S,
        client={"queue_timeout_ms": 300},
        busy_ms=1500,
    ),
}


def _check_failure(err: BaseException, row: Failure, url: str) -> None:
    """The exception a failure must be, whichever API surfaced it."""
    assert isinstance(err, row.error), f"{type(err).__name__}: {err}"
    # A timeout is its own builtin, so `except RuntimeError` doesn't swallow it.
    assert isinstance(err, RuntimeError) == (not issubclass(row.error, TimeoutError))
    assert err.url == url  # type: ignore[attr-defined]
    assert err.kind == row.kind  # type: ignore[attr-defined]
    for fragment in row.says:
        assert fragment.format(url=url) in str(err), str(err)


@pytest.mark.parametrize("name", list(FAILURES))
def test_fetch_failure(
    client: onyxweb.Client, server: HTTPServer, refused_url: str, name: str
) -> None:
    """Each cause raises its exception in time, and the tab serves the next fetch."""
    row = FAILURES[name]
    url = _url(row.target, server, refused_url)
    before = client.config.snapshot()
    client.update_config(**row.client)
    holder: threading.Thread | None = None
    if row.busy_ms:
        seen = len(server.log)
        holder = threading.Thread(
            target=client.fetch,
            args=(server.url_for("/ok"),),
            kwargs={"post_load_scripts": [f"new Promise(r => setTimeout(r, {row.busy_ms}))"]},
        )
        holder.start()
        while len(server.log) == seen:  # its request has arrived, so it holds the tab
            time.sleep(0.01)
    started = time.perf_counter()
    try:
        with pytest.raises(BaseException) as exc:
            client.fetch(url, **row.kwargs)
        elapsed = time.perf_counter() - started
    finally:
        if holder is not None:
            holder.join()
        client.update_config(config=before)  # a no-op when the row changed nothing
    _check_failure(exc.value, row, url)
    assert elapsed < row.within_s, f"failed after {elapsed:.2f} s"
    started = time.perf_counter()
    assert "ok" in client.fetch(server.url_for("/ok"))
    assert time.perf_counter() - started < FAST_S, "the tab was left wedged"


Capture = Literal["html", "png", "both"]
_ITEM_TYPE: dict[Capture, type] = {
    "html": onyxweb.RenderResult,
    "png": bytes,
    "both": onyxweb.FetchResult,
}


@pytest.mark.parametrize("capture", ["html", "png", "both"])
@pytest.mark.parametrize(
    "name", ["invalid_url_no_scheme_or_host", "connection_refused", "navigation_stuck"]
)
def test_batch_returns_failures_in_place(
    server: HTTPServer, refused_url: str, name: str, capture: Capture
) -> None:
    """``batch`` never raises: a failed URL is its exception, at its own position."""
    row = FAILURES[name]
    ok, bad = server.url_for("/ok"), _url(row.target, server, refused_url)
    config = onyxweb.FetchConfig(timeout_ms=TIMEOUT_MS)
    with onyxweb.Client(concurrency=3) as batcher:
        results = batcher.batch([ok, bad, ok], capture=capture, config=config)
    assert isinstance(results[0], _ITEM_TYPE[capture])
    assert isinstance(results[2], _ITEM_TYPE[capture])
    assert isinstance(results[1], BaseException)
    _check_failure(results[1], row, bad)


def test_screenshot_rejects_an_invalid_url_fast(client: onyxweb.Client) -> None:
    """The screenshot path validates the URL as fetch does."""
    started = time.perf_counter()
    with pytest.raises(onyxweb.OnyxwebError) as exc:
        client.screenshot("not-a-url")
    assert time.perf_counter() - started < FAST_S
    _check_failure(exc.value, FAILURES["invalid_url_no_scheme_or_host"], "not-a-url")


_MAIN = "MAIN_DOCUMENT_CONTENT"
# Frame src -> extra response headers for the page. Real sites embed frames that fail
# routinely (blocked trackers, local-network checks, dead hosts).
SUBFRAME_FAILURES: dict[str, tuple[str, dict[str, str]]] = {
    "refused": (REFUSED, {}),
    "unresolvable_host": ("https://this-host-does-not-exist-onyxweb.invalid/", {}),
    "blocked_by_csp": ("/ok", {"Content-Security-Policy": "frame-src 'none'"}),
}


@pytest.mark.parametrize("name", list(SUBFRAME_FAILURES))
def test_subframe_failure_is_not_page_failure(
    client: onyxweb.Client, httpserver: HTTPServer, refused_url: str, name: str
) -> None:
    """``loadingFailed`` reports subframe documents too; only the main frame can fail a fetch."""
    src, headers = SUBFRAME_FAILURES[name]
    frame = (
        refused_url if src == REFUSED else (httpserver.url_for(src) if src.startswith("/") else src)
    )
    httpserver.expect_request("/ok").respond_with_data("<p>frame</p>", content_type="text/html")
    httpserver.expect_request("/").respond_with_data(
        f"<html><body><h1>{_MAIN}</h1><iframe src='{frame}'></iframe></body></html>",
        content_type="text/html",
        headers=headers,
    )
    r = client.fetch(httpserver.url_for("/"), wait_after_ms=500)
    assert r.status_code == 200
    assert _MAIN in r
