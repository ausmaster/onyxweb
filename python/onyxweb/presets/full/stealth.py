"""Full-Chrome stealth — real Chrome, no JS patches.

The full engine drives a real Chrome/Chromium in ``--headless=new`` with the
automation tells already stripped by the launcher (``--enable-automation``
dropped, ``AutomationControlled`` disabled → ``navigator.webdriver === false``,
real ``chrome.runtime`` / plugins / hardware, real Chrome UA). That clean
environment is what defeats Akamai/Cloudflare-class WAFs — verified traversing
tesla.com end-to-end.

Crucially it ships **no** stealth JS patches: on full Chrome they'd fake things
real Chrome already has legitimately and *create* tells (e.g.
``navigator.webdriver`` → ``undefined``, which real Chrome never is — that alone
flips tesla.com back to 403). The full engine *is* the stealth.

Needs a full Chrome binary — ``onyxweb-download-chrome --engine full``, a
system Chrome/Chromium, or ``chrome_path=``.

    from onyxweb import Client
    from onyxweb.presets.full import stealth

    Client(**stealth.BASIC).fetch("https://www.tesla.com/")
"""

from __future__ import annotations

from typing import Any

BASIC: dict[str, Any] = {
    "engine": "full",
    "bypass_anti_bot": True,
}

__all__ = ["BASIC"]
