//! The `a2a-middleware` request adapter: one configured middleware per agent,
//! called with the JSON-RPC request, response or SSE event about to pass
//! through the gateway.
//!
//! A snapshot names at most one middleware plugin per A2A agent (plan D14).
//! Applying the snapshot configures it ([`precompile`]); the guest `configure`
//! runs when an instance is first needed. Hooks are **asynchronous**: a guest
//! yields to the tokio executor at every epoch tick
//! (`epoch_deadline_async_yield_and_update`), so a slow plugin never occupies a
//! worker thread, and a request that goes away drops the future, which stops the
//! guest and discards its Store.
//!
//! There is no fixed time limit in this module. Every hook call is bounded by
//! the caller's timeout (`PluginRef.timeout_ms`, 0 = [`DEFAULT_TIMEOUT`]): on
//! expiry the future is dropped, that Store is discarded, the call counts as
//! `timeout` and the plugin's breaker sees a failure. Waiting for a pool
//! instance counts towards the same timeout.
//!
//! A trap or a guest error is the caller's to map (502 / the final error event);
//! a guest `rpc-error` is a deliberate, client-facing JSON-RPC error. The guest
//! has no host imports and no I/O.

use std::{
    collections::HashMap,
    sync::{atomic::Ordering, Arc, Mutex, OnceLock, RwLock},
    time::{Duration, Instant},
};

use tokio::sync::{OwnedSemaphorePermit, Semaphore};
use wasmtime::component::ResourceAny;

use crate::{
    middleware::{exports::lumewarp::plugin::a2a_middleware as wire, lumewarp::plugin::types},
    Plugin, Prepared, State,
};

/// `PluginDescriptor.kind` of the `a2a-middleware` world.
pub const KIND: i32 = 3;
/// `lumewarp_plugin_*` label for this kind.
pub const KIND_LABEL: &str = "a2a-middleware";
/// One hook call when the target's `PluginRef.timeout_ms` is 0.
pub const DEFAULT_TIMEOUT: Duration = Duration::from_secs(60);
/// A request body, response body or SSE event the hooks may see (plan L10).
pub const MAX_BODY: usize = 4 * 1024 * 1024;
/// Configured middlewares kept without a snapshot replacing them.
pub const MAX_CONFIGURED: usize = 4096;

/// The empty `types` interface has no host functions, but the world still needs
/// the empty host impl.
impl types::Host for State {}

/// Which hook is being called. Only used for labels and dispatch.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Hook {
    Request,
    Response,
    Stream,
}

impl Hook {
    pub fn label(self) -> &'static str {
        match self {
            Self::Request => "on-request",
            Self::Response => "on-response",
            Self::Stream => "on-stream-event",
        }
    }
}

/// One agent's plugin and settings, as the snapshot carries them.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Spec {
    /// The agent's stable id (`A2aAgent.id`), or its name when the id is empty
    /// (standalone YAML).
    pub id: String,
    pub name: String,
    /// The agent's JSON-RPC endpoint; part of the identity, like auth's.
    pub url: String,
    pub plugin: String,
    pub params_json: String,
    pub digest: [u8; 32],
}

impl Spec {
    /// The cache identity of an agent.
    pub fn key(&self) -> String {
        self.id.clone()
    }
}

/// Why a middleware could not be configured, or one hook could not be called.
#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error("middleware plugin {0:?} is not loaded")]
    Missing(String),
    #[error("invalid settings: {0}")]
    Params(String),
    #[error("configure failed: {0}")]
    Configure(String),
    /// The guest answered with its own JSON-RPC error. This is a deliberate,
    /// client-facing refusal, not a host failure.
    #[error("middleware refused: {message}")]
    Rpc { code: i32, message: String },
    /// The caller's timeout expired; the Store was discarded.
    #[error("hook exceeded its timeout")]
    Timeout,
    #[error("hook trapped")]
    Trap,
    #[error("the hook was cancelled")]
    Canceled,
    /// The plugin's breaker is open after repeated failures.
    #[error("the plugin is paused after repeated failures")]
    Paused,
    #[error("hook input or output exceeds {MAX_BODY} bytes")]
    TooLarge,
}

/// A configuration refused because one agent's middleware would not load.
#[derive(Debug, thiserror::Error)]
#[error("agent {agent:?}: {source}")]
pub struct Refused {
    /// The agent name, for the gateway's log and NACK reason.
    pub agent: String,
    #[source]
    pub source: Error,
}

