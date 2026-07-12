"""``FetchConfig.wait_after_post_load_ms``: optional settle delay AFTER
``post_load_scripts`` run and BEFORE actions / capture. Distinct from
``wait_after_ms`` which fires BETWEEN the lifecycle event and
``post_load_scripts``.

Use case: post_load_scripts that schedule asynchronous work (setTimeout,
fetch, deferred DOM mutations) need a settle window before the engine
captures HTML. Today consumers blanket-set ``wait_after_ms`` which adds
latency to every fetch even when post_load_scripts aren't used.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import onyxweb
import pytest

DataUrl = Callable[[bytes], str]


def test_wait_after_post_load_ms_lets_async_work_finish(data_url: DataUrl) -> None:
    """A post_load_script schedules a 300ms-deferred DOM mutation.

    The captured HTML must reflect it when wait_after_post_load_ms covers
    the scheduled deferral.
    """
    url = data_url(b"<html><body><div id='t'>initial</div></body></html>")
    schedule_async_mutation = (
        "setTimeout(() => "
        "{document.getElementById('t').textContent = 'POST_ASYNC_DONE';}, 300);"
    )

    with onyxweb.Client() as c:
        r = c.fetch(
            url,
            post_load_scripts=[schedule_async_mutation],
            wait_after_post_load_ms=500,  # > 300ms scheduled deferral
        )
    assert "POST_ASYNC_DONE" in r, f"settle window missed; html: {r[:200]!r}"


def test_zero_default_no_wait(data_url: DataUrl) -> None:
    """Default 0ms means no added latency for existing flows."""
    url = data_url(b"<html><body>x</body></html>")

    with onyxweb.Client() as c:
        # Warm up so first-fetch chromium overhead doesn't pollute the timer.
        c.fetch(url)
        t0 = time.perf_counter()
        c.fetch(url, post_load_scripts=["1"])
        no_wait = time.perf_counter() - t0

    # No knob set → no extra wait. Bound generously to absorb chromium
    # variability; a regression that adds 100ms+ would still trip this.
    assert no_wait < 0.5, f"unexpectedly slow with default wait: {no_wait:.3f}s"


def test_wait_after_post_load_ms_runs_after_scripts(data_url: DataUrl) -> None:
    """The settle fires AFTER post_load_scripts return.

    Scripts themselves don't see the settle as latency.
    """
    url = data_url(b"<html><body><div id='t'>initial</div></body></html>")

    with onyxweb.Client() as c:
        c.fetch(url)  # warm up
        t0 = time.perf_counter()
        c.fetch(url, post_load_scripts=["1+1"], wait_after_post_load_ms=400)
        elapsed = time.perf_counter() - t0

    # Settle adds ~400ms; expect at least 350ms.
    assert elapsed >= 0.35, f"settle didn't apply: {elapsed:.3f}s"


def test_wait_after_post_load_ms_distinct_from_wait_after_ms(data_url: DataUrl) -> None:
    """``wait_after_ms`` fires BEFORE post_load_scripts; the new knob fires AFTER.

    A post_load_script that mutates the DOM synchronously is visible
    without ``wait_after_post_load_ms``; an async mutation needs it.
    """
    url = data_url(b"<html><body><div id='t'>before</div></body></html>")
    sync_mutate = "document.getElementById('t').textContent = 'SYNC_DONE';"
    with onyxweb.Client() as c:
        # No wait_after_post_load_ms — sync mutation is immediately visible.
        r = c.fetch(url, post_load_scripts=[sync_mutate])
    assert "SYNC_DONE" in r


@pytest.mark.asyncio
async def test_async_client_wait_after_post_load_ms(data_url: DataUrl) -> None:
    url = data_url(b"<html><body><div id='t'>initial</div></body></html>")
    async_mutate = (
        "setTimeout(() => "
        "{document.getElementById('t').textContent = 'ASYNC_VIA_AC';}, 200);"
    )

    async with onyxweb.AsyncClient() as ac:
        r = await ac.fetch(
            url,
            post_load_scripts=[async_mutate],
            wait_after_post_load_ms=400,
        )
    assert "ASYNC_VIA_AC" in r
