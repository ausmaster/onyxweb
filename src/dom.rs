//! Rust-side HTML query — exposed as Python `Dom` and `Element` pyclasses.
//!
//! Backed by `scraper` (html5ever + selectors). **Lazy parsing**: we hold the
//! source HTML string and only parse when the user actually queries.
//!
//! Element is a **by-value snapshot** — no back-reference to the source DOM.
//! This sidesteps lifetime nightmares (scraper's ElementRef borrows from Html)
//! and keeps Python code safe: Elements can outlive their Dom freely.

use std::cell::RefCell;

use pyo3::prelude::*;
use pyo3::types::PyDict;
use scraper::{ElementRef, Html, Selector};

// ----------------------------------------------------------------------------
// Dom pyclass
// ----------------------------------------------------------------------------

#[pyclass(unsendable)]
pub struct Dom {
    html: String,
    parsed: RefCell<Option<Html>>,
}

impl Dom {
    pub fn from_html(html: String) -> Self {
        Self {
            html,
            parsed: RefCell::new(None),
        }
    }

    /// Parse on first access; re-use the cached Html afterwards.
    fn with_parsed<R>(&self, f: impl FnOnce(&Html) -> R) -> R {
        {
            let borrowed = self.parsed.borrow();
            if let Some(h) = borrowed.as_ref() {
                return f(h);
            }
        }
        let t0 = std::time::Instant::now();
        let parsed = Html::parse_document(&self.html);
        log::trace!(
            target: "onyxweb::dom",
            "parsed {} bytes in {:?}",
            self.html.len(),
            t0.elapsed()
        );
        let r = f(&parsed);
        *self.parsed.borrow_mut() = Some(parsed);
        r
    }
}

#[pymethods]
impl Dom {
    // --- CSS-selector primitive ---------------------------------------------

    /// Run a CSS selector; return list of matching Elements.
    fn query(&self, selector: &str) -> PyResult<Vec<Element>> {
        let sel = parse_selector(selector)?;
        Ok(self.with_parsed(|h| h.select(&sel).map(Element::from_ref).collect()))
    }

    /// First match, or None.
    fn query_one(&self, selector: &str) -> PyResult<Option<Element>> {
        let sel = parse_selector(selector)?;
        Ok(self.with_parsed(|h| h.select(&sel).next().map(Element::from_ref)))
    }

    /// Count of matches.
    fn count(&self, selector: &str) -> PyResult<usize> {
        let sel = parse_selector(selector)?;
        Ok(self.with_parsed(|h| h.select(&sel).count()))
    }

    /// True iff at least one element matches. Stops at the first match.
    fn exists(&self, selector: &str) -> PyResult<bool> {
        let sel = parse_selector(selector)?;
        Ok(self.with_parsed(|h| h.select(&sel).next().is_some()))
    }

    // --- BS4-familiar aliases ------------------------------------------------

    fn select(&self, selector: &str) -> PyResult<Vec<Element>> {
        self.query(selector)
    }

    fn select_one(&self, selector: &str) -> PyResult<Option<Element>> {
        self.query_one(selector)
    }

