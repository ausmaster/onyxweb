"""CDP error context: timeout / evaluate-script errors carry diagnostic
context in their messages so consumers can identify which script index
threw, which lifecycle event was awaited, and which URL was navigating.

Today's bare ``RuntimeError("CDP: Request timed out.")`` carries zero
diagnostic context; debugging takes multiple probe rounds. The fix
extends ``OnyxError::NavigationTimeout`` and adds
``OnyxError::PostLoadScript`` to surface this metadata in the error
message that reaches Python.
"""

from __future__ import annotations

import socket
from collections.abc import Callable

import onyxweb
import pytest

DataUrl = Callable[[bytes], str]


def test_navigation_timeout_includes_lifecycle_context() -> None:
    """A navigation that never reaches the lifecycle event must surface
    the URL, awaited lifecycle stage, and timeout duration.

    Bound a port that accepts the connection but never responds, so the
    fetch's wait for ``Page.loadEventFired`` will time out.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}/hangs"

    try:
        # Timeouts raise the builtin TimeoutError (not a bare RuntimeError) so
        # callers can branch on them. It is NOT a RuntimeError.
        with onyxweb.Client() as c, pytest.raises(TimeoutError) as exc:
            c.fetch(url, timeout_ms=1500)
        assert not isinstance(exc.value, RuntimeError)
        msg = str(exc.value)
        assert url in msg, f"expected URL in error: {msg!r}"
        assert "load" in msg.lower(), f"expected lifecycle name in error: {msg!r}"
        assert "1500" in msg, f"expected timeout ms in error: {msg!r}"
    finally:
        sock.close()


def test_non_timeout_error_is_onyxweb_error() -> None:
    """Non-timeout failures raise ``onyxweb.OnyxwebError``, which subclasses
    ``RuntimeError`` so existing ``except RuntimeError`` code keeps working."""
    with onyxweb.Client() as c, pytest.raises(onyxweb.OnyxwebError) as exc:
        c.fetch("https://this-domain-does-not-exist-onyxweb-xyz.invalid")
    assert isinstance(exc.value, RuntimeError)


def test_post_load_script_error_includes_script_index(data_url: DataUrl) -> None:
    """A post_load_script that throws must surface its index in the error."""
    url = data_url(b"<html><body>x</body></html>")
    with onyxweb.Client() as c, pytest.raises(RuntimeError) as exc:
        c.fetch(
            url,
            post_load_scripts=[
                "1 + 1",
                "throw new Error('SECOND_THREW')",
                "2 + 2",
            ],
        )
    msg = str(exc.value)
    assert "post_load_scripts[1]" in msg, f"expected script index in error: {msg!r}"
    assert "SECOND_THREW" in msg, f"expected JS error text in error: {msg!r}"


def test_post_load_script_first_index_zero(data_url: DataUrl) -> None:
    """Make sure 0 is used (not 1) for the first script index."""
    url = data_url(b"<html><body>x</body></html>")
    with onyxweb.Client() as c, pytest.raises(RuntimeError) as exc:
        c.fetch(url, post_load_scripts=["throw new Error('FIRST')"])
    msg = str(exc.value)
    assert "post_load_scripts[0]" in msg, f"expected 0-indexed script: {msg!r}"
    assert "FIRST" in msg
