"""C1 capture — a page plus capture knobs → HTML, console, script results and
screenshot reflect the page at the configured wait point.

Sync only: sync and async route through the same ``do_*_inner`` helpers (C6 pins
shape parity), so this file never duplicates a case for ``AsyncClient``.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import quote

import onyxweb
import pytest
from conftest import JPEG_MAGIC, PNG_MAGIC, is_webp
from onyxweb import Click, Fill, Hover, Wait
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Request, Response


@pytest.fixture(scope="module")
def client() -> Iterator[onyxweb.Client]:
    with onyxweb.Client(concurrency=1) as c:
        yield c


def _b64(html: bytes) -> str:
    return "data:text/html;base64," + base64.b64encode(html).decode()


# ----------------------------------------------------------------------------
# ACTIONS — Click / Fill / Hover / Wait, run after the lifecycle event
# ----------------------------------------------------------------------------

_ACTIONS_PAGE = _b64(
    b"<html><body>"
    b"<button id='trusted' onclick=\"document.body.dataset.trusted = event.isTrusted\">t</button>"
    b"<button id='mutate' onclick=\"document.body.dataset.mutated = 'yes'\">m</button>"
    b"<input id='i' value='preexisting' />"
    b"<button id='submit' onclick=\"document.body.dataset.value = "
    b"document.getElementById('i').value\">s</button>"
    b"<div id='hover' style='width:50px;height:50px' "
    b'onmouseover="document.body.dataset.hovered = event.isTrusted">h</div>'
    b"<script>"
    b"document.getElementById('i').addEventListener('input', e => "
    b"document.body.dataset.inputEvent = e.target.value);"
    b"document.getElementById('i').addEventListener('change', e => "
    b"document.body.dataset.changeEvent = e.target.value);"
    b"</script>"
    b"</body></html>"
)


@dataclass(frozen=True)
class ActionCase:
    """One ``actions`` sequence and what it must leave behind."""

    actions: list[Any]
    dataset: dict[str, str] = field(default_factory=dict)  # data-* attr -> expected value
    errors_contain: str | None = None  # a fragment r.errors must carry
    errors_empty: bool = False


ACTIONS: dict[str, ActionCase] = {
    "click_is_trusted": ActionCase([Click(type="click", selector="#trusted")], {"trusted": "true"}),
    "click_mutates_html": ActionCase([Click(type="click", selector="#mutate")], {"mutated": "yes"}),
    "fill_sets_value_and_fires_events": ActionCase(
        [Fill(type="fill", selector="#i", value="hi")],
        {"inputEvent": "hi", "changeEvent": "hi"},
    ),
    "fill_replaces_not_appends": ActionCase(
        [
            Fill(type="fill", selector="#i", value="REPLACEMENT"),
            Click(type="click", selector="#submit"),
        ],
        {"value": "REPLACEMENT"},  # not "preexistingREPLACEMENT"
    ),
    "hover_is_trusted": ActionCase([Hover(type="hover", selector="#hover")], {"hovered": "true"}),
    "fill_then_click_runs_in_order": ActionCase(
        [
            Fill(type="fill", selector="#i", value="ORDERED"),
            Click(type="click", selector="#submit"),
        ],
        {"value": "ORDERED"},
    ),
    "continue_policy_records_and_runs_next": ActionCase(
        [Click(type="click", selector="#nonexistent"), Click(type="click", selector="#mutate")],
        {"mutated": "yes"},
        errors_contain="click(#nonexistent)",
    ),
    "ignore_policy_runs_next_without_recording": ActionCase(
        [
            Click(type="click", selector="#missing", on_error="ignore"),
            Click(type="click", selector="#mutate"),
        ],
        {"mutated": "yes"},
        errors_empty=True,
    ),
    # Each action's own wait_after_ms delays before the NEXT action, not just the end.
    "per_action_wait_after_ms_delays_the_next_action": ActionCase(
        [
            Click(type="click", selector="#mutate", wait_after_ms=200),
            Click(type="click", selector="#trusted"),
        ],
        {"mutated": "yes", "trusted": "true"},
    ),
}


def _kebab(name: str) -> str:
    """camelCase -> kebab-case, matching how a ``dataset`` key becomes an attribute."""
    return "".join(f"-{c.lower()}" if c.isupper() else c for c in name)


@pytest.mark.parametrize("name", list(ACTIONS))
def test_action(client: onyxweb.Client, name: str) -> None:
    case = ACTIONS[name]
    r = client.fetch(_ACTIONS_PAGE, actions=case.actions, wait_after_ms=150)
    for attr, expected in case.dataset.items():
        marker = f'data-{_kebab(attr)}="{expected}"'
        assert marker in r, f"{marker!r} not in captured html: {r.html[:300]!r}"
    if case.errors_contain is not None:
        assert any(case.errors_contain in e for e in r.errors), r.errors
    if case.errors_empty:
        assert r.errors == []


def test_wait_action_sleeps_for_its_duration(client: onyxweb.Client) -> None:
    blank = _b64(b"<html><body>x</body></html>")
    baseline_s = _timed(lambda: client.fetch(blank))
    with_wait_s = _timed(lambda: client.fetch(blank, actions=[Wait(type="wait", duration_ms=500)]))
    delta = with_wait_s - baseline_s
    assert 0.4 <= delta < 1.5, f"Wait(500) added {delta:.3f}s (baseline {baseline_s:.3f}s)"


def _timed(call: Callable[[], object]) -> float:
    t0 = time.perf_counter()
    call()
    return time.perf_counter() - t0


# ----------------------------------------------------------------------------
# PLS — post_load_scripts: DOM access, ordering, mutation, click loops, forms
# ----------------------------------------------------------------------------


def test_pls_reads_dom_the_page_itself_built(client: onyxweb.Client) -> None:
    """post_load_scripts run after the page's own scripts — they see what those built."""
    page = _b64(
        b"<html><body><script>"
        b"const d = document.createElement('div'); d.id = 'late'; d.textContent = 'BUILT';"
        b"document.body.appendChild(d);"
        b"</script></body></html>"
    )
    r = client.fetch(page, post_load_scripts=["document.getElementById('late').textContent"])
    assert r.post_load_results == ["BUILT"]


