//! Rust-side mirror of the Python pydantic config hierarchy.
//!
//! Python side owns validation via pydantic. We accept a plain dict (the
//! `.model_dump()` output) across FFI and parse it here into typed Rust structs.
//! One place converts Python → Rust; nowhere else. This keeps Rust free of
//! pydantic and Python free of Rust's view.

use std::collections::HashMap;

use pyo3::prelude::*;
use pyo3::types::{PyAny, PyDict, PyList, PyTuple};

use crate::error::{OnyxError, Result};

// ----------------------------------------------------------------------------
// Top-level Client config
// ----------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum WaitUntil {
    /// Resolve on Page.loadEventFired — window.onload, all subresources loaded.
    /// Default. Matches Playwright / Puppeteer default behavior. Semantically
    /// complete: deferred scripts have run, SPAs have hydrated, etc.
    #[default]
    Load,
    /// Resolve on Page.domContentEventFired — DOM parsed but async scripts
    /// may still be running. Opt-in for speed on lean/static sites. Falls
    /// through to `load` for the rare edge case where DCL doesn't fire.
    /// Note: measurable wins are narrow — chromiumoxide's goto() already
    /// blocks until main-doc commits, which is where most of the latency lives.
    DomContentLoaded,
}

/// Filter threshold for ``RenderResult.console_messages`` capture.
/// Mirrors ``ClientConfig.capture_console_level``.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum CaptureConsoleLevel {
    /// Default. Capture only ``console.error`` + uncaught exceptions.
    #[default]
    Error,
    /// Capture warnings and errors.
    Warn,
    /// Capture every standard ``console.*`` method.
    All,
}

#[derive(Debug, Clone)]
pub struct ClientConfigRs {
    pub concurrency: usize,
    /// Which lifecycle event to wait for after navigation commits.
    pub wait_until: WaitUntil,
    /// Extra sleep after the chosen lifecycle event fires, in milliseconds.
    /// Useful for SPAs that render content via async JS AFTER DCL / load.
    pub wait_after_ms: u64,
    /// Extra sleep AFTER ``post_load_scripts`` run and BEFORE actions /
    /// capture, in milliseconds. Default 0 — opt-in. Use when post_load
    /// scripts schedule async work (setTimeout, fetch, deferred mutations)
    /// that needs to settle before capture.
    pub wait_after_post_load_ms: u64,
    /// When true, wait out WAF challenge interstitials (poll until they
    /// self-resolve) and self-heal hard blocks (drop anti-bot cookies + retry).
    /// Per-call `FetchConfig.bypass_anti_bot` overrides.
    pub bypass_anti_bot: bool,
    /// Which console levels populate `RenderResult.console_messages`.
    pub capture_console_level: CaptureConsoleLevel,
    pub viewport: ViewportRs,
    pub network: NetworkRs,
    pub emulation: EmulationRs,
    pub scripts: ScriptsRs,
    pub timeout: TimeoutRs,
    pub chrome: ChromeRs,
}

#[derive(Debug, Clone)]
pub struct ViewportRs {
    pub width: u32,
    pub height: u32,
    pub device_scale_factor: f64,
    pub mobile: bool,
}

#[derive(Debug, Clone, Default)]
pub struct NetworkRs {
    pub user_agent: Option<String>,
    pub user_agent_metadata: Option<UserAgentMetadataRs>,
    pub proxy: Option<String>,
    pub extra_headers: HashMap<String, String>,
    pub ignore_https_errors: bool,
    pub block_urls: Vec<String>,
    pub disable_cache: bool,
    pub offline: bool,
    pub latency_ms: Option<f64>,
    pub download_bps: Option<u64>,
    pub upload_bps: Option<u64>,
}

/// One entry in ``Sec-CH-UA``. Mirrors CDP's ``Emulation.UserAgentBrandVersion``.
#[derive(Debug, Clone)]
pub struct UserAgentBrandVersionRs {
    pub brand: String,
    pub version: String,
}

/// Structured client-hint metadata. Mirrors CDP's ``Emulation.UserAgentMetadata``
/// and feeds ``Network.setUserAgentOverride``'s ``userAgentMetadata`` field.
#[derive(Debug, Clone)]
pub struct UserAgentMetadataRs {
    pub brands: Option<Vec<UserAgentBrandVersionRs>>,
    pub full_version_list: Option<Vec<UserAgentBrandVersionRs>>,
    pub platform: String,
    pub platform_version: String,
    pub architecture: String,
    pub model: String,
    pub mobile: bool,
    pub bitness: Option<String>,
    pub wow64: bool,
    pub form_factors: Option<Vec<String>>,
}

