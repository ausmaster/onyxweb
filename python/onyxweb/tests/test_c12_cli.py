"""C12 CLI — arguments map to an exit code, stdout, stderr and written files.

``main(argv)`` runs in-process against a local page. Exit codes follow the module
docstring: 0 success, 1 bad argument, 2 fetch error. ``BAD_ARGUMENTS`` never reach
Chrome: each exits 1 with a usage line and a message naming the problem, never a
traceback. ``OUTPUTS`` fetch the page and route HTML, JSON, metadata and images to
stdout, stderr or files. One subprocess case proves ``python -m onyxweb`` starts. ``--json``
prints a snapshot, to ``-o`` when given; ``PAGE_COMMANDS`` drive ``onyxweb page``, which
queries a saved snapshot offline: no Chrome, no network.
"""

from __future__ import annotations

import json
import struct
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib.metadata import version
from pathlib import Path

import pytest
from conftest import BUCKET_PAGE, JPEG_MAGIC, PNG_MAGIC, is_webp
from onyxweb import RenderResult
from onyxweb.__main__ import main
from pytest_httpserver import HTTPServer

NEVER_FETCHED = "http://127.0.0.1:9/"  # argument errors stop before any fetch
# The script's marker is assembled at runtime, so it shows only if JavaScript ran.
_PAGE = (
    "<html><head><meta charset='utf-8'><title>CLI Page</title></head>"
    "<body><p>CLI_PAGE</p><script>document.body.append('JS_' + 'RAN')</script></body></html>"
)

IS_IMAGE: dict[str, Callable[[bytes], bool]] = {
    "png": lambda b: b[:8] == PNG_MAGIC,
    "jpeg": lambda b: b[:3] == JPEG_MAGIC,
    "webp": is_webp,
}


@dataclass(frozen=True)
class Run:
    """What one CLI invocation produced."""

    code: int
    out: str
    err: str


def _run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> Run:
    try:
        code = main(argv)
    except SystemExit as se:  # argparse exits for --help and usage errors
        code = se.code if isinstance(se.code, int) else 1
    captured = capsys.readouterr()
    return Run(code, captured.out, captured.err)


# Arguments -> fragment the error message must contain.
BAD_ARGUMENTS: dict[str, tuple[list[str], str]] = {
    "no_url": ([], "URL is required"),
    "unknown_preset": (["--preset", "stealth.NOPE", NEVER_FETCHED], "unknown preset"),
    "malformed_preset": (["--preset", "stealthBASIC", NEVER_FETCHED], "engine.purpose.NAME"),
    "screenshot_only_with_output": (
        [NEVER_FETCHED, "--screenshot-only", "/tmp/x", "-o", "/tmp/y"],
        "mutually exclusive",
    ),
    "screenshot_only_with_json": (
        [NEVER_FETCHED, "--screenshot-only", "/tmp/x", "--json"],
        "mutually exclusive",
    ),
    "quality_out_of_range": (
        [NEVER_FETCHED, "--screenshot-only", "/tmp/x.jpg", "--quality", "150"],
        "--quality",
    ),
    "malformed_header": ([NEVER_FETCHED, "-H", "no-separator"], "KEY=VALUE"),
    # A flag that does nothing is an error, not a silent no-op.
    "force_without_install": ([NEVER_FETCHED, "--force"], "--force applies to --install"),
    # Config validation rejects it; Chromium would drop it silently.
    "forbidden_header": ([NEVER_FETCHED, "-H", "Cookie: a=b"], "Cookie"),
}


@pytest.mark.parametrize("name", list(BAD_ARGUMENTS))
def test_bad_argument(capsys: pytest.CaptureFixture[str], name: str) -> None:
    """A bad argument exits 1 with usage and a named problem — never a traceback."""
    argv, says = BAD_ARGUMENTS[name]
    run = _run(argv, capsys)
    assert run.code == 1, run.err
    assert run.err.startswith("usage:")
    assert says in run.err
    assert "Traceback" not in run.err


@dataclass(frozen=True)
class Output:
    """Arguments (``{url}`` and ``{tmp}`` filled in) and where the output lands."""

    argv: list[str]
    stdout: str  # "html", "json" or "empty"
    stderr: tuple[str, ...] = ()
    files: dict[str, str] = field(default_factory=dict)  # file name -> "html", "snapshot" or image
    js: bool = True  # whether the page's script ran
    size: tuple[int, int] | None = None  # PNG width, height


