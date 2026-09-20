"""C17 testing helpers — ``onyxweb.testing``'s fake client acts as ``AsyncClient`` does, so code
written against the real client can be tested without Chrome.

A row names a state or an input and what any of the client's calls must then do. One test pins
each method's signature to the real client's. ``FETCHES`` serve a page through ``fetch``,
``fetch_all`` and ``batch``. ``LIFECYCLES`` put the fake in a state (closed, dead, failing, a
context manager) and run every call shape through it. ``MAGIC``, ``BATCHES``, ``RECORDED`` and
``REFUSED_CALLS`` pin the image, batch, record and validation edges. Nothing here launches Chrome.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

import onyxweb
import pytest
from onyxweb.testing import FakeClient, FakeClientFactory

URL = "http://example.test/page"
CANNED = onyxweb.RenderResult("<p>CANNED_RESULT</p>", final_url="http://example.test/final")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
Result = onyxweb.RenderResult | onyxweb.FetchResult | bytes

# Every call shape on one URL. A batch's result is its list; `_run` unwraps it.
CALLS: dict[str, Callable[[FakeClient], Awaitable[Any]]] = {
    "fetch": lambda f: f.fetch(URL),
    "screenshot": lambda f: f.screenshot(URL),
    "fetch_all": lambda f: f.fetch_all(URL),
    "batch": lambda f: f.batch([URL]),
}
# The call shapes that return a page, on any URL.
PAGE_CALLS: dict[str, Callable[[FakeClient, str], Awaitable[Any]]] = {
    "fetch": lambda f, u: f.fetch(u),
    "fetch_all": lambda f, u: f.fetch_all(u),
    "batch": lambda f, u: f.batch([u]),
}


# --- signatures ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["fetch", "screenshot", "fetch_all", "batch", "aclose", "__aenter__", "__aexit__", "alive"],
)
def test_the_fake_matches_the_real_clients_signature(name: str) -> None:
    def shape(fn: Any) -> list[tuple[str, Any, Any]]:
        """A callable's parameters as (name, kind, default), the part a caller depends on."""
        return [(p.name, p.kind, p.default) for p in inspect.signature(fn).parameters.values()]

    real, fake = getattr(onyxweb.AsyncClient, name), getattr(FakeClient, name)
    if name == "alive":  # a read-only property on both
        assert isinstance(real, property) and isinstance(fake, property)
        assert fake.fset is None
        return
    assert inspect.iscoroutinefunction(fake) == inspect.iscoroutinefunction(real)
    assert shape(fake) == shape(real)


# --- pages ------------------------------------------------------------------------------------

# Canned pages -> the URL asked for, a fragment the html must carry, and whether the very object
# given comes back (a ready-made `RenderResult` is served as it is).
FETCHES: dict[str, tuple[dict[str, str | onyxweb.RenderResult], str, str, bool]] = {
    "canned_html": ({URL: "<p>CANNED_HTML</p>"}, URL, "CANNED_HTML", False),
    "an_unknown_url_echoes_itself": ({}, URL, URL, False),
    "another_url_is_not_the_canned_one": ({"http://elsewhere.test/": "<p>NO</p>"}, URL, URL, False),
    "a_ready_made_result_is_served_as_it_is": ({URL: CANNED}, URL, "CANNED_RESULT", True),
}


@pytest.mark.parametrize("op", list(PAGE_CALLS))
@pytest.mark.parametrize("name", list(FETCHES))
async def test_a_page_is_served_for_the_url(name: str, op: str) -> None:
    pages, url, says, same = FETCHES[name]
    out = await PAGE_CALLS[op](FakeClient(pages), url)
    page = out.html if isinstance(out, onyxweb.FetchResult) else out[0] if op == "batch" else out
    assert isinstance(page, onyxweb.RenderResult)
    assert says in page.html
    if same:
        assert page is pages[url]
    else:
        assert (page.final_url, page.status_code) == (url, 0)  # a fake claims no response
    if isinstance(out, onyxweb.FetchResult):  # forwards its page's fields, and carries an image
        assert (out.final_url, out.status_code) == (page.final_url, page.status_code)
        assert out.png[:8] == PNG_MAGIC


