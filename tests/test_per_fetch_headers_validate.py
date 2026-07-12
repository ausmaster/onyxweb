"""Per-fetch ``extra_headers`` validation: a small, explicit list of headers
that chromium silently drops or computes from request state. Setting them via
``Network.setExtraHTTPHeaders`` either has no effect or is rejected; we
surface the issue at config-construction time with a clear pydantic error
that names a CDP alternative.

``Referer`` is NOT on the list — it's handled automatically (lifted out of
extra_headers and routed through ``Page.navigate(referrer=...)``).
"""

from __future__ import annotations

import onyxweb
import pytest
from onyxweb.config import ClientConfig, FetchConfig, ScreenshotConfig
from pydantic import ValidationError

FORBIDDEN = [
    "Cookie",
    "Cookie2",
    "Set-Cookie",
    "Host",
    "Origin",
    "Content-Length",
    "Transfer-Encoding",
    "Connection",
]


@pytest.mark.parametrize("header", FORBIDDEN)
def test_fetch_config_rejects_forbidden_header(header: str) -> None:
    with pytest.raises(ValidationError) as exc:
        FetchConfig(extra_headers={header: "v"})
    msg = str(exc.value)
    assert header.lower() in msg.lower(), f"error doesn't name header: {msg}"


@pytest.mark.parametrize("header", FORBIDDEN)
def test_client_config_rejects_forbidden_header(header: str) -> None:
    with pytest.raises(ValidationError) as exc:
        ClientConfig.from_flat(extra_headers={header: "v"})
    msg = str(exc.value)
    assert header.lower() in msg.lower()


@pytest.mark.parametrize("header", FORBIDDEN)
def test_screenshot_config_rejects_forbidden_header(header: str) -> None:
    with pytest.raises(ValidationError) as exc:
        ScreenshotConfig(extra_headers={header: "v"})
    msg = str(exc.value)
    assert header.lower() in msg.lower()


def test_forbidden_headers_case_insensitive() -> None:
    """Header names are case-insensitive per RFC 7230; lower-case rejected too."""
    with pytest.raises(ValidationError):
        FetchConfig(extra_headers={"cookie": "x"})


def test_referer_is_not_forbidden() -> None:
    """Referer is handled by the navigation path, not rejected."""
    fc = FetchConfig(extra_headers={"Referer": "http://foo.bar/"})
    assert fc.extra_headers == {"Referer": "http://foo.bar/"}


def test_normal_headers_accepted() -> None:
    """Custom and standard non-forbidden headers pass through unchanged."""
    fc = FetchConfig(
        extra_headers={
            "X-Custom": "ok",
            "User-Agent": "MyBot/1.0",
            "Accept-Language": "en-US",
            "DNT": "1",
        }
    )
    assert fc.extra_headers["X-Custom"] == "ok"


def test_forbidden_at_runtime_via_kwargs() -> None:
    """A direct ``client.fetch(extra_headers=...)`` call also surfaces the error."""
    with onyxweb.Client() as c, pytest.raises((ValidationError, ValueError)):
        c.fetch("https://example.com/", extra_headers={"Cookie": "x=y"})
