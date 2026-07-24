//! Pre-warmed pool of chromium pages — pre-created at Client launch with base
//! config + listeners applied once. Each fetch navigates an existing page and
//! returns it to the pool, avoiding the ~50-150ms per-URL new_page tax.

use std::collections::HashMap;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use chromiumoxide::cdp::browser_protocol::browser::BrowserContextId;
use chromiumoxide::cdp::browser_protocol::emulation::{
    MediaFeature, SetDeviceMetricsOverrideParams, SetEmulatedMediaParams,
    SetGeolocationOverrideParams, SetLocaleOverrideParams, SetScriptExecutionDisabledParams,
    SetTimezoneOverrideParams, UserAgentBrandVersion, UserAgentMetadata,
};
// EmulateNetworkConditionsParams is deprecated upstream in chromiumoxide
// 0.9 (CDP renamed it). The replacement isn't yet exported; allow the
// deprecation until the upstream type lands.
#[allow(deprecated)]
use chromiumoxide::cdp::browser_protocol::network::EmulateNetworkConditionsParams;
use chromiumoxide::cdp::browser_protocol::network::{
    BlockPattern, DeleteCookiesParams, Headers, SetBlockedUrLsParams, SetCacheDisabledParams,
    SetExtraHttpHeadersParams, SetUserAgentOverrideParams,
};
use chromiumoxide::cdp::browser_protocol::page::{
    AddScriptToEvaluateOnNewDocumentParams, EventFrameNavigated, EventNavigatedWithinDocument,
};
use chromiumoxide::cdp::browser_protocol::target::{
    CreateBrowserContextParams, CreateTargetParams,
};
use chromiumoxide::{Browser, Page};
use futures::StreamExt;
use parking_lot::Mutex;
use tokio::sync::{OwnedSemaphorePermit, Semaphore};

use crate::config::{CaptureConsoleLevel, ClientConfigRs, UserAgentMetadataRs};
use crate::error::{OnyxError, Result};
use crate::result::ConsoleMessageRs;

/// Name of the isolated JS world used for ``scripts.isolated_world``
/// registrations. Page JS cannot read or tamper with globals defined here.
const DEFAULT_ISOLATED_WORLD_NAME: &str = "util";

/// Build a `Vec<BlockPattern>` for `Network.setBlockedURLs` from a slice of
/// URL pattern strings. URLPattern syntax (`*://*.doubleclick.net/*`),
/// case-sensitive matching enabled.
pub(crate) fn block_patterns(urls: &[String]) -> Vec<BlockPattern> {
    urls.iter()
        .map(|p| BlockPattern::new(p.clone(), true))
        .collect()
}

/// Anti-bot cookie names (the "bot verdict" carriers) that self-heal drops
/// before a retry, keeping all other cookies. Covers Akamai (`_abck`, `bm_*`,
/// `ak_bmsc`), PerimeterX (`_px*`), DataDome, Cloudflare.
fn is_antibot_cookie(name: &str) -> bool {
    let n = name.to_ascii_lowercase();
    n.starts_with("bm_")
        || n.starts_with("_px")
        || matches!(
            n.as_str(),
            "_abck" | "ak_bmsc" | "datadome" | "cf_clearance" | "__cf_bm"
        )
}

/// Snapshot of the main-document HTTP response. Status + metadata + cert come
/// from `Network.responseReceived`; the full headers (incl. Set-Cookie) come
/// from `Network.responseReceivedExtraInfo`, merged in by matching
/// `(request_id, status)`. Everything onyxweb surfaces as
/// `RenderResult.metadata` / `.headers` derives from here.
#[derive(Clone, Debug, Default)]
pub struct MainResponseRs {
    pub status: u16,
    pub status_text: String,
    pub mime_type: String,
    /// Negotiated protocol, e.g. `"http/1.1"`, `"h2"`, `"h3"`. Empty when the
    /// browser didn't report one (data: URLs, some cached responses).
    pub protocol: String,
    pub remote_ip: Option<String>,
    pub remote_port: Option<u16>,
    /// CDP request id — correlates `responseReceived` with its `extraInfo`.
    pub request_id: String,
    /// Response headers as (name, value) pairs (duplicates preserved). From
    /// `responseReceivedExtraInfo` when available (includes Set-Cookie);
    /// otherwise the parsed `responseReceived.response.headers` (no Set-Cookie).
    pub headers: Vec<(String, String)>,
    /// TLS certificate info (from `securityDetails`). `None` for plain HTTP.
    pub cert_info: Option<CertInfoRs>,
}

