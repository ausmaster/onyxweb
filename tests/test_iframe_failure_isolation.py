"""A failing iframe must not fail the whole fetch.

``Network.loadingFailed`` reports ``ResourceType::Document`` for subframe
documents too, so a dead iframe used to abort the parent page. Real sites embed
frames that fail routinely (blocked trackers, local-network checks, dead hosts);
the main document still rendered and the caller wants it.
"""

from __future__ import annotations

import onyxweb
from pytest_httpserver import HTTPServer

_MAIN = "MAIN_DOCUMENT_CONTENT"


def _page(iframe_src: str) -> str:
    return (
        f"<html><body><h1>{_MAIN}</h1>"
        f"<iframe src='{iframe_src}'></iframe></body></html>"
    )


def test_dead_iframe_does_not_fail_the_page(httpserver: HTTPServer) -> None:
    """An iframe pointing at a closed port must not abort the parent fetch."""
    httpserver.expect_request("/").respond_with_data(
        _page("http://127.0.0.1:1/gone"), content_type="text/html"
    )
    with onyxweb.Client(concurrency=1) as c:
        r = c.fetch(httpserver.url_for("/"), wait_after_ms=500)
    assert _MAIN in r
    assert r.status_code == 200


def test_unresolvable_iframe_does_not_fail_the_page(httpserver: HTTPServer) -> None:
    """Same for an iframe whose host does not resolve."""
    httpserver.expect_request("/").respond_with_data(
        _page("https://this-host-does-not-exist-onyxweb.invalid/"), content_type="text/html"
    )
    with onyxweb.Client(concurrency=1) as c:
        r = c.fetch(httpserver.url_for("/"), wait_after_ms=500)
    assert _MAIN in r


def test_main_document_failure_still_raises() -> None:
    """The guard stays: a real main-document failure must still raise."""
    import pytest

    with onyxweb.Client(concurrency=1) as c, pytest.raises((onyxweb.OnyxwebError, TimeoutError)):
        c.fetch("https://this-host-does-not-exist-onyxweb.invalid/", timeout_ms=8000)
