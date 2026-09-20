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


async def _open(host: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_connection(host, port)


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
        connect: Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]
        | None = None,
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
        self._is_allowed = is_allowed
        self._resolve = resolve
        self._connect = connect or _open
        self._host = host
        self._server: asyncio.Server | None = None
        self._url = ""
        self._slots = asyncio.Semaphore(MAX_CONNECTIONS)
        self._tasks: set[asyncio.Task[None]] = set()
        self.refusals = 0  # requests refused since it started

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

    # --- one connection -----------------------------------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._tasks.add(task)
        try:
            async with self._slots:
                await self._serve(reader, writer)
        except asyncio.CancelledError:
            pass
        except (ConnectionError, OSError):
            pass  # the browser went away; nothing to answer
        finally:
            self._tasks.discard(task)
            writer.close()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            method, target, headers = await self._read_head(reader)
            if method == "CONNECT":
                host, port = self._split_authority(target)
                out = await self._dial(host, port)
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                await _relay(reader, writer, *out)
                return
            parts = urlsplit(target)
            if parts.scheme != "http" or not parts.hostname:
                raise _Bad(400, "send an absolute http:// URI, or CONNECT host:port")
            port = parts.port or 80
            out = await self._dial(parts.hostname, port)
            path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
            out[1].write(_origin_head(method, path, headers))
            await out[1].drain()
            await _relay(reader, writer, *out)
        except _Bad as bad:
            await _reply(writer, bad.status, str(bad), refused=bad.status == 403)

    async def _read_head(
        self, reader: asyncio.StreamReader
    ) -> tuple[str, str, list[tuple[str, str]]]:
        """The request line and headers, as (method, target, [(name, value)])."""
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT_S)
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
    def _split_authority(target: str) -> tuple[str, int]:
        parts = urlsplit(f"//{target}")
        try:
            port = parts.port
        except ValueError as ve:
            raise _Bad(400, "CONNECT needs host:port with a valid port") from ve
        if not parts.hostname or not port or not 0 < port < 65536:
            raise _Bad(400, "CONNECT needs host:port with a valid port")
        return parts.hostname, port

    async def _dial(
        self, host: str, port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Resolve `host` once, refuse unless every answer is allowed, connect to one that is."""
        try:
            addresses = await asyncio.to_thread(self._resolve, host)
        except OSError as oe:
            raise _Bad(502, f"cannot resolve {host}") from oe
        if not addresses:
            raise _Bad(502, f"cannot resolve {host}")
        if not all(self._is_allowed(a) for a in addresses):
            self.refusals += 1
            log.warning(f"egress refused {host}:{port}")
            raise _Bad(
                403, f"{host} is a private or internal address; only public addresses are fetched."
            )
        for address in addresses:
            # Connect to the address that was checked, never to the name again.
            try:
                return await asyncio.wait_for(self._connect(str(address), port), CONNECT_TIMEOUT_S)
            except (OSError, TimeoutError):
                continue
        raise _Bad(502, f"cannot connect to {host}:{port}")


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


async def _reply(
    writer: asyncio.StreamWriter, status: int, message: str, *, refused: bool = False
) -> None:
    body = f"{message}\n".encode()
    marker = f"{REFUSED_HEADER}: refused\r\n" if refused else ""
    head = (
        f"HTTP/1.1 {status} {REASONS[status]}\r\nContent-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n{marker}Connection: close\r\n\r\n"
    )
    try:
        writer.write(head.encode() + body)
        await writer.drain()
    except (ConnectionError, OSError):
        pass


async def _pipe(source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
    """Copy `source` to `sink` until it ends, then tell the sink nothing more is coming."""
    try:
        while data := await source.read(CHUNK):
            sink.write(data)
            await sink.drain()
        if sink.can_write_eof():
            sink.write_eof()
    except (ConnectionError, OSError):
        pass


async def _relay(
    client_r: asyncio.StreamReader,
    client_w: asyncio.StreamWriter,
    origin_r: asyncio.StreamReader,
    origin_w: asyncio.StreamWriter,
) -> None:
    """Copy both ways until one side ends and the other has had `GRACE_S` to drain."""
    up = asyncio.create_task(_pipe(client_r, origin_w))
    down = asyncio.create_task(_pipe(origin_r, client_w))
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


__all__ = ["REFUSED_HEADER", "EgressProxy", "is_public", "resolve_host"]
