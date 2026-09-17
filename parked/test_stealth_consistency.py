"""Stealth surfaces must agree with each other and with a real browser.

A half-applied spoof is more detectable than none: a patched value that
contradicts an unpatched neighbour is a signal no real browser emits. Each test
states the real-Chrome behaviour it holds the preset to.
"""

from __future__ import annotations

import json

import onyxweb
from onyxweb.presets.shell import stealth as shell_stealth

BLANK = "data:text/html,<html><body>x</body></html>"


def _probe(scripts: list[str], **kw: object) -> list[object]:
    """Evaluate JS on a blank page under the given Client config."""
    with onyxweb.Client(concurrency=1, **kw) as c:  # type: ignore[arg-type]
        return c.fetch(BLANK, post_load_scripts=scripts).post_load_results


# --- navigator.webdriver ----------------------------------------------------


def test_webdriver_reports_false_not_undefined() -> None:
    """Real Chrome exposes `webdriver` with the value `false`.

    `undefined` means the property was deleted or shadowed with a getter that
    returns nothing — a state no shipping browser is in. onyxweb's own
    full-engine preset documents that this exact difference flips tesla.com
    back to a 403.
    """
    value, present = _probe(
        ["navigator.webdriver", "'webdriver' in navigator"], **shell_stealth.BASIC
    )
    assert present is True, "the property must exist"
    assert value is False, f"expected false, got {value!r}"


def test_webdriver_descriptor_matches_real_chrome() -> None:
    """The real descriptor is an enumerable, configurable accessor on the prototype."""
    (desc,) = _probe(
        [
            "JSON.stringify((d => d && {e: d.enumerable, c: d.configurable, g: typeof d.get})"
            "(Object.getOwnPropertyDescriptor(Navigator.prototype, 'webdriver')))"
        ],
        **shell_stealth.BASIC,
    )
    assert desc is not None, "webdriver must stay an own property of Navigator.prototype"
    d = json.loads(desc)
    assert d == {"e": True, "c": True, "g": "function"}, d


# --- WebGL ------------------------------------------------------------------


def test_webgl_renderer_uses_chrome_angle_dialect() -> None:
    """Chrome on Linux reports ANGLE-wrapped strings, not WebKit's bare names.

    `Intel Inc.` is the Safari/WebKit form; under a Chrome-on-Linux UA it is a
    straight contradiction.
    """
    vendor, renderer = _probe(
        [
            "(() => {const g=document.createElement('canvas').getContext('webgl');"
            "const e=g.getExtension('WEBGL_debug_renderer_info');"
            "return g.getParameter(e.UNMASKED_VENDOR_WEBGL)})()",
            "(() => {const g=document.createElement('canvas').getContext('webgl');"
            "const e=g.getExtension('WEBGL_debug_renderer_info');"
            "return g.getParameter(e.UNMASKED_RENDERER_WEBGL)})()",
        ],
        **shell_stealth.FINGERPRINT,
    )
    assert isinstance(vendor, str) and isinstance(renderer, str)
    assert vendor.startswith("Google Inc. ("), f"not the Chrome dialect: {vendor!r}"
    assert renderer.startswith("ANGLE ("), f"not the Chrome dialect: {renderer!r}"


def test_webgl_spoof_does_not_leak_swiftshader() -> None:
    """A claimed hardware GPU must not sit next to SwiftShader's own strings."""
    results = _probe(
        [
            "(() => {const g=document.createElement('canvas').getContext('webgl');"
            "const e=g.getExtension('WEBGL_debug_renderer_info');"
            "return [g.getParameter(e.UNMASKED_RENDERER_WEBGL), g.getParameter(g.VERSION),"
            "g.getParameter(g.SHADING_LANGUAGE_VERSION)].join(' | ')})()"
        ],
        **shell_stealth.FINGERPRINT,
    )
    joined = str(results[0]).lower()
    assert "swiftshader" not in joined, f"SwiftShader leaked: {results[0]!r}"


# --- canvas -----------------------------------------------------------------


def _canvas_hash_script() -> str:
    return (
        "(() => {const c=document.createElement('canvas');c.width=200;c.height=50;"
        "const x=c.getContext('2d');x.textBaseline='top';x.font='14px Arial';"
        "x.fillStyle='#f60';x.fillRect(0,0,100,20);x.fillStyle='#069';"
        "x.fillText('onyxweb-fp',2,15);return c.toDataURL()})()"
    )


def test_canvas_fingerprint_is_stable_across_sessions() -> None:
    """Real hardware yields the same canvas hash every session.

    A per-session random seed makes one identity produce a new hash each run,
    which is itself the anomaly.
    """
    first = _probe([_canvas_hash_script()], **shell_stealth.FINGERPRINT)[0]
    second = _probe([_canvas_hash_script()], **shell_stealth.FINGERPRINT)[0]
    assert first == second, "canvas hash changed between sessions"


def test_canvas_readback_agrees_with_dataurl() -> None:
    """If `toDataURL` is perturbed, `getImageData` must be perturbed identically.

    Vendors render the same canvas twice and compare the two read paths; a page
    where they disagree is reporting noise no GPU produces.
    """
    (agree,) = _probe(
        [
            "(() => {const c=document.createElement('canvas');c.width=20;c.height=20;"
            "const x=c.getContext('2d');x.fillStyle='#123456';x.fillRect(0,0,20,20);"
            "const px=x.getImageData(0,0,1,1).data;"
            "const c2=document.createElement('canvas');c2.width=20;c2.height=20;"
            "const x2=c2.getContext('2d');"
            "x2.fillStyle=`rgb(${px[0]},${px[1]},${px[2]})`;x2.fillRect(0,0,20,20);"
            "return c.toDataURL()===c2.toDataURL()})()"
        ],
        **shell_stealth.FINGERPRINT,
    )
    assert agree is True, "toDataURL and getImageData disagree about the same pixels"


# --- client hints -----------------------------------------------------------


def test_ua_ch_grease_brand_matches_the_real_browser() -> None:
    """The GREASE brand entry must match what this Chrome build actually emits.

    Chrome changed the placeholder form (`Not_A Brand`;v=24 is several majors
    stale); a current UA paired with a retired GREASE brand is a contradiction.
    """
    (real,) = _probe(["JSON.stringify(navigator.userAgentData.brands)"])
    real_brands = {(b["brand"], b["version"]) for b in json.loads(str(real))}
    greased = {b for b in real_brands if "Brand" in b[0]}
    preset = {
        (b["brand"], b["version"]) for b in shell_stealth.BASIC["user_agent_metadata"]["brands"]
    }
    preset_grease = {b for b in preset if "Brand" in b[0]}
    assert preset_grease == greased, (
        f"preset GREASE {preset_grease} != browser GREASE {greased}"
    )


# --- screen geometry --------------------------------------------------------


def test_screen_is_larger_than_the_viewport() -> None:
    """A real desktop has chrome around the page: screen > outer >= inner.

    Headless leaves `screen.height == innerHeight` and `availTop == 0`, which is
    a one-line check.
    """
    sw, sh, iw, ih, avail_top = _probe(
        [
            "screen.width", "screen.height",
            "window.innerWidth", "window.innerHeight", "screen.availTop",
        ],
        viewport=(1200, 800),
    )
    assert isinstance(sh, int) and isinstance(ih, int)
    assert sh > ih, f"screen.height ({sh}) must exceed innerHeight ({ih})"
    assert isinstance(sw, int) and isinstance(iw, int) and sw >= iw
    assert avail_top != 0 or sh - ih > 50, "no window chrome accounted for"
