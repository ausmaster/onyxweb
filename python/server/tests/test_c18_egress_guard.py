"""C18 egress guard — the server connects only to public addresses: a URL, a connection request
through the proxy, a redirect or a page's own subrequest reaches one, or is refused before any
connection is made to it.

``GUARD`` is the first layer, the URL a caller asks for: the scheme, the host, credentials, and
every address a name resolves to. ``REQUESTS`` is the second, the forward proxy the browser is
pointed at: a request (CONNECT or plain GET, well formed or not, to a public or private target)
gives a status, what the origin saw and what the proxy dialled, and a refusal is marked and
counted. The last table drives a real Chrome through a core wired to the proxy: an image, a
redirect, an https navigation and a script that each aim at a private host must never reach it.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import socket
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import onyxweb
import pytest
from onyxweb_server.core import CoreConfig, FetchOptions, Refused, ServerCore, check_url
from onyxweb_server.egress import EgressProxy, is_public
from pytest_httpserver import HTTPServer

# --- the URL guard ----------------------------------------------------------------------------

_SCHEME = "only http and https"
# URL -> fragment of the refusal. A refusal names the fix, so each is checked for it too.
REFUSED: dict[str, str] = {
    "file:///etc/passwd": _SCHEME,
    "ftp://example.com/": _SCHEME,
    "data:text/html,x": _SCHEME,
    "javascript:alert(1)": _SCHEME,
    "about:blank": _SCHEME,
    "http://": "no host",
    "http://user:pass@93.184.216.34/": "credentials",
    "http://user@93.184.216.34/": "credentials",
    "http://127.0.0.1/": "private",
    "http://localhost:8080/": "private",
    "http://[::1]/": "private",
    "http://0.0.0.0/": "private",
    "http://10.0.0.1/": "private",
    "http://172.16.0.1/": "private",
    "http://192.168.1.1/": "private",
    "http://100.64.0.1/": "private",  # carrier-grade NAT: not global, not "private" either
    "http://169.254.169.254/latest/meta-data/": "private",
    "http://224.0.0.1/": "private",
    "http://[fe80::1]/": "private",
    "http://[fc00::1]/": "private",
    # 127.0.0.1 in disguise: none of these is an IP literal to Python, and Chrome reads them all.
    "http://[::ffff:127.0.0.1]/": "private",
    "http://[::ffff:a9fe:a9fe]/": "private",
    "http://2130706433/": "private",
    "http://0177.0.0.1/": "private",
    "http://0x7f.0.0.1/": "private",
    "http://127.1/": "private",
}

# A hostname -> the addresses it resolves to (a fake resolver, so no DNS is needed).
HOSTS: dict[str, list[str]] = {
    "internal.example": ["10.1.2.3"],
    "mixed.example": ["93.184.216.34", "10.1.2.3"],  # one private answer is enough to refuse
    "meta.example": ["169.254.169.254"],
    "public.example": ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"],
    "gone.example": [],
}
# URL -> the fragment of its refusal, or None when it must be accepted. A refusal names the fix.
GUARD: dict[str, str | None] = {
    **REFUSED,
    "http://internal.example/": "private",
    "http://mixed.example/": "private",
    "http://meta.example/": "private",
    "http://gone.example/": "cannot resolve",
    "http://93.184.216.34/": None,
    "https://8.8.8.8/dns": None,
    "http://[2606:4700:4700::1111]/x": None,
    "http://public.example/": None,
}


@pytest.mark.parametrize("url", list(GUARD))
def test_the_url_guard_refuses_what_is_not_public(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    real = socket.getaddrinfo

    def fake(name: str, *args: Any, **kwargs: Any) -> Any:
        if name not in HOSTS:
            return real(name, *args, **kwargs)
        if not HOSTS[name]:
            raise socket.gaierror(socket.EAI_NONAME, "unknown host")
        return [
            (socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 0))
            for a in HOSTS[name]
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    says = GUARD[url]
    if says is None:
        check_url(url)
        return
    with pytest.raises(Refused) as exc:
        check_url(url)
    message = str(exc.value)
    assert exc.value.code == "refused_url"
    assert says in message, message
    assert "public" in message or "http" in message, "the refusal doesn't say what to do"


# --- the proxy --------------------------------------------------------------------------------

HOSTS_PROXY = {
    "internal.test": ["10.1.2.3"],
    "mixed.test": ["93.184.216.34", "10.1.2.3"],
    "public.test": ["93.184.216.34"],
}
REFUSED_HEADER = b"X-Onyxweb-Egress: refused"


class Resolves:
    """The proxy's name lookups, recorded; `HOSTS_PROXY` by name, else unresolvable."""

    def __init__(self, rebinding: bool = False) -> None:
        self.calls: list[str] = []
        self._rebinding = rebinding

    def __call__(self, host: str) -> list[Any]:
        self.calls.append(host)
        if self._rebinding:  # public the first time, private the second: a DNS rebind
            answer = "93.184.216.34" if len(self.calls) == 1 else "10.0.0.1"
            return [ipaddress.ip_address(answer)]
        try:
            return [ipaddress.ip_address(host)]
        except ValueError:
            pass
        if host not in HOSTS_PROXY:
            raise socket.gaierror(socket.EAI_NONAME, "unknown host")
        return [ipaddress.ip_address(a) for a in HOSTS_PROXY[host]]


