"""C13 agent tools — a tool call maps to bounded text that agrees with the page.

``onyxweb_server.mcp`` gives an agent seven tools over pages it fetched: ``fetch``, ``pages``,
``overview``, ``find``, ``query``, ``read`` and ``page_text``. In-process tests call them through
``FastMCP.call_tool`` with the URL guard swapped for a permissive one, since the test server
listens on 127.0.0.1. The guard, the ceilings, the store and the client rules are the shared core
and are tested in C14. The tables:

- ``CALLS``: one call on a fetched page → the fragments it must and must not show, and a
  size cap that holds even on a page far bigger than the cap.
- ``ERRORS``: bad input → a ``ToolError`` that names the fix, and a page that still answers.

One test spawns the server over stdio, the way Claude Code does. The server never offers
``scripts``, ``post_load_scripts`` or ``actions`` to its caller, and never fetches a private,
loopback or link-local address; both are pinned here as absences.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import onyxweb
import pytest
from conftest import BUCKET_PAGE
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from onyxweb_server.mcp import build_server
from pytest_httpserver import HTTPServer

TOOLS = {"fetch", "pages", "overview", "find", "query", "read", "page_text"}
REFUSED_FIELDS = {"scripts", "post_load_scripts", "actions"}
TOOL_CAP = 4000  # characters of body a tool returns; mirrors TOOL_CAP in mcp.py
READ_CAP = 6000  # the same for read and page_text
QUERY_CAP = 8000  # characters query returns for all its questions together
PASSAGE_CAP = 300  # longest passage query shows; mirrors PASSAGE_CHARS in mcp.py
OVERHEAD = 200  # the label and continuation lines around a capped body
UNTRUSTED = "untrusted page content"
PAGE_ID = re.compile(r"\bp[0-9a-f]{10}\b")
NEXT_OFFSET = re.compile(r"offset=(\d+)")


def _page(title: str, body: str) -> str:
    return f"<html><head><title>{title}</title></head><body>{body}</body></html>"


# Every page but ``bucket`` is served by the ``site`` fixture; ``bucket`` is conftest's.
PAGES: dict[str, str] = {
    "alpha": _page("Alpha", "<p>SHARED_MARK ALPHA_ONLY</p>"),
    "beta": _page("Beta", "<p>SHARED_MARK BETA_ONLY</p>"),
    "gamma": _page("Gamma", "<p>SHARED_MARK GAMMA_ONLY</p>"),
    # Far bigger than either cap: a 200 KB script, 100 KB of text and 60 links.
    "big": _page(
        "Big",
        f"<script>var pad='{'x' * 200_000}';</script><p>{'word ' * 20_000}</p>"
        + "".join(f"<a href='l{i}.html'>MANY_LINK</a>" for i in range(60)),
    ),
    # Several topics, one passage each, plus two decoys: a weaker mention of "license" and a
    # passage made of filler words.
    "facts": _page(
        "Facts",
        "<h1>Example Tool</h1>\n"
        "<p>Status: the maintainer took a long break, but development has resumed and the "
        "project is maintained again by a small team.</p>\n"
        "<p>Contributors must sign the license agreement before their first patch is merged.</p>\n"
        "<p>License: the tool is released under the MIT License. The license permits reuse in "
        "commercial work.</p>\n"
        "<p>Installation: run pip install example-tool. It requires Python 3.11 or newer.</p>\n"
        "<p>What is the answer? The answer is what the answer is, and the whole point of the "
        "answer is the answer.</p>",
    ),
    # Five passages full of one common word and one with a single rare word.
    "rare": _page(
        "Rare",
        "\n".join(
            f"<p>Notes {n} on the common workflow: common steps, common tools, common habits, "
            "common checks and more.</p>"
            for n in range(5)
        )
        + "\n<p>A single zebra appeared near the end of this otherwise ordinary paragraph "
        "about the workflow.</p>",
    ),
    # A phrase that begins just before the 300-character cut of a line with no breaks.
    "straddle": _page(
        "Straddle",
        "<p>" + "abcd " * 58 + "alpha bravo charlie delta " + "abcd " * 40 + "</p>",
    ),
    # Code indentation must survive; whitespace is collapsed everywhere else.
    "code": _page(
        "Code",
        "<p>Example follows.</p><pre>def f():\n    return 1\n</pre><p>That is all.</p>",
    ),
    # A nav of link text competing with prose for the same word.
    "navpage": _page(
        "Nav",
        "<nav><a href='/a'>Platform</a> <a href='/b'>Platform Tools</a> "
        "<a href='/c'>Platform Docs</a> <a href='/d'>Platform Status</a></nav>"
        "<p>The platform runs on a small server and needs no configuration to start.</p>"
        "<footer><a href='/x'>Cookies</a> <a href='/y'>Cookie Policy</a></footer>",
    ),
    # Two passages with the same words; only the first carries a link, and its share stays
    # under LINK_HEAVY, so neither may be demoted and the first wins on position.
    "tied": _page(
        "Tied",
        "<p><a href='/z'>Aardvark</a> zebra topics appear in this deliberately long opening "
        "paragraph. Marker ALPHA.</p>"
        "<p>Pangolin zebra topics appear in this deliberately long opening paragraph. "
        "Marker BETA.</p>",
    ),
    # Two passages that are nothing but link text; the better match must still rank first.
    "twonavs": _page(
        "TwoNavs",
        "<nav><a href='/a'>Salvage ALPHA</a></nav>"
        "<p>An ordinary sentence sits between them so they stay separate passages here.</p>"
        "<nav><a href='/b'>Salvage Salvage BETA</a></nav>",
    ),
    # A heading carries the word but not the answer; the paragraph under it does.
    "heading": _page(
        "Heading",
        "<h2>Telemetry</h2>"
        "<p>The telemetry subsystem records timing for every request and writes it to disk "
        "once a minute for later analysis.</p>",
    ),
    # 60 labelled items with enough padding that showing each in context blows the cap.
    "manytools": _page(
        "Many",
        "".join(
            f"<p>tool_{n:02d} Title: does thing {n}. " + "padding words here " * 12 + "</p>"
            for n in range(60)
        ),
    ),
    # Enough distinct values that even a list of them outgrows the cap.
    "manywords": _page("ManyWords", "<p>" + " ".join(f"item{n}" for n in range(2000)) + "</p>"),
    # A menu: every line is a few characters, so a passage is several lines together.
    "menu": _page(
        "Menu",
        "<ul>\n<li>Politics</li>\n<li>Business</li>\n<li>Tech</li>\n<li>Health</li>\n</ul>",
    ),
    # The same text with no line breaks between blocks, as script-built pages produce it.
    "glued": _page(
        "Glued",
        "<div>Status: the maintainer took a long break, but development has resumed.</div>"
        "<div>License: the tool is released under the MIT License.</div>"
        "<div>Installation: run pip install example-tool. It requires Python 3.11 or newer.</div>"
        "<div>" + "Filler sentence about nothing in particular. " * 8 + "</div>",
    ),
    # Real pages are mostly indentation: CNN's text is 85% whitespace.
    "spaced": _page(
        "Spaced",
        "\n\n   <div>\n      <p>FIRST_LINE</p>\n\n\n      <p>SECOND_LINE</p>\n   </div>"
        + " " * 20_000
        + "<p>AFTER_THE_SPACE</p>",
    ),
    "signature": _page(
        "Signature",
        "<p>Path.rglob(pattern, *, case_sensitive=None, recurse_symlinks=False)</p>"
        "<p>Glob the pattern.</p>",
    ),
    # What a bot check leaves behind: a 200 with almost nothing on it.
    "empty": _page("", "<pre>Unknown Error</pre>"),
    # The marker is assembled at runtime, so the script source can't supply it.
    "late": _page(
        "Late",
        "<div id='d'>EARLY_TEXT</div><script>setTimeout(() => {"
        "document.getElementById('d').textContent += ' ' + 'LATE' + '_TEXT'}, 300)</script>",
    ),
}


@pytest.fixture(scope="module")
def shared() -> Iterator[onyxweb.AsyncClient]:
    client = onyxweb.AsyncClient(concurrency=2)
    yield client
    asyncio.run(client.aclose())


def _server(
    shared: onyxweb.AsyncClient, **kwargs: Any
) -> FastMCP:  # a fresh store per test, one shared browser
    kwargs.setdefault("url_guard", lambda url: None)
    return build_server(lambda engine: shared, **kwargs)


@pytest.fixture
def server(shared: onyxweb.AsyncClient) -> FastMCP:
    return _server(shared)


@pytest.fixture
def site(httpserver: HTTPServer, bucket_page: str) -> dict[str, str]:
    """Every page's URL, by name."""
    for name, html in PAGES.items():
        httpserver.expect_request(f"/{name}.html").respond_with_data(html, content_type="text/html")
    return {"bucket": bucket_page, **{n: httpserver.url_for(f"/{n}.html") for n in PAGES}}


