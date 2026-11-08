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
/// A JSON-RPC request body a request hook may see (plan L10).
pub const MAX_REQUEST_BODY: usize = 4 * 1024 * 1024;
/// A non-streaming JSON-RPC response body a response hook may see (plan L10).
pub const MAX_RESPONSE_BODY: usize = 16 * 1024 * 1024;
/// One SSE event's data a stream hook may see (plan L10).
pub const MAX_STREAM_EVENT: usize = 1024 * 1024;
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

    /// The L10 body bound for this hook: 4 MiB request, 16 MiB non-stream
    /// response, 1 MiB per SSE event. The proxy checks the same bounds on the
    /// hook's output; the adapter must accept what the proxy can hand it.
    pub fn limit(self) -> usize {
        match self {
            Self::Request => MAX_REQUEST_BODY,
            Self::Response => MAX_RESPONSE_BODY,
            Self::Stream => MAX_STREAM_EVENT,
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
    /// Apply-time `configure` did not finish inside [`crate::CONFIGURE_TIMEOUT`].
    #[error("configuring the middleware timed out")]
    ConfigureTimeout,
    #[error("hook input or output exceeds its size bound")]
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
        Self::with_plugin(plugin, spec)
    }

    /// Builds a middleware around an explicit plugin, so a test can own its
    /// breaker instead of sharing the process-wide one.
    pub(crate) fn with_plugin(plugin: Arc<Plugin>, spec: Spec) -> Result<Self, Error> {
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
        let limit = hook.limit();
        if data.len() > limit {
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
                Ok(Some(Ok(value))) if value.len() > limit => (Err(Error::TooLarge), true, "error"),
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
                // A guest `rpc-error` is a client-facing refusal and a size
                // refusal is the host rejecting one body: neither says the
                // plugin is broken, so neither trips the breaker (M3). The
                // breaker counts a trap, a timeout and a failed configure.
                Err(Error::Rpc { .. }) | Err(Error::TooLarge) => {}
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
        self.instantiate(permit, None).await
    }

    async fn instantiate(
        &self,
        permit: OwnedSemaphorePermit,
        budget: Option<Duration>,
    ) -> Result<Instance, Error> {
        let Prepared::Middleware(pre) = self.plugin.prepared() else {
            return Err(Error::Missing(self.spec.plugin.clone()));
        };
        // The shared lane: fairness epoch, the process memory budget and the
        // circuit breaker state live on `Plugin`. Apply-time `configure` gets a
        // hard epoch budget instead of the fairness yield (M4).
        let mut store = self.plugin.store();
        if let Some(budget) = budget {
            crate::set_apply_deadline(&mut store, budget);
        }
        let binding = pre
            .instantiate_async(&mut store)
            .await
            .map_err(|error| Error::Configure(format!("{error:#}")))?;
        let resource = binding
            .lumewarp_plugin_a2a_middleware()
            .middleware()
            .call_configure(&mut store, &self.agent)
            .await
            .map_err(|error| {
                if budget.is_some() && crate::is_epoch_trap(&error) {
                    Error::ConfigureTimeout
                } else {
                    Error::Configure(format!("{error:#}"))
                }
            })?
            .map_err(Error::Configure)?;
        if budget.is_some() {
            // Back to yielding: a hook is cancelled by its own timeout, never
            // trapped by the configure budget.
            crate::set_fair_epoch(&mut store);
        }
        Ok(Instance {
            store,
            binding,
            resource,
            _permit: permit,
        })
    }

    /// Apply-time pre-warm: one configured Store in the idle pool. Called only
    /// through [`Cache::stage`], never from a request, and bounded there.
    async fn warm(self: &Arc<Self>, timeout: Duration) -> Result<(), Error> {
        let permit = Arc::clone(&self.permits)
            .acquire_owned()
            .await
            .map_err(|_| Error::Canceled)?;
        let instance = self.instantiate(permit, Some(timeout)).await?;
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

    /// Configures `specs` into a set that does not serve yet (m1): a version
    /// the gateway later refuses leaves the previous set untouched.
    ///
    /// Agents whose name, settings and destination did not change keep their
    /// configured value; a fresh one is warmed inside
    /// [`crate::CONFIGURE_TIMEOUT`], so a `configure` error or a `configure`
    /// that never returns refuses the version (M4). No lock is held while it
    /// runs.
    pub fn stage(&self, specs: &[Spec]) -> Result<Staged, Refused> {
        self.stage_within(specs, crate::CONFIGURE_TIMEOUT)
    }

    /// [`Cache::stage`] with an explicit configure budget (tests use a short
    /// one; the gateway always takes the default).
    pub fn stage_within(&self, specs: &[Spec], timeout: Duration) -> Result<Staged, Refused> {
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
                // not a 502 on the first request, and the wait is bounded so a
                // `configure` that never returns cannot wedge the config
                // stream. `block_on` runs on its own thread and runtime
                // (`lib.rs`), so this works from a tokio worker or a thread.
                let middleware = Arc::clone(&configured);
                let warmed =
                    crate::block_on_timeout(async move { middleware.warm(timeout).await }, timeout);
                match warmed {
                    Some(Ok(())) => {}
                    Some(Err(source)) => {
                        return Err(Refused {
                            agent: spec.name.clone(),
                            source,
                        })
                    }
                    None => {
                        return Err(Refused {
                            agent: spec.name.clone(),
                            source: Error::ConfigureTimeout,
                        })
                    }
                }
            }
            if next.insert(key, configured).is_some() {
                return Err(Refused {
                    agent: spec.name.clone(),
                    source: Error::Params(format!("two agents share the id {:?}", spec.id)),
                });
            }
        }
        Ok(Staged { entries: next })
    }

    /// Makes a staged set the one serving; called once the version is accepted.
    pub fn publish(&self, staged: Staged) {
        *self
            .entries
            .write()
            .unwrap_or_else(|error| error.into_inner()) = staged.entries;
    }

    /// Stage and publish in one step (tests and standalone load).
    pub fn replace(&self, specs: &[Spec]) -> Result<(), Refused> {
        let staged = self.stage(specs)?;
        self.publish(staged);
        Ok(())
    }
}