def test_pls_run_in_order(client: onyxweb.Client) -> None:
    blank = _b64(b"<html><body>x</body></html>")
    r = client.fetch(
        blank,
        post_load_scripts=[
            "window.__o = []",
            "window.__o.push(1)",
            "window.__o.push(2); window.__o",
        ],
    )
    assert r.post_load_results[-1] == [1, 2]


def test_pls_dom_mutation_reflected_in_capture(client: onyxweb.Client) -> None:
    page = _b64(b"<html><body><div id='t'>ORIGINAL</div></body></html>")
    r = client.fetch(
        page, post_load_scripts=["document.getElementById('t').textContent = 'MUTATED'"]
    )
    assert "MUTATED" in r
    assert "ORIGINAL" not in r


def test_pls_click_loop_fires_onclick_and_javascript_urls(client: onyxweb.Client) -> None:
    page = _b64(
        b"<html><body>"
        b"<button onclick=\"console.error('CLICKED_b1')\">b1</button>"
        b"<a href=\"javascript:console.error('JS_URL_a1')\">a1</a>"
        b"</body></html>"
    )
    loop = (
        "[...document.querySelectorAll('[onclick]'),"
        " ...document.querySelectorAll('[href^=\"javascript:\"]')].forEach(el => el.click())"
    )
    r = client.fetch(
        page, post_load_scripts=[loop], block_navigation=True, wait_after_post_load_ms=200
    )
    texts = [m.text for m in r.console_messages]
    assert any("CLICKED_b1" in t for t in texts)
    assert any("JS_URL_a1" in t for t in texts)


def test_pls_form_fill_and_submit_via_synthetic_click(client: onyxweb.Client) -> None:
    page = _b64(
        b"<html><body><form onsubmit=\"console.error('SUBMITTED_' + this.q.value); return false\">"
        b"<input name='q' /><button type='submit'>Go</button></form></body></html>"
    )
    script = (
        "document.querySelector('input[name=q]').value = 'PAYLOAD';"
        "document.querySelector('button[type=submit]').click();"
    )
    r = client.fetch(page, post_load_scripts=[script])
    assert any("SUBMITTED_PAYLOAD" in m.text for m in r.console_messages)


def test_pls_awaits_an_async_iife(client: onyxweb.Client) -> None:
    """A Promise the script returns is awaited before capture, not raced."""
    page = _b64(b"<html><body><div id='t'>before</div></body></html>")
    script = (
        "(async () => {await new Promise(r => setTimeout(r, 400));"
        "document.getElementById('t').setAttribute('data-mark', 'AWAITED');})()"
    )
    r = client.fetch(page, post_load_scripts=[script])
    assert 'data-mark="AWAITED"' in r


def test_pls_caught_exception_proceeds(client: onyxweb.Client) -> None:
    page = _b64(b"<html><body><div id='after'></div></body></html>")
    script = (
        "try { throw new Error('BOOM'); } catch (e) { console.error('caught: ' + e.message); }"
        "document.getElementById('after').setAttribute('data-ran', 'yes');"
    )
    r = client.fetch(page, post_load_scripts=[script])
    assert any("caught: BOOM" in m.text for m in r.console_messages)
    assert 'data-ran="yes"' in r


# ----------------------------------------------------------------------------
# JS_VALUES — every post_load_script return-value shape, in one fetch
# ----------------------------------------------------------------------------

