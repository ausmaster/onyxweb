"""Per-fetch post-load scripts: ``FetchConfig.post_load_scripts`` runs
arbitrary JavaScript via ``page.evaluate(src)`` AFTER the lifecycle
event and any ``wait_after_ms`` settle, BEFORE actions and HTML
capture.

This is the primary primitive for DOMino-style "do JS work on the
loaded page" flows. Unlike ``scripts`` (which fires at new-document
time, before the page's own scripts) and ``actions`` (which is a
pre-batched list of CDP-trusted operations), ``post_load_scripts``
runs once on the fully-loaded page with full DOM access and arbitrary
JS expressiveness — one CDP roundtrip per script.

DOMino uses this to express ClickJSElements, FillAndSubmit,
PostMessage, and WindowName — all of which are "build a JS payload
and run it on the loaded page".
"""

from __future__ import annotations

import base64

import onyxweb
import pytest

# ----------------------------------------------------------------------------
# TDD #1: post_load_scripts runs after lifecycle, has DOM access
# ----------------------------------------------------------------------------


def test_post_load_script_runs_and_observes_console_output() -> None:
    """A post_load_script's ``console.error`` shows up in console_messages —
    proves the script ran."""
    html = b"<html><body>blank</body></html>"
    url = "data:text/html;base64," + base64.b64encode(html).decode()

    with onyxweb.Client() as c:
        r = c.fetch(url, post_load_scripts=["console.error('PLS_RAN')"])

    assert any("PLS_RAN" in m.text for m in r.console_messages), (
        f"post_load_script didn't run; console: {[m.text for m in r.console_messages]}"
    )


def test_post_load_script_has_dom_access() -> None:
    """post_load_scripts run AFTER the page's scripts — they can see the
    fully-built DOM. Page has an inline script that creates an element;
    post_load_script reads it via querySelector."""
    html = (
        b"<html><body><script>"
        b"  const div = document.createElement('div');"
        b"  div.id = 'late_div';"
        b"  div.textContent = 'PAGE_BUILT_THIS';"
        b"  document.body.appendChild(div);"
        b"</script></body></html>"
    )
    url = "data:text/html;base64," + base64.b64encode(html).decode()

    with onyxweb.Client() as c:
        r = c.fetch(
            url,
            post_load_scripts=[
                "console.error('reads_dom=' + document.getElementById('late_div').textContent)"
            ],
        )

    msgs = [m.text for m in r.console_messages]
    assert any("reads_dom=PAGE_BUILT_THIS" in t for t in msgs), (
        f"post_load_script can't see page-built DOM; console: {msgs}"
    )


def test_multiple_post_load_scripts_run_in_order() -> None:
    """Each entry in the list runs, in declared order."""
    html = b"<html><body>blank</body></html>"
    url = "data:text/html;base64," + base64.b64encode(html).decode()

    with onyxweb.Client() as c:
        r = c.fetch(
            url,
            post_load_scripts=[
                "console.error('PLS_FIRST')",
                "console.error('PLS_SECOND')",
                "console.error('PLS_THIRD')",
            ],
        )

    error_msgs = [m.text for m in r.console_messages if m.text.startswith("PLS_")]
    assert error_msgs == ["PLS_FIRST", "PLS_SECOND", "PLS_THIRD"]


def test_post_load_script_dom_mutation_reflected_in_capture() -> None:
    """post_load_scripts run BEFORE HTML capture, so DOM mutations they
    make are reflected in the captured HTML."""
    html = b"<html><body><div id='target'>ORIGINAL</div></body></html>"
    url = "data:text/html;base64," + base64.b64encode(html).decode()

    with onyxweb.Client() as c:
        r = c.fetch(
            url,
            post_load_scripts=[
                "document.getElementById('target').textContent = 'MUTATED_BY_PLS'"
            ],
        )

    assert "MUTATED_BY_PLS" in r, f"PLS mutation missing from capture: {r[:300]}"
    assert "ORIGINAL" not in r


# ----------------------------------------------------------------------------
# TDD #2: DOMino ClickJSElements equivalent — synthetic click loop covers
# the trusted-events case. This is the proof that post_load_scripts replaces
# Phase 3's per-action CDP dispatch for DOMino's needs.
# ----------------------------------------------------------------------------


