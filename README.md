# onyxweb

onyxweb takes a URL and returns the HTML a real browser renders, after the page's JavaScript has run. It's a Python package, but the work happens in a Rust core that drives Chromium over the Chrome DevTools Protocol. There's no Node process in the middle like Playwright, and no separate WebDriver like Selenium.

The common tools each fall short somewhere. requests and httpx are fast but don't run JavaScript, so a modern site returns an empty `<div id="root">`. Playwright renders the page, but every call crosses from Python into Node, which adds up over thousands of URLs. Selenium is older, slower, and keeps a driver between the caller and the browser. onyxweb returns the rendered HTML, fast, from a single install, with nothing extra in the call chain.

The response is the post-JavaScript HTML, and on request a screenshot, the full HTTP response (status, real headers, TLS cert, redirect chain, content hashes), and a read on whether the site sits behind a WAF. It targets high-throughput, read-mostly work like security recon, subdomain sweeps, and change detection, where a single process pulls hundreds of URLs a minute.

Requires Python 3.10+.

```python
import onyxweb

html = onyxweb.fetch("https://example.com")        # rendered HTML, post-JS
png  = onyxweb.screenshot("https://example.com")   # PNG by default (jpeg/webp too)
both = onyxweb.fetch_all("https://example.com")     # HTML and image from one visit

# The CSS/BS4 queries run in Rust, so there's no Python parse on every page
print(html.dom.title())            # "Example Domain"
print(html.dom.find("h1").text)    # "Example Domain"
print(html.dom.links())            # ["https://www.iana.org/..."]
```

## Requirements

- Python 3.10+
- A Chromium binary. `onyxweb --install` downloads the pinned `chrome-headless-shell` (~180 MB, one time), or point onyxweb at a system `chromium` / `chrome`.

## Install

Two steps, the same idea as Playwright: a small wheel, then a one-time browser download.

```bash
uv add onyxweb              # or pip install onyxweb, or pipx install onyxweb
uv run onyxweb --install    # download chrome-headless-shell
```

If chromium is already on the PATH, skip `--install`. onyxweb looks for a browser in this order: `chrome_path=` or `ONYXWEB_CHROME__PATH`, then the `--install` download, then `chromium` / `chrome` on the PATH.

