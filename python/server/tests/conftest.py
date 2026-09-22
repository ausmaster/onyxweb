"""Shared fixtures for onyxweb-server's contracts.

The page fixture below is a copy of the library's own (``python/onyxweb/tests/conftest.py``),
kept here on purpose: it is data for the library's own contracts, not public API. The core and
HTTP tables run on ``onyxweb.testing.FakeClient`` without Chrome; the MCP tools and one HTTP case
use the real browser, which needs a Chrome from ``onyxweb --install``.
"""

from __future__ import annotations

import socket

import pytest
from pytest_httpserver import HTTPServer

PUBLIC = "http://93.184.216.34/"  # a public literal, so the real URL guard needs no DNS


# One page carrying every bucket category, shared by the bucket / view / search /
# overview suites. Every URL is same-origin and relative so resolution is
# exercised without a foreign host the browser would actually dial. It also holds
# the edge cases real pages produce: a charset and a nameless <meta>, an empty
# <img src>, blank frames, a repeated comment, a match deep in a long script, and
# non-ASCII text long enough that a byte-offset clip lands inside a character.
BUCKET_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<title>Bucket Fixture</title>
<meta charset="utf-8">
<meta name="generator" content="META_GENERATOR">
<meta property="og:title" content="META_OG">
<meta id="canon" content="">
<style>.card{color:#111}/*INLINE_CSS_ONE*/</style>
<style>.row{margin:0}/*INLINE_CSS_TWO*/</style>
<link rel="stylesheet" href="site.css" media="screen">
<link rel="stylesheet" href="/deep/print.css">
<script>window.cfg={token:'INLINE_JS_ONE'};</script>
<script>window.tracker='INLINE_JS_TWO';/* café PADDING_PADDING_PADDING_PADDING_PADDING
PADDING_PADDING_PADDING_PADDING_PADDING_PADDING_PADDING_PADDING_PADDING_PADDING
PADDING_PADDING */
var deepConfig={"apiKey":"DEEP_KEY_42"};</script>
<script src="static/app.js" integrity="sha384-ABC123" nonce="N1"></script>
<script src="/deep/vendor.js" type="module"></script>
<script type="application/ld+json">{"@type":"Organization","name":"LDJSON_NAME"}</script>
</head>
<body data-page="bucket">
<!--COMMENT_ONE-->
<h1>VISIBLE_HEADING</h1>
<p>Body sentence.</p>
<p id="accents">aéééééééééééééééééééééééééééééééééééééééééééééééééééééééééééééééééééééé</p>
<form action="/submit" method="post">
<input type="hidden" name="csrf" value="CSRF_TOKEN">
<input type="text" name="q" value="">
</form>
<a href="about.html">ABOUT_LINK</a>
<a href="/deep/faq.html">FAQ_LINK</a>
<img src="pic.png" alt="IMG_ALT">
<img src="" alt="EMPTY_SRC">
<iframe src="inner.html"></iframe>
<iframe srcdoc="&lt;p&gt;SRCDOC_BODY&lt;/p&gt;"></iframe>
<iframe src="about:blank" id="ad_slot"></iframe>
<iframe></iframe>
<!--COMMENT_TWO-->
<!--COMMENT_TWO-->
</body>
</html>"""

# Subresources the page pulls in. Served so the fixture produces no 404 noise;
# contents are irrelevant — buckets read the markup, not the bodies.
_SUBRESOURCES: dict[str, tuple[str, str]] = {
    "/site.css": (".a{}", "text/css"),
    "/deep/print.css": (".b{}", "text/css"),
    "/static/app.js": ("//noop", "application/javascript"),
    "/deep/vendor.js": ("//noop", "application/javascript"),
    "/pic.png": ("", "image/png"),
    "/inner.html": ("<html><body><p>FRAME_INNER</p></body></html>", "text/html"),
}


@pytest.fixture
def refused_url() -> str:
    """A URL on a closed local port: its navigation reaches Chrome and fails there."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}/"


@pytest.fixture
def bucket_page(httpserver: HTTPServer) -> str:
    """Serve the shared every-category fixture page; return its URL."""
    httpserver.expect_request("/page.html").respond_with_data(BUCKET_PAGE, content_type="text/html")
    for path, (body, ctype) in _SUBRESOURCES.items():
        httpserver.expect_request(path).respond_with_data(body, content_type=ctype)
    return httpserver.url_for("/page.html")
