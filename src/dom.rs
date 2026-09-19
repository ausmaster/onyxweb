//! Rust-side HTML query — exposed as Python `Dom`, `Buckets` and `Element`.
//!
//! Backed by `scraper` (html5ever + selectors). **Lazy parsing**: we hold the
//! source HTML string and only parse when the user actually queries.
//!
//! `Dom` (CSS selection) and `Buckets` (the page sorted into categories) share
//! one `DomCore`, so however many views a result exposes it parses once. The
//! core is `Send + Sync` — results cross Python threads, since one Client serves
//! many — so the lazy parse sits behind a mutex rather than a `RefCell`.
//!
//! Element is a **by-value snapshot** — no back-reference to the source DOM.
//! This sidesteps lifetime nightmares (scraper's ElementRef borrows from Html)
//! and keeps Python code safe: Elements can outlive their Dom freely.

use std::collections::{HashMap, HashSet};
use std::sync::Arc;

use parking_lot::Mutex;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyList, PyTuple};
use regex::{Regex, RegexBuilder};
use scraper::{ElementRef, Html, Node, Selector};
use serde_json::Value;
use url::Url;

/// Buckets a caller may name. Extend alongside the match arms below.
const BUCKET_NAMES: &[&str] = &[
    "scripts", "styles", "links", "images", "iframes", "forms", "meta", "comments", "json_ld",
    "loaded",
];

/// Buckets whose records split into inline and external halves.
const SPLIT_BUCKETS: &[&str] = &["scripts", "styles", "iframes"];

// ----------------------------------------------------------------------------
// Shared parse core
// ----------------------------------------------------------------------------

/// The parsed document plus the base every relative URL resolves against.
struct Parsed {
    html: Html,
    base: Option<Url>,
}

/// Source HTML + its lazy parse, shared by every view over one result.
struct DomCore {
    source: String,
    doc_url: Option<String>,
    // `Html` is Send but not Sync, so a Mutex (not OnceLock) makes the core Sync.
    parsed: Mutex<Option<Parsed>>,
}

impl DomCore {
    /// Parse on first access; re-use the cached document afterwards.
    ///
    /// `f` runs under the lock, which is not reentrant: never call `with_parsed`
    /// from inside `f`.
    fn with_parsed<R>(&self, f: impl FnOnce(&Parsed) -> R) -> R {
        let mut guard = self.parsed.lock();
        let parsed = guard.get_or_insert_with(|| {
            let t0 = std::time::Instant::now();
            let html = Html::parse_document(&self.source);
            let base = effective_base(&html, self.doc_url.as_deref());
            log::trace!(
                target: "onyxweb::dom",
                "parsed {} bytes in {:?}",
                self.source.len(),
                t0.elapsed()
            );
            Parsed { html, base }
        });
        f(parsed)
    }
}

/// `<base href>` resolved against the document URL, else the document URL.
/// The first `<base href>` wins, per the HTML spec.
fn effective_base(html: &Html, doc_url: Option<&str>) -> Option<Url> {
    let doc = doc_url.and_then(|u| Url::parse(u).ok());
    let Ok(sel) = Selector::parse("base[href]") else {
        return doc;
    };
    let Some(href) = html
        .select(&sel)
        .next()
        .and_then(|e| e.value().attr("href"))
    else {
        return doc;
    };
    match doc {
        Some(d) => d.join(href).ok().or(Some(d)),
        None => Url::parse(href).ok(),
    }
}

/// `(absolute, as-authored)` for one URL attribute. Both fall back to the raw
/// value when there is no base or the join fails — a `data:` document is
/// cannot-be-a-base, so nothing relative to it resolves.
fn url_record(base: Option<&Url>, raw: &str) -> (String, String) {
    let resolved = base
        .and_then(|b| b.join(raw).ok())
        .map(String::from)
        .unwrap_or_else(|| raw.to_string());
    (resolved, raw.to_string())
}

/// True when a `src` makes the browser fetch something. An empty value or an
/// `about:` URL loads nothing, so it names no resource.
fn fetches(raw: &str) -> bool {
    let raw = raw.trim();
    !raw.is_empty()
        && !raw
            .get(..6)
            .is_some_and(|s| s.eq_ignore_ascii_case("about:"))
}

fn unknown_bucket(name: &str) -> PyErr {
    pyo3::exceptions::PyValueError::new_err(format!(
        "unknown bucket {name:?}; expected one of {}",
        BUCKET_NAMES.join(", ")
    ))
}

// ----------------------------------------------------------------------------
// Dom pyclass
// ----------------------------------------------------------------------------

#[pyclass]
pub struct Dom {
    core: Arc<DomCore>,
}

impl Dom {
    /// Build over `html`, resolving relative URLs against `doc_url`.
    pub fn new(html: String, doc_url: Option<String>) -> Self {
        Self {
            core: Arc::new(DomCore {
                source: html,
                doc_url,
                parsed: Mutex::new(None),
            }),
        }
    }

