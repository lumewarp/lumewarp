//! The `upstream-auth` request adapter: one configured authenticator per target,
//! called with the bytes that are about to go upstream.
//!
//! A snapshot names a WASM plugin for one MCP server or A2A agent; applying it
//! configures that plugin once (a `configure` error refuses the version) and the
//! configured value is kept until the plugin name, its settings or the target
//! changes, so a plugin's token cache survives snapshot swaps. [`Cache`] holds
//! them; [`global`] is the process-wide instance the gateway uses.
//!
//! Calls are wasmtime async futures: the guest yields to the executor at every
//! epoch tick, and `host.http-fetch` awaits instead of blocking a thread. The
//! caller's timeout is the only limit; when it fires the future is dropped, the
//! Store is discarded and the call counts as `timeout`.
//!
//! Trust boundaries this module enforces, because the guest is not trusted:
//!
//! - `allowed_origins` (removed from the settings before `configure`) is the
//!   only reachable origin set; a non-listed origin fails before any connect.
//! - `secret()` sees `LUMEWARP_PLUGIN_SECRET_<NAME>` only, one read per value
//!   per configured authenticator.
//! - a trapped or timed-out Store is dropped, never returned to the pool.

use std::{
    collections::HashMap,
    sync::{
        atomic::{AtomicU8, Ordering},
        Arc, Mutex, OnceLock, RwLock,
    },
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

use tokio::sync::Semaphore;
use wasmtime::{component::ResourceAny, Store};

use crate::{
    auth::{
        exports::lumewarp::plugin::upstream_auth as wire, lumewarp::plugin::host as host_wire, Auth,
    },
    fetch,
    limits::{self, BoxFuture, MemoryReservation},
    pool::{self, InstancePool},
    AuthHost, Plugin, State,
};

/// `PluginDescriptor.kind` of the `upstream-auth` world.
pub const KIND: i32 = 2;
/// `lumewarp_plugin_*` label for this kind.
pub const KIND_LABEL: &str = "upstream-auth";
/// A plugin may ask for a longer cache, but not longer than this.
pub const MAX_CACHE: Duration = Duration::from_secs(3600);
/// Longest secret name accepted by `secret()`.
pub const MAX_SECRET_NAME: usize = 64;
/// Configured authenticators kept without a snapshot replacing them.
pub const MAX_CONFIGURED: usize = 4096;

/// Cacheability of an authenticator: unknown until the first result.
const UNKNOWN: u8 = 0;
const CACHEABLE: u8 = 1;
const UNCACHEABLE: u8 = 2;

/// Which proxy a target belongs to. Two targets may share a name across kinds.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Traffic {
    Mcp,
    A2a,
}

impl Traffic {
    fn to_wire(self) -> wire::Traffic {
        match self {
            Self::Mcp => wire::Traffic::Mcp,
            Self::A2a => wire::Traffic::A2a,
        }
    }

    /// `mcp` or `a2a`, for logs and spans.
    pub fn label(self) -> &'static str {
        match self {
            Self::Mcp => "mcp",
            Self::A2a => "a2a",
        }
    }
}

impl std::fmt::Display for Traffic {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.label())
    }
}

/// One target's plugin and settings, as the snapshot carries them. The timeout
/// is not part of the identity: it is passed per call, so editing it in the UI
/// keeps the configured plugin (and its token cache).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Spec {
    pub traffic: Traffic,
    /// The target's stable id (`McpServer.id` / `A2aAgent.id`), or its name when
    /// the id is empty (standalone YAML).
    pub id: String,
    pub name: String,
    /// The URL the gateway sends requests to; part of the identity because a
    /// token for one destination must not be sent to another.
    pub url: String,
    pub plugin: String,
    pub params_json: String,
    pub digest: [u8; 32],
}

impl Spec {
    /// The cache identity of a target: `(traffic, id)`.
    pub fn key(&self) -> (Traffic, String) {
        (self.traffic, self.id.clone())
    }
}

