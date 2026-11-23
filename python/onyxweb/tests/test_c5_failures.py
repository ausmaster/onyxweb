"""C5 failures — a failure cause maps to one exception, soon, with a tab left working.

``FAILURES`` names a cause and the exception a fetch must raise: ``TimeoutError``
for a timeout (deliberately not a ``RuntimeError``), ``OnyxwebError``
(a ``RuntimeError``) for everything else, each carrying ``.url`` and an exact
``.kind`` plus a message that says what went wrong. Every case then fetches again
on the same one-tab Client, which must work at once: a failure never wedges the
pool. ``BATCH_FAILURES`` checks that ``batch`` returns the same exception in
place, in every capture mode. ``SUBFRAME_FAILURES`` checks the inverse: a frame
that fails is not the page failing.
"""

from __future__ import annotations

import ssl
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from threading import Thread
from time import perf_counter, sleep
from typing import Any, Literal

import pytest
from onyxweb import (
    Click,
    Client,
    FetchConfig,
    FetchResult,
    OnyxwebError,
    QueueTimeoutError,
    RenderResult,
)
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Request, Response

TIMEOUT_MS = 700  # per-fetch timeout for the timing-out causes
SLACK_S = 0.75  # past the timeout, for the reset and error path
FAST_S = 1.0  # a cause that fails before navigating, or a healthy recovery fetch
REFUSED = "{refused}"  # stands in for the refused_url fixture


def _slow(_request: Request) -> Response:
    sleep(3)  # far past TIMEOUT_MS, so the navigation is still stuck when it fires
    return Response("<html><body>late</body></html>", content_type="text/html")


@pytest.fixture(scope="module")
def server() -> Iterator[HTTPServer]:
    # Threaded, so a stuck request doesn't block the recovery fetch behind it.
    with HTTPServer(threaded=True) as s:
        s.expect_request("/slow").respond_with_handler(_slow)
        s.expect_request("/ok").respond_with_data(
            "<html><body>ok</body></html>", content_type="text/html"
        )
        # A redirect that only ever points at itself: Chrome gives up on the loop.
        s.expect_request("/loop").respond_with_response(
            Response(status=302, headers={"Location": "/loop"})
        )
        yield s


@pytest.fixture(scope="module")
def client() -> Iterator[Client]:
    with Client(concurrency=1) as c:
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
        "not-a-url", OnyxwebError, "invalid_url", ("https://",)
    ),
    "invalid_url_bare_host": Failure(
        "example.com", OnyxwebError, "invalid_url", ("https://",)
    ),
    "invalid_url_empty_host": Failure("http://", OnyxwebError, "invalid_url"),
    # Reaches Chrome and fails in its network stack: each cause the browser names gets its
    # own kind, so a caller branches on what happened rather than on "browser trouble".
    "connection_refused": Failure(
        REFUSED, OnyxwebError, "connection_refused", ("nothing accepted a connection",)
    ),
    "a_host_that_does_not_resolve": Failure(
        "https://this-host-does-not-exist-onyxweb.invalid/",
        OnyxwebError,
        "dns_failure",
        ("cannot resolve",),
        within_s=5.0,
    ),
    "a_redirect_that_only_points_at_itself": Failure(
        "/loop", OnyxwebError, "redirect_loop", ("redirected too many times",), within_s=5.0
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
        OnyxwebError,
        "post_load_script",
        ("post_load_scripts[1]", "SECOND_THREW"),
        kwargs={"post_load_scripts": ["1 + 1", "throw new Error('SECOND_THREW')", "2 + 2"]},
    ),
    "post_load_script_throws_first": Failure(
        "/ok",
        OnyxwebError,
        "post_load_script",
        ("post_load_scripts[0]", "FIRST_THREW"),
        kwargs={"post_load_scripts": ["throw new Error('FIRST_THREW')"]},
    ),
    "post_load_script_rejects": Failure(
        "/ok",
        OnyxwebError,
        "post_load_script",
        ("ASYNC_THREW",),
        kwargs={"post_load_scripts": ["(async () => { throw new Error('ASYNC_THREW'); })()"]},
    ),
    "action_aborts": Failure(
        "/ok",
        OnyxwebError,
        "cdp",
        ("click(#nonexistent)",),
        kwargs={"actions": [Click(type="click", selector="#nonexistent", on_error="abort")]},
    ),
    # Has a scheme, so config validation passes; Chrome's parser refuses regexp groups.
    "block_url_chrome_cannot_parse": Failure(
        "/ok",
        OnyxwebError,
        "invalid_config",
        ("block_urls", "(\\d+)", "*://*.doubleclick.net/*"),
        kwargs={"block_urls": ["*://*/(\\d+)"]},
    ),
    # A client-level change forces the tab to be recreated, and Chrome refuses the pattern then.
    "tab_cannot_be_recreated": Failure(
        "/ok",
        OnyxwebError,
        "invalid_config",
        ("block_urls", "(\\d+)", "*://*.doubleclick.net/*"),
        client={"block_urls": ["*://*/(\\d+)"]},
    ),
    # The one tab is held longer than the queue timeout, so the fetch gives up waiting for it.
    "queue_wait_exceeds_the_timeout": Failure(
        "/ok",
        QueueTimeoutError,
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
    client: Client, server: HTTPServer, refused_url: str, name: str
) -> None:
    """Each cause raises its exception in time, and the tab serves the next fetch."""
    row = FAILURES[name]
    url = _url(row.target, server, refused_url)
    before = client.config.snapshot()
    client.update_config(**row.client)
    holder: Thread | None = None
    if row.busy_ms:
        seen = len(server.log)
        holder = Thread(
            target=client.fetch,
            args=(server.url_for("/ok"),),
            kwargs={"post_load_scripts": [f"new Promise(r => setTimeout(r, {row.busy_ms}))"]},
        )
        holder.start()
        while len(server.log) == seen:  # its request has arrived, so it holds the tab
            sleep(0.01)
    started = perf_counter()
    try:
        with pytest.raises(BaseException) as exc:
            client.fetch(url, **row.kwargs)
        elapsed = perf_counter() - started
    finally:
        if holder is not None:
            holder.join()
        client.update_config(config=before)  # a no-op when the row changed nothing
    _check_failure(exc.value, row, url)
    assert elapsed < row.within_s, f"failed after {elapsed:.2f} s"
    started = perf_counter()
    assert "ok" in client.fetch(server.url_for("/ok"))
    assert perf_counter() - started < FAST_S, "the tab was left wedged"


