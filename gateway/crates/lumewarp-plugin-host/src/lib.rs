//! Startup-only component loading, the router strategy adapter and the
//! upstream-auth request adapter. No WASI imports are linked.
//!
//! Guests run as wasmtime *async* components: a call is a future that yields to
//! its executor at every epoch tick (`epoch_deadline_async_yield_and_update`),
//! so a long-running plugin never pins a tokio worker. The epoch is a fairness
//! mechanism only — it never traps. A call ends when it returns, when it traps,
//! or when the caller drops it (its own timeout, or the client going away); a
//! dropped call's Store is discarded rather than pooled.

use anyhow::{bail, Context, Result};
use std::sync::atomic::{AtomicU64, Ordering};
use std::{
    collections::HashMap,
    path::Path,
    sync::{Arc, OnceLock},
    time::Duration,
};

/// The gateway registers fixed-cardinality Prometheus series and installs this
/// hook once. Built-ins never enter it. No settings or error text are exposed.
pub trait Metrics: Send + Sync {
    fn record(&self, kind: &str, plugin: &str, result: &str, seconds: f64);
}
static METRICS: OnceLock<Arc<dyn Metrics>> = OnceLock::new();
pub fn install_metrics(metrics: Arc<dyn Metrics>) -> Result<()> {
    METRICS
        .set(metrics)
        .map_err(|_| anyhow::anyhow!("plugin metrics already installed"))
}
/// Records one call: `ok`, `error`, `timeout` or `trap`.
pub(crate) fn record(kind: &str, plugin: &str, result: &str, elapsed: Duration) {
    if let Some(metrics) = METRICS.get() {
        metrics.record(kind, plugin, result, elapsed.as_secs_f64());
    }
}

use lumewarp_proto::config::v1::{PluginDescriptor, RouterStrategyInfo};
use serde::Deserialize;
use serde_json::Value;
use sha2::{Digest, Sha256};
use wasmtime::{
    component::{Component, Linker},
    Config, Engine, ResourceLimiter, Store, StoreLimits,
};

pub mod limits;

mod pool;

pub mod router {
    wasmtime::component::bindgen!({ path: "../../wit", world: "router", exports: { default: async } });
}
pub mod auth {
    // Both directions are async: `http-fetch` awaits inside the guest's call, so
    // the executor is free while the identity provider answers.
    wasmtime::component::bindgen!({
        path: "../../wit",
        world: "auth",
        imports: { default: async },
        exports: { default: async },
    });
}
pub mod middleware {
    wasmtime::component::bindgen!({ path: "../../wit", world: "middleware", exports: { default: async } });
}
#[path = "middleware.rs"]
pub mod a2a_middleware;
#[path = "auth.rs"]
pub mod authenticator;
pub mod fetch;
pub use router::exports::lumewarp::plugin::router_strategy as types;
pub const ABI: u32 = 1;

/// One epoch tick. A guest yields to its executor at most this often.
pub const EPOCH_TICK: Duration = Duration::from_micros(500);

/// Longest manifest `title` accepted (the control plane's limit).
pub const MAX_TITLE_CHARS: usize = 200;

/// Longest manifest `description` accepted (the control plane's limit).
pub const MAX_DESCRIPTION_CHARS: usize = 2000;

/// Consecutive traps/errors/timeouts that pause one plugin.
pub const BREAKER_FAILURES: u32 = 5;

/// How long a tripped breaker pauses its plugin.
pub const BREAKER_PAUSE: Duration = Duration::from_secs(10);