Wheels are prebuilt for linux (x86_64, aarch64), macOS (arm64), and Windows x86_64. On anything else, including Intel macOS, pip builds from source, which needs a stable Rust toolchain (install [rustup](https://rustup.rs)). The `--install` step is the same either way.

If you're embedding onyxweb in another tool, you can do the browser install from Python and pick where the binary lands:

```python
path = onyxweb.ensure_chrome(dest="~/.mytool/tools")         # sync; returns the binary path
path = await onyxweb.aensure_chrome(dest="~/.mytool/tools")  # async, for setup hooks
here = onyxweb.find_chrome(dest="~/.mytool/tools")           # Path or None, no download
```

Both `ensure` calls are idempotent (they skip the download if the binary is already there) and hand back the path, which you pass straight to `Client(chrome_path=path)`. `find_chrome` is a cheap "is it installed?" predicate — useful for logging a one-time "downloading ~179 MB…" before the first `aensure_chrome`. That's the pattern a BBOT module uses: fetch the pinned browser into `~/.bbot/tools/` during async `setup_deps`, then drive it.

Every expected install failure — unsupported/non-downloadable platform, network, permissions, disk, or a corrupt archive — raises `onyxweb.OnyxwebDownloadError` (a subclass of `OnyxwebError`, so `except onyxweb.OnyxwebError` catches it). `find_chrome` never raises — it returns `None` on any host it can't map.

## Benchmarks

This is the reason the core is Rust rather than a wrapper over Playwright. 48-URL gauntlet, 16-core Linux, `chrome-headless-shell 148`. Full method is in `BENCHMARKS.md`.

| Tool | Config | URL/s |
|---|---|---|
| onyxweb | P=16, mode=both | 8.54 |
| Playwright-python | P=16 | 5.82 |
| Chromium headless, fork per URL | P=16 | 4.51 |
| Servo 0.1.0, in-process | P=8 | 1.13 |

At equal concurrency that's about 1.9 s/URL for onyxweb against 2.7 s/URL for Playwright. Most of the gap is the Python-to-Node hop onyxweb doesn't have to make.

## CLI

```bash
onyxweb https://example.com                    # HTML to stdout
onyxweb https://example.com -o page.html        # HTML to a file
onyxweb https://example.com -s shot.png         # HTML plus a screenshot file
onyxweb https://example.com --screenshot-only shot.webp   # image only
onyxweb https://example.com --json              # HTML and metadata as JSON
```

Image format follows the output extension (`.jpg`/`.jpeg`, `.webp`, otherwise png); override it with `--format` and `--quality`. The config options are all flags too: `--user-agent`, `--width`, `--height`, `--timeout-ms`, `--locale`, `--timezone`, `--proxy`, `--header K=V`, `--no-js`, `--chrome PATH`, and a few more (`onyxweb --help` lists them all). Presets plug in with `--preset`:

```bash
onyxweb --preset full.stealth.BASIC https://www.tesla.com/
onyxweb --preset shell.recon.FAST https://example.com -o page.html
onyxweb --preset list        # print every preset and exit
```

## The Client

The module-level `fetch` / `screenshot` / `fetch_all` share one lazily-created Client, which is fine for a script or a notebook. For anything high-volume, make a dedicated Client to set the concurrency and reuse the warm browser instead of paying the launch cost on every call.

```python
with onyxweb.Client(
    concurrency=16,
    viewport=(1920, 1080),
    user_agent="MyScraper/1.0",
    locale="en-GB",
    timezone="Europe/London",
    block_urls=["*doubleclick*", "*.googletagmanager.com/*"],
) as client:
    for r in client.batch(urls, capture="html"):   # runs N at a time inside tokio
        ...
```

Configuration takes three forms, whichever fits: flat keyword arguments like above, a `ClientConfig` object, or `ONYXWEB_*` environment variables, which pydantic loads on its own.

A single Client is safe to share across threads. Every method releases the GIL before it goes into Rust, so N Python threads do real parallel work, capped by the Client's concurrency.

```python
with onyxweb.Client(concurrency=16) as client:
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(client.fetch, urls))
```

There's a full async version as well. It constructs the same way and takes the same config; the methods return coroutines.

```python
async with onyxweb.AsyncClient(concurrency=16) as ac:
    r = await ac.fetch(url)
    htmls = await asyncio.gather(*(ac.fetch(u) for u in urls))
```

`client.config` is a live view of the config. Assign to it at any depth and the next fetch uses the new value. The fields Chrome needs at startup (concurrency, the chrome options) can't change after launch, so assigning to those raises a `ValueError` right at that line rather than failing silently.

```python
client.config.network.user_agent = "Bot/2.0"    # takes effect next fetch
client.config.concurrency = 32                   # ValueError, make a new Client for this
```

## Two engines: shell and full

There are two Chromium builds to choose from, and the right one depends on what the target runs.

```python
onyxweb.Client(engine="shell")   # default, the bundled chrome-headless-shell
onyxweb.Client(engine="full", chrome_path="/usr/bin/google-chrome")
```

`shell` is the default: the small bundled `chrome-headless-shell`, fast and light. The catch is that it's the old headless mode, which WAFs like Akamai can detect and flag even with a spoofed user agent.

`full` is a real Chrome running in `--headless=new`, launched with the automation tells stripped out (`navigator.webdriver` reads `false`, a real Chrome user agent, and so on). It's close enough to a normal browser to get past the Akamai/Cloudflare-class sites the shell can't. It's heavier, and it needs a full Chrome binary, so either pass `chrome_path=` or have `google-chrome` / `chromium` installed. It pairs naturally with `bypass_anti_bot`.

## Anti-bot

`r.anti_bot` is populated on every fetch, whether or not a bypass is requested. Even a plain fetch reports that a host sits behind Akamai, which is a useful recon signal on its own.

```python
r.anti_bot          # AntiBot(vendor="akamai", kind="challenge", resolved=True) or None
r.anti_bot.vendor   # "akamai" / "cloudflare" / "datadome" / ... or None if it's generic
r.anti_bot.kind     # "challenge" (a JS interstitial) or "block" (a hard 403/429)
r.anti_bot.resolved # True if the real page was reached
```

`bypass_anti_bot` is the switch that actually tries to get through, and it's vendor-agnostic (Akamai, Cloudflare, DataDome, PerimeterX, Imperva). It does two things. First, it waits out challenge interstitials, the "checking your browser" page that runs a JS check and then reloads itself to the real content, polling until the real page settles so there's no settle time to guess. Second, it self-heals a hard block: on a 403 or 429 with a WAF signature it drops just the anti-bot cookies (`_abck`, `bm_*`, `datadome`, `cf_clearance`, and similar), keeps everything else, and retries once.

```python
onyxweb.Client(bypass_anti_bot=True)          # on for the whole client
client.fetch(url, bypass_anti_bot=True)       # on for one fetch
```

It's off by default. The `recon` and `stealth` presets turn it on. It works best on the full engine, since the shell gets hard-blocked. An interactive captcha it can't solve will time out, returning whatever is on the screen.

On where it lands: in testing, the full engine with stealth pulled real content past Akamai, DataDome, PerimeterX, and Cloudflare on sites like tesla.com, ticketmaster, and hermes. It does not beat Kasada (footlocker, nike's launch pages) or an interactive captcha, and when it can't get through it reports that plainly through `.anti_bot` rather than returning a challenge stub as if the fetch had succeeded.

## Per-fetch options

Each `client.fetch(url, ...)` takes overrides that apply to just that one call. Afterward the pool tab is reset to its baseline (scripts removed, blocks cleared, headers restored), so nothing set on one fetch leaks into the next.

```python
client.fetch(
    url,
    scripts=[HOOK_JS],              # runs before any page script; for hooks and patches
    post_load_scripts=[             # runs on the loaded page; returns land in r.post_load_results
        "Array.from(document.querySelectorAll('a')).map(a => a.href)",
    ],
    block_urls=["*://*.tracker.example/*"],   # network block for this fetch only
    block_navigation=True,          # abort navigations the page tries after it loads
    actions=[onyxweb.Click(selector="#login")],   # CDP-trusted click / fill / hover / wait
    extra_headers={"Referer": "https://ref.example/"},   # cross-origin Referer works here
)
```

The result carries the side channels so they line up with the HTML:

```python
r.post_load_results    # one entry per post_load_script, json-decoded
r.console_messages     # the page's console.* output
r.errors               # console errors and uncaught exceptions
r.final_url            # after redirects
r.status_code
```

## The response

Every result carries the whole HTTP response, not just the body. The shape deliberately follows [blasthttp](https://github.com/blacklanternsecurity/blasthttp), so a rendered onyxweb response drops straight into BBOT's `HTTP_RESPONSE`.

```python
r = client.fetch("https://example.com")

r.metadata.status_code     # 200
r.metadata.mime_type       # "text/html"
r.metadata.protocol        # "h2" / "http/1.1" / "h3"
r.metadata.remote_ip       # "93.184.216.34"
r.metadata.redirect_chain  # [RedirectHop(url=..., status=301, ...), ...]
r.metadata.cert_info       # CertInfo(common_name, sans, issuer, ...) or None
r.metadata.body_hashes     # Hashes(md5, mmh3, sha256)

r.headers["content-type"]  # case-insensitive
r.headers.set_cookie       # every Set-Cookie value
r.headers.cookies          # parsed name -> value
r.headers.raw              # the canonical "Name: Value\r\n..." block
```

The hashes are computed in Rust and come out byte-for-byte identical to Python's `hashlib` and `mmh3.hash()` (mmh3 being the signed 32-bit MurmurHash3 at seed 0), so they line up with BBOT and blasthttp. Worth knowing: on HTTP/2 and HTTP/3 there is no textual header block on the wire, the headers are binary. So `headers.raw` is a canonical reconstruction from the real received headers, and it is never a fabricated `HTTP/1.1 200 OK` status line. The names and values are always the real ones.

## Querying the DOM

`r.dom` is a Rust-parsed DOM (parsed lazily, on first use) with both CSS selectors and BeautifulSoup-style lookups. The parse and the queries run in Rust, which keeps the HTML-parsing cost off Python across a lot of pages.

```python
r.dom.query("a[href^='https://']")     # list of Elements
r.dom.query_one("meta[name='generator']")
r.dom.exists("script[type='module']")  # bool
r.dom.find("nav", class_="main-nav")   # BS4-style
r.dom.find_all("div", class_="card", limit=10)
r.dom.title(); r.dom.links(); r.dom.images()
r.dom.contains("Cloudflare")           # plain substring, skips the parse entirely
```

## Injecting JavaScript, and presets

`ScriptsConfig` injects JS into each page through CDP, with the timing and scope options usually needed:

```python
Client(scripts={
    "on_new_document":       [js],   # before any page script, on every navigation
    "on_dom_content_loaded": [js],   # wrapped in a DOMContentLoaded listener
    "on_load":               [js],   # wrapped in a window.load listener
    "isolated_world":        [js],   # a separate JS world; page scripts can't see its globals
    "url_scoped":            {"/path": [js]},   # only when the URL contains the key
})
```

Two limits worth stating up front, both CDP's rather than onyxweb's: init scripts don't reach cross-origin iframes or workers, and a change to `scripts.*` at runtime only affects new pool tabs, not the ones already open.

Presets are just `dict`s spread into `Client(...)`. They're organized engine-first, because the shell and the full engine want opposite recipes, and then by purpose.

```python
from onyxweb.presets.shell import recon, stealth as shell_stealth
from onyxweb.presets.full import stealth as full_stealth

Client(**recon.FAST).fetch(url)                    # fast shell sweep, JS off
Client(**shell_stealth.BASIC).fetch(url)           # against naive detection (shell)
Client(**full_stealth.BASIC).fetch("https://www.tesla.com/")   # against a real WAF (full Chrome)
```

| Preset | What it sets | When to use it |
|---|---|---|
| `full.stealth.BASIC` | real Chrome, automation tells stripped, `bypass_anti_bot`, no JS patches | Akamai/Cloudflare-class WAFs (needs a full Chrome binary) |
| `shell.stealth.BASIC` | UA and `Sec-CH-UA` swap, JS patches, `bypass_anti_bot` | sites with naive JS bot checks, not a real WAF bypass |
| `shell.stealth.FINGERPRINT` | BASIC plus a WebGL vendor override and canvas noise | a bit more fingerprint diversity on the shell |
| `shell.recon.FAST` | JS off, 5 s timeout, an ad/tracker block list | subdomain sweeps where only the bytes matter, fast |
| `shell.archival.FULL_PAGE` | 1920x1080, 30 s timeout, a 2 s post-load settle | change-detection snapshots of SPA-heavy sites |

Making a custom one is just a dict literal:

```python
CRAWLER = {
    "user_agent": "CorpCrawler/2.0",
    "extra_headers": {"X-Token": os.environ["TOKEN"]},
    "block_urls": ["*://*.tracker.example/*"],
}
with Client(**CRAWLER) as c: ...
```

The shell stealth patches are modeled on [rebrowser-patches](https://github.com/rebrowser/rebrowser-patches). They're opt-in and each one is commented with the specific check it's meant to defeat, so nothing is hidden. They're for naive client-side bot checks, not WAFs. The shell is old-headless and Akamai-class WAFs hard-block it no matter how many patches are applied, so use the full engine for those. Stealth also deliberately leaves some things untouched, listed here rather than left as a surprise: the TLS ClientHello (it already matches real Chrome, since it's the same BoringSSL build), cross-origin iframes (Cloudflare Turnstile runs in one), workers, behavioral timing, and the `Runtime.enable` CDP tell, which would need a patched Chromium binary to remove.

## Config reference

Everything lives under a nested sub-config, and the flat keyword arguments map onto the nested field.

| Section | Fields |
|---|---|
| top level | `concurrency`, `wait_until`, `wait_after_ms`, `wait_after_post_load_ms`, `bypass_anti_bot`, `capture_console_level` |
| `viewport` | `width`, `height`, `device_scale_factor`, `mobile` |
| `network` | `user_agent`, `user_agent_metadata`, `proxy`, `proxy_bypass_list`, `extra_headers`, `ignore_https_errors`, `block_urls`, `disable_cache`, `offline`, `latency_ms`, `download_bps`, `upload_bps` |
| `emulation` | `locale`, `timezone`, `geolocation`, `prefers_color_scheme`, `javascript_enabled` |
| `scripts` | `on_new_document`, `on_dom_content_loaded`, `on_load`, `isolated_world`, `isolated_world_name`, `url_scoped` |
| `timeout` | `navigation_ms`, `launch_ms`, `screenshot_ms` |
| `chrome` | `path`, `args`, `user_data_dir`, `headless`, `engine` |

Environment variables use the `ONYXWEB_` prefix with `__` for nesting, so `ONYXWEB_CONCURRENCY=32`, `ONYXWEB_VIEWPORT__WIDTH=1920`, `ONYXWEB_NETWORK__USER_AGENT='...'`. The proxy accepts `http://user:pass@host:port` (auth handled internally) and can be changed at runtime; when proxying localhost during testing, pair it with `proxy_bypass_list="<-loopback>"`.

`wait_until` is either `"load"` (the default, window.onload) or `"domcontentloaded"` (parser done, faster, but it can miss work an SPA does after DCL). `wait_after_ms` and `wait_after_post_load_ms` are fixed sleeps for pages that hydrate late.

## Errors

Timeouts, both onyxweb's own navigation timeout and Chromium's CDP timeout, raise the builtin `TimeoutError`. Anything else raises `onyxweb.OnyxwebError`, which is a subclass of `RuntimeError`, so existing `except RuntimeError` still catches it.

```python
try:
    r = client.fetch(url)
except TimeoutError:
    ...
except onyxweb.OnyxwebError:   # bad URL, DNS failure, CDP error, chrome not found, ...
    ...
```

`batch` is the exception to this, it never raises. A URL that fails comes back as the exception object in its input position in the list, with `.url` and `.kind` attached, so one bad URL in a batch of thousands doesn't take down the rest.

## Logging

Set `ONYXWEB_LOG` (or `RUST_LOG` as a fallback) to `error` (the default), `warn`, `info`, `debug`, or `trace`, or call `onyxweb.set_log_level(...)` at runtime. A bare level narrows to onyxweb's own logs, so `debug` won't be buried under tungstenite and hyper noise.

```bash
ONYXWEB_LOG=debug python script.py
```

## Development

Development needs [`uv`](https://docs.astral.sh/uv/) and a stable Rust toolchain (`rustup`; the channel is pinned in `rust-toolchain.toml`). uv manages the Python version through `.python-version`.

```bash
uv sync                             # venv, deps, and the Rust extension built in editable mode
uv run onyxweb-download-chrome      # fetch chrome-headless-shell
uv run pytest                       # the test suite
uv run ruff check .                 # lint
uv run mypy python/onyxweb          # typecheck
uv run pytest -m benchmark -s       # the perf gauntlet, skipped by default
```

Editing anything in `src/*.rs` triggers a rebuild on the next `uv run`, so there's no manual build step. The tests are all Python end-to-end; there are deliberately no Rust unit tests, so there's a single test layer to reason about.

## License

BSD 3-Clause. The bundled `chrome-headless-shell` is also BSD-3-Clause (it's Google's Chrome for Testing).
