"""Views — ``r.content`` and ``r.resources`` over the same buckets, filtered.

``content`` is what lives in the document itself; ``resources`` is what the
document points the browser at. Scripts, styles and iframes split across both;
every other bucket belongs to exactly one side. A view hands back lazy buckets,
so filtering costs nothing until records are read.
"""

from __future__ import annotations

import onyxweb
import pytest
from pytest_httpserver import HTTPServer

# --- the split buckets divide cleanly ----------------------------------------


@pytest.mark.parametrize("bucket", ["scripts", "styles", "iframes"])
def test_halves_sum_to_the_whole(bucket_page: str, bucket: str) -> None:
    r = onyxweb.fetch(bucket_page)
    whole = len(getattr(r, bucket))
    inline = len(getattr(r.content, bucket))
    external = len(getattr(r.resources, bucket))
    assert inline > 0
    assert external > 0
    assert inline + external == whole


def test_content_scripts_are_only_inline(bucket_page: str) -> None:
    scripts = list(onyxweb.fetch(bucket_page).content.scripts)
    assert {s.where for s in scripts} == {"inline"}
    assert any("INLINE_JS_ONE" in (s.text or "") for s in scripts)


def test_resource_scripts_are_only_external(bucket_page: str) -> None:
    scripts = list(onyxweb.fetch(bucket_page).resources.scripts)
    assert {s.where for s in scripts} == {"external"}
    assert {s.raw for s in scripts} == {"static/app.js", "/deep/vendor.js"}


def test_styles_and_iframes_filter_by_side(bucket_page: str) -> None:
    r = onyxweb.fetch(bucket_page)
    assert {s.where for s in r.content.styles} == {"inline"}
    assert {s.where for s in r.resources.styles} == {"external"}
    assert {f.where for f in r.content.iframes} == {"inline"}
    assert {f.where for f in r.resources.iframes} == {"external"}


# --- single-sided buckets ----------------------------------------------------


@pytest.mark.parametrize("bucket", ["comments", "forms", "meta", "json_ld"])
def test_content_holds_document_only_buckets_whole(bucket_page: str, bucket: str) -> None:
    r = onyxweb.fetch(bucket_page)
    assert len(getattr(r.content, bucket)) == len(getattr(r, bucket)) > 0


@pytest.mark.parametrize("bucket", ["images", "links"])
def test_resources_hold_url_only_buckets_whole(bucket_page: str, bucket: str) -> None:
    r = onyxweb.fetch(bucket_page)
    assert len(getattr(r.resources, bucket)) == len(getattr(r, bucket)) > 0


# --- asking the wrong view ---------------------------------------------------


def test_content_points_to_resources_for_images(bucket_page: str) -> None:
    content = onyxweb.fetch(bucket_page).content
    with pytest.raises(AttributeError, match=r"r\.resources\.images"):
        content.images  # type: ignore[attr-defined]  # noqa: B018


def test_resources_points_to_content_for_comments(bucket_page: str) -> None:
    resources = onyxweb.fetch(bucket_page).resources
    with pytest.raises(AttributeError, match=r"r\.content\.comments"):
        resources.comments  # type: ignore[attr-defined]  # noqa: B018


def test_unknown_attribute_is_a_plain_attribute_error(bucket_page: str) -> None:
    """Only real buckets get the redirect hint; a typo gets no false lead."""
    content = onyxweb.fetch(bucket_page).content
    with pytest.raises(AttributeError) as exc:
        content.nonsense  # type: ignore[attr-defined]  # noqa: B018
    assert "r.resources" not in str(exc.value)


# --- resources.all() ---------------------------------------------------------


def test_all_lists_fetched_resources_in_document_order(bucket_page: str) -> None:
    loaded = list(onyxweb.fetch(bucket_page).resources.all())
    assert [(res.kind, res.raw) for res in loaded] == [
        ("style", "site.css"),
        ("style", "/deep/print.css"),
        ("script", "static/app.js"),
        ("script", "/deep/vendor.js"),
        ("image", "pic.png"),
        ("iframe", "inner.html"),
    ]


def test_all_excludes_links(bucket_page: str) -> None:
    """A link is referenced, never fetched, so it is not a loaded resource."""
    loaded = onyxweb.fetch(bucket_page).resources.all()
    assert all(res.raw not in ("about.html", "/deep/faq.html") for res in loaded)


def test_all_resolves_urls(bucket_page: str) -> None:
    loaded = onyxweb.fetch(bucket_page).resources.all()
    assert all(res.url.startswith("http://") for res in loaded)


def test_all_excludes_an_iframe_whose_srcdoc_wins(httpserver: HTTPServer) -> None:
    """``srcdoc`` beats ``src`` — the browser never requests that ``src``."""
    httpserver.expect_request("/f.html").respond_with_data(
        '<html><body><iframe srcdoc="&lt;p&gt;x&lt;/p&gt;" src="unused.html"></iframe>'
        '<iframe src="used.html"></iframe></body></html>',
        content_type="text/html",
    )
    httpserver.expect_request("/used.html").respond_with_data("<p>y</p>", content_type="text/html")
    loaded = onyxweb.fetch(httpserver.url_for("/f.html")).resources.all()
    assert [res.raw for res in loaded] == ["used.html"]


# --- laziness and caching ----------------------------------------------------


def test_filtered_len_does_not_materialize(bucket_page: str) -> None:
    scripts = onyxweb.fetch(bucket_page).content.scripts
    assert len(scripts) == 2
    assert scripts._records is None


def test_view_buckets_are_cached(bucket_page: str) -> None:
    r = onyxweb.fetch(bucket_page)
    assert r.content is r.content
    assert r.content.scripts is r.content.scripts
    assert r.resources.all() is r.resources.all()


# --- the Rust filter rejects nonsense ----------------------------------------


def test_side_filter_rejects_an_unsplit_bucket(bucket_page: str) -> None:
    rust = onyxweb.fetch(bucket_page).dom.buckets
    with pytest.raises(ValueError, match="no inline/external split"):
        rust.count("links", "inline")


def test_side_filter_rejects_an_unknown_side(bucket_page: str) -> None:
    rust = onyxweb.fetch(bucket_page).dom.buckets
    with pytest.raises(ValueError, match="inline.*external"):
        rust.count("scripts", "sideways")
