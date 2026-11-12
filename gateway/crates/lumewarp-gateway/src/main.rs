mod a2a;
mod a2a_ids;
mod auth;
mod balance;
mod catalog;
mod config_stream;
mod endpoint;
mod error;
mod grants;
mod health;
mod mcp;
mod mcp_hash;
mod metrics;
#[cfg(test)]
mod model_types_tests;
mod multipart;
mod openapi;
mod probe;
mod proxy;
mod ratelimit;
mod request_body;
mod rewrite;
mod router;
mod runtime;
mod sealed;
mod server;
mod snapshot;
mod targets;
mod telemetry;
mod transform;
mod upstream_auth;
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

use std::{path::PathBuf, sync::Arc, time::Duration};

use anyhow::Context;
use arc_swap::ArcSwapOption;
use clap::{Parser, Subcommand};
use lumewarp_config::{AuthMode, Config, LocalSettings};
use sha2::{Digest, Sha256};

#[derive(Parser)]
#[command(
    name = "lumewarp",
    version,
    about = "Lumewarp LLM gateway",
    subcommand_negates_reqs = true
)]
struct Args {
    #[command(subcommand)]
    command: Option<Command>,
    /// Standalone YAML config.
    #[arg(
        long,
        short,
        env = "LUMEWARP_CONFIG",
        conflicts_with_all = ["control_plane", "gateway_token_env", "state_dir", "scope", "gateway_id", "local_config"],
        required_unless_present = "control_plane"
    )]
    config: Option<PathBuf>,
    /// Control-plane gRPC URL (http:// or https://); enables control-plane mode.
    #[arg(long, env = "LUMEWARP_CONTROL_PLANE", requires_all = ["gateway_token_env", "state_dir", "scope"])]
    control_plane: Option<String>,
    /// Name of the environment variable holding the gateway token.
    #[arg(long)]
    gateway_token_env: Option<String>,
    /// Directory for the last good snapshot (created 0700).
    #[arg(long)]
    state_dir: Option<PathBuf>,
    /// Control-plane scope this gateway serves.
    #[arg(long)]
    scope: Option<String>,
    /// Identity presented to the control plane.
    #[arg(long, default_value = "gateway")]
    gateway_id: String,
    /// YAML with only `server:` settings (listen, admin, timeouts).
    #[arg(long)]
    local_config: Option<PathBuf>,
}

#[derive(Subcommand)]
enum Command {
    /// List built-in and startup-loaded plugins.
    Plugins {
        /// Print the manifest as JSON.
        #[arg(long)]
        json: bool,
    },
    /// Print the gateway's OpenAPI document (JSON).
    Openapi,
}

fn list_strategies(json: bool) -> anyhow::Result<()> {
    let descriptors = lumewarp_plugin_host::global().descriptors();
    let manifest = descriptors.iter().map(|d| serde_json::json!({
        "kind": match d.kind { 1 => "router-strategy", 2 => "upstream-auth", _ => "a2a-middleware" }, "name": d.name, "title": d.title, "description": d.description,
        "builtin": d.builtin, "params_schema_json": d.params_schema_json,
        "router": d.router.as_ref().map(|r| serde_json::json!({"uses_weight": r.uses_weight, "needs": r.needs, "member_schema_json": r.member_schema_json})),
        "wasm_sha256": d.wasm_sha256, "digest": d.digest,
    })).collect::<Vec<_>>();
    if json {
        println!("{}", serde_json::to_string_pretty(&manifest)?);
    } else {
        for d in descriptors {
            println!("{}\t{}\t{}", d.kind, d.name, d.title);
        }
    }
    Ok(())
}

/// Activates a compiled table that is about to serve: starts its health probes
/// and publishes the plugin sets [`router::RoutingTable::compile`] staged, so the
/// first request after a boot reuses the plugins configured at boot instead of
/// configuring them again (N1). Every boot path — the standalone YAML and the
/// cached snapshot — goes through here.
fn activated(
    table: Result<router::RoutingTable, snapshot::ApplyError>,
) -> Result<router::RoutingTable, snapshot::ApplyError> {
    let mut table = table?;
    table.activate();
    Ok(table)
}