    fn with_parsed<R>(&self, f: impl FnOnce(&Html) -> R) -> R {
        self.core.with_parsed(|p| f(&p.html))
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

    /// The page sorted into categories, over this same parse.
    #[getter]
    fn buckets(&self) -> Buckets {
        Buckets {
            core: self.core.clone(),
        }
    }
}

// ----------------------------------------------------------------------------
// Buckets pyclass — the page sorted into categories
// ----------------------------------------------------------------------------

/// `type` marking a script as data rather than code; it belongs to `json_ld`.
const LD_JSON: &str = "application/ld+json";
/// Inline `<style>` plus stylesheet `<link>`, matched together so the bucket
/// comes back in document order.
const STYLE_SELECTOR: &str = r#"style, link[rel~="stylesheet"]"#;
/// Fields a form submits, in document order.
const FIELD_SELECTOR: &str = "input, select, textarea";
/// Every element that can make the browser fetch something, in document order.
const LOADED_SELECTOR: &str =
    r#"script[src], link[rel~="stylesheet"][href], img[src], iframe[src]"#;
/// Record fields holding onyxweb's own labels rather than page bytes. A search
/// without `field` skips them, so `search("external")` means page text.
const LABEL_FIELDS: &[&str] = &["where", "kind"];

/// One search term as Python sends it: `(needle, field, case_sensitive, regex)`.
type Query = (String, Option<String>, bool, bool);
/// `(bucket, where, count, size)` — one line of the overview.
type CountRow = (&'static str, Option<&'static str>, usize, Option<usize>);

/// Category views over one parsed document, named by string so the Python side
/// exposes one lazy handle per bucket without a pyclass each.
///
/// `count` and `head` size or preview a bucket — searched or not — without
/// handing Python every record: the reason the layer exists.
#[pyclass]
pub struct Buckets {
    core: Arc<DomCore>,
}

#[pymethods]
impl Buckets {
    /// Every record in `bucket` matching every query, keyed to the Python
    /// record's fields. `where_` keeps one half of a split bucket.
    #[pyo3(signature = (bucket, where_=None, queries=None))]
    fn records(
        &self,
        py: Python<'_>,
        bucket: &str,
        where_: Option<&str>,
        queries: Option<Vec<Query>>,
    ) -> PyResult<Vec<PyObject>> {
        check_request(bucket, where_)?;
        let matchers = compile(queries)?;
        self.core.with_parsed(|p| {
            let mut out = Vec::new();
            for item in items(p, bucket, where_)? {
                let record = item.record(p);
                if matches_all(&matchers, &record) {
                    out.push(json_to_py(py, &record)?);
                }
            }
            Ok(out)
        })
    }

    /// How many records in `bucket` match every query. Unsearched it builds no
    /// record; searched it builds each only to test it, never for Python.
    #[pyo3(signature = (bucket, where_=None, queries=None))]
    fn count(
        &self,
        bucket: &str,
        where_: Option<&str>,
        queries: Option<Vec<Query>>,
    ) -> PyResult<usize> {
        check_request(bucket, where_)?;
        let matchers = compile(queries)?;
        self.core.with_parsed(|p| {
            let members = items(p, bucket, where_)?;
            if matchers.is_empty() {
                return Ok(members.len());
            }
            Ok(members
                .iter()
                .filter(|item| matches_all(&matchers, &item.record(p)))
                .count())
        })
    }

    /// What the page displays: text under `<body>` (the whole document when there
    /// is none), with script, style, noscript and template content left out.
    fn text(&self) -> String {
        self.core.with_parsed(|p| {
            let body = Selector::parse("body").ok();
            let root = body
                .as_ref()
                .and_then(|s| p.html.select(s).next())
                .unwrap_or_else(|| p.html.root_element());
            collect_text(root)
        })
    }

    /// The first `<title>`'s text, or `None` when the page has none.
    fn title(&self) -> PyResult<Option<String>> {
        let sel = parse_selector("title")?;
        Ok(self
            .core
            .with_parsed(|p| p.html.select(&sel).next().map(collect_text)))
    }

    /// Every bucket sized from the parse, building no record: display-ordered
    /// `(bucket, where, count, size)` rows, then visible-text and whole-document
    /// byte counts. `size` is `None` where the bytes aren't in the document.
    fn counts(&self) -> PyResult<(Vec<CountRow>, usize, usize)> {
        self.core.with_parsed(|p| {
            let mut rows = Vec::new();
            for bucket in SPLIT_BUCKETS {
                let members = items(p, bucket, None)?;
                // Only a frame can be blank; the other buckets always pick a side.
                let sides: &[&'static str] = match *bucket {
                    "iframes" => &["inline", "external", "blank"],
                    _ => &["inline", "external"],
                };
                for side in sides {
                    let (count, bytes) = members
                        .iter()
                        .filter(|m| m.side() == Some(*side))
                        .fold((0, 0), |(n, b), m| {
                            (n + 1, b + m.inline_size().unwrap_or(0))
                        });
                    rows.push((
                        *bucket,
                        Some(*side),
                        count,
                        (*side == "inline").then_some(bytes),
                    ));
                }
            }
            for bucket in ["comments", "forms", "meta", "json_ld", "links", "images"] {
                let members = items(p, bucket, None)?;
                let size = matches!(bucket, "comments" | "json_ld")
                    .then(|| members.iter().filter_map(Item::inline_size).sum());
                rows.push((bucket, None, members.len(), size));
            }
            let body = Selector::parse("body").ok();
            let root = body
                .as_ref()
                .and_then(|s| p.html.select(s).next())
                .unwrap_or_else(|| p.html.root_element());
            Ok((rows, collect_text(root).len(), self.core.source.len()))
        })
    }