/// One redirect hop on the way to the final main-document response, captured
/// from `Network.requestWillBeSent`'s `redirectResponse`. Mirrors blasthttp's
/// `RedirectHop`.
#[derive(Clone, Debug)]
pub struct RedirectHopRs {
    pub url: String,
    pub status: u16,
    pub remote_ip: Option<String>,
}

/// TLS certificate info from the final response's `securityDetails`. `valid_*`
/// are epoch seconds (Python formats them to ISO 8601). Mirrors blasthttp's
/// `CertInfo`. `None` for plain HTTP.
#[derive(Clone, Debug)]
pub struct CertInfoRs {
    pub common_name: String,
    pub sans: Vec<String>,
    pub issuer: String,
    pub valid_from: f64,
    pub valid_to: f64,
}

/// Staged `responseReceivedExtraInfo` payload, held until the matching
/// `responseReceived` arrives (the two events fire in either order).
#[derive(Clone, Debug, Default)]
struct ExtraInfoRs {
    headers: Vec<(String, String)>,
}

/// Per-tab response tracking, guarded by one mutex so the `responseReceived`
/// and `responseReceivedExtraInfo` listener tasks (plus `acquire`) can never
/// race on lock ordering.
#[derive(Default)]
pub struct ResponseState {
    /// Current fetch's main-doc response (None until headers arrive).
    main: Option<MainResponseRs>,
    /// Prior completed fetch's response — same-doc navs (no new HTTP response)
    /// fall back to this.
    prev: Option<MainResponseRs>,
    /// extraInfo received before its `responseReceived`, keyed by
    /// `(request_id, status)`. Redirect hops reuse the request id, so status is
    /// part of the key — otherwise a 301 hop's headers could wrongly bind to
    /// the final response.
    pending_extra: HashMap<(String, u16), ExtraInfoRs>,
    /// Main-document redirect hops for the current fetch, in order.
    redirects: Vec<RedirectHopRs>,
    /// Main-document network failure (DNS, connection refused, ...), if any.
    nav_error: Option<String>,
}

/// Convert a CDP `Headers` (JSON object) into (name, value) pairs. CDP joins
/// duplicate headers (e.g. multiple Set-Cookie) into one key with values
/// concatenated by `\n`; we split them back into one pair per occurrence so
/// `raw_headers` renders one line each — matching blasthttp's shape.
/// Non-string values (rare) are stringified.
fn headers_to_vec(h: &Headers) -> Vec<(String, String)> {
    let Ok(serde_json::Value::Object(map)) = serde_json::to_value(h) else {
        return Vec::new();
    };
    let mut out = Vec::with_capacity(map.len());
    for (k, v) in map {
        let val = match v {
            serde_json::Value::String(s) => s,
            other => other.to_string(),
        };
        for part in val.split('\n') {
            out.push((k.clone(), part.to_string()));
        }
    }
    out
}

/// One pooled page + its persistent console-message and main-doc-status
/// collectors. Console messages flow in from ``Runtime.consoleAPICalled`` and
/// ``Runtime.exceptionThrown`` listeners spawned at page creation; drained
/// per-fetch by `engine::capture_page`.
pub struct PooledPage {
    pub page: Page,
    /// This tab's isolated browser context (own cookie jar + storage). Target of
    /// context-scoped cookie/storage clears.
    pub browser_context_id: BrowserContextId,
    pub console_messages: Arc<Mutex<Vec<ConsoleMessageRs>>>,
    /// Main-doc response tracking (current + previous response, staged
    /// extraInfo). Populated by the `responseReceived` /
    /// `responseReceivedExtraInfo` listeners; see `ResponseState`.
    pub response: Arc<Mutex<ResponseState>>,
    /// Latest URL the tab is at — updated by long-lived listeners on
    /// `Page.frameNavigated` (full nav) and `Page.navigatedWithinDocument`
    /// (same-doc nav). Used by `engine::capture_page` to detect when a
    /// requested fetch is a same-document navigation, which needs a different
    /// code path than `Page.navigate` (chromiumoxide's command future hangs
    /// for hash-only URLs after a previous nav on the same tab).
    pub current_url: Arc<Mutex<Option<String>>>,
    /// Set when a fetch wedged or killed this tab; `acquire()` recreates it.
    pub poisoned: AtomicBool,
}

