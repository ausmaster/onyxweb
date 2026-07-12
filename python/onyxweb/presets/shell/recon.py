"""Recon preset — fast URL sweeps for security / change-detection pipelines.

Trades completeness for throughput: disables JavaScript, shortens the
navigation timeout, and blocks common ad/analytics/tracker hosts at the
network layer so pages drop a big chunk of tail requests.

Usage::

    from onyxweb import Client
    from onyxweb.presets import recon

    with Client(**recon.FAST) as c:
        for url in urls:
            html = c.fetch(url)     # no JS run; static markup only
"""

from __future__ import annotations

from typing import Any

FAST: dict[str, Any] = {
    "engine": "shell",
    "javascript_enabled": False,
    "navigation_timeout_ms": 5_000,
    "bypass_anti_bot": True,
    "block_urls": [
        "*://*.googlesyndication.com/*",
        "*://*.doubleclick.net/*",
        "*://*.googletagmanager.com/*",
        "*://*.google-analytics.com/*",
        "*://*.scorecardresearch.com/*",
        "*://*.facebook.net/*",
        "*://*.connect.facebook.net/*",
        "*://*.hotjar.com/*",
        "*://*.mixpanel.com/*",
        "*://*.segment.com/*",
    ],
}


__all__ = ["FAST"]
