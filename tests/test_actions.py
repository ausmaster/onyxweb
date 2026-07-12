"""Post-load actions: ``FetchConfig.actions`` runs CDP-trusted operations
on the loaded page after the lifecycle event but before HTML capture.

The trusted-events guarantee — ``event.isTrusted === true`` — is the whole
reason these exist. JS-side ``element.click()`` produces synthetic events
(isTrusted=false) which some handlers (e.g., navigation triggered by
``<a href="javascript:...">``) will reject. CDP ``Input.dispatchMouseEvent``
gives the same trust level as a real user click.

TDD-built across the post-load-actions phase:
1. Click — trusted mouse click via Input.dispatchMouseEvent.
2. Fill — value+input/change events (synthetic event.isTrusted is OK; the
   value lands in the input's ``.value`` so form submits carry it).
3. Hover + Wait.
4. ``on_error`` policy.
5. AsyncClient parity.
6. Pool integrity — actions don't leak state.
"""

from __future__ import annotations

import time

import onyxweb
import pytest
from onyxweb import Click, Fill, Hover, Wait
from pytest_httpserver import HTTPServer

# ----------------------------------------------------------------------------
# TDD #1: Click — CDP-trusted mouse click
# ----------------------------------------------------------------------------


def test_click_action_triggers_onclick_with_trusted_event(httpserver: HTTPServer) -> None:
    """A Click action dispatches a CDP-trusted mouse event; the page's
    onclick handler sees ``event.isTrusted === true``."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body>"
        "<button id='btn' onclick='console.error(\"clicked isTrusted=\" + event.isTrusted)'>"
        "Click me"
        "</button>"
        "</body></html>",
        content_type="text/html",
    )

    with onyxweb.Client() as c:
        r = c.fetch(
            httpserver.url_for("/"),
            actions=[Click(type="click", selector="#btn")],
            wait_after_ms=200,
        )

    msgs = [m.text for m in r.console_messages]
    assert any("clicked isTrusted=true" in t for t in msgs), (
        f"expected trusted click; got console: {msgs}"
    )


def test_click_action_runs_after_page_load(httpserver: HTTPServer) -> None:
    """Action runs AFTER the page's lifecycle event — so the button must
    already exist in the DOM when the action fires."""
    # The button is added to DOM via inline script that runs at parse time —
    # by the time ``load`` fires, it's been there for a while.
    httpserver.expect_request("/").respond_with_data(
        "<html><body>"
        "<div id='target'></div>"
        "<script>"
        "  const b = document.createElement('button');"
        "  b.id = 'late_btn';"
        "  b.onclick = () => console.error('LATE_CLICKED');"
        "  document.body.appendChild(b);"
        "</script>"
        "</body></html>",
        content_type="text/html",
    )

    with onyxweb.Client() as c:
        r = c.fetch(
            httpserver.url_for("/"),
            actions=[Click(type="click", selector="#late_btn")],
            wait_after_ms=200,
        )

    assert any("LATE_CLICKED" in m.text for m in r.console_messages)


def test_click_action_html_capture_reflects_post_action_state(
    httpserver: HTTPServer,
) -> None:
    """Captured HTML must reflect DOM changes the click triggered. Confirms
    actions run BEFORE HTML capture, not after."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body>"
        "<button id='b' onclick='document.body.setAttribute(\"data-clicked\", \"yes\")'>"
        "x</button>"
        "</body></html>",
        content_type="text/html",
    )

    with onyxweb.Client() as c:
        r = c.fetch(
            httpserver.url_for("/"),
            actions=[Click(type="click", selector="#b")],
            wait_after_ms=200,
        )

    assert 'data-clicked="yes"' in r, (
        f"action ran but HTML capture missed the post-action state: {r[:200]}"
    )


# ----------------------------------------------------------------------------
# TDD #2: Fill — set input value, fire input/change events
# ----------------------------------------------------------------------------


