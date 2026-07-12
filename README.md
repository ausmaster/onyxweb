# onyxweb

**URL → fully-rendered HTML (and optionally a screenshot), for Python.**

onyxweb is a Rust + PyO3 Python package that wraps Chromium via the Chrome
DevTools Protocol. It gives you Playwright-class output (post-JavaScript DOM,
PNG screenshots, HTTP header injection, locale/timezone/geo emulation) at
roughly half the per-URL overhead — because there's no Python-to-Node
driver hop in the call chain.

```python
import onyxweb

# fully rendered HTML, post-JS
html = onyxweb.fetch("https://example.com")

# screenshot — PNG by default; JPEG/WebP with quality
png = onyxweb.screenshot("https://example.com")
jpg = onyxweb.screenshot("https://example.com", format="jpeg", quality=80)

# both, from a single page visit (HTML is free once you've loaded)
both = onyxweb.fetch_all("https://example.com")

# Rust-side CSS query — no Python HTML parsing tax
print(html.dom.title())              # "Example Domain"
print(html.dom.find("h1").text)      # "Example Domain"
print(html.dom.links())              # ["https://iana.org/domains/example"]
```

Also available as a CLI:

```bash
python -m onyxweb https://example.com                   # HTML → stdout
python -m onyxweb https://example.com -o page.html      # → file
python -m onyxweb https://example.com -s shot.jpg       # screenshot + HTML
python -m onyxweb https://example.com --screenshot-only shot.webp  # image only
python -m onyxweb https://example.com --json            # JSON w/ metadata
```

## Install

Two steps — mirrors Playwright's pattern (small wheel + one-time browser fetch).

```bash
# CLI
uv tool install onyxweb
onyxweb --install             # fetch chrome-headless-shell (~180 MB, one-time)
onyxweb https://example.com

# Library
uv add onyxweb
uv run onyxweb --install
```

`pipx install onyxweb` and `pip install onyxweb` work the same way.

Chromium resolution order: `chrome_path=` / `ONYXWEB_CHROME__PATH`
→ `--install`-fetched binary → system `chromium` / `chrome` on PATH. If
you already have a system chromium, skip `--install`.

