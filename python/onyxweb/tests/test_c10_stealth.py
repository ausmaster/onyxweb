"""C10 stealth — a preset makes every fingerprint surface report one consistent identity.

A half-applied spoof is more detectable than none: a patched value that
contradicts an unpatched neighbour, or the UA on the wire disagreeing with
``navigator.userAgent`` or the client-hint brands, is a signal no real browser
emits. Each test states the real-Chrome behaviour it holds the preset to;
``full`` is used as the ground-truth oracle where one is needed (no patches,
just automation tells stripped at launch).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator

import onyxweb
import pytest
from onyxweb.download import CHROME_VERSION
from onyxweb.presets.shell import stealth as shell_stealth
from pytest_httpserver import HTTPServer

BLANK = "data:text/html,<html><body>x</body></html>"
CHROME_MAJOR = CHROME_VERSION.split(".")[0]


def _probe(client: onyxweb.Client, scripts: list[str]) -> list[object]:
    """Evaluate JS on a blank page, return each script's return value."""
    return client.fetch(BLANK, post_load_scripts=scripts).post_load_results


def _brands_from_header(raw: str) -> set[tuple[str, str]]:
    """Parse a Sec-CH-UA header into ``{(brand, version)}`` pairs."""
    return set(re.findall(r'"([^"]+)";v="(\d+)"', raw))


@pytest.fixture(scope="module")
def basic() -> Iterator[onyxweb.Client]:
    with onyxweb.Client(concurrency=1, **shell_stealth.BASIC) as c:
        yield c


@pytest.fixture(scope="module")
def fingerprint() -> Iterator[onyxweb.Client]:
    with onyxweb.Client(concurrency=1, **shell_stealth.FINGERPRINT) as c:
        yield c


@pytest.fixture(scope="module")
def plain() -> Iterator[onyxweb.Client]:
    with onyxweb.Client(concurrency=1) as c:
        yield c


# ----------------------------------------------------------------------------
# UA / UA-CH metadata — the preset's own shape, and what it produces live
# ----------------------------------------------------------------------------


def test_basic_ua_matches_chrome_version_and_full_metadata() -> None:
    """BASIC_UA's major, and every brand's full version, must match CHROME_VERSION."""
    m = re.search(r"Chrome/(\d+)", shell_stealth.BASIC_UA)
    assert m is not None, f"BASIC_UA has no Chrome/<n>: {shell_stealth.BASIC_UA!r}"
    assert m.group(1) == CHROME_MAJOR
    brand_versions = {b["brand"]: b["version"] for b in shell_stealth.BASIC_UA_METADATA["brands"]}
    full_versions = {
        b["brand"]: b["version"] for b in shell_stealth.BASIC_UA_METADATA["full_version_list"]
    }
    for brand in ("Google Chrome", "Chromium"):
        assert brand_versions[brand] == CHROME_MAJOR
        assert full_versions[brand] == CHROME_VERSION