_JS_VALUES_PAGE = _b64(
    b"<html><body><span id='a'>hello</span>"
    b"<span data-x='1'>foo</span><span data-x='2'>bar</span></body></html>"
)
# Script -> expected Python value. Order matters: later scripts read state earlier ones set.
JS_VALUES: list[tuple[str, Any]] = [
    ("42", 42),
    ("'hi'", "hi"),
    ("null", None),
    ("true", True),
    ("false", False),
    ("3.14", 3.14),
    ("({a: 1, b: [2, 3], c: 'x'})", {"a": 1, "b": [2, 3], "c": "x"}),
    ("void 0", None),
    ("undefined", None),
    ("(function() {})", None),  # a function has no serialized value
    ("document.body", {}),  # a DOM node serializes to {}
    (
        "JSON.stringify(document.body) === '{}' ? null : document.body",
        None,
    ),  # filtered by the script itself
    ("(async () => { await new Promise(r => setTimeout(r, 20)); return 'done'; })()", "done"),
    ("window.__c = 0", 0),
    ("window.__c += 1", 1),
    ("window.__c += 10", 11),
    ("window.__c", 11),
    ("document.getElementById('a').textContent", "hello"),
    ("Array.from(document.querySelectorAll('[data-x]')).map(e => e.dataset.x)", ["1", "2"]),
    # CDP represents a bare special number via `unserializableValue`, which onyxweb
    # doesn't read — same bucket as `undefined` / a function. Nested, the container is
    # JSON-encoded instead: NaN/Infinity -> null (matches JSON.stringify), -0 -> 0.
    ("NaN", None),
    ("Infinity", None),
    ("-Infinity", None),
    ("-0", None),
    ("[NaN, Infinity, -0]", [None, None, 0]),
    # A Date has no enumerable own properties, so it serializes like any other object.
    ("new Date(0)", {}),
    ("new Date(0).toISOString()", "1970-01-01T00:00:00.000Z"),
]


def test_js_values_in_one_fetch(client: onyxweb.Client) -> None:
    scripts = [s for s, _ in JS_VALUES]
    expected = [v for _, v in JS_VALUES]
    r = client.fetch(_JS_VALUES_PAGE, post_load_scripts=scripts)
    assert r.post_load_results == expected


def test_no_post_load_scripts_yields_an_empty_list(client: onyxweb.Client) -> None:
    assert client.fetch(_JS_VALUES_PAGE).post_load_results == []


# ----------------------------------------------------------------------------
# SETTLE — wait_after_post_load_ms, the delay AFTER post_load_scripts
# ----------------------------------------------------------------------------


def test_settle_captures_a_deferred_mutation_when_the_knob_covers_it(
    client: onyxweb.Client,
) -> None:
    page = _b64(b"<html><body><div id='t'>initial</div></body></html>")
    schedule = "setTimeout(() => {document.getElementById('t').textContent = 'ASYNC_DONE';}, 300);"
    r = client.fetch(page, post_load_scripts=[schedule], wait_after_post_load_ms=500)
    assert "ASYNC_DONE" in r


def test_settle_absent_misses_the_same_deferred_mutation(client: onyxweb.Client) -> None:
    """The complement of the row above: without the knob, the same page is captured too soon."""
    page = _b64(b"<html><body><div id='t'>initial</div></body></html>")
    schedule = "setTimeout(() => {document.getElementById('t').textContent = 'ASYNC_DONE';}, 300);"
    r = client.fetch(page, post_load_scripts=[schedule])
    assert "ASYNC_DONE" not in r
    assert "initial" in r


def test_settle_default_adds_no_delay(client: onyxweb.Client) -> None:
    blank = _b64(b"<html><body>x</body></html>")
    client.fetch(blank)  # warm
    elapsed = _timed(lambda: client.fetch(blank, post_load_scripts=["1"]))
    assert elapsed < 0.5, f"default settle added latency: {elapsed:.3f}s"


def test_settle_knob_adds_its_delay(client: onyxweb.Client) -> None:
    blank = _b64(b"<html><body>x</body></html>")
    client.fetch(blank)  # warm
    elapsed = _timed(
        lambda: client.fetch(blank, post_load_scripts=["1+1"], wait_after_post_load_ms=400)
    )
    assert elapsed >= 0.35, f"settle knob didn't apply: {elapsed:.3f}s"


def test_settle_fires_after_scripts_not_before(client: onyxweb.Client) -> None:
    """A synchronous mutation is visible with no settle at all — the settle isn't needed for it."""
    page = _b64(b"<html><body><div id='t'>before</div></body></html>")
    r = client.fetch(page, post_load_scripts=["document.getElementById('t').textContent = 'SYNC'"])
    assert "SYNC" in r


# ----------------------------------------------------------------------------
# CONSOLE — capture_console_level filters which console.* methods are kept
# ----------------------------------------------------------------------------

_CONSOLE_PAGE = _b64(
    b"<html><script>"
    b"console.log('m_log'); console.info('m_info'); console.warn('m_warn');"
    b"console.error('m_error'); console.debug('m_debug'); console.trace('m_trace');"
    b"throw new Error('m_uncaught');"
    b"</script></html>"
)
# Level -> the console types it must keep.
CONSOLE_LEVELS: dict[str, set[str]] = {
    "error": {"error"},  # default: only console.error + the uncaught exception
    "warn": {"warning", "error"},
    "all": {"log", "info", "warning", "error", "debug", "trace"},
}


