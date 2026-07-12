//! Chrome binary resolver. Priority: explicit arg → bundled → system → PATH.

use std::path::{Path, PathBuf};

use crate::config::ChromeEngine;
use crate::error::{OnyxError, Result};

/// Platform identifier used to pick the right bundled subdir.
pub fn platform_subdir() -> &'static str {
    #[cfg(all(target_os = "linux", target_arch = "x86_64"))]
    return "linux_x86_64";
    #[cfg(all(target_os = "linux", target_arch = "aarch64"))]
    return "linux_aarch64";
    #[cfg(all(target_os = "macos", target_arch = "x86_64"))]
    return "darwin_x86_64";
    #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
    return "darwin_aarch64";
    #[cfg(all(target_os = "windows", target_arch = "x86_64"))]
    return "windows_x86_64";
    // Fallback
    #[allow(unreachable_code)]
    "unknown"
}

/// Canonical bundled binary filename per platform + engine.
pub fn chrome_binary_name(engine: ChromeEngine) -> &'static str {
    match engine {
        ChromeEngine::HeadlessShell => {
            #[cfg(target_os = "windows")]
            return "chrome-headless-shell.exe";
            #[allow(unreachable_code)]
            "chrome-headless-shell"
        }
        ChromeEngine::Full => {
            #[cfg(target_os = "windows")]
            return "chrome.exe";
            #[allow(unreachable_code)]
            "chrome"
        }
    }
}

/// Resolve the chrome binary path. `explicit` is populated from both
/// `Client(chrome_path=...)` and `ONYXWEB_CHROME__PATH` env (via pydantic).
pub fn resolve(explicit: Option<&str>, engine: ChromeEngine) -> Result<PathBuf> {
    if let Some(p) = explicit {
        let path = PathBuf::from(p);
        if path.is_file() {
            log::debug!(target: "onyxweb::chrome", "resolved from explicit arg: {p}");
            return Ok(path);
        }
        return Err(OnyxError::ChromeNotFound(format!(
            "explicit chrome_path {p:?} not a file"
        )));
    }

    // The `full` engine needs a real Chrome (system chromium/chrome), NOT the
    // bundled headless-shell — only look for a bundled binary matching the engine.
    if let Some(bundled) = find_bundled(engine) {
        log::debug!(target: "onyxweb::chrome", "resolved from bundled: {}", bundled.display());
        return Ok(bundled);
    }

    for candidate in &[
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/google-chrome",
        "/usr/local/bin/chromium",
        "/usr/local/bin/chrome",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ] {
        let p = Path::new(candidate);
        if p.is_file() {
            log::debug!(target: "onyxweb::chrome", "resolved from system: {candidate}");
            return Ok(p.to_path_buf());
        }
    }

    for name in &["chromium-browser", "chromium", "google-chrome", "chrome"] {
        if let Ok(p) = which_on_path(name) {
            log::debug!(target: "onyxweb::chrome", "resolved from PATH: {}", p.display());
            return Ok(p);
        }
    }

    Err(OnyxError::ChromeNotFound(format!(
        "chrome-headless-shell not found for platform {plat}. Fix by either:\n\
         \x20 - running `onyxweb --install` (or `onyxweb-download-chrome`) to \
         fetch the pinned chrome-headless-shell into the installed package, or\n\
         \x20 - installing a system chromium (`chromium`, `chromium-browser`, \
         `chrome`, or `google-chrome` on PATH), or\n\
         \x20 - pointing onyxweb at an existing binary with \
         `Client(chrome_path=...)` or `ONYXWEB_CHROME__PATH=...`.",
        plat = platform_subdir(),
    )))
}

/// Look for `_binaries/<platform>/<binary>` under the installed package dir
/// (`ONYXWEB_PKG_DIR`, set by `python/onyxweb/__init__.py` at import) or —
/// for dev builds — under `CARGO_MANIFEST_DIR/python/onyxweb`.
fn find_bundled(engine: ChromeEngine) -> Option<PathBuf> {
    let plat = platform_subdir();
    let bin = chrome_binary_name(engine);
    // The full build ships in a `full/` subdir so its support files
    // (icudtl.dat, locales/, ...) don't collide with the shell's.
    let rel = match engine {
        ChromeEngine::HeadlessShell => format!("_binaries/{plat}/{bin}"),
        ChromeEngine::Full => format!("_binaries/{plat}/full/{bin}"),
    };

    if let Ok(pkg) = std::env::var("ONYXWEB_PKG_DIR") {
        let p = Path::new(&pkg).join(&rel);
        if p.is_file() {
            return Some(p);
        }
    }
    if let Ok(manifest_dir) = std::env::var("CARGO_MANIFEST_DIR") {
        let p = Path::new(&manifest_dir).join("python/onyxweb").join(&rel);
        if p.is_file() {
            return Some(p);
        }
    }
    None
}

fn which_on_path(name: &str) -> Result<PathBuf> {
    let path = std::env::var("PATH")
        .map_err(|_| OnyxError::ChromeNotFound("PATH env not set".to_string()))?;
    for dir in path.split(':') {
        if dir.is_empty() {
            continue;
        }
        let candidate = Path::new(dir).join(name);
        if candidate.is_file() {
            return Ok(candidate);
        }
    }
    Err(OnyxError::ChromeNotFound(format!("{name} not on PATH")))
}
