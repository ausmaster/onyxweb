"""C4 isolation — a fetch after any earlier fetches gives what a fresh client gives.

Pooled tabs are reused, and a per-fetch setting changes the tab's CDP state: extra
headers, init scripts, blocked URLs, the Fetch domain for navigation blocking. Each
must be undone before the tab serves again. ``LEAKS`` runs earlier fetches with one
setting on a one-tab client, then a control fetch, and compares the control with
the same fetch on a fresh client: the requests the server saw, the beacons that
loaded, status, final URL, redirects, HTML, console and in-page state. Every row
also runs with the earlier fetch timing out, where the setting applies before
navigation, and checks the earlier fetch really differed from the fresh one.
"""

from __future__ import annotations

import json
import random
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import onyxweb
import pytest
from onyxweb import Click
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Request, Response

_LEAK_PAGE = (
    "<html><body><p id='state'>fresh</p>"
    "<img src='/beacon/leak.png'><img src='/beacon/client.png'>"
    "<button id='go' onclick=\"document.getElementById('state').textContent = 'clicked';"
    " location.href = '/elsewhere'\">go</button>"
    "<script>console.error('PAGE_' + 'LOG'); window.__visits = (window.__visits || 0) + 1;"
    "</script></body></html>"
)
# Read after load on every observed fetch: state a setting may have left behind.
_READ = (
    "({state: document.getElementById('state') && document.getElementById('state').textContent,"
    " visits: window.__visits || 0, leak: window.__leak || null})"
)
_MUTATE = "document.getElementById('state').textContent = 'MUTATED'; window.__leak = 'pls'"
TIMEOUT_MS = 400  # earlier fetch's budget when it must time out
SLOW_S = 1.0  # the slow page, past TIMEOUT_MS plus the tab reset
_IGNORED = ("/slow",)  # a timed-out request may finish during a later fetch


@dataclass
class Gauge:
    """Counts requests the /who page is serving at once."""

    now: int = 0
    peak: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


@pytest.fixture(scope="module")
def gauge() -> Gauge:
    return Gauge()


@pytest.fixture(scope="module")
def server(gauge: Gauge) -> Iterator[HTTPServer]:
    def slow(_request: Request) -> Response:
        time.sleep(SLOW_S)
        return Response("<html><body>late</body></html>", content_type="text/html")

    def who(request: Request) -> Response:
        with gauge.lock:
            gauge.now += 1
            gauge.peak = max(gauge.peak, gauge.now)
        time.sleep(0.3)  # long enough for the other fetch to arrive meanwhile
        with gauge.lock:
            gauge.now -= 1
        return Response(f"<html><body>{request.args['id']}</body></html>", content_type="text/html")

    # Threaded, so a slow or overlapping request doesn't hold up the next fetch.
    with HTTPServer(threaded=True) as s:
        s.expect_request("/leak").respond_with_data(_LEAK_PAGE, content_type="text/html")
        for beacon in ("leak", "client"):
            s.expect_request(f"/beacon/{beacon}.png").respond_with_data(b"")
        s.expect_request("/elsewhere").respond_with_data("<p>left</p>", content_type="text/html")
        s.expect_request("/timer").respond_with_data(
            "<html><body><script>setInterval(() => console.error('TIMER_' + 'LEAK'), 5)"
            "</script></body></html>",
            content_type="text/html",
        )
        s.expect_request("/redirect").respond_with_response(
            Response(status=302, headers={"Location": "/leak"})
        )
        s.expect_request("/slow").respond_with_handler(slow)
        s.expect_request("/who").respond_with_handler(who)
        yield s


def _observe(
    client: onyxweb.Client, server: HTTPServer, url: str, **kwargs: Any
) -> dict[str, object]:
    """Fetch and record everything a leftover setting could change."""
    seen = len(server.log)
    scripts = [*kwargs.pop("post_load_scripts", []), _READ]
    r = client.fetch(url, post_load_scripts=scripts, **kwargs)
    requests = [req for req, _ in server.log[seen:] if req.path not in _IGNORED]
    return {
        "requests": [
            (
                req.path,
                req.headers.get("X-Leak"),
                req.headers.get("X-Other"),
                req.headers.get("Referer"),
            )
            for req in requests
            if not req.path.startswith("/beacon/")
        ],
        "beacons": sorted(req.path for req in requests if req.path.startswith("/beacon/")),
        "status": r.status_code,
        "final_url": r.final_url,
        "redirects": [(hop.url, hop.status) for hop in r.metadata.redirect_chain],
        "html": r.html,
        "console": [(m.type, m.text) for m in r.console_messages],
        "js": r.post_load_results[-1],
    }


@dataclass(frozen=True)
class Leak:
    """Earlier fetches on one tab, then the control fetch of ``/leak`` + ``then``."""

    earlier: tuple[dict[str, Any], ...]  # fetch kwargs of each earlier fetch
    client: dict[str, Any] = field(default_factory=dict)
    path: str = "/leak"  # where the earlier fetches go
    then: str = ""  # appended to the control fetch's URL
    times_out: bool = True  # also run with each earlier fetch timing out


