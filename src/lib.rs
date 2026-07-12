//! onyxweb — URL in, fully-rendered HTML (and/or screenshot) out.
//!
//! This crate is the Rust side of the `onyxweb` Python package. The Python
//! side (`python/onyxweb/__init__.py`) is the user-facing API; this module
//! is the compiled extension `onyxweb._onyxweb`.

use pyo3::prelude::*;

mod chrome;
mod client;
mod config;
mod dom;
mod engine;
mod error;
mod hash;
mod pool;
mod result;
mod runtime;

use client::Client;
use dom::{Dom, Element};
use result::{ConsoleMessageRs, RawFetchOutput, RawRenderOutput};

/// Initialize env_logger. `ONYXWEB_LOG` takes precedence over `RUST_LOG`;
/// default is "warn" (only warnings + errors). Output has millisecond timestamps
/// + level + module target so trace output reads chronologically.
///
/// Bare levels ("debug", "trace") auto-narrow to the onyxweb crate — otherwise
/// "debug" would dump chromiumoxide + tungstenite + hyper chatter by default.
/// For cross-crate filters use the full env_logger syntax: "onyxweb=trace,hyper=info".
fn init_logger() {
    let raw = std::env::var("ONYXWEB_LOG")
        .or_else(|_| std::env::var("RUST_LOG"))
        .unwrap_or_else(|_| "warn".to_string());
    let filters = if raw.contains('=') || raw.contains(',') {
        raw
    } else {
        format!("onyxweb={raw}")
    };
    let _ = env_logger::Builder::new()
        .parse_filters(&filters)
        .format_timestamp_millis()
        .format_target(true)
        .try_init();
}

/// Change Rust-side log level at runtime. Called from Python's
/// `onyxweb.set_log_level()`. Accepts: trace | debug | info | warn | error | off.
#[pyfunction]
fn _set_rust_log_level(level: &str) -> PyResult<()> {
    let filter = match level.to_lowercase().as_str() {
        "trace" => log::LevelFilter::Trace,
        "debug" => log::LevelFilter::Debug,
        "info" => log::LevelFilter::Info,
        "warn" | "warning" => log::LevelFilter::Warn,
        "error" => log::LevelFilter::Error,
        "off" => log::LevelFilter::Off,
        other => {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "invalid log level {other:?}; expected trace|debug|info|warn|error|off"
            )));
        }
    };
    log::set_max_level(filter);
    log::info!(target: "onyxweb", "rust log level set to {filter}");
    Ok(())
}

#[pymodule]
#[pyo3(name = "_onyxweb")]
fn onyxweb_module(m: &Bound<'_, PyModule>) -> PyResult<()> {
    init_logger();

    // Eager-init the tokio runtime so first Client()/fetch() call doesn't pay it.
    let _ = runtime::shared();

    m.add_class::<Client>()?;
    m.add_class::<RawRenderOutput>()?;
    m.add_class::<RawFetchOutput>()?;
    m.add_class::<ConsoleMessageRs>()?;
    m.add_class::<Dom>()?;
    m.add_class::<Element>()?;
    m.add("OnyxwebError", m.py().get_type::<error::OnyxwebError>())?;
    m.add_function(wrap_pyfunction!(_set_rust_log_level, m)?)?;
    Ok(())
}
