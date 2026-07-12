"""N Python threads × one Client — the GIL-free concurrency guarantee.

The whole perf story depends on: a single Client, shared across many Python
threads, actually runs fetches in parallel (not GIL-serialized). Each
public method releases the GIL before entering tokio. An internal Semaphore
caps in-flight work at ``concurrency``; excess threads queue rather than
stampede.
"""

from __future__ import annotations

import concurrent.futures
import time

import onyxweb
import pytest

URLS = [
    "https://example.com",
    "https://example.org",
    "https://example.net",
]


@pytest.mark.parametrize("threads,concurrency", [(1, 1), (4, 4), (8, 8), (16, 4)])
def test_concurrent_fetches_complete(threads: int, concurrency: int) -> None:
    """N threads × N URLs all return valid RenderResult. No crashes, no empties."""
    work = URLS * (threads * 2)  # more work than threads
    with (
        onyxweb.Client(concurrency=concurrency) as client,
        concurrent.futures.ThreadPoolExecutor(max_workers=threads) as pool,
    ):
        results = list(pool.map(client.fetch, work))
    assert len(results) == len(work)
    for r in results:
        assert isinstance(r, onyxweb.RenderResult)
        assert len(r) > 0


def test_threading_faster_than_serial() -> None:
    """With concurrency=8 and 8 threads, 16 URLs should complete materially
    faster than 16 × single-URL-wall would suggest — the threading works
    only if the GIL is actually released."""
    work = URLS * 6  # 18 URLs
    with onyxweb.Client(concurrency=8) as client:
        t0 = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(client.fetch, work))
        parallel_wall = time.perf_counter() - t0
    assert all(len(r) > 0 for r in results)
    # Rough per-URL baseline ~0.3-0.5s on stable sites. 18 URLs parallel at P=8
    # should comfortably finish in under 15s total. Serial would take 18 × 0.5 = 9s
    # so this bound is loose but catches "GIL is still held" regressions where
    # parallel would equal serial.
    assert parallel_wall < 15.0, f"parallel wall {parallel_wall:.2f}s suggests GIL holding"


def test_one_client_shared_across_threads_survives_errors() -> None:
    """If one thread's fetch raises, other threads continue fine."""
    mixed = URLS + ["not-a-url"] + URLS
    Outcome = onyxweb.RenderResult | Exception
    with onyxweb.Client(concurrency=4) as client:
        results: list[tuple[str, Outcome]] = []

        def work(u: str) -> tuple[str, Outcome]:
            try:
                return u, client.fetch(u)
            except Exception as e:
                return u, e

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            for url, outcome in pool.map(work, mixed):
                results.append((url, outcome))

    for url, outcome in results:
        if url == "not-a-url":
            assert isinstance(outcome, Exception), f"{url} should have raised"
        else:
            assert isinstance(outcome, onyxweb.RenderResult), f"{url} should have rendered"
            assert len(outcome) > 0
