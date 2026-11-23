# Changelog

All notable changes to onyxweb. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Releases before this file are listed under [GitHub tags](https://github.com/ausmaster/onyxweb/tags).

## [Unreleased]

## [0.3.1] - 2026-11-22

### Added
- `RenderResult.main_text`: the page's main content as text, with navigation, headers, footers,
  asides and forms left out. The content is the block holding the most non-link text — a `<main>`,
  `article` or `role="main"` element when the page marks one, otherwise the densest block, followed
  into the single child that holds nearly all of it — and the furniture around it is dropped. The
  same HTML always yields the same text, so it can be stored and compared against.
- `RenderResult.published` and `Published(date, source)`: the date a page states it was published as
  `YYYY-MM-DD`, with the claim it came from (`"json_ld"`, `"meta"` or `"time"`). One fixed order
  decides between competing claims, so a page stating several always reports the same one, and a
  page that states none is `None`.
- Python 3.15 support: onyxweb builds and runs on CPython 3.15, and the Linux release wheels now cover Python 3.14 and 3.15 (0.3.0 shipped Linux wheels for 3.11 to 3.13 only).
- `onyxweb.error_kinds()` lists every failure `kind` a fetch can carry, so a caller can map each cause to its own policy and notice a release that adds one.

### Changed
- `bypass_anti_bot` now presses the control a challenge is gating on, instead of only waiting.
  Some interstitials never navigate by themselves: they hold a control closed until their own
  check finishes, and lead on only when it is pressed. The control is known by exactly that — the
  page held it closed and then opened it — so it is found whatever it is called and whatever
  language it is labelled in, it is pressed once, and no other control on the page is touched.
- A challenge that neither moves nor holds a control closed is now reported as it stands within a
  couple of seconds rather than after the whole wait. A rate-limit page or an interactive captcha
  cannot pass by waiting, and the verdict is worth more to a caller than the delay.
- A challenge that gates a page gets up to 20 s to resolve (was 12 s), which is the room its own
  delay needs before its control opens.
- Google's own waiting room ("Verifying your request") is now detected as a challenge, with vendor
  `google`; it used to come back as an ordinary page with no verdict at all, so a caller could
  store a waiting room as if it were the result.
- onyxweb is built against PyO3 0.30 instead of 0.25, which cannot build for Python 3.15.
- A wrong-typed argument that reaches the compiled extension, such as `r.dom.query(1)`, still raises `TypeError`, but the argument's name now sits in the exception's note (`while processing 'selector'`) instead of opening its message (`argument 'selector': ...`).
- Calling a class that has no constructor, such as `onyxweb.Element()`, still raises `TypeError`, now worded `cannot create 'builtins.Element' instances` instead of `No constructor defined for Element`.
- `ignore_https_errors` defaults to `True`, which states what onyxweb has always done: Chromium's own client accepts any certificate. Setting it to `False` now really validates, where it previously had no effect.

### Fixed
- A navigation the browser refused now says which refusal it was instead of landing in one `cdp` bucket: `dns_failure`, `connection_refused`, `tls_failure` and `redirect_loop` each carry their own cause, and an error status with no body is `http_status` with the origin's code on `.status_code`. Only a cause onyxweb does not recognise stays `cdp`. Classification happens in one place (`error.rs`), so both routes a navigation failure can take agree.
- A page that replaces itself just as it is read, such as a redirect page whose script navigates on a timer, is now captured whole: the fetch returns the page it landed on, read once that page has loaded, with the URL, status, headers and HTML all from that one document. When the replacement fell on the capture point, it used to fail with `CDP: Error -32000: Cannot find context with specified id` or come back empty from the blank document in between.
- On the shell engine, `chrome_args` written with their leading `--` reach Chrome. The launcher adds its own `--`, so such a flag arrived as `----x`, which Chrome ignores, and did nothing; the engine's own list of 29 flags was inert the same way. That list never took effect, so it is removed rather than switched on, and the engine launches exactly as it always ran.
- A flag the engine sets and a caller's `chrome_args` also sets, such as `--lang`, now takes the caller's value on every launch. Both reached Chrome before, in an order that varied between launches, so the caller's value won on some launches and lost on others. A caller's `disable-features` (and the other feature lists) joins the engine's on both engines instead of replacing it.
- A client freed without `close()` is closed the way `close()` closes it, and the temporary Chrome profile it made is deleted. The profile used to stay in the temp dir for good, even after the process exited, a few megabytes per client.

## [0.3.0] - 2026-09-22

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
- `onyxweb.testing` with `FakeClient` and `FakeClientFactory`: an `AsyncClient` stand-in that serves canned pages for `fetch`, `screenshot`, `fetch_all` and `batch`, records its calls and can be told to fail or die, so code that fetches pages tests without Chrome.

### Changed
- `onyxweb --install` and `onyxweb-download-chrome` fetch both Chrome builds, the headless shell and full Chrome, so the server's default engine needs no second command. It is about 380 MB more; `--engine shell` fetches the shell alone, and `ensure_chrome()` still fetches one engine.
- `onyxweb --install --force` downloads the pinned Chrome again, as `onyxweb-download-chrome --force` did; `onyxweb-download-chrome` keeps `--all`, `--platform` and `--dest`.
- **Breaking:** `RenderResult.text` and `Element.text` now read as the page displays them. Block elements break lines, a table row stays on one line with tabs between cells, `<pre>` keeps its spacing, and runs of whitespace collapse elsewhere. Previously adjacent blocks ran together, so `<div>Alice</div><div>30</div>` read as `Alice30`. Response hashes are unaffected: they cover the HTML, not the text.
- **Breaking:** onyxweb requires Python 3.11 or later. The 0.2.3 wheels for Python 3.10 fail on import.
- **Breaking:** Chrome runs with its sandbox by default on the full engine, which always passed `--no-sandbox` before. Where the sandbox cannot start (root, Docker's default container profile, restricted user namespaces), pass `sandbox=False` or set `ONYXWEB_CHROME__SANDBOX=false`. Docker and BBOT users need this. See "Docker and BBOT" in the README.
- `onyxweb URL --json` prints the snapshot: every key it printed before, plus headers, metadata, console messages, script results and the anti-bot verdict. With `-o PATH` it writes there instead of stdout.
- A `RenderResult` built by hand parses the html it holds, so `.dom` and the buckets work on it instead of raising.

### Removed
- The private `Client._render` helper, which no code called.

### Fixed
- `Client(engine="full")` no longer hangs at launch on Windows. It read Chrome's version from `chrome.exe --version`, which Windows Chrome ignores: it starts the browser instead and never exits. The version is now read from the files beside `chrome.exe`.
- The full engine installs and launches on macOS. `onyxweb --install --engine full` failed there with `No such file or directory: .../full/chrome`, because Chrome for Testing ships the macOS build as an app bundle and the installer and the resolver both looked for a flat `chrome`.
- The full engine's own User-Agent names the real host OS on Windows and macOS instead of always claiming `X11; Linux x86_64`, which contradicted the browser's own `Sec-CH-UA-Platform` on every request.
- An upgrade that pins a newer Chrome now replaces the installed build on the next `onyxweb --install` or `ensure_chrome()`. Before, an installed browser was kept whatever its version, until `onyxweb-download-chrome --force`. An install made before this release records no version, so it is replaced once.
- A full-engine Chrome no longer grows with every fetch. It kept each page a tab had left in its back/forward cache, in a renderer process of its own, so one tab went from 5 to 15 renderer processes and 2.0 GB after 60 fetches. Four tabs after 40 fetches now hold about 1.0 GB, down from 1.9 GB. A `disable-features` in `chrome_args` is merged with the engine's own list.
- The source distribution carried the test suite, the `onyxweb-server` package, the CI files and `uv.lock`, and the Python package twice. It now holds the Rust crate and the Python package once, 36 files instead of 92.
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

[Unreleased]: https://github.com/ausmaster/onyxweb/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/ausmaster/onyxweb/compare/v0.2.3...v0.3.0
[0.2.3]: https://github.com/ausmaster/onyxweb/compare/v0.2.2...v0.2.3
