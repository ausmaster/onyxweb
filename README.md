# onyxweb

[![PyPI](https://img.shields.io/pypi/v/onyxweb)](https://pypi.org/project/onyxweb/) [![Python](https://img.shields.io/badge/python-3.11+-blue)](https://www.python.org) [![License](https://img.shields.io/badge/license-BSD--3--Clause-green)](LICENSE) [![Tests](https://github.com/ausmaster/onyxweb/actions/workflows/ci.yml/badge.svg)](https://github.com/ausmaster/onyxweb/actions)

### **URL in, fully-rendered HTML out.** A Rust + Chromium (CDP) engine with a typed Python API, built for high-throughput **recon**, **scraping**, and **change detection**.

No Node process like Playwright, no WebDriver like Selenium. One install, one process, ~8.5 URL/s.

## Installation

```bash
uv add onyxweb              # or: pip install onyxweb
uv run onyxweb --install    # one-time: fetch the pinned chrome-headless-shell (~180 MB)
```

Python 3.11+. Wheels for linux (x86_64, aarch64), macOS (arm64), Windows x64. Anything else builds from source and needs [rustup](https://rustup.rs).

Embedding onyxweb in another tool? `await onyxweb.aensure_chrome(dest=...)` installs the browser wherever you want and returns the path for `Client(chrome_path=...)`; `find_chrome()` is a no-network "is it installed?" check.

## Quickstart

```python
import onyxweb

r    = onyxweb.fetch("https://example.com")       # rendered HTML, post-JS
png  = onyxweb.screenshot("https://example.com")  # png / jpeg / webp
both = onyxweb.fetch_all("https://example.com")   # both, from one page visit

r.title                               # "Example Domain"
r.text                                # visible text; script and style source left out
r.links, r.images, r.scripts          # lazy buckets of records (see Page buckets)

# CSS + BeautifulSoup-style queries, parsed and run in Rust
r.dom.find_all("a", limit=10)
```

`RenderResult` is not a `str`. Pass `r.html` to regex, lxml, or BS4. `str(r)`, `"x" in r`, and `len(r)` still work.

## Examples

#### 1) Sweep a lot of URLs

```python
with onyxweb.Client(concurrency=16) as c:
    for r in c.batch(urls, capture="html"):
        if isinstance(r, Exception):   # batch never raises; failures land in place
            continue
        print(r.status_code, r.title)
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

## Page buckets

A captured page is sorted into 9 lazy buckets: `scripts`, `styles`, `links`, `images`, `iframes`, `forms`, `meta`, `comments`, `json_ld`. Sizing or printing one costs nothing until you read its records.

```python
r.overview(prnt=True)                 # count and size of every bucket, no records built
r.content.scripts                     # inline half: source that lives in the document
r.resources.scripts                   # external half: URLs the page loads
r.resources.all()                     # everything the browser fetches, in document order

r.scripts.search("apiKey")            # records containing a string, matched in Rust
r.scripts.matches(r'"apiKey":"(\w+)"', regex=True)[0].value   # just the captured key
```

Every URL-bearing record carries `url` (absolute) and `raw` (as authored). Search patterns use Rust's `regex` crate: linear time, no lookaround.

## Snapshots

A snapshot is one JSON file holding a page and its response. Save it once, then read it later with no Chrome and no network.

```python
r.save("page.json")                          # html, headers, metadata, verdicts
r = onyxweb.RenderResult.load("page.json")   # same buckets, search and text
```

```bash
onyxweb https://example.com --json -o page.json   # fetch once, keep a snapshot
onyxweb page overview page.json                   # then look, with no re-fetch
onyxweb page search page.json apiKey
onyxweb page text page.json scripts 1             # one record, whole
```

`onyxweb page` reads a snapshot and never fetches:

| Command | Prints |
|---|---|
| `overview FILE` | count and size of every bucket |
| `search FILE QUERY` | each match with its surroundings; `--bucket`, `--field`, `--regex` and `--case-sensitive` narrow it |
| `text FILE BUCKET INDEX` | one record's whole content; `INDEX` is the `#` column of a table |

A snapshot holds the html, final URL, status, headers, metadata, console messages, script results and anti-bot verdict. It holds no screenshot. The file is JSON, not pickle, so loading one runs no code, and it carries a version: `load` rejects a newer one and says how to fix it. `r.snapshot()` returns the same data as a dict.

<details>
<summary><code>onyxweb page --help</code></summary>

```text
usage: python -m onyxweb page [-h] command ...

Query a saved page snapshot offline.

positional arguments:
  command
    overview  count and size of every bucket
    search    show where a query matches
    text      print one record's whole content

options:
  -h, --help  show this help message and exit
```

```text
usage: python -m onyxweb page search [-h]
                                     [--bucket {scripts,styles,iframes,comments,forms,meta,json_ld,links,images}]
                                     [--field FIELD] [--regex]
                                     [--case-sensitive]
                                     snapshot query

positional arguments:
  snapshot              file from --json -o or RenderResult.save
  query                 text to find, or a pattern with --regex

options:
  -h, --help            show this help message and exit
  --bucket {scripts,styles,iframes,comments,forms,meta,json_ld,links,images}
                        search one bucket only
  --field FIELD         match only inside this record field, e.g. url
  --regex               treat the query as a pattern
  --case-sensitive      match letter case exactly
```

</details>

## Serving agents

`onyxweb-server`, a separate package in this repository, serves onyxweb's browser to agents over MCP so an agent such as Claude Code can fetch a page once, then look, find and read it in pieces. Install and usage: [`server/README.md`](server/README.md).

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
onyxweb https://example.com --json           # snapshot JSON: page, headers, metadata
onyxweb https://example.com --json -o page.json   # ... to a file
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

## Docker and BBOT

Chrome runs with its sandbox on, and the sandbox cannot start as root, under Docker's default container profile, or where the kernel restricts user namespaces (Ubuntu 23.10 and later). The launch then fails with `browser launch failed: ... pass sandbox=False`. In Docker, including BBOT in Docker, turn the sandbox off in one of two ways:

```python
onyxweb.Client(sandbox=False)
```

```bash
docker run -e ONYXWEB_CHROME__SANDBOX=false ...   # any process that builds a Client, BBOT included
```

Tested: the default Docker seccomp profile, as root and as a non-root user, on both engines. Off adds `--no-sandbox`, so a renderer exploit from a hostile page runs as the container's user. Treat the container as the boundary: mount no host paths and add no capabilities. A seccomp profile that permits Chrome's namespace calls also keeps the sandbox on inside Docker; that route is not tested here.

The variable applies to any `Client`, including one built with `engine=` or `chrome_path=`. Releases up to 0.2.3 ignored it in that case.

## Errors

```python
try:
    r = client.fetch(url)
except onyxweb.ChromeExitedError:   # Chrome died; every later call fails too, so build a new Client
    ...
except TimeoutError:                # navigation + CDP timeouts, and QueueTimeoutError
    ...
except onyxweb.OnyxwebError:        # subclasses RuntimeError; carries .url and .kind
    ...
```

`client.alive` is `False` once Chrome has exited or the client is closed, and checking it costs no fetch. `Client(queue_timeout_ms=5000)` makes `fetch`, `screenshot` and `fetch_all` raise `QueueTimeoutError` (a `TimeoutError`) after 5 s without a free tab, instead of waiting; `batch` ignores it.

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
