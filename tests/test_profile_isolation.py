"""Each Client gets its own chrome profile dir when ``user_data_dir`` is unset.

Sharing one fixed dir (chromiumoxide's default) trips real Chrome's
ProcessSingleton: concurrent ``full`` Clients can't launch and a stale
``SingletonLock`` bricks later ones. The shell engine has no ProcessSingleton,
which is why it hid this.
"""

from __future__ import annotations

import onyxweb
import pytest

DATA_URL = "data:text/html,<html><body>x</body></html>"


def _full_client(**kw: object) -> onyxweb.Client:
    """Full-engine Client; skips the test when full Chrome is absent."""
    try:
        return onyxweb.Client(engine="full", navigation_timeout_ms=20_000, **kw)  # type: ignore[arg-type]
    except onyxweb.OnyxwebError as e:
        if "not found" in str(e).lower():
            pytest.skip(f"full Chrome unavailable: {e}")
        raise


def test_concurrent_full_engine_clients_coexist() -> None:
    """Regression: shared profile dir made the second launch die on SingletonLock."""
    a = _full_client()
    try:
        b = _full_client()
        try:
            assert a.fetch(DATA_URL).status_code == 200
            assert b.fetch(DATA_URL).status_code == 200
        finally:
            b.close()
    finally:
        a.close()


def test_sequential_full_engine_clients_reuse_is_clean() -> None:
    """A closed ``full`` Client must not leave state that blocks the next one."""
    for _ in range(2):
        c = _full_client()
        try:
            assert c.fetch(DATA_URL).status_code == 200
        finally:
            c.close()


def test_explicit_user_data_dir_is_honored(tmp_path: object) -> None:
    """An explicit ``user_data_dir`` still wins over the per-launch temp dir."""
    profile = tmp_path / "profile"  # type: ignore[operator]
    with onyxweb.Client(user_data_dir=str(profile)) as c:
        assert c.fetch(DATA_URL).status_code == 200
    # Chrome populates the profile it was pointed at.
    assert profile.exists(), "explicit user_data_dir was not used"


def test_concurrent_shell_clients_coexist() -> None:
    """The shell engine keeps working alongside a second Client (no regression)."""
    with onyxweb.Client() as a, onyxweb.Client() as b:
        assert a.fetch(DATA_URL).status_code == 200
        assert b.fetch(DATA_URL).status_code == 200