async def call(server: FastMCP, tool: str, **args: Any) -> str:
    """Text a tool returns; a tool that fails raises ToolError, as it does for a client."""
    result: Any = await server.call_tool(tool, args)
    content = result[0] if isinstance(result, tuple) else result
    return "\n".join(block.text for block in content)


async def open_page(server: FastMCP, site: dict[str, str], name: str, **args: Any) -> str:
    """Fetch a named page; return its id."""
    out = await call(server, "fetch", url=site[name], **args)
    found = PAGE_ID.search(out)
    assert found, out
    return found.group()


def _fill(value: Any, fills: dict[str, str]) -> Any:
    """Substitute ``{id}`` / ``{url}`` / ``{refused}`` inside strings, lists and dicts."""
    if isinstance(value, str):
        return value.format(**fills) if "{" in value else value
    if isinstance(value, dict):
        return {k: _fill(v, fills) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill(v, fills) for v in value]
    return value


# --- calls on a fetched page ------------------------------------------------------------


@dataclass(frozen=True)
class Call:
    """One tool call on a page fetched with ``fetch_args``."""

    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    page: str = "bucket"
    fetch_args: dict[str, Any] = field(default_factory=dict)
    says: tuple[str, ...] = ()
    silent: tuple[str, ...] = ()  # fragments the output must not carry
    cap: int = TOOL_CAP
    count: tuple[str, int] | None = None  # (fragment, how many times it appears)
    before: tuple[str, str] | None = None  # the first fragment appears ahead of the second
    passages: bool = False  # every passage is short, and no two overlap


