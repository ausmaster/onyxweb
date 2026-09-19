"""C11 real sites — a live site behaves as the local contracts promise.

Opt-in via the ``benchmark`` / ``real_sites`` markers (excluded from the default
run, see pyproject's ``addopts = "-m 'not benchmark'"``). Four independent live
targets: cnn.com (Akamai's UA first-byte-match, guarding the C10 stealth preset),
a raw throughput gauntlet, a live Cloudflare Turnstile widget (C9's anti-bot
verdict + C1's ``include_shadow_dom``), and lit.dev's real web components (C8's
shadow-DOM buckets).

    uv run pytest -m real_sites -s tests/test_c11_real_sites.py
    uv run pytest -m benchmark -s tests/test_c11_real_sites.py   # + the gauntlet

Gauntlet env vars — ONYXWEB_GAUNTLET_URLS (default 100, sweep URLs),
ONYXWEB_GAUNTLET_BIG (500, max-throughput run), ONYXWEB_GAUNTLET_MAX_C (128,
sweep ceiling), ONYXWEB_GAUNTLET_THREADS (32, Python threads),
ONYXWEB_NAV_TIMEOUT_MS (10000, per-URL cap).
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
from onyxweb.presets.shell import stealth

pytestmark = [pytest.mark.benchmark, pytest.mark.real_sites]


# ---------------------------------------------------------------------------
# STEALTH — Akamai's UA first-byte-match, guarding the C10 preset
# ---------------------------------------------------------------------------


def test_stealth_basic_preset_fetches_cnn() -> None:
    """Without stealth, cnn.com returns ~250 B ``Unknown Error`` because Akamai
    first-byte-matches ``HeadlessChrome`` in the UA. With ``stealth.BASIC``,
    the real 5 MB homepage comes through."""
    with onyxweb.Client(**stealth.BASIC, navigation_timeout_ms=20_000) as c:
        html = c.fetch("https://cnn.com")
    assert len(html) > 1_000_000, (
        f"cnn.com with stealth.BASIC returned only {len(html)} bytes — "
        f"anti-bot tripwire reactivated? Body head: {str(html)[:400]!r}"
    )


# ---------------------------------------------------------------------------
# GAUNTLET — real-site throughput benchmarks
# ---------------------------------------------------------------------------

URL_FILE = Path(__file__).resolve().parent / "urls_bench_big.txt"
SWEEP_URL_COUNT = int(os.environ.get("ONYXWEB_GAUNTLET_URLS", "100"))
BIG_URL_COUNT = int(os.environ.get("ONYXWEB_GAUNTLET_BIG", "500"))
MAX_CONCURRENCY = int(os.environ.get("ONYXWEB_GAUNTLET_MAX_C", "128"))
PYTHON_THREADS = int(os.environ.get("ONYXWEB_GAUNTLET_THREADS", "32"))
NAV_TIMEOUT_MS = int(os.environ.get("ONYXWEB_NAV_TIMEOUT_MS", "10000"))


def _classify(r: onyxweb.RenderResult | onyxweb.FetchResult | Exception) -> str:
    """ok = real 2xx/3xx response; http4xx = got bytes but error status; fail = nav dead."""
    if isinstance(r, Exception):
        return "fail"
    if not r.status_code:
        return "fail"
    if r.status_code >= 400:
        return "http4xx"
    return "ok"


def _count_ok(
    results: list[onyxweb.RenderResult | onyxweb.FetchResult | bytes | Exception],
    capture: str,
) -> int:
    if capture == "png":
        return sum(1 for b in results if isinstance(b, bytes) and b)
    return sum(1 for r in results if not isinstance(r, bytes) and _classify(r) == "ok")


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
                url
                for url, r in zip(uniq, results, strict=True)
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
        f"Python-thread drive — {PYTHON_THREADS} threads × Client(concurrency={best_concurrency})"
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
    _banner(f"MAX THROUGHPUT — {len(urls)} URLs, concurrency={best_concurrency}, capture={capture}")
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
    assert buckets["fail"] < len(urls) // 2, f"too many failures: {buckets}"


# ---------------------------------------------------------------------------
# TURNSTILE — live Cloudflare widget, integration cover for the C9 mocks
# ---------------------------------------------------------------------------
#
# tests/test_c9_anti_bot.py pins detection/self-heal against local fixtures;
# these prove the same verdicts against a live Cloudflare widget with a real
# sitekey, where the DOM shape and token format are Cloudflare's, not ours.
# Target: pagpeter's public per-mode test pages (real sitekeys, neutral pages).

_TURNSTILE_BASE = "https://peet.ws/turnstile-test/"
_TURNSTILE_SETTLE_MS = 15_000
_TOKEN_SEL = 'input[name="cf-turnstile-response"]'


def _fetch_turnstile(path: str, **kw: object) -> onyxweb.RenderResult:
    try:
        with onyxweb.Client(engine="full", navigation_timeout_ms=40_000) as c:
            return c.fetch(_TURNSTILE_BASE + path, wait_after_ms=_TURNSTILE_SETTLE_MS, **kw)  # type: ignore[arg-type]
    except (onyxweb.OnyxwebError, TimeoutError) as e:
        pytest.skip(f"{_TURNSTILE_BASE}{path} unreachable: {e}")


def _turnstile_token(r: onyxweb.RenderResult) -> str:
    el = r.dom.query_one(_TOKEN_SEL)
    return (el.attr("value") or "") if el else ""


def test_passive_pass_reports_resolved_without_shadow_patch() -> None:
    """Non-interactive mode issues a token with no interaction → resolved.

    Deliberately passes no ``scripts=`` — the token field is light-DOM, so
    ``anti_bot`` must be accurate on a plain ``fetch()``.
    """
    r = _fetch_turnstile("non-interactive.html")
    token = _turnstile_token(r)
    if not token:
        pytest.skip("Cloudflare declined to issue a token from this IP/session")
    assert token != "XXXX.DUMMY.TOKEN.XXXX", "expected a real token, not the test-key dummy"
    assert r.anti_bot is not None
    assert r.anti_bot.vendor == "cloudflare"
    assert r.anti_bot.resolved is True


def test_interactive_gate_reports_unresolved() -> None:
    """Managed mode withholds the token until interaction → not resolved."""
    r = _fetch_turnstile("managed.html")
    if _turnstile_token(r):
        pytest.skip("managed widget auto-passed; nothing to assert about the gate")
    assert r.anti_bot is not None
    assert r.anti_bot.resolved is False


def test_include_shadow_dom_recovers_real_widget_markup() -> None:
    """Turnstile's widget lives in a closed shadow root — captured only when enabled."""
    plain = _fetch_turnstile("managed.html")
    assert "challenges.cloudflare.com/cdn-cgi" not in plain.html

    try:
        with onyxweb.Client(
            engine="full", navigation_timeout_ms=40_000, include_shadow_dom=True
        ) as c:
            deep = c.fetch(_TURNSTILE_BASE + "managed.html", wait_after_ms=_TURNSTILE_SETTLE_MS)
    except (onyxweb.OnyxwebError, TimeoutError) as e:
        pytest.skip(f"unreachable: {e}")
    assert "challenges.cloudflare.com/cdn-cgi" in deep.html
    assert deep.dom.query_one("iframe") is not None


