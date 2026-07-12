//! Process-wide tokio runtime, eager-init on module import. Python-driven
//! calls enter from non-runtime threads via `block_on()`.

use std::sync::Arc;

use once_cell::sync::OnceCell;
use tokio::runtime::{Builder, Runtime};

static RUNTIME: OnceCell<Arc<Runtime>> = OnceCell::new();

/// Get (or lazily init) the shared multi-threaded runtime. Defaults to CPU
/// count worker threads; override with `TOKIO_WORKER_THREADS`.
pub fn shared() -> Arc<Runtime> {
    RUNTIME
        .get_or_init(|| {
            let rt = Builder::new_multi_thread()
                .enable_all()
                .thread_name("onyxweb-tokio")
                .build()
                .expect("failed to build onyxweb tokio runtime");
            log::info!(target: "onyxweb::runtime", "tokio runtime initialized");
            Arc::new(rt)
        })
        .clone()
}
