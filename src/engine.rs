//! Core navigate-and-capture step, running on a page drawn from the pool.
//!
//! Driven by `Client` via tokio. All async, all inside `py.allow_threads()`.
//! One pooled page per fetch: configure (per-call overrides only), navigate,
//! capture, reset on error. Pool pages keep their base config + console
//! listeners across fetches.

use std::time::{Duration, Instant};

use chromiumoxide::cdp::browser_protocol::emulation::SetDeviceMetricsOverrideParams;
use chromiumoxide::cdp::browser_protocol::fetch::{
    DisableParams as FetchDisableParams, EnableParams as FetchEnableParams, EventRequestPaused,
    FailRequestParams, RequestPattern,
};
use chromiumoxide::cdp::browser_protocol::network::{
    ErrorReason, ResourceType, SetBlockedUrLsParams, SetExtraHttpHeadersParams,
};
use chromiumoxide::cdp::browser_protocol::page::{
    AddScriptToEvaluateOnNewDocumentParams, CaptureScreenshotFormat, CaptureScreenshotParams,
    EventDomContentEventFired, EventFrameNavigated, EventLoadEventFired, NavigateParams,
    ReferrerPolicy, RemoveScriptToEvaluateOnNewDocumentParams, ScriptIdentifier,
};
use futures::StreamExt;

use crate::config::{
    ActionErrorPolicy, ActionRs, ClientConfigRs, FetchConfigRs, ImageFormat, ScreenshotConfigRs,
    WaitUntil,
};
use crate::error::{OnyxError, Result};
use crate::pool::{CertInfoRs, PageGuard, block_patterns};
use crate::result::ConsoleMessageRs;

/// True when ``target`` differs from ``prev`` only by URL fragment (the part
/// after `#`) AND the fragment actually differs. chromium treats such
/// transitions as same-document navigations: no new HTTP request, no `load`
/// event, no `domContentLoaded` event — only `Page.navigatedWithinDocument`
/// fires.
///
/// Identical URLs (same path/query, same fragment or both fragmentless) are
/// NOT same-doc — chromium does a full reload, the init scripts re-fire, and
/// the load event fires; we want the normal goto path.
///
/// Used by `capture_page` to route hash-only navs through `Runtime.evaluate`
/// (which goes through a separate CDP command channel) rather than
/// `Page.navigate` (which empirically hangs in chromiumoxide for hash-only
/// URLs after a previous nav on the same pool tab).
fn is_same_document_change(prev: &str, target: &str) -> bool {
    fn split(s: &str) -> (&str, Option<&str>) {
        match s.split_once('#') {
            Some((p, h)) => (p, Some(h)),
            None => (s, None),
        }
    }
    let (prev_prefix, prev_hash) = split(prev);
    let (target_prefix, target_hash) = split(target);
    prev_prefix == target_prefix && prev_hash != target_hash
}

/// Append a unique nanosecond cache-buster query parameter to ``url``,
/// preserving any existing fragment. Used to force chromium to treat a
/// same-document URL as a new document (so per-call init scripts fire).
///
/// data: URLs and other URLs without a query slot fall through unchanged
/// (best-effort — caller will get the chromiumoxide hang for those).
fn append_cache_buster(url: &str) -> String {
    let nano = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let suffix = format!("__onyxweb_t={nano}");
    let (path_query, hash) = match url.split_once('#') {
        Some((p, h)) => (p, Some(h)),
        None => (url, None),
    };
    // data: URLs / opaque schemes don't have a useful query slot; bail.
    if path_query.starts_with("data:") || !path_query.contains("://") {
        return url.to_string();
    }
    let with_q = if path_query.contains('?') {
        format!("{path_query}&{suffix}")
    } else {
        format!("{path_query}?{suffix}")
    };
    match hash {
        Some(h) => format!("{with_q}#{h}"),
        None => with_q,
    }
}

