"""C9 anti-bot — a response's status, headers and body map to one ``AntiBot`` verdict.

Two tables. ``VERDICTS`` pins detection: each row serves one response, and
``RenderResult.anti_bot`` must report its verdict whether or not
``bypass_anti_bot`` is on — the indicator is a recon signal in its own right.
``SCENARIOS`` pins what bypass does with a verdict: heal a hard block by
dropping the tab's anti-bot cookies and retrying once, or wait out a challenge
until the real page arrives.

Leaky markers — a CAPTCHA widget, Cloudflare's beacon, Imperva's resource
script — count only on a small stub, never on a full page. Every row declares
which side of that gate its body sits on, and the table checks it, so a fixture
that drifts across the gate fails as a fixture error instead of passing for the
wrong reason.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import onyxweb
import pytest
from conftest import reloaded
from onyxweb import AntiBot
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Request, Response

STUB_MAX_BYTES = 30_720  # mirrors CHALLENGE_STUB_MAX_BYTES in src/engine.rs
CHALLENGE_MAX_WAIT_S = 12.0  # mirrors CHALLENGE_MAX_WAIT_MS in src/engine.rs


@dataclass(frozen=True)
class Served:
    """One response a verdict row serves."""

    body: str
    status: int = 200
    headers: tuple[tuple[str, str], ...] = ()
    full: bool = False  # body is past the stub gate


def _stub(inner: str) -> str:
    """A small, ordinary page carrying ``inner``."""
    return f"<html><head><title>Sign in</title></head><body><h1>Welcome</h1>{inner}</body></html>"


def _full(inner: str) -> Served:
    """A rendered page well past the stub gate, carrying ``inner``."""
    return Served(f"<html><body>{inner}{'<p>page content row</p>' * 2000}</body></html>", full=True)


_TURNSTILE = (
    "<html><body><div class='cf-turnstile'>"
    "<input name='cf-turnstile-response' type='hidden'{value}></div>{rest}</body></html>"
)


def _challenge(vendor: str, resolved: bool = False) -> AntiBot:
    return AntiBot(vendor=vendor, kind="challenge", resolved=resolved)


def _block(vendor: str | None) -> AntiBot:
    return AntiBot(vendor=vendor, kind="block", resolved=False)


# Row -> (response served, verdict expected, premise the capture must meet or None).
VERDICTS: dict[
    str, tuple[Served, AntiBot | None, Callable[[onyxweb.RenderResult], bool] | None]
] = {
    # --- no signal ---------------------------------------------------------
    "clean": (Served("<html><body>hello world</body></html>"), None, None),
    "plain_403": (Served("<html><body>forbidden</body></html>", status=403), None, None),
    # --- leaky markers on a full page are not a gate ---------------------------
    "cloudflare_beacon_full": (
        _full(
            '<script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script>'
            '<div class="cf-turnstile" data-sitekey="x"></div>'
        ),
        None,
        None,
    ),
    "imperva_resource_full": (
        _full('<script src="/_Incapsula_Resource?SWJIYW=719d34d31c8e3a6e."></script>'),
        None,
        None,
    ),
    "recaptcha_form_full": (
        _full('<form><div class="g-recaptcha" data-sitekey="x"></div></form>'),
        None,
        None,
    ),
    # `px-captcha` is a cookie-consent category key on real pages.
    "px_captcha_consent_full": (
        _full('<script>window.consent={"px-captcha":"NECESSARY"};</script>'),
        None,
        None,
    ),
    # `aws-waf-token` is set on passed pages too.
    "aws_token_cookie_full": (
        Served(
            _full("").body,
            headers=(("Set-Cookie", "aws-waf-token=abc123; Path=/"),),
            full=True,
        ),
        None,
        None,
    ),
    # A solved AWS page keeps the config var but drops the loader.
    "aws_envelope_without_loader_full": (
        _full("<script>window.awsWafCookieDomainList=['x'];</script>"),
        None,
        None,
    ),
    # --- markers that ship on ordinary small pages are not a gate ---------------
    # reCAPTCHA v3 is scoreless and invisible; its badge sits on login pages.
    "recaptcha_v3_badge": (
        Served(
            _stub(
                "<script src='https://www.gstatic.com/recaptcha/releases/abc/recaptcha__en.js'>"
                "</script><div class='grecaptcha-badge' style='display:none'></div>"
                "<textarea id='g-recaptcha-response' name='g-recaptcha-response'></textarea>"
            )
        ),
        None,
        None,
    ),
    # `akam/13` loads on every Akamai-fronted page, challenged or not.
    "akamai_sensor_alone": (Served(_stub("<script src='/akam/13/abc123'></script>")), None, None),
    # --- challenges --------------------------------------------------------
    "cloudflare_stub": (
        Served(
            "<html><head><title>Just a moment...</title></head><body>"
            "<script>window._cf_chl_opt={cvId:'3',cType:'managed'};</script>"
            "<h1>Verifying you are human. This may take a few seconds.</h1></body></html>"
        ),
        _challenge("cloudflare"),
        None,
    ),
    "imperva_stub": (
        Served(
            "<html><body>"
            '<script src="/_Incapsula_Resource?SWKMTFSR=1&e=abc"></script></body></html>'
        ),
        _challenge("imperva"),
        None,
    ),
    "imperva_pardon_our_interruption": (
        Served(_stub("<h2>Pardon Our Interruption</h2><p>you are browsing quickly</p>")),
        _challenge("imperva"),
        None,
    ),
    "akamai_interstitial_stub": (
        Served('<html><body><div id="sec-if-cpt-container"></div></body></html>'),
        _challenge("akamai"),
        None,
    ),
    "akamai_sensor_with_cosignal": (
        Served(_stub("<script src='/akam/13/abc'></script><script>sensor_data='x'</script>")),
        _challenge("akamai"),
        None,
    ),
    "akamai_abck_cookie_marker": (
        Served(_stub("<script>document.cookie='_abck=xyz~-1~';</script>")),
        _challenge("akamai"),
        None,
    ),
    "recaptcha_stub": (
        Served(
            "<html><head><title>Verify</title></head><body>"
            '<div class="g-recaptcha" data-sitekey="x"></div></body></html>'
        ),
        _challenge("recaptcha"),
        None,
    ),
    "recaptcha_checkbox": (
        Served(
            _stub(
                "<iframe src='https://www.google.com/recaptcha/api2/anchor?k=x'></iframe>"
                "<div class='g-recaptcha' data-sitekey='x'></div>"
            )
        ),
        _challenge("recaptcha"),
        None,
    ),
    "hcaptcha_stub": (
        Served(
            "<html><head><title>Verify</title></head><body>"
            '<div class="h-captcha" data-sitekey="x"></div></body></html>'
        ),
        _challenge("hcaptcha"),
        None,
    ),
    "perimeterx_press_and_hold_entity": (
        Served(_stub("<p>Press &amp; Hold to confirm you are human</p>")),
        _challenge("perimeterx"),
        None,
    ),
    "perimeterx_press_and_hold_ampersand": (
        Served(_stub("<p>Press & Hold to confirm you are human</p>")),
        _challenge("perimeterx"),
        None,
    ),
    "perimeterx_press_and_hold_words": (
        Served(_stub("<p>press and hold to confirm you are human</p>")),
        _challenge("perimeterx"),
        None,
    ),
    # Kasada serves 200 shells too, not only 403/429.
    "kasada_body_marker": (
        Served(_stub("<script src='/ips.js'></script><script>window._kpsdk=1</script>")),
        _challenge("kasada"),
        None,
    ),
    # The AWS envelope with its live loader is an active challenge at any size.
    "aws_loader_full": (
        _full(
            "<script>window.gokuProps={};</script>"
            "<script src='https://token.awswaf.com/x/challenge.js'></script>"
        ),
        _challenge("aws"),
        None,
    ),
    # Turnstile writes its token into a light-DOM input: filled means passed.
    "turnstile_token": (
        Served(_TURNSTILE.format(value=" value='0.abc123token'", rest="")),
        _challenge("cloudflare", resolved=True),
        None,
    ),
    "turnstile_empty_token": (
        Served(_TURNSTILE.format(value=" value=''", rest="")),
        _challenge("cloudflare"),
        None,
    ),
    "turnstile_no_value_yet": (
        Served(_TURNSTILE.format(value="", rest="")),
        _challenge("cloudflare"),
        None,
    ),
    # The widget's iframe hides in a closed shadow root; the token still reads.
    "turnstile_token_beside_closed_shadow": (
        Served(
            _TURNSTILE.format(
                value=" value='0.tok'",
                rest="<script>document.querySelector('.cf-turnstile')"
                ".attachShadow({mode:'closed'})"
                ".innerHTML = '<iframe title=\"widget\"></iframe>';</script>",
            )
        ),
        _challenge("cloudflare", resolved=True),
        lambda r: r.dom.query_one("iframe") is None,
    ),
    # --- header and status signals ----------------------------------------------
    # `cf-mitigated: challenge` marks a challenge on a 403 even with no body markers.
    "cloudflare_cf_mitigated_header": (
        Served(
            _full("").body,
            status=403,
            headers=(("Server", "cloudflare"), ("cf-mitigated", "challenge")),
            full=True,
        ),
        _challenge("cloudflare"),
        None,
    ),
    "aws_challenge_action_header": (
        Served(
            "<html><body>challenge</body></html>",
            status=202,
            headers=(("x-amzn-waf-action", "challenge"),),
        ),
        _challenge("aws"),
        None,
    ),
    # CAPTCHA is interactive, so onyxweb can't solve it.
    "aws_captcha_action_header": (
        Served(
            "<html><body>captcha</body></html>",
            status=405,
            headers=(("x-amzn-waf-action", "captcha"),),
        ),
        _challenge("aws"),
        None,
    ),
    "akamai_block_403": (
        Served(
            "<html><head><title>Access Denied</title></head><body>"
            "You don't have permission. Reference #18.abcdef</body></html>",
            status=403,
            headers=(("Server", "AkamaiGHost"),),
        ),
        _block("akamai"),
        None,
    ),
    "kasada_ct_header_429": (
        Served("", status=429, headers=(("x-kpsdk-ct", "01HZ...token"),)),
        _block("kasada"),
        None,
    ),
    # Fastly / Signal Sciences block with 406, not 403/429.
    "fastly_406_signature": (
        Served("<title>Access Denied</title>", status=406),
        _block(None),
        None,
    ),
}


@pytest.fixture(scope="module")
def client() -> Iterator[onyxweb.Client]:
    with onyxweb.Client(concurrency=1) as c:
        yield c


@pytest.mark.parametrize("row", list(VERDICTS))
def test_verdict(client: onyxweb.Client, httpserver: HTTPServer, row: str) -> None:
    """Each response maps to its verdict; detection runs with bypass off."""
    served, expected, premise = VERDICTS[row]
    assert (len(served.body.encode()) > STUB_MAX_BYTES) == served.full, (
        "fixture sits on the wrong side of the stub gate"
    )
    httpserver.expect_request(f"/{row}").respond_with_response(
        Response(
            served.body,
            status=served.status,
            headers=list(served.headers),
            content_type="text/html",
        )
    )
    r = client.fetch(httpserver.url_for(f"/{row}"))
    assert r.status_code == served.status
    if premise is not None:
        assert premise(r), "the capture doesn't meet the row's premise"
    assert r.anti_bot == expected
    assert reloaded(r).anti_bot == expected  # the verdict survives a snapshot


# --- bypass scenarios --------------------------------------------------------


@dataclass
class Site:
    """A stateful server-side behavior, plus what it saw."""

    handler: Callable[[Request], Response]
    cookies: list[str] = field(default_factory=list)  # Cookie header of each request


def _poisoning() -> Site:
    """200 that sets `_abck` and a benign cookie; 403 from Akamai once `_abck` comes back."""
    site = Site(handler=lambda _r: Response())

    def handler(request: Request) -> Response:
        cookie = request.headers.get("Cookie", "")
        site.cookies.append(cookie)
        if "_abck=flagged" in cookie:
            resp = Response(
                "<html><head><title>Access Denied</title></head>"
                "<body>You don't have permission. Reference #18.abcdef</body></html>",
                status=403,
                content_type="text/html",
            )
            resp.headers["Server"] = "AkamaiGHost"
        else:
            resp = Response(
                "<html><head><title>Real Page</title></head><body>ok</body></html>",
                content_type="text/html",
            )
        resp.headers.add("Set-Cookie", "_abck=flagged; Path=/")
        resp.headers.add("Set-Cookie", "sess=keepme; Path=/")
        return resp

    site.handler = handler
    return site


def _plain_403() -> Site:
    site = Site(handler=lambda _r: Response())

    def handler(request: Request) -> Response:
        site.cookies.append(request.headers.get("Cookie", ""))
        return Response("<html><body>forbidden</body></html>", status=403, content_type="text/html")

    site.handler = handler
    return site


def _challenge_then_real() -> Site:
    """An Akamai interstitial that sets `solved` and reloads into a 20 KB real page.

    20 KB sits between the resolved-page floor (15 KB) and the stub gate (30 KB).
    """
    site = Site(handler=lambda _r: Response())

    def handler(request: Request) -> Response:
        cookie = request.headers.get("Cookie", "")
        site.cookies.append(cookie)
        if "solved=1" in cookie:
            body = (
                "<html><head><title>Real Page</title></head><body>"
                + "x" * 20_000
                + "</body></html>"
            )
        else:
            body = (
                '<html><body><div id="sec-if-cpt-container"></div>'
                '<script>document.cookie="solved=1;path=/";'
                "setTimeout(function(){location.reload();},800);</script></body></html>"
            )
        return Response(body, content_type="text/html")

    site.handler = handler
    return site


@dataclass(frozen=True)
class Scenario:
    """Fetches against one site; the last fetch is judged."""

    site: Callable[[], Site]
    client: dict[str, Any]
    fetches: tuple[dict[str, Any], ...]
    status: int
    shows: str  # text the judged page contains
    verdict: AntiBot | None
    requests: int | None = None  # total server hits, when retries matter
    keeps_cookie: str | None = None  # benign cookie the judged request still sent
    drops_cookie: str | None = None  # anti-bot cookie it no longer sent
    max_s: float | None = None  # wall time of the judged fetch


_HEALED = AntiBot(vendor="akamai", kind="block", resolved=True)
SCENARIOS: dict[str, Scenario] = {
    # The first fetch poisons the tab's jar; the second meets the block.
    "heals_a_block_per_fetch": Scenario(
        _poisoning,
        {},
        ({}, {"bypass_anti_bot": True}),
        200,
        "Real Page",
        _HEALED,
        keeps_cookie="sess=keepme",
        drops_cookie="_abck",
    ),
    "heals_a_block_by_client_default": Scenario(
        _poisoning,
        {"bypass_anti_bot": True},
        ({}, {}),
        200,
        "Real Page",
        _HEALED,
        keeps_cookie="sess=keepme",
        drops_cookie="_abck",
    ),
    "leaves_a_block_without_bypass": Scenario(
        _poisoning,
        {},
        ({}, {}),
        403,
        "Access Denied",
        _block("akamai"),
        requests=2,
    ),
    "never_retries_a_plain_403": Scenario(
        _plain_403,
        {},
        ({"bypass_anti_bot": True},),
        403,
        "forbidden",
        None,
        requests=1,
    ),
    "captures_a_challenge_without_bypass": Scenario(
        _challenge_then_real,
        {},
        ({},),
        200,
        "sec-if-cpt-container",
        _challenge("akamai"),
    ),
    # Waiting ends when the real page arrives, far short of the 12 s cap.
    "waits_out_a_challenge_with_bypass": Scenario(
        _challenge_then_real,
        {"bypass_anti_bot": True},
        ({"timeout_ms": 30_000},),
        200,
        "Real Page",
        _challenge("akamai", resolved=True),
        max_s=CHALLENGE_MAX_WAIT_S / 2,
    ),
}


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_bypass_scenario(httpserver: HTTPServer, name: str) -> None:
    """What ``bypass_anti_bot`` does with a verdict; each scenario gets a fresh cookie jar."""
    s = SCENARIOS[name]
    site = s.site()
    httpserver.expect_request("/").respond_with_handler(site.handler)
    url = httpserver.url_for("/")
    with onyxweb.Client(concurrency=1, **s.client) as client:
        for kwargs in s.fetches[:-1]:
            client.fetch(url, **kwargs)
        started = time.perf_counter()
        r = client.fetch(url, **s.fetches[-1])
        elapsed = time.perf_counter() - started
    assert r.status_code == s.status
    assert s.shows in r
    assert r.anti_bot == s.verdict
    if s.requests is not None:
        assert len(site.cookies) == s.requests
    if s.keeps_cookie is not None:
        assert s.keeps_cookie in site.cookies[-1]
    if s.drops_cookie is not None:
        assert s.drops_cookie not in site.cookies[-1]
    if s.max_s is not None:
        assert elapsed < s.max_s, f"waited {elapsed:.1f} s for a page that resolved"
