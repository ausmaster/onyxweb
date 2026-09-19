"""Splice the platform's onyxweb_wrapper binary into a just-built wheel.

Maturin can't bundle a second compiled binary beside the pyo3 cdylib in one wheel, so
CI builds the wrapper separately and this repacks it in via `wheel unpack`/`pack`
(which regenerates RECORD hashes). It lands in its own `wrapper/` subdir, matching
`chrome::resolve_wrapper` in Rust, so `onyxweb --install` preserves it.

Usage:
    python scripts/inject_wrapper.py WHEEL WRAPPER_BINARY PLATFORM_SUBDIR WRAPPER_NAME
"""

from __future__ import annotations

import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


def main(argv: list[str]) -> int:
    """Splice `WRAPPER_BINARY` into `WHEEL` at `onyxweb/_binaries/PLATFORM_SUBDIR/wrapper/`."""
    if len(argv) != 4:
        print(
            "usage: inject_wrapper.py WHEEL WRAPPER_BINARY PLATFORM_SUBDIR WRAPPER_NAME",
            file=sys.stderr,
        )
        return 1
    wheel_path, wrapper_binary = Path(argv[0]), Path(argv[1])
    platform_subdir, wrapper_name = argv[2], argv[3]

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        subprocess.run(
            [sys.executable, "-m", "wheel", "unpack", str(wheel_path), "-d", str(tmp_path)],
            check=True,
        )
        (package_dir,) = tmp_path.iterdir()
        dest_dir = package_dir / "onyxweb" / "_binaries" / platform_subdir / "wrapper"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / wrapper_name
        shutil.copyfile(wrapper_binary, dest)
        # No-op on Windows; on Linux/macOS this is the bit `wheel pack` must preserve.
        dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        wheel_path.unlink()  # `wheel pack` writes the same filename back here
        subprocess.run(
            [sys.executable, "-m", "wheel", "pack", str(package_dir), "-d", str(wheel_path.parent)],
            check=True,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