/// Pool sized to the Client's `concurrency`. `acquire()` returns page + permit
/// together; excess callers queue on the semaphore.
pub struct PagePool {
    /// For per-tab context create/dispose + context-scoped cookie clears.
    browser: Arc<Browser>,
    pages: Mutex<Vec<PooledPage>>,
    sem: Arc<Semaphore>,
    /// Base config, kept so a poisoned tab can be recreated on acquire.
    config: ClientConfigRs,
    #[allow(dead_code)]
    size: usize,
}

impl PagePool {
    /// Create `size` pages in parallel, each with base config applied and
    /// console/exception listeners wired up.
    pub async fn new(
        browser: Arc<Browser>,
        size: usize,
        base: &ClientConfigRs,
    ) -> Result<Arc<Self>> {
        let t0 = std::time::Instant::now();
        log::info!(target: "onyxweb::pool", "creating pool of {size} pages");
        let futs = (0..size).map(|_| create_pooled_page(&browser, base));
        let created: Vec<PooledPage> = futures::future::try_join_all(futs).await?;
        log::info!(
            target: "onyxweb::pool",
            "pool of {size} pages ready in {:?}",
            t0.elapsed()
        );
        Ok(Arc::new(Self {
            browser,
            pages: Mutex::new(created),
            sem: Arc::new(Semaphore::new(size)),
            config: base.clone(),
            size,
        }))
    }

    #[allow(dead_code)]
    pub fn size(&self) -> usize {
        self.size
    }

    /// Acquire a page (waits on Semaphore if pool is saturated).
    pub async fn acquire(self: &Arc<Self>) -> Result<PageGuard> {
        let t0 = std::time::Instant::now();
        let permit = self
            .sem
            .clone()
            .acquire_owned()
            .await
            .map_err(|e| OnyxError::Internal(format!("pool sem: {e}")))?;
        let mut pooled = self
            .pages
            .lock()
            .pop()
            .expect("semaphore permitted but pool is empty");
        if pooled.poisoned.load(Ordering::Acquire) {
            log::debug!(target: "onyxweb::pool", "recreating poisoned pooled tab");
            let _ = tokio::time::timeout(Duration::from_secs(5), pooled.page.close()).await;
            let _ = self
                .browser
                .dispose_browser_context(pooled.browser_context_id.clone())
                .await;
            pooled = create_pooled_page(&self.browser, &self.config).await?;
        }
        pooled.console_messages.lock().clear();
        // Snapshot previous response before clearing — same-doc navs (no new
        // HTTP response) propagate the prior document's metadata. Only shift
        // into `prev` if `main` was actually populated by the prior fetch;
        // otherwise keep the existing prev so chains of same-doc navs (each
        // leaving `main` unset) all see the document's original response.
        // `pending_extra` is always cleared — it's per-fetch staging.
        {
            let mut rs = pooled.response.lock();
            if rs.main.is_some() {
                rs.prev = rs.main.take();
            }
            rs.pending_extra.clear();
            rs.redirects.clear();
            rs.nav_error = None;
        }
        log::trace!(
            target: "onyxweb::pool",
            "acquired page (waited {:?}, pool available={})",
            t0.elapsed(),
            self.sem.available_permits()
        );
        Ok(PageGuard {
            page: Some(pooled),
            pool: self.clone(),
            _permit: permit,
        })
    }

    fn return_page(&self, p: PooledPage) {
        self.pages.lock().push(p);
        log::trace!(target: "onyxweb::pool", "page returned to pool");
    }

    /// Close every page in the pool. Call before dropping the Browser so we
    /// don't leak CDP targets.
    pub async fn close_all(&self) {
        let pages = std::mem::take(&mut *self.pages.lock());
        log::debug!(target: "onyxweb::pool", "closing {} pooled pages", pages.len());
        for p in pages {
            let _ = p.page.close().await;
            // Disposing the context frees its cookie jar + storage partition.
            let _ = self
                .browser
                .dispose_browser_context(p.browser_context_id)
                .await;
        }
    }
}

/// RAII handle to a pooled page. Drops → page goes back to pool.
pub struct PageGuard {
    page: Option<PooledPage>,
    pool: Arc<PagePool>,
    _permit: OwnedSemaphorePermit,
}

impl PageGuard {
    pub fn page(&self) -> &Page {
        &self.page.as_ref().expect("guard drained").page
    }