/// Why an authenticator could not be configured, or one request could not be.
#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error("auth plugin {0:?} is not loaded")]
    Missing(String),
    #[error("invalid settings: {0}")]
    Params(String),
    #[error("configure failed: {0}")]
    Configure(String),
    #[error("authorize failed: {0}")]
    Plugin(String),
    #[error("the call exceeded its configured timeout")]
    Timeout,
    /// Apply-time `configure` did not finish inside [`crate::CONFIGURE_TIMEOUT`].
    #[error("configuring the plugin timed out")]
    ConfigureTimeout,
    #[error("authorize trapped")]
    Trap,
    #[error("the plugin task did not finish")]
    Canceled,
}

/// The headers a plugin produced for one request.
#[derive(Clone, Debug, Default)]
pub struct Headers {
    pub headers: Vec<(String, Vec<u8>)>,
    /// The plugin's promise, in milliseconds. `0` means "this result must not be
    /// reused", and the gateway will call the plugin again next time.
    pub cache_ms: u32,
}

/// What the auth layer sees of the request about to be sent upstream. Client
/// credentials are already gone (plan L9).
#[derive(Debug)]
pub struct Outbound<'a> {
    pub method: &'a str,
    pub url: &'a str,
    pub headers: Vec<(String, Vec<u8>)>,
    pub body: &'a [u8],
}

/// Everything one configured authenticator may reach: its origins and the
/// secrets its plugin read.
struct Scope {
    origins: Arc<Vec<fetch::Origin>>,
    secrets: Mutex<HashMap<String, Option<String>>>,
}

impl Scope {
    fn secret(&self, name: &str) -> Option<String> {
        if name.is_empty()
            || name.len() > MAX_SECRET_NAME
            || !name
                .bytes()
                .all(|b| b.is_ascii_uppercase() || b.is_ascii_digit() || b == b'_')
        {
            return None;
        }
        let mut secrets = self.secrets.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(value) = secrets.get(name) {
            return value.clone();
        }
        // Read once per configured authenticator: a token fetched later must not
        // mix two secrets, and an operator's rotation takes effect on reconfigure.
        let value = std::env::var(format!("LUMEWARP_PLUGIN_SECRET_{name}")).ok();
        secrets.insert(name.to_owned(), value.clone());
        value
    }
}

/// The `host` interface of one call. `http-fetch` is a future: the guest's call
/// suspends while the request runs on the executor.
struct CallHost {
    scope: Arc<Scope>,
}

impl AuthHost for CallHost {
    fn http_fetch<'a>(
        &'a self,
        request: host_wire::HttpRequest,
    ) -> BoxFuture<'a, Result<host_wire::HttpResponse, String>> {
        Box::pin(async move {
            // The origin check happens here, before the client ever sees the
            // URL: a plugin cannot reach an origin its settings do not list.
            fetch::check_origin(&self.scope.origins, &request.url)?;
            let response = fetch::send(fetch::RequestSpec {
                method: request.method,
                url: request.url,
                headers: request.headers,
                body: request.body,
            })
            .await?;
            Ok(host_wire::HttpResponse {
                status: response.status,
                headers: response.headers,
                body: response.body,
            })
        })
    }

    fn secret(&self, name: &str) -> Option<String> {
        self.scope.secret(name)
    }

    fn now_unix_ms(&self) -> u64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_or(0, |elapsed| elapsed.as_millis() as u64)
    }
}

struct Cached {
    headers: Vec<(String, Vec<u8>)>,
    cache_ms: u32,
    expires: Instant,
}

struct Instance {
    store: Store<State>,
    binding: Auth,
    resource: ResourceAny,
    _slot: pool::Slot,
}

/// One target's configured plugin. Cheap to share: a call checks out a Store,
/// and calls never run in one Store at the same time.
pub struct Authenticator {
    plugin: Arc<Plugin>,
    spec: Spec,
    target: wire::TargetInfo,
    scope: Arc<Scope>,
    pool: InstancePool<Instance>,
    memory: Arc<Semaphore>,
    prewarm: usize,
    cached: Mutex<Option<Cached>>,
    /// Single-flight for a miss: a token refresh is not stampeded (plan P9).
    flight: tokio::sync::Mutex<()>,
    cacheable: AtomicU8,
}

