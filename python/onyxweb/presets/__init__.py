"""Pre-packaged ``ClientConfig`` bundles — spread into ``Client(**preset)``.

Presets are plain ``dict`` s, organized **engine-first** because the two engines
need opposite recipes:

* :mod:`onyxweb.presets.shell` — ``chrome-headless-shell`` (light, fast). Its
  stealth uses JS patches to counter *naive* bot checks — **not** a WAF bypass.
* :mod:`onyxweb.presets.full` — real Chrome (``--headless=new``, heavier). Its
  stealth is the clean engine itself (no patches) and defeats Akamai/Cloudflare-
  class WAFs.

Then by purpose (``stealth`` / ``recon`` / ``archival``)::

    from onyxweb import Client
    from onyxweb.presets.shell import recon, stealth as shell_stealth
    from onyxweb.presets.full import stealth as full_stealth

    Client(**recon.FAST).fetch(url)                       # fast shell sweep
    Client(**shell_stealth.BASIC).fetch(url)              # anti-naive-detection
    Client(**full_stealth.BASIC).fetch("https://www.tesla.com/")  # anti-WAF

Tweak a preset by pre-merging (Python forbids duplicate keyword args across
``**`` spreads)::

    Client(**{**recon.FAST, "user_agent": "MyBot/1.0"})

A note on **list-valued fields**: naive dict spread replaces rather than
concatenates ``scripts.on_new_document``. To cumulate, spread manually.
"""

from onyxweb.presets import full, shell

__all__ = ["full", "shell"]
