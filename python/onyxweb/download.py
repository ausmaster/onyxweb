"""Pinned Chrome-for-Testing downloader (engine-aware).

Fetches a Chromium build into ``python/onyxweb/_binaries/<platform>/`` so the
Rust binary resolver finds it. Two engines (see ``ChromeConfig.engine``):

- ``shell`` → ``chrome-headless-shell`` (flat: ``_binaries/<plat>/``)
- ``full`` → full Chrome (``_binaries/<plat>/full/`` — a subdir so its support
  files don't collide with the shell's)

Exposed as the ``onyxweb-download-chrome`` console script::

    uv run onyxweb-download-chrome                 # default engine, current platform
    uv run onyxweb-download-chrome --engine full   # full Chrome
    uv run onyxweb-download-chrome --all            # every supported platform
    uv run onyxweb-download-chrome --force          # re-download even if present

Idempotent: skips if the binary is already present and non-empty. Bump
``CHROME_VERSION`` to upgrade the pinned build across all platforms/engines.
"""

from __future__ import annotations

import argparse
import asyncio
import platform
import shutil
import stat
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from onyxweb._onyxweb import OnyxwebError


class OnyxwebDownloadError(OnyxwebError):
    """Chrome download / install failure.

    Raised for every expected failure of the install path — unsupported /
    non-downloadable platform, network (DNS / 404 / timeout), permissions, disk,
    or a corrupt / unsafe archive. Subclasses ``onyxweb.OnyxwebError`` (itself a
    ``RuntimeError``), so a host app can branch on this one type
    (``except onyxweb.OnyxwebError``) instead of a grab-bag of ``OSError`` /
    ``zipfile.BadZipFile`` / ``RuntimeError``. Programming errors (``ImportError``,
    ``ValueError``, …) deliberately escape unwrapped.
    """


# Pinned Chrome for Testing version. Find current URLs at:
#   https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json
CHROME_VERSION = "148.0.7778.56"

# CDN: https://storage.googleapis.com/chrome-for-testing-public/<version>/<cft_plat>/<zip>
CDN_BASE = "https://storage.googleapis.com/chrome-for-testing-public"

# internal_key (matches Rust `chrome::platform_subdir()`) → Chrome-for-Testing slug
CFT_PLATFORM: dict[str, str] = {
    "linux_x86_64": "linux64",
    "darwin_x86_64": "mac-x64",
    "darwin_aarch64": "mac-arm64",
    "windows_x86_64": "win64",
}

ENGINES = ("shell", "full")

# Per-socket-op timeout for the CDN fetch (connect + each read), NOT a total cap
# — a healthy download makes steady progress. Without it urllib blocks forever on
# a stalled connection, hanging a caller's setup (e.g. BBOT's setup_deps) silently.
DOWNLOAD_TIMEOUT_S = 30


def _default_engine() -> str:
    """The engine onyxweb drives by default (mirrors ``ChromeConfig.engine``)."""
    from onyxweb.config import ChromeConfig

    return ChromeConfig().engine


def _engine_download(engine: str, cft_plat: str) -> tuple[str, str, str]:
    """Return ``(zip_base, binary_name, dest_subdir)`` for an engine + platform.

    ``zip_base`` is both the ``.zip`` filename stem and its top-level directory.
    ``dest_subdir`` is "" (flat) for the shell, "full" for full Chrome.
    """
    is_win = cft_plat.startswith("win")
    if engine == "shell":
        binary = "chrome-headless-shell.exe" if is_win else "chrome-headless-shell"
        return f"chrome-headless-shell-{cft_plat}", binary, ""
    if engine == "full":
        return f"chrome-{cft_plat}", "chrome.exe" if is_win else "chrome", "full"
    raise ValueError(f"unknown engine {engine!r}; expected one of {ENGINES}")


def current_platform_key() -> str:
    """Return the internal_key matching the host OS+arch."""
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Linux" and machine in {"x86_64", "amd64"}:
        return "linux_x86_64"
    if system == "Linux" and machine in {"aarch64", "arm64"}:
        return "linux_aarch64"
    if system == "Darwin" and machine == "x86_64":
        return "darwin_x86_64"
    if system == "Darwin" and machine in {"arm64", "aarch64"}:
        return "darwin_aarch64"
    if system == "Windows" and machine in {"amd64", "x86_64"}:
        return "windows_x86_64"
    raise OnyxwebDownloadError(
        f"unsupported host platform: {system}/{machine}. Pass chrome_path= to use "
        "a system Chromium."
    )


