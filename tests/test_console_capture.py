"""Console capture: structured ``ConsoleMessage`` records on
``RenderResult.console_messages``, with a configurable
``capture_console_level`` filter.

TDD-built across four slices:
1. Plumbing (this file's first tests) — ``ConsoleMessage`` exists,
   ``console_messages`` property defaults to ``[]``.
2. Structured records — console errors flow into the list, ``.errors``
   becomes a derived view.
3. Level filter — ``capture_console_level=...`` controls what's captured.
4. Async parity — same behavior via ``AsyncClient``.
"""

from __future__ import annotations

import base64
import dataclasses
import time

import onyxweb
import pytest
from onyxweb import ConsoleMessage
from pydantic import ValidationError

# ----------------------------------------------------------------------------
# TDD #1: plumbing — type exists, default is []
# ----------------------------------------------------------------------------


def test_console_message_type_exists_with_expected_fields() -> None:
    """``ConsoleMessage`` is a constructible type with type/text/timestamp."""
    msg = ConsoleMessage(type="log", text="hello", timestamp=1.5)
    assert msg.type == "log"
    assert msg.text == "hello"
    assert msg.timestamp == 1.5


def test_console_message_is_frozen_dataclass() -> None:
    """``ConsoleMessage`` is immutable — assignment raises after construction."""
    msg = ConsoleMessage(type="log", text="x", timestamp=0.0)
    assert dataclasses.is_dataclass(msg)
    with pytest.raises(dataclasses.FrozenInstanceError):
        msg.text = "modified"  # type: ignore[misc]


def test_console_message_value_equality() -> None:
    """Two ConsoleMessages with identical fields compare equal."""
    a = ConsoleMessage(type="warning", text="x", timestamp=2.0)
    b = ConsoleMessage(type="warning", text="x", timestamp=2.0)
    assert a == b


def test_render_result_has_console_messages_attr() -> None:
    """``RenderResult.console_messages`` exists and is a list."""
    r = onyxweb.fetch("data:text/html,<html><body>no console</body></html>")
    assert hasattr(r, "console_messages")
    assert isinstance(r.console_messages, list)


def test_render_result_console_messages_defaults_empty() -> None:
    """A page that prints nothing yields an empty ``console_messages``."""
    r = onyxweb.fetch("data:text/html,<html><body>no console</body></html>")
    assert r.console_messages == []


async def test_async_render_result_console_messages_defaults_empty() -> None:
    """Same plumbing applies to ``AsyncClient.fetch``."""
    async with onyxweb.AsyncClient() as ac:
        r = await ac.fetch("data:text/html,<html><body>no console</body></html>")
    assert r.console_messages == []


# ----------------------------------------------------------------------------
# TDD #2: structured records — console.error → ConsoleMessage(type="error", ...)
# ----------------------------------------------------------------------------


def test_console_error_captured_as_structured_record() -> None:
    """A page-side console.error() produces a ConsoleMessage(type='error', ...)
    in console_messages."""
    html = b"<html><script>console.error('test error msg')</script></html>"
    url = "data:text/html;base64," + base64.b64encode(html).decode()
    r = onyxweb.fetch(url)

    error_msgs = [m for m in r.console_messages if m.type == "error"]
    assert len(error_msgs) == 1, f"expected 1 error, got {r.console_messages}"
    assert "test error msg" in error_msgs[0].text


def test_multiple_console_errors_captured_in_order() -> None:
    """Multiple console.error calls show up in dispatch order."""
    html = b"<html><script>console.error('first');console.error('second')</script></html>"
    url = "data:text/html;base64," + base64.b64encode(html).decode()
    r = onyxweb.fetch(url)

    error_msgs = [m for m in r.console_messages if m.type == "error"]
    assert len(error_msgs) == 2
    assert "first" in error_msgs[0].text
    assert "second" in error_msgs[1].text


def test_errors_backward_compat_returns_text_strings() -> None:
    """RenderResult.errors continues to expose a list[str] of error texts —
    derived from console_messages where type=='error'."""
    html = b"<html><script>console.error('bw compat')</script></html>"
    url = "data:text/html;base64," + base64.b64encode(html).decode()
    r = onyxweb.fetch(url)

    assert isinstance(r.errors, list)
    assert all(isinstance(e, str) for e in r.errors)
    assert any("bw compat" in e for e in r.errors)


def test_console_message_timestamp_is_within_fetch_window() -> None:
    """Timestamp falls between the wall-clock window of the fetch call."""
    html = b"<html><script>console.error('time test')</script></html>"
    url = "data:text/html;base64," + base64.b64encode(html).decode()
    before = time.time()
    r = onyxweb.fetch(url)
    after = time.time()

    msgs = [m for m in r.console_messages if "time test" in m.text]
    assert len(msgs) == 1
    assert before <= msgs[0].timestamp <= after, (
        f"timestamp {msgs[0].timestamp} outside [{before}, {after}]"
    )