    /// Flag this tab as unusable so `acquire()` recreates it before reuse.
    pub fn mark_poisoned(&self) {
        self.page
            .as_ref()
            .expect("guard drained")
            .poisoned
            .store(true, Ordering::Release);
    }

    pub fn console_messages(&self) -> Arc<Mutex<Vec<ConsoleMessageRs>>> {
        self.page
            .as_ref()
            .expect("guard drained")
            .console_messages
            .clone()
    }

    /// Latest main-doc response snapshot. None if no response has arrived yet.
    pub fn main_response(&self) -> Option<MainResponseRs> {
        self.page
            .as_ref()
            .expect("guard drained")
            .response
            .lock()
            .main
            .clone()
    }

    /// Response from the most recently completed prior fetch on this tab. Used
    /// as a fallback for same-document navs which don't trigger a new
    /// `Network.responseReceived` event.
    pub fn prev_main_response(&self) -> Option<MainResponseRs> {
        self.page
            .as_ref()
            .expect("guard drained")
            .response
            .lock()
            .prev
            .clone()
    }

    /// Main-document redirect hops observed during the current fetch, in order.
    pub fn redirects(&self) -> Vec<RedirectHopRs> {
        self.page
            .as_ref()
            .expect("guard drained")
            .response
            .lock()
            .redirects
            .clone()
    }

    /// Main-document network failure recorded during this fetch, if any.
    pub fn nav_error(&self) -> Option<String> {
        self.page
            .as_ref()
            .expect("guard drained")
            .response
            .lock()
            .nav_error
            .clone()
    }

    /// Tab's currently-loaded URL, tracked via long-lived listeners on
    /// `Page.frameNavigated` and `Page.navigatedWithinDocument`. None until
    /// the first navigation completes.
    pub fn current_url(&self) -> Option<String> {
        self.page
            .as_ref()
            .expect("guard drained")
            .current_url
            .lock()
            .clone()
    }

    /// Delete this tab's anti-bot cookies, keeping every other cookie. Used by
    /// the self-heal path to shed the bot verdict before a retry. Errors are
    /// swallowed — a failed clear must not mask the original block.
    pub async fn clear_antibot_cookies(&self) {
        let page = &self.page.as_ref().expect("guard drained").page;
        let Ok(cookies) = page.get_cookies().await else {
            return;
        };
        let doomed: Vec<DeleteCookiesParams> = cookies
            .into_iter()
            .filter(|c| is_antibot_cookie(&c.name))
            .filter_map(|c| {
                DeleteCookiesParams::builder()
                    .name(c.name)
                    .domain(c.domain)
                    .path(c.path)
                    .build()
                    .ok()
            })
            .collect();
        if !doomed.is_empty() {
            let _ = page.delete_cookies(doomed).await;
        }
    }
}

impl Drop for PageGuard {
    fn drop(&mut self) {
        if let Some(p) = self.page.take() {
            self.pool.return_page(p);
        }
    }
}

