"""Per-fetch init scripts: ``FetchConfig.scripts`` runs JavaScript on the
page before any of its own scripts.

TDD-built across the per-fetch-extensions phase:
1. Scripts execute (this file's first tests).
2. Scripts cleanup — no leak between fetches on the same pool tab —
   see ``test_per_fetch_scripts_cleanup.py``.

Each test below uses a fresh ``Client`` to isolate from any pool-state
carry-over from other tests; the cleanup-discipline tests exercise the
shared-pool path explicitly.
"""

from __future__ import annotations

import base64

import onyxweb


def test_per_fetch_script_executes_on_page() -> None:
    """A script passed via ``FetchConfig.scripts`` runs on the page; its
    side effects are observable in ``RenderResult.console_messages``."""
    html = b"<html><body>no script of its own</body></html>"
    url = "data:text/html;base64," + base64.b64encode(html).decode()

    with onyxweb.Client() as c:
        r = c.fetch(url, scripts=["console.error('script ran')"])

    msgs = [m for m in r.console_messages if "script ran" in m.text]
    assert len(msgs) == 1, f"expected one capture of 'script ran', got {r.console_messages}"


def test_multiple_per_fetch_scripts_all_execute() -> None:
    """Every entry in the ``scripts`` list runs."""
    html = b"<html><body>blank</body></html>"
    url = "data:text/html;base64," + base64.b64encode(html).decode()

    with onyxweb.Client() as c:
        r = c.fetch(
            url,
            scripts=[
                "console.error('first script')",
                "console.error('second script')",
            ],
        )

    texts = [m.text for m in r.console_messages]
    assert any("first script" in t for t in texts)
    assert any("second script" in t for t in texts)


def test_per_fetch_script_runs_before_page_scripts() -> None:
    """Scripts registered via ``addScriptToEvaluateOnNewDocument`` run before
    the page's own inline scripts. We verify by setting a window global from
    the init script and reading it from a page-side script."""
    html = (
        b"<html><body><script>"
        b"console.error('page sees ' + (window.__onyxweb_marker || 'NOTHING'))"
        b"</script></body></html>"
    )
    url = "data:text/html;base64," + base64.b64encode(html).decode()

    with onyxweb.Client() as c:
        r = c.fetch(url, scripts=["window.__onyxweb_marker = 'INIT_RAN'"])

    msgs = [m for m in r.console_messages if "page sees" in m.text]
    assert len(msgs) == 1
    assert "page sees INIT_RAN" in msgs[0].text


# ----------------------------------------------------------------------------
# TDD #2: cleanup — scripts must NOT leak to subsequent fetches on the same
# pooled tab. ``concurrency=1`` forces tab reuse so the leak is observable.
# ----------------------------------------------------------------------------


def test_per_fetch_scripts_do_not_leak_to_next_fetch_on_same_pool_tab() -> None:
    """A script from fetch #1 must not fire during fetch #2 on the same tab."""
    html = b"<html><body>blank</body></html>"
    url = "data:text/html;base64," + base64.b64encode(html).decode()

    with onyxweb.Client(concurrency=1) as c:
        r1 = c.fetch(url, scripts=["console.error('LEAK_MARKER_42')"])
        r2 = c.fetch(url)

    assert any("LEAK_MARKER_42" in m.text for m in r1.console_messages), (
        f"sanity check failed: fetch #1 should have seen the script: {r1.console_messages}"
    )
    assert not any("LEAK_MARKER_42" in m.text for m in r2.console_messages), (
        f"script leaked from fetch #1 to fetch #2: {r2.console_messages}"
    )


def test_per_fetch_multiple_scripts_all_cleaned_up() -> None:
    """All scripts in the list get removed, not just the first one."""
    html = b"<html><body>blank</body></html>"
    url = "data:text/html;base64," + base64.b64encode(html).decode()

    with onyxweb.Client(concurrency=1) as c:
        r1 = c.fetch(
            url,
            scripts=[
                "console.error('LEAK_A_99')",
                "console.error('LEAK_B_99')",
                "console.error('LEAK_C_99')",
            ],
        )
        r2 = c.fetch(url)

    # All three should have fired in #1 (sanity).
    texts1 = " ".join(m.text for m in r1.console_messages)
    assert "LEAK_A_99" in texts1 and "LEAK_B_99" in texts1 and "LEAK_C_99" in texts1
    # None should fire in #2.
    texts2 = " ".join(m.text for m in r2.console_messages)
    assert "LEAK_A_99" not in texts2
    assert "LEAK_B_99" not in texts2
    assert "LEAK_C_99" not in texts2


def test_per_fetch_scripts_cleanup_runs_even_when_call_has_no_scripts() -> None:
    """A fetch with no per-call scripts doesn't crash because of empty cleanup."""
    html = b"<html><body>blank</body></html>"
    url = "data:text/html;base64," + base64.b64encode(html).decode()

    with onyxweb.Client(concurrency=1) as c:
        r = c.fetch(url)

    assert isinstance(r, onyxweb.RenderResult)


def test_per_fetch_scripts_cleanup_after_consecutive_calls_with_different_scripts() -> None:
    """Three consecutive fetches each with different scripts — none of them
    leak across boundaries."""
    html = b"<html><body>blank</body></html>"
    url = "data:text/html;base64," + base64.b64encode(html).decode()

    with onyxweb.Client(concurrency=1) as c:
        r1 = c.fetch(url, scripts=["console.error('GEN_1_MARKER')"])
        r2 = c.fetch(url, scripts=["console.error('GEN_2_MARKER')"])
        r3 = c.fetch(url, scripts=["console.error('GEN_3_MARKER')"])

    # Each fetch sees ONLY its own script.
    t1 = " ".join(m.text for m in r1.console_messages)
    t2 = " ".join(m.text for m in r2.console_messages)
    t3 = " ".join(m.text for m in r3.console_messages)
    assert "GEN_1_MARKER" in t1 and "GEN_2_MARKER" not in t1 and "GEN_3_MARKER" not in t1
    assert "GEN_2_MARKER" in t2 and "GEN_1_MARKER" not in t2 and "GEN_3_MARKER" not in t2
    assert "GEN_3_MARKER" in t3 and "GEN_1_MARKER" not in t3 and "GEN_2_MARKER" not in t3


# ----------------------------------------------------------------------------
# TDD #5: AsyncClient parity — same script behavior + cleanup via async.
# ----------------------------------------------------------------------------


async def test_async_per_fetch_script_executes() -> None:
    """``await ac.fetch(scripts=...)`` runs the script identically to sync."""
    html = b"<html><body>blank</body></html>"
    url = "data:text/html;base64," + base64.b64encode(html).decode()

    async with onyxweb.AsyncClient() as ac:
        r = await ac.fetch(url, scripts=["console.error('ASYNC_RAN')"])

    assert any("ASYNC_RAN" in m.text for m in r.console_messages)


async def test_async_per_fetch_scripts_cleanup_no_leak() -> None:
    """Cleanup discipline holds on the async path."""
    html = b"<html><body>blank</body></html>"
    url = "data:text/html;base64," + base64.b64encode(html).decode()

    async with onyxweb.AsyncClient(concurrency=1) as ac:
        r1 = await ac.fetch(url, scripts=["console.error('ASYNC_LEAK_77')"])
        r2 = await ac.fetch(url)

    assert any("ASYNC_LEAK_77" in m.text for m in r1.console_messages)
    assert not any("ASYNC_LEAK_77" in m.text for m in r2.console_messages)
