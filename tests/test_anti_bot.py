"""``bypass_anti_bot`` — the single anti-bot switch, two layers:

1. **Challenge wait**: a WAF challenge interstitial (a small stub that runs a JS
   check then self-reloads to the real page) is waited out instead of captured.
2. **Self-heal**: a hard block (403/429 with a WAF signature) drops the tab's
   anti-bot cookies (``_abck`` / ``bm_*`` / ..., keeping benign ones) and
   retries once. Each pooled tab has its own cookie jar, so the clear is scoped.

Plus the ``RenderResult.anti_bot`` indicator (:class:`onyxweb.AntiBot`): a
WAF/anti-bot signal that populates whether or not ``bypass_anti_bot`` is on, so
a plain fetch still reports "this site is behind Akamai" as recon.
"""

from __future__ import annotations

import onyxweb
from werkzeug.wrappers import Request, Response


def _akamai_like(request: Request) -> Response:
    """403 (Akamai stub) when the flagged cookie is present, else 200.

    Always (re)sets the flagged cookie, so a naive second fetch on the same tab
    would carry it and get blocked — only dropping the cookie recovers.
    """
    flagged = "_abck=flagged" in request.headers.get("Cookie", "")
    if flagged:
        body = (
            "<html><head><title>Access Denied</title></head><body>"
            "You don't have permission. Reference #18.abcdef</body></html>"
        )
        resp = Response(body, status=403, content_type="text/html")
        resp.headers["Server"] = "AkamaiGHost"
    else:
        body = "<html><head><title>Real Page</title></head><body>ok</body></html>"
        resp = Response(body, status=200, content_type="text/html")
    resp.headers["Set-Cookie"] = "_abck=flagged; Path=/"
    return resp


def test_bypass_anti_bot_recovers(httpserver) -> None:
    httpserver.expect_request("/").respond_with_handler(_akamai_like)
    url = httpserver.url_for("/")
    with onyxweb.Client(concurrency=1) as client:
        assert client.fetch(url).status_code == 200  # fresh context -> 200, poisons it
        # Same tab (LIFO reuse) now carries _abck=flagged -> 403 -> heal drops
        # the cookie -> retry has no _abck -> 200.
        r2 = client.fetch(url, bypass_anti_bot=True)
        assert r2.status_code == 200
        assert "Real Page" in r2
        # Indicator records the block we healed through.
        assert r2.anti_bot == onyxweb.AntiBot(vendor="akamai", kind="block", resolved=True)


def test_off_by_default_stays_blocked(httpserver) -> None:
    """Without the opt-in, the poisoned second fetch stays a 403."""
    httpserver.expect_request("/").respond_with_handler(_akamai_like)
    url = httpserver.url_for("/")
    with onyxweb.Client(concurrency=1) as client:
        assert client.fetch(url).status_code == 200
        blocked = client.fetch(url)
        assert blocked.status_code == 403  # no retry
        # Detection runs with bypass OFF — the block is still flagged as recon.
        assert blocked.anti_bot == onyxweb.AntiBot(
            vendor="akamai", kind="block", resolved=False
        )


def test_client_level_default_heals(httpserver) -> None:
    """Client(bypass_anti_bot=True) heals every fetch without a per-call flag."""
    httpserver.expect_request("/").respond_with_handler(_akamai_like)
    url = httpserver.url_for("/")
    with onyxweb.Client(concurrency=1, bypass_anti_bot=True) as client:
        assert client.fetch(url).status_code == 200  # fresh -> 200, poisons
        assert client.fetch(url).status_code == 200  # inherits base True -> heals


def test_plain_403_not_retried_even_with_heal(httpserver) -> None:
    """A 403 without an anti-bot signature is a normal response, not a block."""
    calls: list[int] = []

    def handler(_request: Request) -> Response:
        calls.append(1)
        return Response(
            "<html><body>forbidden</body></html>", status=403, content_type="text/html"
        )

    httpserver.expect_request("/").respond_with_handler(handler)
    with onyxweb.Client(concurrency=1) as client:
        r = client.fetch(httpserver.url_for("/"), bypass_anti_bot=True)
        assert r.status_code == 403
        assert r.anti_bot is None  # a bare 403 is not a WAF signature
    assert len(calls) == 1  # no self-heal retry


