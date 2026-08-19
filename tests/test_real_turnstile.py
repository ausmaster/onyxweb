"""Real production Cloudflare Turnstile — integration cover for the mocks.

``test_anti_bot_turnstile.py`` / ``test_open_shadow_roots.py`` pin the parsing and
patching logic against local fixtures; these prove the same behavior against a
live Cloudflare widget with a real sitekey, where the DOM shape and token format
are Cloudflare's rather than ours.

Excluded from the default run (``addopts = -m 'not benchmark'``)::

    uv run pytest -m real_sites -s tests/test_real_turnstile.py

Target: pagpeter's public per-mode test pages (real production sitekeys, neutral
static pages, no forms). A network failure skips; a wrong verdict fails.
"""

from __future__ import annotations

import onyxweb
import pytest

pytestmark = [pytest.mark.benchmark, pytest.mark.real_sites]

BASE = "https://peet.ws/turnstile-test/"
SETTLE_MS = 15_000

_TOKEN_SEL = 'input[name="cf-turnstile-response"]'


def _fetch(path: str, **kw: object) -> onyxweb.RenderResult:
    try:
        with onyxweb.Client(engine="full", navigation_timeout_ms=40_000) as c:
            return c.fetch(BASE + path, wait_after_ms=SETTLE_MS, **kw)  # type: ignore[arg-type]
    except (onyxweb.OnyxwebError, TimeoutError) as e:
        pytest.skip(f"{BASE}{path} unreachable: {e}")


def _token(r: onyxweb.RenderResult) -> str:
    el = r.dom.query_one(_TOKEN_SEL)
    return (el.attr("value") or "") if el else ""


def test_passive_pass_reports_resolved_without_shadow_patch() -> None:
    """Non-interactive mode issues a token with no interaction → resolved.

    Deliberately passes no ``scripts=`` — the token field is light-DOM, so
    ``anti_bot`` must be accurate on a plain ``fetch()``.
    """
    r = _fetch("non-interactive.html")
    token = _token(r)
    if not token:
        pytest.skip("Cloudflare declined to issue a token from this IP/session")
    assert token != "XXXX.DUMMY.TOKEN.XXXX", "expected a real token, not the test-key dummy"
    assert r.anti_bot is not None
    assert r.anti_bot.vendor == "cloudflare"
    assert r.anti_bot.resolved is True


def test_interactive_gate_reports_unresolved() -> None:
    """Managed mode withholds the token until interaction → not resolved."""
    r = _fetch("managed.html")
    if _token(r):
        pytest.skip("managed widget auto-passed; nothing to assert about the gate")
    assert r.anti_bot is not None
    assert r.anti_bot.resolved is False


def test_include_shadow_dom_recovers_real_widget_markup() -> None:
    """Turnstile's widget lives in a closed shadow root — captured only when enabled."""
    plain = _fetch("managed.html")
    assert "challenges.cloudflare.com/cdn-cgi" not in plain.dom.html()

    try:
        with onyxweb.Client(
            engine="full", navigation_timeout_ms=40_000, include_shadow_dom=True
        ) as c:
            deep = c.fetch(BASE + "managed.html", wait_after_ms=SETTLE_MS)
    except (onyxweb.OnyxwebError, TimeoutError) as e:
        pytest.skip(f"unreachable: {e}")
    assert "challenges.cloudflare.com/cdn-cgi" in deep.dom.html()
    assert deep.dom.query_one("iframe") is not None