class Connects:
    """The proxy's outbound connections, recorded; each goes to `port` on loopback when given."""

    def __init__(self, port: int | None = None) -> None:
        self.calls: list[tuple[str, int]] = []
        self._port = port

    async def __call__(
        self, host: str, port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        self.calls.append((host, port))
        return await asyncio.open_connection("127.0.0.1", self._port or port)


class Upstream:
    """A loopback origin: an HTTP one that answers ``ok``, or an echo one for tunnels."""

    def __init__(self, echo: bool) -> None:
        self.echo = echo
        self.connections = 0
        self.requests: list[bytes] = []
        self.port = 0

    @classmethod
    @contextlib.asynccontextmanager
    async def serving(cls, kind: str) -> AsyncIterator[Upstream]:
        """A loopback origin of `kind` ("http", "echo"), or a closed port ("down")."""
        origin = cls(echo=kind == "echo")
        if kind == "down":
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                origin.port = sock.getsockname()[1]
            yield origin
            return
        server = await asyncio.start_server(origin._handle, "127.0.0.1", 0)
        origin.port = server.sockets[0].getsockname()[1]
        try:
            yield origin
        finally:
            server.close()
            await server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        try:
            if self.echo:
                while data := await reader.read(65536):
                    writer.write(data)
                    await writer.drain()
                return
            head = await reader.readuntil(b"\r\n\r\n")
            length = 0
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":", 1)[1])
            self.requests.append(head + await reader.readexactly(length))
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
        finally:
            writer.close()


_ALLOW: dict[str, Callable[[Any], bool]] = {
    "public": is_public,
    "local": lambda ip: str(ip) == "127.0.0.1",
    "any": lambda ip: True,
}


@dataclass(frozen=True)
class Req:
    """One request to the proxy, the world it runs in, and everything that must come of it."""

    raw: str  # the request head ({port} is the origin's); an origin-form line is a bad request
    status: int = 200
    allow: str = "public"  # the address rule: public (the real one), local (127.0.0.1) or any
    origin: str = "http"  # http, echo, or down (a closed port)
    resolver: str = "fake"  # fake, or rebinding (public the first time, private after)
    body: bytes = b""
    refused: bool = False  # a marked 403, counted, with no connection made
    sees: tuple[bytes, ...] = ()  # what the origin's request must carry
    never: tuple[bytes, ...] = ()  # and must not
    tunnel: tuple[bytes, ...] = ()  # messages that must come back unchanged after a 200
    dialled: tuple[tuple[str, int], ...] | None = None  # outbound connections, when it matters
    lookups: int | None = None  # how often a name may be resolved, when it matters


def _to(host: str, connect: bool = True, **kw: Any) -> Req:
    """A refusal aimed at `host` as a CONNECT (or a plain GET) on the origin's port."""
    line = (
        f"CONNECT {host}:{{port}} HTTP/1.1" if connect else f"GET http://{host}:{{port}}/x HTTP/1.1"
    )
    return Req(line + "\r\nHost: x\r\n\r\n", 403, refused=True, dialled=(), **kw)