_OWN_BODY = {"bucket": "scripts", "index": 1}
CALLS: dict[str, Call] = {
    "fetch_reports_the_page": Call(
        "fetch",
        {"url": "{url}"},
        says=(
            "Bucket Fixture",
            "200",
            "scripts",
            "json_ld",
            UNTRUSTED,
            "query(queries=",
            "find",
            "read",
        ),
        silent=("almost no visible text",),
    ),
    "fetch_says_when_a_page_is_almost_empty": Call(
        "fetch",
        {"url": "{url}"},
        page="empty",
        says=("almost no visible text", 'engine="full"', "wait_ms"),
    ),
    # Already on the full engine, so suggesting it again would send the agent in a circle.
    "fetch_on_full_does_not_suggest_full": Call(
        "fetch",
        {"url": "{url}", "engine": "full"},
        page="empty",
        says=("almost no visible text", "wait_ms"),
        silent=('engine="full"',),
    ),
    "fetch_of_a_big_page_stays_under_the_cap": Call(
        "fetch", {"url": "{url}"}, page="big", says=("Big", UNTRUSTED)
    ),
    "overview_is_the_table": Call("overview", says=("bucket", "scripts", "json_ld", "total")),
    "find_looks_in_every_bucket": Call(
        "find", {"query": "INLINE_JS_ONE"}, says=("Scripts ·", "INLINE_JS_ONE")
    ),
    "find_ignores_case_by_default": Call(
        "find", {"query": "inline_js_one"}, says=("INLINE_JS_ONE",)
    ),
    "find_can_match_case": Call(
        "find",
        {"query": "inline_js_one", "case_sensitive": True},
        says=("No matches",),
        silent=("Scripts ·",),
    ),
    "find_in_one_bucket_by_regex": Call(
        "find",
        {"query": r'"apiKey":"(\w+)"', "bucket": "scripts", "regex": True},
        says=("Scripts ·", "DEEP_KEY_42"),
        silent=("Styles ·",),
    ),
    "find_in_one_field": Call(
        "find",
        {"query": "deep", "field": "url"},
        says=("Scripts ·", "Styles ·", "Links ·"),
        silent=("Comments ·",),
    ),
    # The visible text is no bucket, yet it is what an agent most often looks for.
    "find_searches_the_visible_text": Call(
        "find", {"query": "VISIBLE_HEADING"}, says=("Text ·", "VISIBLE_HEADING")
    ),
    "find_in_a_bucket_skips_the_text": Call(
        "find",
        {"query": "VISIBLE_HEADING", "bucket": "scripts"},
        says=("No matches",),
        silent=("Text ·",),
    ),
    "find_in_the_text_alone": Call(
        "find",
        {"query": "visible_heading", "bucket": "text"},
        says=("Text ·", "VISIBLE_HEADING"),
        silent=("Scripts ·",),
    ),
    "find_in_a_field_skips_the_text": Call(
        "find", {"query": "VISIBLE_HEADING", "field": "url"}, says=("No matches",)
    ),
    "find_in_the_text_by_regex": Call(
        "find",
        {"query": r"VISIBLE_(\w+)", "bucket": "text", "regex": True},
        says=("Text ·", "VISIBLE_HEADING"),
    ),
    "find_text_can_match_case": Call(
        "find",
        {"query": "visible_heading", "bucket": "text", "case_sensitive": True},
        says=("No matches",),
    ),
    "find_text_shows_the_whole_signature": Call(
        "find",
        {"query": "rglob(", "bucket": "text"},
        page="signature",
        says=("recurse_symlinks=False)",),
    ),
    "find_says_one_more_match_in_the_singular": Call(
        "find",
        {"query": "MANY_LINK", "bucket": "links", "limit": 59},
        page="big",
        says=("1 more match;",),
        silent=("1 more matches",),
    ),
    # --- query: several questions in one call, ranked passages of the visible text ------------
    "query_answers_several_questions_at_once": Call(
        "query",
        {"queries": ["license", "python version", "maintenance status"]},
        page="facts",
        says=(
            UNTRUSTED,
            '## 1. "license"',
            "MIT License",
            '## 2. "python version"',
            "Python 3.11",
            '## 3. "maintenance status"',
            "maintained again",
        ),
        before=('## 1. "license"', '## 2. "python version"'),
        cap=QUERY_CAP,
    ),
    "query_ranks_the_better_passage_first": Call(
        "query",
        {"queries": ["license"], "per_query": 2},
        page="facts",
        says=("MIT License", "license agreement"),
        before=("MIT License", "license agreement"),
        cap=QUERY_CAP,
    ),
    "query_matches_other_forms_of_a_word": Call(
        "query",
        {"queries": ["maintenance"]},
        page="facts",
        says=("maintained again",),
        cap=QUERY_CAP,
    ),
    "query_ignores_filler_words": Call(
        "query",
        {"queries": ["what is the license"], "per_query": 1},
        page="facts",
        says=("MIT License",),
        silent=("What is the answer",),
        cap=QUERY_CAP,
    ),
    "query_says_which_question_matched_nothing": Call(
        "query",
        {"queries": ["zzz_absent", "license"]},
        page="facts",
        says=('## 1. "zzz_absent"', "No passages match", "MIT License"),
        cap=QUERY_CAP,
    ),
    "query_limits_the_passages_per_question": Call(
        "query",
        {"queries": ["license"], "per_query": 1},
        page="facts",
        says=("MIT License",),
        count=("@", 1),
        cap=QUERY_CAP,
    ),
    # Text with no line breaks is cut into overlapping pieces; one match is shown once.
    "query_reads_text_with_no_line_breaks": Call(
        "query",
        {"queries": ["license"]},
        page="glued",
        says=("MIT License",),
        count=("MIT License", 1),
        cap=QUERY_CAP,
    ),
    # Cut passages share text, and a match in the shared part must not be shown twice.
    "query_shows_short_passages_that_do_not_overlap": Call(
        "query",
        {"queries": ["filler sentence"], "per_query": 5},
        page="glued",
        says=("Filler sentence",),
        passages=True,
        cap=QUERY_CAP,
    ),
    "query_weighs_a_rare_word_above_a_common_one": Call(
        "query",
        {"queries": ["common zebra"], "per_query": 1},
        page="rare",
        says=("A single zebra appeared",),
        silent=("Notes 0 on the common workflow",),
        cap=QUERY_CAP,
    ),
    # The phrase starts before the cut and ends after it; only an overlap keeps it whole.
    "query_finds_a_phrase_that_crosses_a_cut": Call(
        "query",
        {"queries": ["alpha bravo"]},
        page="straddle",
        says=("alpha bravo charlie",),  # the question is only "alpha bravo", so this is a passage
        cap=QUERY_CAP,
    ),
    "query_joins_short_lines_into_one_passage": Call(
        "query",
        {"queries": ["business"]},
        page="menu",
        says=("Politics Business Tech Health",),
        cap=QUERY_CAP,
    ),
    # Eight questions of five passages each outgrow the cap; the tool cuts and says so.
    "query_cuts_a_long_answer_at_the_cap": Call(
        "query",
        {"queries": [" ".join(["word"] * n) for n in range(1, 9)], "per_query": 5},
        page="big",
        says=("cut at 8000 characters", '## 1. "word"'),
        passages=True,
        cap=QUERY_CAP,
    ),
    # Context for 60 matches would be cut; the distinct values all fit, so they are shown.
    "find_lists_every_match_when_context_would_be_cut": Call(
        "find",
        {"query": r"tool_(\d+)", "bucket": "text", "regex": True, "limit": 60},
        page="manytools",
        says=("60 matches", "00", "59", "narrow the query"),
        # A capture group names what to list, as `Match.value` does; the prefix is not it.
        silent=("padding words here", "tool_0"),
        cap=TOOL_CAP,
    ),
    # Few enough to show whole: context is what a reader wants, so it stays.
    "find_keeps_context_when_it_fits": Call(
        "find",
        {"query": r"tool_0(\d)", "bucket": "text", "regex": True, "limit": 3},
        page="manytools",
        says=("padding words here",),
        cap=TOOL_CAP,
    ),
    "find_with_no_match": Call("find", {"query": "zzz_absent"}, says=("No matches",)),
    "find_keeps_only_limit_rows": Call(
        "find",
        {"query": "MANY_LINK", "bucket": "links", "limit": 5},
        page="big",
        says=("55 more matches",),
        count=("MANY_LINK", 5),
    ),
    # 20,000 matches of one word: listing the distinct values beats cutting the context.
    "find_lists_a_repeated_match_once": Call(
        "find",
        {"query": "word", "bucket": "text", "limit": 1000},
        page="big",
        says=("20000 matches", "1 distinct"),
        count=("word", 1),  # 20,000 matches collapse to one listed value
        cap=TOOL_CAP,
    ),
    # Even the list can outgrow the cap; then it is cut and says so.
    "find_cuts_a_list_that_is_still_too_long": Call(
        "find",
        {"query": r"item(\d+)", "bucket": "text", "regex": True, "limit": 3000},
        page="manywords",
        says=("cut at 4000 characters", "2000 matches"),
        cap=TOOL_CAP,
    ),
    "read_returns_a_whole_body": Call(
        "read", _OWN_BODY, says=("DEEP_KEY_42", "café", UNTRUSTED), cap=READ_CAP
    ),
    "read_caps_a_big_body_and_names_the_offset": Call(
        "read",
        {"bucket": "scripts", "index": 0},
        page="big",
        says=(f"offset={READ_CAP}",),
        cap=READ_CAP,
    ),
    "page_text_is_the_visible_text_only": Call(
        "page_text",
        says=("VISIBLE_HEADING", UNTRUSTED),
        silent=("INLINE_JS_ONE", "window.cfg", "apiKey"),
        cap=READ_CAP,
    ),
    # Blank lines and indentation would spend the cap on nothing.
    "page_text_collapses_whitespace": Call(
        "page_text",
        page="spaced",
        says=("FIRST_LINE\nSECOND_LINE\nAFTER_THE_SPACE",),
        silent=("  ", "\n\n", "offset="),
        cap=READ_CAP,
    ),
    "page_text_keeps_code_indentation": Call(
        "page_text", page="code", says=("def f():\n    return 1",), cap=READ_CAP
    ),
    "query_ranks_prose_above_a_link_list": Call(
        "query",
        {"queries": ["platform"], "per_query": 2},
        page="navpage",
        says=("The platform runs on a small server",),
        before=("The platform runs", "Platform Tools"),
        cap=QUERY_CAP,
    ),
    # Demoted, not dropped: a word only the nav carries is still reachable.
    "query_does_not_demote_an_ordinary_passage": Call(
        "query",
        {"queries": ["zebra topics"], "per_query": 1},
        page="tied",
        says=("ALPHA",),
        silent=("BETA",),
        cap=QUERY_CAP,
    ),
    # Demotion keeps their order: link-heavy passages are ranked, not flattened to one score.
    "query_orders_link_heavy_passages_by_match": Call(
        "query",
        {"queries": ["salvage"], "per_query": 2},
        page="twonavs",
        says=("BETA", "ALPHA"),
        before=("BETA", "ALPHA"),
        cap=QUERY_CAP,
    ),
    "query_ranks_a_paragraph_above_a_bare_heading": Call(
        "query",
        {"queries": ["telemetry"], "per_query": 2},
        page="heading",
        says=("The telemetry subsystem records",),
        before=("The telemetry subsystem records", "@0  Telemetry"),
        cap=QUERY_CAP,
    ),
    "query_still_reaches_a_link_list": Call(
        "query",
        {"queries": ["cookie policy"]},
        page="navpage",
        says=("Cookie Policy",),
        cap=QUERY_CAP,
    ),
    "page_text_of_a_big_page_is_capped": Call(
        "page_text", page="big", says=(f"offset={READ_CAP}",), cap=READ_CAP
    ),
    "page_text_after_wait_ms_sees_late_content": Call(
        "page_text", page="late", fetch_args={"wait_ms": 700}, says=("LATE_TEXT",), cap=READ_CAP
    ),
    "page_text_without_wait_ms_misses_it": Call(
        "page_text", page="late", says=("EARLY_TEXT",), silent=("LATE_TEXT",), cap=READ_CAP
    ),
}


