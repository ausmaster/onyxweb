# onyxweb-server

Serves [onyxweb](https://github.com/ausmaster/onyxweb)'s browser to agents over MCP and to programs over HTTP.

Requires Python 3.11+ and the browser from `onyxweb --install`. Running in Docker, or as BBOT does, needs `sandbox=False`: see "Docker and BBOT" in the [onyxweb README](../../README.md).

## HTTP

```bash
uv tool install "onyxweb-server[http]"       # or: pip install "onyxweb-server[http]"
onyxweb-server http --port 8000              # binds 127.0.0.1
```

| Route | Returns |
|---|---|
| `POST /fetch` | the whole page as `RenderResult.snapshot()` JSON; `RenderResult.load` rebuilds it |
| `POST /screenshot` | the image itself, as `image/png`, `image/jpeg` or `image/webp` |
| `POST /fetch_all` | `{"snapshot": {...}, "image": "<base64>", "format": "png"}`: the page and its image from one visit |
| `POST /batch` | NDJSON in the order given, one line per URL: `{"url", "snapshot"}`, or `{"url", "error"}` for a URL that failed |
| `GET /health` | `{"status": "ok", "engines": {"shell": true}, "stats": {...}}`: each engine built so far and whether its Chrome is alive, plus counters (requests, failures by kind, retries, restarts, pages held, egress refusals) |

Every body is a JSON object sent with `Content-Type: application/json`. A route accepts the fields below and refuses any other by name:

| Route | Fields |
|---|---|
| `/fetch` | `url`, `engine`, `wait_ms`, `timeout_ms`, `wait_until`, `headers`, `block_urls`, `bypass_anti_bot` |
| `/screenshot` | `url`, `engine`, `wait_ms`, `timeout_ms`, `wait_until`, `headers`, `full_page`, `format`, `quality`, `viewport` |
| `/fetch_all` | the `/fetch` fields, plus `full_page`, `format` and `quality` |
| `/batch` | `urls` in place of `url`, and the other `/fetch` fields, applied to every URL |

Send `Accept-Encoding: zstd` to get JSON compressed (about 9x on a 7 MB page). A batch is compressed as it is written, and an image never is. `scripts`, `post_load_scripts` and `actions` are refused with a 400, because the server never runs caller-supplied JavaScript, and so is a private, loopback or link-local address. The server holds nothing between requests.

A failure is `{"error": {"kind": ..., "message": ..., "url": ...}}` with a status from the kind:

| Status | Kinds |
|---|---|
| 400 | `invalid_url`, `invalid_config`, `post_load_script`, `invalid_request` (a refused option or URL), `refused_field` |
| 401 | `unauthorized` |
| 413 | `too_large` |
| 422 | `invalid_request` (a body that is not a JSON object, or a missing or unknown field) |
| 502 | `cdp`, `io` |
| 503 | `chrome_exited`, `queue_timeout`, `launch_failed`, `chrome_not_found` |
| 504 | `navigation_timeout`, `timeout` |
| 500 | `internal`, and any kind not listed |

One bad URL never fails a batch. Its line carries the same `error` object, with the same `kind`, and the status stays 200. A batch that is itself refused, with 0 URLs or more than `MAX_BATCH`, is a 400.

Set `ONYXWEB_SERVER_TOKEN` to require `Authorization: Bearer <token>` on every route but `/health`. Without a token, `onyxweb-server http` listens on loopback only and exits 1 for any other `--host`. The server has no other authentication, so put a reverse proxy in front before exposing it.

## Limits and egress

Both front-ends share one core, so they refuse the same things. `ONYXWEB_SERVER_*` variables set the limits:

| Variable | Default | Sets |
|---|---:|---|
| `MAX_PAGES` | 50 | pages held per session |
| `MAX_STORE_BYTES` | 268435456 | bytes of html held; the least recently used page goes first |
| `MAX_PAGE_BYTES` | 20971520 | largest page or image one call may return |
| `MAX_BATCH` | 50 | URLs in one batch |
| `MAX_WAIT_MS` | 30000 | longest settle after the page loads |
| `MAX_TIMEOUT_MS` | 60000 | longest navigation budget |
| `QUEUE_MS` | 10000 | longest a request waits for a free tab, then `queue_timeout` |
| `CONCURRENCY` | 4 | tabs per engine |
| `EGRESS` | 1 | `0` turns the egress proxy off |

A caller may set only `engine`, `wait_ms`, `timeout_ms`, `wait_until`, extra headers, `block_urls` and `bypass_anti_bot`, each within a ceiling. A Chrome that dies mid-call is replaced and the call retried once.

The browser reaches the network through a proxy the server runs on 127.0.0.1. It resolves every host itself, refuses the request unless every answer is a public address, and connects to the address it checked. So a redirect, a rebinding host, or a page's own script cannot reach a private, loopback or link-local address. A refused navigation is refused like a private URL: a 400 over HTTP, a tool error over MCP. A screenshot of a refused plain-HTTP page shows the proxy's refusal text, because an image carries no status. The proxy adds no authentication of its own: it listens on loopback only, and it can reach only public addresses.

## MCP

`onyxweb-server mcp` lets an agent such as Claude Code fetch a page once, then look, find and read it in pieces, so a large page never floods its context. Pages stay in memory for the session.

```bash
uv tool install "onyxweb-server[mcp]"   # or: pip install "onyxweb-server[mcp]"
claude mcp add onyxweb -- onyxweb-server mcp    # register it, then restart the session
```

| Tool | Returns |
|---|---|
| `fetch` | an id, the final URL, status, title and an overview of the page; with `screenshot=true`, also an image of it from the same visit |
| `batch` | many URLs at once: an id per page in the order given, and a `FAILED` line for each URL that failed |
| `screenshot` | an image of the page, taking `full_page`, `format`, `quality` and `viewport` |
| `pages` | every page fetched this session, newest first |
| `overview` | the count and size of every bucket |
| `query` | several questions at once: each gets the best-matching passages of the page text; omit `id` to search all pages |
| `find` | where an exact string or a regex matches, in the page text and every bucket; omit `id` to search all pages |
| `read` | one record's whole content, in pieces of at most 6,000 characters |
| `page_text` | what the page displays, in pieces of at most 6,000 characters |

Fetch a page, then ask everything you need in one `query` call. Ranking counts word overlap, so it finds passages that use your words and misses ones that only mean the same thing, and it puts navigation, link lists and bare headings below prose ([Boilerpipe](https://dl.acm.org/doi/10.1145/1718487.1718542)'s link-density and word-count features). Use `find` for an exact string, a regex or a bucket. Every tool caps its output and names the `offset` to continue from. Each marks page text as untrusted, and puts that label above the page's title and URL, which the page chose. A page that comes back nearly empty gets a note suggesting `engine="full"` or a longer `wait_ms`.

`fetch` and `batch` take `engine`, `wait_ms`, `timeout_ms`, `wait_until`, `headers`, `block_urls` and `bypass_anti_bot`. `screenshot` takes the same except the last 2. The server checks each against its `ONYXWEB_SERVER_*` limits, and a value outside one fails with the limit named. Headers show in the conversation, so an agent sends only what the user gave it. An image cannot be cut, so `screenshot` refuses one over 5 MB and `fetch` leaves it out with a note. Ask for `jpeg` or a smaller `viewport`.

Two things are refused outright, with no setting to turn them on. The tools accept no `scripts`, `post_load_scripts` or `actions`. `fetch`, `batch` and `screenshot` refuse any URL whose host resolves to a private, loopback, link-local or otherwise non-public address, and the egress proxy above stops a redirect or a page's own request from reaching one. It speaks stdio only. `ONYXWEB_SERVER_MAX_PAGES` (default 50) sets how many pages it keeps.