Capture = Literal["html", "png", "both"]
_ITEM_TYPE: dict[Capture, type] = {
    "html": RenderResult,
    "png": bytes,
    "both": FetchResult,
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
    config = FetchConfig(timeout_ms=TIMEOUT_MS)
    with Client(concurrency=3) as batcher:
        results = batcher.batch([ok, bad, ok], capture=capture, config=config)
    assert isinstance(results[0], _ITEM_TYPE[capture])
    assert isinstance(results[2], _ITEM_TYPE[capture])
    assert isinstance(results[1], BaseException)
    _check_failure(results[1], row, bad)


def test_screenshot_rejects_an_invalid_url_fast(client: Client) -> None:
    """The screenshot path validates the URL as fetch does."""
    started = perf_counter()
    with pytest.raises(OnyxwebError) as exc:
        client.screenshot("not-a-url")
    assert perf_counter() - started < FAST_S
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
    client: Client, httpserver: HTTPServer, refused_url: str, name: str
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


# Error statuses an origin can answer with and then send no body at all for. Chrome refuses
# to render such a response and fails the navigation, so the status must survive as the cause.
_EMPTY_STATUSES = {"not_found": 404, "too_many_requests": 429, "unavailable": 503}


@pytest.mark.parametrize(("name", "status"), list(_EMPTY_STATUSES.items()))
def test_an_empty_error_status_is_the_origins_answer(
    httpserver: HTTPServer, name: str, status: int
) -> None:
    """An error status with no body is ``http_status`` carrying the origin's code.

    New test: ``FAILURES`` drives one shell client for every cause, and only the full engine
    loses the status (the shell completes the navigation), so this needs a client of its own —
    and a positive control that the same status *with* a body is still an ordinary page.
    """
    httpserver.expect_request(f"/empty-{name}").respond_with_data(
        "", status=status, content_type="text/html"
    )
    httpserver.expect_request(f"/body-{name}").respond_with_data(
        "<p>a body</p>", status=status, content_type="text/html"
    )
    httpserver.expect_request("/ok").respond_with_data(
        "<html><body>ok</body></html>", content_type="text/html"
    )
    with Client(engine="full", concurrency=1) as client:
        with pytest.raises(OnyxwebError) as exc:
            client.fetch(httpserver.url_for(f"/empty-{name}"))
        err = exc.value
        assert not isinstance(err, TimeoutError)  # a refusal is not a timeout
        assert err.kind == "http_status"
        assert err.status_code == status  # the origin's own code
        assert err.url == httpserver.url_for(f"/empty-{name}")
        assert str(status) in str(err), str(err)
        # The same status with a body is the page the origin sent, however unwelcome.
        page = client.fetch(httpserver.url_for(f"/body-{name}"))
        assert (page.status_code, page.text.strip()) == (status, "a body")
        # A failed navigation leaves the tab serving.
        assert "ok" in client.fetch(httpserver.url_for("/ok"))


def test_the_shell_engine_renders_an_empty_error_status(httpserver: HTTPServer) -> None:
    """The shell finishes the navigation the full engine aborts, so it returns the empty page.

    New test: the difference is the engines', not onyxweb's, and a caller choosing ``shell``
    must see it stated — the status is still right, there is simply no ``http_status`` cause.
    """
    httpserver.expect_request("/empty").respond_with_data(
        "", status=429, content_type="text/html"
    )
    with Client(engine="shell", concurrency=1) as client:
        page = client.fetch(httpserver.url_for("/empty"))
    assert page.status_code == 429
    assert page.text == ""


def test_a_refused_certificate_is_not_a_browser_fault() -> None:
    """A certificate Chrome rejects is ``tls_failure``, naming the switch that allows it.

    New test: it needs an HTTPS server, which no other case here uses. It also pins the one
    setting that decides whether certificates are checked at all — the default trusts them
    (Chromium's own client does), and ``ignore_https_errors=False`` is what validates.
    """
    certs = Path(__file__).parent / "fixtures"
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certs / "self_signed_cert.pem", certs / "self_signed_key.pem")
    with HTTPServer(ssl_context=context) as secure:
        secure.expect_request("/").respond_with_data("<p>secret</p>", content_type="text/html")
        url = secure.url_for("/")
        with Client(concurrency=1) as trusting:  # the default: any certificate is accepted
            assert "secret" in trusting.fetch(url).text
        with Client(concurrency=1, ignore_https_errors=False) as checking:
            with pytest.raises(OnyxwebError) as exc:
                checking.fetch(url)
            err = exc.value
            assert err.kind == "tls_failure"
            assert err.url == url
            assert "ignore_https_errors" in str(err), str(err)  # names the switch
