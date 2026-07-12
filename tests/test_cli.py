"""python -m onyxweb CLI surface."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

URL = "https://example.com"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"


def _run(args: list[str], **kw: Any) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, "-m", "onyxweb", *args],
        capture_output=True, text=False, timeout=60, **kw,
    )


def test_help_shows_usage() -> None:
    p = _run(["--help"])
    assert p.returncode == 0
    assert b"python -m onyxweb" in p.stdout


def test_version() -> None:
    p = _run(["--version"])
    assert p.returncode == 0
    assert p.stdout.strip()  # some version string


def test_no_args_errors() -> None:
    p = _run([])
    assert p.returncode != 0
    assert b"URL is required" in p.stderr or b"usage" in p.stderr


def test_preset_list_prints_known_presets() -> None:
    p = _run(["--preset", "list"])
    assert p.returncode == 0, p.stderr
    out = p.stdout.decode()
    assert "shell.stealth.BASIC" in out
    assert "shell.stealth.FINGERPRINT" in out
    assert "shell.recon.FAST" in out
    assert "shell.archival.FULL_PAGE" in out
    assert "full.stealth.BASIC" in out


def test_preset_unknown_errors_cleanly() -> None:
    p = _run(["--preset", "stealth.NOPE", URL])
    assert p.returncode != 0
    assert b"unknown preset" in p.stderr


def test_preset_malformed_errors_cleanly() -> None:
    p = _run(["--preset", "stealthBASIC", URL])  # missing dot
    assert p.returncode != 0
    # argparse error-exit path prints usage + the message
    assert b"engine.purpose.NAME" in p.stderr or b"stealth.BASIC" in p.stderr


def test_preset_recon_fast_smoke_no_js(tmp_path: Path) -> None:
    """shell.recon.FAST sets javascript_enabled=False; we can't easily assert
    that end-to-end without a page that depends on JS, but we can at least
    confirm the preset spreads and a fetch succeeds."""
    out = tmp_path / "page.html"
    p = _run(["--preset", "shell.recon.FAST", URL, "-o", str(out)])
    assert p.returncode == 0, p.stderr
    assert out.exists()
    assert "Example Domain" in out.read_text()


def test_fetch_url_stdout_is_html() -> None:
    p = _run([URL])
    assert p.returncode == 0, p.stderr
    html = p.stdout.decode()
    assert "Example Domain" in html
    # Should be real HTML, not JSON
    assert not html.strip().startswith("{")


def test_json_mode_valid_json() -> None:
    p = _run([URL, "--json"])
    assert p.returncode == 0, p.stderr
    data = json.loads(p.stdout)
    assert data["status_code"] == 200
    assert data["final_url"].startswith("https://example.com")
    assert "Example Domain" in data["html"]
    assert isinstance(data["errors"], list)


def test_meta_goes_to_stderr() -> None:
    p = _run([URL, "--meta"])
    assert p.returncode == 0, p.stderr
    assert "Example Domain" in p.stdout.decode()
    assert b"status=200" in p.stderr
    assert b"final_url=" in p.stderr


def test_screenshot_writes_file(tmp_path: Path) -> None:
    out = tmp_path / "shot.png"
    p = _run([URL, "--screenshot", str(out)])
    assert p.returncode == 0, p.stderr
    assert out.exists()
    assert out.read_bytes()[:8] == PNG_MAGIC
    # HTML still goes to stdout
    assert "Example Domain" in p.stdout.decode()


def test_screenshot_only_suppresses_html(tmp_path: Path) -> None:
    out = tmp_path / "shot.png"
    p = _run([URL, "--screenshot-only", str(out)])
    assert p.returncode == 0, p.stderr
    assert out.exists()
    assert p.stdout == b""


def test_output_writes_html_to_file(tmp_path: Path) -> None:
    out = tmp_path / "page.html"
    p = _run([URL, "-o", str(out)])
    assert p.returncode == 0, p.stderr
    assert out.exists()
    assert "Example Domain" in out.read_text()
    # stdout should be empty when writing to file
    assert p.stdout == b""


def test_output_dash_is_stdout(tmp_path: Path) -> None:
    """-o - means 'explicitly stdout' — same as no -o flag."""
    p = _run([URL, "-o", "-"])
    assert p.returncode == 0, p.stderr
    assert "Example Domain" in p.stdout.decode()


def test_output_and_screenshot_both_written(tmp_path: Path) -> None:
    html_out = tmp_path / "page.html"
    png_out = tmp_path / "page.png"
    p = _run([URL, "-o", str(html_out), "-s", str(png_out)])
    assert p.returncode == 0, p.stderr
    assert html_out.exists()
    assert "Example Domain" in html_out.read_text()
    assert png_out.read_bytes()[:8] == PNG_MAGIC
    # When -o is given, stdout is suppressed
    assert p.stdout == b""


def test_screenshot_only_conflicts_with_output() -> None:
    p = _run([URL, "--screenshot-only", "/tmp/x", "-o", "/tmp/y"])
    assert p.returncode != 0
    assert b"mutually exclusive" in p.stderr


def test_screenshot_only_conflicts_with_json() -> None:
    p = _run([URL, "--screenshot-only", "/tmp/x", "--json"])
    assert p.returncode != 0
    assert b"mutually exclusive" in p.stderr


def test_header_flag() -> None:
    """Verify -H flag is accepted (we don't verify the header actually reached
    the server — that needs httpbin, which our suite avoids for stability)."""
    p = _run([URL, "-H", "X-Foo: bar", "-H", "X-Baz=qux"])
    assert p.returncode == 0, p.stderr


def test_width_height_flags(tmp_path: Path) -> None:
    out = tmp_path / "shot.png"
    p = _run([URL, "--screenshot-only", str(out), "--width", "400", "--height", "300"])
    assert p.returncode == 0, p.stderr
    assert out.exists()
    # PNG header parse — IHDR tells us dimensions
    import struct
    data = out.read_bytes()
    assert data[:8] == PNG_MAGIC
    # IHDR chunk starts at offset 8+4+4 = 16 (png_magic + chunk_len + chunk_type)
    w = struct.unpack(">I", data[16:20])[0]
    h = struct.unpack(">I", data[20:24])[0]
    assert w == 400
    assert h == 300


def test_bad_url_exits_nonzero() -> None:
    p = _run(["https://this-does-not-exist-onyxweb-cli-test.invalid"])
    assert p.returncode != 0
    # Should have an error message on stderr
    assert p.stderr  # non-empty


def test_format_infers_jpeg_from_extension(tmp_path: Path) -> None:
    out = tmp_path / "shot.jpg"
    p = _run([URL, "--screenshot-only", str(out)])
    assert p.returncode == 0, p.stderr
    assert out.read_bytes()[:3] == JPEG_MAGIC


def test_format_infers_webp_from_extension(tmp_path: Path) -> None:
    out = tmp_path / "shot.webp"
    p = _run([URL, "--screenshot-only", str(out)])
    assert p.returncode == 0, p.stderr
    data = out.read_bytes()
    assert data[:4] == b"RIFF"
    assert data[8:12] == b"WEBP"


def test_explicit_format_overrides_extension(tmp_path: Path) -> None:
    # .png extension but --format jpeg wins
    out = tmp_path / "shot.png"
    p = _run([URL, "--screenshot-only", str(out), "--format", "jpeg"])
    assert p.returncode == 0, p.stderr
    assert out.read_bytes()[:3] == JPEG_MAGIC


def test_jpeg_quality_affects_size(tmp_path: Path) -> None:
    hq = tmp_path / "hq.jpg"
    lq = tmp_path / "lq.jpg"
    p_hq = _run([URL, "--screenshot-only", str(hq), "--quality", "95"])
    p_lq = _run([URL, "--screenshot-only", str(lq), "--quality", "5"])
    assert p_hq.returncode == 0 and p_lq.returncode == 0
    assert lq.stat().st_size < hq.stat().st_size


def test_quality_out_of_range_rejected(tmp_path: Path) -> None:
    p = _run([URL, "--screenshot-only", str(tmp_path / "x.jpg"), "--quality", "150"])
    assert p.returncode != 0
    assert b"--quality" in p.stderr
