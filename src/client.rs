//! `Client` pyclass — the main entry point. Owns a chromiumoxide Browser and a
//! Semaphore, dispatches Python calls to the shared tokio runtime.
//!
//! Two callable shapes, one implementation:
//! - **Sync methods** (`fetch`, `screenshot`, `fetch_all`, `batch`, `close`)
//!   release the GIL via `py.detach()` and `block_on()` the work on
//!   the shared runtime. N Python threads can call them concurrently and
//!   make real parallel progress; the page-pool semaphore caps in-flight
//!   pages at `concurrency`.
//! - **Async methods** (`fetch_async`, `screenshot_async`, `fetch_all_async`,
//!   `batch_async`, `close_async`) bridge to Python via
//!   `pyo3_async_runtimes::tokio::future_into_py`. They return Python
//!   awaitables that callers `await` from an asyncio event loop. No
//!   `detach` needed — the bridge handles GIL release.
//!
//! Both forms route through the same `do_*_inner` async helpers, so there's
//! exactly one implementation of each operation and two callable shapes.

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use chromiumoxide::browser::BrowserConfigBuilder;
use chromiumoxide::cdp::browser_protocol::browser::CloseParams;
use chromiumoxide::error::CdpError;
use chromiumoxide::{Browser, BrowserConfig};
use futures::StreamExt;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyList};

use crate::chrome;
use crate::config::{
    ChromeEngine, ClientConfigRs, FetchConfigRs, ScreenshotConfigRs, parse_client_config,
    parse_fetch_config, parse_screenshot_config,
};
use crate::engine::{CaptureMode, CaptureOutput, capture_page};
use crate::error::{OnyxError, Result};
use crate::pool::{PageGuard, PagePool};
use crate::result::{RawFetchOutput, RawRenderOutput};
use crate::runtime;

// One place maps a captured page into each Python-visible raw output. Both
// success paths (fetch / fetch_all / batch) funnel through these `From` impls
// so a new result field is added in exactly one spot per shape.
/// Build the canonical `Name: Value` raw-header string and its digests from
/// the header list. Both `From` impls funnel through this so the exposed
/// `header_raw` and `header_*` hashes always cover the same bytes.
fn header_raw_and_hashes(headers: &[(String, String)]) -> (String, crate::hash::HashTriple) {
    let raw = crate::hash::build_raw_headers(headers);
    let hashes = crate::hash::hash_bytes(raw.as_bytes());
    (raw, hashes)
}

impl From<CaptureOutput> for RawRenderOutput {
    fn from(out: CaptureOutput) -> Self {
        let html = out.html.unwrap_or_default();
        let body = crate::hash::hash_bytes(html.as_bytes());
        let (header_raw, header) = header_raw_and_hashes(&out.headers);
        let cert_info = out
            .cert_info
            .map(|c| (c.common_name, c.sans, c.issuer, c.valid_from, c.valid_to));
        RawRenderOutput {
            html,
            console_messages: out.console_messages,
            final_url: out.final_url,
            request_url: out.request_url,
            redirect_chain: out.redirect_chain,
            status_code: out.status_code,
            status_text: out.status_text,
            mime_type: out.mime_type,
            protocol: out.protocol,
            remote_ip: out.remote_ip,
            remote_port: out.remote_port,
            headers: out.headers,
            header_raw,
            body_md5: body.md5,
            body_mmh3: body.mmh3,
            body_sha256: body.sha256,
            header_md5: header.md5,
            header_mmh3: header.mmh3,
            header_sha256: header.sha256,
            cert_info,
            content_length: out.content_length,
            elapsed_s: out.elapsed_s,
            post_load_results: out.post_load_results,
            anti_bot: out
                .anti_bot
                .map(|a| (a.vendor, a.kind.to_string(), a.resolved)),
        }
    }
}

impl From<CaptureOutput> for RawFetchOutput {
    fn from(out: CaptureOutput) -> Self {
        let html = out.html.unwrap_or_default();
        let body = crate::hash::hash_bytes(html.as_bytes());
        let (header_raw, header) = header_raw_and_hashes(&out.headers);
        let cert_info = out
            .cert_info
            .map(|c| (c.common_name, c.sans, c.issuer, c.valid_from, c.valid_to));
        RawFetchOutput {
            html,
            png: out.png.unwrap_or_default(),
            console_messages: out.console_messages,
            final_url: out.final_url,
            request_url: out.request_url,
            redirect_chain: out.redirect_chain,
            status_code: out.status_code,
            status_text: out.status_text,
            mime_type: out.mime_type,
            protocol: out.protocol,
            remote_ip: out.remote_ip,
            remote_port: out.remote_port,
            headers: out.headers,
            header_raw,
            body_md5: body.md5,
            body_mmh3: body.mmh3,
            body_sha256: body.sha256,
            header_md5: header.md5,
            header_mmh3: header.mmh3,
            header_sha256: header.sha256,
            cert_info,
            content_length: out.content_length,
            elapsed_s: out.elapsed_s,
            post_load_results: out.post_load_results,
            anti_bot: out
                .anti_bot
                .map(|a| (a.vendor, a.kind.to_string(), a.resolved)),
        }
    }
}

/// Switches whose value is a comma list Chrome reads as one set: a caller's entries join the
/// engine's own. Any other switch a caller passes replaces the engine's value for it.
const LIST_SWITCHES: &[&str] = &[
    "disable-features",
    "enable-features",
    "disable-blink-features",
    "enable-blink-features",
];

