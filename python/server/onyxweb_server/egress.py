"""Egress guard: the address rule, and the forward proxy that applies it to the browser.

The URL guard checks the address a caller asked for. It cannot see a redirect, a name that
resolves differently a second time, or a request a page's own script makes. The proxy closes all
three: the browser is pointed at it, so every connection Chrome makes, for any reason, is resolved
here, checked here, and made here to the address that was checked.

It speaks the two forms an HTTP proxy is asked for. ``CONNECT host:port`` is checked, connected,
answered ``200`` and then relayed as bytes (TLS runs end to end through it). A plain
``GET http://host/path`` is checked, rewritten to origin form with ``Connection: close`` so the
connection carries that one request and no later one can reach another host, and relayed.
A target that resolves to any non-public address is refused with a ``403`` marked
``X-Onyxweb-Egress: refused``, before a socket to it is opened.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from collections.abc import Awaitable, Callable
from typing import Final
from urllib.parse import urlsplit

log = logging.getLogger("onyxweb_server")

Address = ipaddress.IPv4Address | ipaddress.IPv6Address
Streams = tuple[asyncio.StreamReader, asyncio.StreamWriter]

REFUSED_HEADER: Final = "X-Onyxweb-Egress"
MAX_HEAD: Final = 16 * 1024  # bytes of request line and headers
HEAD_TIMEOUT_S: Final = 10  # to receive that head
CONNECT_TIMEOUT_S: Final = 10  # to open the outbound connection
GRACE_S: Final = 5  # the other direction of a relay may drain this long after one ends
CHUNK: Final = 64 * 1024
MAX_CONNECTIONS: Final = 256  # in flight at once; more wait

REASONS: Final = {
    200: "Connection Established",
    400: "Bad Request",
    403: "Forbidden",
    431: "Request Header Fields Too Large",
    502: "Bad Gateway",
}


def is_public(address: Address) -> bool:
    """Whether `address` is one the server may connect to: global, and none of the special kinds."""
    # Before Python 3.11.10 a mapped ::ffff:a.b.c.d escaped the IPv4 private ranges; judge the
    # embedded IPv4. No test can show it here, because this interpreter already does.
    address = getattr(address, "ipv4_mapped", None) or address
    return not (
        not address.is_global
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or address.is_loopback
        or address.is_link_local
    )


def resolve_host(host: str) -> list[Address]:
    """Every address `host` names; a name that is not an IP literal is resolved.

    Raises:
        OSError: If the name cannot be resolved.
    """
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    # Decimal (2130706433) and octal (0177.0.0.1) forms are names to Python but 127.0.0.1
    # to Chrome, so anything that is not a plain literal goes through the resolver.
    found = socket.getaddrinfo(host, None)
    return [ipaddress.ip_address(str(info[4][0]).split("%")[0]) for info in found]


class _Bad(Exception):
    """A request the proxy answers with `status` and closes."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class EgressProxy:
    """A local forward proxy that connects only to public addresses.

    Bind it to loopback, start it, and give `url` to the browser as its proxy together with
    ``proxy_bypass_list="<-loopback>"``: without that Chrome skips a proxy for loopback and
    link-local addresses and connects to them directly.
    """

    def __init__(
        self,
        *,
        is_allowed: Callable[[Address], bool] = is_public,
        resolve: Callable[[str], list[Address]] = resolve_host,
        connect: Callable[[str, int], Awaitable[Streams]] | None = None,
        host: str = "127.0.0.1",
    ) -> None:
        """Build a proxy that is not yet listening.

        Args:
            is_allowed: Whether an address may be connected to. Default: `is_public`. Tests
                widen it because their servers listen on loopback; a real server never does.
            resolve: Every address of a host name. Default: `resolve_host`.
            connect: Opens an outbound connection to an address. Default: asyncio's.
            host: The address to listen on.
        """
        self.is_allowed = is_allowed
        self.resolve = resolve
        self.connect = connect or asyncio.open_connection
        self.refusals = 0  # requests refused since it started
        self._host = host
        self._server: asyncio.Server | None = None
        self._url = ""
        self._slots = asyncio.Semaphore(MAX_CONNECTIONS)
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def url(self) -> str:
        """The proxy's address, ``http://127.0.0.1:port``; empty until it is started."""
        return self._url

    async def start(self) -> str:
        """Listen on a free port and return `url`; starting a started proxy changes nothing."""
        if self._server is None:
            self._server = await asyncio.start_server(self._handle, self._host, 0, limit=MAX_HEAD)
            self._url = f"http://{self._host}:{self._server.sockets[0].getsockname()[1]}"
            log.info(f"egress proxy listening on {self._url}")
        return self._url

    async def aclose(self) -> None:
        """Stop listening and end every connection in flight; closing twice is harmless."""
        server, self._server = self._server, None
        if server is None:
            return
        server.close()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Serve one accepted connection, counted against `MAX_CONNECTIONS`."""
        task = asyncio.current_task()
        assert task is not None
        self._tasks.add(task)
        try:
            async with self._slots:
                await _Connection(self, reader, writer).serve()
        except asyncio.CancelledError:
            pass
        except (ConnectionError, OSError):
            pass  # the browser went away; nothing to answer
        finally:
            self._tasks.discard(task)
            writer.close()


class _Connection:
    """One connection from the browser: its request, the address check, and the relay.

    It holds the browser's own streams for its whole life, and the proxy for the address
    policy, so nothing below passes them along.
    """

    def __init__(
        self, proxy: EgressProxy, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._proxy = proxy
        self._reader = reader
        self._writer = writer

    async def serve(self) -> None:
        """Read the request, connect where it is allowed to, and relay; else answer the refusal."""
        try:
            method, target, headers = await self._head()
            if method == "CONNECT":
                host, port = self._authority(target)
                origin_r, origin_w = await self._dial(host, port)
                self._writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await self._writer.drain()
                await self._relay(origin_r, origin_w)
                return
            parts = urlsplit(target)
            if parts.scheme != "http" or not parts.hostname:
                raise _Bad(400, "send an absolute http:// URI, or CONNECT host:port")
            origin_r, origin_w = await self._dial(parts.hostname, parts.port or 80)
            path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
            origin_w.write(self._origin_head(method, path, headers))
            await origin_w.drain()
            await self._relay(origin_r, origin_w)
        except _Bad as bad:
            await self._reply(bad.status, str(bad), refused=bad.status == 403)

    async def _head(self) -> tuple[str, str, list[tuple[str, str]]]:
        """The request line and headers, as (method, target, [(name, value)])."""
        try:
            raw = await asyncio.wait_for(self._reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT_S)
        except asyncio.LimitOverrunError as lo:
            raise _Bad(431, "the request head is too long") from lo
        except (asyncio.IncompleteReadError, TimeoutError) as err:
            raise _Bad(400, "the request head never finished") from err
        lines = raw.decode("latin-1").split("\r\n")
        pieces = lines[0].split(" ")
        if len(pieces) != 3 or not pieces[2].startswith("HTTP/"):
            raise _Bad(400, "not an HTTP request line")
        headers = [
            (name.strip(), value.strip())
            for line in lines[1:]
            if line and ":" in line
            for name, _, value in [line.partition(":")]
        ]
        return pieces[0], pieces[1], headers

    @staticmethod
    def _authority(target: str) -> tuple[str, int]:
        """The host and port of a ``CONNECT host:port`` target."""
        parts = urlsplit(f"//{target}")
        try:
            port = parts.port
        except ValueError as ve:
            raise _Bad(400, "CONNECT needs host:port with a valid port") from ve
        if not parts.hostname or not port or not 0 < port < 65536:
            raise _Bad(400, "CONNECT needs host:port with a valid port")
        return parts.hostname, port

    async def _dial(self, host: str, port: int) -> Streams:
        """Resolve `host` once, refuse unless every answer is allowed, connect to one that is."""
        proxy = self._proxy
        try:
            addresses = await asyncio.to_thread(proxy.resolve, host)
        except OSError as oe:
            raise _Bad(502, f"cannot resolve {host}") from oe
        if not addresses:
            raise _Bad(502, f"cannot resolve {host}")
        if not all(proxy.is_allowed(a) for a in addresses):
            proxy.refusals += 1
            log.warning(f"egress refused {host}:{port}")
            raise _Bad(
                403, f"{host} is a private or internal address; only public addresses are fetched."
            )
        for address in addresses:
            # Connect to the address that was checked, never to the name again.
            try:
                return await asyncio.wait_for(proxy.connect(str(address), port), CONNECT_TIMEOUT_S)
            except (OSError, TimeoutError):
                continue
        raise _Bad(502, f"cannot connect to {host}:{port}")

    @staticmethod
    def _origin_head(method: str, path: str, headers: list[tuple[str, str]]) -> bytes:
        """The request as the origin should see it: origin form, one request per connection."""
        upgrade = any(name.lower() == "upgrade" for name, _ in headers)
        kept = [
            f"{name}: {value}"
            for name, value in headers
            if name.lower() not in ("proxy-connection", "proxy-authorization")
            and not (name.lower() == "connection" and not upgrade)
        ]
        if not upgrade:
            kept.append("Connection: close")
        return "\r\n".join([f"{method} {path} HTTP/1.1", *kept, "", ""]).encode("latin-1")

    async def _relay(self, origin_r: asyncio.StreamReader, origin_w: asyncio.StreamWriter) -> None:
        """Copy both ways until one side ends and the other has had `GRACE_S` to drain."""

        async def pipe(source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
            """Copy `source` to `sink` until it ends, then tell the sink nothing more is coming."""
            try:
                while data := await source.read(CHUNK):
                    sink.write(data)
                    await sink.drain()
                if sink.can_write_eof():
                    sink.write_eof()
            except (ConnectionError, OSError):
                pass

        up = asyncio.create_task(pipe(self._reader, origin_w))
        down = asyncio.create_task(pipe(origin_r, self._writer))
        try:
            _, pending = await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
            if pending:
                _, pending = await asyncio.wait(pending, timeout=GRACE_S)
            for task in pending:
                task.cancel()
        finally:
            up.cancel()
            down.cancel()
            origin_w.close()

    async def _reply(self, status: int, message: str, *, refused: bool = False) -> None:
        """Answer the browser with `status` and close; a refusal carries `REFUSED_HEADER`."""
        body = f"{message}\n".encode()
        marker = f"{REFUSED_HEADER}: refused\r\n" if refused else ""
        head = (
            f"HTTP/1.1 {status} {REASONS[status]}\r\nContent-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n{marker}Connection: close\r\n\r\n"
        )
        try:
            self._writer.write(head.encode() + body)
            await self._writer.drain()
        except (ConnectionError, OSError):
            pass


__all__ = ["REFUSED_HEADER", "EgressProxy", "is_public", "resolve_host"]