/// Declarative JS injection. Applied at pool-page creation via
/// ``Page.addScriptToEvaluateOnNewDocument``. Timing variants
/// (``on_dom_content_loaded`` / ``on_load``) and URL scoping are sugar
/// implemented by wrapping the source; only ``on_new_document`` and
/// ``isolated_world`` map 1:1 to the CDP primitive.
#[derive(Debug, Clone, Default)]
pub struct ScriptsRs {
    pub on_new_document: Vec<String>,
    pub on_dom_content_loaded: Vec<String>,
    pub on_load: Vec<String>,
    pub isolated_world: Vec<String>,
    pub isolated_world_name: String,
    pub url_scoped: HashMap<String, Vec<String>>,
}

#[derive(Debug, Clone, Default)]
pub struct EmulationRs {
    pub locale: Option<String>,
    pub timezone: Option<String>,
    pub geolocation: Option<(f64, f64)>,
    pub prefers_color_scheme: Option<String>, // "light" | "dark"
    pub javascript_enabled: bool,
}

#[derive(Debug, Clone)]
pub struct TimeoutRs {
    pub navigation_ms: u64,
    pub launch_ms: u64,
    pub screenshot_ms: u64,
}

/// Which Chromium build the Client drives.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum ChromeEngine {
    /// Bundled `chrome-headless-shell` (old-headless; light, but anti-bot-flagged).
    #[default]
    HeadlessShell,
    /// Full Chrome in `--headless=new` with automation tells stripped.
    Full,
}

#[derive(Debug, Clone, Default)]
pub struct ChromeRs {
    pub path: Option<String>,
    pub args: Vec<String>,
    pub user_data_dir: Option<String>,
    pub headless: bool,
    pub engine: ChromeEngine,
}

impl Default for ViewportRs {
    fn default() -> Self {
        Self {
            width: 1200,
            height: 800,
            device_scale_factor: 1.0,
            mobile: false,
        }
    }
}

impl Default for TimeoutRs {
    fn default() -> Self {
        Self {
            navigation_ms: 30_000,
            launch_ms: 15_000,
            screenshot_ms: 5_000,
        }
    }
}

impl Default for ClientConfigRs {
    fn default() -> Self {
        Self {
            concurrency: 16,
            wait_until: WaitUntil::default(),
            wait_after_ms: 0,
            wait_after_post_load_ms: 0,
            bypass_anti_bot: false,
            capture_console_level: CaptureConsoleLevel::default(),
            viewport: ViewportRs::default(),
            network: NetworkRs::default(),
            emulation: EmulationRs {
                javascript_enabled: true,
                ..Default::default()
            },
            scripts: ScriptsRs::default(),
            timeout: TimeoutRs::default(),
            chrome: ChromeRs {
                headless: true,
                ..Default::default()
            },
        }
    }
}

// ----------------------------------------------------------------------------
// Per-call overrides
// ----------------------------------------------------------------------------

/// Failure policy for a selector-targeted action. Wait has no policy
/// because a sleep can't fail.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum ActionErrorPolicy {
    /// Record the error in ``RenderResult.errors`` and continue.
    #[default]
    Continue,
    /// Propagate the error and short-circuit the fetch.
    Abort,
    /// Silently skip; no error recorded.
    Ignore,
}

/// One post-load action — runs after the lifecycle event and any
/// ``wait_after_ms`` settle, before HTML capture. Mirrors the
/// pydantic discriminated union on the Python side.
#[derive(Debug, Clone)]
pub enum ActionRs {
    /// CDP-trusted mouse click on the element matched by ``selector``.
    Click {
        selector: String,
        wait_after_ms: u64,
        on_error: ActionErrorPolicy,
    },
    /// Set the input/textarea's value, fire bubbling input/change events.
    Fill {
        selector: String,
        value: String,
        wait_after_ms: u64,
        on_error: ActionErrorPolicy,
    },
    /// CDP-trusted mouse hover (``mouseMoved``) over the matched element.
    Hover {
        selector: String,
        wait_after_ms: u64,
        on_error: ActionErrorPolicy,
    },
    /// Sleep ``duration_ms`` in the action sequence.
    Wait { duration_ms: u64 },
}