/// Apply an action's failure policy. Returns ``Ok(true)`` if the action
/// succeeded (caller should run any post-action wait), ``Ok(false)`` if it
/// failed but the policy said to continue/ignore, or ``Err`` to propagate
/// (policy=Abort).
fn handle_action_result(
    res: Result<()>,
    policy: ActionErrorPolicy,
    guard: &PageGuard,
    action_name: &str,
    selector: &str,
) -> Result<bool> {
    match res {
        Ok(()) => Ok(true),
        Err(e) => match policy {
            ActionErrorPolicy::Abort => Err(e),
            ActionErrorPolicy::Continue => {
                let timestamp = std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map(|d| d.as_secs_f64())
                    .unwrap_or(0.0);
                guard.console_messages().lock().push(ConsoleMessageRs {
                    kind: "error".to_string(),
                    text: format!("Action {action_name}({selector}) failed: {e}"),
                    timestamp,
                });
                Ok(false)
            }
            ActionErrorPolicy::Ignore => {
                log::debug!(
                    target: "onyxweb::engine",
                    "action {action_name}({selector}) ignored error: {e}"
                );
                Ok(false)
            }
        },
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CaptureMode {
    Html,
    Png,
    Both,
}

/// A WAF / anti-bot measure encountered during a fetch. Detected always (even
/// with `bypass_anti_bot` off) so callers get the signal regardless.
pub struct AntiBotRs {
    /// Vendor: `"akamai"` / `"cloudflare"` / `"datadome"` / `"perimeterx"` /
    /// `"imperva"`, or `None` for a generic challenge with no identifiable vendor.
    pub vendor: Option<String>,
    /// `"challenge"` (a JS interstitial) or `"block"` (a hard 403/429).
    pub kind: &'static str,
    /// True if onyxweb got past it to the real page (challenge cleared or the
    /// self-heal recovered); False if the captured page is the stub / block.
    pub resolved: bool,
}

#[derive(Default)]
pub struct CaptureOutput {
    pub html: Option<String>,
    pub png: Option<Vec<u8>>,
    pub console_messages: Vec<ConsoleMessageRs>,
    pub final_url: String,
    /// The URL originally requested (before any redirects).
    pub request_url: String,
    /// Redirect hops to the final response — (url, status, remote_ip) in order.
    pub redirect_chain: Vec<(String, u16, Option<String>)>,
    pub status_code: u16,
    pub status_text: String,
    pub mime_type: String,
    /// Negotiated protocol (`"http/1.1"`, `"h2"`, `"h3"`, ...). Empty when the
    /// browser reported none (data: URLs, some cached responses).
    pub protocol: String,
    pub remote_ip: Option<String>,
    pub remote_port: Option<u16>,
    /// Main-doc response headers as (name, value) pairs, duplicates preserved
    /// (Set-Cookie split into one pair each). The canonical `raw_headers`
    /// string + its hash are derived from this list.
    pub headers: Vec<(String, String)>,
    /// TLS certificate info (`None` for plain HTTP).
    pub cert_info: Option<CertInfoRs>,
    /// Byte length of the captured (post-JS) HTML body. 0 when no HTML was
    /// captured (screenshot-only mode).
    pub content_length: usize,
    pub elapsed_s: f64,
    /// JSON-string per ``FetchConfig.post_load_scripts`` entry, ``None`` for
    /// ``undefined`` / non-serializable JS returns. Empty when no
    /// ``post_load_scripts`` were configured.
    pub post_load_results: Vec<Option<String>>,
    /// WAF / anti-bot measure encountered, if any (`None` = clean).
    pub anti_bot: Option<AntiBotRs>,
}

/// Navigate an already-configured pooled page to `url` and capture.
pub async fn capture_page(
    guard: &PageGuard,
    url: &str,
    base: &ClientConfigRs,
    per_call: &FetchConfigRs,
    per_shot: &ScreenshotConfigRs,
    mode: CaptureMode,
) -> Result<CaptureOutput> {
    let t0 = Instant::now();
    log::debug!(target: "onyxweb::engine", "[{url}] capture_page mode={mode:?}");

    let timeout_ms = per_call
        .timeout_ms
        .or(per_shot.timeout_ms)
        .unwrap_or(base.timeout.navigation_ms);

    // Hoist wait_until computation before the fut block so the timeout
    // error path (outside the fut) can name which lifecycle event was
    // being awaited.
    let wait_until = per_call
        .wait_until
        .or(per_shot.wait_until)
        .unwrap_or(base.wait_until);
    let wait_until_label: &'static str = match wait_until {
        WaitUntil::Load => "load",
        WaitUntil::DomContentLoaded => "domcontentloaded",
    };
    let bypass_anti_bot = per_call.bypass_anti_bot.unwrap_or(base.bypass_anti_bot);

    let page = guard.page();

    // Per-call init scripts — register BEFORE the timeout-wrapped main work
    // so we hold their identifiers in outer scope for cleanup. ``page.execute``
    // for ``Page.addScriptToEvaluateOnNewDocument`` is fast (one CDP RTT) and
    // shouldn't itself block long enough to need the lifecycle timeout.
    // Cleanup runs unconditionally below — success or failure path.
    let mut script_ids: Vec<ScriptIdentifier> = Vec::with_capacity(per_call.scripts.len());
    for src in &per_call.scripts {
        log::trace!(
            target: "onyxweb::engine",
            "[{url}] registering per-call init script ({} chars)",
            src.len()
        );
        let resp = page
            .execute(AddScriptToEvaluateOnNewDocumentParams::new(src.clone()))
            .await?;
        script_ids.push(resp.identifier.clone());
    }

    // On an anti-bot block, `bypass_anti_bot` retries once after dropping the
    // tab's anti-bot cookies (loop body re-runs a fresh navigate+capture).
    let mut healed = false;
    let mut block_hit: Option<&'static str> = None;
    let fut_result = loop {
        let fut = async {
            // Per-call viewport override (e.g. different size just for this screenshot).
            if let Some((w, h)) = per_shot.viewport {
                log::trace!(target: "onyxweb::engine", "[{url}] override viewport {w}x{h}");
                page.execute(
                    SetDeviceMetricsOverrideParams::builder()
                        .width(w as i64)
                        .height(h as i64)
                        .device_scale_factor(base.viewport.device_scale_factor)
                        .mobile(base.viewport.mobile)
                        .build()
                        .map_err(|e| OnyxError::Cdp(format!("metrics: {e}")))?,
                )
                .await?;
            }

            // Per-call URL blocking — apply the merged `base + per-call` list;
            // the baseline is restored after capture (cleanup below).
            if !per_call.block_urls.is_empty() {
                let mut merged = base.network.block_urls.clone();
                merged.extend(per_call.block_urls.iter().cloned());
                log::trace!(
                    target: "onyxweb::engine",
                    "[{url}] applying {} blocked URLs (base={}, per_call={})",
                    merged.len(),
                    base.network.block_urls.len(),
                    per_call.block_urls.len()
                );
                page.execute(SetBlockedUrLsParams {
                    url_patterns: Some(block_patterns(&merged)),
                })
                .await?;
            }

            // Per-call header merge — only if there ARE per-call / per-shot extras
            // OR a base Referer that needs lifting out.
            let mut headers_map = base.network.extra_headers.clone();
            for (k, v) in &per_call.extra_headers {
                headers_map.insert(k.clone(), v.clone());
            }
            for (k, v) in &per_shot.extra_headers {
                headers_map.insert(k.clone(), v.clone());
            }
            // Lift `Referer` (case-insensitive) out of the merged headers map.
            // Setting Referer via Network.setExtraHTTPHeaders is rejected by
            // chromium with ERR_BLOCKED_BY_CLIENT for cross-origin values (W3C
            // Referrer Policy enforcement at the URL loader). The supported
            // path is `Page.navigate` with the `referrer` parameter, applied
            // below.
            let mut referrer: Option<String> = None;
            let referer_keys: Vec<String> = headers_map
                .keys()
                .filter(|k| k.eq_ignore_ascii_case("referer"))
                .cloned()
                .collect();
            for k in referer_keys {
                referrer = headers_map.remove(&k);
            }
            let has_per_fetch_extras =
                !per_call.extra_headers.is_empty() || !per_shot.extra_headers.is_empty();
            if has_per_fetch_extras || referrer.is_some() {
                log::trace!(
                    target: "onyxweb::engine",
                    "[{url}] merging headers (per_call={}, per_shot={}, referrer={})",
                    per_call.extra_headers.len(),
                    per_shot.extra_headers.len(),
                    referrer.is_some()
                );
                let headers = chromiumoxide::cdp::browser_protocol::network::Headers::new(
                    serde_json::to_value(&headers_map)
                        .map_err(|e| OnyxError::Internal(e.to_string()))?,
                );
                page.execute(SetExtraHttpHeadersParams::new(headers))
                    .await?;
            }

            // Subscribe before the nav so the race below can't miss an early event.
            let t_goto = Instant::now();
            let mut dcl_stream = page
                .event_listener::<EventDomContentEventFired>()
                .await
                .map_err(OnyxError::from)?;
            let mut load_stream = page
                .event_listener::<EventLoadEventFired>()
                .await
                .map_err(OnyxError::from)?;
            let mut frame_nav_stream = page
                .event_listener::<EventFrameNavigated>()
                .await
                .map_err(OnyxError::from)?;

            // chromiumoxide's Page.navigate future hangs on a hash-only nav on a
            // pooled tab: same-doc navs without init scripts use Runtime.evaluate;
            // with init scripts, a cache-buster forces a new-document nav so
            // addScriptToEvaluateOnNewDocument fires.
            let needs_init_scripts = !per_call.scripts.is_empty();
            let is_same_doc =
                matches!(guard.current_url(), Some(prev) if is_same_document_change(&prev, url));

            let nav_params: Option<NavigateParams> = if is_same_doc && !needs_init_scripts {
                let escaped = serde_json::to_string(url).unwrap_or_else(|_| "''".to_string());
                page.evaluate(format!("location.href = {escaped};").as_str())
                    .await?;
                None
            } else {
                let target = if is_same_doc {
                    append_cache_buster(url)
                } else {
                    url.to_string()
                };
                // ReferrerPolicy::UnsafeUrl passes the full referrer through; the
                // default strips path/query cross-origin.
                Some(match referrer.as_ref() {
                    Some(r) => NavigateParams::builder()
                        .url(target)
                        .referrer(r.clone())
                        .referrer_policy(ReferrerPolicy::UnsafeUrl)
                        .build()
                        .map_err(|e| OnyxError::Cdp(format!("navigate params: {e}")))?,
                    None => NavigateParams::new(target),
                })
            };
            let same_doc_nav = nav_params.is_none();

            // Race goto (blocks until every frame loads) against the lifecycle
            // event, gated behind the new doc's main-frame commit so the outgoing
            // page's lingering pushState / late load can't resolve early and
            // capture stale content. Dropping goto is safe (see nav_error below).
            if let Some(nav_params) = nav_params {
                let nav_fut = page.goto(nav_params);
                let lifecycle = async {
                    while let Some(evt) = frame_nav_stream.next().await {
                        if evt.frame.parent_id.is_none() {
                            break;
                        }
                    }
                    match wait_until {
                        // load: DCL-miss fallback.
                        WaitUntil::DomContentLoaded => tokio::select! {
                            _ = dcl_stream.next() => {}
                            _ = load_stream.next() => {}
                        },
                        WaitUntil::Load => {
                            let _ = load_stream.next().await;
                        }
                    }
                };
                tokio::select! {
                    r = nav_fut => { r?; }
                    _ = lifecycle => {}
                }
            }
            log::trace!(
                target: "onyxweb::engine",
                "[{url}] nav done in {:?}",
                t_goto.elapsed()
            );

            // A main-doc network failure (DNS, connection refused) still fires a
            // lifecycle event via its error page, so surface it explicitly.
            if !same_doc_nav && let Some(err) = guard.nav_error() {
                return Err(OnyxError::Cdp(err));
            }

            // Optional post-event settle — lets late async JS mutate the DOM on
            // SPAs that render AFTER the chosen lifecycle event fires.
            let wait_after_ms = per_call
                .wait_after_ms
                .or(per_shot.wait_after_ms)
                .unwrap_or(base.wait_after_ms);
            if wait_after_ms > 0 {
                log::trace!(target: "onyxweb::engine", "[{url}] settle {wait_after_ms}ms");
                tokio::time::sleep(Duration::from_millis(wait_after_ms)).await;
            }

            // Anti-bot challenge interstitial (Akamai/Cloudflare/DataDome/...):
            // a small "checking your browser / verify you're human" stub that
            // runs a JS check then self-reloads/redirects to the real page. When
            // bypass_anti_bot is on, poll for that resolution instead of
            // capturing the stub. The metadata + content captured below then
            // reflect the real page (the reload fires a fresh responseReceived).
            // A challenge that never auto-solves (e.g. an interactive captcha)
            // just times out and we capture whatever's on screen.
            let mut challenge_hit: Option<&'static str> = None;
            if bypass_anti_bot {
                let mut probe = page.content().await.unwrap_or_default();
                if let Some(vendor) = challenge_vendor(&probe) {
                    challenge_hit = Some(vendor);
                    log::debug!(
                        target: "onyxweb::engine",
                        "[{url}] anti-bot challenge ({vendor}) detected — waiting up to {CHALLENGE_MAX_WAIT_MS}ms to resolve"
                    );
                    let deadline = Instant::now() + Duration::from_millis(CHALLENGE_MAX_WAIT_MS);
                    let mut last_len = 0usize;
                    while Instant::now() < deadline {
                        tokio::time::sleep(Duration::from_millis(CHALLENGE_POLL_MS)).await;
                        probe = page.content().await.unwrap_or_default();
                        // Done once it's no longer a challenge AND the resolved
                        // real page has stopped growing (two equal polls). The
                        // challenge reload lands a thin early page (title only);
                        // without the stability check we'd capture that.
                        let stable = probe.len() == last_len;
                        last_len = probe.len();
                        if !looks_like_challenge(&probe)
                            && stable
                            && probe.len() > CHALLENGE_STUB_MAX_BYTES
                        {
                            break;
                        }
                    }
                    log::debug!(
                        target: "onyxweb::engine",
                        "[{url}] challenge wait done (resolved={}, bytes={})",
                        !looks_like_challenge(&probe),
                        probe.len()
                    );
                }
            }

            // Per-call navigation blocking — arm AFTER the initial load + settle
            // so the original page is reachable, but BEFORE actions so any
            // JS-driven navigation they trigger is intercepted. Filter at the
            // Fetch.enable layer (resource_type=Document) so subresources never
            // fire ``Fetch.requestPaused`` — they go through chromium's normal
            // path with zero CDP overhead. Cleanup (``Fetch.disable``) runs
            // unconditionally below.
            if per_call.block_navigation {
                if guard.auth_fetch_active() {
                    // chromiumoxide owns this tab's Fetch domain for proxy auth;
                    // adding our own Fetch interception would clobber it, so
                    // navigation blocking is unavailable here.
                    log::warn!(
                        target: "onyxweb::engine",
                        "[{url}] block_navigation is unsupported with an authenticated proxy; navigation not blocked"
                    );
                } else {
                    log::trace!(
                        target: "onyxweb::engine",
                        "[{url}] block_navigation: enabling Fetch interception (Document only)"
                    );
                    let document_only = RequestPattern::builder()
                        .resource_type(ResourceType::Document)
                        .build();
                    page.execute(FetchEnableParams::builder().pattern(document_only).build())
                        .await?;
                    let mut paused_stream = page
                        .event_listener::<EventRequestPaused>()
                        .await
                        .map_err(OnyxError::from)?;
                    let page_for_task = page.clone();
                    tokio::spawn(async move {
                        while let Some(evt) = paused_stream.next().await {
                            // Pattern guarantees only Document-type requests reach
                            // us; abort each.
                            let _ = page_for_task
                                .execute(FailRequestParams::new(
                                    evt.request_id.clone(),
                                    ErrorReason::Aborted,
                                ))
                                .await;
                        }
                    });
                }
            }

            // Per-call post-load scripts — run arbitrary JS on the loaded page
            // via Runtime.evaluate (one CDP roundtrip each). The primary
            // primitive for "do JS work on the loaded page".
            //
            // chromiumoxide forces return_by_value, so primitives / plain objects
            // / arrays come back JSON-decoded in `RemoteObject.value`. Function
            // returns keep type=Function — surfaced as `None` (not chromium's
            // empty dict). DOM nodes / Window serialize to `{}` (chromium can't
            // enumerate them); callers that must distinguish should filter in
            // their own script. Sources aren't wrapped in a DOM-detecting
            // trampoline — that would break multi-statement / throw / try-catch
            // bodies.
            let mut post_load_results: Vec<Option<String>> =
                Vec::with_capacity(per_call.post_load_scripts.len());
            for (i, src) in per_call.post_load_scripts.iter().enumerate() {
                log::trace!(
                    target: "onyxweb::engine",
                    "[{url}] post_load_script[{i}] ({} chars)",
                    src.len()
                );
                let eval_result =
                    page.evaluate(src.as_str())
                        .await
                        .map_err(|e| OnyxError::PostLoadScript {
                            index: i,
                            source: Box::new(OnyxError::from(e)),
                        })?;
                let is_function = matches!(
                    eval_result.object().r#type,
                    chromiumoxide::cdp::js_protocol::runtime::RemoteObjectType::Function
                );
                let serialized = if is_function {
                    None
                } else {
                    eval_result.value().map(|v| v.to_string())
                };
                if serialized.is_none() {
                    log::debug!(
                        target: "onyxweb::engine",
                        "[{url}] post_load_script[{i}] returned non-serializable / undefined / function"
                    );
                }
                post_load_results.push(serialized);
            }

            // Optional settle window AFTER post_load_scripts and BEFORE actions /
            // capture. Distinct from `wait_after_ms` (which fires before
            // post_load_scripts). Lets scripts that schedule async work
            // (setTimeout, fetch, deferred DOM mutations) finish before capture.
            let wait_after_post_load_ms = per_call
                .wait_after_post_load_ms
                .or(per_shot.wait_after_post_load_ms)
                .unwrap_or(base.wait_after_post_load_ms);
            if wait_after_post_load_ms > 0 {
                log::trace!(
                    target: "onyxweb::engine",
                    "[{url}] post-load settle {wait_after_post_load_ms}ms"
                );
                tokio::time::sleep(Duration::from_millis(wait_after_post_load_ms)).await;
            }

            // Run post-load actions BEFORE HTML capture so the captured DOM
            // reflects post-action state (and a Click that triggers nav still
            // gets a final_url update from the response listener).
            for action in &per_call.actions {
                match action {
                    ActionRs::Click {
                        selector,
                        wait_after_ms: w,
                        on_error,
                    } => {
                        log::trace!(target: "onyxweb::engine", "[{url}] click action: {selector}");
                        let res = async {
                            let element = page.find_element(selector).await?;
                            element.click().await?;
                            Ok::<_, OnyxError>(())
                        }
                        .await;
                        let ok = handle_action_result(res, *on_error, guard, "click", selector)?;
                        if ok && *w > 0 {
                            tokio::time::sleep(Duration::from_millis(*w)).await;
                        }
                    }
                    ActionRs::Fill {
                        selector,
                        value,
                        wait_after_ms: w,
                        on_error,
                    } => {
                        log::trace!(target: "onyxweb::engine", "[{url}] fill action: {selector}");
                        let res = async {
                            let element = page.find_element(selector).await?;
                            let value_js = serde_json::to_string(value)
                                .map_err(|e| OnyxError::Internal(format!("fill value: {e}")))?;
                            let fn_src = format!(
                                "function() {{ \
                                this.focus(); \
                                this.value = {value_js}; \
                                this.dispatchEvent(new Event('input', {{bubbles: true}})); \
                                this.dispatchEvent(new Event('change', {{bubbles: true}})); \
                            }}"
                            );
                            element.call_js_fn(fn_src, false).await?;
                            Ok::<_, OnyxError>(())
                        }
                        .await;
                        let ok = handle_action_result(res, *on_error, guard, "fill", selector)?;
                        if ok && *w > 0 {
                            tokio::time::sleep(Duration::from_millis(*w)).await;
                        }
                    }
                    ActionRs::Hover {
                        selector,
                        wait_after_ms: w,
                        on_error,
                    } => {
                        log::trace!(target: "onyxweb::engine", "[{url}] hover action: {selector}");
                        let res = async {
                            let element = page.find_element(selector).await?;
                            element.hover().await?;
                            Ok::<_, OnyxError>(())
                        }
                        .await;
                        let ok = handle_action_result(res, *on_error, guard, "hover", selector)?;
                        if ok && *w > 0 {
                            tokio::time::sleep(Duration::from_millis(*w)).await;
                        }
                    }
                    ActionRs::Wait { duration_ms } => {
                        log::trace!(target: "onyxweb::engine", "[{url}] wait action: {duration_ms}ms");
                        tokio::time::sleep(Duration::from_millis(*duration_ms)).await;
                    }
                }
            }

            let final_url = page
                .url()
                .await
                .ok()
                .flatten()
                .unwrap_or_else(|| url.to_string());
            // Main-doc response from the pool's Network.responseReceived listener —
            // captured on response headers, independent of wait_until choice.
            // Same-document navs don't trigger a new HTTP response, so fall back to
            // the prior fetch's response (the document hasn't actually changed).
            let main_resp = guard
                .main_response()
                .or_else(|| {
                    if same_doc_nav {
                        guard.prev_main_response()
                    } else {
                        None
                    }
                })
                .unwrap_or_default();

            let redirect_chain: Vec<(String, u16, Option<String>)> = guard
                .redirects()
                .into_iter()
                .map(|h| (h.url, h.status, h.remote_ip))
                .collect();

            let mut out = CaptureOutput {
                html: None,
                png: None,
                console_messages: Vec::new(),
                final_url,
                request_url: url.to_string(),
                redirect_chain,
                status_code: main_resp.status,
                status_text: main_resp.status_text,
                mime_type: main_resp.mime_type,
                protocol: main_resp.protocol,
                remote_ip: main_resp.remote_ip,
                remote_port: main_resp.remote_port,
                headers: main_resp.headers,
                cert_info: main_resp.cert_info,
                content_length: 0,
                elapsed_s: 0.0,
                post_load_results,
                anti_bot: None,
            };

            if matches!(mode, CaptureMode::Html | CaptureMode::Both) {
                let t_html = Instant::now();
                let html = page.content().await?;
                log::trace!(
                    target: "onyxweb::engine",
                    "[{url}] content: {} bytes in {:?}",
                    html.len(),
                    t_html.elapsed()
                );
                out.content_length = html.len();
                out.html = Some(html);
            }

            // Anti-bot indicator for a challenge we waited out (challenge_hit set
            // above). `resolved` reflects whether the captured page is still a
            // challenge stub. The block case is handled after the retry loop.
            if let Some(vendor) = challenge_hit {
                let resolved = !looks_like_challenge(out.html.as_deref().unwrap_or_default());
                out.anti_bot = Some(AntiBotRs {
                    vendor: vendor_opt(vendor),
                    kind: "challenge",
                    resolved,
                });
            }

            if matches!(mode, CaptureMode::Png | CaptureMode::Both) {
                let cdp_format = match per_shot.format {
                    ImageFormat::Png => CaptureScreenshotFormat::Png,
                    ImageFormat::Jpeg => CaptureScreenshotFormat::Jpeg,
                    ImageFormat::Webp => CaptureScreenshotFormat::Webp,
                };
                let mut builder = CaptureScreenshotParams::builder()
                    .format(cdp_format)
                    .capture_beyond_viewport(per_shot.full_page);
                if let Some(q) = per_shot.quality {
                    builder = builder.quality(q as i64);
                }
                let t_shot = Instant::now();
                let bytes = page.screenshot(builder.build()).await?;
                log::trace!(
                    target: "onyxweb::engine",
                    "[{url}] screenshot: {} bytes ({:?}, format={:?})",
                    bytes.len(),
                    t_shot.elapsed(),
                    per_shot.format
                );
                out.png = Some(bytes);
            }

            // Drain accumulated console messages for this fetch.
            out.console_messages = std::mem::take(&mut *guard.console_messages().lock());
            if !out.console_messages.is_empty() {
                log::trace!(
                    target: "onyxweb::engine",
                    "[{url}] drained {} console messages",
                    out.console_messages.len()
                );
            }

            Ok::<_, OnyxError>(out)
        };

        let attempt = tokio::time::timeout(Duration::from_millis(timeout_ms), fut).await;
        if bypass_anti_bot
            && !healed
            && let Ok(Ok(out)) = &attempt
            && let Some(vendor) = block_vendor(out)
        {
            block_hit = Some(vendor);
            healed = true;
            log::info!(target: "onyxweb::engine", "[{url}] anti-bot block ({vendor}) — dropping anti-bot cookies + retrying once");
            guard.clear_antibot_cookies().await;
            continue;
        }
        break attempt;
    };

    // Per-call init script cleanup — runs unconditionally so we never leak
    // scripts to the next fetch on this pooled tab. Errors here are swallowed:
    // the page may already be in a bad state if we got here via a CDP failure,
    // and surfacing a cleanup error would mask the original cause.
    for id in &script_ids {
        let _ = page
            .execute(RemoveScriptToEvaluateOnNewDocumentParams::new(id.clone()))
            .await;
    }

    // Per-call URL-block cleanup — restore the Client-level baseline so the
    // per-call additions don't leak to the next fetch. Sending an empty
    // pattern list clears all blocks if the base list is itself empty.
    if !per_call.block_urls.is_empty() {
        let _ = page
            .execute(SetBlockedUrLsParams {
                url_patterns: Some(block_patterns(&base.network.block_urls)),
            })
            .await;
    }

    // Per-call extra-headers cleanup — restore the Client-level baseline so
    // per-call overrides don't leak to subsequent fetches on this pooled tab.
    // Mirrors the block_urls cleanup pattern; same swallow-error semantics
    // (cleanup failure must not mask the original cause). Triggers on either
    // per_call/per_shot extras OR a base Referer that we lifted out — both
    // paths called `setExtraHTTPHeaders` with a modified header set.
    if (!per_call.extra_headers.is_empty() || !per_shot.extra_headers.is_empty())
        && let Ok(json) = serde_json::to_value(&base.network.extra_headers)
    {
        let headers = chromiumoxide::cdp::browser_protocol::network::Headers::new(json);
        let _ = page.execute(SetExtraHttpHeadersParams::new(headers)).await;
    }

    // Per-call navigation-block cleanup — disable the Fetch domain this call
    // enabled (CDP auto-continues paused requests on disable). Skipped under an
    // authenticated proxy, where we never enabled our own Fetch (chromiumoxide
    // owns it) — disabling would break auth on the next fetch.
    if per_call.block_navigation && !guard.auth_fetch_active() {
        let _ = page.execute(FetchDisableParams::default()).await;
    }

    // On error, reset the page so the next URL on this tab isn't poisoned by
    // a half-loaded predecessor.
    if matches!(&fut_result, Err(_) | Ok(Err(_))) {
        // Reset the tab; if that fails (wedged by a stuck nav, or the target
        // died) mark it so the pool recreates it before the next fetch.
        let reset = tokio::time::timeout(Duration::from_secs(2), page.goto("about:blank")).await;
        if !matches!(reset, Ok(Ok(_))) {
            guard.mark_poisoned();
        }
    }

    let mut result = fut_result.map_err(|_| {
        log::warn!(target: "onyxweb::engine", "[{url}] nav timeout after {timeout_ms}ms");
        OnyxError::NavigationTimeout {
            timeout_ms,
            url: url.to_string(),
            wait_until: wait_until_label,
        }
    })??;

    // Anti-bot indicator. A challenge waited out in the fut already set
    // `result.anti_bot`. Otherwise (bypass off, or a block): a WAF hard block or
    // challenge stub still on the final page → unresolved; a block we healed
    // (now clean) → resolved. Detection runs regardless of `bypass_anti_bot`.
    if result.anti_bot.is_none() {
        let html = result.html.as_deref().unwrap_or_default();
        result.anti_bot = if let Some((vendor, kind)) = header_signal(&result) {
            Some(AntiBotRs {
                vendor: Some(vendor.to_string()),
                kind,
                resolved: false,
            })
        } else if let Some(v) = block_vendor(&result) {
            Some(AntiBotRs {
                vendor: vendor_opt(v),
                kind: "block",
                resolved: false,
            })
        } else if let Some(v) = challenge_vendor(html) {
            Some(AntiBotRs {
                vendor: vendor_opt(v),
                kind: "challenge",
                resolved: false,
            })
        } else {
            block_hit.map(|v| AntiBotRs {
                vendor: vendor_opt(v),
                kind: "block",
                resolved: true,
            })
        };
    }

    result.elapsed_s = (t0.elapsed().as_secs_f64() * 10000.0).round() / 10000.0;
    log::debug!(
        target: "onyxweb::engine",
        "[{url}] complete in {:.3}s (status={}, console_messages={})",
        result.elapsed_s,
        result.status_code,
        result.console_messages.len()
    );
    Ok(result)
}

/// Max time to wait for an anti-bot challenge interstitial to self-resolve.
const CHALLENGE_MAX_WAIT_MS: u64 = 12_000;
/// Poll interval while waiting for a challenge to resolve.
const CHALLENGE_POLL_MS: u64 = 500;

/// A real page behind these WAFs is large; a challenge interstitial is a tiny
/// stub. Signals that ALSO leak onto normal pages only count below this size.
const CHALLENGE_STUB_MAX_BYTES: usize = 15_000;

/// Anti-bot challenge/interstitial detector — a small "checking your browser /
/// verify you're human" page a WAF serves before the real one, which typically
/// self-reloads once its JS check passes.
///
/// Two tiers, because some markers also appear on the real page the WAF is
/// protecting (Cloudflare's passive `challenge-platform` beacon + embedded
/// Turnstile widgets; Imperva rewriting pages to reference `_Incapsula_Resource`):
///   - Interstitial-exclusive markers never appear on a passed page → a challenge
///     at any body size.
///   - Leaky markers + generic challenge phrases only count on a small stub; a
///     real page is never that tiny.
///
/// Extend either list as new vendors appear.
fn challenge_vendor(html: &str) -> Option<&'static str> {
    let lower = html.to_ascii_lowercase();
    // Interstitial-exclusive: absent from real pages. Any size.
    const STRONG_MARKERS: &[(&str, &str)] = &[
        ("akamai", "sec-if-cpt-container"),
        ("akamai", "scf-akamai"),
        ("cloudflare", "cf-browser-verification"),
        ("cloudflare", "cf_chl_"), // window._cf_chl_opt, cf_chl_opt, ...
        ("datadome", "captcha-delivery.com"),
        ("perimeterx", "px-captcha"),
        ("imperva", "incapsula incident"),
    ];
    if let Some((vendor, _)) = STRONG_MARKERS.iter().find(|(_, m)| lower.contains(m)) {
        return Some(vendor);
    }
    if html.len() >= CHALLENGE_STUB_MAX_BYTES {
        return None;
    }
    // Leaky markers — trusted only on a small stub, above: the passive
    // Cloudflare beacon, Imperva's `_Incapsula_Resource` rewriting, and the
    // reCAPTCHA/hCaptcha widget markers (which also appear on normal pages that
    // merely embed a captcha-protected form).
    const WEAK_MARKERS: &[(&str, &str)] = &[
        ("cloudflare", "cdn-cgi/challenge-platform"),
        ("cloudflare", "turnstile"),
        ("imperva", "_incapsula_resource"),
        ("imperva", "/_incapsula"),
        ("recaptcha", "g-recaptcha"),
        ("recaptcha", "grecaptcha"),
        ("hcaptcha", "h-captcha"),
        ("hcaptcha", "hcaptcha"),
    ];
    if let Some((vendor, _)) = WEAK_MARKERS.iter().find(|(_, m)| lower.contains(m)) {
        return Some(vendor);
    }
    const CHALLENGE_PHRASES: &[&str] = &[
        "checking your browser",
        "checking if the site connection is secure",
        "just a moment",
        "please wait while we",
        "verify you are human",
        "verifying you are human",
        "are you human",
        "enable javascript and cookies to continue",
        "attention required",
        "ddos protection by",
        "one moment please",
    ];
    if CHALLENGE_PHRASES.iter().any(|p| lower.contains(p)) {
        return Some("unknown");
    }
    None
}

