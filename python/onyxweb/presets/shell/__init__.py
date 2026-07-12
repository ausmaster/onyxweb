"""Shell-engine presets — ``chrome-headless-shell`` (light, fast).

These target the default ``shell`` engine. The stealth preset's JS
patches counter *naive* client-side bot checks; they are **not** a WAF bypass
(Akamai/Cloudflare hard-block the shell regardless — use ``presets.full`` for
those). Every preset here pins ``engine="shell"`` explicitly.

    from onyxweb import Client
    from onyxweb.presets.shell import stealth, recon

    Client(**recon.FAST).fetch(url)
    Client(**stealth.BASIC).fetch(url)
"""

from onyxweb.presets.shell import archival, recon, stealth

__all__ = ["archival", "recon", "stealth"]