# ---------------------------------------------------------------------------
# SHADOW DOM — real web components, integration cover for C8's shadow buckets
# ---------------------------------------------------------------------------
#
# The mocked C8/C1 tests pin the ``include_shadow_dom`` mechanism; this proves
# the gap and the fix are real on a production site. lit.dev renders ~76 shadow
# hosts, and its cookie banner text is genuinely absent from a default capture.

_SHADOW_URL = "https://lit.dev/"
_SHADOW_SETTLE_MS = 8_000

_COUNT_HOSTS = """
(() => { let n = 0;
  const walk = (r) => r.querySelectorAll('*').forEach(el => {
    if (el.shadowRoot) { n++; walk(el.shadowRoot); } });
  walk(document); return n; })()
"""


def _fetch_shadow(
    *, post_load_scripts: list[str] | None = None, **client_kw: object
) -> onyxweb.RenderResult:
    try:
        with onyxweb.Client(
            engine="full",
            navigation_timeout_ms=45_000,
            **client_kw,  # type: ignore[arg-type]
        ) as c:
            return c.fetch(
                _SHADOW_URL,
                wait_after_ms=_SHADOW_SETTLE_MS,
                post_load_scripts=post_load_scripts or [],
            )
    except (onyxweb.OnyxwebError, TimeoutError) as e:
        pytest.skip(f"{_SHADOW_URL} unreachable: {e}")


def test_real_site_uses_shadow_dom() -> None:
    """Sanity: the target really does render shadow roots, else the rest proves nothing."""
    r = _fetch_shadow(post_load_scripts=[_COUNT_HOSTS])
    hosts = r.post_load_results[0]
    assert isinstance(hosts, int) and hosts >= 5, f"expected shadow hosts, got {hosts}"


def test_shadow_content_recovered_on_real_site() -> None:
    """Custom-element internals are absent by default and present when enabled."""
    plain = _fetch_shadow()
    deep = _fetch_shadow(include_shadow_dom=True)

    # lit.dev's own components; their markup only exists inside shadow roots.
    assert "<litdev-cookie-banner" in plain.html, "sanity: host element is light-DOM"
    assert len(deep.html) > len(plain.html), "shadow-inclusive capture should be strictly larger"
    # The banner's rendered text lives inside the component's shadow root.
    assert "Cookies consent notice" not in plain.html
    assert "Cookies consent notice" in deep.html
