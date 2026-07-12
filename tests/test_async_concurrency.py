"""Async concurrency: ``asyncio.gather`` over N fetches runs them in real
parallel; the page-pool semaphore caps in-flight pages at ``concurrency``.

Each fetch uses ``wait_after_ms`` so timing is deterministic regardless of
chromium cache behavior — every fetch costs at least the wait."""

from __future__ import annotations

import asyncio
import time

import onyxweb

URL = "https://example.com"
WAIT_MS = 300


async def _timed_fetch(ac: onyxweb.AsyncClient) -> float:
    """Fetch URL once with the standard wait, return elapsed seconds."""
    t0 = time.time()
    await ac.fetch(URL, wait_after_ms=WAIT_MS)
    return time.time() - t0


async def test_gather_runs_in_parallel() -> None:
    """``asyncio.gather`` of N fetches with concurrency=N is much faster
    than N sequential fetches.

    With concurrency=4 and 4 parallel fetches, total ≈ 1× wait_after_ms
    (the waits overlap). Sequential would be ≈ 4×.
    """
    async with onyxweb.AsyncClient(concurrency=4) as ac:
        baseline = await _timed_fetch(ac)
        t0 = time.time()
        await asyncio.gather(*[ac.fetch(URL, wait_after_ms=WAIT_MS) for _ in range(4)])
        gather_total = time.time() - t0

    # 4 parallel ~ 1× wait + tab nav overhead. Definitely under 2× baseline.
    assert gather_total < baseline * 2.0, (
        f"4 parallel fetches took {gather_total:.2f}s, expected < {baseline * 2:.2f}s "
        f"(baseline single fetch: {baseline:.2f}s)"
    )


async def test_batch_async_parallelism_matches_concurrency() -> None:
    """``batch`` (which internally tokio::spawns) honors the concurrency cap."""
    async with onyxweb.AsyncClient(concurrency=4) as ac:
        baseline = await _timed_fetch(ac)
        cfg = onyxweb.FetchConfig(wait_after_ms=WAIT_MS)
        t0 = time.time()
        results = await ac.batch([URL] * 4, capture="html", config=cfg)
        batch_total = time.time() - t0
        assert len(results) == 4
        for r in results:
            assert isinstance(r, onyxweb.RenderResult)
            assert "Example Domain" in r

    assert batch_total < baseline * 2.0, (
        f"batch of 4 took {batch_total:.2f}s, expected < {baseline * 2:.2f}s "
        f"(baseline: {baseline:.2f}s)"
    )


async def test_gather_does_not_starve_other_coroutines() -> None:
    """A long fetch shouldn't block unrelated coroutines — they share the
    event loop. Smoke-test that ``asyncio.sleep(0.05)`` runs concurrently
    with a fetch."""
    async with onyxweb.AsyncClient() as ac:

        async def ticker() -> int:
            count = 0
            for _ in range(10):
                await asyncio.sleep(0.05)
                count += 1
            return count

        ticks, fetch_result = await asyncio.gather(
            ticker(), ac.fetch(URL, wait_after_ms=WAIT_MS)
        )
    assert ticks == 10
    assert "Example Domain" in fetch_result