@pytest.mark.parametrize("level", list(CONSOLE_LEVELS))
def test_console_level_filters_captured_types(level: str) -> None:
    with onyxweb.Client(capture_console_level=level) as c:
        r = c.fetch(_CONSOLE_PAGE)
    kept = {m.type for m in r.console_messages}
    assert kept == CONSOLE_LEVELS[level]
    # Invariant: capture-window timestamps, dispatch order, and errors == error-type texts.
    before_seen = r.console_messages[0].timestamp if r.console_messages else 0
    for m in r.console_messages:
        assert m.timestamp >= before_seen
        before_seen = m.timestamp
    assert r.errors == [m.text for m in r.console_messages if m.type == "error"]
    if "error" in kept:
        assert any("m_uncaught" in m.text for m in r.console_messages if m.type == "error")


def test_console_level_invalid_raises_validation_error() -> None:
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        onyxweb.ClientConfig(capture_console_level="invalid")  # type: ignore[arg-type]


def test_render_result_console_messages_defaults_empty(client: onyxweb.Client) -> None:
    blank = _b64(b"<html><body>no console</body></html>")
    assert client.fetch(blank).console_messages == []


# ----------------------------------------------------------------------------
# DIALOGS — native alert/confirm/prompt are auto-dismissed, never hang
# ----------------------------------------------------------------------------


def test_alert_does_not_hang_and_the_page_continues(client: onyxweb.Client) -> None:
    page = _b64(b"<html><body><script>alert('x')</script><div id='ok'>OK</div></body></html>")
    r = client.fetch(page, timeout_ms=5000)
    assert "OK" in r
    assert r.status_code == 200


def test_confirm_is_dismissed_as_false(client: onyxweb.Client) -> None:
    page = _b64(
        b"<html><body><script>"
        b"document.body.dataset.branch = confirm('x') ? 'accepted' : 'dismissed';"
        b"</script></body></html>"
    )
    r = client.fetch(page, timeout_ms=5000)
    assert 'data-branch="dismissed"' in r


def test_prompt_is_dismissed_as_null(client: onyxweb.Client) -> None:
    page = _b64(
        b"<html><body><script>"
        b"const v = prompt('x', 'default');"
        b"document.body.dataset.result = v === null ? 'null' : 'value:' + v;"
        b"</script></body></html>"
    )
    r = client.fetch(page, timeout_ms=5000)
    assert 'data-result="null"' in r


def test_sequential_dialogs_all_dismiss_in_order(client: onyxweb.Client) -> None:
    page = _b64(
        b"<html><body><script>"
        b"alert('first'); const c = confirm('second?'); const p = prompt('third?');"
        b"document.body.dataset.results = 'confirm=' + c + ';prompt=' + (p === null ? 'null' : p);"
        b"</script></body></html>"
    )
    r = client.fetch(page, timeout_ms=5000)
    assert 'data-results="confirm=false;prompt=null"' in r


def test_post_load_alert_does_not_hang(client: onyxweb.Client) -> None:
    """A dialog triggered by post_load_scripts (not the page's own scripts) also dismisses."""
    page = _b64(
        b"<html><body><button id='b' "
        b"onclick=\"alert('x'); this.dataset.after = 'dismissed'\">x</button></body></html>"
    )
    r = client.fetch(
        page, post_load_scripts=["document.getElementById('b').click()"], timeout_ms=5000
    )
    assert r.status_code == 200
    assert 'data-after="dismissed"' in r  # set only once alert() returned


# ----------------------------------------------------------------------------
# WAIT_POINTS — the lifecycle event a fetch waits for
# ----------------------------------------------------------------------------

_SLOW_S = 1.5


@pytest.fixture
def tserver() -> Iterator[HTTPServer]:
    """Threaded so a sleeping subframe/handler doesn't block the main doc."""
    with HTTPServer(threaded=True) as server:
        yield server


def _serve_iframe_page(server: HTTPServer) -> str:
    server.expect_request("/iframe").respond_with_data(
        "<html><body>main<iframe src='/slow'></iframe></body></html>", content_type="text/html"
    )

    def slow(_r: Request) -> Response:
        time.sleep(_SLOW_S)
        return Response("<html><body>slow-done</body></html>", content_type="text/html")

    server.expect_request("/slow").respond_with_handler(slow)
    return server.url_for("/iframe")


def test_domcontentloaded_returns_before_a_slow_subframe(tserver: HTTPServer) -> None:
    url = _serve_iframe_page(tserver)
    with onyxweb.Client(concurrency=1) as c:
        c.fetch("data:text/html,<html></html>")  # warm the pooled tab
        elapsed = _timed(lambda: c.fetch(url, wait_until="domcontentloaded"))
    assert elapsed < 0.8, f"DCL waited for the {_SLOW_S}s subframe: {elapsed:.2f}s"


def test_load_waits_for_a_slow_subframe(tserver: HTTPServer) -> None:
    url = _serve_iframe_page(tserver)
    with onyxweb.Client(concurrency=1) as c:
        c.fetch("data:text/html,<html></html>")
        elapsed = _timed(lambda: c.fetch(url, wait_until="load"))
    assert elapsed >= _SLOW_S - 0.3, f"load returned before the subframe: {elapsed:.2f}s"


