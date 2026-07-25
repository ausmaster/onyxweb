"""Stealth — UA override on the wire, UA-CH metadata, init scripts, presets.

These tests exercise the two new config primitives (``user_agent_metadata``
and ``ScriptsConfig``) end-to-end — Python → Rust → CDP → actual HTTP headers
and actual document mutations.

Uses ``pytest-httpserver`` for wire-level header assertions (first consumer of
this dev dep). The CNN benchmark at the bottom is the regression guard that
would have caught today's ``HeadlessChrome`` substring tripwire.
"""

from __future__ import annotations

import re

import onyxweb
import pytest
from onyxweb._download_chrome import CHROME_VERSION
from onyxweb.presets.shell import stealth
from pytest_httpserver import HTTPServer

# ---------------------------------------------------------------------------
# UA / UA-CH on the wire
# ---------------------------------------------------------------------------


def test_user_agent_header_on_wire(httpserver: HTTPServer) -> None:
    """Baseline — the plain UA override plumbs through to the actual request."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body>ok</body></html>", content_type="text/html"
    )
    with onyxweb.Client(concurrency=1, user_agent="OnyxTest/1.0") as c:
        c.fetch(httpserver.url_for("/"))
    req = httpserver.log[0][0]
    assert req.headers.get("User-Agent") == "OnyxTest/1.0"


def test_user_agent_metadata_emits_sec_ch_ua(httpserver: HTTPServer) -> None:
    """The structured metadata lands in ``Sec-CH-UA-*`` client-hint headers.

    Chrome sends the three low-entropy hints (``Sec-CH-UA``,
    ``Sec-CH-UA-Mobile``, ``Sec-CH-UA-Platform``) on every navigation by
    default — we assert only those, since high-entropy hints require the
    server to request them via ``Accept-CH``.
    """
    httpserver.expect_request("/").respond_with_data(
        "<html><body>ok</body></html>", content_type="text/html"
    )
    meta = {
        "brands": [
            {"brand": "Google Chrome", "version": "131"},
            {"brand": "Not_A Brand", "version": "24"},
        ],
        "platform": "Linux",
        "platform_version": "",
        "architecture": "x86",
        "model": "",
        "mobile": False,
    }
    with onyxweb.Client(
        concurrency=1,
        user_agent="Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        user_agent_metadata=meta,
    ) as c:
        c.fetch(httpserver.url_for("/"))
    req = httpserver.log[0][0]

    sec_ch_ua = req.headers.get("Sec-CH-UA") or ""
    assert "Google Chrome" in sec_ch_ua
    assert "131" in sec_ch_ua
    # mobile=False encodes as "?0", mobile=True as "?1"
    assert req.headers.get("Sec-CH-UA-Mobile") == "?0"
    assert req.headers.get("Sec-CH-UA-Platform") == '"Linux"'


# ---------------------------------------------------------------------------
# Init scripts — one test per timing / scope variant
# ---------------------------------------------------------------------------


def test_on_new_document_runs_and_mutates_dom(httpserver: HTTPServer) -> None:
    """The canonical CDP primitive — fires before any page script."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body></body></html>", content_type="text/html"
    )
    script = """
    document.addEventListener('DOMContentLoaded', function() {
      document.documentElement.setAttribute('data-newdoc', 'ok');
    });
    """
    with onyxweb.Client(
        concurrency=1,
        scripts={"on_new_document": [script]},
    ) as c:
        result = c.fetch(httpserver.url_for("/"))
    assert 'data-newdoc="ok"' in str(result)


def test_on_dom_content_loaded_sugar_wraps_listener(httpserver: HTTPServer) -> None:
    """The DCL sugar wraps the source in a DOMContentLoaded listener."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body></body></html>", content_type="text/html"
    )
    with onyxweb.Client(
        concurrency=1,
        scripts={"on_dom_content_loaded": [
            "document.body.setAttribute('data-dcl', 'yes');"
        ]},
    ) as c:
        result = c.fetch(httpserver.url_for("/"))
    assert 'data-dcl="yes"' in str(result)


def test_on_load_sugar_wraps_listener(httpserver: HTTPServer) -> None:
    """The load sugar wraps the source in a window.load listener."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body></body></html>", content_type="text/html"
    )
    with onyxweb.Client(
        concurrency=1,
        scripts={"on_load": [
            "document.body.setAttribute('data-onload', 'yes');"
        ]},
    ) as c:
        result = c.fetch(httpserver.url_for("/"))
    assert 'data-onload="yes"' in str(result)


