//! Error types. Timeouts (navigation + CDP) surface to Python as the builtin
//! `TimeoutError`; every other failure surfaces as `onyxweb.OnyxwebError`, a
//! `create_exception!` subclass of `RuntimeError` (so `except RuntimeError`
//! keeps working). Mapping lives in `From<OnyxError> for PyErr` below.

use pyo3::PyErr;
use pyo3::exceptions::{PyRuntimeError, PyTimeoutError};
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

#[derive(Debug, Error)]
pub enum OnyxError {
    #[error(
        "chrome binary not found — pass chrome_path=, set ONYXWEB_CHROME, or install chromium ({0})"
    )]
    ChromeNotFound(String),

    #[error("browser launch failed: {0}")]
    LaunchFailed(String),

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
}

impl From<OnyxError> for PyErr {
    fn from(e: OnyxError) -> Self {
        let msg = e.to_string();
        match e {
            // Any timeout → builtin TimeoutError (catch `except TimeoutError`).
            OnyxError::NavigationTimeout { .. } | OnyxError::Timeout(_) => {
                PyTimeoutError::new_err(msg)
            }
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
            OnyxError::Timeout(e.to_string())
        } else {
            OnyxError::Cdp(e.to_string())
        }
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
