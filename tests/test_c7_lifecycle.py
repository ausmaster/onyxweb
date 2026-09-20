"""C7 lifecycle — launch, close and Chrome install leave nothing half-done.

Closing: the CDP handler task ends only once the browser goes away, so close tells
Chrome to exit rather than waiting out the handler's timeout. Every close shape must
return within ``CLOSE_BUDGET_S``, stop and reap the Chrome it started while the client is
still referenced, ignore a second close, and refuse a fetch afterwards. The same holds when
Chrome was killed first, and then every call must also raise ``ChromeExitedError`` at once.
A frozen Chrome can't answer shutdown at all, so its close is bounded by
``CLOSE_TIMEOUT_S`` instead.

Installing: ``ensure_chrome`` / ``aensure_chrome`` are the public installer a host app
such as BBOT hooks into. ``ENSURE_ARGS`` checks each argument reaches ``download_for``
(monkeypatched, so nothing is fetched). ``INSTALL_FAILURES`` feeds ``download_for`` a
bad network, a bad archive or an unsupported platform: each raises
``OnyxwebDownloadError`` naming the problem, and a prior install survives.
``test_find_chrome`` pins the never-raising "installed?" predicate.
"""

from __future__ import annotations

import contextlib
import inspect
import io
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import onyxweb
import onyxweb.download as dl
import psutil
import pytest

needs_proc = pytest.mark.skipif(not os.path.isdir("/proc"), reason="reads /proc")

# --- closing ------------------------------------------------------------------

CLOSE_BUDGET_S = 1.0  # a healthy close takes ~10 ms; generous for a loaded CI box
CLOSE_TIMEOUT_S = 3.0  # mirrors CLOSE_TIMEOUT in src/client.rs
Shape = Literal["close", "context_manager", "aclose"]


def _chrome_children(*, zombies: bool = False) -> set[int]:
    """Chrome processes started by this test process; zombies (unreaped) only when asked."""
    pids: set[int] = set()
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat") as f:
                stat = f.read()
        except OSError:
            continue
        name = stat[stat.index("(") + 1 : stat.rindex(")")]
        state, ppid = stat[stat.rindex(")") + 2 :].split()[:2]
        if int(ppid) == os.getpid() and "chrome" in name and (zombies or state != "Z"):
            pids.add(int(entry))
    return pids


def _gone(pids: set[int], within_s: float = CLOSE_BUDGET_S) -> bool:
    deadline = time.monotonic() + within_s
    while time.monotonic() < deadline:
        if not pids & _chrome_children():
            return True
        time.sleep(0.01)
    return False


async def _close(shape: Shape, client: onyxweb.Client | onyxweb.AsyncClient) -> None:
    if isinstance(client, onyxweb.AsyncClient):
        await client.aclose()
    elif shape == "context_manager":
        client.__exit__(None, None, None)
    else:
        client.close()


@needs_proc
@pytest.mark.parametrize("killed", [False, True], ids=["running", "killed"])
@pytest.mark.parametrize("shape", ["close", "context_manager", "aclose"])
async def test_close_is_prompt_stops_chrome_and_is_final(shape: Shape, killed: bool) -> None:
    before = _chrome_children()
    client: onyxweb.Client | onyxweb.AsyncClient
    if shape == "aclose":
        client = onyxweb.AsyncClient(concurrency=1)
    else:
        client = onyxweb.Client(concurrency=1)
        if shape == "context_manager":
            client.__enter__()
    launched = _chrome_children() - before
    assert launched, "expected this client to start a Chrome process"
    if killed:  # a Chrome that died under a live client is named on every call, then still closes
        for pid in launched:
            os.kill(pid, signal.SIGKILL)
        assert _gone(launched, within_s=5.0), "Chrome survived SIGKILL"
        await _assert_dead_chrome_is_named(client)

    started = time.perf_counter()
    await _close(shape, client)
    assert time.perf_counter() - started < CLOSE_BUDGET_S
    assert _gone(launched), "Chrome still running after close, though the client is referenced"
    assert not launched & _chrome_children(zombies=True), "Chrome left unreaped after close"

    await _close(shape, client)  # a second close is a no-op
    with pytest.raises(RuntimeError, match="closed"):
        if isinstance(client, onyxweb.AsyncClient):
            await client.fetch("data:text/html,x")
        else:
            client.fetch("data:text/html,x")