def test_dcl_fetch_does_not_leak_a_pending_load_into_the_next_fetch(tserver: HTTPServer) -> None:
    """Regression for the DCL empty-capture bug (memory ``dcl-empty-capture-bug``).

    A DCL-mode fetch returns before its own page's ``load`` fires (a slow ``<img>``
    keeps it pending). The next fetch — a large, otherwise-instant page — must not
    mistake that still-pending ``load`` for its own: every pooled-tab fetch leaves for
    a blank page first, so it never depends on what the tab was last doing.
    """

    def slow_img(_r: Request) -> Response:
        time.sleep(0.8)
        return Response(b"\x89PNG", content_type="image/png")

    tserver.expect_request("/slow.png").respond_with_handler(slow_img)
    tserver.expect_request("/page1").respond_with_data(
        "<html><body>page1<img src='/slow.png'></body></html>", content_type="text/html"
    )
    big = "<html><body>" + "x" * 700_000 + "</body></html>"
    tserver.expect_request("/page2").respond_with_data(big, content_type="text/html")
    with onyxweb.Client(concurrency=1, wait_until="domcontentloaded") as c:
        r1 = c.fetch(tserver.url_for("/page1"))
        r2 = c.fetch(tserver.url_for("/page2"))
    assert r1.metadata.content_length > 0
    # Chrome's own serialization adds a little (e.g. an implicit <head>), so this
    # checks "the full page," not a byte-exact echo of the served source.
    assert r2.metadata.content_length > 700_000, (
        f"expected page2's full ~700 KB body, got {r2.metadata.content_length} bytes"
    )


def test_capture_returns_the_new_page_despite_a_prior_pushstate(tserver: HTTPServer) -> None:
    """The prior page keeps firing pushState and the next page is slow to respond; the
    goto/lifecycle race must not resolve on the outgoing page's events mid-navigation."""
    tserver.expect_request("/spa").respond_with_data(
        "<html><body>SPA_MARKER<script>setInterval(function(){"
        "history.pushState({}, '', '#' + Date.now());}, 100);</script></body></html>",
        content_type="text/html",
    )

    def slow_next(_r: Request) -> Response:
        time.sleep(1.2)
        return Response("<html><body>NEXT_MARKER</body></html>", content_type="text/html")

    tserver.expect_request("/next").respond_with_handler(slow_next)
    with onyxweb.Client(concurrency=1) as c:
        c.fetch(tserver.url_for("/spa"))
        r = c.fetch(tserver.url_for("/next"))
    assert r.status_code == 200
    assert "NEXT_MARKER" in r
    assert "SPA_MARKER" not in r


def test_referer_survives_the_goto_lifecycle_race(tserver: HTTPServer) -> None:
    tserver.expect_request("/").respond_with_data(
        "<html><body>ok</body></html>", content_type="text/html"
    )
    with onyxweb.Client(concurrency=1) as c:
        c.fetch(
            tserver.url_for("/"),
            wait_until="domcontentloaded",
            extra_headers={"Referer": "http://ref.example/x"},
        )
    reqs = [r for r, _ in tserver.log if r.path == "/"]
    assert reqs and reqs[-1].headers.get("Referer") == "http://ref.example/x"


# ----------------------------------------------------------------------------
# SAME_DOC — a fetch whose URL differs from the tab's page only by fragment
# ----------------------------------------------------------------------------


def test_hash_only_navs_complete_without_timeout(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/").respond_with_data(
        "<html><body><h1>same-doc</h1></body></html>", content_type="text/html"
    )
    base = httpserver.url_for("/")
    with onyxweb.Client(concurrency=1) as c:
        elapsed = _timed(lambda: [c.fetch(base), c.fetch(base + "#abc"), c.fetch(base + "#xyz")])
    assert elapsed < 8.0, f"3 hash fetches took {elapsed:.1f}s"


def test_query_only_nav_then_hash_change(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/").respond_with_data(
        "<html><body>x</body></html>", content_type="text/html"
    )
    base = httpserver.url_for("/")
    with onyxweb.Client(concurrency=1) as c:
        r1 = c.fetch(base + "?q=1")
        r2 = c.fetch(base + "?q=1#abc")
    assert r1.status_code == 200
    assert r2.status_code == 200


def test_dcl_mode_handles_same_doc_navs_too(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/").respond_with_data(
        "<html><body>x</body></html>", content_type="text/html"
    )
    base = httpserver.url_for("/")
    with onyxweb.Client(concurrency=1, wait_until="domcontentloaded") as c:
        r1 = c.fetch(base)
        r2 = c.fetch(base + "#abc")
    assert r1.status_code == 200
    assert r2.status_code == 200


