"""Display — see what a page holds before pulling anything out of it.

``r.overview()`` sizes every bucket from the parse without building a record;
``repr(r)`` is the one-line version. A bucket's ``repr`` is a head-truncated
table. Every one of these answers from Rust, so viewing a large page is cheap.
"""

from __future__ import annotations

import onyxweb
import pytest
from onyxweb.records import HEAD_ROWS, Overview, size_str
from pytest_httpserver import HTTPServer

# --- the overview's numbers ---------------------------------------------------


def test_overview_counts_every_bucket_and_side(bucket_page: str) -> None:
    ov = onyxweb.fetch(bucket_page).overview()
    assert isinstance(ov, Overview)
    assert {(row.bucket, row.where): row.count for row in ov.rows} == {
        ("scripts", "inline"): 2,
        ("scripts", "external"): 2,
        ("styles", "inline"): 2,
        ("styles", "external"): 2,
        ("iframes", "inline"): 1,
        ("iframes", "external"): 1,
        ("comments", None): 2,
        ("forms", None): 1,
        ("meta", None): 2,
        ("json_ld", None): 1,
        ("links", None): 2,
        ("images", None): 1,
    }


def test_overview_counts_agree_with_the_buckets(bucket_page: str) -> None:
    """The overview and the buckets are two readings of one parse; they must match."""
    r = onyxweb.fetch(bucket_page)
    views = {"inline": r.content, "external": r.resources}
    for row in r.overview().rows:
        view = r if row.where is None else views[row.where]
        assert len(getattr(view, row.bucket)) == row.count, row


def test_inline_bytes_match_the_records(bucket_page: str) -> None:
    r = onyxweb.fetch(bucket_page)
    sizes = {(row.bucket, row.where): row.size for row in r.overview().rows}
    assert sizes[("scripts", "inline")] == sum(
        len((s.text or "").encode()) for s in r.content.scripts
    )
    assert sizes[("comments", None)] == sum(len(c.text.encode()) for c in r.comments)
    assert sizes[("scripts", "external")] is None
    assert sizes[("links", None)] is None


def test_overview_totals(bucket_page: str) -> None:
    r = onyxweb.fetch(bucket_page)
    ov = r.overview()
    assert ov.text_size == len(r.text.encode())
    assert ov.total_size == len(r.html.encode())


def test_overview_builds_no_records(bucket_page: str) -> None:
    r = onyxweb.fetch(bucket_page)
    r.overview()
    assert r.scripts._records is None
    assert r.links._records is None


# --- the overview's rendering ------------------------------------------------


def test_overview_repr_is_the_table(bucket_page: str) -> None:
    table = repr(onyxweb.fetch(bucket_page).overview())
    assert table.splitlines()[0].split() == ["bucket", "where", "n", "size"]
    assert "json_ld" in table
    assert table.splitlines()[-1].startswith("total")


def test_overview_prnt_prints_and_returns_none(
    bucket_page: str, capfd: pytest.CaptureFixture[str]
) -> None:
    r = onyxweb.fetch(bucket_page)
    assert r.overview(prnt=True) is None
    out, _ = capfd.readouterr()
    assert out.rstrip("\n") == repr(r.overview())


# --- RenderResult repr --------------------------------------------------------


def test_result_repr_is_one_orienting_line(bucket_page: str) -> None:
    line = repr(onyxweb.fetch(bucket_page))
    assert "\n" not in line
    assert line.startswith("<RenderResult ")
    assert "4 scripts" in line
    assert "2 links" in line
    assert len(line) < 200


def test_result_repr_without_a_capture_does_not_raise() -> None:
    assert repr(onyxweb.RenderResult("<p>hi</p>")).startswith("<RenderResult ")


# --- bucket tables ------------------------------------------------------------


def test_split_bucket_header_shows_both_halves(bucket_page: str) -> None:
    assert repr(onyxweb.fetch(bucket_page).scripts).splitlines()[0] == (
        "Scripts · 4 (2 inline, 2 external)"
    )


def test_filtered_bucket_header_names_its_side(bucket_page: str) -> None:
    assert repr(onyxweb.fetch(bucket_page).content.scripts).splitlines()[0] == "Scripts · 2 inline"


def test_unsplit_bucket_table_has_no_where_column(bucket_page: str) -> None:
    table = repr(onyxweb.fetch(bucket_page).links)
    assert table.splitlines()[0] == "Links · 2"
    assert "where" not in table


def test_loaded_table_labels_its_column_kind(bucket_page: str) -> None:
    header = repr(onyxweb.fetch(bucket_page).resources.all()).splitlines()[1]
    assert header.split()[:2] == ["#", "kind"]


def test_long_bucket_shows_head_then_remainder(httpserver: HTTPServer) -> None:
    anchors = "".join(f"<a href='p{i}.html'>p{i}</a>" for i in range(HEAD_ROWS + 5))
    httpserver.expect_request("/many.html").respond_with_data(
        f"<html><body>{anchors}</body></html>", content_type="text/html"
    )
    lines = repr(onyxweb.fetch(httpserver.url_for("/many.html")).links).splitlines()
    assert len(lines) == 2 + HEAD_ROWS + 1  # title, column header, rows, remainder
    assert lines[-1].startswith("… 5 more")


def test_empty_bucket_says_so(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/bare.html").respond_with_data(
        "<html><body><p>nothing here</p></body></html>", content_type="text/html"
    )
    assert repr(onyxweb.fetch(httpserver.url_for("/bare.html")).links) == "Links · empty"


# --- sizes ------------------------------------------------------------------


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


# --- previews that tell rows apart -------------------------------------------


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


def test_form_preview_counts_fields(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/forms.html").respond_with_data(
        "<html><body><form><input name='a'></form>"
        "<form><input name='a'><input name='b'><input type='hidden' name='c'></form>"
        "</body></html>",
        content_type="text/html",
    )
    rows = repr(onyxweb.fetch(httpserver.url_for("/forms.html")).forms).splitlines()[2:]
    assert "1 field" in rows[0]
    assert "3 fields" in rows[1]