LEAKS: dict[str, Leak] = {
    "extra_headers": Leak(({"extra_headers": {"X-Leak": "call"}},)),
    "header_over_the_clients": Leak(
        ({"extra_headers": {"X-Leak": "call"}},), client={"extra_headers": {"X-Leak": "client"}}
    ),
    "referer": Leak(({"extra_headers": {"Referer": "http://foo.bar/leak"}},)),
    "clients_referer_after_a_per_call_header": Leak(
        ({"extra_headers": {"X-Other": "call"}},),
        client={"extra_headers": {"Referer": "http://foo.bar/base"}},
    ),
    "one_script": Leak(({"scripts": ["window.__leak = 'script'; console.error('LEAK_SCRIPT')"]},)),
    "three_scripts": Leak(
        ({"scripts": ["console.error('LEAK_" + tag + "')" for tag in "ABC"]},),
    ),
    "different_scripts_in_turn": Leak(
        tuple({"scripts": [f"window.__leak = 'turn{turn}'"]} for turn in range(3))
    ),
    "block_urls": Leak(({"block_urls": ["*://*:*/beacon/leak.png"]},)),
    "block_urls_over_the_clients": Leak(
        ({"block_urls": ["*://*:*/beacon/leak.png"]},),
        client={"block_urls": ["*://*:*/beacon/client.png"]},
    ),
    "block_navigation": Leak(
        ({"block_navigation": True, "actions": [Click(selector="#go", wait_after_ms=300)]},)
    ),
    "action_error": Leak(({"actions": [Click(selector="#missing_LEAK")]},), times_out=False),
    "post_load_script_mutation": Leak(({"post_load_scripts": [_MUTATE]},), times_out=False),
    "prior_page_timers": Leak(({},), path="/timer", times_out=False),
    "redirect_chain": Leak(({},), path="/redirect", times_out=False),
    # A URL differing only by fragment used to continue the loaded page on the same tab.
    "same_page_then_hash_only": Leak(
        ({"post_load_scripts": [_MUTATE]},), then="#after", times_out=False
    ),
}
_SOAK_PICKS = [
    kwargs
    for row in LEAKS.values()
    if not row.client and row.path == "/leak" and not row.then
    for kwargs in row.earlier
]
LEAKS["mixed_soak"] = Leak(tuple(random.Random(1234).choices(_SOAK_PICKS, k=30)), times_out=False)

LEAK_CASES = [
    (name, earlier)
    for name, row in LEAKS.items()
    for earlier in (("completes", "times_out") if row.times_out else ("completes",))
]


@pytest.fixture(scope="module")
def fresh() -> dict[str, dict[str, object]]:
    """The control fetch on a fresh client, cached by client kwargs and URL."""
    return {}


@pytest.mark.parametrize(("name", "earlier"), LEAK_CASES)
def test_fetch_after_earlier_fetches_matches_a_fresh_client(
    server: HTTPServer, fresh: dict[str, dict[str, object]], name: str, earlier: str
) -> None:
    row = LEAKS[name]
    control = server.url_for("/leak") + row.then
    key = json.dumps([row.client, control], sort_keys=True)
    if key not in fresh:
        with onyxweb.Client(concurrency=1, **row.client) as new:
            fresh[key] = _observe(new, server, control)
    expected = fresh[key]
    with onyxweb.Client(concurrency=1, **row.client) as client:
        for turn, kwargs in enumerate(row.earlier):
            if earlier == "times_out":
                # A distinct URL each turn: Chrome's cache holds a repeat until the first ends.
                slow = server.url_for("/slow") + f"?turn={turn}"
                with pytest.raises(TimeoutError):
                    client.fetch(slow, timeout_ms=TIMEOUT_MS, **kwargs)
            else:
                seen = _observe(client, server, server.url_for(row.path), **kwargs)
                assert seen != expected, "the earlier fetch showed nothing of its setting"
        assert _observe(client, server, control) == expected


def test_simultaneous_fetches_keep_their_own_settings(server: HTTPServer, gauge: Gauge) -> None:
    """Two fetches in flight at once on a two-tab client each see only their own settings.

    Every ``LEAKS`` row is a sequence on one tab, so none can overlap two fetches.
    """
    gauge.peak = 0
    seen = len(server.log)
    with onyxweb.Client(concurrency=2) as client, ThreadPoolExecutor(max_workers=2) as pool:
        for _ in range(3):
            futures = {
                who: pool.submit(
                    client.fetch,
                    server.url_for("/who") + f"?id={who}",
                    extra_headers={"X-Leak": who},
                    scripts=[f"window.__leak = '{who}'"],
                    post_load_scripts=["window.__leak"],
                )
                for who in ("A", "B")
            }
            for who, future in futures.items():
                assert future.result().post_load_results == [who]
    requests = [req for req, _ in server.log[seen:] if req.path == "/who"]
    assert len(requests) == 6
    assert all(req.headers.get("X-Leak") == req.args["id"] for req in requests)
    assert gauge.peak == 2, "the fetches never overlapped, so this proves nothing"


def _full_client() -> onyxweb.Client:
    """A full-engine Client; skips the test when full Chrome is absent."""
    try:
        return onyxweb.Client(engine="full", concurrency=1, navigation_timeout_ms=20_000)
    except onyxweb.OnyxwebError as e:
        if "not found" in str(e).lower():
            pytest.skip(f"full Chrome unavailable: {e}")
        raise


@pytest.mark.parametrize("together", [True, False], ids=["at_once", "one_after_another"])
def test_full_engine_clients_keep_separate_profiles(together: bool) -> None:
    """Real Chrome's ProcessSingleton refuses a profile dir already open or left locked.

    Each Client gets its own temporary profile, so two run at once and a second
    starts cleanly after the first closes. The shell engine has no ProcessSingleton,
    which is why a shared default dir once hid this.
    """
    page = "data:text/html,<html><body>x</body></html>"
    first = _full_client()
    try:
        if together:
            second = _full_client()
            try:
                assert second.fetch(page).status_code == 200
            finally:
                second.close()
        assert first.fetch(page).status_code == 200
    finally:
        first.close()
    if not together:
        again = _full_client()
        try:
            assert again.fetch(page).status_code == 200
        finally:
            again.close()