/// Middlewares configured for one version that has not been accepted yet.
#[derive(Debug, Default)]
pub struct Staged {
    entries: HashMap<String, Arc<Middleware>>,
}

impl Staged {
    /// How many agents it configures (tests and logging).
    pub fn len(&self) -> usize {
        self.entries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
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

/// Stage and publish in one step, for a caller that is already sure the version
/// is accepted (the gateway uses [`Cache::stage`] at compile time and
/// [`Cache::publish`] on activation instead, so a refused version never changes
/// what is serving).
///
/// A missing plugin, unparseable settings or a `configure` that fails or times
/// out becomes a `Refused` (an `INVALID_CONFIG` NACK for the gateway).
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
    fn hook_limits_follow_l10() {
        assert_eq!(Hook::Request.limit(), 4 * 1024 * 1024);
        assert_eq!(Hook::Response.limit(), 16 * 1024 * 1024);
        assert_eq!(Hook::Stream.limit(), 1024 * 1024);
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

    /// A deliberate guest `rpc-error` and a size refusal are answers, not
    /// plugin failures: they must not pause the plugin for every tenant (M3).
    /// A timeout still does (the breaker keeps working).
    #[tokio::test]
    async fn refusals_do_not_trip_the_breaker() {
        if install_fixtures().is_none() {
            return;
        }
        // A plugin of this test only: the breaker is per plugin, and the other
        // tests in this binary share the globally installed one.
        let fixtures = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../target");
        let mut registry = crate::Registry::default();
        registry
            .load_dir(&fixtures.join("plugins-test/a2a-middleware"), KIND)
            .unwrap();
        let plugin = registry.get(KIND, "misbehave_test").unwrap().clone();
        let spec = |id: &str, params: &str| Spec {
            id: id.into(),
            name: id.into(),
            url: format!("http://127.0.0.1:1/{id}"),
            plugin: "misbehave_test".into(),
            params_json: params.into(),
            digest: [3; 32],
        };
        let refuse = Arc::new(
            Middleware::with_plugin(
                Arc::clone(&plugin),
                spec("refuse-agent", r#"{"mode":"refuse"}"#),
            )
            .unwrap(),
        );
        for _ in 0..(crate::BREAKER_FAILURES + 1) {
            let error = refuse
                .on_request("SendMessage", b"{}", Duration::from_secs(10))
                .await
                .unwrap_err();
            assert!(matches!(error, Error::Rpc { .. }), "{error:?}");
        }
        // Still answering: no pause was recorded.
        assert!(matches!(
            refuse
                .on_request("SendMessage", b"{}", Duration::from_secs(10))
                .await,
            Err(Error::Rpc { .. })
        ));
        // A size refusal is the host rejecting one body, not a plugin failure.
        let oversized = vec![b'x'; MAX_REQUEST_BODY + 1];
        for _ in 0..(crate::BREAKER_FAILURES + 1) {
            assert!(matches!(
                refuse
                    .on_request("SendMessage", &oversized, Duration::from_secs(10))
                    .await,
                Err(Error::TooLarge)
            ));
        }
        assert!(matches!(
            refuse
                .on_request("SendMessage", b"{}", Duration::from_secs(10))
                .await,
            Err(Error::Rpc { .. })
        ));

        // A timeout does trip it: after the pause every hook answers `Paused`.
        let slow = Arc::new(
            Middleware::with_plugin(
                Arc::clone(&plugin),
                spec("slow-agent", r#"{"mode":"slow","spin":4000000000}"#),
            )
            .unwrap(),
        );
        for _ in 0..crate::BREAKER_FAILURES {
            let error = slow
                .on_request("SendMessage", b"{}", Duration::from_millis(20))
                .await
                .unwrap_err();
            assert!(matches!(error, Error::Timeout), "{error:?}");
        }
        assert!(matches!(
            slow.on_request("SendMessage", b"{}", Duration::from_secs(10))
                .await,
            Err(Error::Paused)
        ));
    }

    /// An apply-time `configure` that never returns refuses the version once
    /// the configure budget elapses (M4).
    #[test]
    fn a_configure_that_never_returns_refuses_the_version() {
        if install_fixtures().is_none() {
            return;
        }
        let cache = Cache::new();
        let spec = Spec {
            id: "stuck-configure".into(),
            name: "stuck-configure".into(),
            url: "http://127.0.0.1:1/stuck".into(),
            plugin: "misbehave_test".into(),
            params_json: r#"{"mode":"stuck_configure"}"#.into(),
            digest: [1; 32],
        };
        let started = Instant::now();
        let refused = cache
            .stage_within(std::slice::from_ref(&spec), Duration::from_millis(100))
            .unwrap_err();
        assert!(
            matches!(refused.source, Error::ConfigureTimeout),
            "{:?}",
            refused.source
        );
        assert!(started.elapsed() < Duration::from_secs(10));
        // Nothing was published.
        assert!(cache.get("stuck-configure").is_none());
    }

    /// A staged set serves only once it is published (m1).
    #[test]
    fn a_staged_middleware_serves_only_after_publish() {
        if install_fixtures().is_none() {
            return;
        }
        let cache = Cache::new();
        let spec = Spec {
            id: "staged-agent".into(),
            name: "staged-agent".into(),
            url: "http://127.0.0.1:1/staged".into(),
            plugin: "misbehave_test".into(),
            params_json: r#"{"mode":"pass"}"#.into(),
            digest: [2; 32],
        };
        let staged = cache.stage(std::slice::from_ref(&spec)).unwrap();
        assert_eq!(staged.len(), 1);
        assert!(
            cache.get("staged-agent").is_none(),
            "a staged set does not serve before the version is accepted"
        );
        cache.publish(staged);
        assert!(cache.get("staged-agent").is_some());
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