def test_synthetic_click_loop_executes_javascript_urls_and_onclick() -> None:
    """The canonical DOMino flow: page has multiple [onclick] and
    [href^="javascript:"] elements. A single post_load_script does
    querySelectorAll + loop + el.click(). Each onclick fires; each
    javascript: URL executes. One CDP roundtrip total."""
    html = (
        b"<html><body>"
        b"<button id='b1' onclick=\"console.error('CLICKED_b1')\">b1</button>"
        b"<button id='b2' onclick=\"console.error('CLICKED_b2')\">b2</button>"
        b"<a id='a1' href=\"javascript:console.error('JS_URL_a1')\">a1</a>"
        b"<a id='a2' href=\"javascript:console.error('JS_URL_a2')\">a2</a>"
        b"</body></html>"
    )
    url = "data:text/html;base64," + base64.b64encode(html).decode()

    click_loop = """
    (() => {
        const els = [
            ...document.querySelectorAll('[onclick]'),
            ...document.querySelectorAll('[href^="javascript:"]'),
        ];
        for (const el of els) {
            try { el.click(); }
            catch (e) { console.error('click_failed: ' + e.message); }
        }
    })();
    """

    with onyxweb.Client() as c:
        r = c.fetch(url, post_load_scripts=[click_loop], block_navigation=True)

    texts = [m.text for m in r.console_messages]
    # Onclick handlers fired
    assert any("CLICKED_b1" in t for t in texts), f"b1 onclick missed: {texts}"
    assert any("CLICKED_b2" in t for t in texts), f"b2 onclick missed: {texts}"
    # javascript: URLs executed (the load-bearing finding for DOMino)
    assert any("JS_URL_a1" in t for t in texts), f"a1 javascript: URL missed: {texts}"
    assert any("JS_URL_a2" in t for t in texts), f"a2 javascript: URL missed: {texts}"


def test_post_load_script_form_fill_and_submit_via_synthetic_click() -> None:
    """The canonical DOMino FillAndSubmit flow expressed as a single
    post_load_script: fill all form inputs, click submit. Synthetic
    click on a submit button submits the form (form submit is NOT gated
    on isTrusted)."""
    html = (
        b"<html><body>"
        b"<form id='f' onsubmit=\"console.error('SUBMITTED_value=' + this.q.value); "
        b"  return false\">"
        b"  <input name='q' />"
        b"  <button type='submit'>Go</button>"
        b"</form>"
        b"</body></html>"
    )
    url = "data:text/html;base64," + base64.b64encode(html).decode()

    fill_submit = """
    (() => {
        const inputs = document.querySelectorAll('form input');
        for (const inp of inputs) {
            if (['submit','button','reset','image','checkbox','radio'].includes(inp.type)) continue;
            inp.value = 'XSS_PAYLOAD_42';
            inp.dispatchEvent(new Event('input', {bubbles: true}));
            inp.dispatchEvent(new Event('change', {bubbles: true}));
        }
        const submit = document.querySelector(
            'form button[type="submit"], form input[type="submit"]'
        );
        if (submit) submit.click();
    })();
    """

    with onyxweb.Client() as c:
        r = c.fetch(url, post_load_scripts=[fill_submit])

    assert any("SUBMITTED_value=XSS_PAYLOAD_42" in m.text for m in r.console_messages), (
        f"form submit didn't carry the filled value: {[m.text for m in r.console_messages]}"
    )


# ----------------------------------------------------------------------------
# TDD #3: AsyncClient parity — same primitive on the async path.
# ----------------------------------------------------------------------------


async def test_async_post_load_script_runs_and_mutates_dom() -> None:
    """``await ac.fetch(post_load_scripts=...)`` runs identically to sync."""
    html = b"<html><body><div id='t'>orig</div></body></html>"
    url = "data:text/html;base64," + base64.b64encode(html).decode()

    async with onyxweb.AsyncClient() as ac:
        r = await ac.fetch(
            url,
            post_load_scripts=[
                "console.error('async_PLS=' + document.getElementById('t').textContent);"
                "document.getElementById('t').textContent = 'ASYNC_MUTATED'"
            ],
        )

    assert any("async_PLS=orig" in m.text for m in r.console_messages)
    assert "ASYNC_MUTATED" in r