/// One launch's switches, each name once, in the order first given.
///
/// chromiumoxide renders its arguments from a hash map, in no fixed order, and Chrome keeps the
/// last of a repeated switch: a switch given twice took whichever value happened to land last,
/// so a caller's `--lang` or `--user-agent` won on some launches and lost on others. Holding
/// each name once makes a command line mean the same thing on every launch.
#[derive(Default)]
struct Switches(Vec<(String, Vec<String>)>);

impl Switches {
    /// Adds `name[=value]`, with or without a leading `--` (chromiumoxide adds its own, so one
    /// left on would reach Chrome as `----name`, which Chrome ignores).
    fn add(&mut self, switch: &str) {
        let switch = switch.strip_prefix("--").unwrap_or(switch);
        let (name, value) = match switch.split_once('=') {
            Some((name, value)) => (name, Some(value)),
            None => (switch, None),
        };
        let values: Vec<String> = if LIST_SWITCHES.contains(&name) {
            value
                .unwrap_or("")
                .split(',')
                .filter(|v| !v.is_empty())
                .map(str::to_string)
                .collect()
        } else {
            value.map(str::to_string).into_iter().collect()
        };
        match self.0.iter_mut().find(|(n, _)| n == name) {
            Some((_, held)) if LIST_SWITCHES.contains(&name) => {
                for v in values {
                    if !held.contains(&v) {
                        held.push(v);
                    }
                }
            }
            Some((_, held)) => *held = values,
            None => self.0.push((name.to_string(), values)),
        }
    }

    fn apply(self, mut b: BrowserConfigBuilder) -> BrowserConfigBuilder {
        for (name, values) in self.0 {
            b = if values.is_empty() {
                b.arg(name)
            } else {
                let values: Vec<&str> = values.iter().map(String::as_str).collect();
                b.arg((name.as_str(), values.as_slice()))
            };
        }
        b
    }
}

/// The switches the `chrome-headless-shell` engine has always launched with: chromiumoxide
/// 0.9's defaults (Puppeteer's list). Held here rather than left to chromiumoxide so that a
/// caller's own value for one replaces it instead of racing it.
///
/// The engine once also passed a list of its own, written with their leading `--`, so every one
/// reached Chrome as `----x` and was ignored: that list never took effect, and every measurement
/// of this engine was made without it. It is gone rather than switched on, which would have
/// changed the engine under its own numbers.
const SHELL_SWITCHES: &[&str] = &[
    "disable-background-networking",
    "enable-features=NetworkService,NetworkServiceInProcess",
    "disable-background-timer-throttling",
    "disable-backgrounding-occluded-windows",
    "disable-breakpad",
    "disable-client-side-phishing-detection",
    "disable-component-extensions-with-background-pages",
    "disable-default-apps",
    "disable-dev-shm-usage",
    "disable-features=TranslateUI",
    "disable-hang-monitor",
    "disable-ipc-flooding-protection",
    "disable-popup-blocking",
    "disable-prompt-on-repost",
    "disable-renderer-backgrounding",
    "disable-sync",
    "force-color-profile=srgb",
    "metrics-recording-only",
    "no-first-run",
    "enable-automation",
    "password-store=basic",
    "use-mock-keychain",
    "enable-blink-features=IdleDetection",
    "lang=en_US",
];

fn build_shell_launch(builder: BrowserConfigBuilder, cfg: &ClientConfigRs) -> BrowserConfigBuilder {
    let mut switches = Switches::default();
    for switch in SHELL_SWITCHES {
        switches.add(switch);
    }
    for arg in &cfg.chrome.args {
        switches.add(arg);
    }
    switches.apply(builder.disable_default_args())
}

/// Full-Chrome launch (`--headless=new`): drop chromiumoxide's default
/// `--enable-automation`, disable the `AutomationControlled` blink feature (so
/// `navigator.webdriver === false`), and set a real Chrome UA via a launch flag
/// (a CDP UA override presents differently and gets flagged).
fn build_full_launch(
    builder: BrowserConfigBuilder,
    cfg: &ClientConfigRs,
    chrome_path: &Path,
) -> BrowserConfigBuilder {
    let ua = cfg
        .network
        .user_agent
        .clone()
        .or_else(|| derive_chrome_ua(chrome_path))
        .unwrap_or_else(fallback_full_ua);
    let mut switches = Switches::default();
    switches.add("disable-gpu");
    // Software WebGL: without it --disable-gpu leaves getContext('webgl') === null, itself a
    // headless tell.
    switches.add("enable-unsafe-swiftshader");
    switches.add("disable-dev-shm-usage");
    switches.add("disable-blink-features=AutomationControlled");
    switches.add(&format!("user-agent={ua}"));
    switches.add(&format!(
        "window-size={},{}",
        cfg.viewport.width, cfg.viewport.height
    ));
    if cfg.network.ignore_https_errors {
        switches.add("ignore-certificate-errors");
    }
    // No navigation here ever goes back, but full Chrome keeps each page a tab leaves in its
    // back/forward cache, in a renderer process of its own: a tab grew ~14 MB per fetch. A
    // caller's own `disable-features` joins this rather than replacing it.
    switches.add("disable-features=BackForwardCache");
    for arg in &cfg.chrome.args {
        switches.add(arg);
    }
    switches.apply(builder.new_headless_mode().disable_default_args())
}