/// Longest an apply-time `configure`/pre-warm may take, for any plugin kind.
/// Expiry refuses the version (`INVALID_CONFIG`) instead of wedging the config
/// stream; request-path calls are bounded by their own configured timeout.
pub const CONFIGURE_TIMEOUT: Duration = Duration::from_secs(60);

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Manifest {
    abi: u32,
    kind: String,
    name: String,
    title: String,
    description: String,
    params_schema: Value,
    router: Option<RouterManifest>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RouterManifest {
    uses_weight: bool,
    needs: Vec<String>,
    member_schema: Value,
}

/// The `host` interface of the auth world, as the adapter provides it.
///
/// `http-fetch` is async: the guest's call suspends and the request future runs
/// on the executor, so a slow identity provider never blocks a thread.
pub trait AuthHost: Send + Sync {
    fn http_fetch<'a>(
        &'a self,
        request: auth::lumewarp::plugin::host::HttpRequest,
    ) -> limits::BoxFuture<'a, Result<auth::lumewarp::plugin::host::HttpResponse, String>>;
    fn secret(&self, name: &str) -> Option<String>;
    fn now_unix_ms(&self) -> u64;
}

/// Per-Store state: the static limits, the process memory budget and the host
/// adapter of the call currently running.
pub struct State {
    limits: StoreLimits,
    budget: Arc<limits::Budget>,
    reserved: usize,
    pending: usize,
    pub auth_host: Option<Arc<dyn AuthHost>>,
}

impl State {
    pub fn new(budget: Arc<limits::Budget>) -> Self {
        Self {
            limits: limits::store_limits(),
            budget,
            reserved: 0,
            pending: 0,
            auth_host: None,
        }
    }
}

impl Default for State {
    fn default() -> Self {
        Self::new(Arc::clone(limits::memory_budget()))
    }
}

impl Drop for State {
    fn drop(&mut self) {
        // Wasm memory never shrinks: the reservation lasts as long as the Store.
        self.budget.release(self.reserved);
    }
}

impl ResourceLimiter for State {
    fn memory_growing(
        &mut self,
        current: usize,
        desired: usize,
        maximum: Option<usize>,
    ) -> Result<bool> {
        if !self.limits.memory_growing(current, desired, maximum)? {
            return Ok(false);
        }
        let delta = desired.saturating_sub(current);
        if !self.budget.try_reserve(delta) {
            return Ok(false);
        }
        self.reserved += delta;
        self.pending = delta;
        Ok(true)
    }

    fn memory_grow_failed(&mut self, error: anyhow::Error) -> Result<()> {
        // Growth may still fail after the limiter allowed it (OS out of memory).
        if self.pending > 0 {
            self.budget.release(self.pending);
            self.reserved = self.reserved.saturating_sub(self.pending);
            self.pending = 0;
        }
        self.limits.memory_grow_failed(error)
    }

    fn table_growing(
        &mut self,
        current: usize,
        desired: usize,
        maximum: Option<usize>,
    ) -> Result<bool> {
        self.limits.table_growing(current, desired, maximum)
    }

    fn table_grow_failed(&mut self, error: anyhow::Error) -> Result<()> {
        self.limits.table_grow_failed(error)
    }

    fn instances(&self) -> usize {
        self.limits.instances()
    }

    fn tables(&self) -> usize {
        self.limits.tables()
    }

    fn memories(&self) -> usize {
        self.limits.memories()
    }
}

impl auth::lumewarp::plugin::types::Host for State {}
impl auth::lumewarp::plugin::host::Host for State {
    async fn http_fetch(
        &mut self,
        request: auth::lumewarp::plugin::host::HttpRequest,
    ) -> Result<auth::lumewarp::plugin::host::HttpResponse, String> {
        let host = self
            .auth_host
            .clone()
            .ok_or_else(|| "host HTTP adapter is not installed".to_string())?;
        host.http_fetch(request).await
    }

    async fn secret(&mut self, name: String) -> Option<String> {
        self.auth_host.as_ref().and_then(|host| host.secret(&name))
    }

    async fn now_unix_ms(&mut self) -> u64 {
        self.auth_host.as_ref().map_or(0, |host| host.now_unix_ms())
    }
}