def _args(tool: str, args: dict[str, Any], page_id: str, url: str) -> dict[str, Any]:
    """Arguments for a call, with ``{id}`` and ``{url}`` filled; a row's own ``id`` wins."""
    filled: dict[str, Any] = _fill(args, {"id": page_id, "url": url})
    return filled if tool in ("fetch", "pages") else {"id": page_id, **filled}


@pytest.mark.parametrize("name", list(CALLS))
async def test_call(server: FastMCP, site: dict[str, str], name: str) -> None:
    row = CALLS[name]
    page_id = await open_page(server, site, row.page, **row.fetch_args)
    out = await call(server, row.tool, **_args(row.tool, row.args, page_id, site[row.page]))
    for fragment in row.says:
        assert fragment in out, out[:600]
    for fragment in row.silent:
        assert fragment not in out, fragment
    assert len(out) <= row.cap + OVERHEAD, f"{len(out)} characters"
    if row.count is not None:
        fragment, times = row.count
        assert out.count(fragment) == times, out
    if row.passages:
        assert "@" in out, out
        # Two questions may show one passage; within a question no two overlap.
        for section in out.split("\n## "):
            found = [(int(o), t) for o, t in re.findall(r"@(\d+)  (.+)", section)]
            assert all(len(t) <= PASSAGE_CAP for _, t in found), "a passage outgrew the cap"
            spans = sorted((o, o + len(t)) for o, t in found)
            assert all(a[1] <= b[0] for a, b in zip(spans, spans[1:], strict=False)), spans
    if row.before is not None:
        first, second = row.before
        assert first in out and second in out, row.before
        assert out.index(first) < out.index(second), out