    /// First `n` table rows of the matching records, each
    /// `{index, where, size, preview, count}` with the preview clipped to `width`
    /// characters — the table renderer's input.
    ///
    /// Identical comments share one row whose `count` says how many there are, so
    /// a build tool's marker repeated 300 times can't fill the table. When the
    /// bucket is searched and the hit lies in a string too long to show whole,
    /// the preview frames the hit rather than the string's start.
    #[pyo3(signature = (bucket, n, width, where_=None, queries=None))]
    fn head(
        &self,
        py: Python<'_>,
        bucket: &str,
        n: usize,
        width: usize,
        where_: Option<&str>,
        queries: Option<Vec<Query>>,
    ) -> PyResult<Vec<PyObject>> {
        check_request(bucket, where_)?;
        let matchers = compile(queries)?;
        self.core.with_parsed(|p| {
            let mut rows: Vec<Row> = Vec::new();
            let mut seen: HashMap<&str, usize> = HashMap::new();
            let mut index = 0;
            for item in items(p, bucket, where_)? {
                let record = (!matchers.is_empty()).then(|| item.record(p));
                if record.as_ref().is_some_and(|r| !matches_all(&matchers, r)) {
                    continue;
                }
                let at = index;
                index += 1;
                if let Item::Comment(text) = &item {
                    if let Some(&row) = seen.get(text) {
                        rows[row].count += 1;
                        continue;
                    }
                    // Past the head a new comment is never shown, but its repeats
                    // still walk by, so keep going rather than break.
                    if rows.len() >= n {
                        continue;
                    }
                    seen.insert(text, rows.len());
                } else if rows.len() >= n {
                    break;
                }
                let (side, size, mut preview) = item.row(p, width);
                if let Some(window) = record.and_then(|r| match_window(&matchers, &r, width)) {
                    preview = window;
                }
                rows.push(Row {
                    index: at,
                    side,
                    size,
                    preview,
                    count: 1,
                });
            }
            rows.into_iter().map(|row| table_row(py, row)).collect()
        })
    }