/// The one engine every plugin shares: compiled components, the epoch ticker and
/// the wasmtime code registry are process-wide.
pub fn engine() -> &'static Engine {
    static ENGINE: OnceLock<Engine> = OnceLock::new();
    ENGINE.get_or_init(|| {
        let mut config = Config::new();
        config
            .wasm_component_model(true)
            .async_support(true)
            .epoch_interruption(true);
        let engine = Engine::new(&config).expect("wasmtime engine");
        let weak = engine.weak();
        std::thread::Builder::new()
            .name("plugin-epoch".to_owned())
            .spawn(move || loop {
                std::thread::sleep(EPOCH_TICK);
                // Stop when the engine is gone (the process is exiting).
                let Some(engine) = weak.upgrade() else { break };
                engine.increment_epoch();
            })
            .expect("epoch ticker thread");
        engine
    })
}

/// A Store with the host limits and the fairness epoch (never a trap).
pub fn store(engine: &Engine) -> Store<State> {
    store_with_budget(engine, Arc::clone(limits::memory_budget()))
}

/// A Store whose wasm memory counts against `budget` instead of the process
/// budget (tests, and a deployment that wants one budget per plugin set).
pub fn store_with_budget(engine: &Engine, budget: Arc<limits::Budget>) -> Store<State> {
    let mut store = Store::new(engine, State::new(budget));
    // `State` is the limiter: the static per-Store limits and the process
    // memory budget are enforced by one `ResourceLimiter` impl.
    store.limiter(|state| state);
    // Yield to the executor at every tick and keep running: never interrupt.
    store.epoch_deadline_async_yield_and_update(1);
    store.set_epoch_deadline(1);
    store
}

/// Whether a wasmtime error is the epoch deadline of [`set_apply_deadline`].
pub fn is_epoch_trap(error: &wasmtime::Error) -> bool {
    error.downcast_ref::<wasmtime::Trap>() == Some(&wasmtime::Trap::Interrupt)
}

/// Epoch ticks covering `budget` (one tick is [`EPOCH_TICK`]).
pub fn epoch_ticks(budget: Duration) -> u64 {
    u64::try_from(budget.as_micros().div_ceil(EPOCH_TICK.as_micros()))
        .unwrap_or(u64::MAX)
        .max(1)
}

/// Gives a Store a hard apply-time budget: once it elapses the guest **traps**
/// instead of yielding.
///
/// A timer alone cannot cut a guest that spins and re-wakes itself — it never
/// lets the executor park, so the deadline is enforced by the epoch instead
/// (M4). Only apply-time `configure` uses this; request-path calls have no
/// fixed limit and yield for fairness only.
pub fn set_apply_deadline(store: &mut Store<State>, budget: Duration) {
    store.epoch_deadline_trap();
    store.set_epoch_deadline(epoch_ticks(budget));
}

/// Restores the request-path behaviour after an apply-time `configure`: the
/// guest yields at every tick instead of trapping, so a call can be cancelled
/// by its own timeout or by the caller going away.
pub fn set_fair_epoch(store: &mut Store<State>) {
    store.epoch_deadline_async_yield_and_update(1);
    store.set_epoch_deadline(1);
}

/// Drives a future to completion for apply-time work (`configure`, pre-warming).
///
/// Never call this on a request: it runs on its own thread and runtime so it
/// works from a tokio worker, a blocking task or a plain thread alike.
pub(crate) fn block_on<F>(future: F) -> F::Output
where
    F: std::future::Future + Send,
    F::Output: Send,
{
    fn runtime() -> &'static tokio::runtime::Runtime {
        static RUNTIME: OnceLock<tokio::runtime::Runtime> = OnceLock::new();
        RUNTIME.get_or_init(|| {
            tokio::runtime::Builder::new_multi_thread()
                .worker_threads(1)
                .enable_all()
                .thread_name("plugin-apply")
                .build()
                .expect("plugin apply runtime")
        })
    }
    std::thread::scope(|scope| {
        scope
            .spawn(move || runtime().block_on(future))
            .join()
            .expect("plugin apply thread panicked")
    })
}

