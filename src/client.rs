//! `Client` pyclass — the main entry point. Owns a chromiumoxide Browser and a
//! Semaphore, dispatches Python calls to the shared tokio runtime.
//!
//! Two callable shapes, one implementation:
//! - **Sync methods** (`fetch`, `screenshot`, `fetch_all`, `batch`, `close`)
//!   release the GIL via `py.allow_threads()` and `block_on()` the work on
//!   the shared runtime. N Python threads can call them concurrently and
//!   make real parallel progress; the page-pool semaphore caps in-flight
//!   pages at `concurrency`.
//! - **Async methods** (`fetch_async`, `screenshot_async`, `fetch_all_async`,
//!   `batch_async`, `close_async`) bridge to Python via
//!   `pyo3_async_runtimes::tokio::future_into_py`. They return Python
//!   awaitables that callers `await` from an asyncio event loop. No
//!   `allow_threads` needed — the bridge handles GIL release.
//!
//! Both forms route through the same `do_*_inner` async helpers, so there's
//! exactly one implementation of each operation and two callable shapes.

use std::path::Path;
use std::sync::Arc;

use chromiumoxide::browser::BrowserConfigBuilder;
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
use crate::pool::PagePool;
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

/// Legacy launch flags for the bundled `chrome-headless-shell` engine. Kept
/// verbatim (including the `--`-prefixed args, which chromiumoxide renders
/// inertly) so the default engine's behavior is unchanged.
fn build_shell_launch(builder: BrowserConfigBuilder, cfg: &ClientConfigRs) -> BrowserConfigBuilder {
    let mut b = builder
        .arg("--headless=new")
        .arg("--disable-gpu")
        .arg("--no-sandbox")
        .arg("--hide-scrollbars")
        .arg("--disable-dev-shm-usage")
        .arg("--no-first-run")
        .arg("--no-default-browser-check")
        .arg("--disable-background-networking")
        .arg("--disable-background-timer-throttling")
        .arg("--disable-backgrounding-occluded-windows")
        .arg("--disable-breakpad")
        .arg("--disable-client-side-phishing-detection")
        .arg("--disable-component-extensions-with-background-pages")
        .arg("--disable-component-update")
        .arg("--disable-default-apps")
        .arg("--disable-domain-reliability")
        .arg("--disable-extensions")
        .arg("--disable-features=Translate,BackForwardCache,AcceptCHFrame,MediaRouter,OptimizationHints,IsolateOrigins,site-per-process")
        .arg("--disable-hang-monitor")
        .arg("--disable-ipc-flooding-protection")
        .arg("--disable-popup-blocking")
        .arg("--disable-prompt-on-repost")
        .arg("--disable-renderer-backgrounding")
        .arg("--disable-sync")
        .arg("--metrics-recording-only")
        .arg("--mute-audio")
        .arg("--password-store=basic")
        .arg("--use-mock-keychain")
        .arg(format!(
            "--window-size={},{}",
            cfg.viewport.width, cfg.viewport.height
        ));
    if cfg.network.ignore_https_errors {
        b = b.arg("--ignore-certificate-errors");
    }
    if let Some(user_data_dir) = &cfg.chrome.user_data_dir {
        b = b.arg(format!("--user-data-dir={user_data_dir}"));
    }
    for arg in &cfg.chrome.args {
        b = b.arg(arg.clone());
    }
    b
}

