"""Per-context proxy + Basic auth (``Fetch.authRequired``) + runtime mutability.

Proxy is a per-browser-context setting (``CreateBrowserContextParams.proxyServer``),
supports ``user:pass`` credentials (stripped from the URL and supplied via
``Fetch.authRequired``, since Chrome ignores creds in ``--proxy-server``), and is
runtime-mutable (a change applies as pooled tabs cycle).

These tests stand up a real local **forward proxy** (stdlib ``http.server``) and
point onyxweb at it. Chromium bypasses the proxy for loopback by default, so every
client sets ``proxy_bypass_list="<-loopback>"`` to force localhost through the proxy
(the same trick rod's proxy test uses).
"""

from __future__ import annotations

import base64
import http.server
import threading
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

import onyxweb
import pytest
from pytest_httpserver import HTTPServer

_BYPASS = "<-loopback>"  # remove the implicit loopback rule → proxy localhost too


@dataclass
class ProxyState:
    """Records what a running forward proxy saw, for assertions."""

    require_auth: bool
    username: str = ""
    password: str = ""
    forwarded: list[str] = field(default_factory=list)  # absolute URIs it relayed
    saw_valid_auth: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def expected_header(self) -> str:
        token = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
        return f"Basic {token}"


def _handler_for(state: ProxyState) -> type[http.server.BaseHTTPRequestHandler]:
    st = state

    class _Proxy(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args: object) -> None:
            pass  # keep test output clean

        def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
            auth = self.headers.get("Proxy-Authorization")
            if st.require_auth and auth != st.expected_header:
                body = b"proxy auth required"
                self.send_response(407)
                self.send_header("Proxy-Authenticate", 'Basic realm="onyxweb-test"')
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
                return
            with st._lock:  # noqa: SLF001
                st.forwarded.append(self.path)
                if st.require_auth:
                    st.saw_valid_auth = True
            try:
                with urllib.request.urlopen(self.path, timeout=5) as resp:  # noqa: S310
                    data = resp.read()
                    self.send_response(resp.status)
                    self.send_header(
                        "Content-Type", resp.headers.get("Content-Type", "text/html")
                    )
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(data)
            except Exception:  # noqa: BLE001 — relay any upstream failure as 502
                self.send_response(502)
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()

    return _Proxy


@pytest.fixture
def proxy_factory() -> Iterator[Callable[..., tuple[str, ProxyState]]]:
    """Spin up local forward proxies; returns (proxy_url, state). Auto-cleaned."""
    servers: list[http.server.ThreadingHTTPServer] = []

    def make(
        *, require_auth: bool = False, username: str = "", password: str = ""
    ) -> tuple[str, ProxyState]:
        state = ProxyState(require_auth=require_auth, username=username, password=password)
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(state))
        servers.append(srv)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]
        return f"http://127.0.0.1:{port}", state

    yield make

    for srv in servers:
        srv.shutdown()


def _target(httpserver: HTTPServer, marker: str = "PROXIED_OK") -> str:
    httpserver.expect_request("/").respond_with_data(
        f"<html><body>{marker}</body></html>", content_type="text/html"
    )
    return httpserver.url_for("/")


# ---------------------------------------------------------------------------
# Routing — the proxy actually carries the traffic (Stage 0 + 1)
# ---------------------------------------------------------------------------


def test_proxy_transits_requests(
    httpserver: HTTPServer, proxy_factory: Callable[..., tuple[str, ProxyState]]
) -> None:
    """With a proxy configured, the page load routes through it."""
    url = _target(httpserver)
    proxy_url, state = proxy_factory()
    with onyxweb.Client(proxy=proxy_url, proxy_bypass_list=_BYPASS) as c:
        r = c.fetch(url)
    assert "PROXIED_OK" in r
    assert state.forwarded, "proxy never saw the request — traffic bypassed it"
    assert any(url in f for f in state.forwarded), (
        f"proxy forwarded {state.forwarded}, not the target {url}"
    )


# ---------------------------------------------------------------------------
# Auth — user:pass via Fetch.authRequired (Stage 2). Canonical red→green.
# ---------------------------------------------------------------------------


