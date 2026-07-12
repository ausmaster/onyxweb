"""Phase 5 acceptance: a real DOMino-style XSS detection flow produces a
finding via onyxweb's primitives. No new public surface — these tests
prove that the existing primitives (init scripts, console capture,
``post_load_scripts``, ``block_navigation``) compose to handle DOMino's
three capability buckets:

- **Bucket A** (``BaseInput`` + ``Alert_fire``): payload in URL is reflected
  unsanitized; an init-script detector hooks ``window.alert``; the page's
  rendered ``<script>`` triggers the alert, hook reports via ``console.error``.
- **Bucket B** (``FillAndSubmit`` + ``Eval_sniffer``): an init-script detector
  hooks ``window.eval``; ``post_load_scripts`` fills a form input and clicks
  submit; the page's ``onsubmit`` invokes ``window.eval(value)``; hook fires.
- **Bucket C** (``ClickJSElements`` + ``Dom_search``): ``post_load_scripts``
  with ``block_navigation`` clicks every ``[href^="javascript:"]`` element on
  the same page state; a follow-up ``post_load_script`` scans the DOM for
  injection patterns and reports each finding via ``console.error``.

The acceptance bar from the plan is "one input × one detector pair produces
a finding." Bucket A is that minimum; B and C exercise the full stack.
"""

from __future__ import annotations

from urllib.parse import quote

import onyxweb
from pytest_httpserver import HTTPServer

# ----------------------------------------------------------------------------
# Bucket A: BaseInput + Alert_fire
# ----------------------------------------------------------------------------


def test_domino_bucket_a_baseinput_alert_fire_finds_reflected_xss(
    httpserver: HTTPServer,
) -> None:
    """Reflected-XSS detection via init-script alert hook + console capture.

    The vulnerable endpoint's response embeds ``q`` into ``<body>``
    unsanitized — the canonical DOMino ``BaseInput`` shape. The payload is
    a ``<script>alert("XSS-A")</script>`` tag that fires synchronously
    during page-script execution. A ``Client``-level init script
    (``FetchConfig.scripts``) hooks ``window.alert`` BEFORE the page's
    scripts run, so the hook is in place when the payload fires.

    Note: the hook intentionally does NOT call the original ``alert`` —
    real XSS detectors follow this pattern: log the finding, swallow the
    side effect. (The engine also auto-dismisses dialogs — see
    ``test_dialog_handling.py`` — so calling orig would be safe too. The
    hook stays side-effect-free as the cleaner detector style.)
    """
    payload = '<script>alert("XSS-A")</script>'
    httpserver.expect_request("/vuln").respond_with_data(
        f"<html><body><h1>q={payload}</h1></body></html>",
        content_type="text/html",
    )

    alert_fire_detector = """
    (() => {
        window.alert = function(...args) {
            try { console.error('FINDING:alert:' + JSON.stringify(args)); }
            catch (e) { console.error('FINDING:alert:<unstringifiable>'); }
        };
    })();
    """

    url = httpserver.url_for("/vuln") + "?q=" + quote(payload)

    with onyxweb.Client() as c:
        r = c.fetch(url, scripts=[alert_fire_detector])

    findings = [m.text for m in r.console_messages if m.text.startswith("FINDING:alert:")]
    assert any("XSS-A" in f for f in findings), (
        f"alert hook didn't fire on reflected payload; console: "
        f"{[m.text for m in r.console_messages]}"
    )


# ----------------------------------------------------------------------------
# Bucket B: FillAndSubmit + Eval_sniffer
# ----------------------------------------------------------------------------


def test_domino_bucket_b_fillandsubmit_eval_sniffer_finds_eval(
    httpserver: HTTPServer,
) -> None:
    """Form-fill XSS detection via init-script eval hook + post_load_scripts.

    The page has a form whose ``onsubmit`` invokes ``window.eval(value)`` —
    indirect eval, which goes through the (writable) ``window.eval`` slot
    and IS hookable. A ``Client``-level init script overrides
    ``window.eval`` and reports each call via ``console.error``.
    ``post_load_scripts`` fills the input and clicks submit; the
    ``onsubmit`` runs the eval, the hook fires, and the payload also runs
    (we assert both, so a silent hook failure is caught).
    """
    httpserver.expect_request("/").respond_with_data(
        "<html><body>"
        "<form id='f' onsubmit='window.eval(this.q.value); return false'>"
        "<input name='q' />"
        "<button type='submit'>Go</button>"
        "</form>"
        "</body></html>",
        content_type="text/html",
    )

    eval_sniffer = """
    (() => {
        const orig = window.eval;
        window.eval = function(code) {
            console.error('FINDING:eval:' + code);
            return orig.call(this, code);
        };
    })();
    """

    fill_submit = """
    (() => {
        document.querySelector('input[name=q]').value =
            "console.error('PAYLOAD_RAN_VIA_EVAL')";
        document.querySelector('button[type=submit]').click();
    })();
    """

    with onyxweb.Client() as c:
        r = c.fetch(
            httpserver.url_for("/"),
            scripts=[eval_sniffer],
            post_load_scripts=[fill_submit],
        )

    texts = [m.text for m in r.console_messages]
    assert any("FINDING:eval:" in t and "PAYLOAD_RAN_VIA_EVAL" in t for t in texts), (
        f"eval sniffer didn't fire on form submit; console: {texts}"
    )
    assert any(t == "PAYLOAD_RAN_VIA_EVAL" for t in texts), (
        f"payload didn't actually run through eval (hook may have shadowed); "
        f"console: {texts}"
    )


