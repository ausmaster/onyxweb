"""C14 server core — a URL, an id or a client request maps to the same guard verdict, page and
eviction on any front-end.

``onyxweb_server.core`` is what MCP and HTTP share. ``check_url`` is tested on its own
(``REFUSED`` / ``ACCEPTED`` / ``HOSTS``). ``ServerCore.fetch`` must apply it, the ceilings and the
client rules before any browser work (``REFUSED_FETCHES``, ``CEILINGS``), and it holds nothing:
``hold`` and ``page`` keep pages under ids with least-recently-used eviction (``EVICTIONS``).
A fake client stands in for the browser, so nothing here launches Chrome.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass
from typing import Any

import onyxweb
import pytest
from conftest import PUBLIC, Factory
from onyxweb_server.core import MAX_WAIT_MS, ServerCore, check_url

# --- the URL guard ----------------------------------------------------------------------

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
ACCEPTED = ("http://93.184.216.34/", "https://8.8.8.8/dns", "http://[2606:4700:4700::1111]/x")

# Hostname -> the addresses it resolves to (a fake resolver, so no DNS is needed).
HOSTS: dict[str, list[str]] = {
    "internal.example": ["10.1.2.3"],
    "mixed.example": ["93.184.216.34", "10.1.2.3"],  # one private answer is enough to refuse
    "meta.example": ["169.254.169.254"],
    "public.example": ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"],
    "gone.example": [],
}
HOST_REFUSED = {
    "internal.example": "private",
    "mixed.example": "private",
    "meta.example": "private",
    "gone.example": "cannot resolve",
}


@pytest.mark.parametrize("url", list(REFUSED))
def test_the_guard_refuses(url: str) -> None:
    with pytest.raises(ValueError) as exc:
        check_url(url)
    message = str(exc.value)
    assert REFUSED[url] in message, message
    assert "public" in message or "http" in message, "the refusal doesn't say what to do"


@pytest.mark.parametrize("url", ACCEPTED)
def test_the_guard_accepts_a_public_address(url: str) -> None:
    check_url(url)


@pytest.mark.parametrize("host", list(HOSTS))
def test_the_guard_resolves_a_hostname(monkeypatch: pytest.MonkeyPatch, host: str) -> None:
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
    expected = HOST_REFUSED.get(host)
    if expected is None:
        check_url(f"http://{host}/")
        return
    with pytest.raises(ValueError, match=expected):
        check_url(f"http://{host}/")


# --- fetch ------------------------------------------------------------------------------

# Target -> URL and the fragment its refusal carries.
REFUSED_FETCHES: dict[str, tuple[str, str]] = {
    "a_local_server": ("http://127.0.0.1:8000/", "private"),
    "a_file": ("file:///etc/passwd", _SCHEME),
    "credentials_in_the_url": ("http://user:pw@93.184.216.34/", "credentials"),
}
# Fetch kwargs -> fragments of the refusal.
CEILINGS: dict[str, tuple[dict[str, Any], tuple[str, ...]]] = {
    "wait_over_the_ceiling": ({"wait_ms": 999_999}, ("wait_ms", str(MAX_WAIT_MS))),
    "wait_below_zero": ({"wait_ms": -1}, ("wait_ms", "0")),
    "unknown_engine": ({"engine": "turbo"}, ("'shell'", "'full'")),
}


@pytest.mark.parametrize("name", list(REFUSED_FETCHES))
async def test_fetch_refuses_before_any_client_is_built(name: str) -> None:
    """The default guard runs inside ``fetch``, ahead of the browser, whoever calls it."""
    url, says = REFUSED_FETCHES[name]
    factory = Factory()
    core = ServerCore(factory)
    with pytest.raises(ValueError, match=says):
        await core.fetch(url)
    assert factory.built == [], "a client was built for a URL the guard refuses"
    # A public URL goes through the same core, so the refusal came from the guard.
    assert (await core.fetch(PUBLIC + "ok")).final_url == PUBLIC + "ok"
    assert factory.engines == ["shell"]


@pytest.mark.parametrize("name", list(CEILINGS))
async def test_fetch_enforces_its_ceilings_before_any_client_is_built(name: str) -> None:
    kwargs, says = CEILINGS[name]
    factory = Factory()
    core = ServerCore(factory)
    with pytest.raises(ValueError) as exc:
        await core.fetch(PUBLIC, **kwargs)
    for fragment in says:
        assert fragment in str(exc.value), str(exc.value)
    assert factory.built == []


async def test_fetch_passes_the_settle_through_and_holds_nothing() -> None:
    """Fetching is stateless: a page is held only when a front-end asks with ``hold``.

    New test: the rows above judge refusals, and this one judges what a good fetch leaves behind.
    """
    factory = Factory()
    core = ServerCore(factory)
    page = await core.fetch(PUBLIC, wait_ms=700)
    assert factory.built[0][1].fetched == [(PUBLIC, 700)]
    assert core.pages() == []
    assert core.page(core.hold(page)) is page


async def test_each_engine_gets_one_client_built_when_first_used() -> None:
    """``engine`` picks the client, and a client is built once however often it is used.

    New test: the rows can't see which client a fetch went through.
    """
    factory = Factory()
    core = ServerCore(factory)
    assert factory.built == [], "a client was built before any fetch"
    await core.fetch(PUBLIC + "a")
    await core.fetch(PUBLIC + "b")
    assert factory.engines == ["shell"]
    await core.fetch(PUBLIC + "c", engine="full")
    assert factory.engines == ["shell", "full"]


async def test_a_dead_client_is_replaced_and_closed_on_the_next_fetch() -> None:
    """A Chrome that died leaves its client useless; the next fetch gets a fresh one.

    New test: nothing else makes a client die between two fetches.
    """
    factory = Factory()
    core = ServerCore(factory)
    await core.fetch(PUBLIC + "a")
    ((_, first),) = factory.built
    assert core.health() == {"shell": True}
    first.alive = False
    assert core.health() == {"shell": False}
    await core.fetch(PUBLIC + "b")
    assert core.health() == {"shell": True}
    assert factory.engines == ["shell", "shell"]
    assert first.closed and not factory.built[1][1].closed


# --- the pages held ---------------------------------------------------------------------


@dataclass(frozen=True)
class Eviction:
    """Pages held (or used) in order, on a store of ``cap``, and the pages left."""

    cap: int
    ops: tuple[tuple[str, str], ...]  # ("hold" | "use", page name)
    held: tuple[str, ...]


EVICTIONS: dict[str, Eviction] = {
    "under_the_cap_keeps_everything": Eviction(
        3, (("hold", "alpha"), ("hold", "beta")), ("alpha", "beta")
    ),
    "the_oldest_goes_first": Eviction(
        2, (("hold", "alpha"), ("hold", "beta"), ("hold", "gamma")), ("beta", "gamma")
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
}


def _capture(name: str) -> onyxweb.RenderResult:
    return onyxweb.RenderResult(
        f"<html><body>{name.upper()}_BODY</body></html>", final_url=PUBLIC + name
    )


@pytest.mark.parametrize("name", list(EVICTIONS))
def test_the_store_holds_at_most_its_cap(name: str) -> None:
    row = EVICTIONS[name]
    core = ServerCore(max_pages=row.cap)
    ids: dict[str, str] = {}
    for op, page in row.ops:
        if op == "hold":
            got = core.hold(_capture(page))
            assert ids.setdefault(page, got) == got, "the same page got a new id"
        else:
            core.page(ids[page])
    listed = {page_id for page_id, _, _ in core.pages()}
    for page, page_id in ids.items():
        assert (page_id in listed) == (page in row.held), (page, listed)
        if page in row.held:
            assert page.upper() in core.page(page_id).html
        else:
            with pytest.raises(ValueError, match="fetch the URL again"):
                core.page(page_id)


def test_an_id_names_one_capture() -> None:
    """A page whose content changed gets a new id, and the old id keeps the old content.

    New test: the page has to change between two holds, which no static row does.
    """
    core = ServerCore()
    first = core.hold(onyxweb.RenderResult("<p>VERSION_ONE</p>", final_url=PUBLIC))
    second = core.hold(onyxweb.RenderResult("<p>VERSION_TWO</p>", final_url=PUBLIC))
    assert first != second
    assert "VERSION_ONE" in core.page(first).html
    assert "VERSION_TWO" in core.page(second).html


def test_a_store_needs_room_for_a_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ONYXWEB_SERVER_MAX_PAGES`` sets the store's size; a value that holds no page is refused."""
    monkeypatch.setenv("ONYXWEB_SERVER_MAX_PAGES", "0")
    with pytest.raises(ValueError, match="ONYXWEB_SERVER_MAX_PAGES must be at least 1"):
        ServerCore()
    monkeypatch.setenv("ONYXWEB_SERVER_MAX_PAGES", "2")
    ServerCore()  # the same variable, set sensibly, builds
    with pytest.raises(ValueError, match="max_pages must be at least 1"):
        ServerCore(max_pages=0)