def test_isolated_world_invisible_to_main_world(httpserver: HTTPServer) -> None:
    """Isolated-world scripts run in a separate JS global; main-world scripts
    can't see the globals they set. The DOM is shared (both worlds can mutate
    ``document``), so we use a dataset attribute to exfiltrate the main-world
    observation."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body></body></html>", content_type="text/html"
    )
    with onyxweb.Client(
        concurrency=1,
        scripts={
            "isolated_world": ["window.__iso_marker = 'yes';"],
            "on_load": [
                "document.body.setAttribute('data-main-sees-iso', "
                "(typeof window.__iso_marker === 'undefined') ? 'no' : 'yes');"
            ],
        },
    ) as c:
        result = c.fetch(httpserver.url_for("/"))
    assert 'data-main-sees-iso="no"' in str(result)


def test_url_scoped_only_fires_on_substring_match(httpserver: HTTPServer) -> None:
    """Scripts keyed in ``url_scoped`` fire only when the URL contains the key."""
    httpserver.expect_request("/foo").respond_with_data(
        "<html><body></body></html>", content_type="text/html"
    )
    httpserver.expect_request("/bar").respond_with_data(
        "<html><body></body></html>", content_type="text/html"
    )
    with onyxweb.Client(
        concurrency=1,
        scripts={
            "url_scoped": {
                "/foo": [
                    "document.addEventListener('DOMContentLoaded', function() {"
                    "  document.body.setAttribute('data-scoped', 'hit');"
                    "});"
                ],
            }
        },
    ) as c:
        foo_html = str(c.fetch(httpserver.url_for("/foo")))
        bar_html = str(c.fetch(httpserver.url_for("/bar")))
    assert 'data-scoped="hit"' in foo_html
    assert "data-scoped" not in bar_html


# ---------------------------------------------------------------------------
# Preset bundles — dict-spread construction works end-to-end
# ---------------------------------------------------------------------------


def test_basic_ua_major_matches_chrome_version() -> None:
    """Stealth UA's Chrome major must match the bundled CHROME_VERSION.

    Detectors compare UA-claimed version against JS-feature shape; a mismatch
    is itself a fingerprint tell. Bumping ``CHROME_VERSION`` in
    ``_download_chrome.py`` requires a paired bump of ``BASIC_UA`` and
    ``BASIC_UA_METADATA`` here.
    """
    chrome_major = CHROME_VERSION.split(".")[0]
    m = re.search(r"Chrome/(\d+)", stealth.BASIC_UA)
    assert m is not None, f"BASIC_UA has no Chrome/<n>: {stealth.BASIC_UA!r}"
    assert m.group(1) == chrome_major, (
        f"BASIC_UA major {m.group(1)} != CHROME_VERSION major {chrome_major}"
    )
    for brand in stealth.BASIC_UA_METADATA["brands"]:
        if brand["brand"] in ("Google Chrome", "Chromium"):
            assert brand["version"] == chrome_major, (
                f"brand {brand['brand']} version {brand['version']} != {chrome_major}"
            )


def test_download_engine_specs() -> None:
    """The downloader's per-engine layout must match the Rust resolver.

    full → ``full/chrome`` subdir; shell → flat ``chrome-headless-shell``. If
    these drift, ``onyxweb-download-chrome`` puts the binary where
    ``chrome::find_bundled`` won't look.
    """
    from onyxweb._download_chrome import _engine_download

    assert _engine_download("full", "linux64") == ("chrome-linux64", "chrome", "full")
    assert _engine_download("shell", "linux64") == (
        "chrome-headless-shell-linux64", "chrome-headless-shell", "",
    )
    assert _engine_download("full", "win64")[1] == "chrome.exe"
    with pytest.raises(ValueError):
        _engine_download("bogus", "linux64")


def test_stealth_basic_preset_constructs() -> None:
    """``Client(**stealth.BASIC)`` spreads into the config hierarchy cleanly."""
    with onyxweb.Client(**stealth.BASIC) as c:
        ua = c.config.network.user_agent
        assert ua is not None and ua.endswith("Safari/537.36")
        assert "HeadlessChrome" not in ua
        meta = c.config.network.user_agent_metadata
        assert meta is not None and meta.platform == "Linux"
        # 5 Phase-1 patches.
        assert len(c.config.scripts.on_new_document) == 5


def test_stealth_fingerprint_preset_constructs() -> None:
    with onyxweb.Client(**stealth.FINGERPRINT) as c:
        # 5 basic + WebGL + canvas-noise = 7
        assert len(c.config.scripts.on_new_document) == 7


def test_preset_overridable_via_pre_merge() -> None:
    """Users tweak a preset field by pre-merging into a fresh dict. Python
    forbids duplicate keys across multiple ``**`` spreads (or between a ``**``
    and an explicit kwarg) in the same call, so the idiom is to build the
    merged dict first, then spread once."""
    with onyxweb.Client(**{**stealth.BASIC, "user_agent": "MyBot/9.9"}) as c:
        assert c.config.network.user_agent == "MyBot/9.9"
        # Rest of the preset is preserved.
        assert len(c.config.scripts.on_new_document) == 5


def test_stealth_webdriver_getter_stringifies_native() -> None:
    """Under stealth.BASIC the patched webdriver getter — and our patched
    toString itself — report ``[native code]``, closing the arrow-getter
    ``.toString()`` tell, while webdriver still reads undefined."""
    with onyxweb.Client(**stealth.BASIC) as c:
        r = c.fetch(
            "data:text/html,<html></html>",
            post_load_scripts=[
                "Object.getOwnPropertyDescriptor(Navigator.prototype,'webdriver').get.toString()",
                "Function.prototype.toString.toString()",
                "navigator.webdriver",
            ],
        )
    wd = r.post_load_results[0]
    assert "[native code]" in wd and "webdriver" in wd, wd
    assert "[native code]" in r.post_load_results[1]
    assert r.post_load_results[2] is None


def test_stealth_basic_removes_headless_substring(httpserver: HTTPServer) -> None:
    """Regression guard at the wire level — ``HeadlessChrome`` must not appear
    in the UA when stealth.BASIC is active. This is the specific tripwire that
    Akamai first-byte-matched against cnn.com."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body>ok</body></html>", content_type="text/html"
    )
    with onyxweb.Client(concurrency=1, **stealth.BASIC) as c:
        c.fetch(httpserver.url_for("/"))
    ua = httpserver.log[0][0].headers.get("User-Agent") or ""
    assert "HeadlessChrome" not in ua
    assert f"Chrome/{CHROME_VERSION.split('.')[0]}" in ua