def test_same_doc_with_init_scripts_loads_fresh(httpserver: HTTPServer) -> None:
    """Per-call init scripts only fire on a new document, and a hash-only fetch loads
    one by default — without changing the URL the page sees."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body><div id='out'></div><script>document.getElementById('out').textContent = "
        "(window.__hooked === true ? 'hooked' : 'not_hooked');</script></body></html>",
        content_type="text/html",
    )
    base = httpserver.url_for("/")
    with onyxweb.Client(concurrency=1) as c:
        r1 = c.fetch(base)
        out1 = r1.dom.query_one("#out")
        assert out1 is not None and out1.text == "not_hooked", "sanity: no init script yet"
        r2 = c.fetch(base + "#x", scripts=["window.__hooked = true;"])
        out2 = r2.dom.query_one("#out")
        assert out2 is not None and out2.text == "hooked"
        assert r2.final_url == base + "#x"


def test_hash_navigation_continue_keeps_the_loaded_document(httpserver: HTTPServer) -> None:
    """``hash_navigation="continue"`` moves within the loaded page, as an in-page
    anchor does: no request, the page keeps its state and its response's metadata."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body><h1 id='h'>same-doc</h1></body></html>",
        content_type="text/html",
        headers={"X-Bw": "sd"},
    )
    base = httpserver.url_for("/")
    mutate = "document.getElementById('h').textContent = 'KEPT_STATE'"
    with onyxweb.Client(concurrency=1, hash_navigation="continue") as c:
        r1 = c.fetch(base, post_load_scripts=[mutate])
        r2 = c.fetch(base + "#abc")
        requests_before_reload = len(httpserver.log)
        r3 = c.fetch(base + "#def", hash_navigation="reload")
    assert requests_before_reload == 1
    assert (r2.status_code, r2.final_url) == (200, base + "#abc")
    assert "KEPT_STATE" in r2
    assert r2.metadata.protocol == r1.metadata.protocol
    assert r2.headers.get("x-bw") == "sd"
    assert len(httpserver.log) == 2
    assert "KEPT_STATE" not in r3
    assert r3.final_url == base + "#def"


# ----------------------------------------------------------------------------
# INCLUDE matrix — shadow_dom / iframes, and what stays absent without them
# ----------------------------------------------------------------------------

_OPEN_SHADOW = _b64(
    b"<html><body><div id='host'></div><script>"
    b"document.getElementById('host').attachShadow({mode:'open'})"
    b".innerHTML='<span>'+'OPEN'+'MARKER'+'</span>';"
    b"</script></body></html>"
)
_CLOSED_SHADOW = _b64(
    b"<html><body><div id='host'></div><script>"
    b"document.getElementById('host').attachShadow({mode:'closed'})"
    b".innerHTML='<span>'+'CLOSED'+'MARKER'+'</span>';"
    b"</script></body></html>"
)
_LIGHT_DOM = "<!DOCTYPE html><html><body><h1>LIGHT_HEADING</h1></body></html>"


def _serve_iframe_pages(httpserver: HTTPServer) -> str:
    httpserver.expect_request("/inner.html").respond_with_data(
        "<html><body><p>IFRAME_INNER</p></body></html>", content_type="text/html"
    )
    httpserver.expect_request("/outer.html").respond_with_data(
        "<html><body><h1>PARENT_HEADING</h1><iframe src='inner.html'></iframe></body></html>",
        content_type="text/html",
    )
    return httpserver.url_for("/outer.html")


def test_open_shadow_absent_by_default() -> None:
    with onyxweb.Client() as c:
        assert "OPENMARKER" not in c.fetch(_OPEN_SHADOW).html


def test_open_shadow_captured_when_enabled() -> None:
    with onyxweb.Client(include_shadow_dom=True) as c:
        r = c.fetch(_OPEN_SHADOW)
    assert "OPENMARKER" in r.html
    assert r.dom.query_one("span") is not None


def test_closed_shadow_absent_by_default() -> None:
    with onyxweb.Client() as c:
        assert "CLOSEDMARKER" not in c.fetch(_CLOSED_SHADOW).html


def test_closed_shadow_captured_when_enabled() -> None:
    """Closed roots need the forced-open half of the include patch, not just serializable."""
    with onyxweb.Client(include_shadow_dom=True) as c:
        assert "CLOSEDMARKER" in c.fetch(_CLOSED_SHADOW).html


def test_shadow_capture_preserves_light_dom_and_doctype() -> None:
    with onyxweb.Client(include_shadow_dom=True) as c:
        r = c.fetch(_b64(_LIGHT_DOM.encode()))
    assert r.dom.query_one("h1") is not None
    assert "LIGHT_HEADING" in r.html
    assert r.html.lower().lstrip().startswith("<!doctype")


def test_same_origin_iframe_absent_by_default(httpserver: HTTPServer) -> None:
    url = _serve_iframe_pages(httpserver)
    with onyxweb.Client() as c:
        r = c.fetch(url, wait_after_ms=500)
    assert "PARENT_HEADING" in r.html
    assert "IFRAME_INNER" not in r.html


def test_same_origin_iframe_included_when_enabled(httpserver: HTTPServer) -> None:
    url = _serve_iframe_pages(httpserver)
    with onyxweb.Client(include_iframes=True) as c:
        r = c.fetch(url, wait_after_ms=500)
    assert "PARENT_HEADING" in r.html
    assert "IFRAME_INNER" in r.html