_BASE = "GET http://127.0.0.1:{port}/a/b?q=1 HTTP/1.1\r\n"
REQUESTS: dict[str, Req] = {
    # --- a target that is not public is refused before any connection ----------------------------
    "connect_to_loopback": _to("127.0.0.1"),
    "connect_to_ipv6_loopback": _to("[::1]"),
    "connect_to_a_private_range": _to("10.0.0.5"),
    "connect_to_link_local_metadata": _to("169.254.169.254"),
    "connect_to_unspecified": _to("0.0.0.0"),
    "connect_to_a_name_that_resolves_private": _to("internal.test"),
    # One private answer among public ones is enough to refuse.
    "connect_to_mixed_answers": _to("mixed.test"),
    "get_to_loopback": _to("127.0.0.1", connect=False),
    "get_to_metadata": _to("169.254.169.254", connect=False),
    "get_to_a_name_that_resolves_private": _to("internal.test", connect=False),
    # --- a request that is not well formed gets an error, not a hang ----------------------------
    "garbage": Req("hello\r\n\r\n", 400, allow="any", dialled=()),
    "a_request_line_of_two_parts": Req("GET /\r\n\r\n", 400, allow="any", dialled=()),
    "a_request_in_origin_form": Req(
        "GET /path HTTP/1.1\r\nHost: x\r\n\r\n", 400, allow="any", dialled=()
    ),
    "an_unsupported_scheme": Req(
        "GET ftp://example.com/ HTTP/1.1\r\n\r\n", 400, allow="any", dialled=()
    ),
    "an_absolute_https_uri": Req(
        "GET https://example.com/ HTTP/1.1\r\n\r\n", 400, allow="any", dialled=()
    ),
    "connect_without_a_port": Req(
        "CONNECT example.com HTTP/1.1\r\n\r\n", 400, allow="any", dialled=()
    ),
    "connect_to_port_zero": Req(
        "CONNECT example.com:0 HTTP/1.1\r\n\r\n", 400, allow="any", dialled=()
    ),
    "connect_to_a_port_too_big": Req(
        "CONNECT example.com:70000 HTTP/1.1\r\n\r\n", 400, allow="any", dialled=()
    ),
    "a_head_too_long": Req(
        "GET http://x/ HTTP/1.1\r\nX: " + "a" * 40_000 + "\r\n\r\n", 431, allow="any", dialled=()
    ),
    "a_name_that_does_not_resolve": Req(
        "CONNECT nx.test:443 HTTP/1.1\r\n\r\n", 502, allow="any", dialled=()
    ),
    # A public address that is down is the origin's fault, not a refusal.
    "an_origin_that_refuses_the_connection": Req(
        "CONNECT 127.0.0.1:{port} HTTP/1.1\r\n\r\n", 502, allow="local", origin="down"
    ),
    # --- a plain request reaches the origin in origin form, on a connection of its own ----------
    "origin_form_and_close": Req(
        _BASE + "Host: 127.0.0.1:{port}\r\nProxy-Connection: keep-alive\r\nAccept: */*\r\n\r\n",
        allow="local",
        sees=(b"GET /a/b?q=1 HTTP/1.1", b"Connection: close", b"Accept: */*", b"Host: 127.0.0.1:"),
        never=(b"http://", b"Proxy-Connection"),
    ),
    "keep_alive_is_replaced_by_close": Req(
        _BASE + "Host: x\r\nConnection: keep-alive\r\n\r\n",
        allow="local",
        sees=(b"Connection: close",),
        never=(b"keep-alive",),
    ),
    "the_fragment_is_dropped": Req(
        "GET http://127.0.0.1:{port}/p#frag HTTP/1.1\r\nHost: x\r\n\r\n",
        allow="local",
        sees=(b"GET /p HTTP/1.1",),
    ),
    "an_empty_path_is_a_slash": Req(
        "GET http://127.0.0.1:{port} HTTP/1.1\r\nHost: x\r\n\r\n",
        allow="local",
        sees=(b"GET / HTTP/1.1",),
    ),
    "a_body_is_forwarded": Req(
        "POST http://127.0.0.1:{port}/submit HTTP/1.1\r\nHost: x\r\nContent-Length: 4\r\n\r\n",
        allow="local",
        body=b"body",
        sees=(b"POST /submit HTTP/1.1", b"body"),
    ),
    # A protocol upgrade must keep its Connection header, or the origin never switches.
    "an_upgrade_keeps_its_connection_header": Req(
        _BASE + "Host: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n",
        allow="local",
        sees=(b"Upgrade: websocket", b"Connection: Upgrade"),
        never=(b"Connection: close",),
    ),
    # --- a CONNECT is a pipe once it is answered ------------------------------------------------
    # The second message is large enough to cross several reads.
    "a_connect_tunnels_bytes_both_ways": Req(
        "CONNECT 127.0.0.1:{port} HTTP/1.1\r\n\r\n",
        allow="local",
        origin="echo",
        tunnel=(b"ping", b"x" * 200_000),
    ),
    # A name that resolves public once and private after cannot steer the connection: the proxy
    # resolves once and dials the address it checked, not the name.
    "the_proxy_dials_the_address_it_checked": Req(
        "CONNECT rebind.test:443 HTTP/1.1\r\n\r\n",
        origin="echo",
        resolver="rebinding",
        tunnel=(b"hi",),
        dialled=(("93.184.216.34", 443),),
        lookups=1,
    ),
}