    /// Every match of `query` inside the records of `bucket` that match every
    /// `queries` term, in record order.
    ///
    /// Each is `{index, field, text, groups, start, end}`: `index` is the record's
    /// position in the searched bucket, `field` the top-level record field holding
    /// the match, and `start`/`end` character offsets into the matched string.
    /// With `width`, each is instead a table row whose preview frames the match.
    /// A text an earlier field of the same record already matched is skipped:
    /// `url`, `raw` and a `src` attribute repeat one address, which is one place.
    #[pyo3(signature = (bucket, query, where_=None, queries=None, width=None))]
    fn matches(
        &self,
        py: Python<'_>,
        bucket: &str,
        query: Query,
        where_: Option<&str>,
        queries: Option<Vec<Query>>,
        width: Option<usize>,
    ) -> PyResult<Vec<PyObject>> {
        check_request(bucket, where_)?;
        let matchers = compile(queries)?;
        let Some(target) = compile(Some(vec![query]))?.pop() else {
            return Ok(Vec::new());
        };
        self.core.with_parsed(|p| {
            let mut out = Vec::new();
            let mut index = 0;
            for item in items(p, bucket, where_)? {
                let record = item.record(p);
                if !matches_all(&matchers, &record) {
                    continue;
                }
                let at = index;
                index += 1;
                let hits = hits_in(&target, &record, width);
                let row = match (width, hits.is_empty()) {
                    (Some(w), false) => Some(item.row(p, w)),
                    _ => None,
                };
                for hit in hits {
                    let obj = match &row {
                        Some((side, size, _)) => table_row(
                            py,
                            Row {
                                index: at,
                                side,
                                size: *size,
                                preview: hit.window.unwrap_or_default(),
                                count: 1,
                            },
                        )?,
                        None => {
                            let d = PyDict::new(py);
                            d.set_item("index", at)?;
                            d.set_item("field", hit.field)?;
                            d.set_item("text", hit.text)?;
                            d.set_item("groups", PyTuple::new(py, hit.groups)?)?;
                            d.set_item("start", hit.start)?;
                            d.set_item("end", hit.end)?;
                            d.into_any().unbind()
                        }
                    };
                    out.push(obj);
                }
            }
            Ok(out)
        })
    }
}

/// Reject an unknown bucket or a side filter it can't honour, before any walk.
fn check_request(bucket: &str, where_: Option<&str>) -> PyResult<()> {
    if !BUCKET_NAMES.contains(&bucket) {
        return Err(unknown_bucket(bucket));
    }
    let Some(w) = where_ else {
        return Ok(());
    };
    if !matches!(w, "inline" | "external") {
        return Err(pyo3::exceptions::PyValueError::new_err(format!(
            "where must be \"inline\" or \"external\", got {w:?}"
        )));
    }
    if !SPLIT_BUCKETS.contains(&bucket) {
        return Err(pyo3::exceptions::PyValueError::new_err(format!(
            "bucket {bucket:?} has no inline/external split; omit where"
        )));
    }
    Ok(())
}

/// Which side of a split bucket `e` falls on, or `None` when the bucket
/// excludes it. `srcdoc` beats `src` — the browser renders it and never
/// requests the `src`. A frame with neither a `srcdoc` nor a `src` that fetches
/// is `"blank"`: it holds nothing and loads nothing.
fn side_of(bucket: &str, e: &ElementRef<'_>) -> Option<&'static str> {
    let v = e.value();
    match bucket {
        "scripts" if is_ld_json(e) => None,
        "scripts" if v.attr("src").is_some() => Some("external"),
        "scripts" => Some("inline"),
        "styles" if v.name() == "style" => Some("inline"),
        "styles" => v.attr("href").map(|_| "external"),
        "iframes" if v.attr("srcdoc").is_some() => Some("inline"),
        "iframes" if v.attr("src").is_some_and(fetches) => Some("external"),
        "iframes" => Some("blank"),
        _ => None,
    }
}

/// True when a `<script>` carries JSON-LD data rather than code.
fn is_ld_json(e: &ElementRef<'_>) -> bool {
    e.value()
        .attr("type")
        .is_some_and(|t| t.eq_ignore_ascii_case(LD_JSON))
}

/// One member of a bucket, held before it becomes a record or a table row.
/// Keeping the element rather than a record is what lets a count or a preview
/// skip copying bodies it never shows.
enum Item<'a> {
    Script(ElementRef<'a>, &'static str),
    Style(ElementRef<'a>, &'static str),
    Frame(ElementRef<'a>, &'static str),
    Link(ElementRef<'a>),
    Image(ElementRef<'a>),
    Form(ElementRef<'a>),
    Meta(ElementRef<'a>),
    Comment(&'a str),
    /// Decoded data, and the byte length of the source text it came from.
    JsonLd(Value, usize),
    /// A subresource the browser fetches, and its kind.
    Loaded(ElementRef<'a>, &'static str),
}

/// The members of `bucket` in document order, held to `where_` for a split
/// bucket. This walk alone defines membership — records, table rows, counts
/// and the overview all read it, so they can never disagree.
fn items<'a>(p: &'a Parsed, bucket: &str, where_: Option<&str>) -> PyResult<Vec<Item<'a>>> {
    let mut out = Vec::new();
    match bucket {
        "scripts" | "styles" | "iframes" => {
            let sel = parse_selector(match bucket {
                "scripts" => "script",
                "styles" => STYLE_SELECTOR,
                _ => "iframe",
            })?;
            for e in p.html.select(&sel) {
                let Some(side) = side_of(bucket, &e) else {
                    continue;
                };
                if where_.is_some_and(|w| w != side) {
                    continue;
                }
                out.push(match bucket {
                    "scripts" => Item::Script(e, side),
                    "styles" => Item::Style(e, side),
                    _ => Item::Frame(e, side),
                });
            }
        }
        "links" => out.extend(p.html.select(&parse_selector("a[href]")?).map(Item::Link)),
        "images" => out.extend(
            p.html
                .select(&parse_selector("img[src]")?)
                .filter(|e| e.value().attr("src").is_some_and(fetches))
                .map(Item::Image),
        ),
        "forms" => out.extend(p.html.select(&parse_selector("form")?).map(Item::Form)),
        "meta" => out.extend(p.html.select(&parse_selector("meta")?).map(Item::Meta)),
        "comments" => out.extend(p.html.tree.nodes().filter_map(|n| match n.value() {
            Node::Comment(c) => Some(Item::Comment(&c.comment)),
            _ => None,
        })),
        "json_ld" => {
            for e in p.html.select(&parse_selector("script")?).filter(is_ld_json) {
                let text = collect_text(e);
                // A malformed block is skipped rather than surfaced half-parsed.
                if let Ok(value) = serde_json::from_str(&text) {
                    out.push(Item::JsonLd(value, text.len()));
                }
            }
        }
        "loaded" => {
            for e in p.html.select(&parse_selector(LOADED_SELECTOR)?) {
                let (kind, split) = match e.value().name() {
                    "script" => ("script", Some("scripts")),
                    "link" => ("style", Some("styles")),
                    "iframe" => ("iframe", Some("iframes")),
                    _ => ("image", None),
                };
                // Only what the browser requests: an ld+json script is data, a
                // frame whose `srcdoc` wins never loads its `src`, and an empty or
                // `about:` src loads nothing.
                let requested = match split {
                    Some(b) => side_of(b, &e) == Some("external"),
                    None => e.value().attr("src").is_some_and(fetches),
                };
                if !requested {
                    continue;
                }
                out.push(Item::Loaded(e, kind));
            }
        }
        other => return Err(unknown_bucket(other)),
    }
    Ok(out)
}

impl Item<'_> {
    /// The side of a split bucket this member sits on; `None` for any other bucket.
    fn side(&self) -> Option<&'static str> {
        match self {
            Item::Script(_, side) | Item::Style(_, side) | Item::Frame(_, side) => Some(side),
            _ => None,
        }
    }

    /// The whole record, keyed to the Python record's fields. Search reads this
    /// too, so what a search matches is exactly what the caller gets back.
    fn record(&self, p: &Parsed) -> Value {
        let url = |raw: Option<&str>| match raw {
            Some(raw) => {
                let (resolved, raw) = url_record(p.base.as_ref(), raw);
                (Value::String(resolved), Value::String(raw))
            }
            None => (Value::Null, Value::Null),
        };
        match self {
            Item::Script(e, side) => {
                let v = e.value();
                let text = match *side {
                    "inline" => Value::String(collect_text(*e)),
                    _ => Value::Null,
                };
                let (url, raw) = url(v.attr("src"));
                let attrs = v
                    .attrs()
                    .map(|(k, val)| (k.to_string(), Value::from(val)))
                    .collect();
                object([
                    ("where", Value::from(*side)),
                    ("text", text),
                    ("url", url),
                    ("raw", raw),
                    ("type", Value::from(v.attr("type").unwrap_or_default())),
                    ("attrs", Value::Object(attrs)),
                ])
            }
            Item::Style(e, "inline") => object([
                ("where", Value::from("inline")),
                ("text", Value::String(collect_text(*e))),
                ("url", Value::Null),
                ("raw", Value::Null),
                ("media", Value::Null),
            ]),
            Item::Style(e, side) => {
                let (url, raw) = url(e.value().attr("href"));
                object([
                    ("where", Value::from(*side)),
                    ("text", Value::Null),
                    ("url", url),
                    ("raw", raw),
                    (
                        "media",
                        e.value().attr("media").map_or(Value::Null, Value::from),
                    ),
                ])
            }
            Item::Frame(e, side) => {
                let v = e.value();
                let (url, raw) = match (*side, v.attr("src")) {
                    // A blank frame's src loads nothing, so it stays as authored:
                    // resolving "" would turn it into the page's own URL.
                    ("blank", Some(src)) => (Value::from(src), Value::from(src)),
                    (_, src) => url(src),
                };
                object([
                    ("where", Value::from(*side)),
                    ("srcdoc", v.attr("srcdoc").map_or(Value::Null, Value::from)),
                    ("url", url),
                    ("raw", raw),
                ])
            }
            Item::Link(e) => {
                let (url, raw) = url(e.value().attr("href"));
                object([
                    ("url", url),
                    ("raw", raw),
                    ("text", Value::String(collect_text(*e))),
                ])
            }
            Item::Image(e) => {
                let (url, raw) = url(e.value().attr("src"));
                let alt = e.value().attr("alt").unwrap_or_default();
                object([("url", url), ("raw", raw), ("alt", Value::from(alt))])
            }
            Item::Form(e) => {
                let v = e.value();
                let (url, raw) = url(Some(v.attr("action").unwrap_or_default()));
                let method = v.attr("method").unwrap_or("get").to_ascii_lowercase();
                // Hidden inputs stay — a CSRF token or stashed id is what a caller wants.
                let inputs = match parse_selector(FIELD_SELECTOR) {
                    Ok(sel) => e
                        .select(&sel)
                        .map(|f| {
                            let fv = f.value();
                            object([
                                ("name", Value::from(fv.attr("name").unwrap_or_default())),
                                ("type", Value::from(fv.attr("type").unwrap_or_default())),
                                ("value", Value::from(fv.attr("value").unwrap_or_default())),
                            ])
                        })
                        .collect(),
                    Err(_) => Vec::new(),
                };
                object([
                    ("url", url),
                    ("raw", raw),
                    ("method", Value::String(method)),
                    ("inputs", Value::Array(inputs)),
                ])
            }
            Item::Meta(e) => {
                let v = e.value();
                // `name`, `property` and `http-equiv` fold into one key, so a
                // caller looks in one place for what the page declares. A bare
                // `<meta charset>` reads as `charset` with its value as content.
                let name = v
                    .attr("name")
                    .or_else(|| v.attr("property"))
                    .or_else(|| v.attr("http-equiv"))
                    .or_else(|| v.attr("charset").map(|_| "charset"))
                    .unwrap_or_default();
                let content = v
                    .attr("content")
                    .or_else(|| v.attr("charset"))
                    .unwrap_or_default();
                object([
                    ("name", Value::from(name)),
                    ("content", Value::from(content)),
                ])
            }
            Item::Comment(text) => object([("text", Value::from(*text))]),
            Item::JsonLd(value, _) => object([("data", value.clone())]),
            Item::Loaded(e, kind) => {
                let attr = if *kind == "style" { "href" } else { "src" };
                let (url, raw) = url(e.value().attr(attr));
                object([("kind", Value::from(*kind)), ("url", url), ("raw", raw)])
            }
        }
    }

    /// `(where, size, preview)` for a table row. Only an inline body is costly to
    /// copy, so only it is previewed straight from the element; every other row
    /// reads the cheap record.
    fn row(&self, p: &Parsed, width: usize) -> (&'static str, Option<usize>, String) {
        // `k=v` pairs for an element with no name to show, `skip` left out.
        let attrs_of = |e: &ElementRef<'_>, skip: &str| {
            e.value()
                .attrs()
                .filter(|(k, _)| *k != skip)
                .map(|(k, v)| format!("{k}={v}"))
                .collect::<Vec<_>>()
                .join(" ")
        };
        match self {
            Item::Script(e, "inline") | Item::Style(e, "inline") => {
                let text = collect_text(*e);
                ("inline", Some(text.len()), preview_of(&text, width))
            }
            Item::Frame(e, "inline") => {
                let body = e.value().attr("srcdoc").unwrap_or_default();
                ("inline", Some(body.len()), preview_of(body, width))
            }
            Item::Frame(e, "blank") => {
                // A blank frame shows what it loads plus its id or name — ad slots
                // are told apart by those alone.
                let src = e.value().attr("src").filter(|s| !s.trim().is_empty());
                let rest = attrs_of(e, "src");
                let src = src.unwrap_or("about:blank");
                let shown = if rest.is_empty() {
                    src.to_string()
                } else {
                    format!("{src} · {rest}")
                };
                ("blank", None, preview_of(&shown, width))
            }
            Item::Comment(text) => ("", Some(text.len()), preview_of(text, width)),
            Item::JsonLd(value, _) => ("", None, preview_of(&value.to_string(), width)),
            Item::Form(_) => {
                let record = self.record(p);
                let fields = record["inputs"].as_array().map_or(0, Vec::len);
                let noun = if fields == 1 { "field" } else { "fields" };
                // Method and field count first, so a long action can't clip them away.
                let lead = format!("{} · {fields} {noun} · ", str_field(&record, "method"));
                let url_width = width.saturating_sub(lead.chars().count());
                let url = preview_url(str_field(&record, "url"), url_width);
                ("", None, format!("{lead}{url}"))
            }
            Item::Meta(e) => {
                let record = self.record(p);
                let shown = match str_field(&record, "name") {
                    // Nothing names the tag, so its attributes are what tells it apart.
                    "" => attrs_of(e, ""),
                    name => format!("{name}={}", str_field(&record, "content")),
                };
                ("", None, preview_of(&shown, width))
            }
            Item::Loaded(_, kind) => {
                let record = self.record(p);
                (kind, None, preview_url(str_field(&record, "url"), width))
            }
            Item::Script(..) | Item::Style(..) | Item::Frame(..) => {
                let record = self.record(p);
                (
                    "external",
                    None,
                    preview_url(str_field(&record, "url"), width),
                )
            }
            Item::Link(_) | Item::Image(_) => {
                let record = self.record(p);
                ("", None, preview_url(str_field(&record, "url"), width))
            }
        }
    }

    /// Bytes this member carries inside the document, or `None` when its body
    /// lives elsewhere or there is nothing to measure.
    fn inline_size(&self) -> Option<usize> {
        match self {
            Item::Script(e, "inline") | Item::Style(e, "inline") => {
                // Sum text-node lengths in place rather than copy the body to measure it.
                Some(e.text().map(str::len).sum())
            }
            Item::Frame(e, "inline") => Some(e.value().attr("srcdoc").map_or(0, str::len)),
            Item::Comment(text) => Some(text.len()),
            Item::JsonLd(_, size) => Some(*size),
            _ => None,
        }
    }
}

/// A JSON object from `(key, value)` pairs, values moved rather than copied.
fn object<const N: usize>(fields: [(&str, Value); N]) -> Value {
    Value::Object(
        fields
            .into_iter()
            .map(|(k, v)| (k.to_string(), v))
            .collect(),
    )
}

/// A string field of a record, or `""` when absent or not a string.
fn str_field<'v>(record: &'v Value, key: &str) -> &'v str {
    record.get(key).and_then(Value::as_str).unwrap_or_default()
}