OUTPUTS: dict[str, Output] = {
    "stdout_is_html": Output(["{url}"], "html"),
    "output_dash_is_stdout": Output(["{url}", "-o", "-"], "html"),
    "json": Output(["{url}", "--json"], "json"),
    "meta_goes_to_stderr": Output(["{url}", "--meta"], "html", ("status=200", "final_url=")),
    "output_file_silences_stdout": Output(
        ["{url}", "-o", "{tmp}/page.html"], "empty", files={"page.html": "html"}
    ),
    "screenshot_keeps_stdout_html": Output(
        ["{url}", "-s", "{tmp}/shot.png"], "html", files={"shot.png": "png"}
    ),
    "screenshot_only_silences_html": Output(
        ["{url}", "--screenshot-only", "{tmp}/shot.png"], "empty", files={"shot.png": "png"}
    ),
    "output_and_screenshot": Output(
        ["{url}", "-o", "{tmp}/page.html", "-s", "{tmp}/page.png"],
        "empty",
        files={"page.html": "html", "page.png": "png"},
    ),
    "jpeg_from_extension": Output(
        ["{url}", "--screenshot-only", "{tmp}/shot.jpg"], "empty", files={"shot.jpg": "jpeg"}
    ),
    "webp_from_extension": Output(
        ["{url}", "--screenshot-only", "{tmp}/shot.webp"], "empty", files={"shot.webp": "webp"}
    ),
    "format_overrides_extension": Output(
        ["{url}", "--screenshot-only", "{tmp}/shot.png", "--format", "jpeg"],
        "empty",
        files={"shot.png": "jpeg"},
    ),
    "width_and_height": Output(
        ["{url}", "--screenshot-only", "{tmp}/shot.png", "--width", "400", "--height", "300"],
        "empty",
        files={"shot.png": "png"},
        size=(400, 300),
    ),
    # --json is a snapshot; -o names where it goes, as it does for the HTML.
    "json_to_a_file_is_a_snapshot": Output(
        ["{url}", "--json", "-o", "{tmp}/page.json"], "empty", files={"page.json": "snapshot"}
    ),
    "json_with_a_screenshot_file": Output(
        ["{url}", "--json", "-s", "{tmp}/shot.png"], "json", files={"shot.png": "png"}
    ),
    # The extension never picks the format: only --json makes a snapshot.
    "output_named_json_is_still_html": Output(
        ["{url}", "-o", "{tmp}/page.json"], "empty", files={"page.json": "html"}
    ),
    "preset_recon_fast_turns_js_off": Output(
        ["--preset", "shell.recon.FAST", "{url}", "-o", "{tmp}/page.html"],
        "empty",
        files={"page.html": "html"},
        js=False,
    ),
}


@pytest.mark.parametrize("name", list(OUTPUTS))
def test_output(
    capsys: pytest.CaptureFixture[str], httpserver: HTTPServer, tmp_path: Path, name: str
) -> None:
    """Each flag set routes the fetched page where it says."""
    row = OUTPUTS[name]
    httpserver.expect_request("/").respond_with_data(_PAGE, content_type="text/html")
    url = httpserver.url_for("/")
    run = _run([arg.format(url=url, tmp=tmp_path) for arg in row.argv], capsys)
    assert run.code == 0, run.err
    htmls: list[str] = []  # every place the page's HTML landed
    if row.stdout == "html":
        htmls.append(run.out)
    elif row.stdout == "json":
        data = json.loads(run.out)
        assert (data["status_code"], data["final_url"], data["errors"]) == (200, url, [])
        # A snapshot, and still every key the old --json printed.
        assert data["url"] == url
        assert {"onyxweb_snapshot", "headers", "metadata", "anti_bot"} <= set(data)
        htmls.append(data["html"])
    else:
        assert run.out == ""
    for fragment in row.stderr:
        assert fragment in run.err
    for file_name, kind in row.files.items():
        written = tmp_path / file_name
        if kind == "html":
            text = written.read_text()
            assert text.lstrip().startswith("<"), f"{file_name} is not HTML: {text[:40]!r}"
            htmls.append(text)
        elif kind == "snapshot":
            loaded = RenderResult.load(written)
            assert (loaded.status_code, loaded.final_url) == (200, url)
            htmls.append(loaded.html)
        else:
            assert IS_IMAGE[kind](written.read_bytes()), f"{file_name} is not {kind}"
    for html in htmls:
        assert "CLI_PAGE" in html
        assert ("JS_RAN" in html) == row.js
    if row.size is not None:
        data = (tmp_path / "shot.png").read_bytes()
        assert struct.unpack(">II", data[16:24]) == row.size  # IHDR width, height


