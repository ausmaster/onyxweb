"""Rust-side Dom + Element queries exposed via RenderResult.dom."""

from __future__ import annotations

import onyxweb

URL = "https://example.com"


def _get_dom() -> onyxweb.Dom:
    """Shared Dom instance for read-only queries — reuses one Chrome visit."""
    return onyxweb.fetch(URL).dom


class TestCSSSelectors:
    def test_query_returns_elements(self) -> None:
        dom = _get_dom()
        ps = dom.query("p")
        assert isinstance(ps, list)
        assert len(ps) > 0
        assert all(hasattr(e, "text") for e in ps)

    def test_query_one(self) -> None:
        dom = _get_dom()
        h1 = dom.query_one("h1")
        assert h1 is not None
        assert h1.text.strip() == "Example Domain"

    def test_query_one_none(self) -> None:
        dom = _get_dom()
        assert dom.query_one("nonexistent-tag-xyz") is None

    def test_count(self) -> None:
        dom = _get_dom()
        assert dom.count("h1") >= 1
        assert dom.count("nonexistent-tag-xyz") == 0

    def test_exists(self) -> None:
        dom = _get_dom()
        assert dom.exists("h1") is True
        assert dom.exists("nonexistent-tag-xyz") is False


class TestBS4StyleFind:
    def test_find_by_tag(self) -> None:
        dom = _get_dom()
        h1 = dom.find("h1")
        assert h1 is not None
        assert h1.tag == "h1"

    def test_find_all(self) -> None:
        dom = _get_dom()
        ps = dom.find_all("p")
        assert len(ps) >= 1

    def test_find_all_with_limit(self) -> None:
        dom = _get_dom()
        ps = dom.find_all("p", limit=1)
        assert len(ps) == 1


class TestElementAttrs:
    def test_element_text_and_html(self) -> None:
        dom = _get_dom()
        h1 = dom.query_one("h1")
        assert h1 is not None
        assert "Example Domain" in h1.text
        assert "<h1>" in h1.html.lower()

    def test_element_attr_method(self) -> None:
        dom = _get_dom()
        a = dom.query_one("a")
        if a:  # example.com has one <a>
            href = a.attr("href")
            assert href is not None
            assert href.startswith("http")

    def test_element_attrs_dict(self) -> None:
        dom = _get_dom()
        a = dom.query_one("a")
        if a:
            attrs = a.attrs
            assert isinstance(attrs, dict)
            assert "href" in attrs


class TestNestedQueries:
    """Element.query() / .find() allow scoping to a subtree."""

    def test_nested_query_on_body(self) -> None:
        dom = _get_dom()
        body = dom.query_one("body")
        assert body is not None
        inner_h1 = body.query("h1")
        assert len(inner_h1) >= 1