/// A compiled search term. Every mode is a regex — a plain substring is an
/// escaped pattern — so a case-insensitive search never lowercases a copy of
/// the body it scans.
struct Matcher {
    re: Regex,
    field: Option<String>,
}

fn compile(queries: Option<Vec<Query>>) -> PyResult<Vec<Matcher>> {
    queries
        .unwrap_or_default()
        .into_iter()
        .map(|(needle, field, case_sensitive, regex)| {
            let pattern = if regex {
                needle.clone()
            } else {
                regex::escape(&needle)
            };
            let re = RegexBuilder::new(&pattern)
                .case_insensitive(!case_sensitive)
                .build()
                .map_err(|e| {
                    pyo3::exceptions::PyValueError::new_err(format!(
                        "invalid search pattern {needle:?}: {e}"
                    ))
                })?;
            Ok(Matcher { re, field })
        })
        .collect()
}

/// True when `record` satisfies every matcher.
fn matches_all(matchers: &[Matcher], record: &Value) -> bool {
    matchers.iter().all(|m| {
        let mut hit = false;
        each_string(record, m.field.as_deref(), &mut |_, s| {
            hit = m.re.is_match(s);
            !hit
        });
        hit
    })
}

/// Feed `visit` every string a search reads in `record`, with the top-level
/// field holding it: `field` alone when given, else every field but onyxweb's
/// labels. Numbers and nested object keys count, so a search for `nonce` finds
/// a script carrying that attribute. `visit` returns false to stop the walk.
fn each_string(record: &Value, field: Option<&str>, visit: &mut dyn FnMut(&str, &str) -> bool) {
    fn walk(top: &str, v: &Value, visit: &mut dyn FnMut(&str, &str) -> bool) -> bool {
        match v {
            Value::String(s) => visit(top, s),
            Value::Number(n) => visit(top, &n.to_string()),
            Value::Array(items) => items.iter().all(|item| walk(top, item, visit)),
            Value::Object(fields) => fields
                .iter()
                .all(|(k, val)| visit(top, k) && walk(top, val, visit)),
            _ => true,
        }
    }
    let Some(fields) = record.as_object() else {
        return;
    };
    for (k, v) in fields {
        let wanted = match field {
            Some(f) => k == f,
            None => !LABEL_FIELDS.contains(&k.as_str()),
        };
        if wanted && !walk(k, v, visit) {
            return;
        }
    }
}