# --- states -----------------------------------------------------------------------------------

# State (actions on a fresh client) -> (alive, closed, what a following call raises; None: a page).
LIFECYCLES: dict[str, tuple[tuple[str, ...], bool, bool, tuple[type[Exception], str] | None]] = {
    "untouched": ((), True, False, None),
    "closed": (("aclose",), False, True, (onyxweb.OnyxwebError, "closed")),
    "died": (("die",), False, False, (onyxweb.ChromeExitedError, "exited")),
    # Closing a client whose Chrome is gone works, as it does for the real one.
    "died_then_closed": (("die", "aclose"), False, True, (onyxweb.OnyxwebError, "closed")),
    "used_as_a_context_manager": (("with",), False, True, (onyxweb.OnyxwebError, "closed")),
    # `error` lets a test drive a caller's failure handling; clearing it lets calls succeed.
    "the_error_knob_set": (("error",), True, False, (RuntimeError, "boom")),
    "the_error_knob_cleared": (("error", "clear"), True, False, None),
}


@pytest.mark.parametrize("op", list(CALLS))
@pytest.mark.parametrize("name", list(LIFECYCLES))
async def test_a_client_in_any_state_answers_every_call(name: str, op: str) -> None:
    async def run() -> Result:
        """The call's result; a failure a batch returns in place is raised, like the others."""
        out = await CALLS[op](fake)
        if not isinstance(out, list):
            return out  # type: ignore[no-any-return]
        [item] = out
        if isinstance(item, Exception):
            raise item
        return item  # type: ignore[no-any-return]

    actions, alive, closed, raises = LIFECYCLES[name]
    fake = FakeClient()
    for action in actions:
        if action == "aclose":
            await fake.aclose()
        elif action == "die":
            fake.die()
        elif action == "with":
            async with fake:
                assert fake.alive
        elif action == "error":
            fake.error = RuntimeError("boom")
        else:
            fake.error = None
    assert (fake.alive, fake.closed) == (alive, closed)
    if raises is None:
        out = await run()
        if isinstance(out, bytes):
            assert out[:8] == PNG_MAGIC
        else:
            page = out.html if isinstance(out, onyxweb.FetchResult) else out
            assert page.final_url == URL
        return
    error, says = raises
    with pytest.raises(error, match=says) as exc:
        await run()
    if error is onyxweb.ChromeExitedError:
        # Enriched as the real client's errors are, so callers that read them work unchanged.
        assert (exc.value.kind, exc.value.url) == ("chrome_exited", URL)  # type: ignore[attr-defined]


# --- images and batches ---------------------------------------------------------------------

# Format -> whether the bytes open as that format.
MAGIC: dict[str, Callable[[bytes], bool]] = {
    "png": lambda b: b[:8] == PNG_MAGIC,
    "jpeg": lambda b: b[:3] == b"\xff\xd8\xff",
    "webp": lambda b: b[:4] == b"RIFF" and b[8:12] == b"WEBP",
}


@pytest.mark.parametrize("fmt", list(MAGIC))
@pytest.mark.parametrize("call", ["screenshot", "fetch_all"])
async def test_an_image_comes_back_in_the_format_asked(call: str, fmt: str) -> None:
    out = await getattr(FakeClient(), call)(URL, format=fmt)
    assert MAGIC[fmt](out.png if call == "fetch_all" else out)


# Name -> (URLs, capture, state before it, what comes back). A tuple of type names is the items,
# in order; an exception type and a message is what the whole call raises.
BATCHES: dict[str, tuple[list[str], str, str, Any]] = {
    "html_in_order": ([URL, URL + "2", URL + "3"], "html", "", ("RenderResult",) * 3),
    "png": ([URL, URL + "2"], "png", "", ("bytes",) * 2),
    "both": ([URL, URL + "2"], "both", "", ("FetchResult",) * 2),
    "empty": ([], "html", "", ()),
    # A dead Chrome is returned in each URL's place, so the rest of a caller's loop still runs.
    "a_dead_chrome_is_returned_in_place": (
        [URL, URL + "2"],
        "html",
        "die",
        ("ChromeExitedError",) * 2,
    ),
    "a_closed_client_refuses_the_whole_call": (
        [URL],
        "html",
        "aclose",
        (onyxweb.OnyxwebError, "closed"),
    ),
    "an_unknown_capture_names_the_valid_ones": (
        [URL],
        "invalid",
        "",
        (ValueError, r"capture must be 'html'\|'png'\|'both'"),
    ),
}


