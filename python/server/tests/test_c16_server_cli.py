"""C16 server CLI — arguments map to an exit code, stdout and stderr.

``SERVER_COMMANDS`` drive ``onyxweb-server``'s own arguments in-process: help, a missing or
unknown command, and a bad option each exit with a usage line and a message naming the problem,
never a traceback. Starting a server is C13's (MCP) and C15's (HTTP) business, since a running
server serves until its client leaves. One subprocess case per front-end proves a missing extra
exits 1 and names the fix.
"""

from __future__ import annotations

import subprocess
import sys

import pytest
from onyxweb_server.__main__ import main as server_main

# `onyxweb-server` arguments -> exit code, stdout fragment, stderr fragment.
SERVER_COMMANDS: dict[str, tuple[list[str], int, str, str]] = {
    "help": (["--help"], 0, "usage: onyxweb-server", ""),
    "mcp_help": (["mcp", "--help"], 0, "usage: onyxweb-server mcp", ""),
    "no_command": ([], 1, "", "command"),
    "unknown_command": (["ftp"], 1, "", "invalid choice"),
    "unknown_argument": (["mcp", "--bogus"], 1, "", "unrecognized arguments: --bogus"),
    "http_help": (["http", "--help"], 0, "usage: onyxweb-server http", ""),
    "http_port_not_a_number": (["http", "--port", "abc"], 1, "", "invalid int value"),
}


@pytest.mark.parametrize("name", list(SERVER_COMMANDS))
def test_server_command(capsys: pytest.CaptureFixture[str], name: str) -> None:
    """``onyxweb-server``'s own arguments; starting the server is C13's.

    New test: the server serves until its client leaves, so no output table can drive it.
    """
    argv, code, prints, complains = SERVER_COMMANDS[name]
    try:
        got = server_main(argv)
    except SystemExit as se:  # argparse exits for --help and usage errors
        got = se.code if isinstance(se.code, int) else 1
    captured = capsys.readouterr()
    assert got == code, captured.err
    assert prints in captured.out and complains in captured.err
    if code == 1:
        assert captured.err.startswith("usage: onyxweb-server")
        assert "Traceback" not in captured.err


@pytest.mark.parametrize(
    ("command", "missing", "extra"), [("mcp", "mcp", "mcp"), ("http", "fastapi", "http")]
)
def test_server_without_its_extra_exits_1_and_names_the_fix(
    command: str, missing: str, extra: str
) -> None:
    """Without a front-end's package ``onyxweb-server`` says what to install, not a traceback.

    New test: it needs an interpreter that can't import the package, so it runs in a subprocess.
    """
    block = (
        f"import sys; sys.modules[{missing!r}] = None; from onyxweb_server.__main__ import main; "
        f"raise SystemExit(main([{command!r}]))"
    )
    p = subprocess.run([sys.executable, "-c", block], capture_output=True, text=True, timeout=60)
    assert p.returncode == 1, p.stderr
    assert f"onyxweb-server[{extra}]" in p.stderr
    assert "Traceback" not in p.stderr