/// A preview framing the latest query's first hit in `record`, or `None` to keep
/// the ordinary preview because that hit sits in a string short enough to show
/// whole. The latest query is the narrowest, so it is the one worth framing.
fn match_window(matchers: &[Matcher], record: &Value, width: usize) -> Option<String> {
    for m in matchers.iter().rev() {
        let mut found = None;
        each_string(record, m.field.as_deref(), &mut |_, s| {
            if let Some(hit) = m.re.find(s) {
                found = Some((s.len() > width).then(|| preview_around(s, hit.start(), width)));
            }
            found.is_none()
        });
        if let Some(window) = found {
            return window;
        }
    }
    None
}

/// One match inside a record, before it crosses to Python.
struct Hit {
    field: String,
    text: String,
    groups: Vec<Option<String>>,
    /// Character offsets into the matched string.
    start: usize,
    end: usize,
    /// A preview framing the match; built only for a table.
    window: Option<String>,
}

/// Every match of `m` in `record`, in field order. A text an earlier field
/// already matched is skipped, so one address held in `url`, `raw` and a `src`
/// attribute is reported once, at `url`.
fn hits_in(m: &Matcher, record: &Value, width: Option<usize>) -> Vec<Hit> {
    let mut hits: Vec<Hit> = Vec::new();
    let mut claimed: HashSet<String> = HashSet::new();
    let mut field_start = 0;
    each_string(record, m.field.as_deref(), &mut |field, s| {
        if hits.get(field_start).is_some_and(|h| h.field != field) {
            claimed.extend(hits[field_start..].iter().map(|h| h.text.clone()));
            field_start = hits.len();
        }
        // Walk the string once, counting characters only between matches.
        let (mut byte, mut chars) = (0, 0);
        for caps in m.re.captures_iter(s) {
            let whole = caps.get(0).expect("group 0 is the whole match");
            if claimed.contains(whole.as_str()) {
                continue;
            }
            chars += s[byte..whole.start()].chars().count();
            let start = chars;
            chars += whole.as_str().chars().count();
            byte = whole.end();
            hits.push(Hit {
                field: field.to_string(),
                text: whole.as_str().to_string(),
                groups: caps
                    .iter()
                    .skip(1)
                    .map(|g| g.map(|g| g.as_str().to_string()))
                    .collect(),
                start,
                end: chars,
                window: width.map(|w| preview_around(s, whole.start(), w)),
            });
        }
        true
    });
    hits
}