def download_for(
    internal_key: str,
    *,
    engine: str,
    dest_root: Path,
    force: bool = False,
    verbose: bool = True,
) -> Path:
    """Download + extract the given engine's build for ``internal_key``.

    Returns the path to the installed binary.
    """
    cft_plat = CFT_PLATFORM.get(internal_key)
    if cft_plat is None:
        if internal_key == "linux_aarch64":
            raise OnyxwebDownloadError(
                "Chrome for Testing publishes no linux-arm64 build. Install a system "
                "chromium (e.g. `apt install chromium`) and point onyxweb at it with "
                "Client(chrome_path=...) or ONYXWEB_CHROME__PATH=..."
            )
        raise OnyxwebDownloadError(
            f"no Chrome-for-Testing download for platform {internal_key!r} "
            f"(downloadable: {list(CFT_PLATFORM)}). Pass chrome_path= to use a "
            "system Chromium."
        )
    zip_base, binary_name, dest_sub = _engine_download(engine, cft_plat)

    dest_dir = dest_root / internal_key
    if dest_sub:
        dest_dir = dest_dir / dest_sub
    dest_bin = dest_dir / binary_name

    if dest_bin.is_file() and dest_bin.stat().st_size > 0 and not force:
        if verbose:
            print(
                f"  [{internal_key}/{engine}] already present at {dest_bin} — skip "
                "(pass --force to re-download)."
            )
        return dest_bin

    url = f"{CDN_BASE}/{CHROME_VERSION}/{cft_plat}/{zip_base}.zip"
    if verbose:
        print(f"  [{internal_key}/{engine}] downloading {url}")

    # Extract into a staging dir on the SAME filesystem as the target, then swap
    # per-file. A partial/interrupted extraction never leaves a half-installed
    # dest that a later run would skip (binary present but support files missing
    # → chrome fails at launch); the `finally` deletes the staging dir on abort.
    tmp_path: Path | None = None
    staging: Path | None = None
    try:
        dest_root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".onyxweb-stage-", dir=dest_root))
        staging_root = staging.resolve()
        # Stream to a tempfile so we don't buffer 100+MB in memory.
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_S) as resp, open(
            tmp_path, "wb"
        ) as out:
            total = int(resp.headers.get("Content-Length", 0))
            chunk = 1024 * 1024
            downloaded = 0
            last_pct = -10
            while True:
                buf = resp.read(chunk)
                if not buf:
                    break
                out.write(buf)
                downloaded += len(buf)
                if total and verbose:
                    pct = int(downloaded * 100 / total)
                    if pct >= last_pct + 10:
                        print(
                            f"  [{internal_key}/{engine}] {pct}% "
                            f"({downloaded // (1024 * 1024)}/"
                            f"{total // (1024 * 1024)} MB)"
                        )
                        last_pct = pct

        if verbose:
            print(f"  [{internal_key}/{engine}] extracting...")

        with zipfile.ZipFile(tmp_path) as zf:
            # Archive layout: <zip_base>/<files>. Flatten into the staging dir.
            for member in zf.namelist():
                rel = member
                if rel.startswith(zip_base + "/"):
                    rel = rel[len(zip_base) + 1:]
                if not rel or rel.endswith("/"):
                    continue
                target = (staging / rel).resolve()
                # Zip-slip guard: reject any member that escapes the staging dir.
                if not target.is_relative_to(staging_root):
                    raise OnyxwebDownloadError(f"unsafe path in archive: {member!r}")
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                # Preserve executable bit if it was set in the zip.
                if (zf.getinfo(member).external_attr >> 16) & 0o111:
                    target.chmod(target.stat().st_mode | 0o755)

        # Always ensure the main binary is executable (some zips lose the bit).
        staged_bin = staging / binary_name
        if staged_bin.is_file():
            staged_bin.chmod(
                staged_bin.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            )

        # Swap staged files into dest_dir, replacing this engine's files but
        # PRESERVING the sibling engine: shell and full share the platform dir
        # (shell flat in it, full in the `full/` subdir), so a (re)install of one
        # must not delete the other. dest_sub is "" for shell, "full" for full.
        preserve = set() if dest_sub else {"full"}
        dest_dir.mkdir(parents=True, exist_ok=True)
        for existing in list(dest_dir.iterdir()):
            if existing.name in preserve:
                continue
            if existing.is_dir():
                shutil.rmtree(existing)
            else:
                existing.unlink()
        for item in list(staging.iterdir()):
            shutil.move(str(item), str(dest_dir / item.name))

        size_mb = dest_bin.stat().st_size / (1024 * 1024)
        if verbose:
            print(f"  [{internal_key}/{engine}] ok — {dest_bin} ({size_mb:.0f} MB)")
        return dest_bin
    except (OSError, zipfile.BadZipFile) as e:
        # Wrap the whole I/O + archive surface (permissions, disk, and network —
        # URLError / HTTPError / timeout are all OSError in 3.10+ — plus a
        # truncated or corrupt zip) into one type. The platform + zip-slip raises
        # above are OnyxwebDownloadError already and pass straight through.
        raise OnyxwebDownloadError(
            f"failed to install {engine} Chrome for {internal_key}: {e}"
        ) from e
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)


def default_dest_dir() -> Path:
    """Default install destination next to this module.

    Resolves to ``<site-packages>/onyxweb/_binaries/`` for normal installs.
    """
    return Path(__file__).resolve().parent / "_binaries"


