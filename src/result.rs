//! PyO3-visible output types returned from engine operations.
//!
//! `RawRenderOutput` and `RawFetchOutput` are simple data containers. The Python
//! `__init__.py` wraps them into `RenderResult` (str subclass) and `FetchResult`
//! (dataclass-ish). We keep the Rust side minimal and let Python shape the UX.

use pyo3::prelude::*;

use crate::dom::Dom;

/// One captured ``console.*`` event (or uncaught exception). The Python side
/// wraps these into the user-facing ``ConsoleMessage`` dataclass.
#[pyclass(name = "_ConsoleMessage", frozen)]
#[derive(Clone, Debug)]
pub struct ConsoleMessageRs {
    /// The console method that fired, lowercase: ``"log"`` / ``"info"`` /
    /// ``"warning"`` / ``"error"`` / ``"debug"`` / ``"trace"``. Uncaught
    /// exceptions appear as ``"error"``.
    #[pyo3(get, name = "type")]
    pub kind: String,
    /// The rendered message text (chrome stringifies non-string args before
    /// the event fires; this is the joined result of all args).
    #[pyo3(get)]
    pub text: String,
    /// ``time.time()`` (Unix epoch seconds, f64) at the moment the event was
    /// captured by the Rust listener.
    #[pyo3(get)]
    pub timestamp: f64,
}

/// Output of a single fetch (HTML-only). Constructed by engine; passed to Python.
#[pyclass(name = "_RenderOutput")]
#[derive(Clone)]
pub struct RawRenderOutput {
    #[pyo3(get)]
    pub html: String,
    #[pyo3(get)]
    pub console_messages: Vec<ConsoleMessageRs>,
    #[pyo3(get)]
    pub final_url: String,
    #[pyo3(get)]
    pub request_url: String,
    /// Redirect hops (url, status, remote_ip) to the final response, in order.
    #[pyo3(get)]
    pub redirect_chain: Vec<(String, u16, Option<String>)>,
    #[pyo3(get)]
    pub status_code: u16,
    #[pyo3(get)]
    pub status_text: String,
    #[pyo3(get)]
    pub mime_type: String,
    #[pyo3(get)]
    pub protocol: String,
    #[pyo3(get)]
    pub remote_ip: Option<String>,
    #[pyo3(get)]
    pub remote_port: Option<u16>,
    /// Response headers as (name, value) pairs; Python wraps into the
    /// case-insensitive ``ResponseHeaders`` mapping.
    #[pyo3(get)]
    pub headers: Vec<(String, String)>,
    /// Canonical ``Name: Value\r\n...`` header block (no status line, no
    /// pseudo-headers). Always present; matches blasthttp's ``raw_headers``.
    #[pyo3(get)]
    pub header_raw: String,
    /// Hashes over the rendered body bytes (Python builds ``Hashes`` from these).
    #[pyo3(get)]
    pub body_md5: String,
    #[pyo3(get)]
    pub body_mmh3: i32,
    #[pyo3(get)]
    pub body_sha256: String,
    /// Hashes over ``header_raw`` bytes. Always present.
    #[pyo3(get)]
    pub header_md5: String,
    #[pyo3(get)]
    pub header_mmh3: i32,
    #[pyo3(get)]
    pub header_sha256: String,
    /// TLS cert as ``(common_name, sans, issuer, valid_from, valid_to)`` with
    /// epoch-seconds validity, or ``None`` (plain HTTP). Python wraps into
    /// ``CertInfo``.
    #[pyo3(get)]
    pub cert_info: Option<(String, Vec<String>, String, f64, f64)>,
    #[pyo3(get)]
    pub content_length: usize,
    #[pyo3(get)]
    pub elapsed_s: f64,
    /// One JSON-string entry per ``FetchConfig.post_load_scripts`` entry, in
    /// input order. ``None`` when the script returned ``undefined`` or a
    /// non-JSON-serializable value (DOM node, function). Python-side
    /// (`_make_render_result`) ``json.loads`` each entry into Python natives.
    #[pyo3(get)]
    pub post_load_results: Vec<Option<String>>,
    /// WAF/anti-bot measure as ``(vendor, kind, resolved)``, or ``None`` when
    /// clean. Python wraps into ``AntiBot``; ``vendor`` is ``None`` for a
    /// generic challenge with no identifiable vendor.
    #[pyo3(get)]
    pub anti_bot: Option<(Option<String>, String, bool)>,
}

#[pymethods]
impl RawRenderOutput {
    /// Build a Dom from the HTML (lazy-parse on first query). Called from
    /// Python-side RenderResult.dom property.
    fn make_dom(&self) -> Dom {
        Dom::from_html(self.html.clone())
    }
}

/// Output of fetch_all — HTML + PNG from one page visit.
#[pyclass(name = "_FetchOutput")]
#[derive(Clone)]
pub struct RawFetchOutput {
    #[pyo3(get)]
    pub html: String,
    #[pyo3(get)]
    pub png: Vec<u8>,
    #[pyo3(get)]
    pub console_messages: Vec<ConsoleMessageRs>,
    #[pyo3(get)]
    pub final_url: String,
    #[pyo3(get)]
    pub request_url: String,
    /// See ``RawRenderOutput.redirect_chain``.
    #[pyo3(get)]
    pub redirect_chain: Vec<(String, u16, Option<String>)>,
    #[pyo3(get)]
    pub status_code: u16,
    #[pyo3(get)]
    pub status_text: String,
    #[pyo3(get)]
    pub mime_type: String,
    #[pyo3(get)]
    pub protocol: String,
    #[pyo3(get)]
    pub remote_ip: Option<String>,
    #[pyo3(get)]
    pub remote_port: Option<u16>,
    /// See ``RawRenderOutput.headers``.
    #[pyo3(get)]
    pub headers: Vec<(String, String)>,
    /// See ``RawRenderOutput.header_raw``.
    #[pyo3(get)]
    pub header_raw: String,
    #[pyo3(get)]
    pub body_md5: String,
    #[pyo3(get)]
    pub body_mmh3: i32,
    #[pyo3(get)]
    pub body_sha256: String,
    #[pyo3(get)]
    pub header_md5: String,
    #[pyo3(get)]
    pub header_mmh3: i32,
    #[pyo3(get)]
    pub header_sha256: String,
    /// See ``RawRenderOutput.cert_info``.
    #[pyo3(get)]
    pub cert_info: Option<(String, Vec<String>, String, f64, f64)>,
    #[pyo3(get)]
    pub content_length: usize,
    #[pyo3(get)]
    pub elapsed_s: f64,
    /// See ``RawRenderOutput.post_load_results``.
    #[pyo3(get)]
    pub post_load_results: Vec<Option<String>>,
    /// See ``RawRenderOutput.anti_bot``.
    #[pyo3(get)]
    pub anti_bot: Option<(Option<String>, String, bool)>,
}

#[pymethods]
impl RawFetchOutput {
    fn make_dom(&self) -> Dom {
        Dom::from_html(self.html.clone())
    }
}
