"""JavaScript dialog auto-dismiss: onyxweb auto-dismisses ``alert``,
``confirm``, and ``prompt`` so pages that call these don't hang the
fetch waiting for a UI dismissal that will never come.

Without this, any page that calls ``alert()`` during page-script
execution blocks the lifecycle event, causing the fetch to time out.
Common real-world triggers: cookie banners, error popups, paywall
walls, tutorial overlays, anti-bot challenges.

Behavior:
- ``alert(msg)``: dialog dismissed; ``alert`` returns ``undefined`` to
  the page; lifecycle continues normally.
- ``confirm(msg)``: dismissed with ``accept=false``; ``confirm`` returns
  ``false``.
- ``prompt(msg, default)``: dismissed with ``accept=false``; ``prompt``
  returns ``null``.

Mirrors Playwright / Selenium / Puppeteer defaults — automation tools
auto-dismiss because the alternative is hanging on any page that calls
these APIs.
"""

from __future__ import annotations

import base64

import onyxweb


def test_alert_does_not_hang_the_fetch() -> None:
    """A page that calls ``alert()`` during page-script execution must
    complete the fetch within normal time — the engine auto-dismisses
    the dialog so the lifecycle event can fire.
    """
    html = (
        b"<html><body><script>alert('cookie banner')</script>"
        b"<div id='loaded'>OK</div></body></html>"
    )
    url = "data:text/html;base64," + base64.b64encode(html).decode()

    with onyxweb.Client(navigation_timeout_ms=5000) as c:
        r = c.fetch(url)

    assert "OK" in r, f"page didn't load past alert(); html: {r[:200]}"
    assert r.status_code == 200


def test_confirm_returns_false_when_auto_dismissed() -> None:
    """``confirm(msg)`` returns ``false`` when the dialog is auto-dismissed
    with ``accept=false``. The page can branch on the result; we assert
    the false branch ran.
    """
    html = (
        b"<html><body><script>"
        b"if (confirm('proceed?')) {"
        b"  document.body.dataset.branch = 'accepted';"
        b"} else {"
        b"  document.body.dataset.branch = 'dismissed';"
        b"}"
        b"</script></body></html>"
    )
    url = "data:text/html;base64," + base64.b64encode(html).decode()

    with onyxweb.Client(navigation_timeout_ms=5000) as c:
        r = c.fetch(url)

    assert 'data-branch="dismissed"' in r, (
        f"confirm() didn't return false; html: {r[:300]}"
    )


def test_prompt_returns_null_when_auto_dismissed() -> None:
    """``prompt(msg, default)`` returns ``null`` when dismissed.
    Page branches accordingly.
    """
    html = (
        b"<html><body><script>"
        b"const v = prompt('name?', 'default');"
        b"document.body.dataset.result = (v === null ? 'null' : 'value:' + v);"
        b"</script></body></html>"
    )
    url = "data:text/html;base64," + base64.b64encode(html).decode()

    with onyxweb.Client(navigation_timeout_ms=5000) as c:
        r = c.fetch(url)

    assert 'data-result="null"' in r, (
        f"prompt() didn't return null; html: {r[:300]}"
    )


def test_multiple_dialogs_in_sequence_all_dismissed() -> None:
    """A page with multiple sequential dialogs (alert/confirm/prompt) all
    get auto-dismissed in order; the page continues past each.
    """
    html = (
        b"<html><body><script>"
        b"alert('first');"
        b"const c = confirm('second?');"
        b"const p = prompt('third?');"
        b"document.body.dataset.results = "
        b"  'confirm=' + c + ';prompt=' + (p === null ? 'null' : p);"
        b"</script></body></html>"
    )
    url = "data:text/html;base64," + base64.b64encode(html).decode()

    with onyxweb.Client(navigation_timeout_ms=5000) as c:
        r = c.fetch(url)

    assert 'data-results="confirm=false;prompt=null"' in r, (
        f"sequential dialogs didn't all dismiss; html: {r[:400]}"
    )


def test_alert_during_post_load_script_does_not_hang() -> None:
    """A ``post_load_scripts`` entry that triggers ``alert()`` (e.g., via
    a synthetic click on an ``onclick`` that calls alert) must not hang
    the fetch — auto-dismiss applies post-load too.
    """
    html = (
        b"<html><body>"
        b"<button id='b' onclick=\"alert('clicked!')\">x</button>"
        b"</body></html>"
    )
    url = "data:text/html;base64," + base64.b64encode(html).decode()

    with onyxweb.Client(navigation_timeout_ms=5000) as c:
        r = c.fetch(
            url,
            post_load_scripts=["document.getElementById('b').click()"],
        )

    assert r.status_code == 200
    assert "<button" in r


async def test_async_alert_does_not_hang() -> None:
    """Same auto-dismiss applies on the AsyncClient path."""
    html = b"<html><body><script>alert('x')</script><div id='ok'>ASYNC_OK</div></body></html>"
    url = "data:text/html;base64," + base64.b64encode(html).decode()

    async with onyxweb.AsyncClient(navigation_timeout_ms=5000) as ac:
        r = await ac.fetch(url)

    assert "ASYNC_OK" in r
