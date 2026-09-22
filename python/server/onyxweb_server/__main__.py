"""``onyxweb-server`` — serve onyxweb's browser to agents and programs. Commands: ``mcp``, ``http``.

  onyxweb-server mcp                  # MCP over stdio, for Claude Code and other agents
  onyxweb-server http --port 8000     # HTTP, for programs; loopback unless a token is set

Exit codes: 0 after a clean stop, 1 for a bad argument or a missing extra.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import sys
from typing import NoReturn


class _Parser(argparse.ArgumentParser):
    """Argument parser whose usage errors exit 1, as `onyxweb` does."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(1, f"{self.prog}: error: {message}\n")


def _build_parser() -> argparse.ArgumentParser:
    p = _Parser(prog="onyxweb-server", description="Serve onyxweb's browser to agents.")
    commands = p.add_subparsers(dest="command", required=True, metavar="command")
    commands.add_parser(
        "mcp",
        help="serve MCP over stdio",
        description=(
            "Serve onyxweb to an agent over MCP (stdio): fetch a page, then look, find and "
            "read it in pieces. Register it with: claude mcp add onyxweb -- onyxweb-server mcp"
        ),
        epilog="ONYXWEB_SERVER_MAX_PAGES (default 50) sets how many fetched pages it keeps.",
    )
    http = commands.add_parser(
        "http",
        help="serve HTTP",
        description=(
            "Serve onyxweb over HTTP: POST /fetch, /screenshot, /fetch_all and /batch, "
            "and GET /health for a status."
        ),
        epilog=(
            "Set ONYXWEB_SERVER_TOKEN to require 'Authorization: Bearer <token>' on every route "
            "but /health. Without a token only a loopback address is served."
        ),
    )
    http.add_argument("--host", default="127.0.0.1", help="address to bind (default: 127.0.0.1)")
    http.add_argument("--port", type=int, default=8000, help="port to listen on (default: 8000)")
    return p


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``onyxweb-server``; returns the process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "mcp":
        try:
            from onyxweb_server.mcp import build_server
        except ImportError as ie:  # the extra isn't installed; the message names it
            sys.stderr.write(f"onyxweb-server: {ie}\n")
            return 1
        # Nothing but the protocol may reach stdout until the client leaves.
        build_server().run()
    else:
        try:
            import uvicorn

            from onyxweb_server.http import TOKEN_VAR, build_app
        except ImportError as ie:  # the extra isn't installed; the message names it
            sys.stderr.write(f"onyxweb-server: {ie}\n")
            return 1
        try:
            loopback = ipaddress.ip_address(args.host).is_loopback
        except ValueError:  # a name: only `localhost` is known to be local
            loopback = args.host == "localhost"
        if not loopback and not os.environ.get(TOKEN_VAR):
            parser.error(
                f"--host {args.host} is not a loopback address, so a token is required; "
                f"set {TOKEN_VAR}, or bind 127.0.0.1 behind a reverse proxy."
            )
        uvicorn.run(build_app(), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