# ----------------------------------------------------------------------------
# Bucket C: ClickJSElements + Dom_search
# ----------------------------------------------------------------------------


def test_domino_bucket_c_clickjselements_domsearch_finds_injections(
    httpserver: HTTPServer,
) -> None:
    """JS-link click loop + post-click DOM scan, with navigation blocked.

    The page has three ``[href^="javascript:"]`` links: two inject DOM
    nodes (the "vulnerable" pattern Dom_search looks for); the middle one
    triggers a navigation that, without ``block_navigation``, would
    scramble the page before later clicks fire. With ``block_navigation``
    the page state survives all three clicks; a follow-up
    ``post_load_script`` scans for ``[id^="inj"]`` and reports each via
    ``console.error``.
    """
    httpserver.expect_request("/").respond_with_data(
        "<html><body>"
        "<a id='a1' href=\"javascript:document.body.appendChild("
        "Object.assign(document.createElement('div'), "
        "{id:'inj1', textContent:'XSS_INJ_1'}))\">a1</a>"
        "<a id='a2' href=\"javascript:window.location.href='/elsewhere'\">a2</a>"
        "<a id='a3' href=\"javascript:document.body.appendChild("
        "Object.assign(document.createElement('div'), "
        "{id:'inj3', textContent:'XSS_INJ_3'}))\">a3</a>"
        "</body></html>",
        content_type="text/html",
    )
    httpserver.expect_request("/elsewhere").respond_with_data(
        "<html><body>SHOULD_NOT_REACH</body></html>",
        content_type="text/html",
    )

    click_loop = """
    document.querySelectorAll('[href^="javascript:"]').forEach(el => {
        try { el.click(); }
        catch (e) { console.error('click_failed:' + e.message); }
    });
    """

    dom_search = """
    (() => {
        document.querySelectorAll('[id^="inj"]').forEach(el => {
            console.error('FINDING:dom-injection:' + el.id + ':' + el.textContent);
        });
    })();
    """

    with onyxweb.Client() as c:
        r = c.fetch(
            httpserver.url_for("/"),
            post_load_scripts=[click_loop, dom_search],
            block_navigation=True,
        )

    findings = [
        m.text for m in r.console_messages
        if m.text.startswith("FINDING:dom-injection:")
    ]
    assert any("inj1" in f and "XSS_INJ_1" in f for f in findings), (
        f"first DOM injection missing; findings: {findings}"
    )
    assert any("inj3" in f and "XSS_INJ_3" in f for f in findings), (
        f"third DOM injection missing (page scrambled by middle click?); "
        f"findings: {findings}"
    )
    assert "/elsewhere" not in r.final_url, (
        f"middle click escaped block_navigation; final_url={r.final_url}"
    )


# ----------------------------------------------------------------------------
# AsyncClient parity: bucket A on the async path. The internals are shared
# (one ``do_*_inner`` per operation), so async parity for one bucket is
# enough to demonstrate that DOMino's async port works.
# ----------------------------------------------------------------------------


async def test_async_domino_bucket_a_baseinput_alert_fire(httpserver: HTTPServer) -> None:
    """Bucket A on the async surface — same flow, ``await ac.fetch(...)``."""
    payload = '<script>alert("XSS-async")</script>'
    httpserver.expect_request("/vuln").respond_with_data(
        f"<html><body><h1>q={payload}</h1></body></html>",
        content_type="text/html",
    )

    alert_fire_detector = """
    (() => {
        window.alert = function(...args) {
            console.error('FINDING:alert:' + JSON.stringify(args));
        };
    })();
    """

    url = httpserver.url_for("/vuln") + "?q=" + quote(payload)

    async with onyxweb.AsyncClient() as ac:
        r = await ac.fetch(url, scripts=[alert_fire_detector])

    findings = [m.text for m in r.console_messages if m.text.startswith("FINDING:alert:")]
    assert any("XSS-async" in f for f in findings), (
        f"async alert hook didn't fire; console: "
        f"{[m.text for m in r.console_messages]}"
    )