def test_fill_action_sets_input_value(httpserver: HTTPServer) -> None:
    """Fill action populates the input's ``.value``."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body>"
        "<input id='i' />"
        "<button id='b' "
        "  onclick='console.error(\"value=\" + document.getElementById(\"i\").value)'>"
        "  x"
        "</button>"
        "</body></html>",
        content_type="text/html",
    )

    with onyxweb.Client() as c:
        r = c.fetch(
            httpserver.url_for("/"),
            actions=[
                Fill(type="fill", selector="#i", value="hello"),
                Click(type="click", selector="#b"),
            ],
            wait_after_ms=200,
        )

    assert any("value=hello" in m.text for m in r.console_messages), (
        f"expected value=hello; got: {[m.text for m in r.console_messages]}"
    )


def test_fill_action_fires_input_event(httpserver: HTTPServer) -> None:
    """Fill triggers an ``input`` event handler (framework reactivity)."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body>"
        "<input id='i' />"
        "<script>"
        "  document.getElementById('i').addEventListener('input', e => "
        "    console.error('input_event=' + e.target.value));"
        "</script>"
        "</body></html>",
        content_type="text/html",
    )

    with onyxweb.Client() as c:
        r = c.fetch(
            httpserver.url_for("/"),
            actions=[Fill(type="fill", selector="#i", value="hi")],
            wait_after_ms=200,
        )

    assert any("input_event=hi" in m.text for m in r.console_messages)


def test_fill_action_fires_change_event(httpserver: HTTPServer) -> None:
    """Fill triggers a ``change`` event handler."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body>"
        "<input id='i' />"
        "<script>"
        "  document.getElementById('i').addEventListener('change', e => "
        "    console.error('change_event=' + e.target.value));"
        "</script>"
        "</body></html>",
        content_type="text/html",
    )

    with onyxweb.Client() as c:
        r = c.fetch(
            httpserver.url_for("/"),
            actions=[Fill(type="fill", selector="#i", value="changed")],
            wait_after_ms=200,
        )

    assert any("change_event=changed" in m.text for m in r.console_messages)


def test_fill_replaces_existing_input_value(httpserver: HTTPServer) -> None:
    """Fill on an input that already has a value REPLACES, doesn't append."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body>"
        "<input id='i' value='preexisting' />"
        "<button id='b' "
        "  onclick='console.error(\"final=\" + document.getElementById(\"i\").value)'>"
        "  x"
        "</button>"
        "</body></html>",
        content_type="text/html",
    )

    with onyxweb.Client() as c:
        r = c.fetch(
            httpserver.url_for("/"),
            actions=[
                Fill(type="fill", selector="#i", value="REPLACEMENT"),
                Click(type="click", selector="#b"),
            ],
            wait_after_ms=200,
        )

    assert any("final=REPLACEMENT" in m.text for m in r.console_messages), (
        f"expected REPLACEMENT, not concat with preexisting; got: "
        f"{[m.text for m in r.console_messages]}"
    )


# ----------------------------------------------------------------------------
# TDD #3: Hover + Wait
# ----------------------------------------------------------------------------


def test_hover_action_triggers_mouseover_handler(httpserver: HTTPServer) -> None:
    """Hover action moves the mouse to the element center; mouseover fires."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body>"
        "<div id='target' style='width:100px;height:100px;background:red'"
        "  onmouseover='console.error(\"hovered isTrusted=\" + event.isTrusted)'>"
        "  hover me"
        "</div>"
        "</body></html>",
        content_type="text/html",
    )

    with onyxweb.Client() as c:
        r = c.fetch(
            httpserver.url_for("/"),
            actions=[Hover(type="hover", selector="#target")],
            wait_after_ms=200,
        )

    assert any("hovered isTrusted=true" in m.text for m in r.console_messages), (
        f"expected hover to fire trusted mouseover; got: "
        f"{[m.text for m in r.console_messages]}"
    )


def test_wait_action_sleeps_for_duration(httpserver: HTTPServer) -> None:
    """Wait action delays the action loop by the requested duration."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body>blank</body></html>",
        content_type="text/html",
    )

    with onyxweb.Client() as c:
        # Baseline: no actions, just measure the fetch overhead.
        t0 = time.perf_counter()
        c.fetch(httpserver.url_for("/"))
        baseline_s = time.perf_counter() - t0

        # With Wait(500): elapsed must be at least baseline + ~0.4s.
        t0 = time.perf_counter()
        c.fetch(httpserver.url_for("/"), actions=[Wait(type="wait", duration_ms=500)])
        with_wait_s = time.perf_counter() - t0

    delta = with_wait_s - baseline_s
    assert delta >= 0.4, (
        f"Wait(500) added only {delta:.3f}s vs baseline {baseline_s:.3f}s "
        f"(total {with_wait_s:.3f}s); expected ≥0.4s"
    )


