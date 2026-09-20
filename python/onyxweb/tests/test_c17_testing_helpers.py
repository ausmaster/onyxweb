"""C17 testing helpers — ``onyxweb.testing``'s fake client acts as ``AsyncClient`` does, so code
written against the real client can be tested without Chrome.

``SURFACE`` pins that each method the fake offers has the real client's signature, so the two
cannot drift apart unnoticed. ``FETCHES`` pin what a fetch returns, ``LIFECYCLES`` what a client
does after it is closed or its Chrome dies, and the factory builds one fake per engine. Nothing
here launches Chrome.
"""

from __future__ import annotations

import inspect
from typing import Any

import onyxweb
import pytest
from onyxweb.testing import FakeClient, FakeClientFactory

URL = "http://example.test/page"
CANNED = onyxweb.RenderResult("<p>CANNED_RESULT</p>", final_url="http://example.test/final")


def _shape(fn: Any) -> list[tuple[str, Any, Any]]:
    """A callable's parameters as (name, kind, default), the part a caller depends on."""
    return [(p.name, p.kind, p.default) for p in inspect.signature(fn).parameters.values()]


@pytest.mark.parametrize("name", ["fetch", "aclose", "__aenter__", "__aexit__"])
def test_the_fake_matches_the_real_clients_signature(name: str) -> None:
    real, fake = getattr(onyxweb.AsyncClient, name), getattr(FakeClient, name)
    assert inspect.iscoroutinefunction(fake) == inspect.iscoroutinefunction(real)
    assert _shape(fake) == _shape(real)


def test_alive_is_a_read_only_property_like_the_real_clients() -> None:
    assert isinstance(onyxweb.AsyncClient.alive, property)
    assert isinstance(FakeClient.alive, property)
    assert FakeClient.alive.fset is None


# Canned pages -> the URL fetched and a fragment its html must carry.
FETCHES: dict[str, tuple[dict[str, str | onyxweb.RenderResult], str, str]] = {
    "canned_html": ({URL: "<p>CANNED_HTML</p>"}, URL, "CANNED_HTML"),
    "an_unknown_url_echoes_itself": ({}, URL, URL),
    "another_url_is_not_the_canned_one": ({"http://elsewhere.test/": "<p>NO</p>"}, URL, URL),
}


@pytest.mark.parametrize("name", list(FETCHES))
async def test_fetch_serves_a_page_for_the_url(name: str) -> None:
    pages, url, says = FETCHES[name]
    page = await FakeClient(pages).fetch(url)
    assert isinstance(page, onyxweb.RenderResult)
    assert says in page.html
    assert page.final_url == url
    assert page.status_code == 0  # a fake page claims no response it never had


async def test_a_canned_result_is_returned_as_it_is() -> None:
    """A test that needs a page with headers, status or buckets hands one in ready-made.

    New test: the rows above build the page from a string, and this one must not rebuild it.
    """
    assert await FakeClient({URL: CANNED}).fetch(URL) is CANNED


async def test_fetch_records_its_calls_with_their_overrides() -> None:
    """A caller's own test can assert what it asked the client for.

    New test: the record is state on the client, which no fetch row reads.
    """
    fake = FakeClient()
    await fake.fetch(URL, wait_after_ms=700)
    await fake.fetch(URL + "2")
    assert fake.fetched == [(URL, {"wait_after_ms": 700}), (URL + "2", {})]


async def test_fetch_refuses_an_unknown_keyword_as_the_real_client_does() -> None:
    """Both validate overrides with one function, so a typo fails in the test, not in production.

    New test: no row above passes an override the real client would reject.
    """
    with pytest.raises(TypeError, match="unknown fetch kwarg: 'nonsense'"):
        await FakeClient().fetch(URL, nonsense=1)


# Action on a fresh client -> (alive after, closed after, what a following fetch raises).
LIFECYCLES: dict[str, tuple[str | None, bool, bool, tuple[type[Exception], str] | None]] = {
    "untouched": (None, True, False, None),
    "closed": ("aclose", False, True, (onyxweb.OnyxwebError, "closed")),
    "died": ("die", False, False, (onyxweb.ChromeExitedError, "exited")),
}


@pytest.mark.parametrize("name", list(LIFECYCLES))
async def test_a_client_after_it_is_closed_or_dies(name: str) -> None:
    action, alive, closed, raises = LIFECYCLES[name]
    fake = FakeClient()
    if action == "aclose":
        await fake.aclose()
    elif action == "die":
        fake.die()
    assert (fake.alive, fake.closed) == (alive, closed)
    if raises is None:
        assert (await fake.fetch(URL)).final_url == URL
        return
    error, says = raises
    with pytest.raises(error, match=says) as exc:
        await fake.fetch(URL)
    if error is onyxweb.ChromeExitedError:
        # Enriched as the real client's errors are, so callers that read them work unchanged.
        assert (exc.value.kind, exc.value.url) == ("chrome_exited", URL)  # type: ignore[attr-defined]


async def test_a_dead_client_can_still_be_closed() -> None:
    """Closing a client whose Chrome is gone must work, as it does for the real one.

    New test: it needs a client that has died and then been closed.
    """
    fake = FakeClient()
    fake.die()
    await fake.aclose()
    assert fake.closed and not fake.alive


async def test_the_error_knob_makes_every_fetch_raise_until_cleared() -> None:
    """``error`` lets a test drive a caller's failure handling.

    New test: it is the one knob that changes what a healthy client returns.
    """
    boom = RuntimeError("boom")
    fake = FakeClient(error=boom)
    with pytest.raises(RuntimeError, match="boom"):
        await fake.fetch(URL)
    fake.error = None
    assert (await fake.fetch(URL)).final_url == URL


async def test_an_async_with_block_closes_the_client() -> None:
    """The fake is a context manager like the real client.

    New test: the rows above never enter one.
    """
    async with FakeClient() as fake:
        assert fake.alive
    assert fake.closed


async def test_the_factory_builds_one_fake_per_call_and_remembers_the_engines() -> None:
    """A test passes the factory where the real code builds a client for an engine.

    New test: the factory is a second object with its own record.
    """
    boom = RuntimeError("boom")
    factory = FakeClientFactory(pages={URL: "<p>FROM_FACTORY</p>"}, error=boom)
    shell, full = factory("shell"), factory("full")
    assert factory.engines == ["shell", "full"]
    assert [client for _, client in factory.built] == [shell, full]
    assert shell is not full
    assert shell.error is boom
    shell.error = None
    assert "FROM_FACTORY" in (await shell.fetch(URL)).html
