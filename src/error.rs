//! Error types. Timeouts (navigation + CDP) surface to Python as the builtin
//! `TimeoutError`; every other failure surfaces as `onyxweb.OnyxwebError`, a
//! `create_exception!` subclass of `RuntimeError` (so `except RuntimeError`
//! keeps working). Mapping lives in `From<OnyxError> for PyErr` below.

use pyo3::exceptions::{PyRuntimeError, PyTimeoutError};
use pyo3::prelude::*;
use thiserror::Error;

// Base class for all onyxweb errors. Subclasses RuntimeError so existing
// `except RuntimeError` code keeps working. Registered on the module in
// `lib.rs` as `onyxweb.OnyxwebError`. Note: timeouts intentionally raise the
// builtin `TimeoutError` (below), NOT this — so callers can branch on them.
pyo3::create_exception!(
    _onyxweb,
    OnyxwebError,
    PyRuntimeError,
    "Base class for onyxweb errors (subclass of RuntimeError). Timeouts raise \
     the builtin TimeoutError instead."
);

pyo3::create_exception!(
    _onyxweb,
    ChromeExitedError,
    OnyxwebError,
    "Chrome has exited; every later call on that client fails. Create a new client."
);

// A TimeoutError subclass, so `except TimeoutError` still catches it.
pyo3::create_exception!(
    _onyxweb,
    QueueTimeoutError,
    PyTimeoutError,
    "No pooled tab became free within `queue_timeout_ms`."
);

#[derive(Debug, Error)]
pub enum OnyxError {
    #[error(
        "chrome binary not found — pass chrome_path=, set ONYXWEB_CHROME, or install chromium ({0})"
    )]
    ChromeNotFound(String),

    #[error("browser launch failed: {0}")]
    LaunchFailed(String),

    #[error(
        "Chrome exited ({0}); every later call on this client fails, create a new Client (or AsyncClient) to continue"
    )]
    ChromeExited(String),

    #[error("no tab was free within {timeout_ms}ms; raise queue_timeout_ms or lower the load")]
    QueueTimeout { timeout_ms: u64 },

    #[error("navigation to {url} did not reach lifecycle event {wait_until} within {timeout_ms}ms")]
    NavigationTimeout {
        timeout_ms: u64,
        url: String,
        wait_until: &'static str,
    },

    #[error("post_load_scripts[{index}]: {source}")]
    PostLoadScript {
        index: usize,
        #[source]
        source: Box<OnyxError>,
    },

    /// A CDP-level timeout (chromiumoxide's `Request timed out`) — distinct
    /// from `NavigationTimeout` (onyxweb's own lifecycle wait). Both map to
    /// the builtin `TimeoutError` on the Python side.
    #[error("timed out: {0}")]
    Timeout(String),

    #[error("CDP: {0}")]
    Cdp(String),

    /// The origin answered with an error status and no body, which Chrome will not render:
    /// it aborts the navigation, so the status would otherwise be lost as a `cdp` fault.
    #[error(
        "{url} answered HTTP {status} with no body, which Chrome will not render; the origin \
         refused the request, so retry it later, or fetch with engine=\"shell\" to read the \
         empty page it sends"
    )]
    HttpStatus { status: u16, url: String },

    /// The host name does not resolve. A dead name is the caller's or the DNS's problem,
    /// never the browser malfunctioning, so it gets its own cause.
    #[error("cannot resolve {url}; check the host name, and that DNS works here")]
    DnsFailure { url: String },

    /// The host resolves but nothing is listening on that port.
    #[error("nothing accepted a connection to {url}; the host resolves, but that port is closed")]
    ConnectionRefused { url: String },

    /// The TLS handshake or certificate was refused.
    #[error(
        "TLS to {url} failed ({detail}); pass ignore_https_errors=True only if you mean to \
         trust that certificate"
    )]
    TlsFailure { detail: String, url: String },

    /// The origin's redirects never reached a page.
    #[error("{url} redirected too many times; the origin's redirect loop never reached a page")]
    RedirectLoop { url: String },

    #[error("invalid URL: {0}")]
    InvalidUrl(String),

    #[error("invalid config: {0}")]
    InvalidConfig(String),

    #[error("io: {0}")]
    Io(#[from] std::io::Error),

    #[error("internal: {0}")]
    Internal(String),
}

impl OnyxError {
    pub fn cdp<E: std::fmt::Display>(e: E) -> Self {
        Self::Cdp(e.to_string())
    }