/// [`block_on`] with a bound: `None` when `timeout` elapses first.
///
/// The bound runs inside the apply runtime so the timer is driven; the plugin's
/// own future is dropped then, which discards any Store it created.
pub(crate) fn block_on_timeout<F>(future: F, timeout: Duration) -> Option<F::Output>
where
    F: std::future::Future + Send,
    F::Output: Send,
{
    block_on(async move { tokio::time::timeout(timeout, future).await.ok() })
}

/// The component worlds a plugin can implement, prepared once at load.
pub enum Prepared {
    Router(router::RouterPre<State>),
    Auth(auth::AuthPre<State>),
    Middleware(middleware::MiddlewarePre<State>),
}

fn build_linker(engine: &Engine, component: &Component, kind: i32) -> Result<Linker<State>> {
    let mut linker = Linker::new(engine);
    // Rust's wasm32-wasip2 std retains WASI imports for panic paths. These
    // interfaces have no implementation: every imported function traps.
    // Unknown non-WASI imports must still fail instantiation.
    for (name, _) in component.component_type().imports(engine) {
        if !name.starts_with("wasi:")
            && name != "lumewarp:plugin/types@1.0.0"
            && !(kind == 2 && name == "lumewarp:plugin/host@1.0.0")
        {
            bail!("unsupported component import {name}");
        }
    }
    linker.define_unknown_imports_as_traps(component)?;
    if kind == 2 {
        linker.allow_shadowing(true);
        auth::Auth::add_to_linker::<State, wasmtime::component::HasSelf<State>>(
            &mut linker,
            |state| state,
        )?;
    }
    Ok(linker)
}

pub struct Plugin {
    component: Component,
    prepared: Prepared,
    pub descriptor: PluginDescriptor,
    /// Calls cut short by their configured timeout.
    pub timeouts: AtomicU64,
    /// Calls that trapped or could not get an instance.
    pub traps: AtomicU64,
    /// Consecutive failures, for the breaker.
    failures: std::sync::atomic::AtomicU32,
    /// Process-relative ms before which the breaker keeps the plugin paused.
    paused_until: AtomicU64,
}

impl Plugin {
    /// The shared engine.
    pub fn engine(&self) -> &'static Engine {
        engine()
    }

    pub fn component(&self) -> &Component {
        &self.component
    }

    /// The world bindings, pre-linked once per component.
    pub fn prepared(&self) -> &Prepared {
        &self.prepared
    }

    /// A new Store for this plugin. The adapter instantiates and configures it.
    pub fn store(&self) -> Store<State> {
        store(self.engine())
    }

    /// A linker for this component, for an adapter that builds its own bindings.
    pub fn linker(&self) -> Result<Linker<State>> {
        build_linker(self.engine(), &self.component, self.descriptor.kind)
    }

    /// Whether the breaker is holding this plugin back.
    pub fn paused(&self) -> bool {
        limits::mono_ms() < self.paused_until.load(Ordering::Relaxed)
    }

    /// One trap, error or timeout: enough in a row pause the plugin.
    pub fn record_failure(&self) {
        let failures = self.failures.fetch_add(1, Ordering::Relaxed) + 1;
        if failures >= BREAKER_FAILURES {
            self.failures.store(0, Ordering::Relaxed);
            self.paused_until.store(
                limits::mono_ms().saturating_add(BREAKER_PAUSE.as_millis() as u64),
                Ordering::Relaxed,
            );
        }
    }

    /// A call that answered: the breaker starts over.
    pub fn record_success(&self) {
        self.failures.store(0, Ordering::Relaxed);
    }

    /// Clears the breaker, including an open pause (operations and tests).
    pub fn reset_breaker(&self) {
        self.failures.store(0, Ordering::Relaxed);
        self.paused_until.store(0, Ordering::Relaxed);
    }
}