@pytest.mark.parametrize("name", list(REQUESTS))
async def test_a_request_through_the_proxy_gets_its_answer_and_effects(name: str) -> None:
    row = REQUESTS[name]
    resolves = Resolves(rebinding=row.resolver == "rebinding")
    async with Upstream.serving(row.origin) as origin:
        connects = Connects(port=origin.port if row.resolver == "rebinding" else None)
        proxy = EgressProxy(is_allowed=_ALLOW[row.allow], resolve=resolves, connect=connects)
        url = await proxy.start()
        host, port = url.removeprefix("http://").rsplit(":", 1)
        reader, writer = await asyncio.open_connection(host, int(port))
        try:
            writer.write(row.raw.replace("{port}", str(origin.port)).encode() + row.body)
            await writer.drain()
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            assert int(head.split(b" ", 2)[1]) == row.status, head
            assert (REFUSED_HEADER in head) == row.refused
            if row.tunnel:
                for message in row.tunnel:
                    writer.write(message)
                    await writer.drain()
                    assert await asyncio.wait_for(reader.readexactly(len(message)), 5) == message
            else:
                rest = b""
                while chunk := await asyncio.wait_for(reader.read(65536), 5):
                    rest += chunk  # returns only once the proxy closes the connection
                if row.status == 200:
                    assert rest.endswith(b"ok"), rest
        finally:
            writer.close()
            await proxy.aclose()
        if row.status == 403 or row.status == 400 or row.status == 431:
            assert origin.connections == 0, "the origin was reached"
        if row.sees or row.never:
            [seen] = origin.requests
            for fragment in row.sees:
                assert fragment in seen, seen
            for fragment in row.never:
                assert fragment not in seen, seen
        assert proxy.refusals == int(row.refused)
        if row.dialled is not None:
            assert connects.calls == list(row.dialled), "the proxy dialled something else"
        if row.lookups is not None:
            assert len(resolves.calls) == row.lookups, "the name was resolved more than once"


async def test_a_closed_proxy_takes_no_more_connections() -> None:
    """Closing stops the listener, and starting or closing twice is harmless.

    New test: nothing else ends a proxy, and a row is one request on a running one.
    """
    proxy = EgressProxy(resolve=Resolves())
    url = await proxy.start()
    assert await proxy.start() == url, "starting twice built a second listener"
    await proxy.aclose()
    await proxy.aclose()
    host, port = url.removeprefix("http://").rsplit(":", 1)
    with pytest.raises(OSError):
        await asyncio.open_connection(host, int(port))


# --- a real Chrome through the core -----------------------------------------------------------


@pytest.fixture
def secret() -> Iterator[HTTPServer]:
    """A server on 127.0.0.2 standing in for an internal service the browser must never reach."""
    with HTTPServer(host="127.0.0.2", port=0) as server:
        server.expect_request(r"/secret.png").respond_with_data(b"", content_type="image/png")
        server.expect_request("/secret").respond_with_data("SECRET_BODY", content_type="text/html")
        yield server


@dataclass(frozen=True)
class Attack:
    """A page the public origin serves, and what the fetch must do with it."""

    page: str  # html; `{secret}` is the internal origin
    start: str = "/page"  # where the fetch starts: a path on the public origin, or a full URL
    refused: bool = False  # the fetch itself must raise `Refused("refused_url")`
    probes: bool = False  # the page itself aims at the private host, and still loads
    down: bool = False  # the target is public but nothing listens: a browser error, not a refusal
    wait_ms: int = 0
    shows: tuple[str, ...] = ()  # fragments the fetched page must carry
    extra: dict[str, str] = field(default_factory=dict)  # more public paths: path -> html


