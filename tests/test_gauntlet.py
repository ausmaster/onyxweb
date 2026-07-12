"""Performance gauntlet — real-site throughput benchmarks.

All tests marked ``benchmark`` + ``real_sites``. Skipped by default (pyproject
sets ``addopts = "-m 'not benchmark'"``). Run with:

    uv run pytest -m benchmark -s                           # full gauntlet
    uv run pytest tests/test_gauntlet.py -m benchmark -s    # just this file

``-s`` (no output capture) is important — these tests print structured timing
tables you'll want to see. Tune via env vars:

    ONYXWEB_GAUNTLET_URLS (default 100)     — URLs per sweep iteration
    ONYXWEB_GAUNTLET_BIG (default 500)      — URLs for the max-throughput run
    ONYXWEB_GAUNTLET_MAX_C (default 128)    — ceiling concurrency in the sweep
    ONYXWEB_GAUNTLET_THREADS (default 32)   — Python threads for the drive test
    ONYXWEB_NAV_TIMEOUT_MS (default 10000)  — per-URL nav timeout (keeps flaky
                                               tail URLs from dominating)
"""

from __future__ import annotations

import os
import random
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

import onyxweb
import pytest

pytestmark = [pytest.mark.benchmark, pytest.mark.real_sites]

URL_FILE = Path(__file__).resolve().parent / "urls_bench_big.txt"
SWEEP_URL_COUNT = int(os.environ.get("ONYXWEB_GAUNTLET_URLS", "100"))
BIG_URL_COUNT = int(os.environ.get("ONYXWEB_GAUNTLET_BIG", "500"))
MAX_CONCURRENCY = int(os.environ.get("ONYXWEB_GAUNTLET_MAX_C", "128"))
PYTHON_THREADS = int(os.environ.get("ONYXWEB_GAUNTLET_THREADS", "32"))
NAV_TIMEOUT_MS = int(os.environ.get("ONYXWEB_NAV_TIMEOUT_MS", "10000"))


def _classify(r: onyxweb.RenderResult | onyxweb.FetchResult) -> str:
    """ok = real 2xx/3xx response; http4xx = got bytes but error status; fail = nav dead."""
    if not r.status_code:
        return "fail"
    if r.status_code >= 400:
        return "http4xx"
    return "ok"


def _count_ok(
    results: list[onyxweb.RenderResult | onyxweb.FetchResult | bytes],
    capture: str,
) -> int:
    if capture == "png":
        return sum(1 for b in results if isinstance(b, bytes) and b)
    return sum(
        1 for r in results
        if not isinstance(r, bytes) and _classify(r) == "ok"
    )


def _banner(msg: str) -> None:
    print(f"\n─── {msg} ───", flush=True)


def _expand(clean: list[str], n: int) -> list[str]:
    out: list[str] = []
    while len(out) < n:
        out.extend(clean)
    random.Random(42).shuffle(out)
    return out[:n]


@pytest.fixture(scope="module")
def raw_urls() -> list[str]:
    if not URL_FILE.is_file():
        pytest.skip(f"URL seed file missing: {URL_FILE}")
    base = [
        ln.strip()
        for ln in URL_FILE.read_text().splitlines()
        if ln.strip() and not ln.startswith("#")
    ]
    pool: list[str] = []
    while len(pool) < SWEEP_URL_COUNT:
        pool.extend(base)
    random.Random(42).shuffle(pool)
    return pool[:SWEEP_URL_COUNT]


@pytest.fixture(scope="module")
def clean_urls(raw_urls: list[str]) -> list[str]:
    """Prewarm + filter: probe every unique URL twice, keep only those that
    succeeded both passes. Also warms DNS + Chrome cache so later phases
    measure actual throughput and not nav-timeout wall time from flaky URLs.
    """
    uniq = sorted(set(raw_urls))
    _banner(f"prewarm + filter — probing {len(uniq)} unique URLs (2 passes)")
    survivors = set(uniq)
    with onyxweb.Client(concurrency=16, navigation_timeout_ms=NAV_TIMEOUT_MS) as client:
        for pass_n in (1, 2):
            t = time.perf_counter()
            results = client.batch(uniq, capture="html")
            elapsed = time.perf_counter() - t
            bad = {
                url for url, r in zip(uniq, results, strict=True)
                if not isinstance(r, bytes) and _classify(r) != "ok"
            }
            survivors -= bad
            print(
                f"  pass {pass_n}: {len(uniq) - len(bad)}/{len(uniq)} ok in {elapsed:.1f}s "
                f"(dropped {len(bad)} this pass)"
            )
    final = [u for u in uniq if u in survivors]
    if not final:
        pytest.skip("no URLs survived prewarm — network issue?")
    print(f"  survivors: {len(final)} URLs")
    return final


