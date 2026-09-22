"""C16 server CLI — arguments map to an exit code, stdout and stderr.

``SERVER_COMMANDS`` drive ``onyxweb-server``'s own arguments in-process: help, a missing or
unknown command, and a bad option each exit with a usage line and a message naming the problem,
never a traceback. Starting a server is C13's (MCP) and C15's (HTTP) business, since a running
server serves until its client leaves. One subprocess case per front-end proves a missing extra
exits 1 and names the fix. ``BINDS`` pin which addresses ``http`` will listen on: loopback freely,
anything else only with ``ONYXWEB_SERVER_TOKEN`` set.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

import pytest
import uvicorn
from fastapi.testclient import TestClient
from onyxweb_server.__main__ import main as server_main

# `onyxweb-server` arguments -> exit code, stdout fragment, stderr fragment.
SERVER_COMMANDS: dict[str, tuple[list[str], int, str, str]] = {
    "help": (["--help"], 0, "usage: onyxweb-server", ""),
    "mcp_help": (["mcp", "--help"], 0, "usage: onyxweb-server mcp", ""),
    "no_command": ([], 1, "", "command"),
    "unknown_command": (["ftp"], 1, "", "invalid choice"),
    "unknown_argument": (["mcp", "--bogus"], 1, "", "unrecognized arguments: --bogus"),
    "http_help": (["http", "--help"], 0, "usage: onyxweb-server http", ""),
    "http_help_names_the_token": (["http", "--help"], 0, "ONYXWEB_SERVER_TOKEN", ""),
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


# --host, ONYXWEB_SERVER_TOKEN -> whether `http` starts listening.
BINDS: dict[str, tuple[str, str | None, bool]] = {
    "loopback": ("127.0.0.1", None, True),
    "localhost": ("localhost", None, True),
    "ipv6_loopback": ("::1", None, True),
    "every_interface_with_a_token": ("0.0.0.0", "s3cret", True),
    "every_interface": ("0.0.0.0", None, False),
    "ipv6_any": ("::", None, False),
    "a_private_address": ("192.168.1.5", None, False),
    "a_host_name": ("myhost.internal", None, False),
    "an_empty_token_is_no_token": ("0.0.0.0", "", False),
}


@pytest.mark.parametrize("name", list(BINDS))
def test_a_public_bind_needs_a_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    """Only a loopback address starts without a token, and the token the CLI read is the app's.

    New test: starting is the one thing ``test_server_command`` cannot do, so ``uvicorn.run`` is
    replaced by a recorder.
    """
    host, token, starts = BINDS[name]
    started: list[tuple[Any, dict[str, Any]]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: started.append((app, kw)))
    monkeypatch.delenv("ONYXWEB_SERVER_TOKEN", raising=False)
    if token is not None:
        monkeypatch.setenv("ONYXWEB_SERVER_TOKEN", token)
    try:
        code = server_main(["http", "--host", host])
    except SystemExit as se:
        code = se.code if isinstance(se.code, int) else 1
    err = capsys.readouterr().err
    if starts:
        assert code == 0, err
        [(app, kwargs)] = started
        assert kwargs["host"] == host
        if token:
            with TestClient(app) as client:  # the gate is on, and no browser is needed to see it
                assert (
                    client.post("/fetch", json={"url": "http://93.184.216.34/"}).status_code == 401
                )
    else:
        assert code == 1 and started == []
        assert err.startswith("usage: onyxweb-server")
        assert "ONYXWEB_SERVER_TOKEN" in err and host in err