/// A Chrome that exits before it is ready most often has a sandbox that cannot start: running
/// as root, in a container, or with restricted user namespaces. Chrome's own stderr may not
/// reach the error (chromiumoxide races its exit against reading it), so the early exit
/// itself is what triggers the hint.
fn launch_error(e: CdpError, sandbox: bool) -> OnyxError {
    if sandbox && matches!(e, CdpError::LaunchExit(..) | CdpError::LaunchIo(..)) {
        return OnyxError::LaunchFailed(format!(
            "{e}. If Chrome's sandbox cannot start here (running as root, in a container such \
             as Docker or BBOT, or with restricted user namespaces), pass sandbox=False or set \
             ONYXWEB_CHROME__SANDBOX=false."
        ));
    }
    OnyxError::from(e)
}

/// Profile dir + whether it's ours to delete. Must go through the builder:
/// chromiumoxide otherwise defaults every Client to one shared fixed path, which
/// real Chrome's ProcessSingleton then refuses to open twice.
fn resolve_user_data_dir(cfg: &ClientConfigRs) -> (PathBuf, bool) {
    if let Some(dir) = &cfg.chrome.user_data_dir {
        return (PathBuf::from(dir), false);
    }
    let unique = format!(
        "onyxweb-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0)
    );
    (std::env::temp_dir().join(unique), true)
}

/// This OS's `Mozilla/5.0 (...)` token, so the plain UA agrees with Chrome's own
/// `Sec-CH-UA-Platform` instead of contradicting it.
fn platform_token() -> &'static str {
    #[cfg(target_os = "windows")]
    return "Windows NT 10.0; Win64; x64";
    #[cfg(target_os = "macos")]
    return "Macintosh; Intel Mac OS X 10_15_7";
    #[cfg(not(any(target_os = "windows", target_os = "macos")))]
    "X11; Linux x86_64"
}

/// Fallback UA if the binary version can't be read.
fn fallback_full_ua() -> String {
    format!(
        "Mozilla/5.0 ({}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
        platform_token()
    )
}

/// Derive a real Chrome UA from the binary's `--version` (a UA/binary mismatch
/// is itself a tell). Returns None if the version can't be parsed.
fn derive_chrome_ua(chrome_path: &Path) -> Option<String> {
    let version = if cfg!(windows) {
        // Windows Chrome ignores `--version` and starts the browser, which never exits. The
        // version names a file beside it (`148.0.7778.56.manifest`) or, installed, a folder.
        std::fs::read_dir(chrome_path.parent()?)
            .ok()?
            .filter_map(|entry| entry.ok()?.file_name().into_string().ok())
            .map(|name| name.trim_end_matches(".manifest").to_string())
            .find(|name| {
                let parts: Vec<&str> = name.split('.').collect();
                parts.len() == 4 && parts.iter().all(|p| p.parse::<u32>().is_ok())
            })?
    } else {
        let out = std::process::Command::new(chrome_path)
            .arg("--version")
            .output()
            .ok()?;
        // e.g. "Chromium 147.0.7727.137" / "Google Chrome 148.0.7778.56"
        String::from_utf8_lossy(&out.stdout)
            .split_whitespace()
            .find(|t| t.chars().next().is_some_and(|c| c.is_ascii_digit()))?
            .to_string()
    };
    let major = version.split('.').next()?;
    Some(format!(
        "Mozilla/5.0 ({}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36",
        platform_token()
    ))
}

/// Chromium Browser + page pool + handler task. `config` is RwLock-wrapped so
/// `update_config` can swap atomically without blocking in-flight fetches.
struct ClientState {
    runtime: Arc<tokio::runtime::Runtime>,
    /// Keeps the browser process alive while the pool exists; `close` shuts it down. The lock
    /// is for `try_wait` and `wait`, which need `&mut`; every other call takes it shared.
    browser: Arc<tokio::sync::RwLock<Browser>>,
    /// How Chrome ended, once it has; set by the first call that finds it gone.
    exit: parking_lot::Mutex<Option<String>>,
    pool: Arc<PagePool>,
    handler_task: parking_lot::Mutex<Option<tokio::task::JoinHandle<()>>>,
    config: Arc<parking_lot::RwLock<ClientConfigRs>>,
    closed: std::sync::atomic::AtomicBool,
    /// The profile made for this launch. Last on purpose: fields drop in order, so it goes after
    /// the browser, whose drop kills a Chrome that `Drop for ClientState` could not stop.
    profile: EphemeralProfile,
}

/// A profile directory onyxweb made for one launch, deleted when the client goes: by `close`,
/// or when a client that was never closed is freed. `None` when the caller supplied their own.
struct EphemeralProfile(Option<PathBuf>);

impl EphemeralProfile {
    /// Best effort: a leftover temp dir must never surface as an error. `attempts` above one
    /// is for a Chrome only just killed, whose processes can still write for a moment.
    fn remove(&self, attempts: u32) {
        let Some(dir) = &self.0 else { return };
        for attempt in 1..=attempts {
            let _ = std::fs::remove_dir_all(dir);
            if !dir.exists() || attempt == attempts {
                return;
            }
            std::thread::sleep(std::time::Duration::from_millis(50));
        }
    }
}

impl Drop for EphemeralProfile {
    fn drop(&mut self) {
        self.remove(20);
    }
}

impl Drop for ClientState {
    /// A client freed without `close` is closed here the same way, before its fields drop: the
    /// browser, then the profile, so nothing of Chrome is left to write into the profile. Inside
    /// the runtime (an async call held the last reference) nothing may block, so Chrome is left to
    /// the browser's own drop, which kills it.
    fn drop(&mut self) {
        if self.is_closed() || tokio::runtime::Handle::try_current().is_ok() {
            return;
        }
        let runtime = self.runtime.clone();
        runtime.block_on(stop_chrome(self));
    }
}