def test_heal_preserves_benign_cookies(httpserver) -> None:
    """Heal drops only anti-bot cookies; a benign cookie survives the retry."""
    seen: dict[str, str] = {}

    def handler(request: Request) -> Response:
        cookie = request.headers.get("Cookie", "")
        seen["last"] = cookie
        if "_abck=flagged" in cookie:
            resp = Response(
                "<title>Access Denied</title> Reference #1", status=403, content_type="text/html"
            )
            resp.headers["Server"] = "AkamaiGHost"
        else:
            resp = Response("<title>Real Page</title>", status=200, content_type="text/html")
        resp.headers.add("Set-Cookie", "_abck=flagged; Path=/")
        resp.headers.add("Set-Cookie", "sess=keepme; Path=/")
        return resp

    httpserver.expect_request("/").respond_with_handler(handler)
    url = httpserver.url_for("/")
    with onyxweb.Client(concurrency=1) as client:
        client.fetch(url)  # sets _abck + sess
        client.fetch(url, bypass_anti_bot=True)  # 403 -> drop _abck, keep sess -> 200
    # The healed retry (the last request) carried the benign cookie, not _abck.
    assert "sess=keepme" in seen["last"]
    assert "_abck" not in seen["last"]


def _challenge_then_real(request: Request) -> Response:
    """A WAF interstitial (Akamai ``sec-if-cpt`` marker) that self-reloads to a
    real page once its JS sets the ``solved`` cookie."""
    if "solved=1" in request.headers.get("Cookie", ""):
        body = (
            "<html><head><title>Real Page</title></head><body>"
            + ("x" * 20000) + "</body></html>"
        )
    else:
        body = (
            '<html><body><div id="sec-if-cpt-container"></div>'
            '<script>document.cookie="solved=1;path=/";'
            "setTimeout(function(){location.reload();},800);</script></body></html>"
        )
    return Response(body, status=200, content_type="text/html")


def test_challenge_interstitial_captured_without_bypass(httpserver) -> None:
    httpserver.expect_request("/").respond_with_handler(_challenge_then_real)
    with onyxweb.Client(concurrency=1) as client:  # bypass off
        r = client.fetch(httpserver.url_for("/"))
        assert "sec-if-cpt-container" in r  # captured the stub, not the real page
        assert "Real Page" not in r
        # Flagged as an unresolved challenge even though we didn't try to bypass.
        assert r.anti_bot == onyxweb.AntiBot(
            vendor="akamai", kind="challenge", resolved=False
        )


def test_challenge_interstitial_waited_out_with_bypass(httpserver) -> None:
    httpserver.expect_request("/").respond_with_handler(_challenge_then_real)
    # Fresh context (no solved cookie) so the fetch actually faces the challenge.
    with onyxweb.Client(concurrency=1, bypass_anti_bot=True) as client:
        r = client.fetch(httpserver.url_for("/"), timeout_ms=30000)
        assert "sec-if-cpt-container" not in r  # waited past the interstitial
        assert "Real Page" in r
        assert len(r) > 15000  # the full resolved page, not the thin reload
        # Challenge recorded, marked resolved (we got to the real page).
        assert r.anti_bot == onyxweb.AntiBot(
            vendor="akamai", kind="challenge", resolved=True
        )


def test_clean_page_has_no_anti_bot_indicator(httpserver) -> None:
    """A normal 200 with no WAF markers reports ``anti_bot is None``."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body>hello world</body></html>", content_type="text/html"
    )
    with onyxweb.Client(concurrency=1) as client:
        r = client.fetch(httpserver.url_for("/"))
        assert r.status_code == 200
        assert r.anti_bot is None


# ---------------------------------------------------------------------------
# Detection precision: some WAF markers leak onto the REAL (passed) page —
# Cloudflare's passive JS-detections beacon + embedded Turnstile widget, and
# Imperva's `_Incapsula_Resource` script-rewriting. They must count as a
# challenge ONLY on a small stub, never on a large real page — else `anti_bot`
# false-positives across the huge slice of the web behind these WAFs.


def test_large_cloudflare_page_beacon_not_flagged(httpserver) -> None:
    """A big real page with CF's passive beacon + Turnstile widget → not a WAF."""
    body = (
        "<html><head><title>Real Shop</title>"
        '<script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script>'
        '<div class="cf-turnstile" data-sitekey="x"></div></head><body>'
        + ("<p>product listing row</p>" * 2000)  # well past 15 KB
        + "</body></html>"
    )
    httpserver.expect_request("/").respond_with_data(body, content_type="text/html")
    with onyxweb.Client(concurrency=1) as client:
        r = client.fetch(httpserver.url_for("/"))
        assert len(r) > 15000
        assert r.anti_bot is None