def test_full_engine_has_webgl_context() -> None:
    """The full engine must expose a WebGL context.

    ``--disable-gpu`` without a software fallback leaves
    ``getContext('webgl') === null`` — a strong headless tell. Skipped when full
    Chrome isn't installed.
    """
    from onyxweb.presets.full import stealth as full_stealth

    try:
        client = onyxweb.Client(navigation_timeout_ms=15_000, **full_stealth.BASIC)
    except onyxweb.OnyxwebError as e:
        if "not found" in str(e).lower():
            pytest.skip(f"full Chrome unavailable: {e}")
        raise
    try:
        r = client.fetch(
            "data:text/html,<html></html>",
            post_load_scripts=["!!document.createElement('canvas').getContext('webgl')"],
        )
    finally:
        client.close()
    assert r.post_load_results[0] is True, "full engine exposes no WebGL context"


# ---------------------------------------------------------------------------
# Real-site regression guard
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
@pytest.mark.real_sites
def test_stealth_basic_preset_fetches_cnn() -> None:
    """Without stealth, cnn.com returns ~250 B ``Unknown Error`` because Akamai
    first-byte-matches ``HeadlessChrome`` in the UA. With ``stealth.BASIC``,
    the real 5 MB homepage comes through."""
    with onyxweb.Client(**stealth.BASIC, navigation_timeout_ms=20_000) as c:
        html = c.fetch("https://cnn.com")
    assert len(html) > 1_000_000, (
        f"cnn.com with stealth.BASIC returned only {len(html)} bytes — "
        f"anti-bot tripwire reactivated? Body head: {str(html)[:400]!r}"
    )