Platforms without a PyPI wheel trigger a source build ([rustup](https://rustup.rs)
needed); `--install` works the same after.

## Why onyxweb

If you need fully-rendered HTML from a URL (i.e. after JavaScript has run),
your existing options are:

- **`requests` / `httpx`** — fast, but they don't run JS. Modern SPAs return
  an empty `<div id="root">` and nothing useful.
- **BeautifulSoup / lxml** — parse HTML you already have. Doesn't fetch.
- **Playwright-python** — capable but Python → Node driver chain adds latency
  per CDP call; ~2.7s/URL on our bench vs onyxweb's ~1.9s/URL at equal
  concurrency.
- **Selenium** — older, slower, browser-driver abstraction.

onyxweb's sweet spot: **URL → fully-rendered HTML + optional PNG, fast,
Python-native, one pip install.** Tuned for high-throughput read-mostly
pipelines (BBOT-style subdomain fan-outs, security recon, change detection)
where you want hundreds of URLs per minute from a single process.

### Benchmarks (48-URL stable gauntlet, 16-core Linux, ``chrome-headless-shell 148``)

| Engine                                            | Config        | URL/s   |
|---------------------------------------------------|---------------|---------|
| onyxweb (this package)                           | P=16 mode=both| **8.54**|
| Playwright-python                                 | P=16          | 5.82    |
| Chromium headless (CLI fork-per-URL)              | P=16          | 4.51    |
| Servo 0.1.0 in-process                            | P=8           | 1.13    |

Full methodology + breakdown in ``BENCHMARKS.md``.

## The core API

### Module-level convenience

One-shot calls use a shared, lazy-initialized `Client`. Good for scripts and
notebooks. For high-throughput work, instantiate your own `Client` so you
can tune `concurrency` and re-use the warm chromium.

```python
onyxweb.fetch(url)                        # → RenderResult (str subclass + metadata)
onyxweb.screenshot(url)                   # → image bytes (PNG by default)
onyxweb.screenshot(url, format="jpeg", quality=80)  # JPEG
onyxweb.screenshot(url, format="webp", quality=80)  # WebP
onyxweb.fetch_all(url)                    # → FetchResult (html + image)

# Async peers — same return shapes, awaitable
await onyxweb.afetch(url)
await onyxweb.ascreenshot(url)
await onyxweb.afetch_all(url)
```

### Persistent `Client` with config

Three equivalent ways to configure:

```python
# 1. Flat kwargs — most common
with onyxweb.Client(
    viewport=(1920, 1080),
    user_agent="MyScraper/1.0",
    concurrency=16,
    locale="en-GB",
    timezone="Europe/London",
    block_urls=["*doubleclick*", "*.googletagmanager.com/*"],
) as client:
    ...

# 2. Structured pydantic config
from onyxweb import ClientConfig, NetworkConfig, EmulationConfig
cfg = ClientConfig(
    concurrency=32,
    network=NetworkConfig(user_agent="X", extra_headers={"X-Run": "abc"}),
    emulation=EmulationConfig(locale="ja-JP"),
)
client = onyxweb.Client(config=cfg)

# 3. Env vars (auto-loaded by pydantic-settings)
#   ONYXWEB_CONCURRENCY=32 ONYXWEB_VIEWPORT__WIDTH=1920 python script.py
client = onyxweb.Client()
```

### `AsyncClient` — async peer

Every `Client` method has an `AsyncClient` counterpart with the same
construction signature, same config, same page pool. Methods return
coroutines instead of values:

```python
import asyncio
import onyxweb

async with onyxweb.AsyncClient(concurrency=16) as ac:
    r = await ac.fetch(url)
    png = await ac.screenshot(url)
    results = await ac.batch(urls, capture="html")  # awaitable list

    # Real parallelism on one event loop — pool semaphore caps in-flight pages
    htmls = await asyncio.gather(*(ac.fetch(u) for u in urls))
```

Sync and async coexist — each `Client` and `AsyncClient` you construct
owns its own chromium subprocess and pool. Use whichever shape your
codebase already speaks.

### Batching at high concurrency

`client.batch(urls, capture=...)` dispatches N URLs in parallel inside
Rust's tokio runtime, capped by the Client's `concurrency`:

```python
urls = [...]  # thousands
with onyxweb.Client(concurrency=16) as client:
    for result in client.batch(urls, capture="both"):  # "html"|"png"|"both"
        if result.html.dom.exists("meta[name='generator']"):
            ...
```

### Live config updates at runtime

The `client.config` attribute is a live proxy — attribute writes at any depth
take effect on the next fetch:

```python
client.config.network.user_agent = "Bot/2.0"       # next fetch picks up
client.config.emulation.locale = "ja-JP"
client.config.viewport.width = 2560
```

Launch-only fields (things Chrome needs at startup — `concurrency`, chrome
args, proxy, `ignore_https_errors`) raise `ValueError` at the assignment line
so you see the error immediately:

```python
client.config.concurrency = 32         # ValueError — recreate Client instead
client.config.chrome.args = ["--x"]    # ValueError — set at construction
```

### Thread-safe by design

A single `Client` is safe to share across Python threads; every public method
releases the GIL before entering Rust. N Python threads all do real parallel
work inside one tokio runtime, gated by the Client's `concurrency` semaphore:

```python
with onyxweb.Client(concurrency=16) as client:
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(client.fetch, urls))
```

### Rust-side HTML query — fast by default

`result.dom` is a lazy Rust-parsed DOM with both CSS-selector and BS4-style
lookups. Parsing + querying in Rust avoids the Python HTML-parsing tax on
high-volume pipelines:

```python
r = onyxweb.fetch(url)

# CSS selectors
r.dom.query("a[href^='https://']")     # → list[Element]
r.dom.query_one("meta[name='generator']")
r.dom.exists("script[type='module']")  # → bool

# BS4-familiar
r.dom.find("nav", class_="main-nav")
r.dom.find_all("div", class_="card", limit=10)

# Shortcuts
r.dom.title()                          # <title> text
r.dom.links()                          # all <a href=...>
r.dom.images()                         # all <img src=...>

# Fast substring — skips the html5ever parse entirely
if r.dom.contains("Cloudflare"): ...
```

### Navigation lifecycle — `wait_until` and the two settle knobs

```python
client = onyxweb.Client(
    wait_until="load",            # default — window.onload (Playwright-default)
    # wait_until="domcontentloaded",  # opt-in — parser done, no subresource wait
    wait_after_ms=0,              # sleep AFTER lifecycle, BEFORE post-load scripts
    wait_after_post_load_ms=0,    # sleep AFTER post-load scripts, BEFORE capture
)

# Per-call override
client.fetch(url, wait_until="domcontentloaded")
client.fetch(url, wait_after_ms=500)   # settle 500ms after load for SPA hydration
```

- `load` (default) waits for all subresources + deferred scripts; most complete.
- `domcontentloaded` returns as soon as the DOM parser finishes — faster on
  tracker-heavy sites but may miss post-DCL SPA mutations.
- `wait_after_ms` adds a fixed sleep after the chosen event — useful for
  React/Vue/etc. pages that hydrate post-load.
- `wait_after_post_load_ms` adds a fixed sleep AFTER any `post_load_scripts`
  run and BEFORE actions / capture — for scripts that schedule async work
  (`setTimeout`, deferred fetches) that needs to settle before HTML is read.

### Per-fetch primitives

Each `client.fetch(url, ...)` accepts per-call overrides that scope state
to a single fetch. The pool tab is restored to its baseline after each
call (scripts removed, blocks reset, headers restored), so per-fetch
extras never leak between calls. The most common ones:

```python
client.fetch(
    url,

    # JS that runs BEFORE any page script (Page.addScriptToEvaluateOnNewDocument).
    # Used for hooks, instrumentation, browser-API patching.
    scripts=[ALERT_HOOK, DECODEURI_TRACE],

    # JS that runs AFTER the loaded page (Runtime.evaluate). Single CDP call,
    # full DOM access. Each script's return value is captured into
    # r.post_load_results: list[Any] (json-decoded, in input order).
    post_load_scripts=[
        "document.querySelectorAll('[onclick]').forEach(e => e.click())",
        "Array.from(document.querySelectorAll('a')).map(a => a.href)",
    ],

    # Block requests at the network layer for this fetch only (additive over
    # client-level network.block_urls). Restored after capture.
    block_urls=["*://*.tracker.example/*"],

    # Block navigations triggered AFTER initial load (target=_blank,
    # location.href=, action=javascript:...). Initial load proceeds normally;
    # post-load nav requests are aborted so subsequent post_load_scripts /
    # actions / capture all see the original page.
    block_navigation=True,

    # CDP-trusted post-load actions (event.isTrusted === true). Click, Fill,
    # Hover, Wait. Use for bot-detection / strict real-user simulation;
    # post_load_scripts is simpler for everything else.
    actions=[onyxweb.Click("#login-btn")],

    # Per-call extra headers. Cross-origin Referer is supported and routed
    # through Page.navigate's referrer parameter (the W3C-policy-safe path).
    extra_headers={"X-Run": "abc", "Referer": "https://r.example/page"},
)
```

Each result carries the captured side-channels so you can correlate:

```python
r = client.fetch(url, post_load_scripts=["JSON.parse(document.body.dataset.payload || '{}')"])
r.post_load_results[0]                  # → the parsed object
r.console_messages                      # list[ConsoleMessage] from the page
r.errors                                # filtered console errors + uncaught exceptions
r.final_url                             # post-redirect URL
r.status_code                           # main-document HTTP status
```

### Response metadata, headers, and hashes

Every `RenderResult` (and `FetchResult`) exposes the full HTTP response —
metadata, real headers (including Set-Cookie), TLS cert, redirect chain, and
content hashes. The shape mirrors
[blasthttp](https://github.com/blacklanternsecurity/blasthttp), so a rendered
onyxweb response is a drop-in for BBOT's `HTTP_RESPONSE`:

```python
r = client.fetch("https://example.com")

# .metadata — structured response metadata
r.metadata.status_code          # 200
r.metadata.status_text          # "OK"  (empty on h2/h3 — no reason phrase)
r.metadata.mime_type            # "text/html"
r.metadata.protocol             # "h2" / "http/1.1" / "h3"
r.metadata.remote_ip            # "93.184.216.34"
r.metadata.remote_port          # 443
r.metadata.content_length       # len of the rendered (post-JS) body bytes
r.metadata.request_url          # original URL (before redirects)
r.metadata.request_method       # "GET"
r.metadata.redirect_chain       # [RedirectHop(url=..., status=301, remote_ip=...), ...]
r.metadata.cert_info            # CertInfo(common_name, sans, issuer, not_before, ...) | None
r.metadata.body_hashes          # Hashes(md5, mmh3, sha256) over the body

# .headers — case-insensitive Mapping[str, str]
r.headers["content-type"]       # case-insensitive lookup
"server" in r.headers           # membership
dict(r.headers)                 # plain dict
r.headers.set_cookie            # ["sid=...; HttpOnly", ...]  (all Set-Cookie)
r.headers.cookies               # {"sid": "..."}  parsed name→value
r.headers.raw                   # "content-type: text/html\r\nserver: ...\r\n..."
r.headers.hashes                # Hashes(md5, mmh3, sha256) over .raw

# .anti_bot — WAF / anti-bot indicator (None when clean)
r.anti_bot                      # AntiBot(vendor="akamai", kind="challenge", resolved=True) | None
r.anti_bot.vendor               # "akamai"/"cloudflare"/"datadome"/... | None (generic)
r.anti_bot.kind                 # "challenge" (JS interstitial) | "block" (hard 403/429)
r.anti_bot.resolved             # True if we got to the real page; False if stub/block
```

**Hashes** are computed in Rust and are byte-exact with Python's `mmh3.hash()`
and `hashlib` (mmh3 is the signed 32-bit MurmurHash3, seed 0), so they line up
with tools like BBOT and blasthttp.

**On HTTP/2 & HTTP/3:** the protocol has no textual header block on the wire
(headers are binary-compressed), so `headers.raw` is the *canonical*
`Name: Value\r\n…` form built from the real received headers — never a
fabricated `HTTP/1.1 200 OK` status line. The header names/values are always
real; only the h1-style framing is a rendering. (`cert_info.fingerprint_sha256`
is `None` for the same "don't fabricate" reason — CDP's `securityDetails`
doesn't expose it.)

### Chrome engine — `chrome.engine`

Two Chromium builds, selected with `engine`:

```python
onyxweb.Client(engine="shell")   # default — bundled chrome-headless-shell
onyxweb.Client(engine="full", chrome_path="/usr/bin/google-chrome")   # full Chrome
```

- **`"shell"`** (default) — the small bundled `chrome-headless-shell`.
  Fast and light, but *old-headless*: anti-bot vendors (Akamai etc.) flag it even
  with a spoofed UA.
- **`"full"`** — a full Chrome/Chromium in `--headless=new`, launched with
  `--enable-automation` dropped, `AutomationControlled` disabled (so
  `navigator.webdriver === false`), and a real Chrome UA (matched to the binary's
  version) set via a launch flag. Near-indistinguishable from real Chrome — it
  gets past Akamai/Cloudflare-class sites that hard-block the shell. Heavier, and
  it needs a full Chrome binary: pass `chrome_path=` or have `google-chrome` /
  `chromium` on the system. Pairs naturally with `bypass_anti_bot`.

### Anti-bot — `bypass_anti_bot`

One switch handles WAF anti-bot, vendor-agnostic (Akamai / Cloudflare /
DataDome / PerimeterX / Imperva). It does two things:

- **Waits out challenge interstitials.** A WAF often serves a small "checking
  your browser / verify you're human" stub that runs a JS check then
  self-reloads to the real page. Without this, you'd capture the stub (a 200
  that *isn't* the real page). With it on, onyxweb detects the interstitial and
  polls until it resolves *and* the real page stabilizes — **no settle tuning
  needed**. (Verified: `Client(engine="full", bypass_anti_bot=True)` returns
  Tesla's real 1.5 MB homepage on its own.)
- **Self-heals hard blocks.** On a 403/429 with a WAF signature, it drops just
  the tab's **anti-bot** cookies (`_abck`, `bm_*`, `ak_bmsc`, `_px*`,
  `datadome`, `cf_clearance`, …), keeps benign cookies, and retries once. Each
  pooled tab has its own cookie jar, so a flagged cookie from one fetch never
  poisons another (or another Client).

```python
client.fetch(url, bypass_anti_bot=True)      # per fetch
onyxweb.Client(bypass_anti_bot=True)        # Client-wide default
```

Off by default; the `recon` and `stealth` presets enable it. Most effective on
the `full` engine (which gets *offered* the challenge; the shell is
hard-blocked). A plain 403 with no WAF signature is left alone. An interactive
captcha that can't auto-solve just times out and you get whatever's on screen.

**The `.anti_bot` indicator is independent of this switch.** Every response
carries `r.anti_bot` (an `AntiBot` or `None`) — detection runs *whether or not*
`bypass_anti_bot` is on. So a plain fetch still tells you "this host is behind
Akamai" as a recon signal; turn the switch on and the same field reports
`resolved=True` once onyxweb clears the challenge/block. Detection is tuned to
avoid false positives on normal pages behind these WAFs: markers that leak onto
real content (Cloudflare's passive beacon, Imperva's script rewriting) only
count on a small challenge stub, never on a full-size page.

### Errors

Timeouts (both onyxweb's own navigation timeout and Chromium's CDP command
timeout) raise the builtin **`TimeoutError`**. Every other failure raises
**`onyxweb.OnyxwebError`**, which subclasses `RuntimeError` (so existing
`except RuntimeError` code keeps working):

```python
try:
    r = client.fetch(url)
except TimeoutError:
    ...                       # navigation / CDP timed out — skip or retry
except onyxweb.OnyxwebError:
    ...                       # bad URL, DNS failure, CDP error, chrome-not-found, ...
```

### CLI — `python -m onyxweb`

```
python -m onyxweb <URL>                     # HTML → stdout
python -m onyxweb <URL> -o out.html         # HTML → file, stdout silent
python -m onyxweb <URL> -s shot.png         # HTML → stdout, screenshot → file
python -m onyxweb <URL> --screenshot-only shot.jpg   # no HTML, image-only
python -m onyxweb <URL> --json              # metadata + html as JSON
python -m onyxweb <URL> --meta              # final_url/status → stderr
```

Image format is inferred from the output extension (`.jpg` / `.jpeg` → jpeg,
`.webp` → webp, else png). Override with `--format png|jpeg|webp --quality N`.

All the Client config knobs are available as flags: `--user-agent`,
`--width`, `--height`, `--timeout-ms`, `--locale`, `--timezone`, `--proxy`,
`--header K=V`, `--headers-file FILE`, `--no-js`, `--ignore-certs`, `--chrome PATH`.

Presets plug in via `--preset <module>.<NAME>` (with explicit flags
overriding preset fields when they overlap):

```bash
python -m onyxweb --preset full.stealth.BASIC https://www.tesla.com/
python -m onyxweb --preset shell.recon.FAST https://example.com -o page.html
python -m onyxweb --preset shell.archival.FULL_PAGE https://spa.example/ --screenshot shot.webp

python -m onyxweb --preset list    # print every known preset and exit
```

### Logging

Both Rust and Python sides emit structured logs. Control globally via the
`ONYXWEB_LOG` env var (or `RUST_LOG` as fallback); runtime via
`onyxweb.set_log_level()`:

```bash
ONYXWEB_LOG=info  python script.py       # launch, close, batch dispatch
ONYXWEB_LOG=debug python script.py       # per-fetch entry/exit + timings
ONYXWEB_LOG=trace python script.py       # every CDP step with millisecond timing
ONYXWEB_LOG='onyxweb::engine=trace,warn' python script.py   # per-module
```

```python
import onyxweb
onyxweb.set_log_level("debug")           # Python + Rust, both sides
onyxweb.logger.info("my app event")       # hierarchical under "onyxweb.*"
```

Bare levels auto-narrow to `onyxweb` only — you won't drown in
tungstenite/hyper chatter when you set `ONYXWEB_LOG=debug`.

## Configuration reference

Every knob lives under a nested sub-config. Flat kwargs on `Client(...)` and
`ClientConfig.from_flat(...)` map to the corresponding nested field.

| Section        | Fields                                                                              |
|----------------|-------------------------------------------------------------------------------------|
| (top level)    | `concurrency`, `wait_until`, `wait_after_ms`, `wait_after_post_load_ms`, `bypass_anti_bot`, `capture_console_level` |
| `viewport`     | `width`, `height`, `device_scale_factor`, `mobile`                                  |
| `network`      | `user_agent`, `user_agent_metadata`, `proxy`, `extra_headers`, `ignore_https_errors`, `block_urls`, `disable_cache`, `offline`, `latency_ms`, `download_bps`, `upload_bps` |
| `emulation`    | `locale`, `timezone`, `geolocation`, `prefers_color_scheme`, `javascript_enabled`   |
| `scripts`      | `on_new_document`, `on_dom_content_loaded`, `on_load`, `isolated_world`, `url_scoped` |
| `timeout`      | `navigation_ms`, `launch_ms`, `screenshot_ms`                                        |
| `chrome`       | `path`, `args`, `user_data_dir`, `headless`, `engine`                                |

`extra_headers` rejects a small list of headers chromium drops or
computes from request state (`Cookie`, `Host`, `Origin`,
`Content-Length`, `Transfer-Encoding`, `Connection`) at config-construction
time, with a clear error pointing to the right alternative. `Referer` is
not in that list — it's lifted out and routed through `Page.navigate`'s
referrer parameter so cross-origin values pass through unchanged.

Per-call overrides (on `FetchConfig`): `scripts`, `post_load_scripts`,
`block_urls`, `block_navigation`, `bypass_anti_bot`, `actions`, `extra_headers`,
`timeout_ms`, `wait_until`, `wait_after_ms`, `wait_after_post_load_ms`.
`ScreenshotConfig` adds `viewport`, `full_page`, `format`, `quality`.

**Env vars**: set via `ONYXWEB_` prefix + `__` delimiter for nesting.
`ONYXWEB_CONCURRENCY=32`, `ONYXWEB_VIEWPORT__WIDTH=1920`,
`ONYXWEB_NETWORK__USER_AGENT='Mozilla/5.0 …'`. List fields (e.g.
`ONYXWEB_NETWORK__BLOCK_URLS`) JSON-parse: `'["*ad*","*track*"]'`.

**Runtime mutation**: `client.config.<section>.<field> = value` at any
depth auto-syncs to the Rust engine; next fetch picks it up. Launch-only
fields (`concurrency`, `chrome.*`, `network.proxy`, etc.) raise
`ValueError` at the offending assignment. Call `client.config.snapshot()`
for a detached deep-copy.

## Scripts, client hints, presets

onyxweb's config covers everything you'd otherwise have to bolt on after
the fact — JS injection with timing & scope, structured client-hint
metadata, ad/tracker blocking, proxy, network throttling, locale/timezone
overrides. Use the fields directly for one-off setups, or bundle related
settings into a **preset** (a plain `dict`) for reuse.

### Injecting JavaScript — `ScriptsConfig`

Declarative JS injection, applied per pool page via CDP's
`Page.addScriptToEvaluateOnNewDocument`. Five fields cover the common
timing / scope choices:

```python
Client(scripts={
    "on_new_document":       [js],  # fires before any page script, every nav
    "on_dom_content_loaded": [js],  # wrapped in a DOMContentLoaded listener
    "on_load":               [js],  # wrapped in a window.load listener
    "isolated_world":        [js],  # runs in world "onyxweb_isolated";
                                    # page JS can't read the globals it sets
    "url_scoped":            {"/path": [js]},  # substring-gated by URL
})
```

`on_new_document` is the CDP primitive; the rest are sugar implemented by
wrapping the source. `isolated_world` is genuinely separate — page scripts
live in one JS global, the isolated world in another (DOM is shared).
Useful for scraping logic that mustn't be observable by page JS.

Example — extract JSON-LD structured data during navigation:

```python
EXTRACT_LDJSON = """
document.addEventListener('DOMContentLoaded', () => {
  const nodes = document.querySelectorAll('script[type="application/ld+json"]');
  const data = Array.from(nodes).map(n => n.textContent);
  document.documentElement.dataset.ldjson = JSON.stringify(data);
});
"""
with Client(scripts={"on_new_document": [EXTRACT_LDJSON]}) as c:
    html = c.fetch(url)
    # data-ldjson attribute now present on <html>; pull it out with .dom.find(...)
```

**CDP-level limitations** (not onyxweb's — documented so you don't get
surprised):
- Scripts do NOT propagate into cross-origin iframes.
- Scripts do NOT run in service workers / shared workers.
- Runtime updates to `config.scripts.*` affect only *new* pool pages;
  existing pool pages keep their original registrations.

### Structured User-Agent — `network.user_agent_metadata`

The `User-Agent` header has a structured counterpart: the `Sec-CH-UA-*`
client hints. If you override the UA but leave the client hints alone,
servers that compare the two see a mismatch — itself a fingerprinting
tell. Pair them:

```python
Client(
    user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    user_agent_metadata={
        "brands": [
            {"brand": "Google Chrome", "version": "131"},
            {"brand": "Chromium",      "version": "131"},
            {"brand": "Not_A Brand",   "version": "24"},
        ],
        "platform": "Linux",
        "platform_version": "",
        "architecture": "x86",
        "model": "",
        "mobile": False,
        "bitness": "64",
    },
)
```

### Presets — engine-first bundles you can spread

Presets are `dict`s, organized **engine-first** (`shell` vs `full`) because the
two engines need opposite recipes — then by purpose (`stealth` / `recon` /
`archival`). Spread one into `Client(...)`:

```python
from onyxweb import Client
from onyxweb.presets.shell import recon, stealth as shell_stealth
from onyxweb.presets.full import stealth as full_stealth

Client(**recon.FAST).fetch(url)                     # fast shell sweep
Client(**shell_stealth.BASIC).fetch(url)            # anti-naive-detection (shell)
Client(**full_stealth.BASIC).fetch("https://www.tesla.com/")  # anti-WAF (full Chrome)

# Pre-merge to tweak (Python forbids duplicate kwargs across ** spreads)
Client(**{**recon.FAST, "user_agent": "CorpScanner/1.0"}).fetch(url)
```

Rolling your own is just a `dict` literal:

```python
CORP_CRAWLER = {
    "user_agent": "CorpCrawler/2.0 (+https://corp.example/crawler)",
    "extra_headers": {"X-Crawler-Token": os.environ["CRAWLER_TOKEN"]},
    "block_urls": ["*://*.tracker.example/*"],
    "scripts": {"on_load": [EXTRACT_LDJSON]},
}
with Client(**CORP_CRAWLER) as c: ...
```

**Built-in presets** (`presets.<engine>.<purpose>.<NAME>`; CLI: `--preset full.stealth.BASIC`):

| Preset                        | What it sets                                                                             | When to reach for it                                                        |
|-------------------------------|------------------------------------------------------------------------------------------|-----------------------------------------------------------------------------|
| `full.stealth.BASIC`          | `engine="full"` (real Chrome, automation tells stripped) + `bypass_anti_bot`. **No JS patches.** | **Akamai/Cloudflare-class WAFs** — the real anti-bot path (verified on tesla.com). Needs a full Chrome binary. |
| `shell.stealth.BASIC`         | UA brand swap + matching `Sec-CH-UA` + 5 JS patches + `bypass_anti_bot`                     | Sites with *naive* JS bot checks. **Not** a WAF bypass — real WAFs hard-block the shell regardless. |
| `shell.stealth.FINGERPRINT`   | `shell.stealth.BASIC` + WebGL vendor/renderer override + canvas `toDataURL` noise         | Extra fingerprint diversity on the shell                                    |
| `shell.recon.FAST`            | `javascript_enabled=False`, 5s nav timeout, ad/tracker block list, `bypass_anti_bot`        | BBOT-style subdomain sweeps; you want bytes, fast, no JS                     |
| `shell.archival.FULL_PAGE`    | 1920×1080 viewport, 30s nav timeout, 2s post-load settle                                  | Change detection / wayback-style snapshots of SPA-heavy sites               |

### Shell stealth — what each patch counters (and its limits)

`onyxweb.presets.shell.stealth` is modeled on
[rebrowser-patches](https://github.com/rebrowser/rebrowser-patches) —
opt-in, every patch commented with the detection vector it counters. No silent
evasion.

**Scope:** these patches counter *naive* client-side bot checks — they are **not
a WAF bypass**. Akamai/Cloudflare-class WAFs hard-block `chrome-headless-shell`
regardless (empirically, still a 403 on tesla.com even with all patches). For
those, use `full.stealth.BASIC` (real Chrome, no patches — on full Chrome the
patches would fake things it already has and *create* tells).

The default chrome-headless-shell UA contains the literal substring
`HeadlessChrome`, which many sites first-byte-match. Beyond UA, naive anti-bot
scripts probe JS runtime globals:

| `shell.stealth.BASIC` patch                                       | Counters                                                      |
|-------------------------------------------------------------------|---------------------------------------------------------------|
| UA + matching `Sec-CH-UA` brand metadata                          | First-byte UA substring checks; client-hint consistency       |
| `navigator.webdriver` → `undefined`                               | The most-detected automation tell                             |
| `window.chrome.runtime` populated                                 | Checks for `chrome.runtime.OnInstalledReason`                 |
| `navigator.plugins` with 5 PDF-viewer entries                     | `plugins.length === 0` heuristic                              |
| `navigator.permissions.query` returns `default` for notifications | Headless returns `denied`; real Chrome returns `default`      |
| `navigator.hardwareConcurrency = 8`, `deviceMemory = 8`           | CI-env low-value heuristic                                    |

| `shell.stealth.FINGERPRINT` also adds                                  | Counters                                                       |
|------------------------------------------------------------------------|----------------------------------------------------------------|
| WebGL `UNMASKED_VENDOR_WEBGL` / `UNMASKED_RENDERER_WEBGL` override     | `SwiftShader` identity as the software-renderer giveaway       |
| Canvas `toDataURL` per-session noise                                   | Bit-identical canvas fingerprint hash                          |

**What stealth doesn't fix** (documented rather than silently missing):

- **TLS ClientHello fingerprint** — chrome-headless-shell uses the same
  BoringSSL build as full Chrome, so JA3/JA4 already matches "real Chrome"
  by default. Vendors that fingerprint further at the network layer need
  something like `curl-impersonate` or the retired `wreq`/BoringSSL path
  that lived on onyxweb's pre-CDP branch.
- **Cross-origin iframe propagation** — CDP init scripts don't reach
  cross-origin iframes. Cloudflare Turnstile specifically runs in one.
- **Service-worker / shared-worker scope** — same limitation.
- **Behavioral simulation** — mouse curves, scroll physics, timing jitter.
- **`cdc_*` window property** injected when `Runtime.enable` is called —
  requires a Chromium binary patch (see rebrowser-patches).

## Development

Prerequisites: [`uv`](https://docs.astral.sh/uv/) and a stable Rust toolchain
(`rustup` recommended — `rust-toolchain.toml` pins the channel). Python is
auto-managed by `uv` via `.python-version` (3.11).

```bash
# One-shot env setup: creates .venv, installs deps, builds the Rust extension
# in editable mode. Safe to re-run — incremental.
uv sync

# Fetch chrome-headless-shell for this platform (~100 MB, one-time)
uv run onyxweb-download-chrome

# Run things — no venv activation needed
uv run onyxweb https://example.com              # the CLI
uv run pytest                                    # test suite
uv run ruff check .                              # lint
uv run mypy python/onyxweb                      # typecheck

# Benchmarks — gauntlet lives in tests/ as `@pytest.mark.benchmark`, skipped
# by default. Run it with -m benchmark -s (no output capture so the per-phase
# timing tables print live).
uv run pytest -m benchmark -s

# Cross-tool comparison benchmarks (pulls Playwright + its Chromium)
uv sync --group bench
uv run playwright install chromium

# Build a release wheel (bundles chrome-headless-shell for all target platforms)
uv run onyxweb-download-chrome --all
uv build
```

Rust edits to `src/*.rs` trigger a rebuild on the next `uv sync` / `uv run`
automatically — no manual `maturin develop` needed. This is driven by
`[tool.uv] cache-keys` in `pyproject.toml`, which tracks `Cargo.toml`,
`Cargo.lock`, `rust-toolchain.toml`, and `src/**/*.rs`.

Supported platforms for the bundled binary: linux-x86_64, linux-aarch64,
darwin-x86_64, darwin-aarch64, windows-x86_64. (v2.0 ships linux-x86_64;
others in follow-ups.)

## License

onyxweb is Apache-2.0 or MIT (your choice). The bundled
`chrome-headless-shell` is BSD-3-Clause (Google Chrome for Testing).
