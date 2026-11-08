//! Startup-only component loading. No WASI imports are linked.
use anyhow::{bail, Context, Result};
use std::sync::atomic::{AtomicU64, Ordering};
use std::{
    collections::HashMap,
    path::Path,
    sync::{Arc, Mutex, OnceLock},
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

use lumewarp_proto::config::v1::{PluginDescriptor, RouterStrategyInfo};
use serde::Deserialize;
use serde_json::Value;
use sha2::{Digest, Sha256};
use wasmtime::{
    component::{Component, Linker, ResourceAny},
    Config, Engine, Store, StoreLimits, StoreLimitsBuilder,
};

pub mod router {
    wasmtime::component::bindgen!({ path: "../../wit", world: "router" });
}
pub mod auth {
    wasmtime::component::bindgen!({ path: "../../wit", world: "auth" });
}
pub mod middleware {
    wasmtime::component::bindgen!({ path: "../../wit", world: "middleware" });
}
pub use router::exports::lumewarp::plugin::router_strategy as types;
pub const ABI: u32 = 1;

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

/// Request adapters provide scoped origin/secret access for auth stores only.
pub trait AuthHost: Send + Sync {
    fn http_fetch(
        &self,
        request: auth::lumewarp::plugin::host::HttpRequest,
    ) -> Result<auth::lumewarp::plugin::host::HttpResponse, String>;
    fn secret(&self, name: &str) -> Option<String>;
    fn now_unix_ms(&self) -> u64;
}
pub struct State {
    limits: StoreLimits,
    pub auth_host: Option<Arc<dyn AuthHost>>,
}
impl Default for State {
    fn default() -> Self {
        Self {
            auth_host: None,
            limits: StoreLimitsBuilder::new()
                .memory_size(32 * 1024 * 1024)
                .memories(1)
                .build(),
        }
    }
}
impl auth::lumewarp::plugin::types::Host for State {}
impl auth::lumewarp::plugin::host::Host for State {
    fn http_fetch(
        &mut self,
        request: auth::lumewarp::plugin::host::HttpRequest,
    ) -> Result<auth::lumewarp::plugin::host::HttpResponse, String> {
        self.auth_host
            .as_ref()
            .ok_or_else(|| "host HTTP adapter is not installed".to_string())?
            .http_fetch(request)
    }
    fn secret(&mut self, name: String) -> Option<String> {
        self.auth_host.as_ref()?.secret(&name)
    }
    fn now_unix_ms(&mut self) -> u64 {
        self.auth_host.as_ref().map_or(0, |host| host.now_unix_ms())
    }
}
fn linker(engine: &Engine, component: &Component) -> Result<Linker<State>> {
    let mut linker = Linker::new(engine);
    // Rust's wasm32-wasip2 std retains WASI imports for panic paths. These
    // interfaces have no implementation: every imported function traps.
    // Unknown non-WASI imports must still fail instantiation.
    for (name, _) in component.component_type().imports(engine) {
        if !name.starts_with("wasi:") && name != "lumewarp:plugin/types@1.0.0" {
            bail!("unsupported component import {name}");
        }
    }
    linker.define_unknown_imports_as_traps(component)?;
    Ok(linker)
}
/// Reset a call's epoch budget. A 500 us ticker gives a 1 ms call at
/// least half its budget even when it begins immediately before a tick.
pub fn set_deadline(store: &mut Store<State>, duration: Duration) {
    store.set_epoch_deadline(
        u64::try_from(duration.as_micros().div_ceil(500))
            .unwrap_or(u64::MAX)
            .max(1),
    );
}
fn store(engine: &Engine, deadline: Duration) -> Store<State> {
    let mut store = Store::new(engine, State::default());
    store.limiter(|s| &mut s.limits);
    set_deadline(&mut store, deadline);
    store
}

pub struct Plugin {
    engine: Engine,
    component: Component,
    pub descriptor: PluginDescriptor,
    pub timeouts: AtomicU64,
    pub traps: AtomicU64,
}
impl Plugin {
    pub fn engine(&self) -> &Engine {
        &self.engine
    }
    pub fn component(&self) -> &Component {
        &self.component
    }
    /// The adapter resets this deadline before each request-path call.
    pub fn store(&self, deadline: Duration) -> Store<State> {
        store(&self.engine, deadline)
    }
    pub fn linker(&self) -> Result<Linker<State>> {
        if self.descriptor.kind != 2 {
            return linker(&self.engine, &self.component);
        }
        let mut linker = Linker::new(&self.engine);
        linker.define_unknown_imports_as_traps(&self.component)?;
        linker.allow_shadowing(true);
        auth::Auth::add_to_linker::<State, wasmtime::component::HasSelf<State>>(
            &mut linker,
            |state| state,
        )?;
        Ok(linker)
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
    let registry = Registry::from_env()?;
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
        let mut config = Config::new();
        config.wasm_component_model(true).epoch_interruption(true);
        let engine = Engine::new(&config)?;
        // A dedicated ticker per engine makes deadlines independent of tokio scheduling.
        let weak = engine.weak();
        std::thread::spawn(move || loop {
            std::thread::sleep(Duration::from_micros(500));
            let Some(engine) = weak.upgrade() else { break };
            engine.increment_epoch();
        });
        let bytes = std::fs::read(file)?;
        let component = Component::new(&engine, &bytes)?;
        let worlds = component
            .component_type()
            .exports(&engine)
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
            .exports(&engine)
            .any(|(name, _)| name == interface)
        {
            bail!("expected {world} world: kind mismatch");
        }
        for (name, _) in component.component_type().imports(&engine) {
            if !name.starts_with("wasi:")
                && name != "lumewarp:plugin/types@1.0.0"
                && !(kind == 2 && name == "lumewarp:plugin/host@1.0.0")
            {
                bail!("unsupported component import {name}");
            }
        }
        let mut store = store(&engine, Duration::from_secs(2));
        let text = match kind {
            1 => {
                let binding = router::Router::instantiate(
                    &mut store,
                    &component,
                    &linker(&engine, &component)?,
                )
                .context("expected router world")?;
                binding
                    .lumewarp_plugin_router_strategy()
                    .call_manifest(&mut store)?
            }
            2 => {
                let mut linker = Linker::new(&engine);
                linker.define_unknown_imports_as_traps(&component)?;
                linker.allow_shadowing(true);
                auth::Auth::add_to_linker::<State, wasmtime::component::HasSelf<State>>(
                    &mut linker,
                    |s| s,
                )?;
                let binding = auth::Auth::instantiate(&mut store, &component, &linker)
                    .context("expected auth world")?;
                binding
                    .lumewarp_plugin_upstream_auth()
                    .call_manifest(&mut store)?
            }
            3 => {
                let binding = middleware::Middleware::instantiate(
                    &mut store,
                    &component,
                    &linker(&engine, &component)?,
                )
                .context("expected middleware world")?;
                binding
                    .lumewarp_plugin_a2a_middleware()
                    .call_manifest(&mut store)?
            }
            _ => bail!("unknown plugin kind"),
        };
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
                engine,
                component,
                descriptor,
                timeouts: AtomicU64::new(0),
                traps: AtomicU64::new(0),
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

impl lumewarp_router_api::Strategy for RouterInstance {
    fn pick(
        &self,
        req: &lumewarp_router_api::RequestInfo<'_>,
        candidates: &lumewarp_router_api::Candidates<'_>,
        rng: &mut dyn lumewarp_router_api::RngCore,
    ) -> Option<usize> {
        let signals = &self.plugin.descriptor.router.as_ref()?.needs;
        let latency = signals.iter().any(|n| n == "latency");
        let load = signals.iter().any(|n| n == "vllm_load");
        let request = types::RequestInfo {
            requested: req.requested.into(),
            team_id: req.team_id.into(),
            key_id: req.key_id.into(),
            estimated_input_tokens: req.estimated_input_tokens,
            headers: req.headers().map(|(n, v)| (n.into(), v.to_vec())).collect(),
        };
        let candidates: Vec<_> = candidates
            .iter()
            .map(|c| types::Candidate {
                index: c.index as u32,
                member_index: c.member_index() as u32,
                weight: c.weight(),
                in_flight: c.in_flight().min(u32::MAX as usize) as u32,
                latency_ewma_us: if latency { c.latency_ewma_us() } else { None },
                load: load.then(|| {
                    let l = c.load();
                    types::Load {
                        waiting: l.waiting,
                        kv_usage_bp: l.kv_usage_bp,
                        fresh: l.fresh,
                    }
                }),
            })
            .collect();
        self.pick(&request, &candidates, rng.u64(0..u64::MAX))
            .map(|i| i as usize)
    }
}

struct Instance {
    store: Store<State>,
    binding: router::Router,
    resource: ResourceAny,
}
/// Idle instances are protected only while checking out/returning, never while
/// executing guest code. Concurrent misses create independent stores.
pub struct RouterInstance {
    plugin: Arc<Plugin>,
    info: types::RouterInfo,
    idle: Mutex<Vec<Instance>>,
    max_idle: usize,
}
impl RouterInstance {
    pub fn configure(plugin: Arc<Plugin>, info: types::RouterInfo) -> Result<Self> {
        let configured = Self {
            plugin,
            info,
            idle: Mutex::new(Vec::new()),
            max_idle: std::thread::available_parallelism().map_or(1, usize::from),
        };
        let instance = configured.instantiate()?;
        configured.idle.lock().unwrap().push(instance);
        Ok(configured)
    }
    fn instantiate(&self) -> Result<Instance> {
        let mut store = store(&self.plugin.engine, Duration::from_secs(2));
        let binding = router::Router::instantiate(
            &mut store,
            &self.plugin.component,
            &linker(&self.plugin.engine, &self.plugin.component)?,
        )?;
        let resource = binding
            .lumewarp_plugin_router_strategy()
            .strategy()
            .call_configure(&mut store, &self.info)?
            .map_err(|_| anyhow::anyhow!("plugin configure failed"))?;
        Ok(Instance {
            store,
            binding,
            resource,
        })
    }
    pub fn pick(
        &self,
        request: &types::RequestInfo,
        candidates: &[types::Candidate],
        seed: u64,
    ) -> Option<u32> {
        let idle = { self.idle.lock().unwrap().pop() };
        let mut instance = idle.map(Ok).unwrap_or_else(|| self.instantiate()).ok()?;
        set_deadline(&mut instance.store, Duration::from_millis(1));
        let started = std::time::Instant::now();
        let span = tracing::info_span!(
            "plugin",
            kind = "router-strategy",
            plugin = self.plugin.descriptor.name
        );
        let _entered = span.enter();
        let result = instance
            .binding
            .lumewarp_plugin_router_strategy()
            .strategy()
            .call_pick(
                &mut instance.store,
                instance.resource,
                request,
                candidates,
                seed,
            );
        let outcome = match &result {
            Ok(_) => "ok",
            Err(error)
                if error.downcast_ref::<wasmtime::Trap>() == Some(&wasmtime::Trap::Interrupt) =>
            {
                self.plugin.timeouts.fetch_add(1, Ordering::Relaxed);
                "timeout"
            }
            Err(_) => {
                self.plugin.traps.fetch_add(1, Ordering::Relaxed);
                "trap"
            }
        };
        if let Some(metrics) = METRICS.get() {
            metrics.record(
                "router-strategy",
                &self.plugin.descriptor.name,
                outcome,
                started.elapsed().as_secs_f64(),
            );
        }
        // A trapped instance is poisoned and is never returned to the pool.
        if result.is_ok() {
            let mut idle = self.idle.lock().unwrap();
            if idle.len() < self.max_idle {
                idle.push(instance);
            }
        }
        result.ok().flatten()
    }
}
