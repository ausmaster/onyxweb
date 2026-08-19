"""A Turnstile widget that issued a token is a RESOLVED challenge.

Turnstile writes its token into ``input[name=cf-turnstile-response]``. A
populated value means the visitor passed; an empty one means the gate is still
up. Without this, every page embedding Turnstile reports ``resolved=False`` even
after passing — a false "blocked" signal for recon consumers.
"""

from __future__ import annotations

import onyxweb
from pytest_httpserver import HTTPServer

# Small bodies: Turnstile is a "leaky" marker, only trusted under 15 KB.
_SOLVED = (
    "<html><body><div class='cf-turnstile'>"
    "<input name='cf-turnstile-response' type='hidden' value='0.abc123token'>"
    "</div></body></html>"
)
_UNSOLVED = (
    "<html><body><div class='cf-turnstile'>"
    "<input name='cf-turnstile-response' type='hidden' value=''>"
    "</div></body></html>"
)


def _fetch(httpserver: HTTPServer, body: str) -> onyxweb.RenderResult:
    httpserver.expect_request("/").respond_with_data(body, content_type="text/html")
    with onyxweb.Client(concurrency=1) as c:
        return c.fetch(httpserver.url_for("/"))


def test_turnstile_with_token_is_resolved(httpserver: HTTPServer) -> None:
    r = _fetch(httpserver, _SOLVED)
    assert r.anti_bot is not None
    assert r.anti_bot.vendor == "cloudflare"
    assert r.anti_bot.kind == "challenge"
    assert r.anti_bot.resolved is True


def test_turnstile_without_token_is_unresolved(httpserver: HTTPServer) -> None:
    r = _fetch(httpserver, _UNSOLVED)
    assert r.anti_bot is not None
    assert r.anti_bot.resolved is False


def test_turnstile_missing_value_attr_is_unresolved(httpserver: HTTPServer) -> None:
    """Turnstile renders the input with no ``value`` until it issues a token."""
    body = (
        "<html><body><div class='cf-turnstile'>"
        "<input name='cf-turnstile-response' type='hidden'>"
        "</div></body></html>"
    )
    r = _fetch(httpserver, body)
    assert r.anti_bot is not None
    assert r.anti_bot.resolved is False


def test_token_read_needs_no_shadow_root_access(httpserver: HTTPServer) -> None:
    """The token input is light-DOM, so detection works without OPEN_SHADOW_ROOTS.

    Guards the contract that ``anti_bot`` is accurate on a plain ``fetch()`` — the
    widget's iframe is closed-shadow, but its token field is not.
    """
    body = (
        "<html><body><div class='cf-turnstile'>"
        "<input name='cf-turnstile-response' type='hidden' value='0.tok'>"
        "</div><script>"
        "document.querySelector('.cf-turnstile').attachShadow({mode:'closed'})"
        ".innerHTML = '<iframe title=\"widget\"></iframe>';"
        "</script></body></html>"
    )
    httpserver.expect_request("/").respond_with_data(body, content_type="text/html")
    with onyxweb.Client(concurrency=1) as c:
        r = c.fetch(httpserver.url_for("/"))  # no scripts= at all
    assert r.dom.query_one("iframe") is None, "sanity: iframe is hidden in closed shadow"
    assert r.anti_bot is not None
    assert r.anti_bot.resolved is True


def test_real_challenge_stub_still_unresolved(httpserver: HTTPServer) -> None:
    """A genuine interstitial has no token field — must stay unresolved."""
    body = (
        "<html><head><title>Just a moment...</title></head><body>"
        "<script>window._cf_chl_opt={cvId:'3'};</script></body></html>"
    )
    r = _fetch(httpserver, body)
    assert r.anti_bot is not None
    assert r.anti_bot.resolved is False