@needs_proc
def test_close_is_bounded_when_chrome_stops_responding(
    capfd: pytest.CaptureFixture[str],
) -> None:
    """A wedged Chrome can't hold close hostage: the whole shutdown shares one budget."""
    before = _chrome_children()
    client = onyxweb.Client(concurrency=1)
    launched = _chrome_children() - before
    assert launched, "expected this Client to start a Chrome process"
    for pid in launched:
        os.kill(pid, signal.SIGSTOP)  # a frozen Chrome never answers shutdown commands
    # Close on a thread: an unbounded close then fails fast instead of hanging the run.
    closer = threading.Thread(target=client.close, daemon=True)
    try:
        closer.start()
        closer.join(CLOSE_TIMEOUT_S + 1.0)
        finished = not closer.is_alive()
    finally:
        for pid in launched:
            os.kill(pid, signal.SIGKILL)  # also unblocks a close that overran
        closer.join(5.0)
    assert finished, f"close() still running {CLOSE_TIMEOUT_S + 1.0} s after Chrome froze"
    # The warning proves the budget was actually hit, so the bound above isn't vacuous.
    assert "did not shut down" in capfd.readouterr().err


DEAD_CALL_S = 1.0  # a call on a dead Chrome must fail at once, not wait out a timeout
URL = "data:text/html,x"


async def _ready(value: object) -> object:
    """Await a call's result when it is awaitable, so sync and async clients read alike."""
    return await value if inspect.isawaitable(value) else value


async def _batch_failure(client: Any, url: str) -> object:
    """``batch`` returns a failure in place; raise it so every row reads alike."""
    items: Any = await _ready(client.batch([url]))
    if isinstance(items[0], Exception):
        raise items[0]
    return items[0]


# Every call shape, on either client class. All must name a dead Chrome.
DEAD_CALLS: dict[str, Callable[[Any, str], object]] = {
    "fetch": lambda c, u: c.fetch(u),
    "screenshot": lambda c, u: c.screenshot(u),
    "fetch_all": lambda c, u: c.fetch_all(u),
    "batch": _batch_failure,
}


async def _assert_dead_chrome_is_named(client: Any) -> None:
    """Each call shape, three times over, raises ``ChromeExitedError`` at once.

    Repeats matter: a failed tab recreation once left the pool empty, so the third call panicked.
    """
    for call in DEAD_CALLS.values():
        for _ in range(3):
            started = time.perf_counter()
            with pytest.raises(BaseException) as exc:  # a panic is a BaseException, so it shows
                await _ready(call(client, URL))
            assert time.perf_counter() - started < DEAD_CALL_S
            err = exc.value
            assert isinstance(err, onyxweb.ChromeExitedError), f"{type(err).__name__}: {err}"
            assert (err.kind, err.url) == ("chrome_exited", URL)
            assert "exited" in str(err) and "create a new" in str(err), str(err)


def _chrome_tree(root_pid: int) -> set[int]:
    """Every live Chrome process descended from `root_pid` (`psutil`, so this runs on
    all 3 platforms — unlike `_chrome_children`, `root_pid` also need not be this test
    process itself; the owning process below is a subprocess two levels removed).
    """
    try:
        root = psutil.Process(root_pid)
        candidates = [root, *root.children(recursive=True)]
    except psutil.NoSuchProcess:
        return set()
    tree: set[int] = set()
    for p in candidates:
        with contextlib.suppress(psutil.NoSuchProcess, psutil.ZombieProcess, psutil.AccessDenied):
            if "chrome" in p.name().lower():
                tree.add(p.pid)
    return tree