async def test_async_synthetic_click_loop_via_post_load_script() -> None:
    """The DOMino-equivalent click loop also works on the async path."""
    html = (
        b"<html><body>"
        b"<a id='a' href=\"javascript:console.error('ASYNC_JS_URL_FIRED')\">x</a>"
        b"</body></html>"
    )
    url = "data:text/html;base64," + base64.b64encode(html).decode()

    async with onyxweb.AsyncClient() as ac:
        r = await ac.fetch(
            url,
            post_load_scripts=[
                "document.querySelectorAll('[href^=\"javascript:\"]').forEach(el => el.click())"
            ],
            block_navigation=True,
        )

    assert any("ASYNC_JS_URL_FIRED" in m.text for m in r.console_messages)


# ----------------------------------------------------------------------------
# Behavioral guarantees worth nailing down (added during Phase 4.5
# adversarial review — these gaps weren't covered by cycles 1-3 and would
# have silently bitten DOMino's port).
# ----------------------------------------------------------------------------


def test_post_load_script_async_iife_is_awaited() -> None:
    """``page.evaluate`` awaits a Promise returned by an async IIFE. This is
    load-bearing for DOMino's wait-between-clicks pattern: the JS-ported
    equivalent uses ``await new Promise(r => setTimeout(r, 500))`` between
    clicks. If the await didn't honor the promise, capture would race ahead
    and miss async XSS findings."""
    html = b"<html><body><div id='target'>before</div></body></html>"
    url = "data:text/html;base64," + base64.b64encode(html).decode()

    async_pls = """
    (async () => {
        await new Promise(r => setTimeout(r, 500));
        document.getElementById('target').setAttribute('data-mark', 'AWAITED');
    })();
    """

    with onyxweb.Client() as c:
        r = c.fetch(url, post_load_scripts=[async_pls])

    assert 'data-mark="AWAITED"' in r, (
        "page.evaluate did NOT await the async IIFE — the post-load timeout "
        "fired and capture happened before the script's async work completed. "
        f"html: {r[:300]}"
    )


def test_post_load_script_uncaught_exception_aborts_fetch() -> None:
    """An uncaught synchronous exception in a post_load_script propagates as
    RuntimeError, aborting the fetch. Documents the current contract — JS
    errors are NOT swallowed; users must try/catch their own JS to get
    continue-on-error semantics."""
    html = b"<html><body>page</body></html>"
    url = "data:text/html;base64," + base64.b64encode(html).decode()

    with onyxweb.Client() as c, pytest.raises(RuntimeError, match="PLS_THREW"):
        c.fetch(url, post_load_scripts=["throw new Error('PLS_THREW')"])


def test_post_load_script_async_exception_also_aborts_fetch() -> None:
    """Uncaught Promise rejections from an async IIFE also abort the fetch.
    Same contract as sync exceptions."""
    html = b"<html><body>page</body></html>"
    url = "data:text/html;base64," + base64.b64encode(html).decode()

    with onyxweb.Client() as c, pytest.raises(RuntimeError, match="ASYNC_THREW"):
        c.fetch(
            url,
            post_load_scripts=["(async () => { throw new Error('ASYNC_THREW') })()"],
        )


def test_post_load_script_caught_exception_does_not_abort() -> None:
    """If user wraps in try/catch, the fetch proceeds normally — this is the
    pattern users adopt for continue-on-error semantics."""
    html = b"<html><body>page</body></html>"
    url = "data:text/html;base64," + base64.b64encode(html).decode()

    safe_pls = """
    try {
        document.querySelectorAll('[onclick]').forEach(el => {
            try { el.click(); }
            catch (e) { console.error('per_click_error: ' + e.message); }
        });
    } catch (e) {
        console.error('outer_error: ' + e.message);
    }
    """

    with onyxweb.Client() as c:
        # Page has no [onclick] elements — loop is a no-op, no error.
        r = c.fetch(url, post_load_scripts=[safe_pls])

    assert isinstance(r, onyxweb.RenderResult)
    assert "page" in r
