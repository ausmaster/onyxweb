"""Per-fetch ``post_load_scripts`` return values: each script's JS return
value is captured (JSON-serialized via CDP ``Runtime.evaluate``) and
exposed as ``RenderResult.post_load_results: list[Any]`` — one entry per
script, in input order.

Replaces the console-marker workaround pattern (``console.log("MARKER:" +
JSON.stringify(value))`` + Python-side parse) for consumers that just want
their ``post_load_script`` to return data straight back.
"""

from __future__ import annotations

from collections.abc import Callable

import onyxweb
import pytest

DataUrl = Callable[[bytes], str]


def test_returns_primitive_values(data_url: DataUrl) -> None:
    url = data_url(b"<html><body>x</body></html>")
    with onyxweb.Client() as c:
        r = c.fetch(
            url,
            post_load_scripts=["42", "'hi'", "null", "true", "false", "3.14"],
        )
    assert r.post_load_results == [42, "hi", None, True, False, 3.14]


def test_returns_object_value(data_url: DataUrl) -> None:
    url = data_url(b"<html><body>x</body></html>")
    with onyxweb.Client() as c:
        r = c.fetch(url, post_load_scripts=["({a: 1, b: [2, 3], c: 'x'})"])
    assert r.post_load_results == [{"a": 1, "b": [2, 3], "c": "x"}]


def test_undefined_returns_none(data_url: DataUrl) -> None:
    """JS ``undefined`` → Python ``None``."""
    url = data_url(b"<html><body>x</body></html>")
    with onyxweb.Client() as c:
        r = c.fetch(url, post_load_scripts=["void 0", "undefined"])
    assert r.post_load_results == [None, None]


def test_function_returns_none(data_url: DataUrl) -> None:
    """Function returns surface as None.

    chromium's RemoteObjectType=Function has no serialized value.
    """
    url = data_url(b"<html><body>x</body></html>")
    with onyxweb.Client() as c:
        r = c.fetch(url, post_load_scripts=["(function() {})"])
    assert r.post_load_results == [None]


def test_dom_nodes_serialize_to_empty_dict(data_url: DataUrl) -> None:
    """DOM nodes serialize to ``{}`` under returnByValue=true.

    chromium can't enumerate them. Consumers needing to distinguish from a
    genuine ``{}`` should filter in their own script (e.g.
    ``JSON.stringify(x) === '{}' ? null : x``).
    """
    url = data_url(b"<html><body>x</body></html>")
    with onyxweb.Client() as c:
        r = c.fetch(
            url,
            post_load_scripts=[
                "document.body",
                "JSON.stringify(document.body) === '{}' ? null : document.body",
            ],
        )
    assert r.post_load_results[0] == {}, "DOM node serializes to {}"
    assert r.post_load_results[1] is None, "filtered DOM node → None"


def test_async_iife_promise_returns_value(data_url: DataUrl) -> None:
    """page.evaluate awaits promises; the resolved value is captured."""
    url = data_url(b"<html><body>x</body></html>")
    with onyxweb.Client() as c:
        r = c.fetch(
            url,
            post_load_scripts=[
                "(async () => { await new Promise(r => setTimeout(r, 50)); return 'done'; })()"
            ],
        )
    assert r.post_load_results == ["done"]


def test_order_matches_input(data_url: DataUrl) -> None:
    """Results list matches input order even when scripts mutate shared state."""
    url = data_url(b"<html><body>x</body></html>")
    with onyxweb.Client() as c:
        r = c.fetch(
            url,
            post_load_scripts=[
                "window.__counter = 0",
                "window.__counter += 1",
                "window.__counter += 10",
                "window.__counter",
            ],
        )
    # 1st: assignment returns 0; 2nd: += returns 1; 3rd: 11; 4th: 11.
    assert r.post_load_results == [0, 1, 11, 11]


def test_no_post_load_scripts_yields_empty_list(data_url: DataUrl) -> None:
    url = data_url(b"<html><body>x</body></html>")
    with onyxweb.Client() as c:
        r = c.fetch(url)
    assert r.post_load_results == []


def test_dom_query_results(data_url: DataUrl) -> None:
    """Demonstrates the workaround-replacement use case: read from the DOM."""
    url = data_url(
        b"<html><body><span id='a'>hello</span>"
        b"<span data-x='1'>foo</span><span data-x='2'>bar</span></body></html>"
    )
    with onyxweb.Client() as c:
        r = c.fetch(
            url,
            post_load_scripts=[
                "document.getElementById('a').textContent",
                "Array.from(document.querySelectorAll('[data-x]')).map(e => e.dataset.x)",
            ],
        )
    assert r.post_load_results == ["hello", ["1", "2"]]


@pytest.mark.asyncio
async def test_async_client_parity(data_url: DataUrl) -> None:
    url = data_url(b"<html><body>x</body></html>")
    async with onyxweb.AsyncClient() as ac:
        r = await ac.fetch(url, post_load_scripts=["1 + 2", "({k: 'v'})"])
    assert r.post_load_results == [3, {"k": "v"}]
