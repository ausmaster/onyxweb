"""`.dom.text()` returns what the page displays, not its scripts.

A text node inside `<script>` or `<style>` is code, not content. Concatenating
it buries the page's words: measured on saved captures, script contents are
77-99% of the characters (cnn.com: 20 KB of text inside 2.15 MB).
"""

from __future__ import annotations

from collections.abc import Callable

import onyxweb

DataUrl = Callable[[bytes], str]

_PAGE = (
    b"<html><head>"
    b"<style>.hidden{display:none}/*CSS_NOISE*/</style>"
    b"<script>var config={token:'JS_NOISE'};</script>"
    b"</head><body>"
    b"<h1>Visible Heading</h1>"
    b"<p>Body sentence.</p>"
    b"<script>window.tracker='INLINE_JS_NOISE';</script>"
    b"<style>p{color:#333}/*INLINE_CSS_NOISE*/</style>"
    b"<noscript>NOSCRIPT_NOISE</noscript>"
    b"<template><span>TEMPLATE_NOISE</span></template>"
    b"</body></html>"
)

_NOISE = ("JS_NOISE", "CSS_NOISE", "INLINE_JS_NOISE", "INLINE_CSS_NOISE",
          "NOSCRIPT_NOISE", "TEMPLATE_NOISE")


def test_text_excludes_script_and_style(data_url: DataUrl) -> None:
    r = onyxweb.fetch(data_url(_PAGE))
    text = r.dom.text()
    assert "Visible Heading" in text
    assert "Body sentence." in text
    for noise in _NOISE:
        assert noise not in text, f"{noise} leaked into .dom.text()"


def test_element_text_excludes_script(data_url: DataUrl) -> None:
    """Element-scoped text follows the same rule."""
    page = data_url(
        b"<html><body><div id='wrap'>Kept<script>var x='EL_NOISE';</script></div></body></html>"
    )
    el = onyxweb.fetch(page).dom.query_one("#wrap")
    assert el is not None
    assert "Kept" in el.text
    assert "EL_NOISE" not in el.text


def test_find_text_helper_excludes_script(data_url: DataUrl) -> None:
    """`.dom.find(...).text` goes through the same extraction."""
    found = onyxweb.fetch(data_url(_PAGE)).dom.find("body")
    assert found is not None
    assert "Visible Heading" in found.text
    assert "INLINE_JS_NOISE" not in found.text


def test_html_still_contains_the_scripts(data_url: DataUrl) -> None:
    """Only text extraction changes; the captured HTML stays complete."""
    r = onyxweb.fetch(data_url(_PAGE))
    assert "INLINE_JS_NOISE" in str(r)
    assert "INLINE_CSS_NOISE" in str(r)
