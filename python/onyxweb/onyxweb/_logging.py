"""Logging setup for onyxweb (Python + Rust).

Control via ``ONYXWEB_LOG`` env var (read at module import) or
``onyxweb.set_log_level(...)`` at runtime. Levels: ``trace``, ``debug``,
``info``, ``warn``, ``error``, ``off``.

Only the ``onyxweb`` logger is configured, and only when asked. Importing the package leaves the
root logger alone: ``basicConfig`` does nothing once root has a handler, so a library that installs
one silently takes its host's logging with it.
"""

from __future__ import annotations

from logging import (
    CRITICAL,
    DEBUG,
    ERROR,
    INFO,
    NOTSET,
    WARNING,
    Formatter,
    NullHandler,
    StreamHandler,
    getLogger,
)
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


_FORMAT = "%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s: %(message)s"
_DATE_FORMAT = "%H:%M:%S"


def _speak_for_onyxweb(level: int) -> None:
    """Write the ``onyxweb`` logger's lines to stderr, through a handler of its own.

    Only when the caller asked for them, by naming a level here or setting ``ONYXWEB_LOG``. Its own
    handler also stops propagation, so a host that logs as well does not print every line twice.
    """
    if not any(getattr(handler, "_onyxweb", False) for handler in logger.handlers):
        handler = StreamHandler()  # stderr: stdout carries the page
        handler.setFormatter(Formatter(_FORMAT, _DATE_FORMAT))
        handler._onyxweb = True  # type: ignore[attr-defined]
        logger.addHandler(handler)
    logger.propagate = False
    logger.setLevel(level)


def configure(level: str | int | None = None) -> None:
    """Set the level of the ``onyxweb`` logger, and configure nothing else.

    Called on import with ``ONYXWEB_LOG``, or ``"warn"`` when it is unset. A level named here or in
    the environment is a request for onyxweb's lines, so the logger writes them itself. Without one
    the logger is left inheriting: the host's level and handlers apply, and where the host has none
    a warning still reaches stderr through the logging module's fallback. The root logger is never
    touched, so a host that sets up logging after importing onyxweb still gets its own.
    """
    asked = level is not None
    if level is None:
        wanted = environ.get("ONYXWEB_LOG")
        asked = wanted is not None
        level = wanted if wanted is not None else "warn"
    numeric = _parse_level(level)
    if asked:
        _speak_for_onyxweb(numeric)
        return
    if not logger.handlers:
        logger.addHandler(NullHandler())
    logger.setLevel(NOTSET)


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