async def test_reading_by_offset_reassembles_the_body(
    server: FastMCP, site: dict[str, str]
) -> None:
    """Each chunk names where the next starts; together they are the whole body.

    New test: a table row checks one call, and this one follows a chain of them.
    """
    page_id = await open_page(server, site, "bucket")
    whole = onyxweb.RenderResult(BUCKET_PAGE).scripts[1].text
    assert whole is not None
    got, offset, reads = "", 0, 0
    while True:
        out = await call(
            server, "read", id=page_id, bucket="scripts", index=1, offset=offset, max_chars=40
        )
        lines = out.split("\n", 1)[1].split("\n")
        follow = NEXT_OFFSET.search(lines[-1]) if lines[-1].startswith("[") else None
        chunk = "\n".join(lines[:-1] if follow else lines)
        assert len(chunk) <= 40
        got, reads = got + chunk, reads + 1
        if follow is None:
            break
        assert int(follow.group(1)) == len(got), "the next offset counts characters, not bytes"
        offset = int(follow.group(1))
    assert got == whole
    assert reads > 1, "the body fit one read, so this proves nothing"


# --- bad input --------------------------------------------------------------------------


@dataclass(frozen=True)
class Error:
    """A call that must fail with a message naming the problem and the fix."""

    tool: str
    args: dict[str, Any]
    says: tuple[str, ...]


