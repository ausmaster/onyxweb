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
import platform
import shutil
import stat
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

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
    raise RuntimeError(f"unsupported host platform: {system}/{machine}")


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
        raise RuntimeError(
            f"no download config for platform {internal_key!r}. "
            f"Known: {list(CFT_PLATFORM)}"
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

    # Stream to a tempfile so we don't buffer 100+MB in memory.
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        with urllib.request.urlopen(url) as resp, open(tmp_path, "wb") as out:
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

        if dest_dir.exists():
            shutil.rmtree(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)

        if verbose:
            print(f"  [{internal_key}/{engine}] extracting...")

        with zipfile.ZipFile(tmp_path) as zf:
            # Archive layout: <zip_base>/<files>. Flatten into dest_dir/.
            for member in zf.namelist():
                rel = member
                if rel.startswith(zip_base + "/"):
                    rel = rel[len(zip_base) + 1:]
                if not rel or rel.endswith("/"):
                    continue
                target = dest_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                # Preserve executable bit if it was set in the zip.
                info = zf.getinfo(member)
                mode = info.external_attr >> 16
                if mode & 0o111:
                    target.chmod(target.stat().st_mode | 0o755)

        # Always ensure the main binary is executable (some zips lose the bit).
        if dest_bin.is_file():
            dest_bin.chmod(dest_bin.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        size_mb = dest_bin.stat().st_size / (1024 * 1024)
        if verbose:
            print(f"  [{internal_key}/{engine}] ok — {dest_bin} ({size_mb:.0f} MB)")
        return dest_bin
    finally:
        tmp_path.unlink(missing_ok=True)


def default_dest_dir() -> Path:
    """Default install destination next to this module.

    Resolves to ``<site-packages>/onyxweb/_binaries/`` for normal installs.
    """
    return Path(__file__).resolve().parent / "_binaries"


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