struct Instance {
    store: wasmtime::Store<State>,
    binding: crate::middleware::Middleware,
    resource: ResourceAny,
    /// Bounds the number of live Stores of this middleware; held until the
    /// Store is dropped or returned to the idle pool.
    _permit: OwnedSemaphorePermit,
}

/// One agent's configured middleware. Cheap to share: a hook checks out a
/// Store, and hooks never run in one Store at the same time.
pub struct Middleware {
    plugin: Arc<Plugin>,
    spec: Spec,
    agent: wire::AgentInfo,
    idle: Mutex<Vec<Instance>>,
    /// Live Stores (idle + in use) of this middleware.
    permits: Arc<Semaphore>,
    max_idle: usize,
}

impl std::fmt::Debug for Middleware {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Middleware")
            .field("agent", &self.spec.name)
            .field("plugin", &self.spec.plugin)
            .finish_non_exhaustive()
    }
}

impl Middleware {
    fn new(spec: Spec) -> Result<Self, Error> {
        let plugin = crate::global()
            .get(KIND, &spec.plugin)
            .filter(|plugin| plugin.descriptor.kind == KIND)
            .cloned()
            .ok_or_else(|| Error::Missing(spec.plugin.clone()))?;
        let params = match serde_json::from_str::<serde_json::Value>(&spec.params_json) {
            Ok(serde_json::Value::Object(params)) => params,
            _ => return Err(Error::Params("params_json must be a JSON object".into())),
        };
        let agent = wire::AgentInfo {
            name: spec.name.clone(),
            url: spec.url.clone(),
            params_json: serde_json::Value::Object(params).to_string(),
        };
        let max_idle = std::thread::available_parallelism()
            .map_or(1, usize::from)
            .min(4);
        Ok(Self {
            plugin,
            spec,
            agent,
            idle: Mutex::new(Vec::new()),
            // A pool miss waits for a permit rather than failing (F1 revision).
            permits: Arc::new(Semaphore::new(max_idle * 2)),
            max_idle,
        })
    }

    /// The agent this middleware was configured for.
    pub fn agent_name(&self) -> &str {
        &self.spec.name
    }

    /// The plugin name, as the manifest declared it.
    pub fn plugin_name(&self) -> &str {
        &self.spec.plugin
    }

    /// Whether this middleware still describes `spec`; unchanged agents are
    /// reused across snapshots.
    pub fn matches(&self, spec: &Spec) -> bool {
        self.spec == *spec
    }

    /// The request hook: the JSON-RPC request body before it goes upstream.
    pub async fn on_request(
        self: &Arc<Self>,
        method: &str,
        body: &[u8],
        timeout: Duration,
    ) -> Result<Vec<u8>, Error> {
        self.run(Hook::Request, method, body, timeout)
            .await?
            .ok_or(Error::Trap)
    }

    /// The response hook: a non-streaming JSON-RPC response body.
    pub async fn on_response(
        self: &Arc<Self>,
        method: &str,
        body: &[u8],
        timeout: Duration,
    ) -> Result<Vec<u8>, Error> {
        self.run(Hook::Response, method, body, timeout)
            .await?
            .ok_or(Error::Trap)
    }

    /// The stream hook: one SSE event's JSON-RPC response data. `None` drops the
    /// event; the gateway only allows that for a `TaskArtifactUpdateEvent`.
    pub async fn on_stream_event(
        self: &Arc<Self>,
        method: &str,
        data: &[u8],
        timeout: Duration,
    ) -> Result<Option<Vec<u8>>, Error> {
        self.run(Hook::Stream, method, data, timeout).await
    }