impl ClientState {
    fn is_closed(&self) -> bool {
        self.closed.load(std::sync::atomic::Ordering::Acquire)
    }

    /// Reaps Chrome if it has exited and returns how; `None` while it runs. Never waits for
    /// the lock: a Chrome so wedged that a request holds it is not known to be dead.
    fn chrome_exit(&self) -> Option<String> {
        if let Some(seen) = self.exit.lock().clone() {
            return Some(seen);
        }
        let status = self.browser.try_write().ok()?.try_wait().ok()??;
        let text = status.to_string();
        *self.exit.lock() = Some(text.clone());
        Some(text)
    }

    /// How long a single call may wait for a free tab, if the client bounds it.
    fn queue_wait(&self) -> Option<Duration> {
        self.config
            .read()
            .timeout
            .queue_ms
            .map(Duration::from_millis)
    }

    /// Runs one call's work. A Chrome already known dead fails it at once; a failure from a
    /// Chrome that has since died is reported as that, not as the CDP error it caused.
    async fn guarded<T>(&self, work: impl Future<Output = Result<T>>) -> Result<T> {
        if let Some(status) = self.exit.lock().clone() {
            return Err(OnyxError::ChromeExited(status));
        }
        match work.await {
            Err(e @ (OnyxError::InvalidUrl(_) | OnyxError::InvalidConfig(_))) => Err(e),
            Err(e) => Err(self.chrome_exit().map_or(e, OnyxError::ChromeExited)),
            ok => ok,
        }
    }
}

#[pyclass]
pub struct Client {
    inner: Arc<ClientState>,
}

impl Client {
    fn check_open(&self) -> Result<()> {
        if self.inner.is_closed() {
            Err(OnyxError::Internal("Client is closed".to_string()))
        } else {
            Ok(())
        }
    }
}

// ---------------------------------------------------------------------------
// Async helpers — shared by sync (`block_on`) and async (`future_into_py`)
// entry points. Free functions so each can `state.clone()` ownership without
// borrowing `&self` across an await point.
// ---------------------------------------------------------------------------

async fn do_fetch_inner(
    state: Arc<ClientState>,
    url: String,
    fetch_cfg: FetchConfigRs,
) -> Result<RawRenderOutput> {
    let shot_cfg = ScreenshotConfigRs::default();
    state
        .guarded(async {
            let guard = state.pool.acquire(state.queue_wait()).await?;
            let base_cfg = state.config.read().clone();
            let out = capture_page(
                &guard,
                &url,
                &base_cfg,
                &fetch_cfg,
                &shot_cfg,
                CaptureMode::Html,
            )
            .await?;
            Ok(out.into())
        })
        .await
}

/// One browser tab held across several fetches, so a run of requests lands on the same tab and
/// meets what its own pages stored there — cookies, `localStorage`, a script's nonce — instead of
/// each landing on whichever tab the pool hands out next. The tab is leased on the first fetch
/// and returned to the pool when the session is closed or dropped.
///
/// A session adds no state to a tab; it chooses the tab. Every pooled tab keeps its own browser
/// context for as long as it lives, so what a site stored is already there for a later fetch that
/// lands on that tab — but which tab a fetch gets is the pool's choice, and with other work in
/// flight it is rarely the same one twice. A session is how a caller makes it the same one. When
/// it ends, the tab goes back as any fetch returns it, its context intact.
///
/// A fetch that fails does not cost the session its tab: the tab is reset to a blank page with
/// its context intact, so the next fetch still meets what the session's pages stored. Only a tab
/// the engine could not reset is replaced, and that is the one case where what it held is gone.
#[pyclass]
pub struct Session {
    inner: Arc<ClientState>,
    /// `None` until the first fetch leases a tab. Held across a fetch, so a session's own calls
    /// take turns on its one tab — which is what sharing a session means.
    held: Arc<tokio::sync::Mutex<Option<PageGuard>>>,
}

impl Session {
    fn check_open(&self) -> Result<()> {
        if self.inner.is_closed() {
            Err(OnyxError::Internal("Client is closed".to_string()))
        } else {
            Ok(())
        }
    }
}

/// Fetch on a session's own tab. The tab is leased on first use and kept across failures: only a
/// tab the engine could not reset is given up, because only then is what it held gone.
async fn do_session_fetch_inner(
    state: Arc<ClientState>,
    held: Arc<tokio::sync::Mutex<Option<PageGuard>>>,
    url: String,
    fetch_cfg: FetchConfigRs,
) -> Result<RawRenderOutput> {
    let shot_cfg = ScreenshotConfigRs::default();
    state
        .guarded(async {
            let mut slot = held.lock().await;
            if slot.is_none() {
                *slot = Some(state.pool.acquire(state.queue_wait()).await?);
            }
            let base_cfg = state.config.read().clone();
            let captured = {
                let guard = slot.as_ref().expect("a tab was leased just above");
                // The tab is held across fetches, so the per-fetch staging the pool would clear
                // when handing a tab out is cleared here, or the last fetch's `nav_error` would be
                // read again as this fetch's own.
                guard.start_fetch();
                capture_page(
                    guard,
                    &url,
                    &base_cfg,
                    &fetch_cfg,
                    &shot_cfg,
                    CaptureMode::Html,
                )
                .await
            };
            if captured.is_err() {
                // A failure the page survived leaves the tab on about:blank with its browser
                // context intact, so the session keeps it: what its pages stored is still there
                // for the next fetch. Only a tab the engine could not reset is given up, and the
                // pool recreates it before the next fetch.
                let gone = slot.as_ref().is_some_and(|guard| guard.is_poisoned());
                if gone {
                    *slot = None;
                }
            }
            Ok(captured?.into())
        })
        .await
}

