"""Tests for onyxweb.fetch() and Client.fetch() — URL-based rendering."""

from __future__ import annotations

import onyxweb
import pytest
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Response

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

    def test_fetch_404_returns_404_status(self, httpserver: HTTPServer) -> None:
        """We capture the main-doc response status, not 200-on-any-navigation."""
        httpserver.expect_request("/missing").respond_with_data(
            "<html><body>nope</body></html>", status=404, content_type="text/html"
        )
        result = onyxweb.fetch(httpserver.url_for("/missing"))
        assert result.status_code == 404

    def test_fetch_redirect_status_is_final(self, httpserver: HTTPServer) -> None:
        """A redirect: status/final_url reflect the destination, not the 3xx hop."""
        httpserver.expect_request("/from").respond_with_response(
            Response(status=302, headers={"Location": httpserver.url_for("/to")})
        )
        httpserver.expect_request("/to").respond_with_data(
            "<html><body>arrived</body></html>", content_type="text/html"
        )
        result = onyxweb.fetch(httpserver.url_for("/from"))
        assert result.final_url.endswith("/to")
        assert result.status_code == 200

    def test_fetch_html_property(self) -> None:
        result = onyxweb.fetch(HTTPS_URL)
        assert result.html == str(result)

    def test_fetch_html_is_a_real_string(self) -> None:
        """``.html`` is what regex, BeautifulSoup and file writes take."""
        result = onyxweb.fetch(HTTPS_URL)
        assert isinstance(result.html, str)
        assert "Example Domain" in result.html

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
    """RenderResult is a structured result, not a str; raw access survives."""

    def test_is_not_a_str(self) -> None:
        """Dropping the base frees names like ``title`` for the page itself."""
        result = onyxweb.fetch(HTTPS_URL)
        assert not isinstance(result, str)
        assert result.title == "Example Domain"

    def test_raw_access_still_works(self) -> None:
        result = onyxweb.fetch(HTTPS_URL)
        html = result.html
        assert str(result) == html
        assert "Example Domain" in result
        assert "surely-not-present-xyz" not in result
        assert len(result) == len(html)

    def test_contains_is_case_sensitive_like_str(self) -> None:
        """``in`` keeps ``str`` semantics even though Rust answers it."""
        result = onyxweb.fetch(HTTPS_URL)
        assert "Example Domain" in result
        assert "EXAMPLE DOMAIN" not in result

    def test_len_and_contains_do_not_materialize(self) -> None:
        result = onyxweb.fetch(HTTPS_URL)
        assert len(result) > 0
        assert "Example" in result
        assert result._html is None

    def test_repr_shape(self) -> None:
        result = onyxweb.fetch(HTTPS_URL)
        r = repr(result)
        assert r.startswith("<RenderResult ")
        assert len(r) < 200