/// One-line preview clipped to `width` characters, whitespace collapsed so a
/// multi-line body renders as one table cell. Stops reading at the limit, so a
/// 200 KB script costs `width` characters rather than a full copy.
fn preview_of(s: &str, width: usize) -> String {
    let mut out = String::new();
    let mut n = 0usize;
    for word in s.split_whitespace() {
        if n >= width {
            out.push('…');
            return out;
        }
        if n > 0 {
            out.push(' ');
            n += 1;
        }
        for c in word.chars() {
            if n >= width {
                out.push('…');
                return out;
            }
            out.push(c);
            n += 1;
        }
    }
    out
}

/// URL preview that keeps both ends within `width` characters. The host says
/// where a file lives and the tail says what it is; clipping only the end makes
/// every file under one long CDN path look identical.
fn preview_url(url: &str, width: usize) -> String {
    let n = url.chars().count();
    if n <= width {
        return url.to_string();
    }
    let head = width * 2 / 5;
    let tail = width.saturating_sub(head + 1);
    let start: String = url.chars().take(head).collect();
    let end: String = url.chars().skip(n - tail).collect();
    format!("{start}…{end}")
}

/// A one-line preview of `s` starting a third of `width` before byte `start`,
/// so a match deep in a long body shows with what surrounds it. A cut-off start
/// is marked `…`.
fn preview_around(s: &str, start: usize, width: usize) -> String {
    let from = s[..start]
        .char_indices()
        .rev()
        .nth(width / 3)
        .map_or(0, |(i, _)| i);
    if from == 0 {
        return preview_of(s, width);
    }
    format!("…{}", preview_of(&s[from..], width.saturating_sub(1)))
}