impl std::fmt::Debug for Authenticator {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Authenticator")
            .field("traffic", &self.spec.traffic)
            .field("target", &self.spec.name)
            .field("plugin", &self.spec.plugin)
            .finish_non_exhaustive()
    }
}

impl Authenticator {
    fn new(spec: Spec) -> Result<Self, Error> {
        let plugin = crate::global()
            .get(KIND, &spec.plugin)
            .filter(|plugin| plugin.descriptor.kind == KIND)
            .cloned()
            .ok_or_else(|| Error::Missing(spec.plugin.clone()))?;
        let mut params = match serde_json::from_str::<serde_json::Value>(&spec.params_json) {
            Ok(serde_json::Value::Object(params)) => params,
            _ => return Err(Error::Params("params_json must be a JSON object".into())),
        };
        // `allowed_origins` belongs to the platform: the plugin's own settings
        // never see it (`docs/plugins.md`, "Upstream auth").
        let origins = fetch::parse_allowed(params.remove("allowed_origins").as_ref())
            .map_err(Error::Params)?;
        let target = wire::TargetInfo {
            traffic: spec.traffic.to_wire(),
            name: spec.name.clone(),
            url: spec.url.clone(),
            params_json: serde_json::Value::Object(params).to_string(),
        };
        let permits = limits::memory_permits_gate();
        // A quarter of the call permits at most, so a stuck auth plugin holds
        // only its own requests' permits (M2).
        let target_pool = std::thread::available_parallelism()
            .map_or(1, usize::from)
            .min(4)
            .min(permits.pool_share());
        let authenticator = Self {
            plugin,
            spec,
            target,
            scope: Arc::new(Scope {
                origins: Arc::new(origins),
                secrets: Mutex::new(HashMap::new()),
            }),
            pool: InstancePool::new(target_pool),
            memory: permits.semaphore(),
            prewarm: target_pool,
            cached: Mutex::new(None),
            flight: tokio::sync::Mutex::new(()),
            cacheable: AtomicU8::new(UNKNOWN),
        };
        Ok(authenticator)
    }

    /// Creates the warm instances, bounded by [`crate::CONFIGURE_TIMEOUT`]; the
    /// first one is mandatory, so a refused `configure` refuses the version.
    /// Only the apply path calls this (never a request), and it does not hold a
    /// cache lock (M4).
    fn warm(&self, timeout: Duration) -> Result<(), Error> {
        if self.prewarm == 0 {
            return Ok(());
        }
        let Some(slot) = self.pool.reserve().ok() else {
            return Ok(());
        };
        let first = crate::block_on_timeout(self.create_waiting(slot, Some(timeout)), timeout)
            .ok_or(Error::ConfigureTimeout)??;
        self.pool.push(first);
        for _ in 1..self.prewarm {
            let Ok(slot) = self.pool.reserve() else {
                break;
            };
            match crate::block_on_timeout(self.create_now(slot, Some(timeout)), timeout) {
                Some(Ok(Some(instance))) => self.pool.push(instance),
                Some(Ok(None)) => break,
                None => {
                    tracing::warn!(plugin = %self.spec.plugin, "pre-warming an auth plugin instance timed out");
                    break;
                }
                Some(Err(error)) => {
                    // m1: a configuration that fails while warming up is a
                    // counted failure, not a silently smaller pool.
                    tracing::warn!(%error, plugin = %self.spec.plugin, "pre-warming an auth plugin instance failed");
                    self.plugin.traps.fetch_add(1, Ordering::Relaxed);
                    self.plugin.record_failure();
                    crate::record(KIND_LABEL, &self.spec.plugin, "trap", Duration::ZERO);
                    break;
                }
            }
        }
        Ok(())
    }

    /// The plugin name, as the manifest declared it.
    pub fn plugin_name(&self) -> &str {
        &self.spec.plugin
    }

    /// Which proxy the target belongs to.
    pub fn traffic(&self) -> Traffic {
        self.spec.traffic
    }

    /// The target key this authenticator was configured for.
    pub fn target_id(&self) -> &str {
        &self.spec.id
    }

    /// Whether this authenticator still describes `spec`; unchanged targets are
    /// reused across snapshots so their cached token survives.
    pub fn matches(&self, spec: &Spec) -> bool {
        self.spec == *spec
    }