def test_large_imperva_page_resource_ref_not_flagged(httpserver) -> None:
    """A big real page that references `_Incapsula_Resource` → not a WAF."""
    body = (
        "<html><head><title>State Site</title>"
        '<script src="/_Incapsula_Resource?SWJIYW=719d34d31c8e3a6e."></script>'
        "</head><body>" + ("<p>agency content row</p>" * 2000) + "</body></html>"
    )
    httpserver.expect_request("/").respond_with_data(body, content_type="text/html")
    with onyxweb.Client(concurrency=1) as client:
        r = client.fetch(httpserver.url_for("/"))
        assert len(r) > 15000
        assert r.anti_bot is None


def test_cloudflare_interstitial_stub_detected(httpserver) -> None:
    """A small CF "Just a moment" stub (challenge-only token) → detected."""
    body = (
        "<html><head><title>Just a moment...</title></head><body>"
        "<script>window._cf_chl_opt={cvId:'3',cType:'managed'};</script>"
        "<h1>Verifying you are human. This may take a few seconds.</h1>"
        "</body></html>"
    )
    httpserver.expect_request("/").respond_with_data(body, content_type="text/html")
    with onyxweb.Client(concurrency=1) as client:  # bypass off: just detection
        r = client.fetch(httpserver.url_for("/"))
        assert r.anti_bot == onyxweb.AntiBot(
            vendor="cloudflare", kind="challenge", resolved=False
        )


def test_imperva_interstitial_stub_detected(httpserver) -> None:
    """A tiny Imperva JS stub (no visible text) → detected via the size gate."""
    body = (
        "<html><body>"
        '<script src="/_Incapsula_Resource?SWKMTFSR=1&e=abc"></script>'
        "</body></html>"
    )
    httpserver.expect_request("/").respond_with_data(body, content_type="text/html")
    with onyxweb.Client(concurrency=1) as client:
        r = client.fetch(httpserver.url_for("/"))
        assert len(r) < 15000
        assert r.anti_bot == onyxweb.AntiBot(
            vendor="imperva", kind="challenge", resolved=False
        )


def test_cloudflare_cf_mitigated_header_is_challenge_not_block(httpserver) -> None:
    """Cloudflare's `cf-mitigated: challenge` response header authoritatively
    marks a challenge (per CF docs) — a CF challenge is a 403, so without the
    header check `block_vendor` would mislabel it a hard block. The header is
    body-independent: it wins even on a large body with no CF body markers."""

    def handler(_request: Request) -> Response:
        # Large body, no Cloudflare body markers — only the header signals it.
        resp = Response(
            "<html><body>" + ("<p>x</p>" * 3000) + "</body></html>",
            status=403,
            content_type="text/html",
        )
        resp.headers["Server"] = "cloudflare"
        resp.headers["cf-mitigated"] = "challenge"
        return resp

    httpserver.expect_request("/").respond_with_handler(handler)
    with onyxweb.Client(concurrency=1) as client:
        r = client.fetch(httpserver.url_for("/"))
        assert r.status_code == 403
        assert len(r) > 15000  # body-based detector would not fire
        assert r.anti_bot == onyxweb.AntiBot(
            vendor="cloudflare", kind="challenge", resolved=False
        )


# ---------------------------------------------------------------------------
# Header-emitting vendors: detect via response header / status, not body
# (more precise, no false positives). AWS WAF, Kasada, Fastly.


def test_aws_waf_challenge_action_header(httpserver) -> None:
    """AWS WAF Challenge action: `x-amzn-waf-action: challenge` (HTTP 202,
    silent auto-solving interstitial) → aws challenge."""

    def handler(_request: Request) -> Response:
        resp = Response(
            "<html><body>challenge</body></html>", status=202, content_type="text/html"
        )
        resp.headers["x-amzn-waf-action"] = "challenge"
        return resp

    httpserver.expect_request("/").respond_with_handler(handler)
    with onyxweb.Client(concurrency=1) as client:
        r = client.fetch(httpserver.url_for("/"))
        assert r.anti_bot == onyxweb.AntiBot(
            vendor="aws", kind="challenge", resolved=False
        )


def test_aws_waf_captcha_action_header(httpserver) -> None:
    """AWS WAF CAPTCHA action: `x-amzn-waf-action: captcha` (HTTP 405,
    interactive) → aws challenge (we can't auto-solve it → resolved False)."""

    def handler(_request: Request) -> Response:
        resp = Response(
            "<html><body>captcha</body></html>", status=405, content_type="text/html"
        )
        resp.headers["x-amzn-waf-action"] = "captcha"
        return resp

    httpserver.expect_request("/").respond_with_handler(handler)
    with onyxweb.Client(concurrency=1) as client:
        r = client.fetch(httpserver.url_for("/"))
        assert r.anti_bot == onyxweb.AntiBot(
            vendor="aws", kind="challenge", resolved=False
        )