def find_chrome(
    *, engine: str | None = None, dest: Path | str | None = None
) -> Path | None:
    """Path to the installed Chrome binary for this platform, or ``None`` if absent.

    A cheap predicate (no network) so a host app can decide whether to announce a
    first-run download before calling :func:`ensure_chrome`. Checks only the
    install location (the same one :func:`ensure_chrome` writes to); it does NOT
    consult a system chromium or ``PATH`` — the Rust resolver does that at Client
    launch. Returns ``None`` on platforms with no downloadable build (e.g.
    linux-arm64), where pointing ``chrome_path=`` at a system Chromium is the path.

    Args:
        engine: ``"shell"`` (default) or ``"full"``; defaults to the configured engine.
        dest: Install root to check. Default: ``<installed package>/_binaries``.

    Returns:
        The binary ``Path`` if present and non-empty, else ``None``.
    """
    engine = engine or _default_engine()
    try:
        internal_key = current_platform_key()
    except OnyxwebDownloadError:
        return None  # host platform current_platform_key() can't map → nothing here
    cft_plat = CFT_PLATFORM.get(internal_key)
    if cft_plat is None:
        return None
    _, binary_name, dest_sub = _engine_download(engine, cft_plat)
    dest_root = (Path(dest) if dest is not None else default_dest_dir()).resolve()
    dest_dir = dest_root / internal_key
    if dest_sub:
        dest_dir = dest_dir / dest_sub
    dest_bin = dest_dir / binary_name
    return dest_bin if (dest_bin.is_file() and dest_bin.stat().st_size > 0) else None


def install_chrome(
    *,
    engine: str | None = None,
    dest: Path | None = None,
    force: bool = False,
    platform_key: str | None = None,
    all_platforms: bool = False,
) -> int:
    """Fetch a Chromium build. Returns a CLI-style exit code (0 = success).

    ``engine`` defaults to onyxweb's configured default (see
    ``ChromeConfig.engine``). Shared by the ``onyxweb-download-chrome`` console
    script and the ``onyxweb --install`` CLI flag — kept callable (no argparse)
    so both can invoke it without fighting over ``sys.argv``.
    """
    engine = engine or _default_engine()
    dest = (dest or default_dest_dir()).resolve()
    dest.mkdir(parents=True, exist_ok=True)

    if platform_key:
        targets = [platform_key]
    elif all_platforms:
        targets = list(CFT_PLATFORM)
    else:
        targets = [current_platform_key()]

    print(f"Chrome version: {CHROME_VERSION}")
    print(f"Engine:         {engine}")
    print(f"Destination:    {dest}")
    print(f"Platforms:      {targets}")
    for t in targets:
        download_for(t, engine=engine, dest_root=dest, force=force)

    print("done.")
    return 0


def ensure_chrome(
    *,
    engine: str | None = None,
    dest: Path | str | None = None,
    force: bool = False,
    verbose: bool = False,
) -> Path:
    """Ensure the pinned Chrome build for this platform is present; return its path.

    Idempotent: skips the download when the binary is already installed (unless
    ``force``). Downloads only for the current platform. Point ``dest`` somewhere
    other than the package's ``_binaries`` dir (e.g. a host app's tools folder)
    and pass the returned path to ``Client(chrome_path=...)``.

    Args:
        engine: ``"shell"`` (default) or ``"full"``; defaults to onyxweb's
            configured engine (see ``ChromeConfig.engine``).
        dest: Install root. Default: ``<installed package>/_binaries``.
        force: Re-download even if the binary is already present.
        verbose: Print download progress (off by default for library use).

    Returns:
        Path to the installed Chrome binary.

    Raises:
        OnyxwebDownloadError: On any expected failure (unsupported platform,
            network, permissions, disk, corrupt/unsafe archive).
    """
    engine = engine or _default_engine()
    dest_root = (Path(dest) if dest is not None else default_dest_dir()).resolve()
    return download_for(
        current_platform_key(),
        engine=engine,
        dest_root=dest_root,
        force=force,
        verbose=verbose,
    )


async def aensure_chrome(
    *,
    engine: str | None = None,
    dest: Path | str | None = None,
    force: bool = False,
    verbose: bool = False,
) -> Path:
    """Async :func:`ensure_chrome` — offloads the blocking download to a thread.

    Awaitable from an event loop (e.g. a BBOT module's ``setup_deps``) without
    blocking it. Same arguments and return value as :func:`ensure_chrome`.
    """
    return await asyncio.to_thread(
        ensure_chrome, engine=engine, dest=dest, force=force, verbose=verbose
    )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--engine", choices=ENGINES, default=None,
        help="which build to fetch (default: onyxweb's configured engine)",
    )
    p.add_argument(
        "--dest", default=None,
        help="Destination dir (default: <installed package>/_binaries)",
    )
    p.add_argument(
        "--all", action="store_true",
        help="Download for every supported platform, not just the current one",
    )
    p.add_argument(
        "--platform",
        help="Internal platform key to download (overrides --all)",
    )
    p.add_argument(
        "--force", action="store_true",
        help="Re-download even if binary is already present",
    )
    args = p.parse_args()

    return install_chrome(
        engine=args.engine,
        dest=Path(args.dest) if args.dest else None,
        force=args.force,
        platform_key=args.platform,
        all_platforms=args.all,
    )


if __name__ == "__main__":
    sys.exit(main())
