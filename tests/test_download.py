"""``ensure_chrome`` / ``aensure_chrome`` — the public, location-choosable,
async-friendly Chrome installer (the surface a host app like BBOT hooks into).

These monkeypatch the network worker (``download.download_for``) so no 180 MB
fetch happens; they assert the plumbing: dest/engine/force threaded through, a
``Path`` returned, the async variant offloads off the event loop, and the
default dest stays the package ``_binaries`` dir.
"""

from __future__ import annotations

import io
import threading
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from pathlib import Path

import onyxweb
import onyxweb.download as dl
import pytest


def _fake_download_for(record: dict[str, object]) -> Callable[..., Path]:
    def fake(
        internal_key: str,
        *,
        engine: str,
        dest_root: Path,
        force: bool = False,
        verbose: bool = True,
    ) -> Path:
        record.update(
            internal_key=internal_key, engine=engine, dest_root=dest_root, force=force
        )
        record["thread"] = threading.current_thread()
        return dest_root / internal_key / "chrome-headless-shell"

    return fake


def test_ensure_chrome_returns_binary_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rec: dict[str, object] = {}
    monkeypatch.setattr(dl, "download_for", _fake_download_for(rec))
    out = onyxweb.ensure_chrome(dest=tmp_path, engine="shell")
    assert isinstance(out, Path)
    assert rec["engine"] == "shell"
    assert rec["dest_root"] == tmp_path.resolve()
    assert out == tmp_path.resolve() / dl.current_platform_key() / "chrome-headless-shell"


def test_ensure_chrome_default_dest_is_package_binaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec: dict[str, object] = {}
    monkeypatch.setattr(dl, "download_for", _fake_download_for(rec))
    onyxweb.ensure_chrome()
    assert rec["dest_root"] == dl.default_dest_dir().resolve()


def test_ensure_chrome_default_engine_is_configured_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rec: dict[str, object] = {}
    monkeypatch.setattr(dl, "download_for", _fake_download_for(rec))
    onyxweb.ensure_chrome(dest=tmp_path)
    assert rec["engine"] == "shell"  # ChromeConfig().engine default


def test_ensure_chrome_force_threaded_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rec: dict[str, object] = {}
    monkeypatch.setattr(dl, "download_for", _fake_download_for(rec))
    onyxweb.ensure_chrome(dest=tmp_path, force=True)
    assert rec["force"] is True


def test_ensure_chrome_accepts_str_dest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rec: dict[str, object] = {}
    monkeypatch.setattr(dl, "download_for", _fake_download_for(rec))
    onyxweb.ensure_chrome(dest=str(tmp_path))  # str, not Path
    assert rec["dest_root"] == tmp_path.resolve()


async def test_aensure_chrome_offloads_and_returns_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rec: dict[str, object] = {}
    monkeypatch.setattr(dl, "download_for", _fake_download_for(rec))
    main_thread = threading.current_thread()
    out = await onyxweb.aensure_chrome(dest=tmp_path, engine="full")
    assert isinstance(out, Path)
    assert rec["engine"] == "full"
    # asyncio.to_thread ran the blocking work off the event-loop thread.
    assert rec["thread"] is not main_thread


# ---------------------------------------------------------------------------
# download_for robustness (review fixes): clear arm64 error, socket timeout,
# sibling-engine preservation on force reinstall, zip-slip guard.
# ---------------------------------------------------------------------------


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