    async fn run(
        self: &Arc<Self>,
        hook: Hook,
        method: &str,
        data: &[u8],
        timeout: Duration,
    ) -> Result<Option<Vec<u8>>, Error> {
        if data.len() > MAX_BODY {
            return Err(Error::TooLarge);
        }
        let this = Arc::clone(self);
        let method = method.to_owned();
        let data = data.to_vec();
        let span = tracing::info_span!(
            parent: tracing::Span::current(),
            "plugin",
            kind = KIND_LABEL,
            plugin = %self.spec.plugin,
            agent = %self.spec.name,
            hook = hook.label(),
        );
        if self.plugin.paused() {
            crate::record(KIND_LABEL, &self.spec.plugin, "error", Duration::ZERO);
            return Err(Error::Paused);
        }
        let future = async move {
            let started = Instant::now();
            let mut instance = match this.checkout().await {
                Ok(instance) => instance,
                Err(error) => {
                    // A pool/configure failure is a plugin error: count it and
                    // keep the breaker honest.
                    this.plugin.record_failure();
                    crate::record(KIND_LABEL, &this.spec.plugin, "error", started.elapsed());
                    return Err(error);
                }
            };
            let interface = instance.binding.lumewarp_plugin_a2a_middleware();
            let result = match hook {
                Hook::Request => interface
                    .middleware()
                    .call_on_request(&mut instance.store, instance.resource, &method, &data)
                    .await
                    .map(Some),
                Hook::Response => interface
                    .middleware()
                    .call_on_response(&mut instance.store, instance.resource, &method, &data)
                    .await
                    .map(Some),
                Hook::Stream => interface
                    .middleware()
                    .call_on_stream_event(&mut instance.store, instance.resource, &method, &data)
                    .await
                    .map(|inner| match inner {
                        Ok(value) => value.map(Ok),
                        Err(error) => Some(Err(error)),
                    }),
            };
            let elapsed = started.elapsed();
            let (result, reusable, label) = match result {
                Ok(Some(Ok(value))) if value.len() > MAX_BODY => {
                    (Err(Error::TooLarge), true, "error")
                }
                Ok(Some(Ok(value))) => (Ok(Some(value)), true, "ok"),
                Ok(None) if hook == Hook::Stream => (Ok(None), true, "ok"),
                Ok(None) => (Err(Error::Trap), true, "trap"),
                // A guest-declared RPC error leaves the Store usable.
                Ok(Some(Err(error))) => (
                    Err(Error::Rpc {
                        code: error.code,
                        message: error.message,
                    }),
                    true,
                    "error",
                ),
                Err(_) => {
                    this.plugin.traps.fetch_add(1, Ordering::Relaxed);
                    (Err(Error::Trap), false, "trap")
                }
            };
            match &result {
                Ok(_) => this.plugin.record_success(),
                // The breaker counts a guest error, a trap and a timeout.
                Err(_) => this.plugin.record_failure(),
            }
            crate::record(KIND_LABEL, &this.spec.plugin, label, elapsed);
            // A trapped Store is poisoned and is never returned to the pool.
            if reusable {
                let mut idle = this.idle.lock().unwrap_or_else(|error| error.into_inner());
                if idle.len() < this.max_idle {
                    idle.push(instance);
                }
            }
            result
        };
        match tokio::time::timeout(timeout, tracing::Instrument::instrument(future, span)).await {
            Ok(result) => result,
            // Dropping the future drops its Store; the guest stops at its next
            // epoch yield.
            Err(_) => {
                self.plugin.timeouts.fetch_add(1, Ordering::Relaxed);
                self.plugin.record_failure();
                crate::record(KIND_LABEL, &self.spec.plugin, "timeout", timeout);
                Err(Error::Timeout)
            }
        }
    }

    /// A pooled instance, or a new one. A pool miss waits for one of this
    /// middleware's live-Store permits; it never fails on emptiness.
    async fn checkout(self: &Arc<Self>) -> Result<Instance, Error> {
        if let Some(instance) = self
            .idle
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .pop()
        {
            return Ok(instance);
        }
        let permit = Arc::clone(&self.permits)
            .acquire_owned()
            .await
            .map_err(|_| Error::Canceled)?;
        self.instantiate(permit).await
    }

    async fn instantiate(&self, permit: OwnedSemaphorePermit) -> Result<Instance, Error> {
        let Prepared::Middleware(pre) = self.plugin.prepared() else {
            return Err(Error::Missing(self.spec.plugin.clone()));
        };
        // The shared lane: fairness epoch, the process memory budget and the
        // circuit breaker state live on `Plugin`.
        let mut store = self.plugin.store();
        let binding = pre
            .instantiate_async(&mut store)
            .await
            .map_err(|error| Error::Configure(format!("{error:#}")))?;
        let resource = binding
            .lumewarp_plugin_a2a_middleware()
            .middleware()
            .call_configure(&mut store, &self.agent)
            .await
            .map_err(|error| Error::Configure(format!("{error:#}")))?
            .map_err(Error::Configure)?;
        Ok(Instance {
            store,
            binding,
            resource,
            _permit: permit,
        })
    }

