"""Archival preset — change-detection / wayback-style snapshots.

Trades throughput for completeness: a large viewport, long nav timeout,
and an extra settle period after the load event so SPAs finish hydrating
before capture. Pair with ``full_page=True, format='webp', quality=85``
on the ``screenshot()`` / ``fetch_all()`` call for ready-to-archive output
(those flags live on :class:`ScreenshotConfig`, not here).

Usage::

    from onyxweb import Client
    from onyxweb.presets import archival

    with Client(**archival.FULL_PAGE) as c:
        page = c.fetch_all(url, full_page=True, format="webp", quality=85)
"""

from __future__ import annotations

from typing import Any

FULL_PAGE: dict[str, Any] = {
    "engine": "shell",
    "viewport": (1920, 1080),
    "navigation_timeout_ms": 30_000,
    "wait_until": "load",
    "wait_after_ms": 2_000,
}


__all__ = ["FULL_PAGE"]