def test_basic_identity_agrees_on_wire_js_and_client_hints(
    basic: onyxweb.Client, httpserver: HTTPServer
) -> None:
    """The HeadlessChrome-free wire UA, navigator.userAgent, and both the wire
    and JS client-hint brands must all describe the same one identity."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body>ok</body></html>", content_type="text/html"
    )
    r = basic.fetch(
        httpserver.url_for("/"),
        post_load_scripts=[
            "navigator.userAgent",
            "JSON.stringify(navigator.userAgentData.brands)",
        ],
    )
    request = httpserver.log[0][0]
    wire_ua = request.headers.get("User-Agent") or ""
    assert "HeadlessChrome" not in wire_ua
    assert f"Chrome/{CHROME_MAJOR}" in wire_ua
    assert r.post_load_results[0] == wire_ua

    preset_brands = {(b["brand"], b["version"]) for b in shell_stealth.BASIC_UA_METADATA["brands"]}
    wire_brands = _brands_from_header(request.headers.get("Sec-CH-UA") or "")
    js_brands = {(b["brand"], b["version"]) for b in json.loads(str(r.post_load_results[1]))}
    assert wire_brands == js_brands == preset_brands


# ----------------------------------------------------------------------------
# navigator.webdriver — must read false (a real boolean), not undefined
# ----------------------------------------------------------------------------


def test_webdriver_reports_false_not_undefined(basic: onyxweb.Client) -> None:
    """Real Chrome exposes `webdriver` as `false`; `undefined` means the
    property was deleted or shadowed — a state no shipping browser is in."""
    value, present = _probe(basic, ["navigator.webdriver", "'webdriver' in navigator"])
    assert present is True, "the property must exist"
    assert value is False, f"expected false, got {value!r}"


def test_webdriver_descriptor_matches_real_chrome(basic: onyxweb.Client) -> None:
    """The real descriptor is an enumerable, configurable accessor on the prototype."""
    (desc,) = _probe(
        basic,
        [
            "JSON.stringify((d => d && {e: d.enumerable, c: d.configurable, g: typeof d.get})"
            "(Object.getOwnPropertyDescriptor(Navigator.prototype, 'webdriver')))"
        ],
    )
    assert desc is not None, "webdriver must stay an own property of Navigator.prototype"
    assert json.loads(str(desc)) == {"e": True, "c": True, "g": "function"}


def test_webdriver_getter_and_tostring_report_native(basic: onyxweb.Client) -> None:
    """The patched getter — and our patched `toString` itself — stringify to
    `[native code]`, closing the arrow-getter `.toString()` tell."""
    getter_src, tostring_src, value = _probe(
        basic,
        [
            "Object.getOwnPropertyDescriptor(Navigator.prototype,'webdriver').get.toString()",
            "Function.prototype.toString.toString()",
            "navigator.webdriver",
        ],
    )
    assert "[native code]" in str(getter_src) and "webdriver" in str(getter_src)
    assert "[native code]" in str(tostring_src)
    assert value is False


# ----------------------------------------------------------------------------
# WebGL — Chrome/ANGLE dialect, no SwiftShader leak, and a real context exists
# ----------------------------------------------------------------------------

_WEBGL_VENDOR = (
    "(() => {const g=document.createElement('canvas').getContext('webgl');"
    "const e=g.getExtension('WEBGL_debug_renderer_info');"
    "return g.getParameter(e.UNMASKED_VENDOR_WEBGL)})()"
)
_WEBGL_RENDERER = (
    "(() => {const g=document.createElement('canvas').getContext('webgl');"
    "const e=g.getExtension('WEBGL_debug_renderer_info');"
    "return g.getParameter(e.UNMASKED_RENDERER_WEBGL)})()"
)


def test_webgl_renderer_uses_chrome_angle_dialect(fingerprint: onyxweb.Client) -> None:
    """Chrome on Linux reports ANGLE-wrapped strings, not WebKit's bare names."""
    vendor, renderer = _probe(fingerprint, [_WEBGL_VENDOR, _WEBGL_RENDERER])
    assert isinstance(vendor, str) and isinstance(renderer, str)
    assert vendor.startswith("Google Inc. ("), f"not the Chrome dialect: {vendor!r}"
    assert renderer.startswith("ANGLE ("), f"not the Chrome dialect: {renderer!r}"


def test_webgl_spoof_does_not_leak_swiftshader(fingerprint: onyxweb.Client) -> None:
    """A claimed hardware GPU must not sit next to SwiftShader's own strings."""
    (joined,) = _probe(
        fingerprint,
        [
            "(() => {const g=document.createElement('canvas').getContext('webgl');"
            "const e=g.getExtension('WEBGL_debug_renderer_info');"
            "return [g.getParameter(e.UNMASKED_RENDERER_WEBGL), g.getParameter(g.VERSION),"
            "g.getParameter(g.SHADING_LANGUAGE_VERSION)].join(' | ')})()"
        ],
    )
    assert "swiftshader" not in str(joined).lower(), f"SwiftShader leaked: {joined!r}"


def test_full_engine_has_webgl_context() -> None:
    """`--disable-gpu` without a software fallback leaves `getContext('webgl')`
    null — a strong headless tell. Skipped when full Chrome isn't installed."""
    from onyxweb.presets.full import stealth as full_stealth

    try:
        client = onyxweb.Client(navigation_timeout_ms=15_000, **full_stealth.BASIC)
    except onyxweb.OnyxwebError as e:
        if "not found" in str(e).lower():
            pytest.skip(f"full Chrome unavailable: {e}")
        raise
    try:
        (has_webgl,) = _probe(client, ["!!document.createElement('canvas').getContext('webgl')"])
    finally:
        client.close()
    assert has_webgl is True, "full engine exposes no WebGL context"


# Host OS -> the wire UA's platform token.
_HOST_PLATFORM_TOKEN = {"Linux": "X11; Linux", "Darwin": "Macintosh", "Windows": "Windows NT"}