def test_cross_origin_iframe_content_stays_absent(httpserver: HTTPServer) -> None:
    """An unreadable frame is skipped — capture doesn't fail, and its content never appears."""
    httpserver.expect_request("/x.html").respond_with_data(
        "<html><body><h1>PARENT_HEADING</h1>"
        "<iframe src='https://example.com/'></iframe></body></html>",
        content_type="text/html",
    )
    with onyxweb.Client(include_iframes=True) as c:
        r = c.fetch(httpserver.url_for("/x.html"), wait_after_ms=1500)
    assert "PARENT_HEADING" in r.html
    assert "Example Domain" not in r.html  # cross-origin frame content never crosses in


def test_both_includes_together(httpserver: HTTPServer) -> None:
    url = _serve_iframe_pages(httpserver)
    with onyxweb.Client(include_iframes=True, include_shadow_dom=True) as c:
        r = c.fetch(url, wait_after_ms=500)
    assert "IFRAME_INNER" in r.html


# ----------------------------------------------------------------------------
# IMAGES — screenshot() / fetch_all() image formats
# ----------------------------------------------------------------------------

IMAGE_MAGIC: dict[str, Callable[[bytes], bool]] = {
    "png": lambda b: b[:8] == PNG_MAGIC,
    "jpeg": lambda b: b[:3] == JPEG_MAGIC,
    "webp": is_webp,
}


@pytest.mark.parametrize("fmt", list(IMAGE_MAGIC))
def test_screenshot_format_magic_bytes(
    client: onyxweb.Client, fmt: Literal["png", "jpeg", "webp"]
) -> None:
    page = _b64(b"<html><body>x</body></html>")
    data = client.screenshot(page, format=fmt)
    assert IMAGE_MAGIC[fmt](data), f"screenshot/{fmt}: bad magic bytes {data[:12]!r}"


@pytest.mark.parametrize("fmt", list(IMAGE_MAGIC))
def test_fetch_all_format_magic_bytes(
    client: onyxweb.Client, fmt: Literal["png", "jpeg", "webp"]
) -> None:
    page = _b64(b"<html><body>x</body></html>")
    data = client.fetch_all(page, format=fmt).png
    assert IMAGE_MAGIC[fmt](data), f"fetch_all/{fmt}: bad magic bytes {data[:12]!r}"


def test_jpeg_quality_trades_size(client: onyxweb.Client) -> None:
    page = _b64(b"<html><body>x</body></html>")
    hq = client.screenshot(page, format="jpeg", quality=95)
    lq = client.screenshot(page, format="jpeg", quality=5)
    assert len(lq) < len(hq)


# ----------------------------------------------------------------------------
# BLOCK_NAVIGATION — arms after the initial load, stops navigation, not subresources
# ----------------------------------------------------------------------------


def test_block_navigation_does_not_block_the_initial_load(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/").respond_with_data(
        "<html><body>initial-loaded</body></html>", content_type="text/html"
    )
    with onyxweb.Client() as c:
        r = c.fetch(httpserver.url_for("/"), block_navigation=True)
    assert "initial-loaded" in r
    assert r.status_code == 200


def test_without_block_navigation_a_click_redirect_navigates(httpserver: HTTPServer) -> None:
    """Sanity baseline: without the knob, a click that sets ``location.href`` moves the page."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body><button id='go' onclick='window.location.href=\"/elsewhere\"'>x</button>"
        "</body></html>",
        content_type="text/html",
    )
    httpserver.expect_request("/elsewhere").respond_with_data(
        "<html><body>ELSEWHERE</body></html>", content_type="text/html"
    )
    with onyxweb.Client() as c:
        r = c.fetch(
            httpserver.url_for("/"),
            actions=[Click(type="click", selector="#go", wait_after_ms=500)],
        )
    assert "/elsewhere" in r.final_url


def test_block_navigation_stops_a_js_redirect_from_a_click(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/").respond_with_data(
        "<html><body><button id='go' onclick='window.location.href=\"/elsewhere\"'>x</button>"
        "<div id='marker'>ORIGINAL_PAGE</div></body></html>",
        content_type="text/html",
    )
    httpserver.expect_request("/elsewhere").respond_with_data(
        "<html><body>ELSEWHERE</body></html>", content_type="text/html"
    )
    with onyxweb.Client() as c:
        r = c.fetch(
            httpserver.url_for("/"),
            actions=[Click(type="click", selector="#go", wait_after_ms=500)],
            block_navigation=True,
        )
    assert "/elsewhere" not in r.final_url
    assert "ORIGINAL_PAGE" in r  # the captured HTML reflects the page we never left


def test_block_navigation_does_not_block_subresources(httpserver: HTTPServer) -> None:
    """Arms after load, so subresources fire from a post-load script beside a navigation
    that must be stopped — proving blocking was on without also blocking everything."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body>ready</body></html>", content_type="text/html"
    )
    httpserver.expect_request("/elsewhere").respond_with_data("<p>left</p>")
    for path, body in (
        ("/asset.png", b"\x89PNG"),
        ("/asset.js", b"/* js */"),
        ("/bg.png", b"\x89PNG"),
        ("/api/data", b'{"ok": true}'),
    ):
        httpserver.expect_request(path).respond_with_data(body)
    fire = (
        "const i = new Image(); i.src = '/asset.png'; document.body.appendChild(i);"
        "const s = document.createElement('script'); s.src = '/asset.js';"
        "document.body.appendChild(s);"
        "const d = document.createElement('div'); d.style.background = \"url('/bg.png')\";"
        "d.textContent = 'x'; document.body.appendChild(d);"
        "fetch('/api/data', {mode: 'no-cors'}).catch(() => {});"
        "setTimeout(() => { location.href = '/elsewhere'; }, 50);"
    )
    with onyxweb.Client() as c:
        r = c.fetch(
            httpserver.url_for("/"),
            block_navigation=True,
            post_load_scripts=[fire],
            wait_after_post_load_ms=400,
        )
    paths = {req.path for req, _ in httpserver.log}
    assert "/elsewhere" not in r.final_url, "blocking wasn't on, so this proves nothing"
    for required in ("/asset.png", "/asset.js", "/bg.png", "/api/data"):
        assert required in paths, f"subresource {required} blocked: got paths={paths}"