#[pymethods]
impl Session {
    /// Fetch `url` on this session's tab, leasing the tab on first use.
    fn fetch(
        &self,
        py: Python<'_>,
        url: String,
        per_call: &Bound<'_, PyAny>,
    ) -> PyResult<RawRenderOutput> {
        self.check_open().map_err(PyErr::from)?;
        let fetch_cfg = parse_fetch_config(per_call).map_err(PyErr::from)?;
        let state = self.inner.clone();
        let held = self.held.clone();
        let runtime = state.runtime.clone();
        let url_err = url.clone();
        py.detach(move || runtime.block_on(do_session_fetch_inner(state, held, url, fetch_cfg)))
            .map_err(|e| e.into_py_err(py, &url_err))
    }

    /// Fetch `url` on this session's tab (async shape).
    fn fetch_async<'py>(
        &self,
        py: Python<'py>,
        url: String,
        per_call: &Bound<'_, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        self.check_open().map_err(PyErr::from)?;
        let fetch_cfg = parse_fetch_config(per_call).map_err(PyErr::from)?;
        let state = self.inner.clone();
        let held = self.held.clone();
        let url_err = url.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            do_session_fetch_inner(state, held, url, fetch_cfg)
                .await
                .map_err(|e| Python::attach(|py| e.into_py_err(py, &url_err)))
        })
    }

    /// Return this session's tab to the pool. Safe to call more than once.
    fn close(&self, py: Python<'_>) -> PyResult<()> {
        let held = self.held.clone();
        let runtime = self.inner.runtime.clone();
        py.detach(move || {
            runtime.block_on(async move {
                *held.lock().await = None;
            })
        });
        Ok(())
    }

    /// Return this session's tab to the pool (async shape).
    fn close_async<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let held = self.held.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            *held.lock().await = None;
            Ok(())
        })
    }
}

async fn do_screenshot_inner(
    state: Arc<ClientState>,
    url: String,
    shot_cfg: ScreenshotConfigRs,
) -> Result<Vec<u8>> {
    let fetch_cfg = FetchConfigRs::default();
    state
        .guarded(async {
            let guard = state.pool.acquire(state.queue_wait()).await?;
            let base_cfg = state.config.read().clone();
            let out = capture_page(
                &guard,
                &url,
                &base_cfg,
                &fetch_cfg,
                &shot_cfg,
                CaptureMode::Png,
            )
            .await?;
            Ok(out.png.unwrap_or_default())
        })
        .await
}

async fn do_fetch_all_inner(
    state: Arc<ClientState>,
    url: String,
    fetch_cfg: FetchConfigRs,
    shot_cfg: ScreenshotConfigRs,
) -> Result<RawFetchOutput> {
    state
        .guarded(async {
            let guard = state.pool.acquire(state.queue_wait()).await?;
            let base_cfg = state.config.read().clone();
            let out = capture_page(
                &guard,
                &url,
                &base_cfg,
                &fetch_cfg,
                &shot_cfg,
                CaptureMode::Both,
            )
            .await?;
            Ok(out.into())
        })
        .await
}

/// Run a batch of URLs in parallel. Returns `(url, Result)` per URL, in input
/// order — partial failures are surfaced per-item rather than aborting the
/// batch, and the URL is paired so a failure knows which fetch it was.
async fn do_batch_inner(
    state: Arc<ClientState>,
    urls: Vec<String>,
    fetch_cfg: FetchConfigRs,
    mode: CaptureMode,
) -> Vec<(String, std::result::Result<CaptureOutput, OnyxError>)> {
    let shot_cfg = ScreenshotConfigRs::default();
    // Snapshot config ONCE for the whole batch — in-batch updates don't re-apply.
    let base_cfg = state.config.read().clone();
    let tasks: Vec<_> = urls
        .into_iter()
        .map(|url| {
            let state = state.clone();
            let base = base_cfg.clone();
            let fc = fetch_cfg.clone();
            let sc = shot_cfg.clone();
            // The URL travels with its task so the result pairs back to it.
            tokio::spawn(async move {
                let r = state
                    .guarded(async {
                        let guard = state.pool.acquire(None).await?;
                        capture_page(&guard, &url, &base, &fc, &sc, mode).await
                    })
                    .await;
                (url, r)
            })
        })
        .collect();
    let mut collected = Vec::with_capacity(tasks.len());
    for h in tasks {
        match h.await {
            Ok(pair) => collected.push(pair),
            Err(e) => collected.push((
                String::new(),
                Err(OnyxError::Internal(format!("join: {e}"))),
            )),
        }
    }
    collected
}

/// Longest `close` waits for Chrome to shut down. A healthy shutdown takes about
/// 10 ms; this only bites when Chrome has stopped responding, and such a process
/// is still killed when the Client is freed.
const CLOSE_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(3);

async fn do_close_inner(state: Arc<ClientState>) {
    stop_chrome(&state).await;
    state.profile.remove(1);
}