    /// The request-path form of [`Self::matches`], without building a [`Spec`].
    pub fn matches_request(&self, name: &str, url: &str, plugin: &str, digest: &[u8; 32]) -> bool {
        self.spec.name == name
            && self.spec.url == url
            && self.spec.plugin == plugin
            && self.spec.digest == *digest
    }

    /// Idle instances right now (diagnostics and tests).
    pub fn idle(&self) -> usize {
        self.pool.idle_len()
    }

    /// Live instances (idle and in use).
    pub fn live(&self) -> usize {
        self.pool.live()
    }

    /// Drops idle instances beyond `keep`.
    pub fn shrink(&self, keep: usize) {
        self.pool.shrink(keep);
    }

    async fn create(
        &self,
        slot: pool::Slot,
        _permit: MemoryReservation,
        budget: Option<Duration>,
    ) -> Result<Instance, Error> {
        let mut store = self.plugin.store();
        // Apply-time `configure` gets a hard epoch budget (M4).
        if let Some(budget) = budget {
            crate::set_apply_deadline(&mut store, budget);
        }
        let crate::Prepared::Auth(pre) = self.plugin.prepared() else {
            return Err(Error::Configure(
                "the component does not implement the auth world".to_owned(),
            ));
        };
        let host: Arc<dyn AuthHost> = Arc::new(CallHost {
            scope: Arc::clone(&self.scope),
        });
        store.data_mut().auth_host = Some(host);
        let binding = pre
            .instantiate_async(&mut store)
            .await
            .map_err(|error| Error::Configure(format!("{error:#}")))?;
        let resource = binding
            .lumewarp_plugin_upstream_auth()
            .authenticator()
            .call_configure(&mut store, &self.target)
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
            // Back to yielding: a request-path call is cancelled by its own
            // timeout, never trapped by the configure budget.
            crate::set_fair_epoch(&mut store);
        }
        Ok(Instance {
            store,
            binding,
            resource,
            _slot: slot,
        })
    }

    /// Creates an instance, waiting for a memory permit.
    async fn create_waiting(
        &self,
        slot: pool::Slot,
        budget: Option<Duration>,
    ) -> Result<Instance, Error> {
        let permit = MemoryReservation::acquire(Arc::clone(&self.memory))
            .await
            .ok_or(Error::Canceled)?;
        self.create(slot, permit, budget).await
    }

    /// Creates an instance only when a memory permit is free right now.
    async fn create_now(
        &self,
        slot: pool::Slot,
        budget: Option<Duration>,
    ) -> Result<Option<Instance>, Error> {
        let Some(permit) = MemoryReservation::try_acquire(Arc::clone(&self.memory)) else {
            return Ok(None);
        };
        self.create(slot, permit, budget).await.map(Some)
    }

    async fn checkout(&self) -> Result<Instance, Error> {
        let mut retried = false;
        loop {
            if let Some(instance) = self.pool.pop() {
                return Ok(instance);
            }
            let slot = match self.pool.reserve() {
                Ok(slot) => slot,
                Err(blocked) => match self.pool.wait(blocked).await {
                    Ok(Some(slot)) => slot,
                    // An instance came back; take it if it is still there.
                    Ok(None) => continue,
                    // Only a closed budget gets here, which nothing does.
                    Err(_) => return Err(Error::Canceled),
                },
            };
            // The permit covers instantiation only; queueing for this pool
            // never holds one (M2).
            match self.create_waiting(slot, None).await {
                Ok(instance) => return Ok(instance),
                // Out of budget: give the pool's idle instances back and
                // try once more before failing the call.
                Err(_) if !retried && self.pool.idle_len() > 0 => {
                    retried = true;
                    self.pool.shrink(0);
                    continue;
                }
                Err(error) => return Err(error),
            }
        }
    }

    /// Headers for one upstream request, bounded by `timeout` (a cached result
    /// that is still valid is returned without entering the sandbox).
    pub async fn authorize(
        self: &Arc<Self>,
        request: Outbound<'_>,
        timeout: Duration,
    ) -> Result<Headers, Error> {
        if let Some(hit) = self.hit() {
            return Ok(hit);
        }
        // From here on everything — waiting for the single-flight lock, for a
        // pool instance, the plugin call and its http-fetches — counts against
        // the same timeout.
        match tokio::time::timeout(timeout, self.refresh(request)).await {
            Ok(result) => result,
            Err(_) => {
                self.plugin.timeouts.fetch_add(1, Ordering::Relaxed);
                self.plugin.record_failure();
                crate::record(KIND_LABEL, &self.spec.plugin, "timeout", timeout);
                tracing::warn!(
                    target = %self.spec.name,
                    plugin = %self.spec.plugin,
                    "outbound auth call timed out"
                );
                Err(Error::Timeout)
            }
        }
    }

    async fn refresh(self: &Arc<Self>, request: Outbound<'_>) -> Result<Headers, Error> {
        // A plugin that returned `cache-ms: 0` must be called per request, so
        // it never waits behind another request's token refresh.
        let _flight = if self.cacheable.load(Ordering::Relaxed) != UNCACHEABLE {
            Some(self.flight.lock().await)
        } else {
            None
        };
        if let Some(hit) = self.hit() {
            return Ok(hit);
        }
        let result = self.call(request).await;
        if let Ok(result) = &result {
            if result.cache_ms > 0 {
                let ttl = Duration::from_millis(result.cache_ms.into()).min(MAX_CACHE);
                *self.cached.lock().unwrap_or_else(|e| e.into_inner()) = Some(Cached {
                    headers: result.headers.clone(),
                    cache_ms: result.cache_ms,
                    expires: Instant::now() + ttl,
                });
                self.cacheable.store(CACHEABLE, Ordering::Relaxed);
            } else if self.cacheable.load(Ordering::Relaxed) == UNKNOWN {
                self.cacheable.store(UNCACHEABLE, Ordering::Relaxed);
            }
        }
        // The single-flight guard is released after the cache is filled: a
        // waiter that took it would see no entry before this point.
        drop(_flight);
        result
    }

    fn hit(&self) -> Option<Headers> {
        let cached = self.cached.lock().unwrap_or_else(|e| e.into_inner());
        let entry = cached.as_ref()?;
        (entry.expires > Instant::now()).then(|| Headers {
            headers: entry.headers.clone(),
            cache_ms: entry.cache_ms,
        })
    }

    async fn call(&self, request: Outbound<'_>) -> Result<Headers, Error> {
        if self.plugin.paused() {
            crate::record(KIND_LABEL, &self.spec.plugin, "error", Duration::ZERO);
            return Err(Error::Plugin(
                "the auth plugin is paused after failures".into(),
            ));
        }
        let request = wire::OutboundRequest {
            method: request.method.to_owned(),
            url: request.url.to_owned(),
            headers: request.headers,
            body: request.body.to_vec(),
        };
        let started = Instant::now();
        let span = tracing::info_span!(
            "plugin",
            kind = KIND_LABEL,
            plugin = %self.spec.plugin,
            target = %self.spec.name,
        );
        let mut instance = match self.checkout().await {
            Ok(instance) => instance,
            // A `configure` failure is an error result, not a trap.
            Err(error @ Error::Configure(_)) => {
                let _entered = span.enter();
                self.plugin.record_failure();
                crate::record(KIND_LABEL, &self.spec.plugin, "error", started.elapsed());
                tracing::warn!(%error, plugin = %self.spec.plugin, "an auth plugin instance failed");
                return Err(error);
            }
            Err(error) => {
                let _entered = span.enter();
                self.plugin.traps.fetch_add(1, Ordering::Relaxed);
                self.plugin.record_failure();
                crate::record(KIND_LABEL, &self.spec.plugin, "trap", started.elapsed());
                tracing::warn!(%error, plugin = %self.spec.plugin, "an auth plugin instance failed");
                return Err(Error::Trap);
            }
        };
        // The permit is held for the call only; the instance is already ours,
        // so a queue for this target never blocks another plugin (M2).
        let Some(_permit) = MemoryReservation::acquire(Arc::clone(&self.memory)).await else {
            self.pool.push(instance);
            return Err(Error::Canceled);
        };
        // Each call may install its own host: the secret cache lives in `Scope`.
        instance.store.data_mut().auth_host = Some(Arc::new(CallHost {
            scope: Arc::clone(&self.scope),
        }));
        let result = instance
            .binding
            .lumewarp_plugin_upstream_auth()
            .authenticator()
            .call_authorize(&mut instance.store, instance.resource, &request)
            .await;
        let elapsed = started.elapsed();
        let _entered = span.enter();
        match result {
            Ok(Ok(answer)) => {
                self.plugin.record_success();
                crate::record(KIND_LABEL, &self.spec.plugin, "ok", elapsed);
                self.pool.push(instance);
                Ok(Headers {
                    headers: answer.headers,
                    cache_ms: answer.cache_ms,
                })
            }
            Ok(Err(message)) => {
                // A normal guest error leaves the Store usable.
                self.plugin.record_failure();
                crate::record(KIND_LABEL, &self.spec.plugin, "error", elapsed);
                self.pool.push(instance);
                Err(Error::Plugin(message))
            }
            Err(error) => {
                // A trapped Store is dropped, never pooled.
                self.plugin.traps.fetch_add(1, Ordering::Relaxed);
                self.plugin.record_failure();
                crate::record(KIND_LABEL, &self.spec.plugin, "trap", elapsed);
                tracing::warn!(plugin = %self.spec.plugin, error = %error, "an auth plugin trapped");
                Err(Error::Trap)
            }
        }
    }
}

