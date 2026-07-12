"""Full-Chrome engine presets — real Chrome (anti-WAF).

These target the ``full`` engine: a real Chrome/Chromium in ``--headless=new``,
near-indistinguishable from a real browser, which gets past Akamai/Cloudflare-
class WAFs that hard-block the shell. No stealth JS patches (they'd create tells
on real Chrome). Needs a full Chrome binary (``onyxweb-download-chrome --engine
full`` / system Chrome / ``chrome_path=``).

    from onyxweb import Client
    from onyxweb.presets.full import stealth

    Client(**stealth.BASIC).fetch("https://www.tesla.com/")
"""

from onyxweb.presets.full import stealth

__all__ = ["stealth"]