/// Create one page, apply base config, wire up persistent listeners.
async fn create_pooled_page(browser: &Browser, base: &ClientConfigRs) -> Result<PooledPage> {
    let t0 = std::time::Instant::now();
    // Each tab gets its own browser context so cookies/storage are isolated
    // per-tab (no cross-fetch poisoning, no concurrent clobbering).
    let browser_context_id = browser
        .create_browser_context(CreateBrowserContextParams::default())
        .await
        .map_err(OnyxError::from)?;
    let mut target = CreateTargetParams::new("about:blank");
    target.browser_context_id = Some(browser_context_id.clone());
    let page = browser.new_page(target).await.map_err(OnyxError::from)?;
    log::trace!(target: "onyxweb::pool", "new_page in {:?}", t0.elapsed());

    // Chrome's viewport defaults to 800×600 without an explicit override.
    page.execute(
        SetDeviceMetricsOverrideParams::builder()
            .width(base.viewport.width as i64)
            .height(base.viewport.height as i64)
            .device_scale_factor(base.viewport.device_scale_factor)
            .mobile(base.viewport.mobile)
            .build()
            .map_err(|e| OnyxError::Cdp(format!("metrics: {e}")))?,
    )
    .await?;

    if let Some(ua) = &base.network.user_agent {
        let mut builder = SetUserAgentOverrideParams::builder().user_agent(ua.clone());
        if let Some(meta) = &base.network.user_agent_metadata {
            builder = builder.user_agent_metadata(build_ua_metadata(meta)?);
        }
        page.execute(
            builder
                .build()
                .map_err(|e| OnyxError::Cdp(format!("UA: {e}")))?,
        )
        .await?;
    }

    if !base.network.extra_headers.is_empty() {
        let headers = chromiumoxide::cdp::browser_protocol::network::Headers::new(
            serde_json::to_value(&base.network.extra_headers)
                .map_err(|e| OnyxError::Internal(e.to_string()))?,
        );
        page.execute(SetExtraHttpHeadersParams::new(headers))
            .await?;
    }

    if !base.network.block_urls.is_empty() {
        log::trace!(
            target: "onyxweb::pool",
            "Network.setBlockedURLs ({} patterns)",
            base.network.block_urls.len()
        );
        page.execute(SetBlockedUrLsParams {
            url_patterns: Some(block_patterns(&base.network.block_urls)),
        })
        .await?;
    }

    if base.network.disable_cache {
        page.execute(SetCacheDisabledParams::new(true)).await?;
    }

    if base.network.offline
        || base.network.latency_ms.is_some()
        || base.network.download_bps.is_some()
        || base.network.upload_bps.is_some()
    {
        #[allow(deprecated)]
        page.execute(
            EmulateNetworkConditionsParams::builder()
                .offline(base.network.offline)
                .latency(base.network.latency_ms.unwrap_or(0.0))
                .download_throughput(base.network.download_bps.map(|x| x as f64).unwrap_or(-1.0))
                .upload_throughput(base.network.upload_bps.map(|x| x as f64).unwrap_or(-1.0))
                .build()
                .map_err(|e| OnyxError::Cdp(format!("net emu: {e}")))?,
        )
        .await?;
    }

    if let Some(locale) = &base.emulation.locale {
        page.execute(
            SetLocaleOverrideParams::builder()
                .locale(locale.clone())
                .build(),
        )
        .await?;
    }

    if let Some(tz) = &base.emulation.timezone {
        page.execute(
            SetTimezoneOverrideParams::builder()
                .timezone_id(tz.clone())
                .build()
                .map_err(|e| OnyxError::Cdp(format!("tz: {e}")))?,
        )
        .await?;
    }

    if let Some((lat, lon)) = base.emulation.geolocation {
        page.execute(
            SetGeolocationOverrideParams::builder()
                .latitude(lat)
                .longitude(lon)
                .accuracy(0.0)
                .build(),
        )
        .await?;
    }

    if let Some(scheme) = &base.emulation.prefers_color_scheme {
        page.execute(
            SetEmulatedMediaParams::builder()
                .feature(MediaFeature::new("prefers-color-scheme", scheme.clone()))
                .build(),
        )
        .await?;
    }

    if !base.emulation.javascript_enabled {
        page.execute(SetScriptExecutionDisabledParams::new(true))
            .await?;
    }

    register_init_scripts(&page, base).await?;

    // Runtime domain must be enabled for ``consoleAPICalled`` events to fire.
    page.execute(chromiumoxide::cdp::js_protocol::runtime::EnableParams::default())
        .await?;

    // Persistent per-page listeners: structured console messages + main-doc
    // HTTP status. Level filter is captured here at page creation; runtime
    // updates to ``capture_console_level`` via update_config don't re-arm
    // these listeners.
    let console_messages: Arc<Mutex<Vec<ConsoleMessageRs>>> = Arc::new(Mutex::new(Vec::new()));
    let response: Arc<Mutex<ResponseState>> = Arc::new(Mutex::new(ResponseState::default()));
    let current_url: Arc<Mutex<Option<String>>> = Arc::new(Mutex::new(None));
    let level = base.capture_console_level;
    {
        use chromiumoxide::cdp::browser_protocol::network::{
            EventLoadingFailed, EventRequestWillBeSent, EventResponseReceived,
            EventResponseReceivedExtraInfo, ResourceType,
        };
        use chromiumoxide::cdp::browser_protocol::page::{
            EventJavascriptDialogOpening, HandleJavaScriptDialogParams,
        };
        use chromiumoxide::cdp::js_protocol::runtime::{
            ConsoleApiCalledType, EventConsoleApiCalled, EventExceptionThrown,
        };

        // Runtime.consoleAPICalled — every page-side ``console.*`` call.
        // Filtered by ``capture_console_level``: All keeps everything,
        // Warn drops log/info/debug/trace, Error drops everything except
        // error-level events.
        let cm_cl = console_messages.clone();
        let mut console_stream = page
            .event_listener::<EventConsoleApiCalled>()
            .await
            .map_err(OnyxError::from)?;
        tokio::spawn(async move {
            while let Some(evt) = console_stream.next().await {
                // We only surface six standard methods; ``dir``, ``table``,
                // ``startGroup`` etc. are dropped.
                let kind = match evt.r#type {
                    ConsoleApiCalledType::Log => "log",
                    ConsoleApiCalledType::Info => "info",
                    ConsoleApiCalledType::Warning => "warning",
                    ConsoleApiCalledType::Error => "error",
                    ConsoleApiCalledType::Debug => "debug",
                    ConsoleApiCalledType::Trace => "trace",
                    _ => continue,
                };
                let accept = match level {
                    CaptureConsoleLevel::All => true,
                    CaptureConsoleLevel::Warn => matches!(kind, "warning" | "error"),
                    CaptureConsoleLevel::Error => kind == "error",
                };
                if !accept {
                    continue;
                }
                // Stringify args: JSON strings come through unquoted, other
                // primitives via JSON repr, objects via `.description`.
                let text = evt
                    .args
                    .iter()
                    .map(|arg| {
                        arg.value
                            .as_ref()
                            .map(|v| match v {
                                serde_json::Value::String(s) => s.clone(),
                                other => other.to_string(),
                            })
                            .or_else(|| arg.description.clone())
                            .unwrap_or_default()
                    })
                    .collect::<Vec<_>>()
                    .join(" ");
                let timestamp = std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map(|d| d.as_secs_f64())
                    .unwrap_or(0.0);
                cm_cl.lock().push(ConsoleMessageRs {
                    kind: kind.to_string(),
                    text,
                    timestamp,
                });
            }
        });

        // Runtime.exceptionThrown — uncaught JS errors. Captured as
        // ConsoleMessage(type="error", ...) so they show up alongside
        // console.error calls.
        let cm_cl = console_messages.clone();
        let mut exc_stream = page
            .event_listener::<EventExceptionThrown>()
            .await
            .map_err(OnyxError::from)?;
        tokio::spawn(async move {
            while let Some(evt) = exc_stream.next().await {
                let det = &evt.exception_details;
                let text = det
                    .exception
                    .as_ref()
                    .and_then(|o| o.description.clone())
                    .unwrap_or_else(|| det.text.clone());
                let timestamp = std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map(|d| d.as_secs_f64())
                    .unwrap_or(0.0);
                cm_cl.lock().push(ConsoleMessageRs {
                    kind: "error".to_string(),
                    text,
                    timestamp,
                });
            }
        });

        // Overwrite on every Document response so redirects end with the
        // final response. Fires before DCL/load, much earlier than
        // wait_for_navigation_response would. Headers from responseReceived are
        // the parsed set (no Set-Cookie); if the matching extraInfo already
        // arrived we upgrade to its raw headers + verbatim wire text.
        let resp_cl = response.clone();
        let mut resp_stream = page
            .event_listener::<EventResponseReceived>()
            .await
            .map_err(OnyxError::from)?;
        tokio::spawn(async move {
            while let Some(evt) = resp_stream.next().await {
                if matches!(evt.r#type, ResourceType::Document) {
                    let resp = &evt.response;
                    let rid = evt.request_id.inner().clone();
                    let status = resp.status as u16;
                    let cert_info = resp.security_details.as_ref().map(|sd| CertInfoRs {
                        common_name: sd.subject_name.clone(),
                        sans: sd.san_list.clone(),
                        issuer: sd.issuer.clone(),
                        valid_from: *sd.valid_from.inner(),
                        valid_to: *sd.valid_to.inner(),
                    });
                    let mut mr = MainResponseRs {
                        status,
                        status_text: resp.status_text.clone(),
                        mime_type: resp.mime_type.clone(),
                        protocol: resp.protocol.clone().unwrap_or_default(),
                        remote_ip: resp.remote_ip_address.clone(),
                        remote_port: resp.remote_port.map(|p| p as u16),
                        request_id: rid.clone(),
                        headers: headers_to_vec(&resp.headers),
                        cert_info,
                    };
                    let mut rs = resp_cl.lock();
                    // Only take the extraInfo for THIS response (same id AND
                    // status) — never an intermediate redirect hop's.
                    if let Some(ei) = rs.pending_extra.remove(&(rid, status)) {
                        mr.headers = ei.headers;
                    }
                    rs.main = Some(mr);
                }
            }
        });

        // responseReceivedExtraInfo — the raw headers (incl. Set-Cookie) and,
        // for HTTP/1.x, the verbatim wire header block. Fires for every
        // request in either order relative to responseReceived, so: if it's
        // the current main-doc request, merge in; if the main doc isn't known
        // yet, stage by request id; otherwise (a subresource) drop it.
        let extra_cl = response.clone();
        let mut extra_stream = page
            .event_listener::<EventResponseReceivedExtraInfo>()
            .await
            .map_err(OnyxError::from)?;
        tokio::spawn(async move {
            while let Some(evt) = extra_stream.next().await {
                let rid = evt.request_id.inner().clone();
                let status = evt.status_code as u16;
                let ei = ExtraInfoRs {
                    headers: headers_to_vec(&evt.headers),
                };
                let mut rs = extra_cl.lock();
                match rs.main.as_mut() {
                    // Same request AND same status → this is the final
                    // response's extraInfo; merge it in.
                    Some(mr) if mr.request_id == rid && mr.status == status => {
                        mr.headers = ei.headers;
                    }
                    // Same request, different status → an intermediate redirect
                    // hop for a response we already have; ignore it.
                    Some(mr) if mr.request_id == rid => {}
                    // A subresource we don't track; ignore.
                    Some(_) => {}
                    // Main-doc response not seen yet; stage by (id, status).
                    None => {
                        rs.pending_extra.insert((rid, status), ei);
                    }
                }
            }
        });

        // Network.requestWillBeSent — a present `redirectResponse` means the
        // previous request in this chain was redirected. For the main document
        // (type Document) we record each hop (url, status, remote ip) in order,
        // giving callers the full redirect chain (matches blasthttp).
        let redir_cl = response.clone();
        let mut req_stream = page
            .event_listener::<EventRequestWillBeSent>()
            .await
            .map_err(OnyxError::from)?;
        tokio::spawn(async move {
            while let Some(evt) = req_stream.next().await {
                if !matches!(evt.r#type, Some(ResourceType::Document)) {
                    continue;
                }
                if let Some(resp) = &evt.redirect_response {
                    redir_cl.lock().redirects.push(RedirectHopRs {
                        url: resp.url.clone(),
                        status: resp.status as u16,
                        remote_ip: resp.remote_ip_address.clone(),
                    });
                }
            }
        });

        // Network.loadingFailed — a main-document failure (DNS, connection
        // refused). Skips intentional aborts; capture_page surfaces it since the
        // error page otherwise fires a lifecycle event and looks like success.
        let fail_cl = response.clone();
        let mut fail_stream = page
            .event_listener::<EventLoadingFailed>()
            .await
            .map_err(OnyxError::from)?;
        tokio::spawn(async move {
            while let Some(evt) = fail_stream.next().await {
                if matches!(evt.r#type, ResourceType::Document) && evt.canceled != Some(true) {
                    fail_cl.lock().nav_error = Some(evt.error_text.clone());
                }
            }
        });

        // Page.frameNavigated — fires on every cross-document navigation
        // (full nav). Updates current_url so we can detect when an upcoming
        // fetch is a same-document navigation.
        let url_cl_full = current_url.clone();
        let mut frame_nav_stream = page
            .event_listener::<EventFrameNavigated>()
            .await
            .map_err(OnyxError::from)?;
        tokio::spawn(async move {
            while let Some(evt) = frame_nav_stream.next().await {
                if evt.frame.parent_id.is_none() {
                    *url_cl_full.lock() = Some(evt.frame.url.clone());
                }
            }
        });

        // Page.navigatedWithinDocument — fires on same-document navigation
        // (hash change, history API). Keeps current_url in sync.
        let url_cl_same = current_url.clone();
        let mut within_doc_stream = page
            .event_listener::<EventNavigatedWithinDocument>()
            .await
            .map_err(OnyxError::from)?;
        tokio::spawn(async move {
            while let Some(evt) = within_doc_stream.next().await {
                *url_cl_same.lock() = Some(evt.url.clone());
            }
        });

        // Page.javascriptDialogOpening — auto-dismiss native dialogs
        // (alert/confirm/prompt/beforeunload). Without this, any page
        // that calls these blocks the lifecycle event waiting for a UI
        // dismissal that never comes. Mirrors Playwright/Selenium defaults.
        let page_for_dialogs = page.clone();
        let mut dialog_stream = page
            .event_listener::<EventJavascriptDialogOpening>()
            .await
            .map_err(OnyxError::from)?;
        tokio::spawn(async move {
            while let Some(_evt) = dialog_stream.next().await {
                let _ = page_for_dialogs
                    .execute(
                        HandleJavaScriptDialogParams::builder()
                            .accept(false)
                            .build()
                            .expect("HandleJavaScriptDialogParams: accept is set"),
                    )
                    .await;
            }
        });
    }

    Ok(PooledPage {
        page,
        browser_context_id,
        console_messages,
        response,
        current_url,
        poisoned: AtomicBool::new(false),
    })
}

