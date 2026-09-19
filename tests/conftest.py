"""Shared test fixtures.

These tests require a usable Chromium binary. onyxweb auto-resolves from:
  1. explicit chrome_path= on Client (not used here)
  2. bundled python/onyxweb/_binaries/<platform>/chrome-headless-shell
  3. system chromium (apt install chromium-browser etc.)

If neither bundled nor system chromium is available, tests that spin a Client
will fail at Client() construction with a clear "chrome binary not found"
error — intended. Install chromium to run the suite.
"""

from __future__ import annotations

import base64
import socket
from collections.abc import Callable

import pytest
from pytest_httpserver import HTTPServer

DataUrl = Callable[[bytes], str]

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"


def is_webp(data: bytes) -> bool:
    """True if `data` opens with a RIFF/WEBP header."""
    return data[:4] == b"RIFF" and data[8:12] == b"WEBP"


@pytest.fixture
def data_url() -> DataUrl:
    """Wrap HTML bytes in a base64-encoded ``data:`` URL.

    Tests using ``data:`` URLs avoid the cost of spinning up an HTTP
    server when the test only needs a tiny HTML document loaded once.
    """

    def _make(html: bytes) -> str:
        return "data:text/html;base64," + base64.b64encode(html).decode()

    return _make


@pytest.fixture
def refused_url() -> str:
    """A URL on a closed local port: its navigation reaches Chrome and fails there.

    Port 1 won't do — Chrome refuses it as unsafe before connecting.
    """
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}/"


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
def bucket_page(httpserver: HTTPServer) -> str:
    """Serve the shared every-category fixture page; return its URL."""
    httpserver.expect_request("/page.html").respond_with_data(BUCKET_PAGE, content_type="text/html")
    for path, (body, ctype) in _SUBRESOURCES.items():
        httpserver.expect_request(path).respond_with_data(body, content_type=ctype)
    return httpserver.url_for("/page.html")
