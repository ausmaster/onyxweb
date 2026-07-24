"""Emulation knobs applied on the wire (browser actually reflects the config).

Distinct from ``test_config.py`` (pydantic construction/validation) and
``test_runtime_config.py`` (the live config-proxy plumbing): these spin a real
Client and probe the page to prove the CDP override reached chromium. Home for
``prefers_color_scheme`` today; locale/timezone/geolocation application can join
here as they gain coverage.
"""

from __future__ import annotations

from collections.abc import Callable

import onyxweb

DataUrl = Callable[[bytes], str]

_DARK_QUERY = "matchMedia('(prefers-color-scheme: dark)').matches"


def test_dark_scheme_matches_dark(data_url: DataUrl) -> None:
    url = data_url(b"<html><body>x</body></html>")
    with onyxweb.Client(prefers_color_scheme="dark") as c:
        r = c.fetch(url, post_load_scripts=[_DARK_QUERY])
    assert r.post_load_results[0] is True


def test_light_scheme_does_not_match_dark(data_url: DataUrl) -> None:
    url = data_url(b"<html><body>x</body></html>")
    with onyxweb.Client(prefers_color_scheme="light") as c:
        r = c.fetch(url, post_load_scripts=[_DARK_QUERY])
    assert r.post_load_results[0] is False


def test_default_client_is_not_dark(data_url: DataUrl) -> None:
    """No override -> chromium default (light); the dark query is False."""
    url = data_url(b"<html><body>x</body></html>")
    with onyxweb.Client() as c:
        r = c.fetch(url, post_load_scripts=[_DARK_QUERY])
    assert r.post_load_results[0] is False


def test_dark_scheme_reflected_both_ways(data_url: DataUrl) -> None:
    """With dark emulated, the light query is False and the dark query is True."""
    url = data_url(b"<html><body>x</body></html>")
    with onyxweb.Client(prefers_color_scheme="dark") as c:
        r = c.fetch(
            url,
            post_load_scripts=[
                _DARK_QUERY,
                "matchMedia('(prefers-color-scheme: light)').matches",
            ],
        )
    assert r.post_load_results == [True, False]