/// Build a chromiumoxide ``UserAgentMetadata`` from our parsed config mirror.
fn build_ua_metadata(m: &UserAgentMetadataRs) -> Result<UserAgentMetadata> {
    let mut b = UserAgentMetadata::builder()
        .platform(m.platform.clone())
        .platform_version(m.platform_version.clone())
        .architecture(m.architecture.clone())
        .model(m.model.clone())
        .mobile(m.mobile);
    if let Some(brands) = &m.brands {
        for br in brands {
            b = b.brand(UserAgentBrandVersion::new(
                br.brand.clone(),
                br.version.clone(),
            ));
        }
    }
    if let Some(fvl) = &m.full_version_list {
        for br in fvl {
            b = b.full_version_list(UserAgentBrandVersion::new(
                br.brand.clone(),
                br.version.clone(),
            ));
        }
    }
    if let Some(bitness) = &m.bitness {
        b = b.bitness(bitness.clone());
    }
    if m.wow64 {
        b = b.wow64(true);
    }
    if let Some(ff) = &m.form_factors {
        b = b.form_factors(ff.clone());
    }
    b.build()
        .map_err(|e| OnyxError::Cdp(format!("UA metadata: {e}")))
}

/// Register all declarative init scripts via ``Page.addScriptToEvaluateOnNewDocument``.
/// Timing variants and URL scoping are implemented as source-wrapping; only
/// ``on_new_document`` and ``isolated_world`` map 1:1 to the CDP primitive.
async fn register_init_scripts(page: &Page, base: &ClientConfigRs) -> Result<()> {
    let s = &base.scripts;
    let total = s.on_new_document.len()
        + s.on_dom_content_loaded.len()
        + s.on_load.len()
        + s.isolated_world.len()
        + s.url_scoped.values().map(|v| v.len()).sum::<usize>();
    if total == 0 {
        return Ok(());
    }
    log::trace!(target: "onyxweb::pool", "registering {total} init scripts");

    for src in &s.on_new_document {
        page.execute(AddScriptToEvaluateOnNewDocumentParams::new(src.clone()))
            .await?;
    }

    for src in &s.on_dom_content_loaded {
        let wrapped =
            format!("document.addEventListener('DOMContentLoaded', function() {{ {src} }});");
        page.execute(AddScriptToEvaluateOnNewDocumentParams::new(wrapped))
            .await?;
    }

    for src in &s.on_load {
        let wrapped = format!("window.addEventListener('load', function() {{ {src} }});");
        page.execute(AddScriptToEvaluateOnNewDocumentParams::new(wrapped))
            .await?;
    }

    for src in &s.isolated_world {
        let world_name = if s.isolated_world_name.is_empty() {
            DEFAULT_ISOLATED_WORLD_NAME
        } else {
            s.isolated_world_name.as_str()
        };
        page.execute(
            AddScriptToEvaluateOnNewDocumentParams::builder()
                .source(src.clone())
                .world_name(world_name)
                .build()
                .map_err(|e| OnyxError::Cdp(format!("isolated script: {e}")))?,
        )
        .await?;
    }

    for (pattern, scripts) in &s.url_scoped {
        let pat_esc = js_escape_single_quoted(pattern);
        for src in scripts {
            let wrapped = format!("if (location.href.indexOf('{pat_esc}') !== -1) {{ {src} }}");
            page.execute(AddScriptToEvaluateOnNewDocumentParams::new(wrapped))
                .await?;
        }
    }

    Ok(())
}

/// Escape a string for safe embedding inside a JS single-quoted string literal.
fn js_escape_single_quoted(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\'' => out.push_str("\\'"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            _ => out.push(c),
        }
    }
    out
}
