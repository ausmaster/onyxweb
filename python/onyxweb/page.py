r"""``onyxweb page`` — query a saved snapshot offline, with no Chrome and no network.

Write a snapshot with ``onyxweb <URL> --json -o page.json`` (or ``RenderResult.save``),
then look before you read::

  onyxweb page overview page.json                     # what the page holds
  onyxweb page search page.json apiKey                # matches in every bucket
  onyxweb page search page.json '"key":"(\w+)"' --bucket scripts --regex
  onyxweb page text page.json scripts 1               # one record, whole

Exit codes: 0 success, 1 bad argument or unreadable snapshot.
"""

from __future__ import annotations

import argparse
import sys
from typing import NoReturn

from onyxweb import RenderResult
from onyxweb.records import PAGE_BUCKETS


class _Parser(argparse.ArgumentParser):
    """Argument parser whose usage errors exit 1, as the module docstring promises."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(1, f"{self.prog}: error: {message}\n")


def _build_parser() -> argparse.ArgumentParser:
    p = _Parser(prog="python -m onyxweb page", description="Query a saved page snapshot offline.")
    commands = p.add_subparsers(dest="command", required=True, metavar="command")

    overview = commands.add_parser("overview", help="count and size of every bucket")
    overview.add_argument("snapshot", help="file from --json -o or RenderResult.save")

    search = commands.add_parser("search", help="show where a query matches")
    search.add_argument("snapshot", help="file from --json -o or RenderResult.save")
    search.add_argument("query", help="text to find, or a pattern with --regex")
    search.add_argument("--bucket", choices=PAGE_BUCKETS, help="search one bucket only")
    search.add_argument("--field", help="match only inside this record field, e.g. url")
    search.add_argument("--regex", action="store_true", help="treat the query as a pattern")
    search.add_argument("--case-sensitive", action="store_true", help="match letter case exactly")

    text = commands.add_parser("text", help="print one record's whole content")
    text.add_argument("snapshot", help="file from --json -o or RenderResult.save")
    text.add_argument("bucket", choices=PAGE_BUCKETS)
    text.add_argument("index", type=int, help="the # column of a table, or a match's index")
    return p


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``onyxweb page``, reached through ``onyxweb.__main__.main``.

    Returns the process exit code: 0 success, 1 bad argument or unreadable snapshot.
    """
    p = _build_parser()
    args = p.parse_args(argv)
    try:
        page = RenderResult.load(args.snapshot)
    except (OSError, ValueError) as e:
        p.error(str(e))

    if args.command == "overview":
        page.overview(prnt=True)
        return 0

    if args.command == "text":
        bucket = getattr(page, args.bucket)
        if not 0 <= args.index < len(bucket):
            p.error(f"{args.bucket} has {len(bucket)} records; index {args.index} is out of range")
        bucket.text(args.index, prnt=True)
        return 0

    options = {
        "field": args.field,
        "case_sensitive": args.case_sensitive,
        "regex": args.regex,
    }
    try:
        hits = (
            {args.bucket: getattr(page, args.bucket)}
            if args.bucket
            else page.search(args.query, **options)
        )
        for bucket in hits.values():
            bucket.matches(args.query, prnt=True, **options)
    except ValueError as ve:
        p.error(str(ve))
    if not hits:
        print(f"No matches for {args.query!r}.")
    return 0