/// Full-Chrome launch (`--headless=new`): drop chromiumoxide's default
/// `--enable-automation`, disable the `AutomationControlled` blink feature (so
/// `navigator.webdriver === false`), and set a real Chrome UA via a launch flag
/// (a CDP UA override presents differently and gets flagged). Flag keys are
/// passed WITHOUT `--` so chromiumoxide renders them correctly.
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
        .unwrap_or_else(|| FALLBACK_FULL_UA.to_string());
    let mut b = builder
        .new_headless_mode()
        .disable_default_args()
        .no_sandbox()
        .arg("disable-gpu")
        // Software WebGL: without it --disable-gpu leaves getContext('webgl')
        // === null, itself a headless tell.
        .arg("enable-unsafe-swiftshader")
        .arg("disable-dev-shm-usage")
        .arg("disable-blink-features=AutomationControlled")
        .arg(format!("user-agent={ua}"))
        .arg(format!(
            "window-size={},{}",
            cfg.viewport.width, cfg.viewport.height
        ));
    if cfg.network.ignore_https_errors {
        b = b.arg("ignore-certificate-errors");
    }
    if let Some(user_data_dir) = &cfg.chrome.user_data_dir {
        b = b.arg(format!("user-data-dir={user_data_dir}"));
    }
    for arg in &cfg.chrome.args {
        let a = arg.strip_prefix("--").unwrap_or(arg);
        b = b.arg(a.to_string());
    }
    b
}

/// Fallback UA if the binary version can't be read. Linux desktop Chrome.
const FALLBACK_FULL_UA: &str = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 \
     (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36";

/// Derive a real Chrome UA from the binary's `--version` (so the UA's major
/// matches the actual build — a UA/binary mismatch is itself a tell). Linux
/// desktop shape; returns None if the version can't be parsed.
fn derive_chrome_ua(chrome_path: &Path) -> Option<String> {
    let out = std::process::Command::new(chrome_path)
        .arg("--version")
        .output()
        .ok()?;
    let text = String::from_utf8_lossy(&out.stdout);
    // e.g. "Chromium 147.0.7727.137" / "Google Chrome 148.0.7778.56"
    let version = text
        .split_whitespace()
        .find(|t| t.chars().next().is_some_and(|c| c.is_ascii_digit()))?;
    let major = version.split('.').next()?;
    Some(format!(
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) \
         Chrome/{major}.0.0.0 Safari/537.36"
    ))
}

/// Chromium Browser + page pool + handler task. `config` is RwLock-wrapped so
/// `update_config` can swap atomically without blocking in-flight fetches.
struct ClientState {
    runtime: Arc<tokio::runtime::Runtime>,
    /// Keeps the browser process alive while the pool exists.
    #[allow(dead_code)]
    browser: Arc<Browser>,
    pool: Arc<PagePool>,
    handler_task: parking_lot::Mutex<Option<tokio::task::JoinHandle<()>>>,
    config: Arc<parking_lot::RwLock<ClientConfigRs>>,
    closed: std::sync::atomic::AtomicBool,
}

