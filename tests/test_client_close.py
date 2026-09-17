"""Closing a Client is prompt and actually stops Chrome.

The CDP handler task ends only once the browser goes away. If close never tells
Chrome to exit, joining that task always waits out its full timeout, and Chrome
keeps running until the Client object is garbage-collected.
"""

from __future__ import annotations

import os
import signal
import threading
import time

import onyxweb
import pytest

pytestmark = pytest.mark.skipif(not os.path.isdir("/proc"), reason="reads /proc")

CLOSE_BUDGET_S = 1.0  # a healthy close takes ~10 ms; generous for a loaded CI box
CLOSE_TIMEOUT_S = 3.0  # mirrors CLOSE_TIMEOUT in src/client.rs


def _chrome_children() -> set[int]:
    """Live (non-zombie) Chrome processes started by this test process."""
    pids: set[int] = set()
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat") as f:
                stat = f.read()
        except OSError:
            continue
        name = stat[stat.index("(") + 1 : stat.rindex(")")]
        state, ppid = stat[stat.rindex(")") + 2 :].split()[:2]
        if int(ppid) == os.getpid() and "chrome" in name and state != "Z":
            pids.add(int(entry))
    return pids


def _gone(pids: set[int], within_s: float = CLOSE_BUDGET_S) -> bool:
    deadline = time.monotonic() + within_s
    while time.monotonic() < deadline:
        if not pids & _chrome_children():
            return True
        time.sleep(0.01)
    return False


def test_close_returns_promptly() -> None:
    client = onyxweb.Client(concurrency=1)
    started = time.perf_counter()
    client.close()
    assert time.perf_counter() - started < CLOSE_BUDGET_S


def test_close_stops_chrome_while_the_client_is_still_referenced() -> None:
    before = _chrome_children()
    client = onyxweb.Client(concurrency=1)
    launched = _chrome_children() - before
    assert launched, "expected this Client to start a Chrome process"
    client.close()
    assert _gone(launched), "Chrome still running after close()"
    assert client is not None  # held on purpose: close alone must stop Chrome


def test_context_manager_exit_is_prompt() -> None:
    started = time.perf_counter()
    with onyxweb.Client(concurrency=1):
        pass
    assert time.perf_counter() - started < CLOSE_BUDGET_S


async def test_aclose_returns_promptly_and_stops_chrome() -> None:
    before = _chrome_children()
    client = onyxweb.AsyncClient(concurrency=1)
    launched = _chrome_children() - before
    assert launched, "expected this AsyncClient to start a Chrome process"
    started = time.perf_counter()
    await client.aclose()
    assert time.perf_counter() - started < CLOSE_BUDGET_S
    assert _gone(launched), "Chrome still running after aclose()"


def test_close_is_bounded_when_chrome_stops_responding(
    capfd: pytest.CaptureFixture[str],
) -> None:
    """A wedged Chrome can't hold close hostage: the whole shutdown shares one budget."""
    before = _chrome_children()
    client = onyxweb.Client(concurrency=1)
    launched = _chrome_children() - before
    assert launched, "expected this Client to start a Chrome process"
    for pid in launched:
        os.kill(pid, signal.SIGSTOP)  # a frozen Chrome never answers shutdown commands
    # Close on a thread: an unbounded close then fails fast instead of hanging the run.
    closer = threading.Thread(target=client.close, daemon=True)
    try:
        closer.start()
        closer.join(CLOSE_TIMEOUT_S + 1.0)
        finished = not closer.is_alive()
    finally:
        for pid in launched:
            os.kill(pid, signal.SIGKILL)  # also unblocks a close that overran
        closer.join(5.0)
    assert finished, f"close() still running {CLOSE_TIMEOUT_S + 1.0} s after Chrome froze"
    # The warning proves the budget was actually hit, so the bound above isn't vacuous.
    assert "did not shut down" in capfd.readouterr().err