/// Asks Chrome to exit and waits for it, within `CLOSE_TIMEOUT`: what `close` does, and what a
/// client freed without it gets too. Asked, Chrome shuts its helper processes down in order;
/// killed, they outlive it for a moment, and one can still write into the profile after the
/// profile was removed.
async fn stop_chrome(state: &ClientState) {
    // One budget for the whole shutdown: a frozen Chrome answers none of these
    // commands, and chromiumoxide's own request timeout doesn't bound them.
    let shutdown = async {
        state.pool.close_all().await;
        // The handler task ends only once the browser goes away, so tell Chrome
        // to exit before joining it.
        let _ = state
            .browser
            .read()
            .await
            .execute(CloseParams::default())
            .await;
        // `take()` detaches the JoinHandle so the lock isn't held across the join.
        let task = state.handler_task.lock().take();
        if let Some(task) = task {
            let _ = task.await;
        }
        // Collect the exited process now, or it stays a zombie until the Client is freed.
        let _ = state.browser.write().await.wait().await;
    };
    if tokio::time::timeout(CLOSE_TIMEOUT, shutdown).await.is_err() {
        log::warn!(
            target: "onyxweb::client",
            "Chrome did not shut down within {CLOSE_TIMEOUT:?}; it will be killed when the Client is freed"
        );
    }
}

/// Append one batch item to `list`. A success appends the raw output; a failure
/// appends the enriched exception INSTANCE (`.url` / `.kind`) — callers get a
/// `list[result | Exception]`, never a silent stub. Shared by sync + async.
fn batch_item_to_py(
    py: Python<'_>,
    url: &str,
    result: std::result::Result<CaptureOutput, OnyxError>,
    mode: CaptureMode,
    list: &Bound<'_, PyList>,
) -> PyResult<()> {
    match result {
        Ok(out) => match mode {
            CaptureMode::Html => list.append(RawRenderOutput::from(out)),
            CaptureMode::Png => list.append(PyBytes::new(py, &out.png.unwrap_or_default())),
            CaptureMode::Both => list.append(RawFetchOutput::from(out)),
        },
        Err(e) => {
            log::warn!("batch item failed ({url}): {e}");
            let exc = e.into_py_err(py, url).into_value(py);
            list.append(exc)
        }
    }
}

fn parse_capture_mode(capture: &str) -> PyResult<CaptureMode> {
    match capture {
        "html" => Ok(CaptureMode::Html),
        "png" => Ok(CaptureMode::Png),
        "both" => Ok(CaptureMode::Both),
        other => Err(pyo3::exceptions::PyValueError::new_err(format!(
            "capture must be 'html'|'png'|'both', got {other:?}"
        ))),
    }
}

#[pymethods]
impl Client {
    /// Constructor. Takes a dict form of `ClientConfig` (pydantic `.model_dump()`).
    /// Any field can be None to fall through to defaults.
    #[new]
    fn new(py: Python<'_>, config: &Bound<'_, PyAny>) -> PyResult<Self> {
        let config_rs = parse_client_config(config).map_err(PyErr::from)?;
        let chrome_path =
            chrome::resolve(config_rs.chrome.path.as_deref(), config_rs.chrome.engine)
                .map_err(PyErr::from)?;
        let chrome_display = chrome_path.display().to_string();

        let runtime = runtime::shared();

        // Launch flags diverge by engine: the bundled shell keeps its legacy
        // flag set; the full engine gets a clean new-headless launch with the
        // automation tells stripped (see build_full_launch).
        let (user_data_dir, ephemeral) = resolve_user_data_dir(&config_rs);
        // Owned from here, so a launch that fails still takes the profile it started with it.
        let profile = EphemeralProfile(ephemeral.then(|| user_data_dir.clone()));
        // The wrapper makes Chrome die with an abruptly-killed process; absent (dev
        // build, unsupported platform), Chrome launches directly and unprotected.
        let wrapper_path = chrome::resolve_wrapper();
        let launch_target = wrapper_path.as_deref().unwrap_or(&chrome_path);
        let mut builder = BrowserConfig::builder()
            .chrome_executable(launch_target)
            .user_data_dir(&user_data_dir);
        if wrapper_path.is_some() {
            builder = builder.env("ONYXWEB_REAL_CHROME_PATH", chrome_display.clone());
        } else {
            log::warn!(
                target: "onyxweb::client",
                "onyxweb_wrapper not bundled; launching chrome directly (it may survive an abrupt kill of this process)"
            );
        }
        builder = match config_rs.chrome.engine {
            ChromeEngine::HeadlessShell => build_shell_launch(builder, &config_rs),
            ChromeEngine::Full => build_full_launch(builder, &config_rs, &chrome_path),
        };
        // Chromium's own CDP client ignores certificate errors unless told otherwise, so the
        // config field has to be passed on for `ignore_https_errors=False` to mean anything.
        if !config_rs.network.ignore_https_errors {
            builder = builder.respect_https_errors();
        }
        if !config_rs.chrome.sandbox {
            builder = builder.no_sandbox();
        }
        let sandbox = config_rs.chrome.sandbox;

        log::info!(
            target: "onyxweb::client",
            "launching chrome ({} concurrency, viewport {}x{}, chrome={})",
            config_rs.concurrency,
            config_rs.viewport.width,
            config_rs.viewport.height,
            chrome_display
        );

        let cfg = builder
            .build()
            .map_err(|e| OnyxError::LaunchFailed(e.to_string()))?;

        let concurrency = config_rs.concurrency.max(1);
        let launch_ms = config_rs.timeout.launch_ms;
        let shared_config = Arc::new(parking_lot::RwLock::new(config_rs));
        let pool_config = shared_config.clone();
        let (browser, handler_task, pool) = py
            .detach(|| {
                runtime.block_on(async {
                    let (browser, mut handler) = tokio::time::timeout(
                        Duration::from_millis(launch_ms),
                        Browser::launch(cfg),
                    )
                    .await
                    .map_err(|_| {
                        OnyxError::Timeout(format!("chrome did not launch within {launch_ms}ms"))
                    })?
                    .map_err(|e| launch_error(e, sandbox))?;
                    let task = tokio::spawn(async move {
                        while let Some(res) = handler.next().await {
                            if res.is_err() {
                                // Handler ended — browser will report errors on the next page op.
                                break;
                            }
                        }
                    });
                    let browser = Arc::new(tokio::sync::RwLock::new(browser));
                    let pool = PagePool::new(browser.clone(), concurrency, pool_config).await?;
                    Ok::<_, OnyxError>((browser, task, pool))
                })
            })
            .map_err(PyErr::from)?;

        let state = ClientState {
            runtime: runtime.clone(),
            browser,
            exit: parking_lot::Mutex::new(None),
            pool,
            handler_task: parking_lot::Mutex::new(Some(handler_task)),
            config: shared_config,
            closed: std::sync::atomic::AtomicBool::new(false),
            profile,
        };

        Ok(Self {
            inner: Arc::new(state),
        })
    }