def test_headers_reach_the_server(
    capsys: pytest.CaptureFixture[str], httpserver: HTTPServer
) -> None:
    """``-H`` takes ``KEY: VALUE`` and ``KEY=VALUE``; both arrive on the page request."""
    httpserver.expect_request("/").respond_with_data(_PAGE, content_type="text/html")
    run = _run([httpserver.url_for("/"), "-H", "X-Foo: bar", "-H", "X-Baz=qux"], capsys)
    assert run.code == 0, run.err
    request = next(req for req, _ in httpserver.log if req.path == "/")
    assert (request.headers.get("X-Foo"), request.headers.get("X-Baz")) == ("bar", "qux")


def test_jpeg_quality_trades_size(
    capsys: pytest.CaptureFixture[str], httpserver: HTTPServer, tmp_path: Path
) -> None:
    """``--quality`` reaches the encoder: quality 5 is smaller than quality 95."""
    httpserver.expect_request("/").respond_with_data(_PAGE, content_type="text/html")
    url = httpserver.url_for("/")
    for quality in ("95", "5"):
        out = str(tmp_path / f"q{quality}.jpg")
        assert _run([url, "--screenshot-only", out, "--quality", quality], capsys).code == 0
    assert (tmp_path / "q5.jpg").stat().st_size < (tmp_path / "q95.jpg").stat().st_size


def test_fetch_error_exits_2(capsys: pytest.CaptureFixture[str], refused_url: str) -> None:
    run = _run([refused_url], capsys)
    assert run.code == 2
    assert run.err.startswith("onyxweb: ")
    assert "ERR_CONNECTION_REFUSED" in run.err


def test_informational_flags(capsys: pytest.CaptureFixture[str]) -> None:
    """``--help``, ``--version`` and ``--preset list`` print and exit 0 without fetching."""
    run = _run(["--help"], capsys)
    assert (run.code, run.out.startswith("usage: python -m onyxweb")) == (0, True)
    assert "onyxweb page" in run.out  # the subcommand is discoverable from the main help
    run = _run(["--version"], capsys)
    assert (run.code, run.out.strip()) == (0, version("onyxweb"))
    run = _run(["--preset", "list"], capsys)
    assert run.code == 0
    for preset in (
        "shell.stealth.BASIC",
        "shell.stealth.FINGERPRINT",
        "shell.recon.FAST",
        "shell.archival.FULL_PAGE",
        "full.stealth.BASIC",
    ):
        assert preset in run.out


@pytest.mark.parametrize(
    ("argv", "engine", "force"),
    [
        (["--install"], None, False),
        (["--install", "--engine", "shell"], "shell", False),
        (["--install", "--engine", "full"], "full", False),
        (["--install", "--force"], None, True),
        (["--install", "--engine", "full", "--force"], "full", True),
    ],
    ids=["every_engine", "shell_only", "full_only", "forced", "full_forced"],
)
def test_install_flag_fetches_every_engine_unless_one_is_named(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    engine: str | None,
    force: bool,
) -> None:
    """``onyxweb --install`` asks for every engine, ``--engine`` narrows it to one, and
    ``--force`` re-downloads a build that is already there.

    New test: a real install downloads hundreds of MB, so ``install_chrome`` is replaced by a
    recorder, which the ``BAD_ARGUMENTS`` and ``OUTPUTS`` tables cannot do.
    """
    asked: list[dict[str, object]] = []

    def install(**kwargs: object) -> int:
        asked.append(kwargs)
        return 0

    monkeypatch.setattr("onyxweb.download.install_chrome", install)
    result = _run(argv, capsys)
    assert result.code == 0, result.err
    assert asked == [{"engine": engine, "force": force}]