/// Configured authenticators of the current configuration, keyed by target.
#[derive(Default)]
pub struct Cache {
    entries: RwLock<HashMap<(Traffic, String), Arc<Authenticator>>>,
}

/// A configuration refused because one target's plugin would not configure.
#[derive(Debug, thiserror::Error)]
#[error("target {target:?}: {source}")]
pub struct Refused {
    /// The target name, for the gateway's log and NACK reason.
    pub target: String,
    #[source]
    pub source: Error,
}

impl Cache {
    pub fn new() -> Self {
        Self::default()
    }

    /// The authenticator configured for `traffic`/`id`, if any.
    pub fn get(&self, traffic: Traffic, id: &str) -> Option<Arc<Authenticator>> {
        self.entries
            .read()
            .unwrap_or_else(|e| e.into_inner())
            .get(&(traffic, id.to_owned()))
            .cloned()
    }

    /// The authenticator of `spec`, configuring it if this key is new or its
    /// settings changed.
    pub fn get_or_configure(&self, spec: &Spec) -> Result<Arc<Authenticator>, Error> {
        let key = spec.key();
        if let Some(existing) = self
            .entries
            .read()
            .unwrap_or_else(|e| e.into_inner())
            .get(&key)
        {
            if existing.matches(spec) {
                return Ok(Arc::clone(existing));
            }
        }
        let configured = Arc::new(Authenticator::new(spec.clone())?);
        let mut entries = self.entries.write().unwrap_or_else(|e| e.into_inner());
        if let Some(existing) = entries.get(&key) {
            if existing.matches(spec) {
                return Ok(Arc::clone(existing));
            }
        }
        if entries.len() >= MAX_CONFIGURED && !entries.contains_key(&key) {
            // Stay bounded even if a caller never replaces the whole set.
            if let Some(stale) = entries.keys().find(|entry| **entry != key).cloned() {
                entries.remove(&stale);
            }
        }
        entries.insert(key, Arc::clone(&configured));
        Ok(configured)
    }