#[derive(Default)]
pub struct Registry {
    plugins: HashMap<(i32, String), Arc<Plugin>>,
}
static REGISTRY: OnceLock<Registry> = OnceLock::new();
pub fn global() -> &'static Registry {
    REGISTRY.get_or_init(Registry::default)
}
pub fn initialize() -> Result<()> {
    install(Registry::from_env()?)
}

/// Installs a registry built by a test or a CLI without the environment.
pub fn install(registry: Registry) -> Result<()> {
    REGISTRY
        .set(registry)
        .map_err(|_| anyhow::anyhow!("plugins already initialized"))
}
impl Registry {
    pub fn from_env() -> Result<Self> {
        let mut registry = Self::default();
        for (var, kind) in [
            ("LUMEWARP_ROUTER_PLUGIN_DIR", 1),
            ("LUMEWARP_AUTH_PLUGIN_DIR", 2),
            ("LUMEWARP_A2A_MIDDLEWARE_DIR", 3),
        ] {
            if let Some(dir) = std::env::var_os(var).filter(|v| !v.is_empty()) {
                registry.load_dir(Path::new(&dir), kind)?;
            }
        }
        Ok(registry)
    }
    pub fn load_dir(&mut self, dir: &Path, kind: i32) -> Result<()> {
        let mut files = std::fs::read_dir(dir)
            .with_context(|| format!("plugin directory {}", dir.display()))?
            .map(|e| e.map(|e| e.path()))
            .collect::<std::io::Result<Vec<_>>>()?;
        files.sort();
        for file in files
            .into_iter()
            .filter(|p| p.extension().is_some_and(|e| e == "wasm"))
        {
            self.load_file(&file, kind)
                .with_context(|| format!("plugin {}", file.display()))?;
        }
        Ok(())
    }
    fn load_file(&mut self, file: &Path, kind: i32) -> Result<()> {
        let engine = engine();
        let bytes = std::fs::read(file)?;
        let component = Component::new(engine, &bytes)?;
        let worlds = component
            .component_type()
            .exports(engine)
            .filter(|(name, _)| {
                [
                    "lumewarp:plugin/router-strategy@1.0.0",
                    "lumewarp:plugin/upstream-auth@1.0.0",
                    "lumewarp:plugin/a2a-middleware@1.0.0",
                ]
                .contains(name)
            })
            .count();
        if worlds != 1 {
            bail!("component must implement exactly one plugin world")
        }
        let (world, interface) = match kind {
            1 => ("router", "lumewarp:plugin/router-strategy@1.0.0"),
            2 => ("auth", "lumewarp:plugin/upstream-auth@1.0.0"),
            _ => ("middleware", "lumewarp:plugin/a2a-middleware@1.0.0"),
        };
        if !component
            .component_type()
            .exports(engine)
            .any(|(name, _)| name == interface)
        {
            bail!("expected {world} world: kind mismatch");
        }
        for (name, _) in component.component_type().imports(engine) {
            if !name.starts_with("wasi:")
                && name != "lumewarp:plugin/types@1.0.0"
                && !(kind == 2 && name == "lumewarp:plugin/host@1.0.0")
            {
                bail!("unsupported component import {name}");
            }
        }
        let linker = build_linker(engine, &component, kind)?;
        let instance_pre = linker.instantiate_pre(&component)?;
        let prepared = match kind {
            1 => Prepared::Router(router::RouterPre::new(instance_pre)?),
            2 => Prepared::Auth(auth::AuthPre::new(instance_pre)?),
            _ => Prepared::Middleware(middleware::MiddlewarePre::new(instance_pre)?),
        };
        let text = block_on(async {
            let mut store = store(engine);
            match &prepared {
                Prepared::Router(pre) => {
                    pre.instantiate_async(&mut store)
                        .await?
                        .lumewarp_plugin_router_strategy()
                        .call_manifest(&mut store)
                        .await
                }
                Prepared::Auth(pre) => {
                    pre.instantiate_async(&mut store)
                        .await?
                        .lumewarp_plugin_upstream_auth()
                        .call_manifest(&mut store)
                        .await
                }
                Prepared::Middleware(pre) => {
                    pre.instantiate_async(&mut store)
                        .await?
                        .lumewarp_plugin_a2a_middleware()
                        .call_manifest(&mut store)
                        .await
                }
            }
        })?;
        let manifest: Manifest = serde_json::from_str(&text).context("invalid manifest")?;
        let expected = match kind {
            1 => "router-strategy",
            2 => "upstream-auth",
            _ => "a2a-middleware",
        };
        if manifest.kind != expected {
            bail!("kind mismatch: expected {expected}, got {}", manifest.kind)
        }
        if manifest.abi != ABI {
            bail!("unsupported plugin ABI {}", manifest.abi)
        }
        let name = manifest.name.as_bytes();
        if !(1..=63).contains(&name.len())
            || !name[0].is_ascii_lowercase()
            || !name
                .iter()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || *b == b'_')
        {
            bail!("invalid plugin name")
        }
        if self.plugins.contains_key(&(kind, manifest.name.clone()))
            || builtins()
                .iter()
                .any(|d| d.kind == kind && d.name == manifest.name)
        {
            bail!("duplicate plugin name {}", manifest.name)
        }
        check_manifest_text("title", &manifest.title, MAX_TITLE_CHARS)?;
        check_manifest_text("description", &manifest.description, MAX_DESCRIPTION_CHARS)?;
        check_schema(&manifest.params_schema)?;
        let router = match (kind, manifest.router) {
            (1, Some(r)) => {
                check_schema(&r.member_schema)?;
                if r.needs.iter().any(|n| n != "latency" && n != "vllm_load") {
                    bail!("unknown router signal")
                }
                Some(RouterStrategyInfo {
                    uses_weight: r.uses_weight,
                    needs: r.needs,
                    member_schema_json: r.member_schema.to_string(),
                })
            }
            (1, None) => bail!("router manifest requires router metadata"),
            (_, Some(_)) => bail!("router metadata on non-router plugin"),
            (_, None) => None,
        };
        let mut descriptor = PluginDescriptor {
            kind,
            name: manifest.name,
            title: manifest.title,
            description: manifest.description,
            builtin: false,
            params_schema_json: manifest.params_schema.to_string(),
            router,
            wasm_sha256: hex::encode(Sha256::digest(&bytes)),
            digest: String::new(),
        };
        set_digest(&mut descriptor);
        self.plugins.insert(
            (kind, descriptor.name.clone()),
            Arc::new(Plugin {
                component,
                prepared,
                descriptor,
                timeouts: AtomicU64::new(0),
                traps: AtomicU64::new(0),
                failures: std::sync::atomic::AtomicU32::new(0),
                paused_until: AtomicU64::new(0),
            }),
        );
        Ok(())
    }
    pub fn get(&self, kind: i32, name: &str) -> Option<&Arc<Plugin>> {
        self.plugins.get(&(kind, name.into()))
    }
    pub fn descriptors(&self) -> Vec<PluginDescriptor> {
        let mut all = builtins();
        all.extend(self.plugins.values().map(|p| p.descriptor.clone()));
        all.sort_by(|a, b| (a.kind, &a.name).cmp(&(b.kind, &b.name)));
        all
    }
    pub fn capabilities(&self) -> Vec<String> {
        self.descriptors()
            .iter()
            .map(|d| {
                format!(
                    "{}:{}",
                    match d.kind {
                        1 => "strategy",
                        2 => "auth",
                        _ => "a2a_middleware",
                    },
                    d.name
                )
            })
            .collect()
    }
}