#[derive(Debug, Clone, Default)]
pub struct FetchConfigRs {
    pub extra_headers: HashMap<String, String>,
    /// Per-call init scripts. Registered via
    /// ``Page.addScriptToEvaluateOnNewDocument`` BEFORE navigation; removed
    /// after capture so they don't leak to subsequent fetches.
    pub scripts: Vec<String>,
    /// Per-call post-load scripts. Run via ``page.evaluate(src)`` AFTER
    /// lifecycle event + ``wait_after_ms``, AFTER block_navigation arms,
    /// BEFORE the actions list, BEFORE HTML capture. Single CDP roundtrip
    /// per script. The primary primitive for DOMino-style "do JS work on
    /// the loaded page" use cases.
    pub post_load_scripts: Vec<String>,
    /// Per-call URL patterns to block at the network layer. Merged with
    /// ``base.network.block_urls`` and applied via
    /// ``Network.setBlockedURLs`` before navigation; base is restored
    /// after capture so per-call entries don't leak.
    pub block_urls: Vec<String>,
    /// Post-load actions — Click variants (more in later phases).
    pub actions: Vec<ActionRs>,
    /// When true, intercept navigation requests AFTER initial load via
    /// ``Fetch.requestPaused`` and fail them. Cleaned up before pool return.
    pub block_navigation: bool,
    /// Per-call override for anti-bot self-heal. None = inherit client default.
    pub bypass_anti_bot: Option<bool>,
    pub timeout_ms: Option<u64>,
    /// Per-call override. None = inherit client default.
    pub wait_until: Option<WaitUntil>,
    /// Per-call override for post-event sleep. None = inherit client default.
    pub wait_after_ms: Option<u64>,
    /// Per-call override for post-post_load_scripts settle. None = inherit
    /// client default (which itself defaults to 0 — opt-in).
    pub wait_after_post_load_ms: Option<u64>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum ImageFormat {
    #[default]
    Png,
    Jpeg,
    Webp,
}

#[derive(Debug, Clone, Default)]
pub struct ScreenshotConfigRs {
    pub viewport: Option<(u32, u32)>,
    pub full_page: bool,
    pub timeout_ms: Option<u64>,
    pub extra_headers: HashMap<String, String>,
    pub format: ImageFormat,
    /// 0-100, JPEG/WebP only. Ignored for PNG.
    pub quality: Option<u32>,
    /// Per-call override. None = inherit client default.
    pub wait_until: Option<WaitUntil>,
    /// Per-call override for post-event sleep. None = inherit client default.
    pub wait_after_ms: Option<u64>,
    /// Per-call override for post-post_load_scripts settle. None = inherit
    /// client default.
    pub wait_after_post_load_ms: Option<u64>,
}

// ----------------------------------------------------------------------------
// Python → Rust conversion
// ----------------------------------------------------------------------------

/// Parse a Python dict (from pydantic's `.model_dump()`) into ClientConfigRs.
pub fn parse_client_config(py_dict: &Bound<'_, PyAny>) -> Result<ClientConfigRs> {
    let mut cfg = ClientConfigRs::default();

    if let Some(d) = as_dict(py_dict)? {
        if let Some(v) = d.get_item("concurrency")? {
            cfg.concurrency = v.extract().map_err(to_internal)?;
        }
        if let Some(v) = d.get_item("wait_until")? {
            cfg.wait_until = parse_wait_until(&v)?;
        }
        if let Some(v) = d.get_item("wait_after_ms")?
            && !v.is_none()
        {
            cfg.wait_after_ms = v.extract().map_err(to_internal)?;
        }
        if let Some(v) = d.get_item("wait_after_post_load_ms")?
            && !v.is_none()
        {
            cfg.wait_after_post_load_ms = v.extract().map_err(to_internal)?;
        }
        if let Some(v) = d.get_item("bypass_anti_bot")?
            && !v.is_none()
        {
            cfg.bypass_anti_bot = v.extract().map_err(to_internal)?;
        }
        if let Some(v) = d.get_item("capture_console_level")?
            && !v.is_none()
        {
            let s: String = v.extract().map_err(to_internal)?;
            cfg.capture_console_level = match s.as_str() {
                "all" => CaptureConsoleLevel::All,
                "warn" => CaptureConsoleLevel::Warn,
                "error" => CaptureConsoleLevel::Error,
                other => {
                    return Err(OnyxError::Internal(format!(
                        "invalid capture_console_level {other:?}; expected all|warn|error"
                    )));
                }
            };
        }
        if let Some(v) = d.get_item("viewport")? {
            cfg.viewport = parse_viewport(&v)?;
        }
        if let Some(v) = d.get_item("network")? {
            cfg.network = parse_network(&v)?;
        }
        if let Some(v) = d.get_item("emulation")? {
            cfg.emulation = parse_emulation(&v)?;
        }
        if let Some(v) = d.get_item("scripts")? {
            cfg.scripts = parse_scripts(&v)?;
        }
        if let Some(v) = d.get_item("timeout")? {
            cfg.timeout = parse_timeout(&v)?;
        }
        if let Some(v) = d.get_item("chrome")? {
            cfg.chrome = parse_chrome(&v)?;
        }
    }
    Ok(cfg)
}

pub fn parse_fetch_config(py_dict: &Bound<'_, PyAny>) -> Result<FetchConfigRs> {
    let mut cfg = FetchConfigRs::default();
    if let Some(d) = as_dict(py_dict)? {
        if let Some(v) = d.get_item("extra_headers")? {
            cfg.extra_headers = parse_headers(&v)?;
        }
        if let Some(v) = d.get_item("scripts")? {
            cfg.scripts = v.extract().map_err(to_internal)?;
        }
        if let Some(v) = d.get_item("post_load_scripts")? {
            cfg.post_load_scripts = v.extract().map_err(to_internal)?;
        }
        if let Some(v) = d.get_item("block_urls")? {
            cfg.block_urls = v.extract().map_err(to_internal)?;
        }
        if let Some(v) = d.get_item("actions")?
            && !v.is_none()
        {
            let list = v
                .downcast::<PyList>()
                .map_err(|_| OnyxError::InvalidConfig("actions: expected list".to_string()))?;
            for item in list.iter() {
                cfg.actions.push(parse_action(&item)?);
            }
        }
        if let Some(v) = d.get_item("block_navigation")?
            && !v.is_none()
        {
            cfg.block_navigation = v.extract().map_err(to_internal)?;
        }
        if let Some(v) = d.get_item("bypass_anti_bot")?
            && !v.is_none()
        {
            cfg.bypass_anti_bot = Some(v.extract().map_err(to_internal)?);
        }
        if let Some(v) = d.get_item("timeout_ms")?
            && !v.is_none()
        {
            cfg.timeout_ms = Some(v.extract().map_err(to_internal)?);
        }
        if let Some(v) = d.get_item("wait_until")?
            && !v.is_none()
        {
            cfg.wait_until = Some(parse_wait_until(&v)?);
        }
        if let Some(v) = d.get_item("wait_after_ms")?
            && !v.is_none()
        {
            cfg.wait_after_ms = Some(v.extract().map_err(to_internal)?);
        }
        if let Some(v) = d.get_item("wait_after_post_load_ms")?
            && !v.is_none()
        {
            cfg.wait_after_post_load_ms = Some(v.extract().map_err(to_internal)?);
        }
    }
    Ok(cfg)
}

pub fn parse_screenshot_config(py_dict: &Bound<'_, PyAny>) -> Result<ScreenshotConfigRs> {
    let mut cfg = ScreenshotConfigRs::default();
    if let Some(d) = as_dict(py_dict)? {
        if let Some(v) = d.get_item("viewport")?
            && !v.is_none()
        {
            cfg.viewport = Some(parse_pair(&v)?);
        }
        if let Some(v) = d.get_item("full_page")? {
            cfg.full_page = v.extract().map_err(to_internal)?;
        }
        if let Some(v) = d.get_item("timeout_ms")?
            && !v.is_none()
        {
            cfg.timeout_ms = Some(v.extract().map_err(to_internal)?);
        }
        if let Some(v) = d.get_item("extra_headers")? {
            cfg.extra_headers = parse_headers(&v)?;
        }
        if let Some(v) = d.get_item("format")?
            && !v.is_none()
        {
            let s: String = v.extract().map_err(to_internal)?;
            cfg.format = match s.as_str() {
                "png" => ImageFormat::Png,
                "jpeg" => ImageFormat::Jpeg,
                "webp" => ImageFormat::Webp,
                other => {
                    return Err(OnyxError::InvalidConfig(format!(
                        "unknown image format {other:?}; expected png|jpeg|webp"
                    )));
                }
            };
        }
        if let Some(v) = d.get_item("quality")?
            && !v.is_none()
        {
            cfg.quality = Some(v.extract().map_err(to_internal)?);
        }
        if let Some(v) = d.get_item("wait_until")?
            && !v.is_none()
        {
            cfg.wait_until = Some(parse_wait_until(&v)?);
        }
        if let Some(v) = d.get_item("wait_after_ms")?
            && !v.is_none()
        {
            cfg.wait_after_ms = Some(v.extract().map_err(to_internal)?);
        }
        if let Some(v) = d.get_item("wait_after_post_load_ms")?
            && !v.is_none()
        {
            cfg.wait_after_post_load_ms = Some(v.extract().map_err(to_internal)?);
        }
    }
    Ok(cfg)
}

fn parse_wait_until(v: &Bound<'_, PyAny>) -> Result<WaitUntil> {
    let s: String = v.extract().map_err(to_internal)?;
    match s.as_str() {
        "domcontentloaded" | "dcl" => Ok(WaitUntil::DomContentLoaded),
        "load" => Ok(WaitUntil::Load),
        other => Err(OnyxError::InvalidConfig(format!(
            "unknown wait_until {other:?}; expected 'domcontentloaded' or 'load'"
        ))),
    }
}

/// Parse one element of ``FetchConfig.actions`` (a pydantic-dumped dict)
/// into the matching `ActionRs` variant. The ``type`` field is the
/// discriminator.
fn parse_action(v: &Bound<'_, PyAny>) -> Result<ActionRs> {
    let d = v
        .downcast::<PyDict>()
        .map_err(|_| OnyxError::InvalidConfig("action: expected dict".to_string()))?;
    let type_str: String = d
        .get_item("type")
        .map_err(to_internal)?
        .ok_or_else(|| OnyxError::InvalidConfig("action: missing 'type'".to_string()))?
        .extract()
        .map_err(to_internal)?;
    match type_str.as_str() {
        "click" => Ok(ActionRs::Click {
            selector: action_required_string(d, "selector", "Click")?,
            wait_after_ms: action_wait_after_ms(d)?,
            on_error: action_on_error(d)?,
        }),
        "fill" => Ok(ActionRs::Fill {
            selector: action_required_string(d, "selector", "Fill")?,
            value: action_required_string(d, "value", "Fill")?,
            wait_after_ms: action_wait_after_ms(d)?,
            on_error: action_on_error(d)?,
        }),
        "hover" => Ok(ActionRs::Hover {
            selector: action_required_string(d, "selector", "Hover")?,
            wait_after_ms: action_wait_after_ms(d)?,
            on_error: action_on_error(d)?,
        }),
        "wait" => {
            let duration_ms: u64 = d
                .get_item("duration_ms")
                .map_err(to_internal)?
                .ok_or_else(|| OnyxError::InvalidConfig("Wait: missing 'duration_ms'".to_string()))?
                .extract()
                .map_err(to_internal)?;
            Ok(ActionRs::Wait { duration_ms })
        }
        other => Err(OnyxError::InvalidConfig(format!(
            "unknown action type {other:?}"
        ))),
    }
}

/// Extract a required string field from an action dict. Errors include
/// the action name to make the failure self-describing.
fn action_required_string(d: &Bound<'_, PyDict>, key: &str, action: &str) -> Result<String> {
    d.get_item(key)
        .map_err(to_internal)?
        .ok_or_else(|| OnyxError::InvalidConfig(format!("{action}: missing '{key}'")))?
        .extract()
        .map_err(to_internal)
}

/// Extract the optional ``wait_after_ms`` field from an action dict
/// (defaults to 0 when absent).
fn action_wait_after_ms(d: &Bound<'_, PyDict>) -> Result<u64> {
    d.get_item("wait_after_ms")
        .map_err(to_internal)?
        .map(|v| v.extract::<u64>())
        .transpose()
        .map_err(to_internal)
        .map(|opt| opt.unwrap_or(0))
}

/// Extract the optional ``on_error`` field from an action dict
/// (defaults to ``ActionErrorPolicy::Continue`` when absent).
fn action_on_error(d: &Bound<'_, PyDict>) -> Result<ActionErrorPolicy> {
    let Some(v) = d.get_item("on_error").map_err(to_internal)? else {
        return Ok(ActionErrorPolicy::default());
    };
    if v.is_none() {
        return Ok(ActionErrorPolicy::default());
    }
    let s: String = v.extract().map_err(to_internal)?;
    match s.as_str() {
        "continue" => Ok(ActionErrorPolicy::Continue),
        "abort" => Ok(ActionErrorPolicy::Abort),
        "ignore" => Ok(ActionErrorPolicy::Ignore),
        other => Err(OnyxError::InvalidConfig(format!(
            "unknown on_error {other:?}; expected continue|abort|ignore"
        ))),
    }
}

// --- helpers ---------------------------------------------------------------

fn as_dict<'py>(v: &Bound<'py, PyAny>) -> Result<Option<Bound<'py, PyDict>>> {
    if v.is_none() {
        return Ok(None);
    }
    v.downcast::<PyDict>()
        .map(|d| Some(d.clone()))
        .map_err(|_| OnyxError::InvalidConfig("expected dict".to_string()))
}