ERRORS: dict[str, Error] = {
    "unknown_id": Error(
        "overview",
        {"id": "p0000000000"},
        ("no page p0000000000", "this session", "fetch the URL again"),
    ),
    "malformed_id": Error("overview", {"id": "../x"}, ("'../x' is not a page id", "fetch")),
    "index_past_the_end": Error("read", {"bucket": "scripts", "index": 99}, ("4 records", "99")),
    "negative_index": Error("read", {"bucket": "scripts", "index": -1}, ("out of range",)),
    "unknown_bucket_in_read": Error(
        "read", {"bucket": "nope", "index": 0}, ("unknown bucket 'nope'", "scripts", "json_ld")
    ),
    "unknown_bucket_in_find": Error(
        "find", {"query": "x", "bucket": "nope"}, ("unknown bucket 'nope'", "scripts")
    ),
    "invalid_regex": Error(
        "find", {"query": "(?<=a)b", "regex": True}, ("invalid search pattern",)
    ),
    "unknown_field_in_a_bucket": Error(
        "find", {"query": "x", "bucket": "scripts", "field": "nope"}, ("no field 'nope'", "text")
    ),
    "unknown_field_in_every_bucket": Error(
        "find", {"query": "x", "field": "nope"}, ("no bucket has a field 'nope'",)
    ),
    "field_on_the_text_bucket": Error(
        "find", {"query": "x", "bucket": "text", "field": "url"}, ("text has no fields",)
    ),
    "query_without_questions": Error("query", {"queries": []}, ("at least 1 question",)),
    "query_with_too_many_questions": Error(
        "query", {"queries": ["x"] * 9}, ("at most 8 questions", "split")
    ),
    "query_with_a_blank_question": Error(
        "query", {"queries": ["license", "  "]}, ("question 2 is empty",)
    ),
    "query_per_query_zero": Error(
        "query", {"queries": ["x"], "per_query": 0}, ("per_query must be between 1 and 5",)
    ),
    "query_per_query_over": Error(
        "query", {"queries": ["x"], "per_query": 6}, ("per_query must be between 1 and 5",)
    ),
    "query_on_an_unknown_page": Error(
        "query", {"queries": ["x"], "id": "p0000000000"}, ("no page p0000000000",)
    ),
    "find_limit_zero": Error("find", {"query": "x", "limit": 0}, ("limit must be at least 1",)),
    "max_chars_zero": Error(
        "read", {"bucket": "scripts", "index": 1, "max_chars": 0}, ("max_chars must be at least 1",)
    ),
    "max_chars_over_the_ceiling": Error(
        "read", {"bucket": "scripts", "index": 1, "max_chars": 20_001}, ("at most", "20000")
    ),
    "offset_past_the_end": Error(
        "read", {"bucket": "scripts", "index": 1, "offset": 999_999}, ("past the end", "characters")
    ),
    "negative_offset": Error("page_text", {"offset": -1}, ("offset must be at least 0",)),
    "unknown_engine": Error("fetch", {"url": "{url}", "engine": "turbo"}, ("'shell'", "'full'")),
    # The browser's own failure reaches the caller instead of a silent empty page.
    "unreachable_url": Error("fetch", {"url": "{refused}"}, ("ERR_CONNECTION_REFUSED",)),
}