    /// Stable category string for the cause — attached to Python exceptions as
    /// `.kind` so callers can branch on it without parsing the message.
    pub fn kind(&self) -> &'static str {
        match self {
            OnyxError::ChromeNotFound(_) => "chrome_not_found",
            OnyxError::LaunchFailed(_) => "launch_failed",
            OnyxError::ChromeExited(_) => "chrome_exited",
            OnyxError::QueueTimeout { .. } => "queue_timeout",
            OnyxError::NavigationTimeout { .. } => "navigation_timeout",
            OnyxError::PostLoadScript { .. } => "post_load_script",
            OnyxError::Timeout(_) => "timeout",
            OnyxError::Cdp(_) => "cdp",
            OnyxError::HttpStatus { .. } => "http_status",
            OnyxError::DnsFailure { .. } => "dns_failure",
            OnyxError::ConnectionRefused { .. } => "connection_refused",
            OnyxError::TlsFailure { .. } => "tls_failure",
            OnyxError::RedirectLoop { .. } => "redirect_loop",
            OnyxError::InvalidUrl(_) => "invalid_url",
            OnyxError::InvalidConfig(_) => "invalid_config",
            OnyxError::Io(_) => "io",
            OnyxError::Internal(_) => "internal",
        }
    }

    /// Build the Python exception (`TimeoutError` for timeouts, `OnyxwebError`
    /// otherwise — see `From<OnyxError>`), enriched with `.url` (which fetch
    /// failed) and `.kind` (the cause category), plus `.status_code` when the
    /// cause is the origin's own answer. Used both to raise from a
    /// single fetch and to return the exception in a `batch` result list.
    pub fn into_py_err(self, py: Python<'_>, url: &str) -> PyErr {
        let kind = self.kind();
        let status = match &self {
            OnyxError::HttpStatus { status, .. } => Some(*status),
            _ => None,
        };
        let err: PyErr = self.into();
        let value = err.value(py).as_any();
        let _ = value.setattr("url", url);
        let _ = value.setattr("kind", kind);
        if let Some(code) = status {
            let _ = value.setattr("status_code", code);
        }
        err
    }
}

/// Every kind a failure can carry, for the front-ends that map one to their own vocabulary.
/// Kept beside `kind()` so a new cause is added once, next to its arms; `test_c15_http_api`
/// fails when the server's map lacks one, so the two cannot drift apart quietly.
pub const ALL_KINDS: &[&str] = &[
    "chrome_not_found",
    "launch_failed",
    "chrome_exited",
    "queue_timeout",
    "navigation_timeout",
    "post_load_script",
    "timeout",
    "cdp",
    "http_status",
    "dns_failure",
    "connection_refused",
    "tls_failure",
    "redirect_loop",
    "invalid_url",
    "invalid_config",
    "io",
    "internal",
];

/// The cause behind a failed navigation, from the words the browser used and, when a response
/// preceded the failure, the origin's status.
///
/// This is the one place a navigation failure is classified, so both routes it can take — a
/// `goto` that returns the error, and the pool's recorded `loadingFailed` — agree by
/// construction. Chrome names its reason in `net::ERR_*`; a reason nobody recognised stays
/// `cdp` with the browser's own text, never a guess.
pub fn navigation_failure(text: &str, status: Option<u16>, url: &str) -> OnyxError {
    let says = |needle: &str| text.contains(needle);
    if says("ERR_HTTP_RESPONSE_CODE_FAILURE")
        && let Some(status) = status
    {
        return OnyxError::HttpStatus {
            status,
            url: url.to_string(),
        };
    }
    if says("ERR_NAME_NOT_RESOLVED") || says("ERR_NAME_RESOLUTION_FAILED") {
        return OnyxError::DnsFailure {
            url: url.to_string(),
        };
    }
    if says("ERR_CONNECTION_REFUSED") {
        return OnyxError::ConnectionRefused {
            url: url.to_string(),
        };
    }
    // The whole certificate family (`ERR_CERT_*`) and the handshake ones (`ERR_SSL_*`).
    if says("ERR_CERT_") || says("ERR_SSL_") || says("ERR_BAD_SSL_") {
        return OnyxError::TlsFailure {
            detail: text.to_string(),
            url: url.to_string(),
        };
    }
    if says("ERR_TOO_MANY_REDIRECTS") {
        return OnyxError::RedirectLoop {
            url: url.to_string(),
        };
    }
    OnyxError::Cdp(text.to_string())
}

impl From<OnyxError> for PyErr {
    fn from(e: OnyxError) -> Self {
        let msg = e.to_string();
        match e {
            // Any timeout → builtin TimeoutError (catch `except TimeoutError`).
            OnyxError::NavigationTimeout { .. } | OnyxError::Timeout(_) => {
                PyTimeoutError::new_err(msg)
            }
            OnyxError::QueueTimeout { .. } => QueueTimeoutError::new_err(msg),
            OnyxError::ChromeExited(_) => ChromeExitedError::new_err(msg),
            // Everything else → onyxweb.OnyxwebError (a RuntimeError subclass).
            _ => OnyxwebError::new_err(msg),
        }
    }
}

impl From<chromiumoxide::error::CdpError> for OnyxError {
    fn from(e: chromiumoxide::error::CdpError) -> Self {
        // chromiumoxide's command timeout should surface as a timeout, not a
        // generic CDP error, so it maps to the builtin TimeoutError.
        if matches!(e, chromiumoxide::error::CdpError::Timeout) {
            return OnyxError::Timeout(e.to_string());
        }
        let msg = e.to_string();
        // Chrome parses `block_urls` only when a tab applies them, so a pattern the
        // config check let through fails here; it is the caller's config, not CDP.
        if msg.contains("failed to parse as a URLPattern") {
            return OnyxError::InvalidConfig(format!(
                "block_urls: {msg} Write each entry as a URLPattern, e.g. \"*://*.doubleclick.net/*\"."
            ));
        }
        OnyxError::Cdp(msg)
    }
}

impl From<url::ParseError> for OnyxError {
    fn from(e: url::ParseError) -> Self {
        OnyxError::InvalidUrl(e.to_string())
    }
}

/// Allow `?` to lift PyErr into OnyxError inside Rust-only code paths (e.g.
/// when parsing pydantic-dict config via PyO3 dict access).
impl From<PyErr> for OnyxError {
    fn from(e: PyErr) -> Self {
        OnyxError::InvalidConfig(e.to_string())
    }
}

pub type Result<T> = std::result::Result<T, OnyxError>;
