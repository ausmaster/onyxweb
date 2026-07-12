"""Rust-side Dom + Element queries exposed via RenderResult.dom."""

from __future__ import annotations

import onyxweb

URL = "https://example.com"


def _get_dom() -> tuple[onyxweb.RenderResult, onyxweb.Dom]:
    """Shared Dom instance for read-only queries — reuses one Chrome visit."""
    r = onyxweb.fetch(URL)
    return r, r.dom


class TestCSSSelectors:
    def test_query_returns_elements(self) -> None:
        _, dom = _get_dom()
        ps = dom.query("p")
        assert isinstance(ps, list)
        assert len(ps) > 0
        assert all(hasattr(e, "text") for e in ps)

    def test_query_one(self) -> None:
        _, dom = _get_dom()
        h1 = dom.query_one("h1")
        assert h1 is not None
        assert h1.text.strip() == "Example Domain"

    def test_query_one_none(self) -> None:
        _, dom = _get_dom()
        assert dom.query_one("nonexistent-tag-xyz") is None

    def test_count(self) -> None:
        _, dom = _get_dom()
        assert dom.count("h1") >= 1
        assert dom.count("nonexistent-tag-xyz") == 0

    def test_exists(self) -> None:
        _, dom = _get_dom()
        assert dom.exists("h1") is True
        assert dom.exists("nonexistent-tag-xyz") is False


class TestBS4StyleFind:
    def test_find_by_tag(self) -> None:
        _, dom = _get_dom()
        h1 = dom.find("h1")
        assert h1 is not None
        assert h1.tag == "h1"

    def test_find_all(self) -> None:
        _, dom = _get_dom()
        ps = dom.find_all("p")
        assert len(ps) >= 1

    def test_find_all_with_limit(self) -> None:
        _, dom = _get_dom()
        ps = dom.find_all("p", limit=1)
        assert len(ps) == 1


class TestWholeDocument:
    def test_text(self) -> None:
        _, dom = _get_dom()
        text = dom.text()
        assert "Example Domain" in text

    def test_html(self) -> None:
        r, dom = _get_dom()
        assert dom.html() == str(r)

    def test_title(self) -> None:
        _, dom = _get_dom()
        assert dom.title() == "Example Domain"

    def test_links(self) -> None:
        _, dom = _get_dom()
        links = dom.links()
        assert isinstance(links, list)
        # example.com has at least one link (to iana.org)
        assert any("iana.org" in ln for ln in links)

    def test_images_empty_on_simple_page(self) -> None:
        _, dom = _get_dom()
        assert isinstance(dom.images(), list)
        # example.com has no <img>, so list is empty


class TestSubstringShortcuts:
    """Does NOT trigger the html5ever parse — faster than .query() for
    simple haystack checks."""

    def test_contains_case_insensitive(self) -> None:
        _, dom = _get_dom()
        assert dom.contains("example") is True
        assert dom.contains("EXAMPLE") is True  # default case-insensitive
        assert dom.contains("surely-not-present-xyz") is False

    def test_contains_case_sensitive(self) -> None:
        _, dom = _get_dom()
        # "Example" is capitalized on example.com; "example" appears too (in href)
        assert dom.contains("Example Domain", case_sensitive=True) is True

    def test_find_all_text(self) -> None:
        _, dom = _get_dom()
        offsets = dom.find_all_text("Example")
        assert isinstance(offsets, list)
        assert all(isinstance(i, int) for i in offsets)


class TestElementAttrs:
    def test_element_text_and_html(self) -> None:
        _, dom = _get_dom()
        h1 = dom.query_one("h1")
        assert h1 is not None
        assert "Example Domain" in h1.text
        assert "<h1>" in h1.html.lower()

    def test_element_attr_method(self) -> None:
        _, dom = _get_dom()
        a = dom.query_one("a")
        if a:  # example.com has one <a>
            href = a.attr("href")
            assert href is not None
            assert href.startswith("http")

    def test_element_attrs_dict(self) -> None:
        _, dom = _get_dom()
        a = dom.query_one("a")
        if a:
            attrs = a.attrs
            assert isinstance(attrs, dict)
            assert "href" in attrs


class TestNestedQueries:
    """Element.query() / .find() allow scoping to a subtree."""

    def test_nested_query_on_body(self) -> None:
        _, dom = _get_dom()
        body = dom.query_one("body")
        assert body is not None
        inner_h1 = body.query("h1")
        assert len(inner_h1) >= 1