def test_aws_waf_token_cookie_alone_not_flagged(httpserver) -> None:
    """The `aws-waf-token` cookie is set on passed pages too — presence leaks
    and must NOT be a block signal."""

    def handler(_request: Request) -> Response:
        resp = Response(
            "<html><body>" + ("<p>ok</p>" * 3000) + "</body></html>",
            status=200,
            content_type="text/html",
        )
        resp.headers["Set-Cookie"] = "aws-waf-token=abc123; Path=/"
        return resp

    httpserver.expect_request("/").respond_with_handler(handler)
    with onyxweb.Client(concurrency=1) as client:
        r = client.fetch(httpserver.url_for("/"))
        assert r.anti_bot is None


def test_kasada_x_kpsdk_ct_block_header(httpserver) -> None:
    """Kasada silent-PoW hard block: the `x-kpsdk-ct` response header on a
    403/429 → kasada block (recon signal; not defeatable CDP-only)."""

    def handler(_request: Request) -> Response:
        resp = Response("", status=429, content_type="text/html")
        resp.headers["x-kpsdk-ct"] = "01HZ...token"
        return resp

    httpserver.expect_request("/").respond_with_handler(handler)
    with onyxweb.Client(concurrency=1) as client:
        r = client.fetch(httpserver.url_for("/"))
        assert r.anti_bot == onyxweb.AntiBot(
            vendor="kasada", kind="block", resolved=False
        )


def test_fastly_406_block_with_signature(httpserver) -> None:
    """Fastly / Signal Sciences blocks default to HTTP 406 (not 403/429), so
    a 406 with a WAF body signature must register as a block."""

    def handler(_request: Request) -> Response:
        return Response(
            "<title>Access Denied</title>", status=406, content_type="text/html"
        )

    httpserver.expect_request("/").respond_with_handler(handler)
    with onyxweb.Client(concurrency=1) as client:
        r = client.fetch(httpserver.url_for("/"))
        assert r.status_code == 406
        assert r.anti_bot == onyxweb.AntiBot(vendor=None, kind="block", resolved=False)


# ---------------------------------------------------------------------------
# CAPTCHA widgets (reCAPTCHA / hCaptcha): top-frame DOM markers that LEAK onto
# normal pages with a captcha-protected form, so they count only on a small
# stub (a page that IS a captcha gate), never on a full-size page.


def test_recaptcha_stub_detected(httpserver) -> None:
    """A small page that is essentially a reCAPTCHA gate → recaptcha challenge."""
    body = (
        "<html><head><title>Verify</title></head><body>"
        '<div class="g-recaptcha" data-sitekey="x"></div></body></html>'
    )
    httpserver.expect_request("/").respond_with_data(body, content_type="text/html")
    with onyxweb.Client(concurrency=1) as client:
        r = client.fetch(httpserver.url_for("/"))
        assert len(r) < 15000
        assert r.anti_bot == onyxweb.AntiBot(
            vendor="recaptcha", kind="challenge", resolved=False
        )


def test_hcaptcha_stub_detected(httpserver) -> None:
    """A small page that is essentially an hCaptcha gate → hcaptcha challenge."""
    body = (
        "<html><head><title>Verify</title></head><body>"
        '<div class="h-captcha" data-sitekey="x"></div></body></html>'
    )
    httpserver.expect_request("/").respond_with_data(body, content_type="text/html")
    with onyxweb.Client(concurrency=1) as client:
        r = client.fetch(httpserver.url_for("/"))
        assert len(r) < 15000
        assert r.anti_bot == onyxweb.AntiBot(
            vendor="hcaptcha", kind="challenge", resolved=False
        )


def test_large_page_with_recaptcha_form_not_flagged(httpserver) -> None:
    """A normal large page with a reCAPTCHA-protected form → not a WAF gate."""
    body = (
        "<html><head><title>Contact</title></head><body>"
        '<form><div class="g-recaptcha" data-sitekey="x"></div></form>'
        + ("<p>page content row</p>" * 2000)
        + "</body></html>"
    )
    httpserver.expect_request("/").respond_with_data(body, content_type="text/html")
    with onyxweb.Client(concurrency=1) as client:
        r = client.fetch(httpserver.url_for("/"))
        assert len(r) > 15000
        assert r.anti_bot is None
