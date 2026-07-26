"""CLI entry point — see ``python -m onyxweb --help`` for the full flag list.

  python -m onyxweb <URL>                              # HTML → stdout
  python -m onyxweb <URL> -o page.html                 # HTML → file
  python -m onyxweb <URL> -s shot.png                  # HTML → stdout, PNG → file
  python -m onyxweb <URL> --screenshot-only shot.webp  # image-only, HTML silenced
  python -m onyxweb <URL> --json                       # single-line JSON with metadata

Image format inferred from output extension (``.jpg`` / ``.jpeg`` → jpeg,
``.webp`` → webp, else png). Override with ``--format`` / ``--quality``.

Exit codes: 0 = success, 1 = bad arg, 2 = fetch error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import IO, Any, Literal, cast

import onyxweb


def _parse_header(s: str) -> tuple[str, str]:
    if ":" in s:
        k, _, v = s.partition(":")
    elif "=" in s:
        k, _, v = s.partition("=")
    else:
        raise argparse.ArgumentTypeError(
            f"--header must be KEY=VALUE or KEY:VALUE, got {s!r}"
        )
    return k.strip(), v.strip()


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m onyxweb",
        description="Fetch a URL, return fully-rendered HTML (post-JS) and/or a screenshot.",
    )
    p.add_argument("url", nargs="?", help="URL to fetch")
    p.add_argument("--version", action="store_true", help="print version and exit")
    p.add_argument(
        "--install", action="store_true",
        help=(
            "one-time setup: download the Chrome build for onyxweb's default "
            "engine (see chrome.engine) for this platform into the installed "
            "package. Run once after `uv tool install onyxweb` / `pipx install "
            "onyxweb` / `pip install onyxweb`. For a specific engine, --force, "
            "--all, or --platform, use `onyxweb-download-chrome`."
        ),
    )

    out = p.add_argument_group("output")
    out.add_argument(
        "--output", "-o", metavar="PATH",
        help="write HTML to PATH (stdout suppressed). Use '-' to force stdout.",
    )
    out.add_argument(
        "--screenshot", "-s", metavar="PATH",
        help="also capture a PNG screenshot to PATH",
    )
    out.add_argument(
        "--screenshot-only", metavar="PATH",
        help="capture screenshot to PATH; suppress HTML (image-only mode)",
    )
    out.add_argument(
        "--json", action="store_true",
        help="emit a single JSON object {html, errors, final_url, status_code, elapsed_s}",
    )
    out.add_argument(
        "--meta", action="store_true",
        help="print metadata (final_url, status_code, elapsed_s) to stderr",
    )

    cfg = p.add_argument_group("config")
    cfg.add_argument(
        "--preset", metavar="NAME",
        help=(
            "apply a preset bundle in 'engine.purpose.NAME' form (e.g. "
            "full.stealth.BASIC, shell.stealth.BASIC, shell.recon.FAST). "
            "Explicit flags below override preset fields. Use '--preset list' "
            "to print all known presets and exit."
        ),
    )
    cfg.add_argument("--user-agent", "-A", help="override User-Agent")
    cfg.add_argument("--width", type=int, default=None, help="viewport width (default: 1200)")
    cfg.add_argument("--height", type=int, default=None, help="viewport height (default: 800)")
    cfg.add_argument(
        "--timeout-ms", type=int, default=None,
        help="per-URL navigation cap (default: 30000)",
    )
    cfg.add_argument("--locale", help="e.g. en-US, ja-JP")
    cfg.add_argument("--timezone", help="e.g. America/New_York")
    cfg.add_argument("--proxy", help="e.g. http://host:8080 or socks5://host:1080")
    cfg.add_argument(
        "--header", "-H", action="append", default=[], type=_parse_header,
        metavar="K=V",
        help="extra HTTP header (repeatable). Also accepts 'K: V'.",
    )
    cfg.add_argument(
        "--headers-file", metavar="PATH",
        help="file of KEY=VALUE headers, one per line (blank / '#'-prefixed lines ignored)",
    )
    cfg.add_argument("--no-js", action="store_true", help="disable JavaScript execution")
    cfg.add_argument(
        "--ignore-certs", action="store_true",
        help="ignore HTTPS cert errors (--ignore-certificate-errors)",
    )
    cfg.add_argument("--chrome", metavar="PATH", help="override Chrome binary path")
    cfg.add_argument(
        "--engine", choices=["full", "shell"], default=None,
        help=(
            "which Chromium build to drive (default: shell). 'full' "
            "= real Chrome, anti-WAF (needs a full Chrome binary — see "
            "`onyxweb-download-chrome --engine full`)."
        ),
    )
    cfg.add_argument(
        "--full-page", action="store_true",
        help="capture the full scrollable height for --screenshot / --screenshot-only",
    )
    cfg.add_argument(
        "--format", choices=["png", "jpeg", "webp"], default=None,
        help="screenshot image format (default: inferred from output file extension, else png)",
    )
    cfg.add_argument(
        "--quality", type=int, default=None, metavar="N",
        help="jpeg/webp quality 0-100 (ignored for png)",
    )
    return p


def _infer_format(path: str | None, explicit: str | None) -> str:
    if explicit:
        return explicit
    if path:
        ext = Path(path).suffix.lower()
        if ext in (".jpg", ".jpeg"):
            return "jpeg"
        if ext == ".webp":
            return "webp"
    return "png"


def _read_headers_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for ln in path.read_text().splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        k, v = _parse_header(ln)
        out[k] = v
    return out


def _resolve_preset(name: str) -> dict[str, Any]:
    """Look up a preset by dotted path (e.g., ``full.stealth.BASIC``)."""
    from onyxweb import presets

    parts = name.split(".")
    if len(parts) < 2:
        raise ValueError(
            f"--preset must be in 'engine.purpose.NAME' form (e.g. "
            f"'full.stealth.BASIC', 'shell.recon.FAST'), got {name!r}. "
            f"Try --preset list."
        )
    node: Any = presets
    for part in parts:
        node = getattr(node, part, None)
        if node is None:
            raise ValueError(f"unknown preset {name!r}. Try --preset list.")
    if not isinstance(node, dict):
        raise ValueError(f"unknown preset {name!r}. Try --preset list.")
    return node


def _list_presets(stream: IO[str] = sys.stdout) -> None:
    """Print available presets to ``stream``.

    Walks the engine → purpose → NAME tree under ``onyxweb.presets`` and lists
    every uppercase ``dict`` bundle as ``engine.purpose.NAME``.
    """
    from onyxweb import presets

    stream.write("Available presets (use --preset <engine>.<purpose>.<NAME>):\n")
    for engine_name in sorted(presets.__all__):
        engine_mod = getattr(presets, engine_name)
        for purpose_name in sorted(getattr(engine_mod, "__all__", [])):
            purpose_mod = getattr(engine_mod, purpose_name)
            for attr in sorted(dir(purpose_mod)):
                if attr.startswith("_") or not attr.isupper():
                    continue
                val = getattr(purpose_mod, attr)
                # A preset is a config-kwargs dict (every one pins `engine`);
                # skip constituent dicts like BASIC_UA_METADATA.
                if isinstance(val, dict) and "engine" in val:
                    stream.write(f"  {engine_name}.{purpose_name}.{attr}\n")


def _build_client_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    # Start with the preset (if any) so explicit CLI flags can override below.
    kwargs: dict[str, Any] = dict(_resolve_preset(args.preset)) if args.preset else {}

    # Overlay explicit CLI flags. Sentinel-None semantics on the overlapping
    # flags (viewport, timeout) so preset values flow through unchanged when
    # the user didn't pass the flag.
    if args.width is not None or args.height is not None:
        # If only one dimension is given, fall back to the Client default
        # (1200x800) for the other.
        w = args.width if args.width is not None else 1200
        h = args.height if args.height is not None else 800
        kwargs["viewport"] = (w, h)
    if args.timeout_ms is not None:
        kwargs["navigation_timeout_ms"] = args.timeout_ms
    if args.user_agent:
        kwargs["user_agent"] = args.user_agent
    if args.locale:
        kwargs["locale"] = args.locale
    if args.timezone:
        kwargs["timezone"] = args.timezone
    if args.proxy:
        kwargs["proxy"] = args.proxy
    if args.no_js:
        kwargs["javascript_enabled"] = False
    if args.ignore_certs:
        kwargs["ignore_https_errors"] = True
    if args.chrome:
        kwargs["chrome_path"] = args.chrome
    if args.engine:
        kwargs["engine"] = args.engine

    # Headers: merge preset → file → -H flags (last writer wins per key).
    headers: dict[str, str] = dict(kwargs.get("extra_headers") or {})
    if args.headers_file:
        headers.update(_read_headers_file(Path(args.headers_file)))
    for k, v in args.header:
        headers[k] = v
    if headers:
        kwargs["extra_headers"] = headers
    return kwargs


def _emit_meta(
    html_result: onyxweb.RenderResult, stream: IO[str] = sys.stderr
) -> None:
    stream.write(
        f"final_url={html_result.final_url}  "
        f"status={html_result.status_code}  "
        f"elapsed={html_result.elapsed_s:.3f}s  "
        f"errors={len(html_result.errors)}\n"
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for ``python -m onyxweb`` / ``onyxweb``.

    Parses ``argv`` (default ``sys.argv[1:]``), runs the requested action
    (fetch / screenshot / install / etc.), and returns a process exit code:
    0 success, 1 bad arg, 2 fetch error.
    """
    p = _build_parser()
    args = p.parse_args(argv)

    if args.version:
        try:
            from importlib.metadata import version
            print(version("onyxweb"))
        except Exception:
            print("unknown")
        return 0

    if args.install:
        from onyxweb.download import install_chrome
        return install_chrome()

    if args.preset == "list":
        _list_presets()
        return 0

    if not args.url:
        p.error("URL is required (see --help)")

    if args.screenshot_only and (args.output or args.screenshot):
        p.error("--screenshot-only is mutually exclusive with --output / --screenshot")
    if args.screenshot_only and args.json:
        p.error("--screenshot-only and --json are mutually exclusive")
    if args.quality is not None and not 0 <= args.quality <= 100:
        p.error("--quality must be between 0 and 100")

    try:
        client_kwargs = _build_client_kwargs(args)
    except ValueError as e:
        p.error(str(e))
    want_shot = bool(args.screenshot or args.screenshot_only)
    shot_path = args.screenshot or args.screenshot_only
    img_format = _infer_format(shot_path, args.format)

    # HTML destination: file (-o PATH) / stdout ('-' or unset) / suppressed (--screenshot-only).
    html_to_file: Path | None = None
    html_to_stdout = True
    if args.screenshot_only:
        html_to_stdout = False
    elif args.output and args.output != "-":
        html_to_file = Path(args.output)
        html_to_stdout = False

    def _emit_html(text: str) -> None:
        if html_to_file is not None:
            html_to_file.write_text(text)
        elif html_to_stdout:
            sys.stdout.write(text)
            if not text.endswith("\n"):
                sys.stdout.write("\n")

    try:
        with onyxweb.Client(**client_kwargs) as client:
            if want_shot:
                # img_format has been validated by _infer_format to be one of the
                # three accepted literals, but static-typing doesn't know that.
                shot_result = client.fetch_all(
                    args.url,
                    full_page=args.full_page,
                    format=cast(Literal["png", "jpeg", "webp"], img_format),
                    quality=args.quality,
                )
                Path(shot_path).write_bytes(shot_result.png)
                if args.json:
                    out = {
                        "url": args.url,
                        "final_url": shot_result.final_url,
                        "status_code": shot_result.status_code,
                        "elapsed_s": shot_result.elapsed_s,
                        "errors": shot_result.errors,
                        "html": str(shot_result.html),
                        "image_path": str(Path(shot_path).resolve()),
                        "image_format": img_format,
                        "image_bytes": len(shot_result.png),
                    }
                    sys.stdout.write(json.dumps(out) + "\n")
                else:
                    _emit_html(str(shot_result.html))
                if args.meta and not args.json:
                    _emit_meta(shot_result.html)
            else:
                html_only = client.fetch(args.url)
                if args.json:
                    out = {
                        "url": args.url,
                        "final_url": html_only.final_url,
                        "status_code": html_only.status_code,
                        "elapsed_s": html_only.elapsed_s,
                        "errors": html_only.errors,
                        "html": str(html_only),
                    }
                    sys.stdout.write(json.dumps(out) + "\n")
                else:
                    _emit_html(str(html_only))
                if args.meta and not args.json:
                    _emit_meta(html_only)

    except (RuntimeError, TimeoutError) as e:
        # onyxweb raises OnyxwebError (a RuntimeError) for failures and the
        # builtin TimeoutError for timeouts — surface both cleanly.
        sys.stderr.write(f"onyxweb: {e}\n")
        return 2
    except KeyboardInterrupt:
        sys.stderr.write("onyxweb: interrupted\n")
        return 130

    return 0


if __name__ == "__main__":
    sys.exit(main())