def test_actions_run_in_declared_order(httpserver: HTTPServer) -> None:
    """A sequence Fill → Click runs in order: Fill must complete before Click."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body>"
        "<input id='i' />"
        "<button id='b' "
        "  onclick='console.error(\"submitted=\" + document.getElementById(\"i\").value)'>"
        "  x"
        "</button>"
        "</body></html>",
        content_type="text/html",
    )

    with onyxweb.Client() as c:
        r = c.fetch(
            httpserver.url_for("/"),
            actions=[
                Fill(type="fill", selector="#i", value="ORDERED"),
                Click(type="click", selector="#b"),
            ],
            wait_after_ms=200,
        )

    assert any("submitted=ORDERED" in m.text for m in r.console_messages)


# ----------------------------------------------------------------------------
# TDD #4: on_error policy
# ----------------------------------------------------------------------------


def test_action_on_error_continue_records_error_and_runs_next(
    httpserver: HTTPServer,
) -> None:
    """Default on_error='continue': bad action records an error but doesn't
    abort the fetch; subsequent actions still run."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body>"
        "<button id='real' onclick='console.error(\"REAL_FIRED\")'>x</button>"
        "</body></html>",
        content_type="text/html",
    )

    with onyxweb.Client() as c:
        r = c.fetch(
            httpserver.url_for("/"),
            actions=[
                Click(type="click", selector="#nonexistent"),
                Click(type="click", selector="#real"),
            ],
            wait_after_ms=200,
        )

    # Subsequent action ran.
    assert any("REAL_FIRED" in m.text for m in r.console_messages), (
        f"continue policy didn't run the next action; console: "
        f"{[m.text for m in r.console_messages]}"
    )
    # The error is surfaced via r.errors / console_messages.
    error_texts = " ".join(r.errors)
    assert "nonexistent" in error_texts.lower() or "action" in error_texts.lower(), (
        f"continue policy didn't surface the error in r.errors: {r.errors}"
    )


def test_action_on_error_abort_raises_runtime_error(httpserver: HTTPServer) -> None:
    """on_error='abort': bad action raises RuntimeError, fetch fails."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body>nothing</body></html>",
        content_type="text/html",
    )

    with onyxweb.Client() as c, pytest.raises(RuntimeError):
        c.fetch(
            httpserver.url_for("/"),
            actions=[
                Click(type="click", selector="#nonexistent", on_error="abort"),
            ],
            wait_after_ms=100,
        )


def test_action_on_error_ignore_silently_skips(httpserver: HTTPServer) -> None:
    """on_error='ignore': bad action is silently skipped — no error recorded,
    no abort. Subsequent actions still run."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body>"
        "<button id='real' onclick='console.error(\"AFTER_IGNORE\")'>x</button>"
        "</body></html>",
        content_type="text/html",
    )

    with onyxweb.Client() as c:
        r = c.fetch(
            httpserver.url_for("/"),
            actions=[
                Click(type="click", selector="#missing", on_error="ignore"),
                Click(type="click", selector="#real"),
            ],
            wait_after_ms=200,
        )

    # Next action ran.
    assert any("AFTER_IGNORE" in m.text for m in r.console_messages)
    # No error recorded for the ignored action.
    error_texts = " ".join(r.errors)
    assert "missing" not in error_texts.lower(), (
        f"ignore policy leaked the error: {r.errors}"
    )


# ----------------------------------------------------------------------------
# TDD #5: AsyncClient parity — same Click/Fill/Hover/Wait via async path.
# ----------------------------------------------------------------------------