/// Standalone mode: the YAML is the whole configuration.
fn standalone(
    path: &std::path::Path,
) -> anyhow::Result<(LocalSettings, server::Snapshot, Arc<runtime::Runtime>)> {
    let yaml =
        std::fs::read_to_string(path).with_context(|| format!("loading {}", path.display()))?;
    let (settings, snapshot) = Config::parse(&yaml)?;
    let runtime = runtime::Runtime::from_env();
    let routing_table = activated(router::RoutingTable::compile(
        &snapshot,
        AuthMode::standalone(&snapshot),
        &runtime,
    ))
    .context("compiling routing table snapshot")?;
    runtime
        .metrics
        .set_yaml_hash(&hex::encode(Sha256::digest(yaml.as_bytes()))[..12]);
    runtime.metrics.config_applied(routing_table.version);
    runtime
        .observed
        .retain_servers(routing_table.targets.servers().map(|s| s.name.as_ref()));
    Ok((
        settings,
        Arc::new(ArcSwapOption::from_pointee(routing_table)),
        runtime,
    ))
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let args = Args::parse();
    // Before the plugin host: printing the document needs no plugin directory.
    if let Some(Command::Openapi) = args.command {
        println!("{}", serde_json::to_string_pretty(&openapi::document())?);
        return Ok(());
    }
    lumewarp_plugin_host::initialize()?;
    if let Some(Command::Plugins { json }) = args.command {
        return list_strategies(json);
    }
    let telemetry = tokio::task::spawn_blocking(telemetry::init).await??;
    let mut stream = None;
    let mut reporter = None;
    let (settings, snapshot, runtime) = match (&args.config, &args.control_plane) {
        (Some(path), _) => standalone(path)?,
        (None, Some(url)) => {
            let env_name = args
                .gateway_token_env
                .clone()
                .context("--gateway-token-env")?;
            let token = std::env::var(&env_name)
                .ok()
                .filter(|token| !token.is_empty())
                .with_context(|| format!("gateway token variable {env_name} is not set"))?;
            let scope = args.scope.clone().context("--scope")?;
            let state_dir = args.state_dir.clone().context("--state-dir")?;
            let settings = match &args.local_config {
                Some(path) => {
                    let yaml = std::fs::read_to_string(path)
                        .with_context(|| format!("loading {}", path.display()))?;
                    Config::from_yaml(&yaml)?.into_parts()?.0
                }
                None => Config::from_yaml("{}")?.into_parts()?.0,
            };

            let metrics = Arc::new(metrics::Metrics::new());
            let (sink, handle) = usage::spawn_reporter(
                usage::ReporterConfig {
                    endpoint: url.clone(),
                    token: token.clone(),
                    gateway_id: args.gateway_id.clone(),
                    capacity: usage::ReporterConfig::DEFAULT_CAPACITY,
                    batch_max: usage::ReporterConfig::DEFAULT_BATCH_MAX,
                    flush_interval: usage::ReporterConfig::DEFAULT_FLUSH_INTERVAL,
                },
                metrics.clone(),
            );
            reporter = Some(handle);
            let runtime = runtime::Runtime::with_usage_and_env(
                metrics,
                sink,
                runtime::EnvOptions::from_env(),
            );

            // Serve a valid cached snapshot immediately; otherwise stay
            // not-ready (never an open routing table) until the first full arrives.
            let mut current = lumewarp_proto::config::v1::Snapshot {
                scope: scope.clone(),
                ..Default::default()
            };
            let slot = Arc::new(ArcSwapOption::empty());
            let mut digest = Vec::new();
            let mut has_base = false;
            match snapshot::load_with_digest(&state_dir, &scope) {
                Ok(Some((cached, cached_digest))) => {
                    match activated(config_stream::compile(&cached, &runtime)) {
                        Ok(routing_table) => {
                            tracing::info!(
                                version = cached.version,
                                "booting from cached snapshot"
                            );
                            runtime.metrics.config_applied(cached.version);
                            slot.store(Some(Arc::new(routing_table)));
                            current = cached;
                            digest = cached_digest;
                            has_base = true;
                        }
                        Err(err) => tracing::error!(%err, "cached snapshot rejected; not ready"),
                    }
                }
                Ok(None) => tracing::info!("no cached snapshot; not ready until the first full"),
                Err(err) => tracing::error!(%err, "cached snapshot unusable; not ready"),
            }
            stream = Some(config_stream::spawn(
                config_stream::StreamConfig {
                    url: url.clone(),
                    token,
                    gateway_id: args.gateway_id.clone(),
                    scope,
                    state_dir,
                },
                current,
                digest,
                has_base,
                slot.clone(),
                runtime.clone(),
            )?);
            (settings, slot, runtime)
        }
        (None, None) => unreachable!("clap requires --config or --control-plane"),
    };
    let result = server::serve(&settings, snapshot, runtime, telemetry.is_some()).await;
    if let Some(stream) = stream {
        stream.abort();
    }
    if let Some(reporter) = reporter {
        reporter.shutdown(Duration::from_secs(5)).await;
    }
    if let Some(provider) = telemetry {
        match tokio::task::spawn_blocking(move || provider.shutdown()).await {
            Ok(Ok(())) => {}
            Ok(Err(err)) => tracing::warn!(%err, "OTLP exporter shutdown failed"),
            Err(err) => tracing::warn!(%err, "OTLP exporter shutdown task failed"),
        }
    }
    result
}
