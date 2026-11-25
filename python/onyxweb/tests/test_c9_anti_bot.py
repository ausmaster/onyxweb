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

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

import pytest
from conftest import reloaded
from onyxweb import AntiBot, Client, RenderResult
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Request, Response

STUB_MAX_BYTES = 30_720  # mirrors CHALLENGE_STUB_MAX_BYTES in src/engine.rs
CHALLENGE_MAX_WAIT_S = 20.0  # mirrors CHALLENGE_MAX_WAIT_MS in src/engine.rs


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


def _held_gate(label: str = "Continue", opens_after_ms: int = 0, leads_to: str = "") -> str:
    """A waiting room that holds its own control closed, shaped like a capture of a real one.

    The control is a ``role=button`` holding its label in a child, closed by a ``disabled``
    attribute its own script removes after a delay; only then does a click lead anywhere. Element
    ids differ on every response, so the script's own names are what the page is known by.
    ``leads_to`` is where a click goes; the script appends how many presses it took.
    """
    return (
        f'<div disabled="" id="GfYR0" role="button" tabindex="0"><div>{label}</div></div>'
        "<script>(function(){var button_id='GfYR0';"
        f"var delayMillis={opens_after_ms};var redirectUrl='{leads_to}';var presses=0;"
        "(function(){var continueBtn = document.getElementById(button_id);"
        "var targetUrl = redirectUrl;"
        "setTimeout(function(){continueBtn.removeAttribute('disabled');"
        "continueBtn.addEventListener('click',function(){presses+=1;if(presses===1){"
        "setTimeout(function(){window.location.replace(targetUrl+presses);},1200);}});"
        "},delayMillis);})();})();</script>"
    )


def _challenge(vendor: str, resolved: bool = False) -> AntiBot:
    return AntiBot(vendor=vendor, kind="challenge", resolved=resolved)


def _block(vendor: str | None) -> AntiBot:
    return AntiBot(vendor=vendor, kind="block", resolved=False)