    /// Apply-time pre-warm: one configured Store in the idle pool. Called only
    /// through [`precompile`], never from a request.
    async fn warm(self: &Arc<Self>) -> Result<(), Error> {
        let permit = Arc::clone(&self.permits)
            .acquire_owned()
            .await
            .map_err(|_| Error::Canceled)?;
        let instance = self.instantiate(permit).await?;
        self.idle
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .push(instance);
        Ok(())
    }
}

/// Registered middlewares of the current configuration, keyed by agent id.
#[derive(Default)]
pub struct Cache {
    entries: RwLock<HashMap<String, Arc<Middleware>>>,
}

impl Cache {
    pub fn new() -> Self {
        Self::default()
    }

    /// The middleware registered for `agent_id`, if any.
    pub fn get(&self, agent_id: &str) -> Option<Arc<Middleware>> {
        self.entries
            .read()
            .unwrap_or_else(|error| error.into_inner())
            .get(agent_id)
            .cloned()
    }

    /// The middleware of `spec`, registering it if this agent is new or its
    /// settings changed. The guest `configure` is deferred to the first hook.
    pub fn get_or_configure(&self, spec: &Spec) -> Result<Arc<Middleware>, Error> {
        let key = spec.key();
        if let Some(existing) = self
            .entries
            .read()
            .unwrap_or_else(|error| error.into_inner())
            .get(&key)
        {
            if existing.matches(spec) {
                return Ok(Arc::clone(existing));
            }
        }
        let configured = Arc::new(Middleware::new(spec.clone())?);
        let mut entries = self
            .entries
            .write()
            .unwrap_or_else(|error| error.into_inner());
        if let Some(existing) = entries.get(&key) {
            if existing.matches(spec) {
                return Ok(Arc::clone(existing));
            }
        }
        if entries.len() >= MAX_CONFIGURED && !entries.contains_key(&key) {
            if let Some(stale) = entries.keys().find(|entry| *entry != &key).cloned() {
                entries.remove(&stale);
            }
        }
        entries.insert(key, Arc::clone(&configured));
        Ok(configured)
    }

    /// Registers the middlewares of one applied configuration. Agents whose
    /// name, settings and destination did not change keep their configured
    /// value; everything else is dropped once no hook is using it.
    ///
    /// All of `specs` is registered before anything is replaced, so a refused
    /// or unknown plugin leaves the previous set serving.
    pub fn replace(&self, specs: &[Spec]) -> Result<(), Refused> {
        let current: HashMap<_, _> = self
            .entries
            .read()
            .unwrap_or_else(|error| error.into_inner())
            .clone();
        let mut next: HashMap<String, Arc<Middleware>> = HashMap::with_capacity(specs.len());
        for spec in specs {
            let key = spec.key();
            let (configured, fresh) = match current.get(&key) {
                Some(existing) if existing.matches(spec) => (Arc::clone(existing), false),
                _ => (
                    Arc::new(Middleware::new(spec.clone()).map_err(|source| Refused {
                        agent: spec.name.clone(),
                        source,
                    })?),
                    true,
                ),
            };
            if fresh {
                // Pre-warm on the apply path: a refused `configure` is a NACK,
                // not a 502 on the first request. `block_on` runs on its own
                // thread and runtime (`lib.rs`), so this works from a tokio
                // worker or a plain thread.
                let middleware = Arc::clone(&configured);
                crate::block_on(async move { middleware.warm().await }).map_err(|source| {
                    Refused {
                        agent: spec.name.clone(),
                        source,
                    }
                })?;
            }
            if next.insert(key, configured).is_some() {
                return Err(Refused {
                    agent: spec.name.clone(),
                    source: Error::Params(format!("two agents share the id {:?}", spec.id)),
                });
            }
        }
        *self
            .entries
            .write()
            .unwrap_or_else(|error| error.into_inner()) = next;
        Ok(())
    }
}

static GLOBAL: OnceLock<Cache> = OnceLock::new();

/// The process-wide middleware cache. One gateway process serves one set of
/// agents at a time, so there is one cache.
pub fn global() -> &'static Cache {
    GLOBAL.get_or_init(Cache::new)
}

/// The middleware registered for `agent_id` in the process-wide cache.
pub fn get(agent_id: &str) -> Option<Arc<Middleware>> {
    global().get(agent_id)
}