def _all_gone(pids: set[int], within_s: float) -> bool:
    deadline = time.monotonic() + within_s
    while time.monotonic() < deadline:
        if not any(psutil.pid_exists(pid) for pid in pids):
            return True
        time.sleep(0.05)
    return False


def test_chrome_tree_does_not_survive_an_abrupt_kill_of_its_owning_process(
    tmp_path: Path,
) -> None:
    """Killing only the process holding a Client — no chance for any cleanup code to
    run, unlike closing the process group — must not orphan Chrome's whole tree.

    Regression for a real process leak: `Browser`'s own `Drop`/`kill_on_drop` need the
    owning process's Rust runtime to get a turn, which an abrupt kill never gives.
    New test — nothing else in this suite kills an *external* process and checks
    OS-level survival of what it spawned.
    """
    script = tmp_path / "spawn_client.py"
    ready = tmp_path / "ready"
    script.write_text(
        "import onyxweb\n"
        "c = onyxweb.Client(concurrency=1)\n"
        "c.fetch('data:text/html,<html></html>')\n"
        f"open({str(ready)!r}, 'w').close()\n"
        "import time; time.sleep(60)\n"
    )
    proc = subprocess.Popen([sys.executable, str(script)])
    try:
        deadline = time.monotonic() + 15.0
        while not ready.exists():
            assert time.monotonic() < deadline, "Client() in the subprocess never became ready"
            time.sleep(0.1)
        time.sleep(0.5)  # let the pool's tab finish opening (zygote/GPU/renderer too)
        tree = _chrome_tree(proc.pid)
        assert tree, "expected the subprocess to have a live Chrome process tree"
        proc.kill()  # only the owning process — SIGKILL on POSIX, TerminateProcess on Windows
        assert _all_gone(tree, within_s=5.0), f"orphaned Chrome survived: {tree}"
    finally:
        proc.wait(timeout=5)
        for pid in _chrome_tree(proc.pid):
            with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                psutil.Process(pid).kill()


def test_chrome_found_only_on_path_is_resolved(tmp_path: Path) -> None:
    """The last resolution stage: a Chrome present only on ``PATH`` is found on every OS.

    An instantly-exiting stub stands in, so a found Chrome fails to launch rather than
    "not found". Skips where a system Chrome shadows ``PATH``. New test — nothing else
    reaches this stage, which needs no bundled Chrome.
    """
    windows = sys.platform == "win32"
    exits_at_once = shutil.which("hostname" if windows else "true")
    assert exits_at_once, "no stub binary to copy"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shutil.copy(exits_at_once, bin_dir / ("chrome.exe" if windows else "chrome"))
    probe = (
        "import onyxweb\n"
        "try:\n"
        "    onyxweb.Client(launch_timeout_ms=5000)\n"
        "except (onyxweb.OnyxwebError, TimeoutError) as e:\n"
        "    print(e)\n"
        "else:\n"
        "    print('launched')\n"
    )
    env = {
        **os.environ,
        "ONYXWEB_PKG_DIR": str(tmp_path / "no-bundle"),
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
    }
    out = subprocess.run(
        [sys.executable, "-c", probe], env=env, capture_output=True, text=True, timeout=60
    )
    said = out.stdout.strip()
    assert said, out.stderr
    if said == "launched":
        pytest.skip("a system Chrome shadows PATH")
    assert "not found" not in said.lower(), f"PATH was not searched: {said}"


# --- installing ----------------------------------------------------------------

_PLATFORM = "linux_x86_64"
_SHELL_ZIP_BASE = "chrome-headless-shell-linux64"


def _fake_download_for(record: dict[str, object]) -> Callable[..., Path]:
    def fake(
        internal_key: str,
        *,
        engine: str,
        dest_root: Path,
        force: bool = False,
        verbose: bool = True,
    ) -> Path:
        record.update(internal_key=internal_key, engine=engine, dest_root=dest_root, force=force)
        record["thread"] = threading.current_thread()
        return dest_root / internal_key / "chrome-headless-shell"

    return fake