fn to_internal(e: PyErr) -> OnyxError {
    OnyxError::InvalidConfig(e.to_string())
}

fn parse_viewport(v: &Bound<'_, PyAny>) -> Result<ViewportRs> {
    let mut out = ViewportRs::default();
    if let Some(d) = as_dict(v)? {
        if let Some(x) = d.get_item("width")? {
            out.width = x.extract().map_err(to_internal)?;
        }
        if let Some(x) = d.get_item("height")? {
            out.height = x.extract().map_err(to_internal)?;
        }
        if let Some(x) = d.get_item("device_scale_factor")? {
            out.device_scale_factor = x.extract().map_err(to_internal)?;
        }
        if let Some(x) = d.get_item("mobile")? {
            out.mobile = x.extract().map_err(to_internal)?;
        }
    }
    Ok(out)
}

fn parse_network(v: &Bound<'_, PyAny>) -> Result<NetworkRs> {
    let mut out = NetworkRs::default();
    if let Some(d) = as_dict(v)? {
        if let Some(x) = d.get_item("user_agent")?
            && !x.is_none()
        {
            out.user_agent = Some(x.extract().map_err(to_internal)?);
        }
        if let Some(x) = d.get_item("user_agent_metadata")?
            && !x.is_none()
        {
            out.user_agent_metadata = Some(parse_user_agent_metadata(&x)?);
        }
        if let Some(x) = d.get_item("proxy")?
            && !x.is_none()
        {
            out.proxy = Some(x.extract().map_err(to_internal)?);
        }
        if let Some(x) = d.get_item("extra_headers")? {
            out.extra_headers = parse_headers(&x)?;
        }
        if let Some(x) = d.get_item("ignore_https_errors")? {
            out.ignore_https_errors = x.extract().map_err(to_internal)?;
        }
        if let Some(x) = d.get_item("block_urls")? {
            out.block_urls = parse_str_list(&x)?;
        }
        if let Some(x) = d.get_item("disable_cache")? {
            out.disable_cache = x.extract().map_err(to_internal)?;
        }
        if let Some(x) = d.get_item("offline")? {
            out.offline = x.extract().map_err(to_internal)?;
        }
        if let Some(x) = d.get_item("latency_ms")?
            && !x.is_none()
        {
            out.latency_ms = Some(x.extract().map_err(to_internal)?);
        }
        if let Some(x) = d.get_item("download_bps")?
            && !x.is_none()
        {
            out.download_bps = Some(x.extract().map_err(to_internal)?);
        }
        if let Some(x) = d.get_item("upload_bps")?
            && !x.is_none()
        {
            out.upload_bps = Some(x.extract().map_err(to_internal)?);
        }
    }
    Ok(out)
}