def test_console_log_not_captured_at_default_level() -> None:
    """At default capture level (errors only), console.log is dropped."""
    html = (
        b"<html><script>"
        b"console.log('should be dropped');"
        b"console.error('should be kept')"
        b"</script></html>"
    )
    url = "data:text/html;base64," + base64.b64encode(html).decode()
    r = onyxweb.fetch(url)

    log_msgs = [m for m in r.console_messages if m.type == "log"]
    error_msgs = [m for m in r.console_messages if m.type == "error"]

    assert log_msgs == []
    assert len(error_msgs) == 1
    assert "should be kept" in error_msgs[0].text


# ----------------------------------------------------------------------------
# TDD #3: capture_console_level — config-driven level filter
# ----------------------------------------------------------------------------


def test_capture_console_level_all_captures_log_info_warn_error_debug() -> None:
    """capture_console_level='all' captures every standard console method."""
    html = (
        b"<html><script>"
        b"console.log('msg-log');"
        b"console.info('msg-info');"
        b"console.warn('msg-warning');"
        b"console.error('msg-error');"
        b"console.debug('msg-debug')"
        b"</script></html>"
    )
    url = "data:text/html;base64," + base64.b64encode(html).decode()

    with onyxweb.Client(capture_console_level="all") as c:
        r = c.fetch(url)

    types_seen = {m.type for m in r.console_messages}
    assert "log" in types_seen
    assert "info" in types_seen
    assert "warning" in types_seen
    assert "error" in types_seen
    assert "debug" in types_seen


def test_capture_console_level_warn_drops_log_info_keeps_warning_error() -> None:
    """capture_console_level='warn' drops log/info, keeps warning/error."""
    html = (
        b"<html><script>"
        b"console.log('drop-log');"
        b"console.info('drop-info');"
        b"console.warn('keep-warning');"
        b"console.error('keep-error')"
        b"</script></html>"
    )
    url = "data:text/html;base64," + base64.b64encode(html).decode()

    with onyxweb.Client(capture_console_level="warn") as c:
        r = c.fetch(url)

    types_seen = {m.type for m in r.console_messages}
    assert "log" not in types_seen
    assert "info" not in types_seen
    assert "warning" in types_seen
    assert "error" in types_seen


def test_capture_console_level_invalid_raises_validation_error() -> None:
    """Invalid level value rejected by pydantic at construction time."""
    with pytest.raises(ValidationError):
        onyxweb.ClientConfig(capture_console_level="invalid")  # type: ignore[arg-type]


# ----------------------------------------------------------------------------
# TDD #4: AsyncClient parity — same shape, same filter behavior
# ----------------------------------------------------------------------------


async def test_async_console_error_captured_as_structured_record() -> None:
    """``await ac.fetch(...)`` populates console_messages identically to sync."""
    html = b"<html><script>console.error('async-test-err')</script></html>"
    url = "data:text/html;base64," + base64.b64encode(html).decode()

    async with onyxweb.AsyncClient() as ac:
        r = await ac.fetch(url)

    error_msgs = [m for m in r.console_messages if m.type == "error"]
    assert len(error_msgs) == 1
    assert "async-test-err" in error_msgs[0].text


async def test_async_errors_backward_compat() -> None:
    """``RenderResult.errors`` derivation works the same on the async path."""
    html = b"<html><script>console.error('async bw compat')</script></html>"
    url = "data:text/html;base64," + base64.b64encode(html).decode()

    async with onyxweb.AsyncClient() as ac:
        r = await ac.fetch(url)

    assert any("async bw compat" in e for e in r.errors)


async def test_async_capture_console_level_all() -> None:
    """``capture_console_level='all'`` flows through AsyncClient."""
    html = (
        b"<html><script>"
        b"console.log('a-log');"
        b"console.warn('a-warning');"
        b"console.error('a-error')"
        b"</script></html>"
    )
    url = "data:text/html;base64," + base64.b64encode(html).decode()

    async with onyxweb.AsyncClient(capture_console_level="all") as ac:
        r = await ac.fetch(url)

    types_seen = {m.type for m in r.console_messages}
    assert "log" in types_seen
    assert "warning" in types_seen
    assert "error" in types_seen


async def test_async_default_drops_log() -> None:
    """Default level ('error') on async path drops console.log just like sync."""
    html = (
        b"<html><script>"
        b"console.log('drop-me');"
        b"console.error('keep-me')"
        b"</script></html>"
    )
    url = "data:text/html;base64," + base64.b64encode(html).decode()

    async with onyxweb.AsyncClient() as ac:
        r = await ac.fetch(url)

    assert [m for m in r.console_messages if m.type == "log"] == []
    assert any("keep-me" in m.text for m in r.console_messages if m.type == "error")