# ensure_chrome kwargs (given tmp_path) -> what download_for must receive.
# dest_root "tmp" is tmp_path resolved; "default" is the package _binaries dir.
ENSURE_ARGS: dict[str, tuple[Callable[[Path], dict[str, Any]], str, str, bool]] = {
    "dest_as_path": (lambda tmp: {"dest": tmp, "engine": "shell"}, "shell", "tmp", False),
    "dest_as_str": (lambda tmp: {"dest": str(tmp)}, "shell", "tmp", False),
    "default_dest": (lambda tmp: {}, "shell", "default", False),
    "force": (lambda tmp: {"dest": tmp, "force": True}, "shell", "tmp", True),
    "full_engine": (lambda tmp: {"dest": tmp, "engine": "full"}, "full", "tmp", False),
}


@pytest.mark.parametrize("name", list(ENSURE_ARGS))
def test_ensure_chrome_passes_its_arguments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str
) -> None:
    kwargs, engine, dest, force = ENSURE_ARGS[name]
    monkeypatch.delenv("ONYXWEB_CHROME__ENGINE", raising=False)  # the default engine reads it
    rec: dict[str, object] = {}
    monkeypatch.setattr(dl, "download_for", _fake_download_for(rec))
    out = onyxweb.ensure_chrome(**kwargs(tmp_path))
    dest_root = tmp_path.resolve() if dest == "tmp" else dl.default_dest_dir().resolve()
    assert (rec["engine"], rec["dest_root"], rec["force"]) == (engine, dest_root, force)
    assert out == dest_root / dl.current_platform_key() / "chrome-headless-shell"


async def test_aensure_chrome_offloads_and_returns_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rec: dict[str, object] = {}
    monkeypatch.setattr(dl, "download_for", _fake_download_for(rec))
    out = await onyxweb.aensure_chrome(dest=tmp_path, engine="full")
    assert isinstance(out, Path)
    assert rec["engine"] == "full"
    # asyncio.to_thread ran the blocking download off the event-loop thread.
    assert rec["thread"] is not threading.current_thread()