fn parse_emulation(v: &Bound<'_, PyAny>) -> Result<EmulationRs> {
    let mut out = EmulationRs {
        javascript_enabled: true,
        ..Default::default()
    };
    if let Some(d) = as_dict(v)? {
        if let Some(x) = d.get_item("locale")?
            && !x.is_none()
        {
            out.locale = Some(x.extract().map_err(to_internal)?);
        }
        if let Some(x) = d.get_item("timezone")?
            && !x.is_none()
        {
            out.timezone = Some(x.extract().map_err(to_internal)?);
        }
        if let Some(x) = d.get_item("geolocation")?
            && !x.is_none()
        {
            out.geolocation = Some(parse_pair_f64(&x)?);
        }
        if let Some(x) = d.get_item("prefers_color_scheme")?
            && !x.is_none()
        {
            out.prefers_color_scheme = Some(x.extract().map_err(to_internal)?);
        }
        if let Some(x) = d.get_item("javascript_enabled")? {
            out.javascript_enabled = x.extract().map_err(to_internal)?;
        }
    }
    Ok(out)
}

fn parse_timeout(v: &Bound<'_, PyAny>) -> Result<TimeoutRs> {
    let mut out = TimeoutRs::default();
    if let Some(d) = as_dict(v)? {
        if let Some(x) = d.get_item("navigation_ms")? {
            out.navigation_ms = x.extract().map_err(to_internal)?;
        }
        if let Some(x) = d.get_item("launch_ms")? {
            out.launch_ms = x.extract().map_err(to_internal)?;
        }
        if let Some(x) = d.get_item("screenshot_ms")? {
            out.screenshot_ms = x.extract().map_err(to_internal)?;
        }
    }
    Ok(out)
}