    /// BS4-style: ``find("div", class_="content", id="main", **attrs)``.
    #[pyo3(signature = (tag=None, *, class_=None, id=None, **attrs))]
    fn find(
        &self,
        tag: Option<&str>,
        class_: Option<&str>,
        id: Option<&str>,
        attrs: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<Option<Element>> {
        let selector = build_selector(tag, class_, id, attrs)?;
        self.query_one(&selector)
    }

    #[pyo3(signature = (tag=None, *, class_=None, id=None, limit=None, **attrs))]
    fn find_all(
        &self,
        tag: Option<&str>,
        class_: Option<&str>,
        id: Option<&str>,
        limit: Option<usize>,
        attrs: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<Vec<Element>> {
        let selector = build_selector(tag, class_, id, attrs)?;
        let mut results = self.query(&selector)?;
        if let Some(l) = limit {
            results.truncate(l);
        }
        Ok(results)
    }

    // --- Whole-document text/html --------------------------------------------

    /// All textContent, scripts/styles stripped.
    fn text(&self) -> String {
        self.with_parsed(|h| {
            let body_sel = Selector::parse("body").ok();
            let root = body_sel
                .as_ref()
                .and_then(|s| h.select(s).next())
                .unwrap_or_else(|| h.root_element());
            collect_text(root)
        })
    }

    /// Serialized HTML — same as `str(result)`.
    fn html(&self) -> String {
        self.html.clone()
    }

    // --- Fast substring (no parse) ------------------------------------------

    /// Fast substring check. Does NOT trigger the HTML parser.
    #[pyo3(signature = (needle, *, case_sensitive=false))]
    fn contains(&self, needle: &str, case_sensitive: bool) -> bool {
        if case_sensitive {
            self.html.contains(needle)
        } else {
            self.html.to_lowercase().contains(&needle.to_lowercase())
        }
    }

    /// Byte offsets of every occurrence of `needle`. Does NOT trigger parse.
    #[pyo3(signature = (needle, *, case_sensitive=false))]
    fn find_all_text(&self, needle: &str, case_sensitive: bool) -> Vec<usize> {
        if needle.is_empty() {
            return Vec::new();
        }
        let (haystack, needle) = if case_sensitive {
            (self.html.clone(), needle.to_string())
        } else {
            (self.html.to_lowercase(), needle.to_lowercase())
        };
        haystack.match_indices(&needle).map(|(i, _)| i).collect()
    }

    // --- Common shortcuts ----------------------------------------------------

    /// All `<a href>` values, in document order.
    fn links(&self) -> PyResult<Vec<String>> {
        let sel = parse_selector("a[href]")?;
        Ok(self.with_parsed(|h| {
            h.select(&sel)
                .filter_map(|e| e.value().attr("href").map(str::to_string))
                .collect()
        }))
    }

    /// All `<img src>` values.
    fn images(&self) -> PyResult<Vec<String>> {
        let sel = parse_selector("img[src]")?;
        Ok(self.with_parsed(|h| {
            h.select(&sel)
                .filter_map(|e| e.value().attr("src").map(str::to_string))
                .collect()
        }))
    }

    /// The `<title>` text, if any.
    fn title(&self) -> PyResult<Option<String>> {
        let sel = parse_selector("title")?;
        Ok(self.with_parsed(|h| h.select(&sel).next().map(|e| collect_text(e))))
    }
}

// ----------------------------------------------------------------------------
// Element pyclass (by-value snapshot)
// ----------------------------------------------------------------------------

#[pyclass]
#[derive(Clone)]
pub struct Element {
    #[pyo3(get)]
    pub tag: String,
    #[pyo3(get)]
    pub text: String,
    #[pyo3(get)]
    pub html: String,
    #[pyo3(get)]
    pub inner_html: String,
    /// Serialized subtree HTML — used for nested queries without re-parsing the whole doc.
    outer_html_for_reparse: String,
}

impl Element {
    fn from_ref(e: ElementRef<'_>) -> Self {
        let val = e.value();
        let tag = val.name().to_string();
        let outer = e.html();
        let inner = e.inner_html();
        let text = collect_text(e);
        Self {
            tag,
            text,
            html: outer.clone(),
            inner_html: inner,
            outer_html_for_reparse: outer,
        }
    }
}

#[pymethods]
impl Element {
    #[getter]
    fn attrs<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let d = PyDict::new(py);
        // Parse the outer HTML to extract attrs — we stored outer so we can regenerate.
        // A fragment parse is cheap for a single element's HTML.
        let fragment = Html::parse_fragment(&self.outer_html_for_reparse);
        if let Some(first) = fragment.root_element().children().next()
            && let Some(el) = ElementRef::wrap(first)
        {
            for (k, v) in el.value().attrs() {
                d.set_item(k, v)?;
            }
        }
        Ok(d)
    }

    fn attr(&self, name: &str) -> Option<String> {
        let fragment = Html::parse_fragment(&self.outer_html_for_reparse);
        fragment
            .root_element()
            .children()
            .next()
            .and_then(ElementRef::wrap)
            .and_then(|e| e.value().attr(name).map(str::to_string))
    }

    /// Nested query: parse this element's outer HTML as a fragment and run the selector.
    fn query(&self, selector: &str) -> PyResult<Vec<Element>> {
        let sel = parse_selector(selector)?;
        let fragment = Html::parse_fragment(&self.outer_html_for_reparse);
        Ok(fragment.select(&sel).map(Element::from_ref).collect())
    }

    fn query_one(&self, selector: &str) -> PyResult<Option<Element>> {
        let sel = parse_selector(selector)?;
        let fragment = Html::parse_fragment(&self.outer_html_for_reparse);
        Ok(fragment.select(&sel).next().map(Element::from_ref))
    }

    #[pyo3(signature = (tag=None, *, class_=None, id=None, **attrs))]
    fn find(
        &self,
        tag: Option<&str>,
        class_: Option<&str>,
        id: Option<&str>,
        attrs: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<Option<Element>> {
        let selector = build_selector(tag, class_, id, attrs)?;
        self.query_one(&selector)
    }

    #[pyo3(signature = (tag=None, *, class_=None, id=None, **attrs))]
    fn find_all(
        &self,
        tag: Option<&str>,
        class_: Option<&str>,
        id: Option<&str>,
        attrs: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<Vec<Element>> {
        let selector = build_selector(tag, class_, id, attrs)?;
        self.query(&selector)
    }

    fn __repr__(&self) -> String {
        let short = if self.text.len() > 60 {
            format!("{}…", &self.text[..60])
        } else {
            self.text.clone()
        };
        format!("<Element tag={:?} text={:?}>", self.tag, short)
    }
}

// ----------------------------------------------------------------------------
// Helpers
// ----------------------------------------------------------------------------

fn parse_selector(s: &str) -> PyResult<Selector> {
    Selector::parse(s).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!("bad CSS selector {s:?}: {e:?}"))
    })
}

fn collect_text(e: ElementRef<'_>) -> String {
    let mut out = String::new();
    for chunk in e.text() {
        out.push_str(chunk);
    }
    out
}

/// Translate BS4-style kwargs into a single CSS selector.
/// e.g. tag="div", class_="content", id="main", attrs={"data-x": "1"}
///      → `div.content#main[data-x="1"]`
fn build_selector(
    tag: Option<&str>,
    class_: Option<&str>,
    id: Option<&str>,
    attrs: Option<&Bound<'_, PyDict>>,
) -> PyResult<String> {
    let mut s = String::new();
    if let Some(t) = tag {
        s.push_str(t);
    } else {
        s.push('*');
    }
    if let Some(c) = class_ {
        // allow space-separated classes (BS4 allows this via `class_="a b"`)
        for cls in c.split_whitespace() {
            s.push('.');
            s.push_str(cls);
        }
    }
    if let Some(i) = id {
        s.push('#');
        s.push_str(i);
    }
    if let Some(d) = attrs {
        for (k, v) in d.iter() {
            let key: String = k.extract()?;
            let val: String = v.extract()?;
            s.push_str(&format!(r#"[{}="{}"]"#, key, val.replace('"', "\\\"")));
        }
    }
    Ok(s)
}
