# onyxweb-server

Serves [onyxweb](https://github.com/ausmaster/onyxweb)'s browser to agents over MCP and to programs over HTTP.

Requires Python 3.11+ and the browser from `onyxweb --install`. Running in Docker, or as BBOT does, needs `sandbox=False`: see "Docker and BBOT" in the [onyxweb README](../README.md).

## HTTP

```bash
uv tool install "onyxweb-server[http]"       # or: pip install "onyxweb-server[http]"
onyxweb-server http --port 8000              # binds 127.0.0.1; there is no authentication
```

| Route | Returns |
|---|---|
| `POST /fetch` | the whole page as `RenderResult.snapshot()` JSON; `RenderResult.load` rebuilds it |
| `GET /health` | `{"status": "ok", "engines": {"shell": true}}`: each engine built so far and whether its Chrome is alive |

The body of `POST /fetch` is `{"url": ..., "engine": "shell" | "full", "wait_ms": 0}`. Send `Accept-Encoding: zstd` to get the snapshot compressed (about 9x on a 7 MB page). `scripts`, `post_load_scripts` and `actions` are refused by name with a 400, because the server never runs caller-supplied JavaScript, and a private, loopback or link-local address is refused too. The server holds nothing between requests.

A failure is `{"error": {"kind": ..., "message": ..., "url": ...}}` with a status from the kind: 400 for `invalid_url`, `invalid_config`, `post_load_script` and a refused request; 502 for `cdp` and `io`; 503 for `chrome_exited`, `queue_timeout`, `launch_failed` and `chrome_not_found`; 504 for `navigation_timeout` and `timeout`; 500 for `internal`. Put a reverse proxy in front before exposing it beyond loopback.

## MCP

`onyxweb-server mcp` lets an agent such as Claude Code fetch a page once, then look, find and read it in pieces, so a large page never floods its context. Pages stay in memory for the session.

```bash
uv tool install "onyxweb-server[mcp]"   # or: pip install "onyxweb-server[mcp]"
claude mcp add onyxweb -- onyxweb-server mcp    # register it, then restart the session
```

| Tool | Returns |
|---|---|
| `fetch` | an id, the final URL, status, title and an overview of the page |
| `pages` | every page fetched this session, newest first |
| `overview` | the count and size of every bucket |
| `query` | several questions at once: each gets the best-matching passages of the page text; omit `id` to search all pages |
| `find` | where an exact string or a regex matches, in the page text and every bucket; omit `id` to search all pages |
| `read` | one record's whole content, in pieces of at most 6,000 characters |
| `page_text` | what the page displays, in pieces of at most 6,000 characters |

Fetch a page, then ask everything you need in one `query` call. Ranking counts word overlap, so it finds passages that use your words and misses ones that only mean the same thing, and it puts navigation, link lists and bare headings below prose ([Boilerpipe](https://dl.acm.org/doi/10.1145/1718487.1718542)'s link-density and word-count features). Use `find` for an exact string, a regex or a bucket. Every tool caps its output and names the `offset` to continue from. A page that comes back nearly empty gets a note suggesting `engine="full"` or a longer `wait_ms`.

Two things are refused outright, with no setting to turn them on. The tools accept no `scripts`, `post_load_scripts` or `actions`. `fetch` refuses any URL whose host resolves to a private, loopback, link-local or otherwise non-public address. A public URL that redirects to a private one is not caught, so run it where private ranges are unreachable. It speaks stdio only. `ONYXWEB_SERVER_MAX_PAGES` (default 50) sets how many pages it keeps.
