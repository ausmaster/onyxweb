# Changelog

All notable changes to onyxweb-server. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.1.3] - 2026-11-24

### Changed
- A session keeps its tab through a fetch that fails, so a run of requests is not moved onto a
  fresh tab — with none of what its pages stored — by one bad page. The tab is reset to a blank
  page and its browser context is untouched; a session is still dropped when its browser is gone.
- `onyxweb-server` configures its own `onyxweb_server` logger, with the same timestamped line on
  stderr as before. onyxweb now configures only its own logger, so an application does this for
  itself; nothing here touches the root logger.

### Added
- Sessions: `session` on `POST /fetch` and `POST /batch` names an id of the caller's own, and
  every request naming it runs on one tab, so a page and the pages that follow from it share what
  the first left behind — a cookie, a `localStorage` entry. That is what a link whose next step
  depends on the page above it needs: search engines hand out redirect tokens that resolve only in
  the tab that rendered the results, which is why following them from a pool of tabs failed. A
  session leases one tab for as long as it lives, so it costs one of
  `ONYXWEB_SERVER_CONCURRENCY`; the tabs it does not hold serve sessionless requests as before.
  `POST /batch` with a session runs its URLs in turn on that one tab, not across the pool.
- `POST /session/close` with `{"session": <id>}` gives the tab back: `{"closed": true}`, or
  `{"closed": false}` when no such session was open, which is not an error. A run of searches can
  hold one session per search and close each as it ends.
- `ONYXWEB_SERVER_MAX_SESSIONS` (default 16) and `ONYXWEB_SERVER_SESSION_TTL_MS` (default 120000,
  counted from the session's last use) bound the tabs sessions may hold. The server closes a
  session that idles past the TTL by itself, no later than a quarter of the TTL (5 s at most) after
  it runs out, so a caller that goes away without closing cannot keep a tab.
- A session ends with the browser its tab lived in. Its next call is answered once with
  `chrome_exited` (503) and is not retried on a fresh tab, which would hold none of what the
  session's pages stored and could answer a page that looks right and is not; the id then opens a
  new session. In a `/batch`, each URL left is answered with that failure under its own URL. Such
  a session holds no tab, so it stops counting against the cap and in `/health` at once.
- `refused_session`: 503 when the cap is reached, because the server's state is the reason; the
  message names the way out. An id that is empty or over 200 characters is `RefusedOption`, 400,
  because that is the caller's error rather than a full server.
- `stats()` reports `sessions` and `max_sessions`, so `/health` shows them.

### Changed
- `ServerCore.fetch` and `ServerCore.batch` take `session: str | None = None`. Absent, nothing
  changes for a caller that does not use it.
- Sessions are the HTTP front-end's: the MCP tools take no session, since nothing that calls them
  needs two visits to share a tab.
- The release depends on `onyxweb>=0.3.2`, the first library with `session()`. The metadata keeps
  `onyxweb>=0.3.1` until that release exists, so a development build installs beside a 0.3.2.dev
  wheel, which PEP 440 orders below 0.3.2.

## [0.1.2] - 2026-11-23

### Added
- `select` on `POST /fetch` and `POST /batch`: a list of view names answers with just those views
  of the page instead of its snapshot, as `{"onyxweb_page": 1, ...}` (a batch line as
  `{"url", "page"}`). The names are the page's own views — `title`, `published` (the date the page
  states, and the claim it came from), `main_text` (the content block, without navigation,
  headers, footers or forms), `text`, `html`, `elapsed_s`, `errors`, `console_messages`,
  `metadata`, `headers`, and each bucket's records — encoded as the snapshot encodes them.
  `final_url`, `status_code` and `anti_bot` come with every selection, so a challenge page always
  arrives with its verdict. Only the views named are computed, the keys come in one fixed order so
  the same names over the same HTML give the same bytes, and `ONYXWEB_SERVER_MAX_PAGE_BYTES`
  measures the selection rather than the page. A name that is no view is refused with the list of
  those that are, and a view the library gains is one row in `core.VIEWS`.

### Changed
- `CoreConfig.from_env` takes its optional mapping as `env` instead of `environ`; a caller passing it by keyword must rename the argument.
- The package metadata lists Python 3.14 and 3.15 as supported, which needs an onyxweb release that ships 3.15 wheels: 0.3.0 does not, 0.3.1 does. The dependency is `onyxweb>=0.3.1` for that reason, so a 0.3.1 wheel must be on PyPI before this package is installed.
- A failure the origin or the network caused now answers 502 with its own kind — `http_status` (plus the origin's code as `error.status`), `dns_failure`, `connection_refused`, `tls_failure` or `redirect_loop` — instead of one `cdp` bucket, so a caller can tell a refusing host from a broken browser; a batch carries the same on its line.

### Fixed
- Behind the egress guard, a page's WebRTC can no longer reach a private address. The guard is a proxy, and a proxy carries only TCP, so the STUN datagrams a page sends went around it to any host it named, on both engines. The browser is now told to send no UDP it cannot proxy.

## [0.1.1] - 2026-09-23

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

[Unreleased]: https://github.com/ausmaster/onyxweb/compare/server-v0.1.3...HEAD
[0.1.3]: https://github.com/ausmaster/onyxweb/compare/server-v0.1.2...server-v0.1.3
[0.1.2]: https://github.com/ausmaster/onyxweb/compare/server-v0.1.1...server-v0.1.2
[0.1.1]: https://github.com/ausmaster/onyxweb/compare/server-v0.1.0...server-v0.1.1
[0.1.0]: https://github.com/ausmaster/onyxweb/releases/tag/server-v0.1.0