/// The control plane's limits on the two free-text manifest fields: they end up
/// in `Hello`, whose message size is bounded.
fn check_manifest_text(field: &str, text: &str, max: usize) -> Result<()> {
    if text.chars().count() > max {
        bail!("{field} exceeds {max} characters")
    }
    if text.chars().any(char::is_control) {
        bail!("{field} contains control characters")
    }
    Ok(())
}

fn check_schema(schema: &Value) -> Result<()> {
    if schema.to_string().len() > 16384 {
        bail!("schema exceeds 16 KiB")
    }
    if let Some(problem) = lumewarp_plugin_schema::subset_problem(schema) {
        bail!("invalid schema: {problem}")
    }
    Ok(())
}
fn set_digest(d: &mut PluginDescriptor) {
    use prost::Message;
    d.digest = hex::encode(Sha256::digest(d.encode_to_vec()));
}
pub fn builtins() -> Vec<PluginDescriptor> {
    let mut all: Vec<_> = lumewarp_router_api::descriptors()
        .into_iter()
        .map(|d| PluginDescriptor {
            kind: 1,
            name: d.name.into(),
            title: d.title.into(),
            description: d.description.into(),
            builtin: true,
            params_schema_json: d.params_schema.to_string(),
            router: Some(RouterStrategyInfo {
                uses_weight: d.uses_weight,
                needs: d.needs.into_iter().map(Into::into).collect(),
                member_schema_json: d.member_schema.to_string(),
            }),
            ..Default::default()
        })
        .collect();
    for name in ["none", "static_bearer", "passthrough"] {
        all.push(PluginDescriptor {
            kind: 2,
            name: name.into(),
            title: name.into(),
            builtin: true,
            params_schema_json: serde_json::json!({"type":"object","additionalProperties":false})
                .to_string(),
            ..Default::default()
        });
    }
    for d in &mut all {
        set_digest(d);
    }
    all
}