# Row -> (response served, verdict expected, premise the capture must meet or None).
VERDICTS: dict[
    str, tuple[Served, AntiBot | None, Callable[[RenderResult], bool] | None]
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
    # A control called `continueBtn` sits on checkout pages and sign-up forms everywhere, and
    # any status line can say a request is being verified: neither is a gate by itself.
    "continue_button_alone": (
        Served(_stub('<button id="continueBtn">Continue</button>')),
        None,
        None,
    ),
    "continue_button_alone_full": (_full('<button id="continueBtn">Continue</button>'), None, None),
    "continue_button_named_in_a_script": (
        Served(_stub("<script>var continueBtn = document.getElementById('main');</script>")),
        None,
        None,
    ),
    "verifying_phrase_alone": (Served(_stub("<p>Verifying your request</p>")), None, None),
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
    # Google's waiting room: a 200 with no WAF vendor and no captcha, holding its own Continue
    # control closed while it checks. Its wording is translated and its ids are random, so it
    # is known by its script, whose names appear together in every capture of the gate and in
    # no capture of a results page.
    "google_waiting_room": (
        Served(
            "<html><head><title>Search</title></head><body>"
            f"<h1>Verifying your request</h1>{_held_gate()}</body></html>"
        ),
        _challenge("google"),
        None,
    ),
    "google_waiting_room_in_another_language": (
        Served(f"<html><body><h1>Anfrage wird geprüft</h1>{_held_gate('Weiter')}</body></html>"),
        _challenge("google"),
        None,
    ),
    # The real one is about 88 KB: well past the stub gate, so its size must not excuse it.
    "google_waiting_room_full": (_full(_held_gate()), _challenge("google"), None),
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
    # A bare 429 is the origin refusing this client for now, and naming no vendor does not make
    # that less so: the status alone is the signal, so a rate-limit page is not read as an ordinary
    # page that happens to be empty. No vendor is invented from its wording.
    "rate_limited_429_no_vendor": (
        Served("<html><body><div id=root></div></body></html>", status=429),
        _block(None),
        None,
    ),
    # A 429 whose page is a challenge the detector knows stays that challenge: the status rule is
    # for the page that names nothing, and must not shadow a verdict that says more.
    "challenge_on_a_429_keeps_its_vendor": (
        Served(
            "<html><head><title>Just a moment...</title></head><body>"
            "<script>window._cf_chl_opt={cvId:'3',cType:'managed'};</script>"
            "<h1>Verifying you are human. This may take a few seconds.</h1></body></html>",
            status=429,
        ),
        _challenge("cloudflare"),
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
def client() -> Iterator[Client]:
    with Client(concurrency=1) as c:
        yield c


@pytest.mark.parametrize("row", list(VERDICTS))
def test_verdict(client: Client, httpserver: HTTPServer, row: str) -> None:
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


def _captcha_wall() -> Site:
    """A rate-limit page carrying an interactive captcha: not something a wait can pass."""
    return Site(
        handler=lambda _r: Response(
            "<html><head><title>Unusual traffic</title></head><body>"
            "<p>Our systems have detected unusual traffic from your computer network.</p>"
            '<div class="g-recaptcha" data-sitekey="abc"></div>'
            "<p>This page checks to see if it's really you sending the requests.</p>"
            "</body></html>",
            content_type="text/html",
        )
    )


def _wall() -> Site:
    """A challenge that never moves and offers nothing to press: a refusal dressed as a page."""
    return Site(
        handler=lambda _r: Response(
            '<html><body><div id="cf-browser-verification"></div>'
            "<p>Checking your browser before accessing.</p></body></html>",
            content_type="text/html",
        )
    )


def _gated_waiting_room(label: str = "Continue") -> Site:
    """A waiting room that never reloads itself: it opens its control late, and a press leads on.

    It stays closed for longer than a wall is given to show something, so passing it takes both
    waiting for the control and pressing it. The real page says how many presses it took.
    """
    site = Site(handler=lambda _r: Response())

    def handler(request: Request) -> Response:
        site.cookies.append(request.headers.get("Cookie", ""))
        presses = request.args.get("presses")
        if presses is not None:  # where the control leads
            body = (
                "<html><head><title>Real Page</title></head><body>"
                f"<p>pressed {presses} time(s)</p>" + "x" * 20_000 + "</body></html>"
            )
        else:
            gate = _held_gate(label, opens_after_ms=3000, leads_to="/?presses=")
            body = f"<html><head><title>Search</title></head><body>{gate}</body></html>"
        return Response(body, content_type="text/html")

    site.handler = handler
    return site


_NEVER_CLICKED = (
    '<p id="state">never clicked</p><button id="continueBtn" '
    "onclick=\"document.getElementById('state').textContent='clicked'\">Continue</button>"
)


def _ordinary_page() -> Site:
    """An ordinary page with a Continue button of its own, which is nobody's to press."""
    return Site(
        handler=lambda _r: Response(
            f"<html><head><title>Checkout</title></head><body>{_NEVER_CLICKED}"
            f"{'<p>page content row</p>' * 2000}</body></html>",
            content_type="text/html",
        )
    )


def _wall_with_an_open_control() -> Site:
    """A challenge that never moves, beside a control it never held closed."""
    return Site(
        handler=lambda _r: Response(
            '<html><body><div id="cf-browser-verification"></div>'
            f"<p>Checking your browser before accessing.</p>{_NEVER_CLICKED}</body></html>",
            content_type="text/html",
        )
    )


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
    # Waiting ends when the real page arrives, far short of the cap.
    "waits_out_a_challenge_with_bypass": Scenario(
        _challenge_then_real,
        {"bypass_anti_bot": True},
        ({"timeout_ms": 30_000},),
        200,
        "Real Page",
        _challenge("akamai", resolved=True),
        max_s=CHALLENGE_MAX_WAIT_S / 2,
    ),
    # A waiting room that never reloads itself and opens its control late: the heal waits for the
    # control, presses it once, and the page it leads to is what the caller gets.
    "presses_a_gate_once_it_opens": Scenario(
        _gated_waiting_room,
        {"bypass_anti_bot": True},
        ({"timeout_ms": 30_000},),
        200,
        "pressed 1 time(s)",
        _challenge("google", resolved=True),
        max_s=CHALLENGE_MAX_WAIT_S / 2,
    ),
    # The control is known by the gate having held it closed, never by what it says: the same
    # gate in another language passes the same way.
    "presses_a_gate_in_any_language": Scenario(
        lambda: _gated_waiting_room(label="Weiter"),
        {"bypass_anti_bot": True},
        ({"timeout_ms": 30_000},),
        200,
        "pressed 1 time(s)",
        _challenge("google", resolved=True),
        max_s=CHALLENGE_MAX_WAIT_S / 2,
    ),
    # A control the challenge never held closed is not the gate's, so it is never pressed.
    "never_presses_a_control_the_gate_did_not_hold": Scenario(
        _wall_with_an_open_control,
        {"bypass_anti_bot": True},
        ({"timeout_ms": 30_000},),
        200,
        "never clicked",
        _challenge("cloudflare"),
        max_s=CHALLENGE_MAX_WAIT_S / 4,
    ),
    # An ordinary page is returned as it is, at once, with its own controls untouched.
    "leaves_an_ordinary_page_alone": Scenario(
        _ordinary_page,
        {"bypass_anti_bot": True},
        ({"timeout_ms": 30_000},),
        200,
        "never clicked",
        None,
        max_s=CHALLENGE_MAX_WAIT_S / 4,
    ),
    # A refusal that never moves and offers nothing to press is reported at once rather than
    # waited out: waiting cannot pass it, and the caller needs the verdict, not the delay.
    "reports_a_wall_without_waiting_it_out": Scenario(
        _wall,
        {"bypass_anti_bot": True},
        ({"timeout_ms": 30_000},),
        200,
        "Checking your browser",
        _challenge("cloudflare"),
        max_s=CHALLENGE_MAX_WAIT_S / 4,
    ),
    # An interactive captcha cannot be waited out or pressed: report it as it stands, at once.
    "reports_an_interactive_captcha_at_once": Scenario(
        _captcha_wall,
        {"bypass_anti_bot": True},
        ({"timeout_ms": 30_000},),
        200,
        "unusual traffic",
        _challenge("recaptcha"),
        max_s=CHALLENGE_MAX_WAIT_S / 4,
    ),
}


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_bypass_scenario(httpserver: HTTPServer, name: str) -> None:
    """What ``bypass_anti_bot`` does with a verdict; each scenario gets a fresh cookie jar."""
    s = SCENARIOS[name]
    site = s.site()
    httpserver.expect_request("/").respond_with_handler(site.handler)
    url = httpserver.url_for("/")
    with Client(concurrency=1, **s.client) as client:
        for kwargs in s.fetches[:-1]:
            client.fetch(url, **kwargs)
        started = perf_counter()
        r = client.fetch(url, **s.fetches[-1])
        elapsed = perf_counter() - started
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
