"""Search — find a specific thing inside a bucket, or across all of them.

``bucket.search(q)`` keeps the records that contain ``q`` anywhere in their
strings: bodies, URLs, attribute names and values, nested form fields, JSON-LD
keys and values. Matching runs in Rust and the result stays a lazy bucket, so
sizing or printing a search moves only matches across FFI — never the bodies
that failed to match.
"""

from __future__ import annotations

import onyxweb
import pytest
from pytest_httpserver import HTTPServer

# --- what a search reaches ----------------------------------------------------


def test_finds_inline_source(bucket_page: str) -> None:
    found = onyxweb.fetch(bucket_page).scripts.search("INLINE_JS_ONE")
    assert len(found) == 1
    assert "INLINE_JS_ONE" in (found[0].text or "")


def test_finds_urls(bucket_page: str) -> None:
    found = onyxweb.fetch(bucket_page).scripts.search("app.js")
    assert [s.raw for s in found] == ["static/app.js"]


def test_finds_attribute_values_and_names(bucket_page: str) -> None:
    scripts = onyxweb.fetch(bucket_page).scripts
    assert len(scripts.search("sha384-ABC123")) == 1
    assert len(scripts.search("nonce")) == 1


def test_reaches_nested_form_fields(bucket_page: str) -> None:
    assert len(onyxweb.fetch(bucket_page).forms.search("CSRF_TOKEN")) == 1


def test_reaches_json_ld(bucket_page: str) -> None:
    assert len(onyxweb.fetch(bucket_page).json_ld.search("LDJSON_NAME")) == 1


def test_matches_the_record_text_not_raw_markup(httpserver: HTTPServer) -> None:
    """Search reads the record you'd get back — a link's text across child tags."""
    httpserver.expect_request("/l.html").respond_with_data(
        "<html><body><a href='x.html'>foo<b>bar</b></a></body></html>",
        content_type="text/html",
    )
    assert len(onyxweb.fetch(httpserver.url_for("/l.html")).links.search("foobar")) == 1


def test_skips_classification_fields_by_default(bucket_page: str) -> None:
    """``where`` is onyxweb's label, not page bytes — it matches only when asked for."""
    scripts = onyxweb.fetch(bucket_page).scripts
    assert len(scripts.search("external")) == 0
    assert len(scripts.search("external", field="where")) == 2


# --- matching options --------------------------------------------------------


def test_case_insensitive_by_default(bucket_page: str) -> None:
    scripts = onyxweb.fetch(bucket_page).scripts
    assert len(scripts.search("inline_js_one")) == 1
    assert len(scripts.search("inline_js_one", case_sensitive=True)) == 0


def test_field_narrows_the_search(bucket_page: str) -> None:
    scripts = onyxweb.fetch(bucket_page).scripts
    assert len(scripts.search("window")) == 2
    assert len(scripts.search("window", field="url")) == 0
    assert len(scripts.search("app", field="url")) == 1


def test_unknown_field_names_the_real_ones(bucket_page: str) -> None:
    with pytest.raises(ValueError, match="text"):
        onyxweb.fetch(bucket_page).scripts.search("x", field="nope")


def test_regex(bucket_page: str) -> None:
    found = onyxweb.fetch(bucket_page).scripts.search(r"INLINE_JS_(ONE|TWO)", regex=True)
    assert len(found) == 2


def test_unsupported_regex_is_a_value_error(bucket_page: str) -> None:
    """Rust's ``regex`` has no lookaround — linear time is the trade."""
    with pytest.raises(ValueError, match="pattern"):
        len(onyxweb.fetch(bucket_page).scripts.search(r"(?<=cfg)=", regex=True))


def test_substring_search_treats_metacharacters_literally(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/m.html").respond_with_data(
        "<html><head><script src='app.js'></script><script src='appXjs'></script>"
        "</head><body></body></html>",
        content_type="text/html",
    )
    found = onyxweb.fetch(httpserver.url_for("/m.html")).scripts.search("app.js")
    assert [s.raw for s in found] == ["app.js"]


# --- composition --------------------------------------------------------------


def test_chained_searches_narrow(bucket_page: str) -> None:
    found = onyxweb.fetch(bucket_page).scripts.search("window").search("tracker")
    assert len(found) == 1
    assert "INLINE_JS_TWO" in (found[0].text or "")


def test_search_respects_a_view(bucket_page: str) -> None:
    r = onyxweb.fetch(bucket_page)
    assert len(r.content.scripts.search("app.js")) == 0
    assert len(r.resources.scripts.search("app.js")) == 1


def test_search_stays_lazy(bucket_page: str) -> None:
    found = onyxweb.fetch(bucket_page).scripts.search("window")
    assert len(found) == 2
    repr(found)
    assert found._records is None


# --- display ------------------------------------------------------------------


def test_searched_header_shows_matches_of_total(bucket_page: str) -> None:
    header = repr(onyxweb.fetch(bucket_page).scripts.search("window")).splitlines()[0]
    assert header == "Scripts · 2 of 4 matching 'window'"


def test_searched_view_header_names_its_side(bucket_page: str) -> None:
    header = repr(onyxweb.fetch(bucket_page).content.scripts.search("window")).splitlines()[0]
    assert header == "Scripts · 2 of 2 inline matching 'window'"


def test_empty_search_still_says_what_it_searched(bucket_page: str) -> None:
    table = repr(onyxweb.fetch(bucket_page).scripts.search("zzz_absent"))
    assert table == "Scripts · 0 of 4 matching 'zzz_absent'"


def test_remainder_line_points_at_search(httpserver: HTTPServer) -> None:
    anchors = "".join(f"<a href='p{i}.html'>p{i}</a>" for i in range(15))
    httpserver.expect_request("/many.html").respond_with_data(
        f"<html><body>{anchors}</body></html>", content_type="text/html"
    )
    last = repr(onyxweb.fetch(httpserver.url_for("/many.html")).links).splitlines()[-1]
    assert ".search(q)" in last


# --- across every bucket -------------------------------------------------------


def test_result_search_returns_only_buckets_with_matches(bucket_page: str) -> None:
    r = onyxweb.fetch(bucket_page)
    assert list(r.search("CSRF_TOKEN")) == ["forms"]
    assert set(r.search("_ONE")) == {"scripts", "styles", "comments"}


def test_result_search_with_field_skips_buckets_without_it(bucket_page: str) -> None:
    hits = onyxweb.fetch(bucket_page).search("deep", field="url")
    assert set(hits) == {"scripts", "styles", "links"}


def test_result_search_with_no_match_is_empty(bucket_page: str) -> None:
    assert onyxweb.fetch(bucket_page).search("zzz_absent") == {}


def test_fields_lists_what_can_be_searched(bucket_page: str) -> None:
    assert onyxweb.fetch(bucket_page).scripts.fields == (
        "where",
        "text",
        "url",
        "raw",
        "type",
        "attrs",
    )