fn looks_like_challenge(html: &str) -> bool {
    challenge_vendor(html).is_some()
}

/// Response-header / status WAF signals — preferred over body matching for
/// header-emitting vendors (precise, no false positives on real pages). Returns
/// `(vendor, kind)`. Checked before `block_vendor` so a challenge served with a
/// 403/429 isn't mislabeled a hard block.
fn header_signal(out: &CaptureOutput) -> Option<(&'static str, &'static str)> {
    let header = |name: &str| -> Option<String> {
        out.headers
            .iter()
            .find(|(k, _)| k.eq_ignore_ascii_case(name))
            .map(|(_, v)| v.to_ascii_lowercase())
    };
    // Cloudflare: `cf-mitigated` is present only on a challenge response (CF docs).
    if header("cf-mitigated").is_some_and(|v| v.contains("challenge")) {
        return Some(("cloudflare", "challenge"));
    }
    // AWS WAF: `x-amzn-waf-action` = challenge (202, silent auto-solve) or captcha
    // (405, interactive). Both are challenges; `resolved` reflects whether we
    // cleared it. The `aws-waf-token` cookie leaks onto passed pages — not used.
    if let Some(action) = header("x-amzn-waf-action")
        && (action.contains("challenge") || action.contains("captcha"))
    {
        return Some(("aws", "challenge"));
    }
    // Kasada: `x-kpsdk-ct` on a hard block (silent PoW, no interstitial). A recon
    // signal only — not defeatable CDP-only.
    if matches!(out.status_code, 403 | 429) && header("x-kpsdk-ct").is_some() {
        return Some(("kasada", "block"));
    }
    None
}

