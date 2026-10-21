mod auth;
mod error;
mod metrics;
mod proxy;
mod ratelimit;
mod router;
mod runtime;
mod server;
// validate/apply/store/load are driven by the config stream client (W2);
// standalone mode only validates through Router::compile.
#[allow(dead_code)]
mod snapshot;
mod telemetry;
mod usage;

/// Per-thread allocation counter for allocation regression tests.
#[cfg(test)]
pub(crate) mod test_alloc {
    use std::{
        alloc::{GlobalAlloc, Layout, System},
        cell::Cell,
    };

    thread_local! {
        static COUNT: Cell<(usize, usize)> = const { Cell::new((0, 0)) };
    }

    struct Counting;

    fn bump(bytes: usize) {
        let _ = COUNT.try_with(|count| {
            let (n, total) = count.get();
            count.set((n + 1, total + bytes));
        });
    }

    unsafe impl GlobalAlloc for Counting {
        unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
            bump(layout.size());
            unsafe { System.alloc(layout) }
        }
        unsafe fn alloc_zeroed(&self, layout: Layout) -> *mut u8 {
            bump(layout.size());
            unsafe { System.alloc_zeroed(layout) }
        }
        unsafe fn realloc(&self, ptr: *mut u8, layout: Layout, new_size: usize) -> *mut u8 {
            bump(new_size);
            unsafe { System.realloc(ptr, layout, new_size) }
        }
        unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
            unsafe { System.dealloc(ptr, layout) }
        }
    }

    #[global_allocator]
    static GLOBAL: Counting = Counting;

    /// (allocations, requested bytes) made by this thread while running `f`.
    pub fn count<T>(f: impl FnOnce() -> T) -> ((usize, usize), T) {
        let before = COUNT.with(Cell::get);
        let value = f();
        let after = COUNT.with(Cell::get);
        ((after.0 - before.0, after.1 - before.1), value)
    }
}

use std::{path::PathBuf, sync::Arc};

use anyhow::Context;
use arc_swap::ArcSwap;
use clap::Parser;
use lumewarp_config::{AuthMode, Config};
use sha2::{Digest, Sha256};

#[derive(Parser)]
#[command(name = "lumewarp", version, about = "Lumewarp LLM gateway")]
struct Args {
    /// Standalone YAML config.
    #[arg(long, short, env = "LUMEWARP_CONFIG")]
    config: PathBuf,
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let telemetry = tokio::task::spawn_blocking(telemetry::init).await??;
    let args = Args::parse();
    let yaml = std::fs::read_to_string(&args.config)
        .with_context(|| format!("loading {}", args.config.display()))?;
    let (settings, snapshot) = Config::parse(&yaml)?;
    let runtime = runtime::Runtime::new();
    let router = router::Router::compile(&snapshot, AuthMode::standalone(&snapshot), &runtime)
        .context("compiling router snapshot")?;
    runtime
        .metrics
        .set_yaml_hash(&hex::encode(Sha256::digest(yaml.as_bytes()))[..12]);
    runtime.metrics.config_applied(router.version);
    let snapshot = Arc::new(ArcSwap::from_pointee(router));
    let result = server::serve(&settings, snapshot, telemetry.is_some()).await;
    if let Some(provider) = telemetry {
        match tokio::task::spawn_blocking(move || provider.shutdown()).await {
            Ok(Ok(())) => {}
            Ok(Err(err)) => tracing::warn!(%err, "OTLP exporter shutdown failed"),
            Err(err) => tracing::warn!(%err, "OTLP exporter shutdown task failed"),
        }
    }
    result
}