# ----------------------------------------------------------------------------
# DOMino composition — capture knobs composed into recon flows
# ----------------------------------------------------------------------------


def test_domino_alert_hook_finds_reflected_xss(httpserver: HTTPServer) -> None:
    """init-script alert hook + console capture finds a reflected ``<script>alert()</script>``."""
    payload = '<script>alert("XSS-A")</script>'
    httpserver.expect_request("/vuln").respond_with_data(
        f"<html><body><h1>q={payload}</h1></body></html>", content_type="text/html"
    )
    hook = (
        "(() => { window.alert = function(...args) {"
        "try { console.error('FINDING:alert:' + JSON.stringify(args)); }"
        "catch (e) { console.error('FINDING:alert:<unstringifiable>'); } }; })();"
    )
    url = httpserver.url_for("/vuln") + "?q=" + quote(payload)
    with onyxweb.Client() as c:
        r = c.fetch(url, scripts=[hook])
    findings = [m.text for m in r.console_messages if m.text.startswith("FINDING:alert:")]
    assert any("XSS-A" in f for f in findings)


def test_domino_eval_sniffer_plus_form_fill_and_submit(httpserver: HTTPServer) -> None:
    """init-script eval hook + post_load_scripts fill-and-submit finds an indirect eval."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body><form id='f' onsubmit='window.eval(this.q.value); return false'>"
        "<input name='q' /><button type='submit'>Go</button></form></body></html>",
        content_type="text/html",
    )
    sniffer = (
        "(() => { const orig = window.eval; window.eval = function(code) {"
        "console.error('FINDING:eval:' + code); return orig.call(this, code); }; })();"
    )
    fill_submit = (
        "document.querySelector('input[name=q]').value = \"console.error('PAYLOAD_RAN')\";"
        "document.querySelector('button[type=submit]').click();"
    )
    with onyxweb.Client() as c:
        r = c.fetch(httpserver.url_for("/"), scripts=[sniffer], post_load_scripts=[fill_submit])
    texts = [m.text for m in r.console_messages]
    assert any("FINDING:eval:" in t and "PAYLOAD_RAN" in t for t in texts)
    assert any(t == "PAYLOAD_RAN" for t in texts), "payload didn't actually run through eval"


def test_domino_click_loop_plus_dom_scan_with_blocked_navigation(httpserver: HTTPServer) -> None:
    """Click loop + DOM scan under ``block_navigation``: the middle link's redirect never lands."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body>"
        "<a id='a1' href=\"javascript:document.body.appendChild(Object.assign("
        "document.createElement('div'), {id:'inj1', textContent:'XSS_INJ_1'}))\">a1</a>"
        "<a id='a2' href=\"javascript:window.location.href='/elsewhere'\">a2</a>"
        "<a id='a3' href=\"javascript:document.body.appendChild(Object.assign("
        "document.createElement('div'), {id:'inj3', textContent:'XSS_INJ_3'}))\">a3</a>"
        "</body></html>",
        content_type="text/html",
    )
    httpserver.expect_request("/elsewhere").respond_with_data(
        "<html><body>SHOULD_NOT_REACH</body></html>", content_type="text/html"
    )
    click_loop = (
        "document.querySelectorAll('[href^=\"javascript:\"]').forEach(el => {"
        "try { el.click(); } catch (e) { console.error('click_failed:' + e.message); } });"
    )
    dom_search = (
        "document.querySelectorAll('[id^=\"inj\"]').forEach(el => "
        "console.error('FINDING:dom-injection:' + el.id + ':' + el.textContent));"
    )
    with onyxweb.Client() as c:
        r = c.fetch(
            httpserver.url_for("/"),
            post_load_scripts=[click_loop, dom_search],
            block_navigation=True,
        )
    findings = [m.text for m in r.console_messages if m.text.startswith("FINDING:dom-injection:")]
    assert any("inj1" in f and "XSS_INJ_1" in f for f in findings)
    assert any("inj3" in f and "XSS_INJ_3" in f for f in findings), (
        "middle click scrambled the page"
    )
    assert "/elsewhere" not in r.final_url
    assert httpserver.url_for("/elsewhere") not in {req.url for req, _ in httpserver.log}