/// Anti-bot HARD block — a 403/429 with a WAF signature (server header or body),
/// vendor-agnostic; returns the vendor (`"unknown"` if only the body matched).
/// Distinct from a challenge interstitial (a 200 that self-resolves): a hard
/// block won't clear on its own, so bypass_anti_bot sheds the flagged cookies
/// and retries once. A plain 403 with no WAF signature is left alone.
fn block_vendor(out: &CaptureOutput) -> Option<&'static str> {
    // 403/429 are the usual WAF block codes; Fastly / Signal Sciences defaults
    // to 406. A signature is still required, so a plain 406 isn't flagged.
    if !matches!(out.status_code, 403 | 429 | 406) {
        return None;
    }
    // Join ALL `Server` header values — a CDN/proxy in front of the origin can
    // add its own, so the WAF's may not be the first.
    let server = out
        .headers
        .iter()
        .filter(|(k, _)| k.eq_ignore_ascii_case("server"))
        .map(|(_, v)| v.to_ascii_lowercase())
        .collect::<Vec<_>>()
        .join(" ");
    for vendor in ["akamai", "cloudflare", "imperva", "incapsula", "datadome"] {
        if server.contains(vendor) {
            return Some(if vendor == "incapsula" {
                "imperva"
            } else {
                vendor
            });
        }
    }
    let body = out.html.as_deref().unwrap_or_default().to_ascii_lowercase();
    let body_waf = [
        "access denied",
        "attention required",
        "you have been blocked",
        "incapsula incident",
        "reference #",
    ]
    .iter()
    .any(|p| body.contains(p));
    body_waf.then_some("unknown")
}

/// `"unknown"` (generic match, no identifiable vendor) → `None`; else the vendor.
fn vendor_opt(vendor: &str) -> Option<String> {
    (vendor != "unknown").then(|| vendor.to_string())
}