pub use pool::{Error as PoolError, PoolConfig, RouterInstance, RouterPool};

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn manifest_text_is_bounded_and_has_no_control_characters() {
        assert!(check_manifest_text("title", "Priority failover", MAX_TITLE_CHARS).is_ok());
        assert!(
            check_manifest_text("title", &"t".repeat(MAX_TITLE_CHARS), MAX_TITLE_CHARS).is_ok()
        );
        assert!(
            check_manifest_text("title", &"t".repeat(MAX_TITLE_CHARS + 1), MAX_TITLE_CHARS)
                .is_err()
        );
        assert!(check_manifest_text(
            "description",
            &"d".repeat(MAX_DESCRIPTION_CHARS),
            MAX_DESCRIPTION_CHARS
        )
        .is_ok());
        assert!(check_manifest_text(
            "description",
            &"d".repeat(MAX_DESCRIPTION_CHARS + 1),
            MAX_DESCRIPTION_CHARS
        )
        .is_err());
        for bad in ["a\nb", "a\tb", "nul\0", "esc\u{1b}"] {
            assert!(
                check_manifest_text("description", bad, MAX_DESCRIPTION_CHARS).is_err(),
                "{bad:?}"
            );
        }
        // Multi-byte text counts characters, not bytes.
        assert!(
            check_manifest_text("title", &"字".repeat(MAX_TITLE_CHARS), MAX_TITLE_CHARS).is_ok()
        );
    }

    #[test]
    fn one_engine_and_one_ticker_serve_every_plugin() {
        // A second call hands out the same engine, never a new ticker thread.
        let first = engine() as *const Engine;
        let second = engine() as *const Engine;
        assert!(std::ptr::eq(first, second));
        #[cfg(target_os = "linux")]
        {
            let tickers = std::fs::read_dir("/proc/self/task")
                .expect("task directory")
                .filter_map(|entry| entry.ok())
                .filter(|entry| {
                    std::fs::read_to_string(entry.path().join("comm"))
                        .is_ok_and(|name| name.trim() == "plugin-epoch")
                })
                .count();
            assert_eq!(tickers, 1, "one epoch ticker for the whole process");
        }
    }
}