def test_module_entry_point_runs(tmp_path: Path) -> None:
    """``python -m onyxweb`` starts, and ``page`` reaches its subcommand."""
    for args, usage in (
        (["--help"], "python -m onyxweb ["),
        (["page", "--help"], "python -m onyxweb page"),
    ):
        p = subprocess.run(
            [sys.executable, "-m", "onyxweb", *args],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=tmp_path,  # `-m` puts the cwd on sys.path, and the source package has no extension
        )
        assert p.returncode == 0, p.stderr
        assert p.stdout.startswith(f"usage: {usage}")


# `onyxweb page` arguments ({snap} is the saved BUCKET_PAGE) -> exit code, stdout, stderr.
PAGE_COMMANDS: dict[str, tuple[list[str], int, tuple[str, ...], tuple[str, ...]]] = {
    "help": (["--help"], 0, ("usage: python -m onyxweb page",), ()),
    "overview": (["overview", "{snap}"], 0, ("bucket", "scripts", "json_ld"), ()),
    "search_every_bucket": (
        ["search", "{snap}", "INLINE_JS_ONE"],
        0,
        ("Scripts ·", "INLINE_JS_ONE"),
        (),
    ),
    "search_is_case_insensitive": (
        ["search", "{snap}", "inline_js_one"],
        0,
        ("INLINE_JS_ONE",),
        (),
    ),
    "search_case_sensitive_misses": (
        ["search", "{snap}", "inline_js_one", "--case-sensitive"],
        0,
        ("No matches",),
        (),
    ),
    "search_one_bucket_by_regex": (
        ["search", "{snap}", r'"apiKey":"(\w+)"', "--bucket", "scripts", "--regex"],
        0,
        ("Scripts ·", "DEEP_KEY_42"),
        (),
    ),
    "search_narrowed_to_a_field": (
        ["search", "{snap}", "deep", "--field", "url"],
        0,
        ("Scripts ·", "Styles ·", "Links ·"),
        (),
    ),
    "search_finds_nothing": (["search", "{snap}", "zzz_absent"], 0, ("No matches",), ()),
    "text_prints_the_whole_body": (
        ["text", "{snap}", "scripts", "1"],
        0,
        ("DEEP_KEY_42", "café"),
        (),
    ),
    "no_command": ([], 1, (), ("command",)),
    "text_index_past_the_end": (["text", "{snap}", "scripts", "99"], 1, (), ("scripts", "4")),
    "unknown_bucket": (["text", "{snap}", "nonsense", "0"], 1, (), ("invalid choice",)),
    "invalid_regex": (
        ["search", "{snap}", "(?<=a)b", "--regex"],
        1,
        (),
        ("invalid search pattern",),
    ),
    "file_missing": (["overview", "{tmp}/missing.json"], 1, (), ("missing.json",)),
    "not_a_snapshot": (
        ["overview", "{tmp}/junk.json"],
        1,
        (),
        ("not an onyxweb snapshot", "RenderResult.save"),
    ),
}


@pytest.fixture(scope="module")
def snapshot(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """BUCKET_PAGE as a saved snapshot, built without a browser."""
    path = tmp_path_factory.mktemp("snapshot") / "page.json"
    RenderResult(BUCKET_PAGE, final_url="http://127.0.0.1/page.html").save(path)
    return path


@pytest.mark.parametrize("name", list(PAGE_COMMANDS))
def test_page_command(
    capsys: pytest.CaptureFixture[str], snapshot: Path, tmp_path: Path, name: str
) -> None:
    """Each command reads a saved snapshot and prints what it says; bad input exits 1.

    New test — ``onyxweb page`` is a subcommand that never fetches, so the fetch-driven
    tables above cannot drive it.
    """
    argv, code, prints, complains = PAGE_COMMANDS[name]
    (tmp_path / "junk.json").write_text("not json")
    run = _run(["page", *(a.format(snap=snapshot, tmp=tmp_path) for a in argv)], capsys)
    assert run.code == code, run.err
    for fragment in prints:
        assert fragment in run.out, run.out
    for fragment in complains:
        assert fragment in run.err, run.err
    if code == 1:
        assert run.err.startswith("usage:")
        assert "Traceback" not in run.err