    /// Swap in a new config. Launch-only fields are validated Python-side
    /// before this call, which is only made when something changed. Every
    /// pooled tab is rebuilt on its next acquire: a tab applies UA, emulation,
    /// scripts, blocks and headers once, when it is created, so a tab kept
    /// from before the change would ignore it.
    fn update_config(&self, config: &Bound<'_, PyAny>) -> PyResult<()> {
        self.check_open().map_err(PyErr::from)?;
        let new_cfg = parse_client_config(config).map_err(PyErr::from)?;
        *self.inner.config.write() = new_cfg;
        self.inner.pool.bump_generation();
        log::debug!(target: "onyxweb::client", "update_config applied; tabs rebuild on next acquire");
        Ok(())
    }

    /// Open a session: a run of fetches that share one tab, and so share what the first left in
    /// it. The tab is leased on the first fetch; closing the session returns it to the pool.
    fn session(&self) -> PyResult<Session> {
        self.check_open().map_err(PyErr::from)?;
        Ok(Session {
            inner: self.inner.clone(),
            held: Arc::new(tokio::sync::Mutex::new(None)),
        })
    }

    // -----------------------------------------------------------------------
    // Sync API — `py.detach + block_on(do_*_inner(...))`.
    // -----------------------------------------------------------------------

    /// Fetch URL → RawRenderOutput (HTML only).
    fn fetch(
        &self,
        py: Python<'_>,
        url: String,
        per_call: &Bound<'_, PyAny>,
    ) -> PyResult<RawRenderOutput> {
        self.check_open().map_err(PyErr::from)?;
        let fetch_cfg = parse_fetch_config(per_call).map_err(PyErr::from)?;
        let state = self.inner.clone();
        let runtime = state.runtime.clone();
        let url_err = url.clone();
        py.detach(move || runtime.block_on(do_fetch_inner(state, url, fetch_cfg)))
            .map_err(|e| e.into_py_err(py, &url_err))
    }

