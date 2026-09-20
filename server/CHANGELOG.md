# Changelog

All notable changes to onyxweb-server. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- `onyxweb-server mcp`, an MCP server that lets an agent fetch a page, ask several questions of it in one `query` call, then find and read the details. Install it with `pip install "onyxweb-server[mcp]"`.
  Pages stay in memory for the session, and every tool caps its output. It runs no caller-supplied scripts and never fetches a private, loopback or link-local address.
- `onyxweb_server.core.ServerCore`, the part every front-end shares: a guarded, stateless `fetch`, held pages, and a browser client per engine that is rebuilt after its Chrome dies. `ONYXWEB_SERVER_MAX_PAGES` (default 50) sets how many pages it holds.