@pytest.mark.parametrize("name", list(ERRORS))
async def test_bad_input(
    server: FastMCP, site: dict[str, str], refused_url: str, name: str
) -> None:
    row = ERRORS[name]
    page_id = await open_page(server, site, "bucket")
    args = {**_fill(row.args, {"url": site["bucket"], "refused": refused_url})}
    args = args if row.tool == "fetch" else {"id": page_id, **args}
    with pytest.raises(ToolError) as exc:
        await call(server, row.tool, **args)
    for fragment in row.says:
        assert fragment in str(exc.value), str(exc.value)
    # The page still answers, so the failure came from the input and not a broken store.
    assert "scripts" in await call(server, "overview", id=page_id)


# --- the session's pages ----------------------------------------------------------------


async def test_pages_lists_what_was_fetched_newest_first(
    shared: onyxweb.AsyncClient, site: dict[str, str]
) -> None:
    server = _server(shared)
    assert "No pages" in await call(server, "pages")
    alpha = await open_page(server, site, "alpha")
    beta = await open_page(server, site, "beta")
    out = await call(server, "pages")
    for fragment in (alpha, beta, "Alpha", "Beta", site["alpha"], site["beta"], "200"):
        assert fragment in out, out
    assert out.index(beta) < out.index(alpha)


async def test_find_reads_one_page_or_all_of_them(
    shared: onyxweb.AsyncClient, site: dict[str, str]
) -> None:
    server = _server(shared)
    alpha = await open_page(server, site, "alpha")
    beta = await open_page(server, site, "beta")
    every = await call(server, "find", query="SHARED_MARK")
    for fragment in (alpha, beta, site["alpha"], site["beta"]):
        assert fragment in every, every  # each hit names its page
    one = await call(server, "find", query="SHARED_MARK", id=alpha)
    assert alpha in one and beta not in one
    assert "No matches" in await call(server, "find", query="BETA_ONLY", id=alpha)


# --- what the server offers -------------------------------------------------------------


async def test_the_tools_offer_no_script_execution(server: FastMCP) -> None:
    tools = {t.name: t for t in await server.list_tools()}
    assert set(tools) == TOOLS
    offered = {p for t in tools.values() for p in t.inputSchema.get("properties", {})}
    assert offered.isdisjoint(REFUSED_FIELDS), offered & REFUSED_FIELDS
    assert {"url", "query", "id", "bucket"} <= offered  # so the check above is not vacuous
    assert set(tools["fetch"].inputSchema["properties"]) == {"url", "engine", "wait_ms"}
    assert server.instructions is not None
    assert "untrusted" in server.instructions.lower()
    assert "engine" in server.instructions  # the advice for a page that comes back empty
    # What the text is, so a glued or broken reading is checked rather than believed.
    for stated in ("derived", "<pre>", "table", "find", "search tool"):
        assert stated in server.instructions, stated


