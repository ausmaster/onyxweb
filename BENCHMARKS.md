# Screenshot engine benchmarks

BBOT-scale subdomain screenshot pumping. One workload, multiple engines, honest numbers.

## Test setup

- **Host**: Linux 6.19.10-203.nobara.fc43 (Nobara 43 / Fedora 43), 16 CPU cores, 24 GB free RAM
- **URLs**: 48–50 URLs from `servo_spike/urls_bench_big.txt` (mixed public sites; `gnu.org` and `google.com` excluded from `/tmp/urls_stable.txt` for the CDP/CEF head-to-head — the first due to network flakiness, the second due to headless anti-bot variance)
- **Viewport**: 1200×800
- **Timeout**: 30–45s per URL
- **Build**: all release-mode with `lto = "thin"`, `codegen-units = 1`
- **Measurement**: `/usr/bin/time` for wall + peak RSS
- **Metric**: URL/second sustained (ok_count / wall_time)

Important caveat on RSS: `/usr/bin/time` captures peak RSS of the wrapped process only — for chromiumoxide it UNDERCOUNTS (Chromium is a subprocess not counted); for cef_spike warm mode it's accurate (everything in-process).

## Headline table — fully rendered URL → PNG

| Engine | Best Config | **URL/s** | Peak per-proc RAM | Binary + runtime | Compat |
|---|---|---|---|---|---|
| Servo 0.1.0 (in-process) | cold P=8 | 1.13 | 1.6 GB | 121 MB (self-contained) | 45–46/50 |
| Chromium CLI (fork per URL) | cold P=16 | 4.51 | 310 MB | system chromium | 50/50 |
| Chromium via Python CDP | warm P=16 | 5.46 | ~500 MB | system chromium + Python | 50/50 |
| **CEF via tauri-apps/cef-rs (Option A)** | **cold P=16** | **6.21** | 331 MB | 4 MB bin + 1.3 GB libcef.so | 48/48 |
| **Chromium via chromiumoxide (CDP)** | warm P=32 | **9.30** | ~430 MB | 6 MB + system chromium | 50/50 |
| **Chromium via chromiumoxide + chrome-headless-shell** | **warm P=16** | **8.66** | ~400 MB | 6 MB + 190 MB shell | 47–48/48 |

**Winner for BBOT-scale**: chromiumoxide (Rust-native CDP client). With bundled chrome-headless-shell it's self-contained, 8.66 URL/s, ~350 MB wheel. The "bundle CEF" path (Option A) is ~30% slower AND ~4× larger to distribute.

## Deep data: Option A vs Option C head-to-head (48 URLs stable list)

### Option A — CEF via tauri-apps/cef-rs (in-process)

| Config | Wall (s) | OK/48 | URL/s | Peak MB |
|---|---|---|---|---|
| cef cold P=1 | 63.63 | 48 | 0.75 | 330 |
| cef cold P=4 | 17.08 | 48 | 2.81 | 328 |
| cef cold P=8 | 10.08 | 48 | 4.76 | 330 |
| **cef cold P=16** | **7.73** | 48 | **6.21** | 331 |
| cef warm P=1 | 47.91 | 48 | 1.00 | 1023 |
| cef warm P=4 | 16.36 | 48 | 2.93 | 1025 |
| cef warm P=8 | 15.30 | 48 | 3.14 | 1024 |
| cef warm P=16 | 10.76 | 48 | 4.46 | 999 |

**Counterintuitive finding**: warm mode (one CEF init, N browsers in flight) is SLOWER than cold (N separate CEF processes) at P=16. Reason: CEF's single main thread serializes paint/event dispatch for all browsers in one instance. Cold-parallel gives each browser its own main thread. For BBOT-scale, **always use cold-process parallelism with CEF, not warm-batch**.

### Option C — chromiumoxide (CDP via WebSocket)

| Config | Wall (median, 3 runs) | OK/48 | URL/s |
|---|---|---|---|
| sys chromium P=16 PNG | 6.22 | 48/48 | 7.72 |
| **headless-shell 148 P=16 PNG** | **5.43** | 47/48 | **8.66** |
| headless-shell 148 P=16 PNG+HTML | 5.60 | 48/48 | 8.57 |
| sys chromium P=8 PNG | 20.49 | 47/48 | 2.29 (straggler-dominated) |
| headless-shell 148 P=8 PNG | 7.81 | 47/48 | 6.02 |
| headless-shell 148 P=8 PNG+HTML | 7.45 | 48/48 | 6.44 |

**chrome-headless-shell 148** (Google's automation-optimized chromium variant) is ~12% faster than the full system chromium 146 at the same P. HTML extraction via `page.content()` (post-JS `document.documentElement.outerHTML`) is essentially free — same P, same wall time.

## Decision-rubric recap

| Concern | Option A (CEF) | Option C (chromiumoxide) |
|---|---|---|
| Peak throughput | 6.21 URL/s | **8.66 URL/s** (40% higher) |
| Distribution size | ~1.5 GB (libcef.so + resources + bin) | ~350 MB (wheel + bundled headless-shell) |
| Bundle chromium with wheel | ✓ (that's the point) | ✓ also possible (ship headless-shell alongside) |
| Headless server (no X) | ✗ (needs Xvfb) | **✓** (headless-first) |
| Build complexity | High (wrap macros, multi-process dispatch, sandbox, cache-path collision handling) | Low (one async function) |
| Rust code lines for basic use | ~500 | ~200 |
| Deeper integration possible | ✓ in-process API | ✗ via CDP only |
| Version pinning | ✓ (link libcef version) | ✓ (pin headless-shell download) |
| Time to working spike | ~1 day | ~2 hours |
| External deps at runtime | libcef + libX11 + lots | chrome binary + glibc |

Option A (CEF) has NO win that matters for BBOT-scale headless screenshot pumping. Its unique strength — in-process API — is unused by a fetch-load-screenshot-exit workflow. Its unique cost — 4× larger distribution, X11 requirement, multi-process dispatch complexity — is paid every day.

## Conclusion

**Build onyxweb on chromiumoxide, not CEF.** Ship chrome-headless-shell 148 alongside the wheel (~350 MB total). Target:

- ~9 URL/s per process at sweet-spot concurrency (P=16–32)
- HTML extraction free-of-charge via `page.content()`
- ~400 MB RSS per process; 4 parallel processes handle ~35 URL/s, network-bound beyond
- Wheel distribution: unpacks `chrome-headless-shell` into site-packages
- Python API: `onyxweb.Client()`, `client.screenshot(url)`, `client.batch(urls, concurrency=16)`

10,000 URLs → ~18 min single process, ~5 min with 4 parallel processes.

## Reproduction

```bash
# Option A — CEF
cd experiments/cef_spike
cargo build --release                        # first build downloads CEF 146 binaries (~300MB)
DISPLAY=:0 LD_LIBRARY_PATH=$(find target -name libcef.so | xargs dirname) ./bench.sh

# Option C — chromiumoxide + chrome-headless-shell
cd experiments/chromiumoxide_spike
cargo build --release                        # 90s
curl -sSL -o /tmp/hs.zip https://storage.googleapis.com/chrome-for-testing-public/148.0.7778.56/linux64/chrome-headless-shell-linux64.zip
unzip -q /tmp/hs.zip -d /tmp
HS=/tmp/chrome-headless-shell-linux64/chrome-headless-shell
./target/release/chromiumoxide_spike --chrome $HS --out-dir /tmp/shots --concurrency 16 --mode both < urls.txt
```

All raw benchmark artifacts under `experiments/{cef_spike,chromiumoxide_spike,servo_spike}/bench_*/`.