@pytest.fixture(scope="module")
def sweep_result(clean_urls: list[str]) -> tuple[int, float]:
    """Sweep concurrency levels, return (best_concurrency, best_rate)."""
    urls = _expand(clean_urls, SWEEP_URL_COUNT)
    levels = [c for c in (4, 8, 16, 32, 48, 64, 96, 128) if c <= MAX_CONCURRENCY]
    _banner(f"concurrency sweep — {len(urls)} URLs, nav_timeout={NAV_TIMEOUT_MS}ms")
    print(f"  {'concurrency':>11}  {'URL/s':>7}  {'ok':>7}  {'elapsed':>7}")
    best_c, best_rate = 0, 0.0
    for c in levels:
        with onyxweb.Client(concurrency=c, navigation_timeout_ms=NAV_TIMEOUT_MS) as client:
            t0 = time.perf_counter()
            results = client.batch(urls, capture="html")
            elapsed = time.perf_counter() - t0
        rate = len(urls) / elapsed
        ok = _count_ok(results, "html")
        marker = " ★" if rate > best_rate else ""
        print(f"  {c:>11d}  {rate:>6.2f}   {ok:>3d}/{len(urls):<3d}  {elapsed:>6.2f}s{marker}")
        if rate > best_rate:
            best_c, best_rate = c, rate
    return best_c, best_rate


@pytest.fixture(scope="module")
def best_concurrency(sweep_result: tuple[int, float]) -> int:
    return sweep_result[0]


def test_sweep_picks_reasonable_concurrency(sweep_result: tuple[int, float]) -> None:
    best_c, best_rate = sweep_result
    assert best_c >= 4, f"sweep picked an unreasonably low concurrency: {best_c}"
    assert best_rate > 1.0, f"peak throughput unreasonably low: {best_rate:.2f} URL/s"


def test_capture_modes(clean_urls: list[str], best_concurrency: int) -> None:
    urls = _expand(clean_urls, SWEEP_URL_COUNT)
    _banner(f"capture-mode comparison at concurrency={best_concurrency}")
    print(f"  {'mode':>6}  {'URL/s':>7}  {'ok':>7}  {'elapsed':>7}")
    for mode in ("html", "png", "both"):
        with onyxweb.Client(
            concurrency=best_concurrency, navigation_timeout_ms=NAV_TIMEOUT_MS
        ) as client:
            t0 = time.perf_counter()
            results = client.batch(urls, capture=mode)
            elapsed = time.perf_counter() - t0
        rate = len(urls) / elapsed
        ok = _count_ok(results, mode)
        print(f"  {mode:>6}  {rate:>6.2f}   {ok:>3d}/{len(urls):<3d}  {elapsed:>6.2f}s")


def test_python_threads_drive(clean_urls: list[str], best_concurrency: int) -> None:
    """Prove the GIL-release model: N Python threads drive ONE Client in parallel."""
    urls = _expand(clean_urls, SWEEP_URL_COUNT)
    _banner(
        f"Python-thread drive — {PYTHON_THREADS} threads × "
        f"Client(concurrency={best_concurrency})"
    )
    latencies: list[float] = []
    errors = 0
    with onyxweb.Client(
        concurrency=best_concurrency, navigation_timeout_ms=NAV_TIMEOUT_MS
    ) as client:
        def work(url: str) -> float:
            t = time.perf_counter()
            try:
                r = client.fetch(url)
                if _classify(r) != "ok":
                    return -1.0
            except Exception:
                return -1.0
            return time.perf_counter() - t

        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=PYTHON_THREADS) as pool:
            for lat in pool.map(work, urls):
                if lat >= 0:
                    latencies.append(lat)
                else:
                    errors += 1
        elapsed = time.perf_counter() - t0
    rate = len(urls) / elapsed
    print(f"  {rate:.2f} URL/s   {len(urls) - errors}/{len(urls)} ok   {elapsed:.2f}s")
    if latencies:
        s = sorted(latencies)

        def pctile(q: float) -> float:
            return s[min(int(len(s) * q), len(s) - 1)]

        print(
            f"  per-URL latency: p50={statistics.median(s):.2f}s  "
            f"p95={pctile(0.95):.2f}s  p99={pctile(0.99):.2f}s"
        )
    assert errors < len(urls) // 2, f"too many errors: {errors}/{len(urls)}"


@pytest.mark.parametrize("capture", ["html", "both"])
def test_max_throughput(
    clean_urls: list[str],
    best_concurrency: int,
    capture: Literal["html", "both"],
) -> None:
    """Headline number — a big run at the winning concurrency."""
    urls = _expand(clean_urls, BIG_URL_COUNT)
    _banner(
        f"MAX THROUGHPUT — {len(urls)} URLs, concurrency={best_concurrency}, capture={capture}"
    )
    with onyxweb.Client(
        concurrency=best_concurrency, navigation_timeout_ms=NAV_TIMEOUT_MS
    ) as client:
        t0 = time.perf_counter()
        results = client.batch(urls, capture=capture)
        elapsed = time.perf_counter() - t0
    rate = len(urls) / elapsed
    buckets = {"ok": 0, "fail": 0, "http4xx": 0}
    for r in results:
        if not isinstance(r, bytes):
            buckets[_classify(r)] += 1
    print(
        f"  ★ {rate:.2f} URL/s   "
        f"{buckets['ok']} ok / {buckets['http4xx']} 4xx / {buckets['fail']} fail   "
        f"wall {elapsed:.1f}s"
    )
    if capture == "both":
        fetches = [r for r in results if isinstance(r, onyxweb.FetchResult)]
        html_mb = sum(len(r.html) for r in fetches) / 1e6
        png_mb = sum(len(r.png) for r in fetches) / 1e6
        print(f"  payload: {html_mb:.1f} MB html + {png_mb:.1f} MB png")
    assert rate > 0