    /// Configures `specs` into a set that does **not** serve yet: a version the
    /// gateway later refuses leaves the previous set untouched (m1).
    ///
    /// Instances whose name, settings and destination did not change are kept
    /// (their token cache with them); a fresh one is warmed, so a `configure`
    /// error — or its [`crate::CONFIGURE_TIMEOUT`] — refuses the version. This
    /// is apply-time work and holds no lock while it runs.
    pub fn stage(&self, specs: &[Spec]) -> Result<Staged, Refused> {
        self.stage_within(specs, crate::CONFIGURE_TIMEOUT)
    }

    /// [`Cache::stage`] with an explicit configure budget (tests use a short
    /// one; the gateway always takes the default).
    pub fn stage_within(&self, specs: &[Spec], timeout: Duration) -> Result<Staged, Refused> {
        // A copy of the Arc table, not a read guard: configuring a plugin can
        // take a while, and the request path must not wait behind it.
        let current: HashMap<_, _> = self
            .entries
            .read()
            .unwrap_or_else(|e| e.into_inner())
            .clone();
        let mut next: HashMap<(Traffic, String), Arc<Authenticator>> =
            HashMap::with_capacity(specs.len());
        for spec in specs {
            let key = spec.key();
            let (configured, fresh) = match current.get(&key) {
                Some(existing) if existing.matches(spec) => (Arc::clone(existing), false),
                _ => (
                    Arc::new(Authenticator::new(spec.clone()).map_err(|source| Refused {
                        target: spec.name.clone(),
                        source,
                    })?),
                    true,
                ),
            };
            if fresh {
                configured.warm(timeout).map_err(|source| Refused {
                    target: spec.name.clone(),
                    source,
                })?;
            }
            if next.insert(key, configured).is_some() {
                return Err(Refused {
                    target: spec.name.clone(),
                    source: Error::Params(format!("two targets share the id {:?}", spec.id)),
                });
            }
        }
        Ok(Staged { entries: next })
    }