fn parse_chrome(v: &Bound<'_, PyAny>) -> Result<ChromeRs> {
    let mut out = ChromeRs {
        headless: true,
        ..Default::default()
    };
    if let Some(d) = as_dict(v)? {
        if let Some(x) = d.get_item("path")?
            && !x.is_none()
        {
            out.path = Some(x.extract().map_err(to_internal)?);
        }
        if let Some(x) = d.get_item("args")? {
            out.args = parse_str_list(&x)?;
        }
        if let Some(x) = d.get_item("user_data_dir")?
            && !x.is_none()
        {
            out.user_data_dir = Some(x.extract().map_err(to_internal)?);
        }
        if let Some(x) = d.get_item("headless")? {
            out.headless = x.extract().map_err(to_internal)?;
        }
        if let Some(x) = d.get_item("engine")?
            && !x.is_none()
        {
            let s: String = x.extract().map_err(to_internal)?;
            out.engine = match s.as_str() {
                "full" => ChromeEngine::Full,
                "shell" => ChromeEngine::HeadlessShell,
                other => {
                    return Err(OnyxError::InvalidConfig(format!(
                        "unknown chrome.engine {other:?}; expected full|shell"
                    )));
                }
            };
        }
    }
    Ok(out)
}

fn parse_user_agent_brand_version(v: &Bound<'_, PyAny>) -> Result<UserAgentBrandVersionRs> {
    let d = v
        .downcast::<PyDict>()
        .map_err(|_| OnyxError::InvalidConfig("user_agent brand entry must be dict".to_string()))?;
    let brand: String = d
        .get_item("brand")?
        .ok_or_else(|| OnyxError::InvalidConfig("brand entry missing 'brand'".to_string()))?
        .extract()
        .map_err(to_internal)?;
    let version: String = d
        .get_item("version")?
        .ok_or_else(|| OnyxError::InvalidConfig("brand entry missing 'version'".to_string()))?
        .extract()
        .map_err(to_internal)?;
    Ok(UserAgentBrandVersionRs { brand, version })
}

