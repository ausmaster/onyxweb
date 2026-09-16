"""Detection precision: markers that also ship on ordinary pages.

Several vendor tokens appear verbatim in pages that were never challenged —
invisible reCAPTCHA v3 badges, cookie-consent manifests, Akamai's always-on
sensor bundle. Treating those as a challenge reports a WAF gate where none
exists, which is worse than missing one: it is wrong data that looks right.

Marker sets and the co-signal approach are adapted from BrowserOxide's
classifier (MIT, github.com/yfedoseev/browser_oxide, crates/browser_oxide/src/classify.rs).
"""

from __future__ import annotations

import onyxweb
from pytest_httpserver import HTTPServer


def _fetch(httpserver: HTTPServer, body: str) -> onyxweb.RenderResult:
    httpserver.expect_request("/").respond_with_data(body, content_type="text/html")
    with onyxweb.Client(concurrency=1) as c:
        return c.fetch(httpserver.url_for("/"))


def _page(inner: str) -> str:
    """A small but perfectly normal page (under the 15 KB stub gate)."""
    return f"<html><head><title>Sign in</title></head><body><h1>Welcome</h1>{inner}</body></html>"


# --- false positives that must NOT be flagged ------------------------------


def test_invisible_recaptcha_v3_badge_is_not_a_challenge(httpserver: HTTPServer) -> None:
    """v3 is scoreless and invisible; its badge ships on ordinary login pages."""
    body = _page(
        "<script src='https://www.gstatic.com/recaptcha/releases/abc/recaptcha__en.js'></script>"
        "<div class='grecaptcha-badge' style='display:none'></div>"
        "<textarea id='g-recaptcha-response' name='g-recaptcha-response'></textarea>"
    )
    assert _fetch(httpserver, body).anti_bot is None


def test_px_captcha_string_in_rendered_page_is_not_a_challenge(
    httpserver: HTTPServer,
) -> None:
    """`px-captcha` appears as a cookie-consent category key on real pages.

    Only the size gate can separate this from a genuine gate: on a small body
    the two are textually identical, so the rule is "a fully rendered page is
    not a challenge, whatever strings it carries".
    """
    body = (
        "<html><body><h1>Shop</h1>"
        + "<p>product listing row</p>" * 2000
        + '<script>window.consent={"px-captcha":"NECESSARY"};</script></body></html>'
    )
    r = _fetch(httpserver, body)
    assert len(r) > 15_000
    assert r.anti_bot is None


def test_akamai_sensor_bundle_alone_is_not_a_challenge(httpserver: HTTPServer) -> None:
    """`akam/13` loads on every Akamai-fronted page, challenged or not."""
    body = _page("<script src='/akam/13/abc123'></script>")
    assert _fetch(httpserver, body).anti_bot is None


# --- true positives that must still be flagged ------------------------------


def test_interactive_recaptcha_checkbox_is_a_challenge(httpserver: HTTPServer) -> None:
    """The interactive widget's anchor/bframe iframe means a real gate."""
    body = _page(
        "<iframe src='https://www.google.com/recaptcha/api2/anchor?k=x'></iframe>"
        "<div class='g-recaptcha' data-sitekey='x'></div>"
    )
    r = _fetch(httpserver, body)
    assert r.anti_bot is not None
    assert r.anti_bot.vendor == "recaptcha"


def test_akamai_sensor_with_cosignal_is_a_challenge(httpserver: HTTPServer) -> None:
    """`akam/13` plus a sensor-data post is the real interstitial."""
    body = _page("<script src='/akam/13/abc'></script><script>sensor_data='x'</script>")
    r = _fetch(httpserver, body)
    assert r.anti_bot is not None
    assert r.anti_bot.vendor == "akamai"


def test_akamai_abck_cookie_marker_is_a_challenge(httpserver: HTTPServer) -> None:
    body = _page("<script>document.cookie='_abck=xyz~-1~';</script>")
    r = _fetch(httpserver, body)
    assert r.anti_bot is not None
    assert r.anti_bot.vendor == "akamai"


def test_perimeterx_press_and_hold_is_a_challenge(httpserver: HTTPServer) -> None:
    """PerimeterX's flagship interactive gate, in all three spellings."""
    for phrase in ("Press &amp; Hold", "Press & Hold", "press and hold"):
        body = _page(f"<p>{phrase} to confirm you are human</p>")
        r = _fetch(httpserver, body)
        assert r.anti_bot is not None, phrase
        assert r.anti_bot.vendor == "perimeterx", phrase


def test_imperva_pardon_our_interruption_is_a_challenge(httpserver: HTTPServer) -> None:
    body = _page("<h2>Pardon Our Interruption</h2><p>you are browsing quickly</p>")
    r = _fetch(httpserver, body)
    assert r.anti_bot is not None
    assert r.anti_bot.vendor == "imperva"


def test_kasada_body_marker_is_a_challenge(httpserver: HTTPServer) -> None:
    """Kasada also serves 200 shells, not only 403/429."""
    body = _page("<script src='/ips.js'></script><script>window._kpsdk=1</script>")
    r = _fetch(httpserver, body)
    assert r.anti_bot is not None
    assert r.anti_bot.vendor == "kasada"


def test_aws_waf_challenge_detected_from_body_at_any_size(httpserver: HTTPServer) -> None:
    """The AWS envelope needs its live loader to count as an active challenge."""
    body = (
        "<html><body>" + "<p>filler</p>" * 3000
        + "<script>window.gokuProps={};</script>"
        + "<script src='https://token.awswaf.com/x/challenge.js'></script>"
        + "</body></html>"
    )
    r = _fetch(httpserver, body)
    assert r.anti_bot is not None
    assert r.anti_bot.vendor == "aws"


def test_aws_waf_envelope_without_loader_is_solved_not_blocked(
    httpserver: HTTPServer,
) -> None:
    """A solved page keeps the config var but drops the loader."""
    body = (
        "<html><body>" + "<p>real content</p>" * 3000
        + "<script>window.awsWafCookieDomainList=['x'];</script></body></html>"
    )
    r = _fetch(httpserver, body)
    assert r.anti_bot is None or r.anti_bot.resolved is True