def test_proxy_basic_auth_succeeds(
    httpserver: HTTPServer, proxy_factory: Callable[..., tuple[str, ProxyState]]
) -> None:
    """``user:pass@host`` authenticates to the proxy and the page loads."""
    url = _target(httpserver)
    proxy_url, state = proxy_factory(require_auth=True, username="bob", password="s3cr3t")
    authed = proxy_url.replace("http://", "http://bob:s3cr3t@")
    with onyxweb.Client(proxy=authed, proxy_bypass_list=_BYPASS) as c:
        r = c.fetch(url)
    assert "PROXIED_OK" in r, f"auth proxy did not let us through; html={r[:200]!r}"
    assert state.saw_valid_auth, "proxy never received valid credentials"


def test_proxy_wrong_creds_blocked(
    httpserver: HTTPServer, proxy_factory: Callable[..., tuple[str, ProxyState]]
) -> None:
    """Wrong credentials never get past the proxy (it never forwards)."""
    url = _target(httpserver)
    proxy_url, state = proxy_factory(require_auth=True, username="bob", password="right")
    bad = proxy_url.replace("http://", "http://bob:WRONG@")
    with onyxweb.Client(proxy=bad, proxy_bypass_list=_BYPASS) as c:
        try:
            r = c.fetch(url, timeout_ms=8000)
            assert "PROXIED_OK" not in r
        except (onyxweb.OnyxwebError, TimeoutError):
            pass  # a hard proxy-auth failure is an acceptable outcome
    assert state.forwarded == [], "proxy forwarded despite wrong credentials"


# ---------------------------------------------------------------------------
# Runtime mutability — proxy is no longer launch-only (Stage 0)
# ---------------------------------------------------------------------------


def test_proxy_and_bypass_settable_at_runtime(
    proxy_factory: Callable[..., tuple[str, ProxyState]]
) -> None:
    """Setting proxy / proxy_bypass_list at runtime does not raise (not launch-only)."""
    proxy_url, _ = proxy_factory()
    with onyxweb.Client() as c:
        c.config.network.proxy = proxy_url  # would raise ValueError if launch-only
        c.config.network.proxy_bypass_list = _BYPASS
        assert c.config.network.proxy == proxy_url
        assert c.config.network.proxy_bypass_list == _BYPASS


def test_proxy_change_routes_through_new_proxy(
    httpserver: HTTPServer, proxy_factory: Callable[..., tuple[str, ProxyState]]
) -> None:
    """A runtime proxy change routes the next fetch through the new proxy."""
    url = _target(httpserver)
    proxy_a, state_a = proxy_factory()
    proxy_b, state_b = proxy_factory()
    with onyxweb.Client(concurrency=1, proxy=proxy_a, proxy_bypass_list=_BYPASS) as c:
        c.fetch(url)
        assert state_a.forwarded, "first fetch didn't route through proxy A"
        c.config.network.proxy = proxy_b
        r = c.fetch(url)  # concurrency=1 → tab recycles → recreated with proxy B
    assert "PROXIED_OK" in r
    assert state_b.forwarded, "runtime proxy change didn't route through proxy B"


# ---------------------------------------------------------------------------
# Coexistence — block_navigation must not clobber the auth Fetch domain
# (navigation blocking itself is unavailable under an authenticated proxy, since
# chromiumoxide owns Fetch for auth — a documented limitation).
# ---------------------------------------------------------------------------


def test_block_navigation_doesnt_break_proxy_auth(
    httpserver: HTTPServer, proxy_factory: Callable[..., tuple[str, ProxyState]]
) -> None:
    """``block_navigation=True`` on an authenticated-proxy fetch must not disable
    chromiumoxide's auth Fetch domain — this fetch and the next on the same tab
    both still authenticate."""
    url = _target(httpserver)
    proxy_url, state = proxy_factory(require_auth=True, username="u", password="p")
    authed = proxy_url.replace("http://", "http://u:p@")
    with onyxweb.Client(concurrency=1, proxy=authed, proxy_bypass_list=_BYPASS) as c:
        r1 = c.fetch(url, block_navigation=True)
        r2 = c.fetch(url)
    assert "PROXIED_OK" in r1, "auth broke under block_navigation"
    assert "PROXIED_OK" in r2, "block_navigation cleanup disabled the auth Fetch domain"
    assert state.saw_valid_auth