/// One table line, held until its repeat count is final.
struct Row {
    /// Position in the searched bucket; a group of identical rows keeps its first.
    index: usize,
    /// Side of a split bucket, or `""`.
    side: &'static str,
    size: Option<usize>,
    preview: String,
    /// How many identical records the row stands for.
    count: usize,
}

/// A table row as the Python renderer reads it.
fn table_row(py: Python<'_>, row: Row) -> PyResult<PyObject> {
    let d = PyDict::new(py);
    d.set_item("index", row.index)?;
    d.set_item("where", row.side)?;
    d.set_item("size", row.size)?;
    d.set_item("preview", row.preview)?;
    d.set_item("count", row.count)?;
    Ok(d.into_any().unbind())
}

/// Decode a `serde_json::Value` into Python natives.
fn json_to_py(py: Python<'_>, v: &Value) -> PyResult<PyObject> {
    Ok(match v {
        Value::Null => py.None(),
        Value::Bool(b) => PyBool::new(py, *b).to_owned().into_any().unbind(),
        Value::Number(n) => match n.as_i64() {
            Some(i) => i.into_pyobject(py)?.into_any().unbind(),
            None => n
                .as_f64()
                .unwrap_or(0.0)
                .into_pyobject(py)?
                .into_any()
                .unbind(),
        },
        Value::String(s) => s.into_pyobject(py)?.into_any().unbind(),
        Value::Array(a) => {
            let list = PyList::empty(py);
            for item in a {
                list.append(json_to_py(py, item)?)?;
            }
            list.into_any().unbind()
        }
        Value::Object(o) => {
            let d = PyDict::new(py);
            for (k, val) in o {
                d.set_item(k, json_to_py(py, val)?)?;
            }
            d.into_any().unbind()
        }
    })
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
    /// This element's own attributes, snapshotted at parse time (name, value) —
    /// a re-parse can't recover them for `<html>`/`<body>` (html5ever merges a
    /// literal wrapper tag onto its own implicit context instead of keeping it).
    attrs: Vec<(String, String)>,
}

impl Element {
    fn from_ref(e: ElementRef<'_>) -> Self {
        let val = e.value();
        let tag = val.name().to_string();
        let outer = e.html();
        let inner = e.inner_html();
        let text = collect_text(e);
        let attrs = val
            .attrs()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect();
        Self {
            tag,
            text,
            html: outer,
            inner_html: inner,
            attrs,
        }
    }
}

#[pymethods]
impl Element {
    #[getter]
    fn attrs<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let d = PyDict::new(py);
        for (k, v) in &self.attrs {
            d.set_item(k, v)?;
        }
        Ok(d)
    }

    fn attr(&self, name: &str) -> Option<String> {
        self.attrs
            .iter()
            .find(|(k, _)| k == name)
            .map(|(_, v)| v.clone())
    }

    /// Nested query: parse this element's inner HTML (excludes self) as a fragment.
    fn query(&self, selector: &str) -> PyResult<Vec<Element>> {
        let sel = parse_selector(selector)?;
        let fragment = Html::parse_fragment(&self.inner_html);
        Ok(fragment.select(&sel).map(Element::from_ref).collect())
    }

    fn query_one(&self, selector: &str) -> PyResult<Option<Element>> {
        let sel = parse_selector(selector)?;
        let fragment = Html::parse_fragment(&self.inner_html);
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
        // Clip by character: a byte offset can land inside a multibyte one.
        let short = match self.text.char_indices().nth(60) {
            Some((cut, _)) => format!("{}…", &self.text[..cut]),
            None => self.text.clone(),
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
    // Text inside these is code or inert markup, not page content. Including it
    // buries the words: script contents run 77-99% of the characters on a real
    // page (cnn.com: 20 KB of text inside 2.15 MB).
    const SKIP: &[&str] = &["script", "style", "noscript", "template"];
    let mut out = String::new();
    let mut stack: Vec<_> = e.children().rev().collect();
    while let Some(node) = stack.pop() {
        match node.value() {
            Node::Text(t) => out.push_str(t),
            Node::Element(el) if !SKIP.contains(&el.name()) => {
                stack.extend(node.children().rev());
            }
            _ => {}
        }
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