@pytest.mark.parametrize("name", list(BATCHES))
async def test_a_batch_returns_one_item_per_url_in_order(name: str) -> None:
    urls, capture, state, expected = BATCHES[name]
    fake = FakeClient()
    if state == "die":
        fake.die()
    elif state == "aclose":
        await fake.aclose()
    if expected and isinstance(expected[0], type):
        with pytest.raises(expected[0], match=expected[1]):
            await fake.batch(urls, capture=capture)  # type: ignore[arg-type]
        return
    items = await fake.batch(urls, capture=capture)  # type: ignore[arg-type]
    assert [type(i).__name__ for i in items] == list(expected)
    # Each item answers for its own URL: a page by `final_url`, a failure by `url`.
    for url, item in zip(urls, items, strict=True):
        assert getattr(item, "final_url", getattr(item, "url", url)) == url


# --- records and validation ----------------------------------------------------------------

# Call -> what `fetched` must hold after it: (url, the overrides the caller set).
RECORDED: dict[
    str, tuple[Callable[[FakeClient], Awaitable[object]], list[tuple[str, dict[str, Any]]]]
] = {
    "fetch": (lambda f: f.fetch(URL, wait_after_ms=700), [(URL, {"wait_after_ms": 700})]),
    "fetch_without_overrides": (lambda f: f.fetch(URL), [(URL, {})]),
    "screenshot": (
        lambda f: f.screenshot(URL, format="jpeg", full_page=True),
        [(URL, {"format": "jpeg", "full_page": True})],
    ),
    "fetch_all": (
        lambda f: f.fetch_all(URL, full_page=True, format="webp", quality=50, timeout_ms=900),
        [(URL, {"full_page": True, "format": "webp", "quality": 50, "timeout_ms": 900})],
    ),
    # A quality of 0 is a choice, not an absent one.
    "fetch_all_quality_zero": (lambda f: f.fetch_all(URL, quality=0), [(URL, {"quality": 0})]),
    "fetch_all_with_defaults": (lambda f: f.fetch_all(URL), [(URL, {})]),
    "batch": (
        lambda f: f.batch([URL, URL + "2"], config=onyxweb.FetchConfig(timeout_ms=900)),
        [(URL, {"timeout_ms": 900}), (URL + "2", {"timeout_ms": 900})],
    ),
}


@pytest.mark.parametrize("name", list(RECORDED))
async def test_every_call_is_recorded_with_its_overrides(name: str) -> None:
    call, expected = RECORDED[name]
    fake = FakeClient()
    await call(fake)
    assert fake.fetched == expected


# Call -> the message a keyword the real client would reject raises, so a typo fails in the test.
REFUSED_CALLS: dict[str, tuple[Callable[[FakeClient], Awaitable[object]], str]] = {
    "fetch": (lambda f: f.fetch(URL, nonsense=1), "unknown fetch kwarg: 'nonsense'"),
    "screenshot": (
        lambda f: f.screenshot(URL, nonsense=1),
        "unknown screenshot kwarg: 'nonsense'",
    ),
    "fetch_all": (lambda f: f.fetch_all(URL, nonsense=1), "unknown fetch kwarg: 'nonsense'"),
}


@pytest.mark.parametrize("name", list(REFUSED_CALLS))
async def test_an_unknown_keyword_is_refused_as_the_real_client_does(name: str) -> None:
    call, says = REFUSED_CALLS[name]
    with pytest.raises(TypeError, match=says):
        await call(FakeClient())


async def test_the_factory_builds_one_fake_per_call_and_remembers_the_engines() -> None:
    """A test passes the factory where the real code builds a client for an engine.

    New test: the factory is a second object, not a state of the client the tables above run.
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
