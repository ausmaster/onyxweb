# Changelog

All notable changes to onyxweb. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Releases before this file are listed under [GitHub tags](https://github.com/ausmaster/onyxweb/tags).

## [Unreleased]

### Added
- `Client.alive` and `AsyncClient.alive`: `False` once Chrome has exited or the client is closed, checked without a fetch.
- `sandbox` (`chrome.sandbox`, env `ONYXWEB_CHROME__SANDBOX`), default `True`: `False` runs Chrome with `--no-sandbox`. Launch-only. See "Docker and BBOT" in the README.
- `ChromeExitedError`, an `OnyxwebError` subclass with `.kind == "chrome_exited"`: once Chrome has died, every call on that client raises it at once, with the exit status and a note to create a new client.
- `queue_timeout_ms` (`timeout.queue_ms`), off by default: when set, `fetch`, `screenshot` and `fetch_all` raise `QueueTimeoutError` (a `TimeoutError` subclass, `.kind == "queue_timeout"`) after waiting that long for a free tab. `batch` ignores it.
- `RenderResult.save()` and `RenderResult.load()`: a JSON snapshot that reads back with the same buckets, search, text, headers and metadata, without Chrome or the network.
- `RenderResult.snapshot()`, the JSON-ready dict that `save()` writes.
- `onyxweb page` with `overview`, `search` and `text`, to query a snapshot offline.
- `Dom(html, doc_url)` can be built from Python, and `ResponseHeaders.pairs` lists every header as received.
- `onyxweb URL --json -o PATH` writes the snapshot to a file.

### Changed
- **Breaking:** `RenderResult.text` and `Element.text` now read as the page displays them. Block elements break lines, a table row stays on one line with tabs between cells, `<pre>` keeps its spacing, and runs of whitespace collapse elsewhere. Previously adjacent blocks ran together, so `<div>Alice</div><div>30</div>` read as `Alice30`. Response hashes are unaffected: they cover the HTML, not the text.
- **Breaking:** onyxweb requires Python 3.11 or later. The 0.2.3 wheels for Python 3.10 fail on import.
- **Breaking:** Chrome runs with its sandbox by default on the full engine, which always passed `--no-sandbox` before. Where the sandbox cannot start (root, Docker's default container profile, restricted user namespaces), pass `sandbox=False` or set `ONYXWEB_CHROME__SANDBOX=false`. Docker and BBOT users need this. See "Docker and BBOT" in the README.
- `onyxweb URL --json` prints the snapshot: every key it printed before, plus headers, metadata, console messages, script results and the anti-bot verdict. With `-o PATH` it writes there instead of stdout.
- A `RenderResult` built by hand parses the html it holds, so `.dom` and the buckets work on it instead of raising.

### Removed
- The private `Client._render` helper, which no code called.

### Fixed
- The package docstring example read `result.html.title`, a `str` method. It now reads `result.title`.
- The shell engine's `--no-sandbox` reached Chrome as `----no-sandbox`, which Chrome ignores, so the default engine failed to launch in Docker with no working switch except `chrome_args=["no-sandbox"]`. `sandbox=False` now works on both engines.
- A launch that fails while the sandbox is on now says how to fix it, instead of `CDP: Input/Output error while resolving websocket URL`.
- `ONYXWEB_CHROME__*` environment variables apply when the caller also passes another chrome option such as `engine="full"`. Before, that option dropped every environment value of its section.
- A tab that fails to recreate no longer costs the pool a slot. Before, the next fetch panicked with `semaphore permitted but pool is empty`, a `BaseException` that `except Exception` missed.
- `Client.close()` collects the exited Chrome instead of leaving a zombie until the `Client` is freed.

## [0.2.3] - 2026-09-18

### Added
- Page buckets: `scripts`, `styles`, `links`, `images`, `iframes`, `forms`, `meta`, `comments` and `json_ld`. Each is lazy and Rust-backed.
- `r.content` (inline) and `r.resources` (external) views, plus `r.resources.all()` for everything the browser fetches.
- `r.overview()`, `r.search()`, `Bucket.search()`, `Bucket.matches()` and `Bucket.text()`. Search runs in Rust with linear-time regex.
- `RenderResult.text` and `RenderResult.title`.
- `hash_navigation` (`"reload"` by default, or `"continue"`): what a fetch does when its URL differs from the tab's page only after `#`.
- `onyxweb_wrapper`, a small binary bundled in each wheel. It stops Chrome's whole process tree when the process holding a `Client` dies abruptly.
  Linux uses `PR_SET_PDEATHSIG`, macOS uses `kqueue`, and Windows uses a kill-on-close Job Object.

### Changed
- **Breaking:** `RenderResult` is no longer a `str`. Pass `r.html` to `re`, lxml or BeautifulSoup. `str(r)`, `"x" in r` and `len(r)` still work.
- **Breaking:** `Dom` keeps only CSS selection (`query`, `query_one`, `select`, `select_one`, `find`, `find_all`, `count`, `exists`).
- Wheels now need maturin 1.15 or later to build. The wrapper is bundled through maturin's `include`, with no post-build step.
- The test suite is organised into 12 contracts, one file each.

### Removed
- **Breaking:** `Dom.text()`, `Dom.html()`, `Dom.title()`, `Dom.links()`, `Dom.images()`, `Dom.contains()` and `Dom.find_all_text()`.
  Use `r.text`, `r.html`, `r.title`, `r.links`, `r.images`, `"x" in r`, `Bucket.search()` and `len(r)`.

### Fixed
- Chrome no longer survives an abrupt kill of the process that launched it.
- `Client.close()` returns in about 10 ms instead of waiting a fixed 3 s, and stops Chrome.
- An invalid URL fails immediately instead of waiting out the navigation timeout.
- A subframe failure or response no longer speaks for the main page; anti-bot detection is more precise.
- Pages of 15 KB to 30 KB no longer wait out the full anti-bot challenge window.
- `launch_timeout_ms` and `screenshot_timeout_ms` now take effect.
- `user_agent_metadata` reaches the wire without an explicit `user_agent`.
- `locale` sets `navigator.language`, `navigator.languages` and `Accept-Language` together.
- `Element.attr()` and `Element.attrs` return an element's own attributes, including on `<html>` and `<body>`.
- A capture on `wait_until="domcontentloaded"` no longer returns an empty page after a previous fetch on the same tab.
- Stealth presets: `navigator.webdriver` reads `false`, the WebGL vendor uses Chrome's dialect, the canvas fingerprint stays stable, and the client-hint brand matches the browser.
- `onyxweb --install` keeps the bundled wrapper.
- A Chrome found only on `PATH` is resolved on Windows.

[Unreleased]: https://github.com/ausmaster/onyxweb/compare/v0.2.3...HEAD
[0.2.3]: https://github.com/ausmaster/onyxweb/compare/v0.2.2...v0.2.3