def test_a_missing_mcp_package_names_the_extra() -> None:
    """The module imports ``mcp`` at the top, so without it the error must say what to install."""
    block = "import sys; sys.modules['mcp'] = None; import onyxweb_server.mcp"
    blocked = subprocess.run(
        [sys.executable, "-c", block], capture_output=True, text=True, timeout=60
    )
    assert blocked.returncode != 0
    assert "onyxweb-server[mcp]" in blocked.stderr, blocked.stderr
    present = subprocess.run(
        [sys.executable, "-c", "import onyxweb_server.mcp"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert present.returncode == 0, present.stderr


async def test_the_server_speaks_mcp_over_stdio(httpserver: HTTPServer) -> None:
    """Spawned as Claude Code spawns it: list the tools, fetch, find, and meet the guard."""
    params = StdioServerParameters(command=sys.executable, args=["-m", "onyxweb_server", "mcp"])
    async with (
        asyncio.timeout(120),
        stdio_client(params) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        assert {t.name for t in (await session.list_tools()).tools} == TOOLS
        fetched = await session.call_tool("fetch", {"url": "https://example.com/"})
        assert not fetched.isError, fetched
        found = PAGE_ID.search(fetched.content[0].text)  # type: ignore[union-attr]
        assert found, fetched
        hit = await session.call_tool("find", {"query": "Example Domain", "id": found.group()})
        assert "Example Domain" in hit.content[0].text  # type: ignore[union-attr]
        refused = await session.call_tool("fetch", {"url": httpserver.url_for("/")})
        assert refused.isError
        assert "private" in refused.content[0].text  # type: ignore[union-attr]
    assert httpserver.log == []


@pytest.mark.parametrize(
    ("page", "needle"), [("bucket", "Body sentence"), ("spaced", "SECOND_LINE")]
)
async def test_a_text_match_leads_to_page_text(
    server: FastMCP, site: dict[str, str], page: str, needle: str
) -> None:
    """The offset a text match shows is where ``page_text`` starts reading it.

    New test: a table row checks one call, and this one follows a match into a second tool. The
    ``spaced`` page has whitespace to collapse, which shifts every offset after it.
    """
    page_id = await open_page(server, site, page)
    hit = await call(server, "find", id=page_id, query=needle, bucket="text")
    row = next(line for line in hit.splitlines() if needle in line)
    offset = int(row.split()[0])
    read = await call(server, "page_text", id=page_id, offset=offset, max_chars=len(needle))
    chunk = read.split("\n", 1)[1].split("\n[continues", 1)[0]  # the body, without the footer
    assert chunk == needle


async def test_query_reads_one_page_or_all_of_them(
    shared: onyxweb.AsyncClient, site: dict[str, str]
) -> None:
    server = _server(shared)
    alpha = await open_page(server, site, "alpha")
    beta = await open_page(server, site, "beta")
    every = await call(server, "query", queries=["SHARED_MARK"])
    for fragment in (alpha, beta, site["alpha"], site["beta"]):
        assert fragment in every, every  # each hit names its page
    one = await call(server, "query", queries=["SHARED_MARK"], id=alpha)
    assert alpha in one and beta not in one
    empty = _server(shared)
    with pytest.raises(ToolError, match="fetch"):
        await call(empty, "query", queries=["SHARED_MARK"])


@pytest.mark.parametrize(
    ("page", "question"),
    [("facts", "MIT License"), ("menu", "business"), ("glued", "license")],
)
async def test_a_query_passage_leads_to_page_text(
    server: FastMCP, site: dict[str, str], page: str, question: str
) -> None:
    """The offset a passage shows is where ``page_text`` starts reading it.

    New test: a table row checks one call, and this one follows a passage into a second tool.
    The ``menu`` passage joins lines, and ``glued`` one is cut from a long line.
    """
    page_id = await open_page(server, site, page)
    out = await call(server, "query", id=page_id, queries=[question], per_query=1)
    hit = re.search(r"@(\d+)  (.+)", out)
    assert hit, out
    offset, passage = int(hit.group(1)), hit.group(2)
    read = await call(server, "page_text", id=page_id, offset=offset, max_chars=40)
    chunk = read.split("\n", 1)[1].split("\n[continues", 1)[0]
    assert " ".join(chunk.split())[:20] == passage[:20]