    /// Screenshot URL → image bytes (png/jpeg/webp depending on per_shot.format).
    fn screenshot<'py>(
        &self,
        py: Python<'py>,
        url: String,
        per_shot: &Bound<'_, PyAny>,
    ) -> PyResult<Bound<'py, PyBytes>> {
        self.check_open().map_err(PyErr::from)?;
        let shot_cfg = parse_screenshot_config(per_shot).map_err(PyErr::from)?;
        let state = self.inner.clone();
        let runtime = state.runtime.clone();
        let url_err = url.clone();
        let png = py
            .detach(move || runtime.block_on(do_screenshot_inner(state, url, shot_cfg)))
            .map_err(|e| e.into_py_err(py, &url_err))?;
        Ok(PyBytes::new(py, &png))
    }

    /// Fetch URL → RawFetchOutput (HTML + image from one visit).
    fn fetch_all(
        &self,
        py: Python<'_>,
        url: String,
        per_call: &Bound<'_, PyAny>,
        per_shot: &Bound<'_, PyAny>,
    ) -> PyResult<RawFetchOutput> {
        self.check_open().map_err(PyErr::from)?;
        let fetch_cfg = parse_fetch_config(per_call).map_err(PyErr::from)?;
        let shot_cfg = parse_screenshot_config(per_shot).map_err(PyErr::from)?;
        let state = self.inner.clone();
        let runtime = state.runtime.clone();
        let url_err = url.clone();
        py.detach(move || runtime.block_on(do_fetch_all_inner(state, url, fetch_cfg, shot_cfg)))
            .map_err(|e| e.into_py_err(py, &url_err))
    }

    /// Batch of URLs (parallel inside Rust tokio). Returns list of results
    /// matching the `capture` mode: "html" → list[RawRenderOutput],
    /// "png" → list[bytes], "both" → list[RawFetchOutput].
    fn batch<'py>(
        &self,
        py: Python<'py>,
        urls: Vec<String>,
        capture: &str,
        per_call: &Bound<'_, PyAny>,
    ) -> PyResult<Bound<'py, PyList>> {
        self.check_open().map_err(PyErr::from)?;
        log::debug!(
            target: "onyxweb::client",
            "batch dispatch: {} URLs, capture={capture}",
            urls.len()
        );
        let mode = parse_capture_mode(capture)?;
        let fetch_cfg = parse_fetch_config(per_call).map_err(PyErr::from)?;
        let state = self.inner.clone();
        let runtime = state.runtime.clone();

        let outputs =
            py.detach(move || runtime.block_on(do_batch_inner(state, urls, fetch_cfg, mode)));

        let results = PyList::empty(py);
        for (url, r) in outputs {
            batch_item_to_py(py, &url, r, mode, &results)?;
        }
        Ok(results)
    }

    /// Explicit shutdown. Closes pooled pages, tells Chrome to exit (chromium
    /// quits), and joins the handler task.
    fn close(&self, py: Python<'_>) -> PyResult<()> {
        if self.inner.is_closed() {
            return Ok(());
        }
        log::info!(target: "onyxweb::client", "Client.close");
        self.inner
            .closed
            .store(true, std::sync::atomic::Ordering::Release);
        let state = self.inner.clone();
        let runtime = state.runtime.clone();
        py.detach(move || {
            runtime.block_on(do_close_inner(state));
        });
        Ok(())
    }

    /// False once `close` was called or Chrome has exited. Never waits: a Chrome frozen or
    /// busy enough that a request holds its lock still reads as alive.
    #[getter]
    fn alive(&self) -> bool {
        !self.inner.is_closed() && self.inner.chrome_exit().is_none()
    }

    fn __enter__(slf: Py<Self>) -> Py<Self> {
        slf
    }

    #[pyo3(signature = (_exc_type=None, _exc_val=None, _exc_tb=None))]
    fn __exit__(
        &self,
        py: Python<'_>,
        _exc_type: Option<Py<PyAny>>,
        _exc_val: Option<Py<PyAny>>,
        _exc_tb: Option<Py<PyAny>>,
    ) -> PyResult<()> {
        self.close(py)
    }

    // -----------------------------------------------------------------------
    // Async API — `pyo3_async_runtimes::tokio::future_into_py(do_*_inner(...))`.
    // Returns Python awaitables. The Python-side `AsyncClient` wraps these.
    // -----------------------------------------------------------------------

    /// Fetch URL → awaitable → RawRenderOutput.
    fn fetch_async<'py>(
        &self,
        py: Python<'py>,
        url: String,
        per_call: &Bound<'_, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        self.check_open().map_err(PyErr::from)?;
        let fetch_cfg = parse_fetch_config(per_call).map_err(PyErr::from)?;
        let state = self.inner.clone();
        let url_err = url.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            do_fetch_inner(state, url, fetch_cfg)
                .await
                .map_err(|e| Python::attach(|py| e.into_py_err(py, &url_err)))
        })
    }

    /// Screenshot URL → awaitable → bytes.
    fn screenshot_async<'py>(
        &self,
        py: Python<'py>,
        url: String,
        per_shot: &Bound<'_, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        self.check_open().map_err(PyErr::from)?;
        let shot_cfg = parse_screenshot_config(per_shot).map_err(PyErr::from)?;
        let state = self.inner.clone();
        let url_err = url.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let bytes = do_screenshot_inner(state, url, shot_cfg)
                .await
                .map_err(|e| Python::attach(|py| e.into_py_err(py, &url_err)))?;
            Python::attach(|py| -> PyResult<Py<PyBytes>> { Ok(PyBytes::new(py, &bytes).unbind()) })
        })
    }

    /// Fetch URL → awaitable → RawFetchOutput (HTML + image from one visit).
    fn fetch_all_async<'py>(
        &self,
        py: Python<'py>,
        url: String,
        per_call: &Bound<'_, PyAny>,
        per_shot: &Bound<'_, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        self.check_open().map_err(PyErr::from)?;
        let fetch_cfg = parse_fetch_config(per_call).map_err(PyErr::from)?;
        let shot_cfg = parse_screenshot_config(per_shot).map_err(PyErr::from)?;
        let state = self.inner.clone();
        let url_err = url.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            do_fetch_all_inner(state, url, fetch_cfg, shot_cfg)
                .await
                .map_err(|e| Python::attach(|py| e.into_py_err(py, &url_err)))
        })
    }

    /// Batch URLs → awaitable → list. Same shape as sync `batch()`.
    fn batch_async<'py>(
        &self,
        py: Python<'py>,
        urls: Vec<String>,
        capture: &str,
        per_call: &Bound<'_, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        self.check_open().map_err(PyErr::from)?;
        log::debug!(
            target: "onyxweb::client",
            "batch_async dispatch: {} URLs, capture={capture}",
            urls.len()
        );
        let mode = parse_capture_mode(capture)?;
        let fetch_cfg = parse_fetch_config(per_call).map_err(PyErr::from)?;
        let state = self.inner.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let outputs = do_batch_inner(state, urls, fetch_cfg, mode).await;
            Python::attach(|py| -> PyResult<Py<PyList>> {
                let results = PyList::empty(py);
                for (url, r) in outputs {
                    batch_item_to_py(py, &url, r, mode, &results)?;
                }
                Ok(results.unbind())
            })
        })
    }

    /// Explicit shutdown → awaitable → None.
    fn close_async<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        if self.inner.is_closed() {
            // Already closed — return an immediately-resolved awaitable so
            // double-close doesn't error.
            return pyo3_async_runtimes::tokio::future_into_py(py, async { Ok(()) });
        }
        log::info!(target: "onyxweb::client", "Client.close_async");
        self.inner
            .closed
            .store(true, std::sync::atomic::Ordering::Release);
        let state = self.inner.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            do_close_inner(state).await;
            Ok(())
        })
    }
}
