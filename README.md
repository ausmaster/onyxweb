# onyxweb

[![PyPI](https://img.shields.io/pypi/v/onyxweb)](https://pypi.org/project/onyxweb/) [![Python](https://img.shields.io/badge/python-3.10+-blue)](https://www.python.org) [![License](https://img.shields.io/badge/license-BSD--3--Clause-green)](LICENSE) [![Tests](https://github.com/ausmaster/onyxweb/actions/workflows/ci.yml/badge.svg)](https://github.com/ausmaster/onyxweb/actions)

### **URL in, fully-rendered HTML out.** A Rust + Chromium (CDP) engine with a typed Python API, built for high-throughput **recon**, **scraping**, and **change detection**.

No Node process like Playwright, no WebDriver like Selenium. One install, one process, ~8.5 URL/s.

## Installation

```bash
uv add onyxweb              # or: pip install onyxweb
uv run onyxweb --install    # one-time: fetch the pinned chrome-headless-shell (~180 MB)
```

Python 3.10+. Wheels for linux (x86_64, aarch64), macOS (arm64), Windows x64. Anything else builds from source and needs [rustup](https://rustup.rs).

Embedding onyxweb in another tool? `await onyxweb.aensure_chrome(dest=...)` installs the browser wherever you want and returns the path for `Client(chrome_path=...)`; `find_chrome()` is a no-network "is it installed?" check.

## Quickstart

```python
import onyxweb

html = onyxweb.fetch("https://example.com")       # rendered HTML, post-JS
png  = onyxweb.screenshot("https://example.com")  # png / jpeg / webp
both = onyxweb.fetch_all("https://example.com")   # both, from one page visit

# CSS + BeautifulSoup-style queries, parsed and run in Rust
html.dom.title()                      # "Example Domain"
html.dom.find_all("a", limit=10)
html.dom.links(), html.dom.images()
```

`RenderResult` subclasses `str`, so regex, lxml, and BS4 all take it directly.

## Examples

#### 1) Sweep a lot of URLs

```python
with onyxweb.Client(concurrency=16) as c:
    for r in c.batch(urls, capture="html"):
        if isinstance(r, Exception):   # batch never raises; failures land in place
            continue
        print(r.status_code, r.dom.title())
```

#### 2) Async, or N threads on one Client

```python
async with onyxweb.AsyncClient(concurrency=16) as ac:
    results = await asyncio.gather(*(ac.fetch(u) for u in urls))
```

The GIL is released for all Rust work, so a thread pool over one `Client` runs genuinely parallel.

#### 3) Get past a WAF

```python
from onyxweb.presets.full import stealth

with onyxweb.Client(**stealth.BASIC) as c:      # real Chrome, automation tells stripped
    r = c.fetch("https://www.tesla.com/")
```

#### 4) Drive the page before capture

```python
r = client.fetch(
    url,
    scripts=[HOOK_JS],                          # runs before any page script
    post_load_scripts=["document.title"],       # returns land in r.post_load_results
    actions=[onyxweb.Click(selector="#login")], # CDP-trusted click / fill / hover / wait
    block_urls=["*://*.tracker.example/*"],
    extra_headers={"Referer": "https://ref.example/"},
)
```

Per-call settings are reverted before the tab returns to the pool, so nothing leaks between fetches.

## Anti-bot

`r.anti_bot` is populated on **every** fetch, whether or not you try to get past anything, so a plain fetch tells you a host sits behind Akamai.

```python
r.anti_bot   # AntiBot(vendor="cloudflare", kind="challenge", resolved=True) or None
```

`bypass_anti_bot=True` waits out challenge interstitials and self-heals hard blocks by dropping only the anti-bot cookies, then retrying once. Vendor-agnostic (Akamai, Cloudflare, DataDome, PerimeterX, Imperva).

In testing the full engine cleared Akamai, DataDome, PerimeterX, and Cloudflare on sites like tesla.com and ticketmaster. It does not beat Kasada or an interactive captcha, and says so through `.anti_bot` instead of handing back a challenge stub.

## Two engines

```python
onyxweb.Client(engine="shell")   # default: bundled chrome-headless-shell, fast and light
onyxweb.Client(engine="full")    # real Chrome, --headless=new, beats WAFs the shell can't
```

## Presets

Spread into `Client(...)`. Organized engine-first, since the two engines want opposite recipes.

| Preset | When to use it |
|---|---|
| `full.stealth.BASIC` | Akamai/Cloudflare-class WAFs (needs a full Chrome binary) |
| `shell.stealth.BASIC` | naive JS bot checks, not a real WAF bypass |
| `shell.stealth.FINGERPRINT` | BASIC plus WebGL vendor override and canvas noise |
| `shell.recon.FAST` | subdomain sweeps: JS off, 5 s timeout, ad/tracker blocklist |
| `shell.archival.FULL_PAGE` | change-detection snapshots of SPA-heavy sites |

```bash
onyxweb --preset list        # print every preset and exit
```

## The response

Every result carries the whole HTTP response, shaped to match [blasthttp](https://github.com/blacklanternsecurity/blasthttp) so it drops straight into BBOT's `HTTP_RESPONSE`.

```python
r.metadata.status_code, r.metadata.protocol, r.metadata.remote_ip
r.metadata.redirect_chain, r.metadata.cert_info, r.metadata.body_hashes
r.headers["content-type"], r.headers.cookies, r.headers.raw
```

Hashes (md5 / mmh3 / sha256) are computed in Rust and match Python's `hashlib` and `mmh3.hash()` byte for byte.

## CLI

```bash
onyxweb https://example.com                  # HTML to stdout
onyxweb https://example.com -o page.html -s shot.png
onyxweb https://example.com --json           # HTML + metadata as JSON
onyxweb --help                               # every config knob is a flag
```

## Configuration

Flat kwargs, a `ClientConfig` object, or `ONYXWEB_*` environment variables.

```python
onyxweb.Client(viewport=(1920, 1080), locale="en-GB", proxy="http://user:pass@host:8080")
```

`client.config` is a live view: assign at any depth and the next fetch uses it. Launch-only fields (concurrency, chrome options) raise `ValueError` instead of failing silently. Full field list with docs: [`python/onyxweb/config.py`](python/onyxweb/config.py).

Two knobs worth knowing, both off by default, since `outerHTML` drops this content:

```python
onyxweb.Client(include_shadow_dom=True)   # web components
onyxweb.Client(include_iframes=True)      # same-origin iframes
```

## Errors

```python
try:
    r = client.fetch(url)
except TimeoutError:            # navigation + CDP timeouts
    ...
except onyxweb.OnyxwebError:    # subclasses RuntimeError; carries .url and .kind
    ...
```

## Development

```bash
uv sync                        # venv, deps, Rust extension in editable mode
uv run onyxweb-download-chrome
uv run pytest                  # tests are Python end-to-end; no Rust unit tests, on purpose
uv run pytest -m real_sites    # integration tests against live sites (opt-in)
```

Editing `src/*.rs` rebuilds on the next `uv run`. Set `ONYXWEB_LOG=debug` for engine logs. Benchmarks and the engine comparison that led here are in [BENCHMARKS.md](BENCHMARKS.md).

## License

BSD 3-Clause. The bundled `chrome-headless-shell` is also BSD-3-Clause (Google's Chrome for Testing).