class _FakeResp:
    """Minimal urlopen stand-in: a context manager yielding fixed zip bytes."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self.headers: dict[str, str] = {"Content-Length": str(len(data))}
        self._pos = 0

    def read(self, n: int = -1) -> bytes:
        end = self._pos + n if n and n > 0 else len(self._data)
        chunk = self._data[self._pos : end]
        self._pos += len(chunk)
        return chunk

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def _serve(data: bytes, record: dict[str, object] | None = None) -> Callable[..., _FakeResp]:
    def fake(url: str, timeout: float | None = None) -> _FakeResp:
        if record is not None:
            record["url"], record["timeout"] = url, timeout
        return _FakeResp(data)

    return fake


def _refuse(url: str, timeout: float | None = None) -> _FakeResp:
    raise urllib.error.URLError("name resolution failed")


# Platform key, urlopen stand-in, message fragment. Every row also has a prior install.
INSTALL_FAILURES: dict[str, tuple[str, Callable[..., _FakeResp], str]] = {
    # A URLError is an OSError; a host app should catch one onyxweb type, not urllib's.
    "network_error": (_PLATFORM, _refuse, "failed to install"),
    "corrupt_archive": (_PLATFORM, _serve(b"this is not a zip"), "failed to install"),
    "zip_slip_member": (
        _PLATFORM,
        _serve(_zip_bytes({f"{_SHELL_ZIP_BASE}/../../evil": b"pwned"})),
        "unsafe path",
    ),
    # Chrome for Testing publishes no linux-arm64 build; say what to do instead.
    "unsupported_platform": ("linux_aarch64", _refuse, "linux-arm64"),
}


@pytest.mark.parametrize("name", list(INSTALL_FAILURES))
def test_install_failure_is_contained(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str
) -> None:
    platform, urlopen, says = INSTALL_FAILURES[name]
    prior = tmp_path / platform / "chrome-headless-shell"
    prior.parent.mkdir(parents=True)
    prior.write_bytes(b"PRIOR")
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    with pytest.raises(onyxweb.OnyxwebDownloadError, match=says) as exc:
        dl.download_for(platform, engine="shell", dest_root=tmp_path, force=True, verbose=False)
    assert isinstance(exc.value, onyxweb.OnyxwebError) and isinstance(exc.value, RuntimeError)
    assert prior.read_bytes() == b"PRIOR", "a failed install damaged the prior one"


def test_install_extracts_with_a_timeout_and_keeps_the_other_engine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A forced shell install bounds its socket and leaves a full Chrome beside it intact.

    Both engines share the platform dir (full lives in ``full/``), and an install used
    to wipe it; a download without a socket timeout could hang forever. The bundled
    ``wrapper/`` subdir must survive too: this sweep once deleted a flat-placed wrapper.
    """
    full_chrome = tmp_path / _PLATFORM / "full" / "chrome"
    full_chrome.parent.mkdir(parents=True)
    full_chrome.write_bytes(b"FULL_CHROME")
    wrapper = tmp_path / _PLATFORM / "wrapper" / "onyxweb_wrapper"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_bytes(b"WRAPPER")
    rec: dict[str, object] = {}
    archive = _zip_bytes(
        {
            f"{_SHELL_ZIP_BASE}/chrome-headless-shell": b"SHELL",
            f"{_SHELL_ZIP_BASE}/icudtl.dat": b"ICU",
        }
    )
    monkeypatch.setattr(urllib.request, "urlopen", _serve(archive, rec))
    out = dl.download_for(_PLATFORM, engine="shell", dest_root=tmp_path, force=True, verbose=False)
    assert rec["timeout"] == dl.DOWNLOAD_TIMEOUT_S
    assert out == tmp_path / _PLATFORM / "chrome-headless-shell"
    assert out.read_bytes() == b"SHELL"
    assert (tmp_path / _PLATFORM / "icudtl.dat").read_bytes() == b"ICU"
    assert full_chrome.read_bytes() == b"FULL_CHROME"
    assert wrapper.read_bytes() == b"WRAPPER"


def test_download_engine_specs() -> None:
    """The downloader's per-engine layout must match the Rust resolver — full
    puts the binary in a ``full/chrome`` subdir; shell keeps it flat."""
    assert dl._engine_download("full", "linux64") == ("chrome-linux64", "chrome", "full")
    assert dl._engine_download("shell", "linux64") == (
        "chrome-headless-shell-linux64",
        "chrome-headless-shell",
        "",
    )
    assert dl._engine_download("full", "win64")[1] == "chrome.exe"
    with pytest.raises(ValueError):
        dl._engine_download("bogus", "linux64")


def _unknown_platform() -> str:
    raise onyxweb.OnyxwebDownloadError("unsupported host platform: FreeBSD/amd64")


@pytest.mark.parametrize("state", ["absent", "present", "unknown_platform"])
def test_find_chrome(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, state: str) -> None:
    """``find_chrome`` answers "installed?" with a path or None, and never raises."""
    if state == "unknown_platform":
        # A host app calls it to decide whether to download, on hosts we can't map too.
        monkeypatch.setattr(dl, "current_platform_key", _unknown_platform)
        assert onyxweb.find_chrome(dest=tmp_path) is None
        return
    key = dl.current_platform_key()
    cft = dl.CFT_PLATFORM.get(key)
    if cft is None:
        pytest.skip("no downloadable build for this platform")
    _, binary_name, _ = dl._engine_download("shell", cft)
    binary = tmp_path / key / binary_name
    if state == "present":
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"x")
    expected = binary if state == "present" else None
    assert onyxweb.find_chrome(dest=tmp_path, engine="shell") == expected