fn parse_brand_list(v: &Bound<'_, PyAny>) -> Result<Vec<UserAgentBrandVersionRs>> {
    let lst = v.downcast::<PyList>().map_err(|_| {
        OnyxError::InvalidConfig("brands must be list of {brand,version} dicts".to_string())
    })?;
    lst.iter()
        .map(|item| parse_user_agent_brand_version(&item))
        .collect()
}

fn parse_user_agent_metadata(v: &Bound<'_, PyAny>) -> Result<UserAgentMetadataRs> {
    let d = v
        .downcast::<PyDict>()
        .map_err(|_| OnyxError::InvalidConfig("user_agent_metadata must be dict".to_string()))?;

    let brands = if let Some(x) = d.get_item("brands")? {
        if x.is_none() {
            None
        } else {
            Some(parse_brand_list(&x)?)
        }
    } else {
        None
    };

    let full_version_list = if let Some(x) = d.get_item("full_version_list")? {
        if x.is_none() {
            None
        } else {
            Some(parse_brand_list(&x)?)
        }
    } else {
        None
    };

    let required_str = |key: &str| -> Result<String> {
        d.get_item(key)?
            .ok_or_else(|| {
                OnyxError::InvalidConfig(format!("user_agent_metadata missing '{key}'"))
            })?
            .extract()
            .map_err(to_internal)
    };

    let platform = required_str("platform")?;
    let platform_version = required_str("platform_version")?;
    let architecture = required_str("architecture")?;
    let model = required_str("model")?;

    let mobile: bool = d
        .get_item("mobile")?
        .ok_or_else(|| {
            OnyxError::InvalidConfig("user_agent_metadata missing 'mobile'".to_string())
        })?
        .extract()
        .map_err(to_internal)?;

    let bitness = if let Some(x) = d.get_item("bitness")? {
        if x.is_none() {
            None
        } else {
            Some(x.extract::<String>().map_err(to_internal)?)
        }
    } else {
        None
    };

    let wow64 = match d.get_item("wow64")? {
        Some(x) if !x.is_none() => x.extract().map_err(to_internal)?,
        _ => false,
    };

    let form_factors = if let Some(x) = d.get_item("form_factors")? {
        if x.is_none() {
            None
        } else {
            Some(parse_str_list(&x)?)
        }
    } else {
        None
    };

    Ok(UserAgentMetadataRs {
        brands,
        full_version_list,
        platform,
        platform_version,
        architecture,
        model,
        mobile,
        bitness,
        wow64,
        form_factors,
    })
}

