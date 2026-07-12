"""Tests for onyxweb.fetch() and Client.fetch() — URL-based rendering."""

from __future__ import annotations

import onyxweb
import pytest

HTTPS_URL = "https://example.com"
HTTP_URL = "http://example.com"


class TestFetchTopLevel:
    """Module-level onyxweb.fetch() — uses the shared default Client."""

    def test_fetch_https(self) -> None:
        result = onyxweb.fetch(HTTPS_URL)
        assert isinstance(result, onyxweb.RenderResult)
        assert len(result) > 0
        assert "Example Domain" in result

    def test_fetch_http(self) -> None:
        result = onyxweb.fetch(HTTP_URL)
        assert isinstance(result, onyxweb.RenderResult)
        assert "Example Domain" in result

    def test_fetch_has_errors_attr(self) -> None:
        result = onyxweb.fetch(HTTPS_URL)
        assert isinstance(result.errors, list)

    def test_fetch_has_metadata(self) -> None:
        result = onyxweb.fetch(HTTPS_URL)
        assert result.final_url.startswith("https://example.com")
        assert result.status_code == 200  # real status from main-doc response
        assert result.elapsed_s > 0

    def test_fetch_404_returns_404_status(self) -> None:
        """We capture the main-doc response status, not 200-on-any-navigation."""
        result = onyxweb.fetch("https://httpbin.org/status/404")
        assert result.status_code == 404

    def test_fetch_redirect_status_is_final(self) -> None:
        """http → https redirect: status reflects the final resource, not the 301."""
        result = onyxweb.fetch("http://httpbin.org/redirect-to?url=https://example.com")
        assert result.final_url == "https://example.com/"
        assert result.status_code == 200

    def test_fetch_html_property(self) -> None:
        result = onyxweb.fetch(HTTPS_URL)
        assert result.html == str(result)

    def test_fetch_is_string(self) -> None:
        result = onyxweb.fetch(HTTPS_URL)
        assert isinstance(result, str)

    def test_fetch_invalid_url_raises(self) -> None:
        # chromium treats "not-a-url" as a host and hangs, so this surfaces as a
        # TimeoutError; a fast-failing invalid URL would be OnyxwebError (a
        # RuntimeError). Either way it must raise, not silently succeed.
        with pytest.raises((RuntimeError, TimeoutError)):
            onyxweb.fetch("not-a-url", timeout_ms=3000)

    def test_fetch_nonexistent_domain_raises(self) -> None:
        # A dead domain either fast-fails DNS (OnyxwebError, a RuntimeError) or
        # hangs and times out (TimeoutError), depending on the resolver.
        with pytest.raises((RuntimeError, TimeoutError)):
            onyxweb.fetch(
                "https://this-domain-does-not-exist-onyxweb-test.invalid",
                timeout_ms=5000,
            )


class TestFetchClient:
    """Client.fetch() — persistent, explicit client."""

    def test_client_fetch_basic(self) -> None:
        with onyxweb.Client() as client:
            result = client.fetch(HTTPS_URL)
        assert isinstance(result, onyxweb.RenderResult)
        assert "Example Domain" in result

    def test_client_fetch_reuse(self) -> None:
        """Same client, multiple fetches — all work."""
        with onyxweb.Client() as client:
            a = client.fetch(HTTPS_URL)
            b = client.fetch(HTTPS_URL)
        assert len(a) > 0 and len(b) > 0

    def test_client_fetch_invalid_url(self) -> None:
        with (
            onyxweb.Client() as client,
            pytest.raises((RuntimeError, TimeoutError)),
        ):
            client.fetch("not-a-url", timeout_ms=3000)


class TestRenderResult:
    """RenderResult is a str subclass with extra metadata + a lazy Rust DOM."""

    def test_is_str_subclass(self) -> None:
        result = onyxweb.fetch(HTTPS_URL)
        assert isinstance(result, str)
        # str operations work
        assert result.lower() == str(result).lower()
        assert result[:15] == str(result)[:15]

    def test_dom_lazy_parses_and_queries(self) -> None:
        result = onyxweb.fetch(HTTPS_URL)
        # Title query
        title = result.dom.title()
        assert title == "Example Domain"
        # Links query
        links = result.dom.links()
        assert isinstance(links, list)
        # Count query stops at first match
        assert result.dom.exists("h1") is True
        assert result.dom.exists("fakeneverexists") is False

    def test_repr_shape(self) -> None:
        result = onyxweb.fetch(HTTPS_URL)
        r = repr(result)
        assert r.startswith("RenderResult(")
