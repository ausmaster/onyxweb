"""C8 output model — a captured page's buckets, views, overview, search, ``dom`` and
``text`` agree with the HTML and each other; no display dumps a body.

Every bucket, view, search and DOM query reads one parse (``BUCKET_PAGE`` from
conftest), so they can never disagree — the invariants here check that promise
directly rather than trusting it. ``RECORDS`` names one field of one record per
bucket; ``PARTITION`` checks every bucket's inline/external split sums to the whole
and agrees with the overview; ``DOM`` exercises the CSS-selector engine on the same
page, so a selector and a bucket read of the same markup can be cross-checked.
"""

from __future__ import annotations

import struct
from collections.abc import Callable
from typing import Any

import onyxweb
import pytest
from conftest import PNG_MAGIC, DataUrl, reloaded
from onyxweb.records import HEAD_ROWS, PAGE_BUCKETS, PREVIEW_WIDTH, Overview, size_str
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Response

# ----------------------------------------------------------------------------
# RECORDS — one field of one record, per bucket, read off BUCKET_PAGE
# ----------------------------------------------------------------------------

# Bucket -> (selector into it, a lambda picking the record, field -> expected).
RECORDS: dict[str, tuple[Callable[[onyxweb.RenderResult], Any], dict[str, Any]]] = {
    "scripts_inline": (
        lambda r: next(
            s for s in r.scripts if s.where == "inline" and "INLINE_JS_ONE" in (s.text or "")
        ),
        {"url": None, "raw": None, "type": ""},
    ),
    "scripts_external": (
        lambda r: next(s for s in r.scripts if s.raw == "static/app.js"),
        {
            "where": "external",
            "text": None,
            "type": "",
            "attrs": {"src": "static/app.js", "integrity": "sha384-ABC123", "nonce": "N1"},
        },
    ),
    "styles_external": (
        lambda r: next(s for s in r.styles if s.raw == "site.css"),
        {"where": "external", "text": None, "media": "screen"},
    ),
    "iframes_inline": (
        lambda r: next(f for f in r.iframes if f.where == "inline"),
        {"srcdoc_has": "SRCDOC_BODY"},
    ),
    "iframes_external": (
        lambda r: next(f for f in r.iframes if f.where == "external"),
        {"raw": "inner.html", "srcdoc": None},
    ),
    "links": (
        lambda r: next(link for link in r.links if link.raw == "about.html"),
        {"text": "ABOUT_LINK"},
    ),
    "images": (
        lambda r: r.images[0],
        {"raw": "pic.png", "alt": "IMG_ALT"},
    ),
    "forms": (
        lambda r: r.forms[0],
        {"raw": "/submit", "method": "post"},
    ),
    "meta": (
        lambda r: next(m for m in r.meta if m.name == "generator"),
        {"content": "META_GENERATOR"},
    ),
    "comments": (
        lambda r: r.comments[0],
        {"text": "COMMENT_ONE"},
    ),
    "json_ld": (
        lambda r: r.json_ld[0],
        {"data_name": "LDJSON_NAME"},
    ),
}


@pytest.fixture(params=["live", "snapshot"])
def page(bucket_page: str, request: pytest.FixtureRequest) -> onyxweb.RenderResult:
    """The fixture page as fetched, and as a saved-then-loaded snapshot of it.

    Every read below must agree for both: a snapshot is the same page without a browser.
    """
    fetched = onyxweb.fetch(bucket_page)
    return reloaded(fetched) if request.param == "snapshot" else fetched


@pytest.mark.parametrize("name", list(RECORDS))
def test_record_field(page: onyxweb.RenderResult, name: str) -> None:
    pick, expected = RECORDS[name]
    record = pick(page)
    for field, value in expected.items():
        if field == "srcdoc_has":
            assert value in (record.srcdoc or "")
        elif field == "data_name":
            assert record.data["name"] == value
        else:
            assert getattr(record, field) == value, field


# ----------------------------------------------------------------------------
# MARKUP_EDGES — URL resolution, split rules, and malformed markup
# ----------------------------------------------------------------------------


def test_relative_src_resolves_against_the_document_url(page: onyxweb.RenderResult) -> None:
    app = next(s for s in page.scripts if s.raw == "static/app.js")
    assert app.url == page.final_url.replace("/page.html", "/static/app.js")


def test_base_href_overrides_the_document_url(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/b.html").respond_with_data(
        '<html><head><base href="/assets/"><script src="x.js"></script></head><body></body></html>',
        content_type="text/html",
    )
    script = onyxweb.fetch(httpserver.url_for("/b.html")).scripts[0]
    assert script.url == httpserver.url_for("/assets/x.js")
    assert script.raw == "x.js"


def test_protocol_relative_takes_the_document_scheme(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/p.html").respond_with_data(
        '<html><head><script src="//cdn.example.invalid/a.js"></script></head><body></body></html>',
        content_type="text/html",
    )
    # Blocked at the network layer so the fetch never dials a host that can't resolve.
    with onyxweb.Client(block_urls=["*://cdn.example.invalid/*"]) as c:
        script = c.fetch(httpserver.url_for("/p.html")).scripts[0]
    assert script.url == "http://cdn.example.invalid/a.js"
    assert script.raw == "//cdn.example.invalid/a.js"