fn parse_scripts(v: &Bound<'_, PyAny>) -> Result<ScriptsRs> {
    let mut out = ScriptsRs::default();
    if let Some(d) = as_dict(v)? {
        if let Some(x) = d.get_item("on_new_document")? {
            out.on_new_document = parse_str_list(&x)?;
        }
        if let Some(x) = d.get_item("on_dom_content_loaded")? {
            out.on_dom_content_loaded = parse_str_list(&x)?;
        }
        if let Some(x) = d.get_item("on_load")? {
            out.on_load = parse_str_list(&x)?;
        }
        if let Some(x) = d.get_item("isolated_world")? {
            out.isolated_world = parse_str_list(&x)?;
        }
        if let Some(x) = d.get_item("isolated_world_name")?
            && !x.is_none()
        {
            out.isolated_world_name = x.extract().map_err(to_internal)?;
        }
        if let Some(x) = d.get_item("url_scoped")? {
            out.url_scoped = parse_url_scoped(&x)?;
        }
    }
    Ok(out)
}

fn parse_url_scoped(v: &Bound<'_, PyAny>) -> Result<HashMap<String, Vec<String>>> {
    if v.is_none() {
        return Ok(HashMap::new());
    }
    let d = v
        .downcast::<PyDict>()
        .map_err(|_| OnyxError::InvalidConfig("url_scoped must be dict".to_string()))?;
    let mut out = HashMap::with_capacity(d.len());
    for (k, val) in d.iter() {
        let key: String = k.extract().map_err(to_internal)?;
        let scripts = parse_str_list(&val)?;
        out.insert(key, scripts);
    }
    Ok(out)
}

fn parse_headers(v: &Bound<'_, PyAny>) -> Result<HashMap<String, String>> {
    if v.is_none() {
        return Ok(HashMap::new());
    }
    let d = v
        .downcast::<PyDict>()
        .map_err(|_| OnyxError::InvalidConfig("extra_headers must be dict".to_string()))?;
    let mut out = HashMap::with_capacity(d.len());
    for (k, val) in d.iter() {
        let key: String = k.extract().map_err(to_internal)?;
        let v: String = val.extract().map_err(to_internal)?;
        out.insert(key, v);
    }
    Ok(out)
}

fn parse_str_list(v: &Bound<'_, PyAny>) -> Result<Vec<String>> {
    if v.is_none() {
        return Ok(Vec::new());
    }
    let lst = v
        .downcast::<PyList>()
        .map_err(|_| OnyxError::InvalidConfig("expected list of strings".to_string()))?;
    lst.iter()
        .map(|item| item.extract::<String>().map_err(to_internal))
        .collect()
}

fn parse_pair(v: &Bound<'_, PyAny>) -> Result<(u32, u32)> {
    let tuple = v
        .downcast::<PyTuple>()
        .map_err(|_| OnyxError::InvalidConfig("expected (w, h) tuple".to_string()))?;
    if tuple.len() != 2 {
        return Err(OnyxError::InvalidConfig(
            "tuple must be length 2".to_string(),
        ));
    }
    let w: u32 = tuple
        .get_item(0)
        .map_err(to_internal)?
        .extract()
        .map_err(to_internal)?;
    let h: u32 = tuple
        .get_item(1)
        .map_err(to_internal)?
        .extract()
        .map_err(to_internal)?;
    Ok((w, h))
}

fn parse_pair_f64(v: &Bound<'_, PyAny>) -> Result<(f64, f64)> {
    let tuple = v
        .downcast::<PyTuple>()
        .map_err(|_| OnyxError::InvalidConfig("expected (a, b) tuple".to_string()))?;
    if tuple.len() != 2 {
        return Err(OnyxError::InvalidConfig(
            "tuple must be length 2".to_string(),
        ));
    }
    let a: f64 = tuple
        .get_item(0)
        .map_err(to_internal)?
        .extract()
        .map_err(to_internal)?;
    let b: f64 = tuple
        .get_item(1)
        .map_err(to_internal)?
        .extract()
        .map_err(to_internal)?;
    Ok((a, b))
}