async def test_async_click_action_with_trusted_events(httpserver: HTTPServer) -> None:
    """Trusted-events guarantee holds on the async path."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body>"
        "<button id='b' onclick='console.error(\"async_click_isTrusted=\" + event.isTrusted)'>"
        "x</button>"
        "</body></html>",
        content_type="text/html",
    )

    async with onyxweb.AsyncClient() as ac:
        r = await ac.fetch(
            httpserver.url_for("/"),
            actions=[Click(type="click", selector="#b")],
            wait_after_ms=200,
        )

    assert any("async_click_isTrusted=true" in m.text for m in r.console_messages)


async def test_async_fill_action_sets_value(httpserver: HTTPServer) -> None:
    """Fill works via AsyncClient."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body>"
        "<input id='i' />"
        "<button id='b' "
        "  onclick='console.error(\"async_value=\" + document.getElementById(\"i\").value)'>"
        "  x"
        "</button>"
        "</body></html>",
        content_type="text/html",
    )

    async with onyxweb.AsyncClient() as ac:
        r = await ac.fetch(
            httpserver.url_for("/"),
            actions=[
                Fill(type="fill", selector="#i", value="async_hello"),
                Click(type="click", selector="#b"),
            ],
            wait_after_ms=200,
        )

    assert any("async_value=async_hello" in m.text for m in r.console_messages)


async def test_async_action_on_error_continue(httpserver: HTTPServer) -> None:
    """on_error='continue' default carries through to async path."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body>"
        "<button id='real' onclick='console.error(\"ASYNC_AFTER\")'>x</button>"
        "</body></html>",
        content_type="text/html",
    )

    async with onyxweb.AsyncClient() as ac:
        r = await ac.fetch(
            httpserver.url_for("/"),
            actions=[
                Click(type="click", selector="#missing"),
                Click(type="click", selector="#real"),
            ],
            wait_after_ms=200,
        )

    assert any("ASYNC_AFTER" in m.text for m in r.console_messages)


# ----------------------------------------------------------------------------
# TDD #6: pool integrity — actions don't leak state to next fetch on same tab.
# Actions are transient (DOM gets re-parsed on next goto), but the action-
# error console messages and any focus/scroll state could in principle leak.
# Verify both are clean.
# ----------------------------------------------------------------------------


def test_actions_dont_leak_state_to_next_fetch(httpserver: HTTPServer) -> None:
    """concurrency=1: fetch #1 with actions; fetch #2 clean. Verify fetch #2
    sees no residue — no action errors, no DOM mutations, no console msgs."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body><div id='probe'>baseline</div></body></html>",
        content_type="text/html",
    )

    with onyxweb.Client(concurrency=1) as c:
        # Fetch #1 — action with a missing selector (continue policy records
        # to console_messages); a successful Click that mutates the DOM.
        r1 = c.fetch(
            httpserver.url_for("/"),
            actions=[
                Click(type="click", selector="#nonexistent_LEAK_42"),
                Fill(type="fill", selector="#probe", value="should_be_gone"),
            ],
            wait_after_ms=200,
        )
        # Fetch #2 — same URL, no actions. Must be clean.
        r2 = c.fetch(httpserver.url_for("/"), wait_after_ms=200)

    # Sanity: fetch #1 captured the action error.
    fetch1_texts = " ".join(m.text for m in r1.console_messages) + " ".join(r1.errors)
    assert "nonexistent_LEAK_42" in fetch1_texts, (
        f"sanity: fetch #1 should have recorded the bad-selector error: {fetch1_texts}"
    )

    # Fetch #2 must NOT have residue from fetch #1.
    fetch2_texts = " ".join(m.text for m in r2.console_messages) + " ".join(r2.errors)
    assert "nonexistent_LEAK_42" not in fetch2_texts, (
        f"action error leaked from fetch #1 to fetch #2: {fetch2_texts}"
    )
    # DOM mutation isn't preserved — fetch #2 does a fresh goto, baseline returns.
    assert "baseline" in r2 and "should_be_gone" not in r2