def test_full_engine_ua_names_the_real_host_platform(httpserver: HTTPServer) -> None:
    """The full engine's own derived UA must name this OS, not always claim Linux.

    New test: the closest existing ones don't fit. ``test_basic_identity_agrees_on_wire_js_
    and_client_hints`` checks the shell preset's spoofed brands against known preset data, not
    the full engine's real, unspoofed identity. ``test_full_engine_has_webgl_context`` fetches a
    ``data:`` URL, which never touches the network, so there is no wire ``User-Agent`` to read.
    """
    import platform as host_platform

    httpserver.expect_request("/").respond_with_data(
        "<html><body>x</body></html>", content_type="text/html"
    )
    try:
        client = onyxweb.Client(engine="full", concurrency=1, navigation_timeout_ms=15_000)
    except onyxweb.OnyxwebError as e:
        if "not found" in str(e).lower():
            pytest.skip(f"full Chrome unavailable: {e}")
        raise
    try:
        client.fetch(httpserver.url_for("/"))
    finally:
        client.close()
    wire_ua = httpserver.log[0][0].headers.get("User-Agent") or ""
    token = _HOST_PLATFORM_TOKEN[host_platform.system()]
    assert token in wire_ua, f"{wire_ua!r} does not name {host_platform.system()}"


# ----------------------------------------------------------------------------
# Canvas — stable across navigations, and toDataURL agrees with getImageData
# ----------------------------------------------------------------------------

_CANVAS_HASH = (
    "(() => {const c=document.createElement('canvas');c.width=200;c.height=50;"
    "const x=c.getContext('2d');x.textBaseline='top';x.font='14px Arial';"
    "x.fillStyle='#f60';x.fillRect(0,0,100,20);x.fillStyle='#069';"
    "x.fillText('onyxweb-fp',2,15);return c.toDataURL()})()"
)


def test_canvas_fingerprint_is_stable_across_sessions(fingerprint: onyxweb.Client) -> None:
    """One identity's canvas hash must not change navigation to navigation —
    a per-session random seed would itself be the anomaly."""
    (first,) = _probe(fingerprint, [_CANVAS_HASH])
    (second,) = _probe(fingerprint, [_CANVAS_HASH])
    assert first == second, "canvas hash changed between fetches"


def test_canvas_readback_agrees_with_dataurl(fingerprint: onyxweb.Client) -> None:
    """If `toDataURL` is perturbed, `getImageData` must be perturbed identically —
    vendors compare the two read paths, and disagreement is noise no GPU produces."""
    (agree,) = _probe(
        fingerprint,
        [
            "(() => {const c=document.createElement('canvas');c.width=20;c.height=20;"
            "const x=c.getContext('2d');x.fillStyle='#123456';x.fillRect(0,0,20,20);"
            "const px=x.getImageData(0,0,1,1).data;"
            "const c2=document.createElement('canvas');c2.width=20;c2.height=20;"
            "const x2=c2.getContext('2d');"
            "x2.fillStyle=`rgb(${px[0]},${px[1]},${px[2]})`;x2.fillRect(0,0,20,20);"
            "return c.toDataURL()===c2.toDataURL()})()"
        ],
    )
    assert agree is True, "toDataURL and getImageData disagree about the same pixels"


# ----------------------------------------------------------------------------
# Client hints GREASE — the placeholder brand must match this Chrome's own
# ----------------------------------------------------------------------------


def test_ua_ch_grease_brand_matches_the_real_browser(
    plain: onyxweb.Client, httpserver: HTTPServer
) -> None:
    """Chrome periodically rotates the GREASE placeholder; a current UA paired
    with a retired GREASE brand is a contradiction real Chrome never emits.

    ``navigator.userAgentData`` is undefined on a ``data:`` page (an opaque
    origin), so this needs a served one."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body>x</body></html>", content_type="text/html"
    )
    r = plain.fetch(
        httpserver.url_for("/"),
        post_load_scripts=["JSON.stringify(navigator.userAgentData.brands)"],
    )
    real = r.post_load_results[0]
    real_brands = {(b["brand"], b["version"]) for b in json.loads(str(real))}
    real_grease = {b for b in real_brands if "Brand" in b[0]}
    preset_brands = {(b["brand"], b["version"]) for b in shell_stealth.BASIC_UA_METADATA["brands"]}
    preset_grease = {b for b in preset_brands if "Brand" in b[0]}
    assert preset_grease == real_grease, f"preset {preset_grease} != browser {real_grease}"


# ----------------------------------------------------------------------------
# Screen geometry — a real desktop has chrome around the page
# ----------------------------------------------------------------------------


def test_screen_is_larger_than_the_viewport(plain: onyxweb.Client) -> None:
    """screen > outer >= inner; headless leaves screen.height == innerHeight
    and availTop == 0, which is a one-line check."""
    sw, sh, iw, ih, avail_top = _probe(
        plain,
        [
            "screen.width",
            "screen.height",
            "window.innerWidth",
            "window.innerHeight",
            "screen.availTop",
        ],
    )
    assert isinstance(sh, int) and isinstance(ih, int)
    assert sh > ih, f"screen.height ({sh}) must exceed innerHeight ({ih})"
    assert isinstance(sw, int) and isinstance(iw, int) and sw >= iw
    assert avail_top != 0 or sh - ih > 50, "no window chrome accounted for"