/// The apply-path hook: register every agent's middleware and keep the ones
/// that did not change. Call it from `RoutingTable::compile` (next to
/// `upstream_auth::precompile`). A missing plugin or unparseable settings
/// becomes an `INVALID_CONFIG` NACK; a guest `configure` error is deferred to
/// the first hook, which answers 502 (async support makes a synchronous
/// configure on the apply path impossible).
///
/// `specs` is empty for a snapshot with no agents, which clears the cache.
pub fn precompile(specs: &[Spec]) -> Result<(), Refused> {
    global().replace(specs)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    #[test]
    fn hook_labels_are_stable() {
        assert_eq!(Hook::Request.label(), "on-request");
        assert_eq!(Hook::Response.label(), "on-response");
        assert_eq!(Hook::Stream.label(), "on-stream-event");
    }

    #[test]
    fn a_spec_key_is_the_agent_id() {
        let spec = Spec {
            id: "agent-id".into(),
            name: "agent".into(),
            url: "http://127.0.0.1:1/a2a".into(),
            plugin: "strip_artifacts".into(),
            params_json: "{}".into(),
            digest: [0; 32],
        };
        assert_eq!(spec.key(), "agent-id");
        assert_eq!(DEFAULT_TIMEOUT, Duration::from_secs(60));
    }

    /// Component compilation is the expensive part; load the two fixtures once
    /// for the whole test binary.
    fn install_fixtures() -> Option<()> {
        static INSTALLED: std::sync::OnceLock<bool> = std::sync::OnceLock::new();
        INSTALLED
            .get_or_init(|| {
                let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../target");
                let fixtures = root.join("plugins-test/a2a-middleware");
                let examples = root.join("plugins/a2a-middleware");
                if !fixtures.join("misbehave.wasm").exists()
                    || !examples.join("strip-artifacts.wasm").exists()
                {
                    eprintln!("SKIP: build the middleware components with bash gateway/plugins/build.sh --tests");
                    return false;
                }
                let mut registry = crate::Registry::default();
                if registry.load_dir(&fixtures, KIND).is_err()
                    || registry.load_dir(&examples, KIND).is_err()
                {
                    return false;
                }
                // Another test in this binary may have installed the registry
                // first; both load the same fixtures.
                let _ = crate::install(registry);
                crate::global().get(KIND, "misbehave_test").is_some()
            })
            .then_some(())
    }

    fn configure(id: &str, plugin: &str, params_json: &str) -> Arc<Middleware> {
        let spec = Spec {
            id: id.into(),
            name: id.into(),
            url: format!("http://127.0.0.1:1/{id}"),
            plugin: plugin.into(),
            params_json: params_json.into(),
            digest: [0; 32],
        };
        global().get_or_configure(&spec).unwrap()
    }

    #[tokio::test(flavor = "current_thread")]
    async fn a_slow_hook_yields_to_other_requests() {
        if install_fixtures().is_none() {
            return;
        }
        let slow = configure(
            "slow-agent",
            "misbehave_test",
            r#"{"mode":"slow","spin":300000}"#,
        );
        let fast = configure("fast-agent", "misbehave_test", r#"{"mode":"pass"}"#);
        let task = {
            let slow = Arc::clone(&slow);
            tokio::spawn(async move {
                slow.on_request("SendMessage", b"{}", Duration::from_secs(60))
                    .await
            })
        };
        // Let the slow guest start and reach its first epoch yield.
        tokio::task::yield_now().await;
        let started = Instant::now();
        let fast_result = tokio::time::timeout(
            Duration::from_secs(2),
            fast.on_request("SendMessage", b"{}", Duration::from_secs(5)),
        )
        .await;
        assert!(
            fast_result.is_ok(),
            "the fast hook was starved by the slow one"
        );
        assert!(fast_result.unwrap().is_ok());
        assert!(
            started.elapsed() < Duration::from_secs(1),
            "the fast hook took {:?}",
            started.elapsed()
        );
        assert!(task.await.unwrap().is_ok());
    }

    #[tokio::test]
    async fn a_hook_is_cancelled_at_its_configured_timeout() {
        if install_fixtures().is_none() {
            return;
        }
        let slow = configure(
            "timeout-agent",
            "misbehave_test",
            r#"{"mode":"slow","spin":4000000000}"#,
        );
        let started = Instant::now();
        let result = slow
            .on_request("SendMessage", b"{}", Duration::from_millis(50))
            .await;
        assert!(matches!(result, Err(Error::Timeout)), "{result:?}");
        assert!(started.elapsed() >= Duration::from_millis(50));
        assert!(started.elapsed() < Duration::from_secs(2));
    }
}
