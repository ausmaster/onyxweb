"""``capture.shadow_dom`` against a real web-component site.

The mocked tests pin the mechanism; this proves the gap and the fix are real on
a production site. lit.dev renders ~76 shadow hosts, and its cookie banner text
is genuinely absent from a default capture.

    uv run pytest -m real_sites -s tests/test_real_shadow_dom.py
"""

from __future__ import annotations

import onyxweb
import pytest

pytestmark = [pytest.mark.benchmark, pytest.mark.real_sites]

URL = "https://lit.dev/"
SETTLE_MS = 8_000


def _fetch(**kw: object) -> onyxweb.RenderResult:
    try:
        with onyxweb.Client(engine="full", navigation_timeout_ms=45_000, **kw) as c:  # type: ignore[arg-type]
            return c.fetch(URL, wait_after_ms=SETTLE_MS)
    except (onyxweb.OnyxwebError, TimeoutError) as e:
        pytest.skip(f"{URL} unreachable: {e}")


_COUNT_HOSTS = """
(() => { let n = 0;
  const walk = (r) => r.querySelectorAll('*').forEach(el => {
    if (el.shadowRoot) { n++; walk(el.shadowRoot); } });
  walk(document); return n; })()
"""


def test_real_site_uses_shadow_dom() -> None:
    """Sanity: the target really does render shadow roots, else the rest proves nothing."""
    with onyxweb.Client(engine="full", navigation_timeout_ms=45_000) as c:
        r = c.fetch(URL, wait_after_ms=SETTLE_MS, post_load_scripts=[_COUNT_HOSTS])
    hosts = r.post_load_results[0]
    assert isinstance(hosts, int) and hosts >= 5, f"expected shadow hosts, got {hosts}"


def test_shadow_content_recovered_on_real_site() -> None:
    """Custom-element internals are absent by default and present when enabled."""
    plain = _fetch()
    deep = _fetch(include_shadow_dom=True)

    # lit.dev's own components; their markup only exists inside shadow roots.
    assert "<litdev-cookie-banner" in plain.dom.html(), "sanity: host element is light-DOM"
    assert len(deep.dom.html()) > len(plain.dom.html()), (
        "shadow-inclusive capture should be strictly larger"
    )
    # The banner's rendered text lives inside the component's shadow root.
    assert "Cookies consent notice" not in plain.dom.html()
    assert "Cookies consent notice" in deep.dom.html()
