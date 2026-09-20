"""``onyxweb-server`` — serve onyxweb's browser to agents. Commands: ``mcp``.

  onyxweb-server mcp            # MCP over stdio, for Claude Code and other agents

Exit codes: 0 after a clean stop, 1 for a bad argument or a missing extra.
"""

from __future__ import annotations

import argparse
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
    return p


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``onyxweb-server``; returns the process exit code."""
    args = _build_parser().parse_args(argv)
    if args.command == "mcp":
        try:
            from onyxweb_server.mcp import build_server
        except ImportError as ie:  # the extra isn't installed; the message names it
            sys.stderr.write(f"onyxweb-server: {ie}\n")
            return 1
        # Nothing but the protocol may reach stdout until the client leaves.
        build_server().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