def test_a_data_url_page_leaves_url_equal_raw(data_url: DataUrl) -> None:
    """A ``data:`` document has no base to join against, so the raw value stands."""
    page_html = b'<html><head><script src="/a.js"></script></head><body></body></html>'
    script = onyxweb.fetch(data_url(page_html)).scripts[0]
    assert script.url == script.raw == "/a.js"


def test_srcdoc_beats_src(httpserver: HTTPServer) -> None:
    """``srcdoc`` wins: the browser renders it and never requests ``src``."""
    httpserver.expect_request("/f.html").respond_with_data(
        '<html><body><iframe srcdoc="&lt;p&gt;BODY&lt;/p&gt;" src="unused.html"></iframe>'
        "</body></html>",
        content_type="text/html",
    )
    frame = onyxweb.fetch(httpserver.url_for("/f.html")).iframes[0]
    assert frame.where == "inline"
    assert "BODY" in (frame.srcdoc or "")
    # The declared-but-unused src is still a recon signal, so it stays on the record.
    assert frame.raw == "unused.html"


def test_malformed_json_ld_is_skipped_not_half_parsed(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/j.html").respond_with_data(
        '<html><head><script type="application/ld+json">{"a":</script>'
        '<script type="application/ld+json">{"ok":1}</script></head>'
        "<body></body></html>",
        content_type="text/html",
    )
    blocks = onyxweb.fetch(httpserver.url_for("/j.html")).json_ld
    assert len(blocks) == 1
    assert blocks[0].data == {"ok": 1}


def test_ld_json_is_not_a_script(page: onyxweb.RenderResult) -> None:
    """Data is not code — an ld+json block belongs to neither script half."""
    assert all("LDJSON_NAME" not in (s.text or "") for s in page.scripts)
    assert all(s.type != "application/ld+json" for s in page.scripts)


def test_blank_frames_have_neither_srcdoc_nor_a_resolved_src(page: onyxweb.RenderResult) -> None:
    blank = [(f.raw, f.url, f.srcdoc) for f in page.iframes if f.where == "blank"]
    assert blank == [("about:blank", "about:blank", None), (None, None, None)]


def test_empty_img_src_is_no_image(page: onyxweb.RenderResult) -> None:
    """An empty src fetches nothing, so it is no image — not a copy of the page URL."""
    assert [i.raw for i in page.images] == ["pic.png"]


def test_meta_charset_and_nameless_meta(page: onyxweb.RenderResult) -> None:
    meta = {m.name: m.content for m in page.meta}
    assert meta["charset"] == "utf-8"
    nameless = next(m for m in page.meta if m.name == "")
    assert nameless.content == ""


def test_accents_repr_clips_on_a_character_boundary(page: onyxweb.RenderResult) -> None:
    """A repr clips at 60 characters; byte 60 of this text falls inside an 'é'."""
    accents = page.dom.query_one("#accents")
    assert accents is not None
    assert repr(accents).endswith('"a' + "é" * 59 + '…">')


# ----------------------------------------------------------------------------
# PARTITION — inline/external sides sum to the whole and agree with the overview
# ----------------------------------------------------------------------------


@pytest.mark.parametrize("bucket", ["scripts", "styles", "iframes"])
def test_sides_sum_to_the_whole(page: onyxweb.RenderResult, bucket: str) -> None:
    whole = getattr(page, bucket)
    inline = len(getattr(page.content, bucket))
    external = len(getattr(page.resources, bucket))
    blank = len(whole.search("blank", field="where"))
    assert inline > 0
    assert external > 0
    assert blank == (2 if bucket == "iframes" else 0)
    assert inline + external + blank == len(whole)


@pytest.mark.parametrize("bucket", ["comments", "forms", "meta", "json_ld"])
def test_content_holds_document_only_buckets_whole(page: onyxweb.RenderResult, bucket: str) -> None:
    assert len(getattr(page.content, bucket)) == len(getattr(page, bucket)) > 0


@pytest.mark.parametrize("bucket", ["images", "links"])
def test_resources_hold_url_only_buckets_whole(page: onyxweb.RenderResult, bucket: str) -> None:
    assert len(getattr(page.resources, bucket)) == len(getattr(page, bucket)) > 0


def test_overview_counts_agree_with_the_buckets(page: onyxweb.RenderResult) -> None:
    views = {"inline": page.content, "external": page.resources}
    for row in page.overview().rows:
        bucket = getattr(views.get(row.where or "", page), row.bucket)
        if row.where == "blank":
            bucket = bucket.search("blank", field="where")
        assert len(bucket) == row.count, row


def test_overview_counts_every_bucket_and_side(page: onyxweb.RenderResult) -> None:
    ov = page.overview()
    assert isinstance(ov, Overview)
    assert {(row.bucket, row.where): row.count for row in ov.rows} == {
        ("scripts", "inline"): 2,
        ("scripts", "external"): 2,
        ("styles", "inline"): 2,
        ("styles", "external"): 2,
        ("iframes", "inline"): 1,
        ("iframes", "external"): 1,
        ("iframes", "blank"): 2,
        ("comments", None): 3,
        ("forms", None): 1,
        ("meta", None): 4,
        ("json_ld", None): 1,
        ("links", None): 2,
        ("images", None): 1,
    }