impl ClientState {
    fn is_closed(&self) -> bool {
        self.closed.load(std::sync::atomic::Ordering::Acquire)
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
    let guard = state.pool.acquire().await?;
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
}

async fn do_screenshot_inner(
    state: Arc<ClientState>,
    url: String,
    shot_cfg: ScreenshotConfigRs,
) -> Result<Vec<u8>> {
    let fetch_cfg = FetchConfigRs::default();
    let guard = state.pool.acquire().await?;
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
}

async fn do_fetch_all_inner(
    state: Arc<ClientState>,
    url: String,
    fetch_cfg: FetchConfigRs,
    shot_cfg: ScreenshotConfigRs,
) -> Result<RawFetchOutput> {
    let guard = state.pool.acquire().await?;
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
            let pool = state.pool.clone();
            let base = base_cfg.clone();
            let fc = fetch_cfg.clone();
            let sc = shot_cfg.clone();
            // The URL travels with its task so the result pairs back to it.
            tokio::spawn(async move {
                let r = async {
                    let guard = pool.acquire().await?;
                    capture_page(&guard, &url, &base, &fc, &sc, mode).await
                }
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

async fn do_close_inner(state: Arc<ClientState>) {
    state.pool.close_all().await;
    // Drop the MutexGuard before any await — `take()` detaches the
    // JoinHandle so we can join it without holding the lock.
    let task_opt = state.handler_task.lock().take();
    if let Some(task) = task_opt {
        let _ = tokio::time::timeout(std::time::Duration::from_secs(3), task).await;
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
        let mut builder = BrowserConfig::builder().chrome_executable(&chrome_path);
        builder = match config_rs.chrome.engine {
            ChromeEngine::HeadlessShell => build_shell_launch(builder, &config_rs),
            ChromeEngine::Full => build_full_launch(builder, &config_rs, &chrome_path),
        };

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
        let shared_config = Arc::new(parking_lot::RwLock::new(config_rs));
        let pool_config = shared_config.clone();
        let (browser, handler_task, pool) = py
            .allow_threads(|| {
                runtime.block_on(async {
                    let (browser, mut handler) =
                        Browser::launch(cfg).await.map_err(OnyxError::from)?;
                    let task = tokio::spawn(async move {
                        while let Some(res) = handler.next().await {
                            if res.is_err() {
                                // Handler ended — browser will report errors on the next page op.
                                break;
                            }
                        }
                    });
                    let browser = Arc::new(browser);
                    let pool = PagePool::new(browser.clone(), concurrency, pool_config).await?;
                    Ok::<_, OnyxError>((browser, task, pool))
                })
            })
            .map_err(PyErr::from)?;

        let state = ClientState {
            runtime: runtime.clone(),
            browser,
            pool,
            handler_task: parking_lot::Mutex::new(Some(handler_task)),
            config: shared_config,
            closed: std::sync::atomic::AtomicBool::new(false),
        };

        Ok(Self {
            inner: Arc::new(state),
        })
    }

    /// Swap in a new config. Launch-only fields are validated Python-side
    /// before this call — we just replace atomically. Next fetch sees the
    /// new values.
    fn update_config(&self, config: &Bound<'_, PyAny>) -> PyResult<()> {
        self.check_open().map_err(PyErr::from)?;
        let new_cfg = parse_client_config(config).map_err(PyErr::from)?;
        log::debug!(target: "onyxweb::client", "update_config applied");
        let mut guard = self.inner.config.write();
        let ctx_changed = guard.network.proxy != new_cfg.network.proxy
            || guard.network.proxy_bypass_list != new_cfg.network.proxy_bypass_list;
        *guard = new_cfg;
        drop(guard);
        if ctx_changed {
            self.inner.pool.bump_generation();
        }
        Ok(())
    }

    // -----------------------------------------------------------------------
    // Sync API — `py.allow_threads + block_on(do_*_inner(...))`.
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
        py.allow_threads(move || runtime.block_on(do_fetch_inner(state, url, fetch_cfg)))
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
            .allow_threads(move || runtime.block_on(do_screenshot_inner(state, url, shot_cfg)))
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
        py.allow_threads(move || {
            runtime.block_on(do_fetch_all_inner(state, url, fetch_cfg, shot_cfg))
        })
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

        let outputs = py
            .allow_threads(move || runtime.block_on(do_batch_inner(state, urls, fetch_cfg, mode)));

        let results = PyList::empty(py);
        for (url, r) in outputs {
            batch_item_to_py(py, &url, r, mode, &results)?;
        }
        Ok(results)
    }

    /// Explicit shutdown. Closes pooled pages, drops the Browser (chromium
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
        py.allow_threads(move || {
            runtime.block_on(do_close_inner(state));
        });
        Ok(())
    }

    fn __enter__(slf: Py<Self>) -> Py<Self> {
        slf
    }

    #[pyo3(signature = (_exc_type=None, _exc_val=None, _exc_tb=None))]
    fn __exit__(
        &self,
        py: Python<'_>,
        _exc_type: Option<PyObject>,
        _exc_val: Option<PyObject>,
        _exc_tb: Option<PyObject>,
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
                .map_err(|e| Python::with_gil(|py| e.into_py_err(py, &url_err)))
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
                .map_err(|e| Python::with_gil(|py| e.into_py_err(py, &url_err)))?;
            Python::with_gil(|py| -> PyResult<Py<PyBytes>> {
                Ok(PyBytes::new(py, &bytes).unbind())
            })
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
                .map_err(|e| Python::with_gil(|py| e.into_py_err(py, &url_err)))
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
            Python::with_gil(|py| -> PyResult<Py<PyList>> {
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