ATTACKS: dict[str, Attack] = {
    "a_public_page_and_its_own_image_load": Attack(
        "<html><body>PUBLIC_OK<img src='/pic.png'></body></html>", shows=("PUBLIC_OK",)
    ),
    "an_image_on_a_private_host_is_never_requested": Attack(
        "<html><body>PUBLIC_OK<img src='{secret}/secret.png'></body></html>",
        shows=("PUBLIC_OK",),
        probes=True,
    ),
    "a_script_cannot_reach_a_private_host": Attack(
        "<html><body>PUBLIC_OK<script>fetch('{secret}/secret', {mode: 'no-cors'})"
        ".catch(() => {})</script></body></html>",
        wait_ms=500,
        shows=("PUBLIC_OK",),
        probes=True,
    ),
    "a_redirect_to_a_private_host_is_refused": Attack("", start="/go", refused=True),
    "a_start_url_on_a_private_host_is_refused": Attack("", start="{secret}/secret", refused=True),
    # Chrome reports a tunnel failure for this too; only the proxy's own refusal makes it `Refused`.
    "an_https_url_that_is_down_is_not_a_refusal": Attack(
        "", start="https://127.0.0.1:{closed_port}/", down=True
    ),
    "an_https_url_on_a_private_host_is_refused": Attack(
        "", start="https://127.0.0.2:{secret_port}/", refused=True
    ),
}


@pytest.mark.parametrize("op", ["fetch", "batch", "fetch_all"])
@pytest.mark.parametrize("name", list(ATTACKS))
async def test_a_browser_cannot_reach_a_private_host_through_the_core(
    httpserver: HTTPServer, secret: HTTPServer, name: str, op: str
) -> None:
    row = ATTACKS[name]
    origin = f"http://127.0.0.2:{secret.port}"
    httpserver.expect_request("/page").respond_with_data(
        row.page.replace("{secret}", origin), content_type="text/html"
    )
    httpserver.expect_request("/pic.png").respond_with_data(b"", content_type="image/png")
    httpserver.expect_request("/go").respond_with_data(
        "", status=302, headers={"Location": f"{origin}/secret"}
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        closed = sock.getsockname()[1]
    start = (
        row.start.replace("{secret}", origin)
        .replace("{secret_port}", str(secret.port))
        .replace("{closed_port}", str(closed))
    )
    if start.startswith("/"):
        # By IP: `localhost` also resolves to ::1, which the test's allow rule refuses.
        start = f"http://127.0.0.1:{httpserver.port}{start}"
    # Only the public origin (127.0.0.1) is reachable; the guard is off because the test server
    # is loopback, so the proxy is what stands between the browser and the secret.
    proxy = EgressProxy(is_allowed=_ALLOW["local"])
    core = ServerCore(egress=proxy, url_guard=lambda url: None, config=CoreConfig(egress=True))
    options = FetchOptions(wait_ms=row.wait_ms)
    outcome: Any
    try:
        if op == "batch":  # a batch returns the same refusal in the URL's place
            [outcome] = await core.batch([start], options)
        else:
            try:
                outcome = await getattr(core, op)(start, options)
            except (Refused, onyxweb.OnyxwebError) as failure:
                outcome = failure
        if row.refused:
            assert isinstance(outcome, Refused), outcome
            assert outcome.code == "refused_url"
        elif row.down:
            assert isinstance(outcome, onyxweb.OnyxwebError), outcome
            assert not isinstance(outcome, Refused)
        else:
            assert not isinstance(outcome, Exception), outcome
            page = outcome.html if isinstance(outcome, onyxweb.FetchResult) else outcome
            for fragment in row.shows:
                assert fragment in page.html, page.html[:300]
        assert core.stats()["egress_refusals"] == proxy.refusals
    finally:
        await core.aclose()
    assert secret.log == [], f"the internal service was reached: {secret.log}"
    host, port = proxy.url.removeprefix("http://").rsplit(":", 1)
    with pytest.raises(OSError):  # closing the core closed the proxy with it
        await asyncio.open_connection(host, int(port))
    if name == "a_public_page_and_its_own_image_load":
        assert "/pic.png" in {req.path for req, _ in httpserver.log}  # the positive control
    if row.refused or row.probes:
        assert proxy.refusals >= 1, "the proxy never saw the attempt, so this proves nothing"
    else:
        assert proxy.refusals == 0