def test_inline_bytes_match_the_records(page: onyxweb.RenderResult) -> None:
    sizes = {(row.bucket, row.where): row.size for row in page.overview().rows}
    assert sizes[("scripts", "inline")] == sum(
        len((s.text or "").encode()) for s in page.content.scripts
    )
    assert sizes[("comments", None)] == sum(len(c.text.encode()) for c in page.comments)
    assert sizes[("scripts", "external")] is None
    assert sizes[("links", None)] is None


def test_overview_totals_match_the_document(page: onyxweb.RenderResult) -> None:
    ov = page.overview()
    assert ov.text_size == len(page.text.encode())
    assert ov.total_size == len(page.html.encode())


def test_overview_builds_no_records(page: onyxweb.RenderResult) -> None:
    page.overview()
    assert page.scripts._records is None
    assert page.links._records is None


def test_all_lists_fetched_resources_in_document_order(page: onyxweb.RenderResult) -> None:
    loaded = list(page.resources.all())
    assert [(res.kind, res.raw) for res in loaded] == [
        ("style", "site.css"),
        ("style", "/deep/print.css"),
        ("script", "static/app.js"),
        ("script", "/deep/vendor.js"),
        ("image", "pic.png"),
        ("iframe", "inner.html"),
    ]


def test_all_excludes_links_and_resolves_urls(page: onyxweb.RenderResult) -> None:
    """A link is referenced, never fetched, so it is not a loaded resource."""
    loaded = page.resources.all()
    assert len(loaded) > 0
    assert all(res.raw not in ("about.html", "/deep/faq.html") for res in loaded)
    assert all(res.url.startswith("http://") for res in loaded)


def test_all_excludes_an_iframe_whose_srcdoc_wins(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/f.html").respond_with_data(
        '<html><body><iframe srcdoc="&lt;p&gt;x&lt;/p&gt;" src="unused.html"></iframe>'
        '<iframe src="used.html"></iframe></body></html>',
        content_type="text/html",
    )
    httpserver.expect_request("/used.html").respond_with_data("<p>y</p>", content_type="text/html")
    loaded = onyxweb.fetch(httpserver.url_for("/f.html")).resources.all()
    assert [res.raw for res in loaded] == ["used.html"]


# ----------------------------------------------------------------------------
# wrong view / laziness / caching
# ----------------------------------------------------------------------------


def test_content_points_to_resources_for_images(page: onyxweb.RenderResult) -> None:
    with pytest.raises(AttributeError, match=r"r\.resources\.images"):
        page.content.images  # type: ignore[attr-defined]  # noqa: B018


def test_resources_points_to_content_for_comments(page: onyxweb.RenderResult) -> None:
    with pytest.raises(AttributeError, match=r"r\.content\.comments"):
        page.resources.comments  # type: ignore[attr-defined]  # noqa: B018


def test_unknown_attribute_is_a_plain_attribute_error(page: onyxweb.RenderResult) -> None:
    """Only real buckets get the redirect hint; a typo gets no false lead."""
    with pytest.raises(AttributeError) as exc:
        page.content.nonsense  # type: ignore[attr-defined]  # noqa: B018
    assert "r.resources" not in str(exc.value)


def test_filtered_len_does_not_materialize(page: onyxweb.RenderResult) -> None:
    scripts = page.content.scripts
    assert len(scripts) == 2
    assert scripts._records is None


def test_view_buckets_are_cached(page: onyxweb.RenderResult) -> None:
    assert page.content is page.content
    assert page.content.scripts is page.content.scripts
    assert page.resources.all() is page.resources.all()


def test_side_filter_rejects_an_unsplit_bucket(page: onyxweb.RenderResult) -> None:
    with pytest.raises(ValueError, match="no inline/external split"):
        page.dom.buckets.count("links", "inline")


def test_side_filter_rejects_an_unknown_side(page: onyxweb.RenderResult) -> None:
    with pytest.raises(ValueError, match="inline.*external"):
        page.dom.buckets.count("scripts", "sideways")


def test_len_answers_without_materializing(page: onyxweb.RenderResult) -> None:
    scripts = page.scripts
    assert len(scripts) == 4
    assert scripts._records is None


def test_indexing_materializes_once(page: onyxweb.RenderResult) -> None:
    scripts = page.scripts
    first = scripts[0]
    assert scripts._records is not None
    assert scripts[0] is first


def test_bucket_is_a_sequence(page: onyxweb.RenderResult) -> None:
    from onyxweb.records import Bucket

    scripts = page.scripts
    assert isinstance(scripts, Bucket)
    assert len(list(scripts)) == 4
    assert len(scripts[:2]) == 2
    assert scripts[-1] == scripts[3]


def test_asdict_gives_plain_dicts(page: onyxweb.RenderResult) -> None:
    rows = page.scripts.asdict()
    assert all(isinstance(row, dict) for row in rows)
    assert {"where", "text", "url", "raw", "type", "attrs"} <= set(rows[0])


def test_record_repr_truncates_a_large_body(httpserver: HTTPServer) -> None:
    """Evaluating a record must never dump a 200 KB body to the terminal."""
    padding = "x" * 200_000
    httpserver.expect_request("/big.html").respond_with_data(
        f"<html><head><script>var pad='{padding}';</script></head><body></body></html>",
        content_type="text/html",
    )
    scripts = onyxweb.fetch(httpserver.url_for("/big.html")).scripts
    script = scripts[0]
    assert len(script.text or "") > 200_000
    assert len(repr(script)) < 200
    [run] = scripts.matches("x+", regex=True)
    assert len(run.text) == 200_000
    assert len(repr(run)) < 200
    assert scripts.text(0) == script.text