def _zip_bytes(zip_base: str, members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in members.items():
            z.writestr(f"{zip_base}/{name}", data)
    return buf.getvalue()


def _serve(
    data: bytes, record: dict[str, object] | None = None
) -> Callable[..., _FakeResp]:
    def fake(url: str, timeout: float | None = None) -> _FakeResp:
        if record is not None:
            record["url"], record["timeout"] = url, timeout
        return _FakeResp(data)

    return fake


def test_download_error_is_onyxweb_error_subclass() -> None:
    # Host apps catch the base OnyxwebError (a RuntimeError); no wide tuple needed.
    assert issubclass(onyxweb.OnyxwebDownloadError, onyxweb.OnyxwebError)
    assert issubclass(onyxweb.OnyxwebDownloadError, RuntimeError)


def test_linux_aarch64_gives_clear_actionable_error() -> None:
    with pytest.raises(onyxweb.OnyxwebDownloadError, match="linux-arm64"):
        dl.download_for("linux_aarch64", engine="shell", dest_root=Path("/tmp/nope"))


def test_network_error_wrapped_as_onyxweb_download_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A URLError (DNS/connection failure) — an OSError subclass — surfaces as
    OnyxwebDownloadError, not the raw urllib type."""

    def boom(url: str, timeout: float | None = None) -> _FakeResp:
        raise urllib.error.URLError("name resolution failed")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(onyxweb.OnyxwebDownloadError, match="failed to install"):
        dl.download_for(
            "linux_x86_64", engine="shell", dest_root=tmp_path, force=True, verbose=False
        )


def test_corrupt_archive_wrapped_as_onyxweb_download_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A truncated/garbage download (zipfile.BadZipFile) is wrapped too."""
    monkeypatch.setattr(urllib.request, "urlopen", _serve(b"this is not a zip"))
    with pytest.raises(onyxweb.OnyxwebDownloadError):
        dl.download_for(
            "linux_x86_64", engine="shell", dest_root=tmp_path, force=True, verbose=False
        )


def test_download_passes_socket_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rec: dict[str, object] = {}
    z = _zip_bytes("chrome-headless-shell-linux64", {"chrome-headless-shell": b"BIN"})
    monkeypatch.setattr(urllib.request, "urlopen", _serve(z, rec))
    dl.download_for(
        "linux_x86_64", engine="shell", dest_root=tmp_path, force=True, verbose=False
    )
    assert rec["timeout"] == dl.DOWNLOAD_TIMEOUT_S


def test_force_shell_reinstall_preserves_full_engine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A forced shell (re)install must NOT wipe an installed full Chrome — they
    share the platform dir (full lives in a ``full/`` subdir)."""
    full_chrome = tmp_path / "linux_x86_64" / "full" / "chrome"
    full_chrome.parent.mkdir(parents=True)
    full_chrome.write_bytes(b"FULL_CHROME")
    z = _zip_bytes(
        "chrome-headless-shell-linux64",
        {"chrome-headless-shell": b"SHELL", "icudtl.dat": b"ICU"},
    )
    monkeypatch.setattr(urllib.request, "urlopen", _serve(z))
    out = dl.download_for(
        "linux_x86_64", engine="shell", dest_root=tmp_path, force=True, verbose=False
    )
    assert out == tmp_path / "linux_x86_64" / "chrome-headless-shell"
    assert out.read_bytes() == b"SHELL"
    assert (tmp_path / "linux_x86_64" / "icudtl.dat").read_bytes() == b"ICU"
    # The full engine survived the shell reinstall (the bug this guards).
    assert full_chrome.read_bytes() == b"FULL_CHROME"


def test_zip_slip_member_rejected_and_prior_install_untouched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prior = tmp_path / "linux_x86_64" / "chrome-headless-shell"
    prior.parent.mkdir(parents=True)
    prior.write_bytes(b"PRIOR")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("chrome-headless-shell-linux64/../../evil", b"pwned")
    monkeypatch.setattr(urllib.request, "urlopen", _serve(buf.getvalue()))
    with pytest.raises(onyxweb.OnyxwebDownloadError, match="unsafe path"):
        dl.download_for(
            "linux_x86_64", engine="shell", dest_root=tmp_path, force=True, verbose=False
        )
    # Extraction blew up in staging; the prior install is untouched.
    assert prior.read_bytes() == b"PRIOR"


def test_find_chrome_none_when_absent(tmp_path: Path) -> None:
    if dl.CFT_PLATFORM.get(dl.current_platform_key()) is None:
        pytest.skip("no downloadable build for this platform")
    assert onyxweb.find_chrome(dest=tmp_path) is None


def test_find_chrome_none_on_unknown_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    """The predicate must not raise on a host ``current_platform_key()`` can't map
    (FreeBSD, 32-bit, …) — a host app calls it to decide whether to download."""

    def _boom() -> str:
        raise onyxweb.OnyxwebDownloadError("unsupported host platform: FreeBSD/amd64")

    monkeypatch.setattr(dl, "current_platform_key", _boom)
    assert onyxweb.find_chrome() is None


def test_find_chrome_returns_path_when_present(tmp_path: Path) -> None:
    key = dl.current_platform_key()
    cft = dl.CFT_PLATFORM.get(key)
    if cft is None:
        pytest.skip("no downloadable build for this platform")
    _, binary_name, _ = dl._engine_download("shell", cft)
    binp = tmp_path / key / binary_name
    binp.parent.mkdir(parents=True)
    binp.write_bytes(b"x")
    assert onyxweb.find_chrome(dest=tmp_path, engine="shell") == binp
