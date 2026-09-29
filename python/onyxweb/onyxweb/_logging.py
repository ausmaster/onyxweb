"""Logging setup for onyxweb (Python + Rust).

Control via ``ONYXWEB_LOG`` env var (read at module import) or
``onyxweb.set_log_level(...)`` at runtime. Levels: ``trace``, ``debug``,
``info``, ``warn``, ``error``, ``off``.
"""

from __future__ import annotations

from logging import CRITICAL, DEBUG, ERROR, INFO, WARNING, basicConfig, getLogger
from os import environ

logger = getLogger("onyxweb")

# stdlib has no TRACE level — Rust trace prints via Rust logger; on the Python
# side we bucket it to DEBUG so set_log_level("trace") is still meaningful.
_LEVEL_MAP = {
    "trace": DEBUG,
    "debug": DEBUG,
    "info": INFO,
    "warn": WARNING,
    "warning": WARNING,
    "error": ERROR,
    "off": CRITICAL + 1,
}


def _parse_level(level: str | int) -> int:
    if isinstance(level, int):
        return level
    # Accept env_logger-style filter strings ("onyxweb::engine=trace,warn") by
    # taking the first token's level.
    first = level.split(",")[0].split("=")[-1].strip().lower()
    return _LEVEL_MAP.get(first, WARNING)


def configure(level: str | int | None = None) -> None:
    """Set the Python-side ``onyxweb`` logger level.

    Called on import with ``ONYXWEB_LOG`` (or ``"warn"`` if unset). Installs
    a default timestamped format only if no handlers are already configured.
    """
    if level is None:
        level = environ.get("ONYXWEB_LOG", "warn")
    numeric = _parse_level(level)
    if not getLogger().handlers:
        basicConfig(
            format="%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
            level=WARNING,
        )
    logger.setLevel(numeric)


def set_log_level(level: str | int) -> None:
    """Set log level on BOTH Python and Rust sides.

    Rust's ``set_max_level`` takes a single level — if ``ONYXWEB_LOG`` was
    set with per-module filters at import, this replaces them with one global.
    """
    configure(level)
    rust_level = level if isinstance(level, str) else {
        DEBUG: "debug",
        INFO: "info",
        WARNING: "warn",
        ERROR: "error",
    }.get(level, "warn")
    from onyxweb import _onyxweb

    _onyxweb._set_rust_log_level(str(rust_level).lower())


__all__ = ["logger", "configure", "set_log_level"]