# ----------------------------------------------------------------------------
# LAZINESS — nothing materializes across FFI before it's asked for
# ----------------------------------------------------------------------------


def test_overview_and_repr_never_call_rust_records(
    bucket_page: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from onyxweb._onyxweb import Buckets as RustBuckets

    calls: list[str] = []
    original = RustBuckets.records

    def counted(
        self: RustBuckets,
        bucket: str,
        where_: str | None = None,
        queries: list[tuple[str, str | None, bool, bool]] | None = None,
    ) -> list[dict[str, Any]]:
        calls.append("records")
        return original(self, bucket, where_, queries)

    monkeypatch.setattr(RustBuckets, "records", counted)
    r = onyxweb.fetch(bucket_page)
    r.overview()
    repr(r)
    repr(r.scripts)
    r.search("window")
    assert calls == [], f"a display-only path materialized records: {calls}"
    r.scripts[0]
    assert calls == ["records"], "indexing must materialize exactly once"


def test_dom_is_not_built_until_something_needs_it(bucket_page: str) -> None:
    r = onyxweb.fetch(bucket_page)
    assert len(r) > 0
    assert "VISIBLE_HEADING" in r
    assert r._dom is None
    _ = r.title  # first thing that needs the Rust-side Dom/Buckets
    assert r._dom is not None


# ----------------------------------------------------------------------------
# SEARCH — bucket.search / bucket.matches / result-wide search
# ----------------------------------------------------------------------------


def test_search_finds_inline_source(page: onyxweb.RenderResult) -> None:
    found = page.scripts.search("INLINE_JS_ONE")
    assert len(found) == 1
    assert "INLINE_JS_ONE" in (found[0].text or "")
    assert len(page.scripts.matches("PADDING")) == 17  # every occurrence is its own match


def test_search_finds_urls(page: onyxweb.RenderResult) -> None:
    assert [s.raw for s in page.scripts.search("app.js")] == ["static/app.js"]
    # url, raw and the src attribute hold the same text; it is one place, one match.
    assert [(m.index, m.field) for m in page.scripts.matches("app.js")] == [(2, "url")]


def test_search_finds_attribute_values_and_names(page: onyxweb.RenderResult) -> None:
    assert len(page.scripts.search("sha384-ABC123")) == 1
    assert len(page.scripts.search("nonce")) == 1


def test_search_reaches_nested_form_fields(page: onyxweb.RenderResult) -> None:
    assert len(page.forms.search("CSRF_TOKEN")) == 1
    assert [m.field for m in page.forms.matches("CSRF_TOKEN")] == ["inputs"]


def test_search_reaches_json_ld(page: onyxweb.RenderResult) -> None:
    assert len(page.json_ld.search("LDJSON_NAME")) == 1
    assert [m.field for m in page.json_ld.matches("LDJSON_NAME")] == ["data"]


def test_search_matches_the_record_text_not_raw_markup(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/l.html").respond_with_data(
        "<html><body><a href='x.html'>foo<b>bar</b></a></body></html>",
        content_type="text/html",
    )
    assert len(onyxweb.fetch(httpserver.url_for("/l.html")).links.search("foobar")) == 1


def test_search_skips_classification_fields_by_default(page: onyxweb.RenderResult) -> None:
    """``where`` is onyxweb's label, not page bytes — matches only when asked for."""
    assert len(page.scripts.search("external")) == 0
    assert len(page.scripts.search("external", field="where")) == 2
    assert page.scripts.matches("external") == []
    assert len(page.scripts.matches("external", field="where")) == 2


def test_search_is_case_insensitive_by_default(page: onyxweb.RenderResult) -> None:
    assert len(page.scripts.search("inline_js_one")) == 1
    assert len(page.scripts.search("inline_js_one", case_sensitive=True)) == 0
    assert [m.text for m in page.scripts.matches("inline_js_one")] == ["INLINE_JS_ONE"]


def test_field_narrows_the_search(page: onyxweb.RenderResult) -> None:
    assert len(page.scripts.search("window")) == 2
    assert len(page.scripts.search("window", field="url")) == 0
    assert len(page.scripts.search("app", field="url")) == 1
    assert {m.field for m in page.scripts.matches("window")} == {"text"}
    assert page.scripts.matches("window", field="url") == []


def test_search_unknown_field_names_the_real_ones(page: onyxweb.RenderResult) -> None:
    with pytest.raises(ValueError, match="text"):
        page.scripts.search("x", field="nope")
    with pytest.raises(ValueError, match="text"):
        page.scripts.matches("x", field="nope")


def test_search_regex_and_capture_groups(page: onyxweb.RenderResult) -> None:
    assert len(page.scripts.search(r"INLINE_JS_(ONE|TWO)", regex=True)) == 2
    [key] = page.scripts.matches(r'"apiKey":"(\w+)"', regex=True)
    assert key.value == "DEEP_KEY_42"
    assert key.groups == ("DEEP_KEY_42",)
    assert key.text == '"apiKey":"DEEP_KEY_42"'
    assert (key.index, key.field) == (1, "text")
    # Offsets count characters (the body holds a "café"), so they slice it in Python.
    assert (page.scripts[1].text or "")[key.start : key.end] == key.text


def test_search_unsupported_regex_is_a_value_error(page: onyxweb.RenderResult) -> None:
    """Rust's ``regex`` has no lookaround — linear time is the trade."""
    with pytest.raises(ValueError, match="pattern"):
        len(page.scripts.search(r"(?<=cfg)=", regex=True))


def test_search_treats_metacharacters_literally(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/m.html").respond_with_data(
        "<html><head><script src='app.js'></script><script src='appXjs'></script>"
        "</head><body></body></html>",
        content_type="text/html",
    )
    found = onyxweb.fetch(httpserver.url_for("/m.html")).scripts.search("app.js")
    assert [s.raw for s in found] == ["app.js"]


def test_chained_searches_narrow(page: onyxweb.RenderResult) -> None:
    found = page.scripts.search("window").search("tracker")
    assert len(found) == 1
    assert "INLINE_JS_TWO" in (found[0].text or "")
    assert [m.index for m in found.matches("apiKey")] == [0]


def test_search_respects_a_view(page: onyxweb.RenderResult) -> None:
    assert len(page.content.scripts.search("app.js")) == 0
    assert len(page.resources.scripts.search("app.js")) == 1


def test_search_stays_lazy(page: onyxweb.RenderResult) -> None:
    found = page.scripts.search("window")
    assert len(found) == 2
    repr(found)
    found.matches("window")
    assert found._records is None


def test_result_search_returns_only_buckets_with_matches(page: onyxweb.RenderResult) -> None:
    assert list(page.search("CSRF_TOKEN")) == ["forms"]
    assert set(page.search("_ONE")) == {"scripts", "styles", "comments"}


def test_result_search_with_field_skips_buckets_without_it(page: onyxweb.RenderResult) -> None:
    hits = page.search("deep", field="url")
    assert set(hits) == {"scripts", "styles", "links"}


def test_result_search_with_no_match_is_empty(page: onyxweb.RenderResult) -> None:
    assert page.search("zzz_absent") == {}


def test_fields_lists_what_can_be_searched(page: onyxweb.RenderResult) -> None:
    assert page.scripts.fields == ("where", "text", "url", "raw", "type", "attrs")


# ----------------------------------------------------------------------------
# DISPLAY — repr golden rows and the prnt contract
# ----------------------------------------------------------------------------


def test_overview_repr_is_the_table(page: onyxweb.RenderResult) -> None:
    table = repr(page.overview())
    assert table.splitlines()[0].split() == ["bucket", "where", "n", "size"]
    assert "json_ld" in table
    assert table.splitlines()[-1].startswith("total")


def test_result_repr_is_one_orienting_line(page: onyxweb.RenderResult) -> None:
    line = repr(page)
    assert "\n" not in line
    assert line.startswith("<RenderResult ")
    assert "4 scripts" in line
    assert "2 links" in line
    assert "1 form ·" in line
    assert len(line) < 200


def test_result_repr_without_a_capture_does_not_raise() -> None:
    assert repr(onyxweb.RenderResult("<p>hi</p>")).startswith("<RenderResult ")


def test_split_bucket_header_counts_each_side(page: onyxweb.RenderResult) -> None:
    assert repr(page.scripts).splitlines()[0] == "Scripts · 4 (2 inline, 2 external)"
    assert repr(page.iframes).splitlines()[0] == "Iframes · 4 (1 inline, 1 external, 2 blank)"


def test_filtered_bucket_header_names_its_side(page: onyxweb.RenderResult) -> None:
    assert repr(page.content.scripts).splitlines()[0] == "Scripts · 2 inline"


def test_unsplit_bucket_table_has_no_where_column(page: onyxweb.RenderResult) -> None:
    table = repr(page.links)
    assert table.splitlines()[0] == "Links · 2"
    assert "where" not in table


def test_loaded_table_labels_its_column_kind(page: onyxweb.RenderResult) -> None:
    header = repr(page.resources.all()).splitlines()[1]
    assert header.split()[:2] == ["#", "kind"]


def test_empty_bucket_says_so(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/bare.html").respond_with_data(
        "<html><body><p>nothing here</p></body></html>", content_type="text/html"
    )
    assert repr(onyxweb.fetch(httpserver.url_for("/bare.html")).links) == "Links · empty"


def test_long_bucket_shows_head_then_remainder(httpserver: HTTPServer) -> None:
    anchors = "".join(f"<a href='p{i}.html'>p{i}</a>" for i in range(HEAD_ROWS + 5))
    repeats = "<!--REPEAT-->" * (HEAD_ROWS + 5) + "<!--ONCE-->"
    httpserver.expect_request("/many.html").respond_with_data(
        f"<html><body>{anchors}{repeats}</body></html>", content_type="text/html"
    )
    r = onyxweb.fetch(httpserver.url_for("/many.html"))
    lines = repr(r.links).splitlines()
    assert len(lines) == 2 + HEAD_ROWS + 1  # title, column header, rows, remainder
    assert lines[-1].startswith("… 5 more")
    # Identical records share one counted row, so a repeat can't bury the rest.
    title, _, repeat, once = repr(r.comments).splitlines()
    assert title == f"Comments · {HEAD_ROWS + 6}"
    assert repeat.split()[0] == "0"
    assert repeat.split()[-2:] == [str(HEAD_ROWS + 5), "REPEAT"]
    assert once.split()[0] == str(HEAD_ROWS + 5)
    assert once.split()[-1] == "ONCE"


def test_remainder_line_points_at_search(httpserver: HTTPServer) -> None:
    anchors = "".join(f"<a href='p{i}.html'>p{i}</a>" for i in range(15))
    httpserver.expect_request("/many.html").respond_with_data(
        f"<html><body>{anchors}</body></html>", content_type="text/html"
    )
    last = repr(onyxweb.fetch(httpserver.url_for("/many.html")).links).splitlines()[-1]
    assert ".search(q)" in last


def test_searched_table_shows_where_each_record_matched(page: onyxweb.RenderResult) -> None:
    """A long body previews the match, not its first line."""
    assert (
        repr(page.scripts.search("window")).splitlines()[0] == "Scripts · 2 of 4 matching 'window'"
    )
    title, _, row = repr(page.scripts.search("apiKey")).splitlines()
    assert title == "Scripts · 1 of 4 matching 'apiKey'"
    assert "DEEP_KEY_42" in row


def test_searched_view_header_names_its_side(page: onyxweb.RenderResult) -> None:
    header = repr(page.content.scripts.search("window")).splitlines()[0]
    assert header == "Scripts · 2 of 2 inline matching 'window'"


def test_empty_search_still_says_what_it_searched(page: onyxweb.RenderResult) -> None:
    table = repr(page.scripts.search("zzz_absent"))
    assert table == "Scripts · 0 of 4 matching 'zzz_absent'"


@pytest.mark.parametrize(
    ("n", "shown"),
    [
        (None, "—"),
        (0, "0 B"),
        (412, "412 B"),
        (3_100, "3.1 KB"),
        (204_000, "204 KB"),
        (1_200_000, "1.2 MB"),
    ],
)
def test_size_str(n: int | None, shown: str) -> None:
    assert size_str(n) == shown


def test_url_preview_keeps_host_and_file_name(httpserver: HTTPServer) -> None:
    """Files on one long CDN path differ only at the end; clipping the tail hides that."""
    base = "https://cdn.example.com/assets/bundles/2026.09.16/release-candidate/"
    httpserver.expect_request("/cdn.html").respond_with_data(
        f"<html><body><a href='{base}first-unique.js'>a</a>"
        f"<a href='{base}second-unique.js'>b</a></body></html>",
        content_type="text/html",
    )
    table = repr(onyxweb.fetch(httpserver.url_for("/cdn.html")).links)
    assert "cdn.example.com" in table
    assert "first-unique.js" in table
    assert "second-unique.js" in table


def test_every_preview_identifies_its_record(
    page: onyxweb.RenderResult, httpserver: HTTPServer
) -> None:
    """No row renders blank or as a bare ``=``; a form row leads with its field count."""
    previews = {
        name: [row["preview"] for row in page.dom.buckets.head(name, 50, PREVIEW_WIDTH)]
        for name in PAGE_BUCKETS
    }
    for name, shown in previews.items():
        assert all(p not in ("", "=") for p in shown), (name, shown)
    assert "charset=utf-8" in previews["meta"]
    assert "content= id=canon" in previews["meta"]  # scraper sorts attributes by name
    assert "about:blank · id=ad_slot" in previews["iframes"]

    httpserver.expect_request("/forms.html").respond_with_data(
        "<html><body><form><input name='a'></form>"
        "<form><input name='a'><input name='b'><input type='hidden' name='c'></form>"
        "</body></html>",
        content_type="text/html",
    )
    forms = onyxweb.fetch(httpserver.url_for("/forms.html")).forms
    rows = repr(forms).splitlines()[2:]
    assert "1 field ·" in rows[0]
    assert "3 fields" in rows[1]
    assert repr(forms[0]).endswith("1 field>")


def test_prnt_prints_instead_of_returning(
    page: onyxweb.RenderResult, capfd: pytest.CaptureFixture[str]
) -> None:
    """Every ``prnt`` method prints what it would show and returns None."""
    assert page.overview(prnt=True) is None
    assert capfd.readouterr().out.rstrip("\n") == repr(page.overview())
    assert page.scripts.text(1, prnt=True) is None
    assert capfd.readouterr().out.rstrip("\n") == page.scripts.text(1)
    assert page.scripts.matches("window", prnt=True) is None
    assert capfd.readouterr().out.splitlines()[0] == "Scripts · 2 matches in 2 of 4"


# ----------------------------------------------------------------------------
# MISUSE — errors the public API can actually reach
# ----------------------------------------------------------------------------


def test_unknown_bucket_names_the_valid_ones(page: onyxweb.RenderResult) -> None:
    with pytest.raises(ValueError, match="unknown bucket") as exc:
        page.dom.buckets.count("nope")
    assert "scripts" in str(exc.value)


# ----------------------------------------------------------------------------
# DOM — the CSS-selector engine, on the same fixture the buckets read
# ----------------------------------------------------------------------------


def test_query_and_query_one(page: onyxweb.RenderResult) -> None:
    ps = page.dom.query("p")
    assert len(ps) == 2  # "Body sentence." + #accents
    assert all(hasattr(e, "text") for e in ps)
    h1 = page.dom.query_one("h1")
    assert h1 is not None
    assert h1.text == "VISIBLE_HEADING"
    assert page.dom.query_one("nonexistent-tag-xyz") is None


def test_count_and_exists(page: onyxweb.RenderResult) -> None:
    assert page.dom.count("p") == 2
    assert page.dom.count("nonexistent-tag-xyz") == 0
    assert page.dom.exists("h1") is True
    assert page.dom.exists("nonexistent-tag-xyz") is False


def test_select_is_an_alias_for_query(page: onyxweb.RenderResult) -> None:
    assert [e.html for e in page.dom.select("h1")] == [e.html for e in page.dom.query("h1")]
    a = page.dom.select_one("h1")
    b = page.dom.query_one("h1")
    assert a is not None and b is not None and a.html == b.html


def test_find_by_tag_class_id_and_attrs(page: onyxweb.RenderResult) -> None:
    by_tag = page.dom.find("h1")
    assert by_tag is not None and by_tag.tag == "h1"
    by_id = page.dom.find(id="accents")
    assert by_id is not None and by_id.tag == "p"
    by_attr = page.dom.find(name="csrf")
    assert by_attr is not None and by_attr.tag == "input"
    assert page.dom.find("p", id="accents") is not None
    assert page.dom.find("a", id="accents") is None


def test_find_all_with_limit(page: onyxweb.RenderResult) -> None:
    all_meta = page.dom.find_all("meta")
    assert len(all_meta) == 4
    assert len(page.dom.find_all("meta", limit=2)) == 2


def test_element_attr_and_attrs(page: onyxweb.RenderResult) -> None:
    """The fixture's own `<html lang>` / `<body data-page>` prove attr/attrs on the
    element that IS a Dom.query_one() result works, not just on a queried descendant."""
    html_el = page.dom.query_one("html")
    assert html_el is not None
    assert html_el.attr("lang") == "en"
    assert html_el.attrs == {"lang": "en"}
    body_el = page.dom.query_one("body")
    assert body_el is not None
    assert body_el.attr("data-page") == "bucket"
    assert body_el.attrs == {"data-page": "bucket"}
    link = page.dom.query_one("a")
    assert link is not None
    href = link.attr("href")
    assert href is not None and href == "about.html"
    assert "href" in link.attrs


def test_scoped_query_excludes_a_node_outside_and_excludes_self(page: onyxweb.RenderResult) -> None:
    body = page.dom.query_one("body")
    assert body is not None
    # h1 is inside body: reachable from a body-scoped query.
    assert len(body.query("h1")) == 1
    # A selector matching the scope element itself must never include it.
    assert body.query("body") == []
    assert body.query_one("body") is None


def test_nested_find_scoped_to_an_element(page: onyxweb.RenderResult) -> None:
    form = page.dom.find("form")
    assert form is not None
    hidden = form.find(name="csrf")
    assert hidden is not None and hidden.attr("value") == "CSRF_TOKEN"
    assert len(form.find_all("input")) == 2


# ----------------------------------------------------------------------------
# TEXT — script/style/noscript/template excluded; HTML keeps them
# ----------------------------------------------------------------------------

_TEXT_PAGE = (
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
_TEXT_NOISE = (
    "JS_NOISE",
    "CSS_NOISE",
    "INLINE_JS_NOISE",
    "INLINE_CSS_NOISE",
    "NOSCRIPT_NOISE",
    "TEMPLATE_NOISE",
)


def test_result_text_excludes_script_and_style(data_url: DataUrl) -> None:
    r = onyxweb.fetch(data_url(_TEXT_PAGE))
    text = r.text
    assert "Visible Heading" in text
    assert "Body sentence." in text
    for noise in _TEXT_NOISE:
        assert noise not in text, f"{noise} leaked into .text"


def test_element_text_excludes_script(data_url: DataUrl) -> None:
    page_html = data_url(
        b"<html><body><div id='wrap'>Kept<script>var x='EL_NOISE';</script></div></body></html>"
    )
    el = onyxweb.fetch(page_html).dom.query_one("#wrap")
    assert el is not None
    assert "Kept" in el.text
    assert "EL_NOISE" not in el.text


def test_find_text_excludes_script(data_url: DataUrl) -> None:
    found = onyxweb.fetch(data_url(_TEXT_PAGE)).dom.find("body")
    assert found is not None
    assert "Visible Heading" in found.text
    assert "INLINE_JS_NOISE" not in found.text


def test_html_still_contains_the_scripts(data_url: DataUrl) -> None:
    """Only text extraction changes; the captured HTML stays complete."""
    r = onyxweb.fetch(data_url(_TEXT_PAGE))
    assert "INLINE_JS_NOISE" in str(r)
    assert "INLINE_CSS_NOISE" in str(r)


def test_title_is_the_title_tag(page: onyxweb.RenderResult) -> None:
    assert page.title == "Bucket Fixture"


# ----------------------------------------------------------------------------
# RAW ACCESS — str(r), `in`, len(r), a hand-built RenderResult
# ----------------------------------------------------------------------------

_NON_ASCII_PAGE = "<html><body>hello — éèê</body></html>"


@pytest.fixture
def non_ascii_page(httpserver: HTTPServer) -> str:
    httpserver.expect_request("/na.html").respond_with_response(
        Response(_NON_ASCII_PAGE, content_type="text/html; charset=utf-8")
    )
    return httpserver.url_for("/na.html")


def test_raw_access_is_the_html(non_ascii_page: str) -> None:
    r = onyxweb.fetch(non_ascii_page)
    html = r.html
    assert str(r) == html
    assert "éèê" in r
    assert "surely-not-present-xyz" not in r
    assert len(r) == len(html)


def test_contains_is_case_sensitive(non_ascii_page: str) -> None:
    r = onyxweb.fetch(non_ascii_page)
    assert "hello" in r
    assert "HELLO" not in r


def test_len_and_contains_build_no_dom(non_ascii_page: str) -> None:
    """Counting or scanning the raw capture needs no parse and no copy into a Dom —
    and the Rust `char_len` must count characters, not bytes, on a non-ASCII page."""
    r = onyxweb.fetch(non_ascii_page)
    assert len(r) > 0
    assert r._dom is None
    assert r._html is None  # neither materialized the document
    before = len(r)
    after = len(r.html)  # now materialized — must be the same count either way
    assert before == after
    assert before < len(r.html.encode())  # chars, not the longer UTF-8 byte count
    assert "éèê" in r


def test_repr_is_one_line_under_200_chars(non_ascii_page: str) -> None:
    r = repr(onyxweb.fetch(non_ascii_page))
    assert "\n" not in r
    assert len(r) < 200


def test_raw_access_without_a_capture() -> None:
    r = onyxweb.RenderResult("<p>hello</p>")
    assert "hello" in r
    assert "absent" not in r
    assert len(r) == len("<p>hello</p>")
    assert r.text == "hello"  # the buckets read the html it holds


# ----------------------------------------------------------------------------
# THREADS — a result crosses threads; no view may pin one
# ----------------------------------------------------------------------------


def _touch_in_thread(
    r: onyxweb.RenderResult, touch: Callable[[onyxweb.RenderResult], object]
) -> None:
    import threading

    errors: list[BaseException] = []

    def run() -> None:
        try:
            touch(r)
        except BaseException as be:  # PanicException derives from BaseException
            errors.append(be)

    worker = threading.Thread(target=run)
    worker.start()
    worker.join()
    assert not errors, errors


@pytest.mark.parametrize(
    "first",
    [len, lambda r: "x" in r, lambda r: len(r.scripts), lambda r: r.dom.query("a")],
    ids=["len", "contains", "bucket", "dom"],
)
def test_result_is_usable_from_a_second_thread(
    bucket_page: str, first: Callable[[onyxweb.RenderResult], object]
) -> None:
    r = onyxweb.fetch(bucket_page)
    _touch_in_thread(r, first)
    assert len(r.scripts) == 4
    assert r.title == "Bucket Fixture"
    assert len(r.content.scripts) == 2


# ----------------------------------------------------------------------------
# the fetch() shape — RenderResult vs Dom, status/redirect on the main frame
# ----------------------------------------------------------------------------


def test_dom_is_only_the_selector_engine(page: onyxweb.RenderResult) -> None:
    """Whole-document extraction lives on the result and its buckets, not `dom`."""
    for gone in ("text", "html", "title", "links", "images", "contains", "find_all_text"):
        assert not hasattr(page.dom, gone), gone
    for kept in ("query", "query_one", "count", "exists", "select", "select_one", "find"):
        assert hasattr(page.dom, kept), kept


def test_fetch_status_on_the_main_frame(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/missing").respond_with_data(
        "<html><body>nope</body></html>", status=404, content_type="text/html"
    )
    result = onyxweb.fetch(httpserver.url_for("/missing"))
    assert result.status_code == 404


def test_fetch_redirect_status_is_the_destinations(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/from").respond_with_response(
        Response(status=302, headers={"Location": httpserver.url_for("/to")})
    )
    httpserver.expect_request("/to").respond_with_data(
        "<html><body>arrived</body></html>", content_type="text/html"
    )
    result = onyxweb.fetch(httpserver.url_for("/from"))
    assert result.final_url.endswith("/to")
    assert result.status_code == 200


def test_client_fetch_is_reusable(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/r").respond_with_data(
        "<html><body>x</body></html>", content_type="text/html"
    )
    url = httpserver.url_for("/r")
    with onyxweb.Client() as client:
        a = client.fetch(url)
        b = client.fetch(url)
    assert len(a) > 0 and len(b) > 0


def test_is_not_a_str(page: onyxweb.RenderResult) -> None:
    """Dropping the base frees names like ``title`` for the page itself."""
    assert not isinstance(page, str)
    assert page.title == "Bucket Fixture"


def test_fetch_all_shares_the_same_output_model(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/fa").respond_with_data(
        "<html><body>x</body></html>", content_type="text/html"
    )
    with onyxweb.Client() as client:
        fr = client.fetch_all(httpserver.url_for("/fa"))
    assert isinstance(fr.html, onyxweb.RenderResult)
    assert fr.png[:8] == PNG_MAGIC
    width, height = struct.unpack(">II", fr.png[16:24])
    assert (width, height) == (1200, 800)
