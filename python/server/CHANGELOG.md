# Changelog

All notable changes to onyxweb-server. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Fixed
- The MCP server's instructions fit in the 2048 characters Claude Code shows of them, with the warning that page content is untrusted data first. The 0.1.0 text was 2349 characters, so the client cut that warning off.

### Changed
- The MCP instructions are 959 characters, down from 2349, and carry only what decides between tools; an agent reads them before any tool description loads. Option details live in the description of the tool that takes them, how the page text is derived moved to `page_text`, how `query` ranks moved to `query`, and tool descriptions no longer carry docstring indentation (591 characters over the nine tools). `fetch`'s "Next" line routes by need: `query` for questions, `page_text` for the page in order.
- Measured in headless sessions on 19 tasks: Sonnet 5 picks the right tool in 60 of 63 runs and falls back from a blocked WebFetch in the other 3, where the 1333-character draft launched a browser for 3 plain pages; Opus 5 picks it in 19 of 19; every answer is correct. The instructions also tell an agent given a URL to read the page rather than answer from memory, which Sonnet 5 did in 6 of 9 runs without that line.

## [0.1.0] - 2026-09-22

### Changed
- A JSON body sent to an HTTP route without `Content-Type: application/json` is a 422 that names the fix, where `POST /fetch` read it anyway.
- The default engine is `full`, a real Chrome, and the server's own clients wait out bot-check pages unless a call passes `bypass_anti_bot: false`. On 22 popular sites the full engine passed 18 and the shell 10. `engine="shell"` is lighter and faster; `onyxweb --install` fetches both.

### Added
- The MCP server offers `screenshot` and `batch`, and `fetch` takes every option the core allows: `timeout_ms`, `wait_until`, `headers`, `block_urls`, `bypass_anti_bot`, and `screenshot` for an image from the same visit.
- An MCP image over 5 MB is refused by `screenshot` and left out of `fetch` with a note, since an image cannot be cut. `batch` fetches up to 50 URLs, holds each page and lists an id per URL, with a failure in its place.
- The core owns every policy, so MCP and HTTP refuse the same things: `FetchOptions` and `ShotOptions` allow only `engine`, `wait_ms`, `timeout_ms`, `wait_until`, headers, `block_urls`, `bypass_anti_bot` and the image options, each within a ceiling; `ServerCore.screenshot`, `fetch_all` and `batch` join `fetch`; every refusal is a `Refused`, whose subclasses `RefusedUrl`, `RefusedOption` and `TooLarge` carry the `code` a front-end maps (`refused_url`, `refused_option`, `too_large`), so a caller can catch one cause or all of them.
- `ONYXWEB_SERVER_*` limits: pages and bytes held, the largest page or image, batch size, wait and timeout ceilings, tabs per engine, and a queue timeout (10 s), so a saturated server answers `queue_timeout` instead of waiting forever.
- An egress proxy on 127.0.0.1 that the browser is pointed at. It refuses any private, loopback or link-local address at connect time, so a redirect, a DNS rebinding host or a page's own script cannot reach one. `ONYXWEB_SERVER_EGRESS=0` turns it off.
- A Chrome that dies mid-call is replaced and the call retried once. `ServerCore.stats()` and `GET /health` report requests, failures by kind, retries, restarts, pages held and egress refusals, and each call logs one line to the `onyxweb_server` logger without its query string.
- `POST /fetch` answers 413 with kind `too_large` for a page over the size limit.
- `POST /screenshot`, `POST /fetch_all` and `POST /batch`. `/screenshot` returns the image itself; `/fetch_all` returns the page and its image as base64 in one JSON object; `/batch` returns one NDJSON line per URL in the order given, with a URL that failed as an `error` line in its place. `/fetch` takes `timeout_ms`, `wait_until`, `headers`, `block_urls` and `bypass_anti_bot`.
- `ONYXWEB_SERVER_TOKEN` requires `Authorization: Bearer <token>` on every HTTP route but `/health`. `onyxweb-server http` exits 1 for a non-loopback `--host` unless it is set.
- `onyxweb-server http`, an HTTP front-end: `POST /fetch` returns the page as a snapshot (zstd when the caller accepts it), `GET /health` reports each engine's Chrome. It holds nothing between requests, refuses `scripts`, `post_load_scripts` and `actions` by name, and binds loopback by default. Install it with `pip install "onyxweb-server[http]"`.
- `onyxweb-server mcp`, an MCP server that lets an agent fetch a page, ask several questions of it in one `query` call, then find and read the details. Install it with `pip install "onyxweb-server[mcp]"`.
  Pages stay in memory for the session, and every tool caps its output. It runs no caller-supplied scripts and never fetches a private, loopback or link-local address. Everything a page wrote, its title and URL included, appears below the untrusted label.
- `onyxweb_server.core.ServerCore`, the part every front-end shares: a guarded, stateless `fetch`, held pages, and a browser client per engine that is rebuilt after its Chrome dies. `ONYXWEB_SERVER_MAX_PAGES` (default 50) sets how many pages it holds.

[Unreleased]: https://github.com/ausmaster/onyxweb/compare/server-v0.1.0...HEAD
[0.1.0]: https://github.com/ausmaster/onyxweb/releases/tag/server-v0.1.0
