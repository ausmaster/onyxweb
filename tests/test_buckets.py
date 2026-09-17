"""Buckets — the page sorted into categories, lazily, from Rust.

A million-line document can't be read whole. Buckets let a caller see what is
there and pull only what they need, so ``len()`` and ``repr()`` have to answer
without dragging every record across FFI.

Scripts and styles split across two halves: source that lives in this document
(``where="inline"``) and a URL the document pulls in (``where="external"``).
Every URL-bearing record carries ``url`` resolved absolute and ``raw`` exactly
as authored.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

import onyxweb
import pytest
from onyxweb.records import Bucket
from pytest_httpserver import HTTPServer

DataUrl = Callable[[bytes], str]


# --- the inline / external split -------------------------------------------


def test_scripts_split_inline_and_external(bucket_page: str) -> None:
    where = [s.where for s in onyxweb.fetch(bucket_page).scripts]
    assert where.count("inline") == 2
    assert where.count("external") == 2


def test_inline_script_carries_source_not_url(bucket_page: str) -> None:
    inline = [s for s in onyxweb.fetch(bucket_page).scripts if s.where == "inline"]
    assert any("INLINE_JS_ONE" in (s.text or "") for s in inline)
    assert all(s.url is None for s in inline)


def test_external_script_carries_url_not_source(bucket_page: str) -> None:
    ext = [s for s in onyxweb.fetch(bucket_page).scripts if s.where == "external"]
    assert {s.raw for s in ext} == {"static/app.js", "/deep/vendor.js"}
    assert all(s.text is None for s in ext)


def test_styles_split_inline_and_external(bucket_page: str) -> None:
    styles = onyxweb.fetch(bucket_page).styles
    inline = [s for s in styles if s.where == "inline"]
    ext = [s for s in styles if s.where == "external"]
    assert any("INLINE_CSS_ONE" in (s.text or "") for s in inline)
    assert {s.raw for s in ext} == {"site.css", "/deep/print.css"}


def test_ld_json_is_not_a_script(bucket_page: str) -> None:
    """Data is not code — an ld+json block belongs to neither script half."""
    scripts = onyxweb.fetch(bucket_page).scripts
    assert all("LDJSON_NAME" not in (s.text or "") for s in scripts)
    assert all(s.type != "application/ld+json" for s in scripts)


# --- attributes that matter for recon ---------------------------------------


def test_external_script_preserves_integrity_and_nonce(bucket_page: str) -> None:
    """SRI and CSP nonces are recon signals; they must survive extraction."""
    app = next(s for s in onyxweb.fetch(bucket_page).scripts if s.raw == "static/app.js")
    assert app.attrs["integrity"] == "sha384-ABC123"
    assert app.attrs["nonce"] == "N1"


def test_external_style_carries_media(bucket_page: str) -> None:
    site = next(s for s in onyxweb.fetch(bucket_page).styles if s.raw == "site.css")
    assert site.media == "screen"


# --- URL resolution ----------------------------------------------------------


def test_relative_src_resolves_against_document_url(bucket_page: str) -> None:
    app = next(s for s in onyxweb.fetch(bucket_page).scripts if s.raw == "static/app.js")
    assert app.url == bucket_page.replace("/page.html", "/static/app.js")


def test_base_href_overrides_document_url(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/b.html").respond_with_data(
        '<html><head><base href="/assets/"><script src="x.js"></script></head><body></body></html>',
        content_type="text/html",
    )
    script = onyxweb.fetch(httpserver.url_for("/b.html")).scripts[0]
    assert script.url == httpserver.url_for("/assets/x.js")
    assert script.raw == "x.js"


def test_protocol_relative_takes_document_scheme(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/p.html").respond_with_data(
        '<html><head><script src="//cdn.example.invalid/a.js"></script></head><body></body></html>',
        content_type="text/html",
    )
    # Blocked at the network layer so the fetch never dials a host that can't resolve.
    with onyxweb.Client(block_urls=["*://cdn.example.invalid/*"]) as c:
        script = c.fetch(httpserver.url_for("/p.html")).scripts[0]
    assert script.url == "http://cdn.example.invalid/a.js"
    assert script.raw == "//cdn.example.invalid/a.js"


def test_unresolvable_base_leaves_url_equal_raw(data_url: DataUrl) -> None:
    """A ``data:`` document has no base to join against, so the raw value stands."""
    page = b'<html><head><script src="/a.js"></script></head><body></body></html>'
    script = onyxweb.fetch(data_url(page)).scripts[0]
    assert script.url == script.raw == "/a.js"


# --- laziness ----------------------------------------------------------------


def test_len_answers_without_materializing(bucket_page: str) -> None:
    scripts = onyxweb.fetch(bucket_page).scripts
    assert len(scripts) == 4
    assert scripts._records is None


def test_repr_answers_without_materializing(bucket_page: str) -> None:
    scripts = onyxweb.fetch(bucket_page).scripts
    assert "4" in repr(scripts)
    assert scripts._records is None


def test_indexing_materializes_once(bucket_page: str) -> None:
    scripts = onyxweb.fetch(bucket_page).scripts
    first = scripts[0]
    assert scripts._records is not None
    assert scripts[0] is first


# --- the sequence contract ---------------------------------------------------


def test_bucket_is_a_sequence(bucket_page: str) -> None:
    scripts = onyxweb.fetch(bucket_page).scripts
    assert isinstance(scripts, Bucket)
    assert len(list(scripts)) == 4
    assert len(scripts[:2]) == 2
    assert scripts[-1] == scripts[3]


def test_asdict_gives_plain_dicts(bucket_page: str) -> None:
    rows = onyxweb.fetch(bucket_page).scripts.asdict()
    assert all(isinstance(row, dict) for row in rows)
    assert {"where", "text", "url", "raw", "type", "attrs"} <= set(rows[0])


def test_record_repr_truncates_large_body(httpserver: HTTPServer) -> None:
    """Evaluating a record must never dump a 200 KB body to the terminal."""
    padding = "x" * 200_000
    httpserver.expect_request("/big.html").respond_with_data(
        f"<html><head><script>var pad='{padding}';</script></head><body></body></html>",
        content_type="text/html",
    )
    script = onyxweb.fetch(httpserver.url_for("/big.html")).scripts[0]
    assert len(script.text or "") > 200_000
    assert len(repr(script)) < 200


# --- the remaining buckets ---------------------------------------------------


def test_links_carry_resolved_url_and_text(bucket_page: str) -> None:
    links = onyxweb.fetch(bucket_page).links
    assert {ln.raw for ln in links} == {"about.html", "/deep/faq.html"}
    about = next(ln for ln in links if ln.raw == "about.html")
    assert about.url == bucket_page.replace("/page.html", "/about.html")
    assert about.text.strip() == "ABOUT_LINK"


def test_images_carry_url_and_alt(bucket_page: str) -> None:
    img = onyxweb.fetch(bucket_page).images[0]
    assert img.raw == "pic.png"
    assert img.url == bucket_page.replace("/page.html", "/pic.png")
    assert img.alt == "IMG_ALT"


def test_iframes_split_srcdoc_from_src(bucket_page: str) -> None:
    frames = onyxweb.fetch(bucket_page).iframes
    external = next(f for f in frames if f.where == "external")
    inline = next(f for f in frames if f.where == "inline")
    assert external.raw == "inner.html"
    assert external.url == bucket_page.replace("/page.html", "/inner.html")
    assert inline.srcdoc is not None
    assert "SRCDOC_BODY" in inline.srcdoc


def test_iframe_with_srcdoc_and_src_is_inline(httpserver: HTTPServer) -> None:
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


def test_forms_carry_action_method_and_hidden_inputs(bucket_page: str) -> None:
    """Hidden fields are the point — a CSRF token is a recon signal."""
    form = onyxweb.fetch(bucket_page).forms[0]
    assert form.url == bucket_page.replace("/page.html", "/submit")
    assert form.raw == "/submit"
    assert form.method == "post"
    hidden = next(i for i in form.inputs if i.type == "hidden")
    assert (hidden.name, hidden.value) == ("csrf", "CSRF_TOKEN")
    assert {i.name for i in form.inputs} == {"csrf", "q"}


def test_meta_folds_name_and_property(bucket_page: str) -> None:
    meta = {m.name: m.content for m in onyxweb.fetch(bucket_page).meta}
    assert meta["generator"] == "META_GENERATOR"
    assert meta["og:title"] == "META_OG"


def test_comments_are_captured(bucket_page: str) -> None:
    texts = [c.text for c in onyxweb.fetch(bucket_page).comments]
    assert "COMMENT_ONE" in " ".join(texts)
    assert "COMMENT_TWO" in " ".join(texts)


def test_json_ld_is_decoded(bucket_page: str) -> None:
    blocks = onyxweb.fetch(bucket_page).json_ld
    assert len(blocks) == 1
    assert blocks[0].data["name"] == "LDJSON_NAME"


def test_malformed_json_ld_is_skipped(httpserver: HTTPServer) -> None:
    """A broken block must not raise, and must not become a half-record."""
    httpserver.expect_request("/j.html").respond_with_data(
        '<html><head><script type="application/ld+json">{"a":</script>'
        '<script type="application/ld+json">{"ok":1}</script></head>'
        "<body></body></html>",
        content_type="text/html",
    )
    blocks = onyxweb.fetch(httpserver.url_for("/j.html")).json_ld
    assert len(blocks) == 1
    assert blocks[0].data == {"ok": 1}


# --- scalars -----------------------------------------------------------------


def test_text_is_what_the_page_displays(bucket_page: str) -> None:
    text = onyxweb.fetch(bucket_page).text
    assert "VISIBLE_HEADING" in text
    assert "INLINE_JS_ONE" not in text
    assert "INLINE_CSS_ONE" not in text


def test_title_is_the_title_tag(bucket_page: str) -> None:
    assert onyxweb.fetch(bucket_page).title == "Bucket Fixture"


# --- bucket naming -----------------------------------------------------------


def test_unknown_bucket_names_the_valid_ones(bucket_page: str) -> None:
    with pytest.raises(ValueError, match="unknown bucket") as exc:
        onyxweb.fetch(bucket_page).dom.buckets.count("nope")
    assert "scripts" in str(exc.value)


# --- threads -----------------------------------------------------------------


def _touch_in_thread(
    r: onyxweb.RenderResult, touch: Callable[[onyxweb.RenderResult], object]
) -> None:
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
    """Results cross threads — one Client serves many — so no view may pin one."""
    r = onyxweb.fetch(bucket_page)
    _touch_in_thread(r, first)
    assert len(r.scripts) == 4
    assert r.title == "Bucket Fixture"
    assert len(r.content.scripts) == 2


# --- the result surface -------------------------------------------------------


def test_dom_is_only_the_selector_engine(bucket_page: str) -> None:
    """Whole-document extraction lives on the result and its buckets, not `dom`."""
    dom = onyxweb.fetch(bucket_page).dom
    for gone in ("text", "html", "title", "links", "images", "contains", "find_all_text"):
        assert not hasattr(dom, gone), gone
    for kept in ("query", "query_one", "count", "exists", "select", "select_one", "find"):
        assert hasattr(dom, kept), kept


def test_len_and_contains_build_no_dom(bucket_page: str) -> None:
    """Counting or scanning the raw capture needs no parse and no copy into a Dom."""
    r = onyxweb.fetch(bucket_page)
    assert len(r) > 0
    assert "VISIBLE_HEADING" in r
    assert "VISIBLE_HEADING".lower() not in r
    assert r._dom is None


def test_raw_access_without_a_capture() -> None:
    r = onyxweb.RenderResult("<p>hello</p>")
    assert "hello" in r
    assert "absent" not in r
    assert len(r) == len("<p>hello</p>")
