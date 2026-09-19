# Changelog

All notable changes to onyxweb. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Releases before this file are listed under [GitHub tags](https://github.com/ausmaster/onyxweb/tags).

## [Unreleased]

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

[Unreleased]: https://github.com/ausmaster/onyxweb/compare/v0.2.2...HEAD