    /// Makes a staged set the one serving. Called once the version is accepted.
    pub fn publish(&self, staged: Staged) {
        *self.entries.write().unwrap_or_else(|e| e.into_inner()) = staged.entries;
    }

    /// Stage and publish in one step (tests and standalone load).
    pub fn replace(&self, specs: &[Spec]) -> Result<(), Refused> {
        let staged = self.stage(specs)?;
        self.publish(staged);
        Ok(())
    }
}

/// Authenticators configured for one version that has not been accepted yet.
#[derive(Debug, Default)]
pub struct Staged {
    entries: HashMap<(Traffic, String), Arc<Authenticator>>,
}

impl Staged {
    /// How many targets it configures (tests and logging).
    pub fn len(&self) -> usize {
        self.entries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }
}

static GLOBAL: OnceLock<Cache> = OnceLock::new();

/// The process-wide authenticator cache. One gateway process serves one set of
/// targets at a time, so there is one cache.
pub fn global() -> &'static Cache {
    GLOBAL.get_or_init(Cache::new)
}

/// The authenticator configured for `traffic`/`id` in the process-wide cache.
pub fn get(traffic: Traffic, id: &str) -> Option<Arc<Authenticator>> {
    global().get(traffic, id)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn secret_names_are_restricted_to_the_plugin_prefix() {
        let scope = Scope {
            origins: Arc::new(Vec::new()),
            secrets: Mutex::new(HashMap::new()),
        };
        // Unset names stay absent, including plain environment variables whose
        // name happens to match the regex (`PATH` is set in every process).
        assert_eq!(scope.secret("PATH"), None);
        assert_eq!(scope.secret("lowercase"), None);
        assert_eq!(scope.secret(""), None);
        assert_eq!(scope.secret(&"A".repeat(MAX_SECRET_NAME + 1)), None);
        std::env::set_var("LUMEWARP_PLUGIN_SECRET_UNIT_TEST_KEY", "value");
        assert_eq!(scope.secret("UNIT_TEST_KEY").as_deref(), Some("value"));
        // The first read is cached: the environment is not consulted again.
        std::env::remove_var("LUMEWARP_PLUGIN_SECRET_UNIT_TEST_KEY");
        assert_eq!(scope.secret("UNIT_TEST_KEY").as_deref(), Some("value"));
    }

    #[test]
    fn allowed_origins_are_removed_before_configure() {
        // A settings object without the reserved key keeps its own keys.
        let mut params: serde_json::Map<String, serde_json::Value> = serde_json::from_str(
            r#"{"allowed_origins":["https://auth.example.test"],"secret_name":"KEY"}"#,
        )
        .unwrap();
        let origins = fetch::parse_allowed(params.remove("allowed_origins").as_ref()).unwrap();
        assert_eq!(origins.len(), 1);
        assert_eq!(
            serde_json::Value::Object(params).to_string(),
            r#"{"secret_name":"KEY"}"#
        );
    }
}
