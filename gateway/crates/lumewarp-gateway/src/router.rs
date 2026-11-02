use std::{
    collections::{HashMap, HashSet},
    sync::{
        atomic::{AtomicBool, AtomicUsize, Ordering},
        Arc,
    },
    time::Duration,
};

use bytes::Bytes;
use hyper::{
    header::{HeaderName, HeaderValue},
    HeaderMap, Uri,
};
use lumewarp_config::AuthMode;
use lumewarp_proto::config::v1::{
    CircuitBreaker, KeyStatus, Price, RateLimit, RetryPolicy, RouterMember, Snapshot, StickyBy,
    StreamUsageMode,
};
use lumewarp_router_api::{
    Builtin, CandidateSource, Candidates, Load, MemberInfo, Needs, RequestInfo, RouterInfo,
    Strategy, ThreadRng,
};
use prometheus_client::metrics::{counter::Counter, gauge::Gauge};

use crate::{
    auth::Caller,
    balance::{self, Latency, Scrape, VllmLoad},
    health::{Breaker, Probe},
    metrics::{HandleKey, LabelId, Metrics, RequestMetrics, API_ENDPOINTS},
    ratelimit::clock,
    runtime::Runtime,
    snapshot::{self, ApplyError},
};

/// Proxied endpoints: chat_completions, completions, embeddings.
const ENDPOINTS: usize = 3;

pub struct Model {
    pub name: String,
    /// Reported with usage and spans: what served the request at that moment.
    pub upstream: Arc<str>,
    pub upstream_model: Arc<str>,
    // Each absolute URI contains its precompiled endpoint PathAndQuery.
    pub endpoints: [Uri; ENDPOINTS],
    pub host: HeaderValue,
    pub authorization: Option<HeaderValue>,
    pub upstream_model_json: Bytes,
    pub timeout: Duration,
    /// Shared through `Runtime::inflight` by model name, so it survives swaps.
    pub in_flight: Arc<AtomicUsize>,
    /// Metering context, immutable for every request holding this model:
    /// a request keeps the price it started with across snapshot swaps.
    /// `None` = price unknown (cost reported unknown, never 0).
    pub price: Option<Price>,
    /// Resolved: UNSPECIFIED is stored as `IncludeUsage`.
    pub stream_usage_mode: StreamUsageMode,
    /// Interned model name for metric handle lookups.
    pub label: LabelId,
    pub breaker: Arc<Breaker>,
    pub circuit: Option<CircuitBreaker>,
    pub healthy: Arc<AtomicBool>,
    /// Shared by model name through `Runtime::balance` (survive swaps).
    pub latency: Arc<Latency>,
    /// Feed `latency` only when a `lowest_latency` router uses this model.
    pub track_latency: bool,
    pub load: Arc<VllmLoad>,
}

/// A compiled virtual key with its already-resolved effective policy.
// Ids and scopes are read by usage reporting and rate limiting (W2).
pub struct KeyContext {
    pub key_id: Arc<str>,
    pub team_id: Arc<str>,
    pub status: KeyStatus,
    /// 0 = never expires.
    pub expires_at_ms: i64,
    pub allow_all_models: bool,
    pub allowed_models: HashSet<Box<str>>,
    pub scopes: Vec<(Arc<str>, RateLimit)>,
    /// Interned `team_id` for metric handle lookups (0 = no team).
    pub team_label: LabelId,
}

/// The models of one traffic split (or of the whole router without one).
struct Pool {
    models: Vec<Arc<Model>>,
    /// Position of `models[0]` among all of the router's members, so that a
    /// member keeps its index whichever split's pool it is seen through.
    first_member: usize,
    cumulative_weights: Vec<u64>,
    total_weight: u64,
}

struct PoolSource<'a> {
    pool: &'a Pool,
    now: u64,
}

impl CandidateSource for PoolSource<'_> {
    fn len(&self) -> usize {
        self.pool.models.len()
    }
    fn name(&self, i: usize) -> &str {
        &self.pool.models[i].name
    }
    fn member_index(&self, i: usize) -> usize {
        self.pool.first_member + i
    }
    fn weight(&self, i: usize) -> u64 {
        self.pool.cumulative_weights[i]
            - if i == 0 {
                0
            } else {
                self.pool.cumulative_weights[i - 1]
            }
    }
    fn in_flight(&self, i: usize) -> usize {
        self.pool.models[i].in_flight.load(Ordering::Relaxed)
    }
    fn latency_ewma_us(&self, i: usize) -> Option<u64> {
        let us = self.pool.models[i].latency.get_us();
        (us != 0).then_some(us)
    }
    fn load(&self, i: usize) -> Load {
        self.pool.models[i].load.view(self.now)
    }
    fn total_weight(&self) -> u64 {
        self.pool.total_weight
    }
    fn weighted_index(&self, draw: u64) -> usize {
        self.pool.weighted_index(draw)
    }
}

enum CompiledStrategy {
    Builtin(Builtin),
    Custom(Box<dyn Strategy>, Needs),
}

impl CompiledStrategy {
    /// Every strategy, built-in or not, parses its settings here: a router the
    /// gateway cannot serve is refused at load (standalone) or NACKed.
    fn configure(r: &lumewarp_proto::config::v1::Router) -> Result<Self, ApplyError> {
        let factory = lumewarp_router_api::lookup(&r.strategy)
            .map_err(|_| invalid("duplicate strategy registration".into()))?
            .ok_or_else(|| ApplyError::unsupported("unregistered strategy"))?;
        let (infos, member_params) = router_members(r)?;
        let info = RouterInfo {
            name: &r.name,
            members: &infos,
        };
        let mut params = snapshot::strategy_params(r)?;
        // Reserved for the gateway (`strategy_headers`); strategies never declare it.
        if let Some(params) = params.as_object_mut() {
            params.remove("headers");
        }
        // Do not propagate plugin error text: params may contain private data.
        let created = (factory.create)(&info, &params, &member_params)
            .map_err(|_| invalid(format!("router {:?} strategy configuration failed", r.name)))?;
        Ok(match snapshot::builtin_strategy(&r.strategy) {
            Some(builtin) => Self::Builtin(builtin),
            None => Self::Custom(created, (factory.needs)()),
        })
    }

    fn needs(&self) -> Needs {
        match self {
            Self::Builtin(s) => s.needs(),
            Self::Custom(_, needs) => *needs,
        }
    }

    fn pick<S: CandidateSource, E: Fn(usize) -> bool>(
        &self,
        req: &RequestInfo<'_>,
        c: &Candidates<'_, S, E>,
        rng: &mut ThreadRng,
    ) -> Option<usize> {
        match self {
            Self::Builtin(s) => s.pick_from(c, rng),
            Self::Custom(s, _) => {
                let picked = s.pick(req, &c.erased(), rng)?;
                c.get(picked).map(|d| d.index)
            }
        }
    }
}

/// A pool of models with a strategy: a `Router` of the snapshot, or the
/// single-attempt pool of a `Model`. Holds no name, since several names can
/// share it; what was called comes from `Callable`.
pub struct Router {
    /// One pool per traffic split, in declaration order; exactly one pool
    /// when the router has no split.
    pools: Vec<Pool>,
    split_cumulative: Vec<u64>,
    split_total: u64,
    /// `lumewarp_split_requests_total{router,split}`; empty without splits.
    split_requests: Vec<Counter>,
    sticky: StickyBy,
    sticky_header: Option<HeaderName>,
    strategy: CompiledStrategy,
    strategy_headers: Vec<String>,
    pub retry: Option<RetryPolicy>,
    /// Single-model pools, with `lumewarp_fallbacks_total{router,model}`.
    pub fallbacks: Vec<(Arc<Router>, Counter)>,
    pub budget_exhausted: Counter,
}

/// One entry of the name lookup: what a client can call, and how it got there.
/// An alias shares its target's `Arc<Router>`, so the called name and the
/// provenance must be read from here, never from the pool.
pub struct Callable {
    /// The called name: `requested` in metrics, usage and spans.
    pub name: Arc<str>,
    label: LabelId,
    /// `name` when it is an alias.
    pub alias: Option<Arc<str>>,
    /// The router behind `name`; `None` for a model, directly or via an alias.
    pub router: Option<Arc<str>>,
    pub target: Arc<Router>,
    /// `lumewarp_upstream_attempts_total{outcome="circuit_open"}` labelled with
    /// this name, for every breaker-guarded model of the target and of its
    /// fallbacks.
    circuit_open: Vec<(LabelId, Counter)>,
}

#[cfg(test)]
impl std::ops::Deref for Callable {
    type Target = Router;
    fn deref(&self) -> &Router {
        &self.target
    }
}

/// Models already counted as `circuit_open` for one request, so each
/// skipped candidate is counted at most once however often selection runs.
/// Fixed capacity: no allocation; beyond it, further skips go uncounted.
#[derive(Default)]
pub struct Skipped {
    seen: [LabelId; 16],
    len: usize,
}

impl Skipped {
    fn first(&mut self, label: LabelId) -> bool {
        if self.seen[..self.len].contains(&label) || self.len == self.seen.len() {
            return false;
        }
        self.seen[self.len] = label;
        self.len += 1;
        true
    }
}

impl Router {
    /// The single-attempt pool of a `Model`: no retry, no fallback, the default
    /// strategy. Weights of routers using the model do not apply here.
    fn single(model: Arc<Model>) -> Self {
        Self {
            pools: vec![Pool {
                models: vec![model],
                first_member: 0,
                cumulative_weights: vec![1],
                total_weight: 1,
            }],
            split_cumulative: vec![1],
            split_total: 1,
            split_requests: Vec::new(),
            sticky: StickyBy::None,
            sticky_header: None,
            strategy: CompiledStrategy::Builtin(Builtin::Weighted),
            strategy_headers: Vec::new(),
            retry: None,
            fallbacks: Vec::new(),
            // Never read: only a retry policy can exhaust the budget.
            budget_exhausted: Counter::default(),
        }
    }

    fn models(&self) -> impl Iterator<Item = &Arc<Model>> {
        self.pools.iter().flat_map(|pool| &pool.models)
    }

    /// Creates the zero-valued series of a name on this pool, as P0 did, and
    /// returns its `circuit_open` counters. Compile time only.
    fn preregister(&self, name: &str, metrics: &Metrics) -> Vec<(LabelId, Counter)> {
        for model in std::iter::once("").chain(self.models().map(|d| d.name.as_str())) {
            for endpoint in 0..ENDPOINTS {
                drop(metrics.handles(name, model, endpoint, ""));
            }
        }
        for d in self.models() {
            drop(metrics.attempt_handles(name, &d.name));
        }
        let mut circuit_open: Vec<(LabelId, Counter)> = Vec::new();
        let fallbacks = self.fallbacks.iter().flat_map(|(pool, _)| pool.models());
        for d in self.models().chain(fallbacks) {
            if d.circuit.is_some() && !circuit_open.iter().any(|(l, _)| *l == d.label) {
                let [_, _, _, _, open, _] = metrics.attempt_handles(name, &d.name);
                circuit_open.push((d.label, open));
            }
        }
        circuit_open
    }

    /// Picks the split for a request: by the sticky hash when the router is
    /// sticky and the request carries the input, else by a random draw.
    /// Allocation-free: the header value / ids are hashed in place.
    pub fn split_for(&self, headers: &HeaderMap, caller: &Caller) -> usize {
        if self.pools.len() <= 1 {
            return 0;
        }
        let key: Option<&[u8]> = match (self.sticky, caller) {
            (StickyBy::Header, _) => self
                .sticky_header
                .as_ref()
                .and_then(|name| headers.get(name))
                .map(HeaderValue::as_bytes),
            (StickyBy::Team, Caller::Key(key)) if !key.team_id.is_empty() => {
                Some(key.team_id.as_bytes())
            }
            (StickyBy::Key, Caller::Key(key)) => Some(key.key_id.as_bytes()),
            _ => None,
        };
        let draw = match key {
            Some(key) => balance::scale(balance::sticky_hash(key), self.split_total),
            None => fastrand::u64(0..self.split_total),
        };
        self.split_cumulative.partition_point(|&w| w <= draw)
    }

    /// `first` first, then the other pools in declaration order.
    fn pool_order(&self, first: usize) -> impl Iterator<Item = &Pool> + '_ {
        let len = self.pools.len();
        (0..len).map(move |offset| &self.pools[(first + offset) % len])
    }
}

impl Pool {
    fn pick(
        &self,
        strategy: &CompiledStrategy,
        req: &RequestInfo<'_>,
        excluded: Option<&[LabelId]>,
        now: u64,
        strict: bool,
    ) -> Option<usize> {
        let eligible = |i: usize| {
            let d = &self.models[i];
            !excluded.is_some_and(|tried| tried.contains(&d.label))
                && (excluded.is_none() || d.breaker.retry_after.load(Ordering::Relaxed) <= now)
                && if strict {
                    d.healthy.load(Ordering::Acquire)
                        && (d.circuit.is_none() || d.breaker.available(now))
                } else {
                    // Failing open never piles onto a half-open trial: the
                    // pick could not be admitted, while another candidate can.
                    d.circuit.is_none() || !d.breaker.trial_in_progress()
                }
        };
        let source = PoolSource { pool: self, now };
        let candidates = Candidates::new(&source, &eligible);
        strategy.pick(req, &candidates, &mut ThreadRng)
    }

    fn weighted_index(&self, draw: u64) -> usize {
        self.cumulative_weights
            .partition_point(|&weight| weight <= draw)
    }
}

pub struct InFlight {
    pub model: Arc<Model>,
    pub metrics: Arc<RequestMetrics>,
    trial: bool,
    finished: bool,
}

impl InFlight {
    pub fn complete(&mut self, retryable_failure: bool) {
        self.finished = true;
        if let Some(config) = self.model.circuit {
            self.model
                .breaker
                .finish(config, self.trial, retryable_failure, clock::mono_ms());
        }
    }
}

impl Drop for InFlight {
    fn drop(&mut self) {
        if self.trial && !self.finished {
            self.model.breaker.abandon_trial(clock::mono_ms());
        }
        self.model.in_flight.fetch_sub(1, Ordering::Relaxed);
    }
}

pub struct RoutingTable {
    pub version: u64,
    pub keys: HashMap<[u8; 32], Arc<KeyContext>>,
    /// Every callable name, whatever its kind. A request does one lookup here.
    pub names: HashMap<Box<str>, Arc<Callable>>,
    pub models_json: Bytes,
    pub auth_mode: AuthMode,
    pub runtime: Arc<Runtime>,
    /// Pre-routing rejections and local endpoints (model "", team "").
    pub rejected: [Arc<RequestMetrics>; API_ENDPOINTS.len()],
    /// Every callable name, sorted, for filtered `/v1/models`.
    listed: Box<[Box<str>]>,
    unknown_label: LabelId,
    probes: Vec<Probe>,
    unprobed: Vec<(Arc<AtomicBool>, Gauge)>,
    scrapes: Vec<Scrape>,
    /// Breakers of models whose upstream has no `circuit_breaker`
    /// (any more): reset to closed when this snapshot is activated.
    unbroken: Vec<Arc<Breaker>>,
}

fn invalid(reason: String) -> ApplyError {
    ApplyError::invalid(reason)
}

impl RoutingTable {
    /// Compiles a validated snapshot. Fails closed: with `RequireKey` and no
    /// keys every `/v1/*` request is rejected.
    pub fn compile(
        snapshot: &Snapshot,
        auth_mode: AuthMode,
        runtime: &Arc<Runtime>,
    ) -> Result<Self, ApplyError> {
        let capabilities = snapshot::supported_capabilities();
        let caps: Vec<_> = capabilities.iter().map(String::as_str).collect();
        snapshot::validate(snapshot, &caps)?;
        let mut strategies = HashMap::new();
        for router in &snapshot.routers {
            strategies.insert(router.name.as_str(), CompiledStrategy::configure(router)?);
        }
        let mut upstreams = HashMap::new();
        let mut probes = Vec::new();
        let mut unprobed = Vec::new();
        let mut metrics_uris = HashMap::new();
        // Which models need latency samples, which upstreams scraping.
        let upstream_of: HashMap<&str, &str> = snapshot
            .models
            .iter()
            .map(|d| (d.name.as_str(), d.upstream.as_str()))
            .collect();
        let mut latency_tracked = HashSet::new();
        let mut scraped_upstreams = HashSet::new();
        for r in &snapshot.routers {
            let names = router_model_names(r);
            let needs = strategies[r.name.as_str()].needs();
            if needs.latency {
                latency_tracked.extend(router_model_names(r));
            }
            if needs.vllm_load {
                scraped_upstreams.extend(names.filter_map(|d| upstream_of.get(d).copied()));
            }
        }
        for upstream in &snapshot.upstreams {
            let name = &upstream.name;
            let base: Uri = upstream
                .base_url
                .parse()
                .map_err(|_| invalid(format!("upstream {name:?} has an invalid base URL")))?;
            if !matches!(base.scheme_str(), Some("http" | "https"))
                || base.authority().is_none()
                || base.authority().is_some_and(|a| a.as_str().contains('@'))
                || base.query().is_some()
            {
                return Err(invalid(format!(
                    "upstream {name:?} requires an http(s) base URL without userinfo or query"
                )));
            }
            let authorization = (!upstream.api_key_ref.is_empty())
                .then(|| {
                    let env = &upstream.api_key_ref;
                    // Do not retain VarError::NotUnicode: its Display contains the secret.
                    let key = std::env::var(env)
                        .map_err(|_| invalid(format!("missing or invalid api_key_env {env:?}")))?;
                    let mut value =
                        HeaderValue::from_str(&format!("Bearer {key}")).map_err(|_| {
                            invalid(format!("api_key_env {env:?} is not a valid header value"))
                        })?;
                    value.set_sensitive(true);
                    Ok(value)
                })
                .transpose()?;
            let host =
                HeaderValue::from_str(base.authority().expect("validated authority").as_str())
                    .map_err(|_| invalid(format!("upstream {name:?} has an invalid host")))?;
            let endpoint = |suffix: &str| -> Result<Uri, ApplyError> {
                let mut parts = base.clone().into_parts();
                parts.path_and_query = Some(
                    format!("{}{suffix}", base.path().trim_end_matches('/'))
                        .parse()
                        .map_err(|_| invalid(format!("upstream {name:?} has an invalid path")))?,
                );
                Uri::from_parts(parts)
                    .map_err(|_| invalid(format!("upstream {name:?} has an invalid URL")))
            };
            let endpoints = [
                endpoint("/chat/completions")?,
                endpoint("/completions")?,
                endpoint("/embeddings")?,
            ];
            let metrics_uri = metrics_uri(&base, &upstream.metrics_url).ok_or_else(|| {
                invalid(format!(
                    "upstream {name:?} requires an http(s) metrics_url without userinfo"
                ))
            })?;
            // The upstream's bearer token goes only to its own host.
            let metrics_authorization = authorization
                .clone()
                .filter(|_| metrics_uri.authority() == base.authority());
            metrics_uris.insert(name.as_str(), (metrics_uri, metrics_authorization));
            let upstream_name: Arc<str> = name.as_str().into();
            let healthy = runtime.health.upstream(name);
            let gauge = runtime.metrics.health_gauge(name);
            if let Some(config) = &upstream.health_check {
                probes.push(Probe {
                    name: name.clone(),
                    uri: endpoint(if config.path.is_empty() {
                        "/models"
                    } else {
                        &config.path
                    })?,
                    authorization: authorization.clone(),
                    config: config.clone(),
                    healthy: healthy.clone(),
                    gauge,
                });
            } else {
                unprobed.push((healthy.clone(), gauge));
            }
            // Zero is "unset" on the wire; use the standalone default.
            let timeout_ms = match upstream.timeout_ms {
                0 => lumewarp_config::default_timeout_ms(),
                ms => ms,
            };
            let stream_usage_mode = match StreamUsageMode::try_from(upstream.stream_usage_mode)
                .expect("validated stream_usage_mode")
            {
                StreamUsageMode::Unspecified => StreamUsageMode::IncludeUsage,
                mode => mode,
            };
            upstreams.insert(
                name.as_str(),
                (
                    upstream_name,
                    endpoints,
                    host,
                    authorization,
                    Duration::from_millis(timeout_ms.into()),
                    stream_usage_mode,
                    healthy,
                    upstream.circuit_breaker,
                ),
            );
        }
        let metrics = &runtime.metrics;
        let mut models = HashMap::new();
        for d in &snapshot.models {
            let (
                upstream_name,
                endpoints,
                host,
                authorization,
                timeout,
                stream_usage_mode,
                healthy,
                circuit,
            ) = &upstreams[d.upstream.as_str()];
            metrics.register_model(&d.name);
            models.insert(
                d.name.as_str(),
                Arc::new(Model {
                    name: d.name.clone(),
                    upstream: upstream_name.clone(),
                    upstream_model: d.upstream_model.as_str().into(),
                    endpoints: endpoints.clone(),
                    host: host.clone(),
                    authorization: authorization.clone(),
                    upstream_model_json: Bytes::from(
                        serde_json::to_vec(&d.upstream_model).expect("string serializes"),
                    ),
                    timeout: *timeout,
                    in_flight: runtime.inflight.counter(&d.name),
                    price: d.price,
                    stream_usage_mode: *stream_usage_mode,
                    label: metrics.label_id(&d.name),
                    breaker: runtime
                        .health
                        .breaker(&d.name, metrics.circuit_gauge(&d.name)),
                    circuit: *circuit,
                    healthy: healthy.clone(),
                    latency: runtime.balance.latency(&d.name),
                    track_latency: latency_tracked.contains(d.name.as_str()),
                    load: runtime.balance.load(&d.name, metrics),
                }),
            );
        }

        // Pre-register zero-valued anonymous series as P0 did. This touches
        // only the registry, not the handle cache, which stays sparse.
        for endpoint in 0..ENDPOINTS {
            drop(metrics.handles("unknown", "", endpoint, ""));
        }
        let mut scrapes: Vec<Scrape> = Vec::new();
        for upstream in &snapshot.upstreams {
            let name = upstream.name.as_str();
            if !scraped_upstreams.contains(name) {
                continue;
            }
            let (uri, authorization) = metrics_uris[name].clone();
            scrapes.push(Scrape {
                upstream: name.into(),
                uri,
                authorization,
                targets: snapshot
                    .models
                    .iter()
                    .filter(|d| d.upstream == name)
                    .map(|d| {
                        (
                            d.upstream_model.clone(),
                            models[d.name.as_str()].load.clone(),
                        )
                    })
                    .collect(),
                failures: metrics.scrape_failures(name),
            });
        }

        // A direct model call and a fallback are the same thing: one attempt on
        // one model. Weights of routers do not reach this pool.
        let model_pools: HashMap<&str, Arc<Router>> = models
            .iter()
            .map(|(name, model)| (*name, Arc::new(Router::single(model.clone()))))
            .collect();

        // What a name can resolve to: the pool, and the router's name if any.
        let mut targets: HashMap<&str, (Arc<Router>, Option<Arc<str>>)> = model_pools
            .iter()
            .map(|(name, pool)| (*name, (pool.clone(), None)))
            .collect();
        for r in &snapshot.routers {
            let strategy = strategies
                .remove(r.name.as_str())
                .expect("configured strategy");
            let strategy_headers = snapshot::strategy_headers(r)?;
            let groups: Vec<(&str, &[RouterMember], u32)> = if r.traffic_split.is_empty() {
                vec![("", r.members.as_slice(), 1)]
            } else {
                r.traffic_split
                    .iter()
                    .map(|s| (s.name.as_str(), s.members.as_slice(), s.weight))
                    .collect()
            };
            let mut pools = Vec::with_capacity(groups.len());
            let mut split_cumulative = Vec::with_capacity(groups.len());
            let mut split_total = 0u64;
            let mut first_member = 0;
            for (split, members, split_weight) in groups {
                let mut resolved = Vec::new();
                let mut cumulative_weights = Vec::new();
                let mut total_weight = 0u64;
                for member in members {
                    resolved.push(models[member.model.as_str()].clone());
                    total_weight = total_weight
                        .checked_add(u64::from(member.weight))
                        .ok_or_else(|| invalid(format!("router {:?} weights overflow", r.name)))?;
                    cumulative_weights.push(total_weight);
                }
                if r.strategy == snapshot::DEFAULT_STRATEGY && total_weight == 0 {
                    return Err(invalid(format!(
                        "router {:?}{}{split} must have models with positive total weight",
                        r.name,
                        if split.is_empty() { "" } else { " split " },
                    )));
                }
                split_total += u64::from(split_weight);
                split_cumulative.push(split_total);
                let first = first_member;
                first_member += members.len();
                pools.push(Pool {
                    first_member: first,
                    models: resolved,
                    cumulative_weights,
                    total_weight,
                });
            }
            let sticky = StickyBy::try_from(r.sticky).expect("validated sticky");
            let router = Router {
                pools,
                split_cumulative,
                split_total,
                split_requests: r
                    .traffic_split
                    .iter()
                    .map(|s| metrics.split_handle(&r.name, &s.name))
                    .collect(),
                sticky,
                sticky_header: (sticky == StickyBy::Header).then(|| {
                    HeaderName::from_bytes(r.sticky_header.as_bytes())
                        .expect("validated sticky_header")
                }),
                strategy,
                strategy_headers,
                retry: r.retry.clone(),
                fallbacks: r
                    .fallbacks
                    .iter()
                    .map(|f| {
                        (
                            model_pools[f.as_str()].clone(),
                            metrics.fallback_handle(&r.name, f),
                        )
                    })
                    .collect(),
                budget_exhausted: metrics.budget_handle(&r.name),
            };
            targets.insert(
                r.name.as_str(),
                (Arc::new(router), Some(r.name.as_str().into())),
            );
        }

        let callable = |name: &str, alias: bool, target: &str| -> Arc<Callable> {
            let (pool, router) = &targets[target];
            let name: Arc<str> = name.into();
            Arc::new(Callable {
                label: metrics.label_id(&name),
                alias: alias.then(|| name.clone()),
                router: router.clone(),
                target: pool.clone(),
                circuit_open: pool.preregister(&name, metrics),
                name,
            })
        };
        let mut names = HashMap::with_capacity(targets.len() + snapshot.aliases.len());
        for name in targets.keys() {
            names.insert((*name).into(), callable(name, false, name));
        }
        for a in &snapshot.aliases {
            names.insert(a.name.as_str().into(), callable(&a.name, true, &a.target));
        }

        let mut teams: HashMap<&str, (LabelId, Arc<str>)> = HashMap::new();
        for team in &snapshot.teams {
            teams.insert(
                &team.id,
                (metrics.label_id(&team.id), team.id.as_str().into()),
            );
        }
        let empty: Arc<str> = "".into();
        let mut interned: HashSet<Arc<str>> = HashSet::from([empty.clone()]);
        let mut intern = |s: &str| -> Arc<str> {
            if let Some(shared) = interned.get(s) {
                return shared.clone();
            }
            let shared: Arc<str> = s.into();
            interned.insert(shared.clone());
            shared
        };
        let mut keys = HashMap::with_capacity(snapshot.api_keys.len());
        for key in &snapshot.api_keys {
            let policy = key.policy.as_ref().expect("validated policy");
            let (team_label, team_id) = match teams.get(key.team_id.as_str()) {
                Some((label, id)) => (*label, id.clone()),
                None => (0, empty.clone()),
            };
            let hash: [u8; 32] = key.key_hash.as_slice().try_into().expect("validated hash");
            keys.insert(
                hash,
                Arc::new(KeyContext {
                    key_id: key.key_id.as_str().into(),
                    team_id,
                    status: KeyStatus::try_from(key.status).expect("validated status"),
                    expires_at_ms: key.expires_at_unix_ms,
                    allow_all_models: policy.allow_all_models,
                    allowed_models: policy
                        .allowed_models
                        .iter()
                        .map(|m| m.as_str().into())
                        .collect(),
                    scopes: policy
                        .rate_limit_scopes
                        .iter()
                        .map(|s| (intern(&s.id), s.limit.expect("validated limit")))
                        .collect(),
                    team_label,
                }),
            );
        }

        let mut listed: Vec<Box<str>> = names.keys().cloned().collect();
        listed.sort();
        let listed = listed.into_boxed_slice();
        Ok(Self {
            version: snapshot.version,
            keys,
            names,
            models_json: models_json(listed.iter()),
            auth_mode,
            rejected: std::array::from_fn(|endpoint| metrics.handles("", "", endpoint, "")),
            runtime: runtime.clone(),
            listed,
            unknown_label: metrics.label_id("unknown"),
            probes,
            unprobed,
            scrapes,
            unbroken: models
                .values()
                .filter(|d| d.circuit.is_none())
                .map(|d| d.breaker.clone())
                .collect(),
        })
    }

    fn handles(
        &self,
        caller: &Caller,
        requested: (LabelId, &str),
        model: (LabelId, &str),
        endpoint: usize,
    ) -> Arc<RequestMetrics> {
        let (team, team_id) = match caller {
            Caller::Anonymous => (0, ""),
            Caller::Key(key) => (key.team_label, &*key.team_id),
        };
        self.runtime.metrics.cached_handles(
            HandleKey {
                team,
                requested: requested.0,
                model: model.0,
                endpoint: endpoint as u8,
            },
            team_id,
            requested.1,
            model.1,
        )
    }

    /// Handles for an authenticated request before (or without) a model:
    /// requested and model `""`, the caller's team. `endpoint` indexes `API_ENDPOINTS`.
    pub fn caller_handles(&self, caller: &Caller, endpoint: usize) -> Arc<RequestMetrics> {
        self.handles(caller, (0, ""), (0, ""), endpoint)
    }

    /// The one name lookup of a request.
    #[inline]
    pub fn lookup(&self, name: &str) -> Option<&Arc<Callable>> {
        self.names.get(name)
    }

    /// Handles for a request whose name does not exist.
    pub fn unknown(&self, caller: &Caller, endpoint: usize) -> Arc<RequestMetrics> {
        self.handles(caller, (self.unknown_label, "unknown"), (0, ""), endpoint)
    }

    /// Handles for a request rejected after its model field was parsed: the
    /// called name when it exists, `unknown` otherwise.
    pub fn rejected_model(
        &self,
        caller: &Caller,
        model: &str,
        endpoint: usize,
    ) -> Arc<RequestMetrics> {
        match self.lookup(model) {
            Some(called) => self.handles(caller, (called.label, &called.name), (0, ""), endpoint),
            None => self.unknown(caller, endpoint),
        }
    }

    /// Picks a model and reserves an in-flight slot on it.
    #[cfg(test)]
    pub fn acquire(&self, called: &Callable, endpoint: usize, caller: &Caller) -> InFlight {
        self.acquire_initial(
            called,
            endpoint,
            caller,
            &HeaderMap::new(),
            &mut Skipped::default(),
        )
        .expect("test router has an available model")
        .0
    }

    /// Chooses the request's split (counted once), then its own split, the
    /// other splits, the fallbacks, and finally fails open on its own router.
    /// Returns the guard, the fallback counter if one was used, and the split.
    pub fn acquire_initial<'a>(
        &self,
        called: &'a Callable,
        endpoint: usize,
        caller: &Caller,
        headers: &HeaderMap,
        skipped: &mut Skipped,
    ) -> Option<(InFlight, Option<&'a Counter>, usize)> {
        let router = &*called.target;
        let split = router.split_for(headers, caller);
        if let Some(counter) = router.split_requests.get(split) {
            counter.inc();
        }
        if let Some(guard) = self.acquire_from(
            called, router, split, endpoint, caller, headers, None, false, skipped,
        ) {
            return Some((guard, None, split));
        }
        for (fallback, counter) in &router.fallbacks {
            if let Some(guard) = self.acquire_from(
                called, fallback, 0, endpoint, caller, headers, None, false, skipped,
            ) {
                return Some((guard, Some(counter), split));
            }
        }
        self.acquire_from(
            called, router, split, endpoint, caller, headers, None, true, skipped,
        )
        .map(|guard| (guard, None, split))
    }

    /// First exhaust untried models (the chosen split, then the other
    /// splits), then fallbacks in declaration order. Only when every eligible
    /// candidate is unhealthy/open do we fail open.
    #[allow(clippy::too_many_arguments)]
    pub fn retry<'a>(
        &self,
        called: &'a Callable,
        endpoint: usize,
        caller: &Caller,
        headers: &HeaderMap,
        tried: &[LabelId],
        split: usize,
        skipped: &mut Skipped,
    ) -> Option<(InFlight, Option<&'a Counter>)> {
        let router = &*called.target;
        for strict in [true, false] {
            for excluded in [tried, &[][..]] {
                if let Some(guard) = self.acquire_from(
                    called,
                    router,
                    split,
                    endpoint,
                    caller,
                    headers,
                    Some(excluded),
                    !strict,
                    skipped,
                ) {
                    return Some((guard, None));
                }
                for (fallback, counter) in &router.fallbacks {
                    if let Some(guard) = self.acquire_from(
                        called,
                        fallback,
                        0,
                        endpoint,
                        caller,
                        headers,
                        Some(excluded),
                        !strict,
                        skipped,
                    ) {
                        return Some((guard, Some(counter)));
                    }
                }
            }
        }
        None
    }

    #[allow(clippy::too_many_arguments)]
    fn acquire_from(
        &self,
        root: &Callable,
        router: &Router,
        split: usize,
        endpoint: usize,
        caller: &Caller,
        headers: &HeaderMap,
        excluded: Option<&[LabelId]>,
        fail_open: bool,
        skipped: &mut Skipped,
    ) -> Option<InFlight> {
        let now = clock::mono_ms();
        for d in router.models() {
            if d.circuit.is_some() && !d.breaker.available(now) && skipped.first(d.label) {
                if let Some((_, counter)) = root.circuit_open.iter().find(|(l, _)| *l == d.label) {
                    counter.inc();
                }
            }
        }
        let (team_id, key_id) = match caller {
            Caller::Key(key) => (key.team_id.as_ref(), key.key_id.as_ref()),
            _ => ("", ""),
        };
        let lookup = |name: &str| headers.get(name).map(HeaderValue::as_bytes);
        let req = RequestInfo::new(
            &root.name,
            team_id,
            key_id,
            None,
            &router.strategy_headers,
            &lookup,
        );
        let pick = |strict: bool| {
            router.pool_order(split).find_map(|pool| {
                pool.pick(&router.strategy, &req, excluded, now, strict)
                    .map(|i| &pool.models[i])
            })
        };
        let model = pick(true)
            .or_else(|| fail_open.then(|| pick(false)).flatten())?
            .clone();
        let trial = if model.circuit.is_some() {
            match model.breaker.reserve(now) {
                Some(trial) => trial,
                None if fail_open && !model.breaker.trial_in_progress() => false,
                None => return None,
            }
        } else {
            false
        };
        model.in_flight.fetch_add(1, Ordering::Relaxed);
        let metrics = self.handles(
            caller,
            (root.label, &root.name),
            (model.label, &model.name),
            endpoint,
        );
        Some(InFlight {
            model,
            metrics,
            trial,
            finished: false,
        })
    }

    /// Returns how many probe and scrape tasks were (re)started.
    pub fn activate_health(&self) -> usize {
        for breaker in &self.unbroken {
            breaker.reset();
        }
        for (healthy, gauge) in &self.unprobed {
            healthy.store(true, Ordering::Release);
            gauge.set(1);
        }
        for probe in &self.probes {
            probe
                .gauge
                .set(i64::from(probe.healthy.load(Ordering::Acquire)));
        }
        let probes = self
            .runtime
            .health
            .configure(&self.probes, &self.runtime.client);
        // vLLM load scrapers follow the same accepted-snapshot lifecycle.
        probes
            + self
                .runtime
                .balance
                .configure(&self.scrapes, &self.runtime.client)
    }

    /// `/v1/models` body: every name, or only those the caller's key allows.
    pub fn models_for(&self, caller: &Caller) -> Bytes {
        match caller {
            Caller::Key(key) if !key.allow_all_models => models_json(
                self.listed
                    .iter()
                    .filter(|m| key.allowed_models.contains(m.as_ref())),
            ),
            _ => self.models_json.clone(),
        }
    }
}

/// Every model a router can use: its splits' or its own list.
fn router_model_names(
    r: &lumewarp_proto::config::v1::Router,
) -> Box<dyn Iterator<Item = &str> + '_> {
    if r.traffic_split.is_empty() {
        Box::new(r.members.iter().map(|m| m.model.as_str()))
    } else {
        Box::new(
            r.traffic_split
                .iter()
                .flat_map(|s| s.members.iter().map(|m| m.model.as_str())),
        )
    }
}

/// Every member across the router's splits, in the order `first_member` counts
/// them, with its settings as a JSON object.
fn router_members(
    r: &lumewarp_proto::config::v1::Router,
) -> Result<(Vec<MemberInfo<'_>>, Vec<serde_json::Value>), ApplyError> {
    let groups: Vec<(Option<&str>, &[RouterMember])> = if r.traffic_split.is_empty() {
        vec![(None, r.members.as_slice())]
    } else {
        r.traffic_split
            .iter()
            .map(|s| (Some(s.name.as_str()), s.members.as_slice()))
            .collect()
    };
    let mut infos = Vec::new();
    let mut params = Vec::new();
    for (split, members) in groups {
        for member in members {
            infos.push(MemberInfo {
                model: &member.model,
                weight: member.weight,
                split,
            });
            params.push(if member.params_json.is_empty() {
                serde_json::json!({})
            } else {
                serde_json::from_str(&member.params_json).map_err(|_| {
                    invalid(format!("router {:?} has invalid member params", r.name))
                })?
            });
        }
    }
    Ok((infos, params))
}

/// Explicit `metrics_url`, or `base_url` minus a trailing `/v1` plus
/// `/metrics`. `None` when an explicit URL is not plain http(s).
fn metrics_uri(base: &Uri, explicit: &str) -> Option<Uri> {
    if !explicit.is_empty() {
        let uri: Uri = explicit.parse().ok()?;
        let plain = matches!(uri.scheme_str(), Some("http" | "https"))
            && uri.authority().is_some_and(|a| !a.as_str().contains('@'));
        return plain.then_some(uri);
    }
    let path = base.path().trim_end_matches('/');
    let path = path.strip_suffix("/v1").unwrap_or(path);
    let mut parts = base.clone().into_parts();
    parts.path_and_query = Some(format!("{path}/metrics").parse().ok()?);
    Uri::from_parts(parts).ok()
}

fn models_json<'a>(models: impl Iterator<Item = &'a Box<str>>) -> Bytes {
    let data: Vec<_> = models
        .map(|model| {
            serde_json::json!({
                "id": model.as_ref(), "object": "model", "created": 0, "owned_by": "lumewarp",
            })
        })
        .collect();
    Bytes::from(
        serde_json::to_vec(&serde_json::json!({"object": "list", "data": data}))
            .expect("JSON values serialize"),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::snapshot::tests::{base, key, member, model, router, upstream};
    use lumewarp_proto::config::v1 as pb;

    fn empty_request() -> RequestInfo<'static> {
        RequestInfo::new("public", "", "", None, &[], &|_| None)
    }

    /// Declared in their own modules: one `register!` per module.
    mod header_pin {
        use lumewarp_router_api::{
            Candidates, ConfigError, Configure, Needs, NoParams, RequestInfo, RngCore, RouterInfo,
            Strategy,
        };
        use schemars::JsonSchema;
        use serde::Deserialize;

        pub struct HeaderPin;

        #[derive(Deserialize, JsonSchema)]
        #[serde(deny_unknown_fields)]
        pub struct Params {
            pub reject: Option<String>,
        }

        impl Configure<Params, NoParams> for HeaderPin {
            fn configure(
                _: &RouterInfo<'_>,
                params: Params,
                _: &[NoParams],
            ) -> Result<Self, ConfigError> {
                if params.reject.is_some() {
                    return Err(ConfigError("private params".into()));
                }
                Ok(Self)
            }
        }

        impl Strategy for HeaderPin {
            fn pick(
                &self,
                req: &RequestInfo<'_>,
                c: &Candidates<'_>,
                _: &mut dyn RngCore,
            ) -> Option<usize> {
                let pin = req.header("x-pin");
                c.iter()
                    .find(|d| Some(d.name().as_bytes()) == pin)
                    .or_else(|| c.iter().next())
                    .map(|d| d.index)
            }
        }

        lumewarp_router_api::register! {
            name: "header_pin",
            title: "Header pin",
            description: "Test strategy.",
            uses_weight: false,
            needs: Needs { latency: true, vllm_load: true },
            params: Params,
            member_params: NoParams,
            strategy: HeaderPin,
        }
    }

    mod ranked {
        use lumewarp_router_api::{
            Candidates, ConfigError, Configure, Needs, NoParams, RequestInfo, RngCore, RouterInfo,
            Strategy,
        };
        use schemars::JsonSchema;
        use serde::Deserialize;

        /// Lowest rank wins; ranks are looked up by the stable member index.
        pub struct Ranked(Vec<u32>);

        #[derive(Deserialize, JsonSchema)]
        #[serde(deny_unknown_fields)]
        pub struct Member {
            pub rank: u32,
        }

        impl Configure<NoParams, Member> for Ranked {
            fn configure(
                router: &RouterInfo<'_>,
                _: NoParams,
                members: &[Member],
            ) -> Result<Self, ConfigError> {
                assert_eq!(router.members.len(), members.len());
                Ok(Self(members.iter().map(|m| m.rank).collect()))
            }
        }

        impl Strategy for Ranked {
            fn pick(
                &self,
                _: &RequestInfo<'_>,
                c: &Candidates<'_>,
                _: &mut dyn RngCore,
            ) -> Option<usize> {
                c.iter()
                    .min_by_key(|d| self.0[d.member_index()])
                    .map(|d| d.index)
            }
        }

        lumewarp_router_api::register! {
            name: "ranked",
            title: "Ranked",
            description: "Test strategy.",
            uses_weight: false,
            needs: Needs::default(),
            params: NoParams,
            member_params: Member,
            strategy: Ranked,
        }
    }

    fn with_member_params(member: &mut pb::RouterMember, params: &str) {
        member.params_json = params.into();
    }

    #[test]
    fn member_index_spans_splits_and_member_params_reach_configure() {
        let mut s = split_snapshot(pb::StickyBy::None, "ranked");
        s.required_capabilities
            .retain(|c| !c.starts_with("strategy:"));
        s.required_capabilities.push("strategy:ranked".into());
        // The same models in both splits: only the member index tells them apart.
        let r = &mut s.routers[0];
        r.traffic_split[0].members = vec![member("a", 1), member("b", 1)];
        r.traffic_split[1].members = vec![member("a", 1), member("b", 1)];
        let splits = &mut r.traffic_split;
        with_member_params(&mut splits[0].members[0], r#"{"rank":2}"#);
        with_member_params(&mut splits[0].members[1], r#"{"rank":1}"#);
        with_member_params(&mut splits[1].members[0], r#"{"rank":1}"#);
        with_member_params(&mut splits[1].members[1], r#"{"rank":2}"#);
        let (first, second) = (
            splits[0].members[1].model.clone(),
            splits[1].members[0].model.clone(),
        );
        assert_ne!(first, second);
        let table = compile(&s);
        let router = &table.names["public"];
        let offsets: Vec<_> = router.pools.iter().map(|p| p.first_member).collect();
        assert_eq!(offsets, [0, 2]);
        let req = empty_request();
        for (pool, want) in router.pools.iter().zip([&first, &second]) {
            let picked = pool
                .pick(&router.strategy, &req, None, clock::mono_ms(), true)
                .unwrap();
            assert_eq!(&pool.models[picked].name, want);
        }

        // A member the strategy's struct cannot parse is refused, without its text.
        with_member_params(
            &mut s.routers[0].traffic_split[1].members[0],
            r#"{"rank":"private"}"#,
        );
        let err = RoutingTable::compile(&s, AuthMode::Open, &Runtime::new())
            .err()
            .unwrap();
        assert_eq!(err.code, pb::NackCode::InvalidConfig);
        assert!(!err.reason.contains("private"));
    }

    #[test]
    fn headers_are_removed_before_configure_and_builtins_refuse_member_params() {
        // `Params` of header_pin has deny_unknown_fields and no `headers` field.
        let mut s = snapshot("header_pin");
        s.routers[0].params_json = r#"{"headers":["x-pin"]}"#.into();
        s.required_capabilities = vec!["strategy:header_pin".into()];
        let table = compile(&s);
        assert_eq!(table.names["public"].strategy_headers, ["x-pin"]);
        s.routers[0].params_json = r#"{"headers":["x-pin"],"other":1}"#.into();
        assert!(RoutingTable::compile(&s, AuthMode::Open, &Runtime::new()).is_err());

        let mut s = snapshot("least_in_flight");
        s.required_capabilities = vec!["strategy:least_in_flight".into()];
        s.routers[0].members[0].params_json = r#"{"zone":"a"}"#.into();
        let err = RoutingTable::compile(&s, AuthMode::Open, &Runtime::new())
            .err()
            .unwrap();
        assert_eq!(err.code, pb::NackCode::InvalidConfig);
    }

    #[test]
    fn registered_descriptors_include_builtins_and_declare_needs() {
        let descriptors = lumewarp_router_api::descriptors();
        let needs = |name: &str| {
            descriptors
                .iter()
                .find(|d| d.name == name)
                .unwrap()
                .needs
                .clone()
        };
        assert_eq!(needs("vllm_load"), ["vllm_load"]);
        assert_eq!(needs("lowest_latency"), ["latency"]);
        assert!(needs("weighted").is_empty());
        assert_eq!(needs("header_pin"), ["latency", "vllm_load"]);
        let weighted = descriptors.iter().find(|d| d.name == "weighted").unwrap();
        assert!(weighted.uses_weight);
        for name in snapshot::SUPPORTED_CAPABILITIES
            .iter()
            .filter_map(|c| c.strip_prefix("strategy:"))
        {
            let builtin = lumewarp_router_api::lookup(name).unwrap();
            assert!(builtin.is_some(), "{name} is not registered");
            assert!(snapshot::builtin_strategy(name).is_some());
        }
    }

    #[test]
    fn custom_strategy_snapshot_apply_headers_needs_and_allocations() {
        let mut s = snapshot("header_pin");
        s.upstreams.push(upstream("second"));
        s.models[1].upstream = "second".into();
        s.routers[0].params_json = r#"{"headers":["x-pin"]}"#.into();
        s.required_capabilities = vec!["strategy:header_pin".into()];
        assert!(snapshot::supported_capabilities().contains(&"strategy:header_pin".into()));
        assert!(lumewarp_router_api::lookup("header_pin").unwrap().is_some());
        assert!(lumewarp_router_api::lookup("missing").unwrap().is_none());
        let update = pb::ConfigUpdate {
            update: Some(pb::config_update::Update::Full(s.clone())),
            ..Default::default()
        };
        let applied = snapshot::apply(&Snapshot::default(), update).unwrap();
        let routing_table = compile(&applied);
        let router = &routing_table.names["public"];
        assert!(router.pools[0].models.iter().all(|d| d.track_latency));
        assert_eq!(routing_table.scrapes.len(), 2);
        let mut headers = HeaderMap::new();
        headers.insert("x-pin", HeaderValue::from_static("b"));
        headers.insert("authorization", HeaderValue::from_static("secret"));
        let pick = || {
            routing_table
                .acquire_initial(
                    router,
                    0,
                    &Caller::Anonymous,
                    &headers,
                    &mut Skipped::default(),
                )
                .unwrap()
                .0
        };
        drop(pick()); // warm metric handles
        let (allocations, guard) = crate::test_alloc::count(pick);
        assert_eq!(allocations, (0, 0));
        assert_eq!(guard.model.name, "b");
        drop(guard);
        router.pools[0].models[1]
            .healthy
            .store(false, Ordering::Release);
        assert_eq!(
            pick().model.name,
            "a",
            "custom sees only eligible candidates"
        );
        s.routers[0].params_json = r#"{"reject":"private"}"#.into();
        let err = RoutingTable::compile(&s, AuthMode::Open, &Runtime::new())
            .err()
            .unwrap();
        assert_eq!(err.code, pb::NackCode::InvalidConfig);
        assert!(!err.reason.contains("private"));
    }

    #[test]
    fn custom_strategy_unknown_and_missing_capability_nacks() {
        let mut s = snapshot("not_compiled");
        s.required_capabilities = vec!["strategy:not_compiled".into()];
        assert_eq!(
            RoutingTable::compile(&s, AuthMode::Open, &Runtime::new())
                .err()
                .unwrap()
                .code,
            pb::NackCode::UnsupportedFeature
        );
        s.routers[0].strategy = "header_pin".into();
        s.required_capabilities.clear();
        assert_eq!(
            RoutingTable::compile(&s, AuthMode::Open, &Runtime::new())
                .err()
                .unwrap()
                .code,
            pb::NackCode::InvalidConfig
        );
    }

    #[test]
    fn custom_params_nesting_budget() {
        let mut r = pb::Router {
            params_json: format!("{}{{}}{}", r#"{"x":"#.repeat(126), "}".repeat(126)),
            ..Default::default()
        };
        assert!(snapshot::strategy_params(&r).is_ok());
        r.params_json = format!(r#"{{"x":{}}}"#, r.params_json);
        assert!(snapshot::strategy_params(&r).is_err());
    }

    const BUILTINS: [&str; 4] = ["weighted", "least_in_flight", "lowest_latency", "vllm_load"];

    fn snapshot(strategy: &str) -> Snapshot {
        let mut public = router("public", &[]);
        public.members = vec![member("a", 1), member("b", 3)];
        public.strategy = strategy.into();
        Snapshot {
            upstreams: vec![upstream("u")],
            models: vec![model("a", "u"), model("b", "u")],
            routers: vec![public],
            required_capabilities: vec![format!("strategy:{strategy}")],
            ..Default::default()
        }
    }

    fn compile(s: &Snapshot) -> RoutingTable {
        RoutingTable::compile(s, AuthMode::Open, &Runtime::new()).unwrap()
    }

    /// Golden traces recorded before introducing the strategy interface. Covers
    /// weights, ties, load, stale load, exclusions and strict/fail-open eligibility.
    #[test]
    fn pinned_strategy_traces() {
        for strategy in BUILTINS {
            let routing_table = compile(&snapshot(strategy));
            let pool = &routing_table.names["public"].pools[0];
            let mut trace = 0u64;
            fastrand::seed(0x1234);
            for step in 0..4096 {
                let now = 10_000;
                pool.models[0].in_flight.store(step % 3, Ordering::Relaxed);
                pool.models[1].in_flight.store(step % 5, Ordering::Relaxed);
                pool.models[0].latency.success(Duration::from_micros(10));
                pool.models[1].latency.success(Duration::from_micros(20));
                pool.models[0].load.set((step % 7) as f64, 0.45, now);
                pool.models[1].load.set((step % 4) as f64, 0.10, now);
                if step % 11 == 0 {
                    pool.models[0].load.fail();
                }
                pool.models[0]
                    .healthy
                    .store(step % 13 != 0, Ordering::Release);
                let excluded = [pool.models[1].label];
                let picked = pool.pick(
                    &routing_table.names["public"].strategy,
                    &empty_request(),
                    (step % 17 == 0).then_some(excluded.as_slice()),
                    now,
                    step % 19 != 0,
                );
                trace = trace
                    .wrapping_mul(31)
                    .wrapping_add(picked.map_or(3, |i| i as u64));
            }
            let expected = match strategy {
                "weighted" => 18345758516063820545,
                "least_in_flight" => 425016045968590066,
                "lowest_latency" => 8882522843359636763,
                "vllm_load" => 18199678790619655580,
                _ => unreachable!(),
            };
            assert_eq!(trace, expected, "{strategy:?}");
        }
    }

    /// Run explicitly in release mode; measures selection only, without metrics,
    /// compilation or upstream traffic. Keep the same harness across the refactor.
    #[test]
    #[ignore = "pick microbenchmark"]
    fn pick_timing() {
        for strategy in BUILTINS {
            let routing_table = compile(&snapshot(strategy));
            let pool = &routing_table.names["public"].pools[0];
            fastrand::seed(42);
            let start = std::time::Instant::now();
            for _ in 0..1_000_000 {
                std::hint::black_box(pool.pick(
                    &routing_table.names["public"].strategy,
                    &empty_request(),
                    None,
                    10_000,
                    true,
                ));
            }
            println!(
                "PICK {strategy:?}: {:.2} ns/pick (1000000 iterations, two models)",
                start.elapsed().as_nanos() as f64 / 1_000_000.0
            );
        }
    }

    #[test]
    fn weighted_distribution() {
        let routing_table = compile(&snapshot("weighted"));
        let router = &routing_table.names["public"];
        let mut a = 0;
        // Seeded draws make this deterministic while exercising cumulative selection.
        let mut rng = fastrand::Rng::with_seed(42);
        for _ in 0..20000 {
            let pool = &router.pools[0];
            a += usize::from(pool.weighted_index(rng.u64(0..pool.total_weight)) == 0);
        }
        assert!((4500..5500).contains(&a), "{a}");
    }

    #[test]
    fn least_in_flight_and_guard_release() {
        let routing_table = compile(&snapshot("least_in_flight"));
        let router = &routing_table.names["public"];
        let anonymous = Caller::Anonymous;
        let first = routing_table.acquire(router, 0, &anonymous);
        let first_name = first.model.name.clone();
        let second = routing_table.acquire(router, 0, &anonymous);
        assert_ne!(second.model.name, first_name);
        drop(first);
        assert_eq!(
            routing_table.acquire(router, 0, &anonymous).model.name,
            first_name
        );
        drop(second);
        assert!(router.pools[0]
            .models
            .iter()
            .all(|d| d.in_flight.load(Ordering::Relaxed) == 0));
    }

    #[test]
    fn least_in_flight_ties_spread() {
        let routing_table = compile(&snapshot("least_in_flight"));
        let router = &routing_table.names["public"];
        let mut a = 0;
        for _ in 0..10000 {
            a += usize::from(
                routing_table
                    .acquire(router, 0, &Caller::Anonymous)
                    .model
                    .name
                    == "a",
            );
        }
        assert!((4500..5500).contains(&a), "ties selected a {a} times");
    }

    #[test]
    fn in_flight_and_metrics_survive_swaps() {
        let runtime = Runtime::new();
        let s = snapshot("least_in_flight");
        let old = RoutingTable::compile(&s, AuthMode::Open, &runtime).unwrap();
        let guard = old.acquire(&old.names["public"], 0, &Caller::Anonymous);
        let busy = guard.model.name.clone();
        let mut next = s.clone();
        next.version = 2;
        let new = RoutingTable::compile(&next, AuthMode::Open, &runtime).unwrap();
        drop(old);
        // The stream still running on the old snapshot steers the new one away.
        for _ in 0..100 {
            assert_ne!(
                new.acquire(&new.names["public"], 0, &Caller::Anonymous)
                    .model
                    .name,
                busy
            );
        }
        let text = runtime.encode_metrics().unwrap();
        assert!(text.contains(&format!(
            "lumewarp_in_flight_requests{{model=\"{busy}\"}} 1"
        )));
        drop(guard);
        let text = runtime.encode_metrics().unwrap();
        assert!(text.contains(&format!(
            "lumewarp_in_flight_requests{{model=\"{busy}\"}} 0"
        )));
        // Counters are not reset by recompiling: same label set, same counter.
        crate::metrics::tests::finish_with(&new.rejected[0], 404);
        let third = RoutingTable::compile(&next, AuthMode::Open, &runtime).unwrap();
        drop(new);
        crate::metrics::tests::finish_with(&third.rejected[0], 404);
        assert!(runtime.encode_metrics().unwrap().contains(
            "{requested=\"\",endpoint=\"chat_completions\",model=\"\",team=\"\",status=\"404\"} 2"
        ));
    }

    #[test]
    fn breaker_and_health_survive_swaps_without_hot_path_allocations() {
        let runtime = Runtime::new();
        let mut s = snapshot("weighted");
        s.required_capabilities
            .extend(["circuit_breaker".into(), "retry".into()]);
        s.upstreams[0].circuit_breaker = Some(CircuitBreaker {
            failure_threshold: 1,
            open_ms: 30_000,
        });
        s.routers[0].retry = Some(RetryPolicy::default());
        let old = RoutingTable::compile(&s, AuthMode::Open, &runtime).unwrap();
        let mut first = old.acquire(&old.names["public"], 0, &Caller::Anonymous);
        let name = first.model.name.clone();
        let breaker = first.model.breaker.clone();
        first.complete(true);
        drop(first);
        let next = RoutingTable::compile(&s, AuthMode::Open, &runtime).unwrap();
        let guard = next.acquire(&next.names["public"], 0, &Caller::Anonymous);
        assert_ne!(guard.model.name, name);
        assert!(Arc::ptr_eq(
            &breaker,
            &next.names["public"].pools[0]
                .models
                .iter()
                .find(|d| d.name == name)
                .unwrap()
                .breaker
        ));
        drop(guard);
        let ((allocations, bytes), guard) = crate::test_alloc::count(|| {
            runtime.retry_budget.request();
            next.acquire(&next.names["public"], 0, &Caller::Anonymous)
        });
        assert_eq!((allocations, bytes), (0, 0));
        drop(guard);
        // The only remaining model is also down: routing fails open
        // rather than rejecting all traffic on passive health alone.
        next.names["public"].pools[0].models[0]
            .healthy
            .store(false, Ordering::Release);
        assert!(next
            .acquire_initial(
                &next.names["public"],
                0,
                &Caller::Anonymous,
                &HeaderMap::new(),
                &mut Skipped::default()
            )
            .is_some());
    }

    #[test]
    fn missing_api_key_env_is_load_error() {
        let mut s = snapshot("weighted");
        s.upstreams[0].api_key_ref = "LUMEWARP_TEST_MISSING_KEY_8B3F590E".into();
        let err = RoutingTable::compile(&s, AuthMode::Open, &Runtime::new())
            .err()
            .unwrap();
        assert_eq!(err.code, pb::NackCode::InvalidConfig);
        assert!(err.reason.contains("api_key_env"));
    }

    #[test]
    fn rejects_invalid_snapshots_and_bad_urls() {
        let mut s = snapshot("weighted");
        s.upstreams[0].base_url = "ftp://x/v1".into();
        assert!(RoutingTable::compile(&s, AuthMode::Open, &Runtime::new()).is_err());
        let mut s = snapshot("weighted");
        s.routers[0].members[0].weight = 0;
        s.routers[0].members[1].weight = 0;
        assert!(RoutingTable::compile(&s, AuthMode::Open, &Runtime::new()).is_err());
        let mut s = snapshot("weighted");
        s.routers[0].strategy.clear();
        assert!(RoutingTable::compile(&s, AuthMode::Open, &Runtime::new()).is_err());
    }

    #[test]
    fn keys_and_filtered_models() {
        let mut s = base();
        s.api_keys.push(pb::ApiKey {
            policy: Some(pb::EffectivePolicy {
                allow_all_models: true,
                ..Default::default()
            }),
            ..key("k3", "s3", "t1", &[])
        });
        let routing_table =
            RoutingTable::compile(&s, AuthMode::RequireKey, &Runtime::new()).unwrap();
        assert_eq!(routing_table.keys.len(), 3);
        let hash = |secret: &str| -> [u8; 32] {
            <sha2::Sha256 as sha2::Digest>::digest(secret.as_bytes()).into()
        };
        let k1 = routing_table.keys[&hash("s1")].clone();
        assert_eq!(&*k1.team_id, "t1");
        assert_ne!(k1.team_label, 0);
        let k2 = routing_table.keys[&hash("s2")].clone();
        assert_eq!(k2.team_label, 0);
        let ids = |body: Bytes| -> Vec<String> {
            let v: serde_json::Value = serde_json::from_slice(&body).unwrap();
            v["data"]
                .as_array()
                .unwrap()
                .iter()
                .map(|m| m["id"].as_str().unwrap().to_owned())
                .collect()
        };
        // Every name, whatever its kind, sorted.
        assert_eq!(ids(routing_table.models_for(&Caller::Key(k1))), ["m1"]);
        assert!(ids(routing_table.models_for(&Caller::Key(k2))).is_empty());
        let k3 = routing_table.keys[&hash("s3")].clone();
        assert_eq!(
            ids(routing_table.models_for(&Caller::Key(k3))),
            ["a", "b", "m1", "m2"]
        );
        assert_eq!(
            ids(routing_table.models_for(&Caller::Anonymous)),
            ["a", "b", "m1", "m2"]
        );
    }

    #[test]
    fn team_handles_are_lazy() {
        let runtime = Runtime::new();
        let routing_table = RoutingTable::compile(&base(), AuthMode::RequireKey, &runtime).unwrap();
        assert!(!runtime.encode_metrics().unwrap().contains("team=\"t1\""));
        let k1 = Caller::Key(
            routing_table.keys[&<[u8; 32]>::from(<sha2::Sha256 as sha2::Digest>::digest(b"s1"))]
                .clone(),
        );
        let guard = routing_table.acquire(&routing_table.names["m1"], 0, &k1);
        crate::metrics::tests::finish_with(&guard.metrics, 200);
        let text = runtime.encode_metrics().unwrap();
        assert!(text.contains(
            "lumewarp_requests_total{requested=\"m1\",endpoint=\"chat_completions\",model=\"a\",team=\"t1\",status=\"200\"} 1"
        ));
        assert!(!text.contains("team=\"t2\""));
    }

    fn hash(secret: &str) -> [u8; 32] {
        <sha2::Sha256 as sha2::Digest>::digest(secret.as_bytes()).into()
    }

    /// `n` aliases on one model; team t1's key may only call `m0`.
    fn many_routers(n: usize) -> Snapshot {
        Snapshot {
            upstreams: vec![upstream("u")],
            models: vec![model("a", "u")],
            routers: (0..n).map(|i| router(&format!("m{i}"), &["a"])).collect(),
            teams: vec![crate::snapshot::tests::team("t1")],
            api_keys: vec![key("k1", "s1", "t1", &["m0"])],
            required_capabilities: vec!["strategy:weighted".into()],
            ..Default::default()
        }
    }

    #[test]
    fn handle_cache_is_sparse_and_survives_swaps() {
        use crate::test_alloc::count;
        let mut first_use = Vec::new();
        for n in [1, 2000] {
            let runtime = Runtime::new();
            let s = many_routers(n);
            let routing_table = RoutingTable::compile(&s, AuthMode::RequireKey, &runtime).unwrap();
            let caller = Caller::Key(routing_table.keys[&hash("s1")].clone());
            // Warm this thread's lock-free map state with an unrelated entry.
            drop(routing_table.acquire(&routing_table.names["m0"], 2, &Caller::Anonymous));
            // Create the registry series up front: their cost depends on the
            // registry's size (map resizes), not on the handle cache, which is
            // what this test isolates.
            drop(runtime.metrics.handles("m0", "a", 0, "t1"));
            let (first, guard) =
                count(|| routing_table.acquire(&routing_table.names["m0"], 0, &caller));
            drop(guard);
            first_use.push(first);
            let (warm, guard) =
                count(|| routing_table.acquire(&routing_table.names["m0"], 0, &caller));
            drop(guard);
            assert_eq!(warm, (0, 0), "warm acquire allocated ({n} routers)");
            let (cold, handles) = count(|| routing_table.rejected_model(&caller, "m0", 0));
            drop(handles);
            assert!(cold.0 > 0, "first use of another label set creates it");
            // Key-only swap: already-used pairs need no allocation at all.
            let mut next = s.clone();
            next.version = 2;
            next.api_keys.push(key("k2", "s2", "t1", &["m0"]));
            let swapped = RoutingTable::compile(&next, AuthMode::RequireKey, &runtime).unwrap();
            for secret in ["s1", "s2"] {
                let caller = Caller::Key(swapped.keys[&hash(secret)].clone());
                let (after, guard) = count(|| swapped.acquire(&swapped.names["m0"], 0, &caller));
                drop(guard);
                assert_eq!(after, (0, 0), "acquire after swap allocated ({n} routers)");
                let (after, handles) = count(|| swapped.rejected_model(&caller, "m0", 0));
                drop(handles);
                assert_eq!(after, (0, 0));
            }
        }
        assert!(first_use[0].0 > 0);
        // Same allocations and bytes for 1 and 2000 routers: nothing per router.
        assert_eq!(
            first_use[0], first_use[1],
            "first use (allocations, bytes): 1 router vs 2000 routers"
        );
    }

    #[test]
    fn models_keep_their_metering_context() {
        let runtime = Runtime::new();
        let mut s = snapshot("weighted");
        let mut passthrough = upstream("p");
        passthrough.stream_usage_mode = pb::StreamUsageMode::Passthrough as i32;
        s.upstreams.push(passthrough);
        s.models[0].price = Some(pb::Price {
            input_nano_usd_per_mtok: 150_000_000,
            output_nano_usd_per_mtok: 600_000_000,
        });
        s.models[1].upstream = "p".into();
        s.routers = vec![router("paid", &["a"]), router("free", &["b"])];
        let routing_table = RoutingTable::compile(&s, AuthMode::Open, &runtime).unwrap();
        let paid = routing_table.acquire(&routing_table.names["paid"], 0, &Caller::Anonymous);
        assert_eq!(paid.model.price, s.models[0].price);
        assert_eq!(
            paid.model.stream_usage_mode,
            pb::StreamUsageMode::IncludeUsage,
            "UNSPECIFIED resolves to INCLUDE_USAGE"
        );
        let other = routing_table.acquire(&routing_table.names["free"], 0, &Caller::Anonymous);
        assert_eq!(other.model.price, None);
        assert_eq!(
            other.model.stream_usage_mode,
            pb::StreamUsageMode::Passthrough
        );
        // A new price applies to new requests only; the in-flight guard keeps
        // the price its request started with.
        let mut next = s.clone();
        next.version = 2;
        next.models[0].price = Some(pb::Price {
            input_nano_usd_per_mtok: 1,
            output_nano_usd_per_mtok: 2,
        });
        let swapped = RoutingTable::compile(&next, AuthMode::Open, &runtime).unwrap();
        drop(routing_table);
        assert_eq!(paid.model.price, s.models[0].price);
        let fresh = swapped.acquire(&swapped.names["paid"], 0, &Caller::Anonymous);
        assert_eq!(fresh.model.price, next.models[0].price);
        assert_eq!(fresh.model.in_flight.load(Ordering::Relaxed), 2);
    }

    fn split_snapshot(sticky: pb::StickyBy, strategy: &str) -> Snapshot {
        let mut s = snapshot(strategy);
        s.models.push(model("c", "u"));
        let r = &mut s.routers[0];
        r.members.clear();
        r.traffic_split = vec![
            crate::snapshot::tests::split("stable", &["a"], 9),
            crate::snapshot::tests::split("canary", &["b", "c"], 1),
        ];
        r.sticky = sticky as i32;
        if sticky == pb::StickyBy::Header {
            r.sticky_header = "x-session".into();
        }
        s.required_capabilities.push("traffic_split".into());
        s
    }

    fn headers(session: &str) -> HeaderMap {
        let mut h = HeaderMap::new();
        h.insert("x-session", HeaderValue::from_str(session).unwrap());
        h
    }

    #[test]
    fn ten_percent_split_over_10k_requests() {
        fastrand::seed(7);
        let runtime = Runtime::new();
        let routing_table = RoutingTable::compile(
            &split_snapshot(pb::StickyBy::None, "weighted"),
            AuthMode::Open,
            &runtime,
        )
        .unwrap();
        let router = &routing_table.names["public"];
        let mut canary = 0;
        for _ in 0..10_000 {
            let (guard, _, split) = routing_table
                .acquire_initial(
                    router,
                    0,
                    &Caller::Anonymous,
                    &HeaderMap::new(),
                    &mut Skipped::default(),
                )
                .unwrap();
            assert_eq!(split == 1, guard.model.name != "a");
            canary += usize::from(split == 1);
        }
        assert!((900..=1100).contains(&canary), "canary got {canary}");
        let text = runtime.encode_metrics().unwrap();
        assert!(text.contains(&format!(
            "lumewarp_split_requests_total{{router=\"public\",split=\"canary\"}} {canary}"
        )));
    }

    #[test]
    fn sticky_splits_are_stable_and_follow_weights() {
        let routing_table = compile(&split_snapshot(pb::StickyBy::Header, "weighted"));
        let router = &routing_table.names["public"];
        let anonymous = Caller::Anonymous;
        let mut canary = 0;
        for user in 0..10_000 {
            let h = headers(&format!("session-{user}"));
            let first = router.split_for(&h, &anonymous);
            for _ in 0..3 {
                assert_eq!(router.split_for(&h, &anonymous), first);
            }
            canary += usize::from(first == 1);
        }
        assert!((850..=1150).contains(&canary), "canary got {canary}");
        // Fixed-seed hash: the same session lands in the same split in every
        // process (golden value).
        assert_eq!(router.split_for(&headers("session-1"), &anonymous), 0);
        // Missing header: not sticky, but still split by weight.
        let mut seen = [false; 2];
        for _ in 0..1000 {
            seen[router.split_for(&HeaderMap::new(), &anonymous)] = true;
        }
        assert_eq!(seen, [true, true]);
    }

    #[test]
    fn sticky_by_team_and_key() {
        let mut s = split_snapshot(pb::StickyBy::Team, "weighted");
        s.teams = vec![crate::snapshot::tests::team("t1")];
        s.api_keys = vec![
            key("k1", "s1", "t1", &["public"]),
            key("k2", "s2", "t1", &["public"]),
        ];
        let routing_table =
            RoutingTable::compile(&s, AuthMode::RequireKey, &Runtime::new()).unwrap();
        let k1 = Caller::Key(routing_table.keys[&hash("s1")].clone());
        let k2 = Caller::Key(routing_table.keys[&hash("s2")].clone());
        let router = &routing_table.names["public"];
        let none = HeaderMap::new();
        let team = router.split_for(&none, &k1);
        for _ in 0..100 {
            assert_eq!(router.split_for(&none, &k1), team);
            assert_eq!(router.split_for(&none, &k2), team, "same team, same split");
        }
        s.routers[0].sticky = pb::StickyBy::Key as i32;
        let routing_table =
            RoutingTable::compile(&s, AuthMode::RequireKey, &Runtime::new()).unwrap();
        let router = &routing_table.names["public"];
        let k1 = Caller::Key(routing_table.keys[&hash("s1")].clone());
        let first = router.split_for(&none, &k1);
        for _ in 0..100 {
            assert_eq!(router.split_for(&none, &k1), first);
        }
    }

    #[test]
    fn retries_stay_in_the_split_then_move_to_other_splits() {
        let routing_table = compile(&split_snapshot(pb::StickyBy::Header, "weighted"));
        let router = &routing_table.names["public"];
        let anonymous = Caller::Anonymous;
        // Find a session that the hash puts into the canary split.
        let h = (0..)
            .map(|i| headers(&format!("u{i}")))
            .find(|h| router.split_for(h, &anonymous) == 1)
            .unwrap();
        let (first, _, split) = routing_table
            .acquire_initial(router, 0, &anonymous, &h, &mut Skipped::default())
            .unwrap();
        assert_eq!(split, 1);
        let label = |name: &str| routing_table.runtime.metrics.label_id(name);
        let other = if first.model.name == "b" { "c" } else { "b" };
        assert_ne!(first.model.name, "a");
        let tried = [first.model.label];
        for _ in 0..50 {
            let (next, fallback) = routing_table
                .retry(
                    router,
                    0,
                    &anonymous,
                    &h,
                    &tried,
                    split,
                    &mut Skipped::default(),
                )
                .unwrap();
            assert!(fallback.is_none());
            assert_eq!(next.model.name, other, "stays inside the canary split");
        }
        let tried = [label("b"), label("c")];
        let (next, _) = routing_table
            .retry(
                router,
                0,
                &anonymous,
                &h,
                &tried,
                split,
                &mut Skipped::default(),
            )
            .unwrap();
        assert_eq!(next.model.name, "a", "then the other split");
    }

    #[test]
    fn lowest_latency_prefers_fast_and_explores_unmeasured() {
        fastrand::seed(11);
        let routing_table = compile(&snapshot("lowest_latency"));
        let router = &routing_table.names["public"];
        let pool = &router.pools[0];
        assert!(pool.models.iter().all(|d| d.track_latency));
        // Nothing measured: both are "best" and get explored.
        let mut a = 0;
        for _ in 0..1000 {
            a += usize::from(
                routing_table
                    .acquire(router, 0, &Caller::Anonymous)
                    .model
                    .name
                    == "a",
            );
        }
        assert!((350..650).contains(&a), "{a}");
        let count = |name: &str| {
            (0..1000)
                .filter(|_| {
                    routing_table
                        .acquire(router, 0, &Caller::Anonymous)
                        .model
                        .name
                        == name
                })
                .count()
        };
        pool.models[0].latency.success(Duration::from_millis(5));
        // b is unmeasured, so it still wins against a measured model;
        // only exploration (5%, half of it to a) reaches a.
        assert!(count("b") >= 940);
        pool.models[1].latency.success(Duration::from_millis(50));
        let fast = count("a");
        assert!(fast >= 940, "fast model got {fast}/1000");
        assert!(fast < 1000, "exploration keeps measuring the slow one");
        // Cost scales with queued work (peak-EWMA style): concurrent load
        // spreads roughly inversely to latency (10:1 here) instead of piling
        // everything onto the fastest model.
        let held: Vec<_> = (0..44)
            .map(|_| routing_table.acquire(router, 0, &Caller::Anonymous))
            .collect();
        let busy = |i: usize| pool.models[i].in_flight.load(Ordering::Relaxed);
        assert!((1..=7).contains(&busy(1)), "a={} b={}", busy(0), busy(1));
        drop(held);
    }

    #[test]
    fn vllm_load_avoids_congestion_and_falls_back_when_stale() {
        fastrand::seed(3);
        let routing_table = compile(&snapshot("vllm_load"));
        let router = &routing_table.names["public"];
        let pool = &router.pools[0];
        let count = |name: &str| {
            (0..1000)
                .filter(|_| {
                    routing_table
                        .acquire(router, 0, &Caller::Anonymous)
                        .model
                        .name
                        == name
                })
                .count()
        };
        let now = clock::mono_ms();
        pool.models[0].load.set(50.0, 0.5, now);
        pool.models[1].load.set(0.0, 0.5, now);
        let congested = count("a");
        assert!(congested <= 100, "congested model got {congested}/1000");
        // Equal queues: KV-cache usage decides.
        pool.models[0].load.set(0.0, 0.95, now);
        pool.models[1].load.set(0.0, 0.10, now);
        assert_eq!(count("b"), 1000);
        // Equal load: in-flight decides.
        pool.models[0].load.set(0.0, 0.1, now);
        pool.models[1].in_flight.fetch_add(3, Ordering::Relaxed);
        assert_eq!(count("a"), 1000);
        // a's scrape failed: comparisons with it use in-flight only, so the
        // (stale) queue no longer protects it.
        pool.models[0].load.set(99.0, 1.0, now);
        pool.models[0].load.fail();
        assert_eq!(count("a"), 1000);
        pool.models[1].in_flight.fetch_sub(3, Ordering::Relaxed);
        // Fresh again but old: older than the staleness bound is ignored.
        pool.models[0]
            .load
            .set(99.0, 1.0, now.saturating_sub(balance::LOAD_STALE_MS + 1));
        pool.models[0].in_flight.fetch_add(1, Ordering::Relaxed);
        assert_eq!(count("b"), 1000);
        pool.models[0].in_flight.fetch_sub(1, Ordering::Relaxed);
    }

    #[test]
    fn scrape_targets_and_metrics_urls() {
        let mut s = snapshot("vllm_load");
        s.upstreams.push(upstream("idle"));
        s.models.push(model("c", "idle"));
        let routing_table = compile(&s);
        assert_eq!(
            routing_table.scrapes.len(),
            1,
            "only upstreams behind vllm_load routers"
        );
        let scrape = &routing_table.scrapes[0];
        assert_eq!(scrape.uri, "http://localhost:8000/metrics");
        assert_eq!(scrape.targets.len(), 2);
        assert!(!routing_table.names["public"].pools[0].models[0].track_latency);
        let base: Uri = "https://h:1/prefix/v1/".parse().unwrap();
        assert_eq!(
            metrics_uri(&base, "").unwrap(),
            "https://h:1/prefix/metrics"
        );
        let base: Uri = "http://h/openai".parse().unwrap();
        assert_eq!(metrics_uri(&base, "").unwrap(), "http://h/openai/metrics");
        assert_eq!(
            metrics_uri(&base, "http://other:9/m?x=1").unwrap(),
            "http://other:9/m?x=1"
        );
        for bad in ["ftp://h/m", "http://u:p@h/m", "/metrics", "not a url"] {
            assert!(metrics_uri(&base, bad).is_none(), "{bad}");
        }
        s.upstreams[0].metrics_url = "ftp://h/m".into();
        assert!(RoutingTable::compile(&s, AuthMode::Open, &Runtime::new()).is_err());
    }

    #[test]
    fn load_aware_and_sticky_picks_do_not_allocate() {
        use crate::test_alloc::count;
        for (sticky, strategy) in [
            (pb::StickyBy::None, "lowest_latency"),
            (pb::StickyBy::None, "vllm_load"),
            (pb::StickyBy::Header, "lowest_latency"),
            (pb::StickyBy::Header, "vllm_load"),
        ] {
            let routing_table = compile(&split_snapshot(sticky, strategy));
            let router = &routing_table.names["public"];
            let h = headers("session-1");
            for d in router.pools.iter().flat_map(|p| &p.models) {
                d.latency.success(Duration::from_millis(1));
                d.load.set(1.0, 0.5, clock::mono_ms());
            }
            // Warm every handle the picks below can reach.
            for _ in 0..200 {
                drop(routing_table.acquire_initial(
                    router,
                    0,
                    &Caller::Anonymous,
                    &h,
                    &mut Skipped::default(),
                ));
                drop(routing_table.acquire(router, 0, &Caller::Anonymous));
            }
            let (warm, guard) = count(|| {
                routing_table.acquire_initial(
                    router,
                    0,
                    &Caller::Anonymous,
                    &h,
                    &mut Skipped::default(),
                )
            });
            drop(guard);
            assert_eq!(warm, (0, 0), "{sticky:?} {strategy:?}");
            let tried = [router.pools[1].models[0].label];
            let (retry, guard) = count(|| {
                routing_table.retry(
                    router,
                    0,
                    &Caller::Anonymous,
                    &h,
                    &tried,
                    1,
                    &mut Skipped::default(),
                )
            });
            drop(guard);
            assert_eq!(retry, (0, 0), "retry {sticky:?} {strategy:?}");
        }
    }

    /// Two breaker-guarded models on upstream `u` (threshold 1).
    fn guarded(open_ms: u32) -> Snapshot {
        let mut s = snapshot("weighted");
        s.required_capabilities.push("circuit_breaker".into());
        s.upstreams[0].circuit_breaker = Some(CircuitBreaker {
            failure_threshold: 1,
            open_ms,
        });
        s
    }

    fn open(d: &Model, now: u64) {
        d.breaker.finish(d.circuit.unwrap(), false, true, now);
    }

    #[test]
    fn fail_open_skips_a_model_whose_trial_is_running() {
        let routing_table = compile(&guarded(30_000));
        let router = &routing_table.names["public"];
        let (a, b) = (&router.pools[0].models[0], &router.pools[0].models[1]);
        // a: open since t=0, its single half-open trial now in progress.
        open(a, 0);
        assert_eq!(a.breaker.reserve(30_002), Some(true));
        // b: freshly open. Neither is eligible for strict selection.
        open(b, clock::mono_ms());
        for _ in 0..200 {
            let (guard, fallback, _) = routing_table
                .acquire_initial(
                    router,
                    0,
                    &Caller::Anonymous,
                    &HeaderMap::new(),
                    &mut Skipped::default(),
                )
                .expect("fails open onto the model without a running trial");
            assert!(fallback.is_none());
            assert_eq!(guard.model.name, "b");
        }
        assert!(a.breaker.trial_in_progress(), "the trial is left alone");
    }

    #[test]
    fn circuit_open_is_counted_once_per_request_under_the_root_model() {
        let runtime = Runtime::new();
        let mut s = guarded(30_000);
        s.required_capabilities
            .extend(["retry".into(), "fallbacks".into()]);
        s.routers = vec![router("public", &["a"])];
        s.routers[0].fallbacks = vec!["b".into()];
        s.routers[0].retry = Some(RetryPolicy::default());
        let routing_table = RoutingTable::compile(&s, AuthMode::Open, &runtime).unwrap();
        let router = &routing_table.names["public"];
        let now = clock::mono_ms();
        open(&router.pools[0].models[0], now);
        open(&routing_table.names["b"].pools[0].models[0], now);
        let count = |requested: &str, model: &str| {
            let series = format!(
                "lumewarp_upstream_attempts_total{{requested=\"{requested}\",model=\"{model}\",outcome=\"circuit_open\"}} "
            );
            let text = runtime.encode_metrics().unwrap();
            text.lines()
                .find_map(|line| line.strip_prefix(series.as_str()))
                .map_or(0, |v| v.parse::<u64>().unwrap())
        };
        let anonymous = Caller::Anonymous;
        let h = HeaderMap::new();
        for request in 1..=2u64 {
            let mut skipped = Skipped::default();
            // Strict selection fails everywhere (fallback included), then
            // fails open; retries consider the same open candidates again.
            let (guard, _, split) = routing_table
                .acquire_initial(router, 0, &anonymous, &h, &mut skipped)
                .unwrap();
            let tried = [guard.model.label];
            for _ in 0..3 {
                drop(routing_table.retry(router, 0, &anonymous, &h, &tried, split, &mut skipped));
            }
            drop(guard);
            assert_eq!(count("public", "a"), request);
            assert_eq!(count("public", "b"), request, "fallback models too");
            assert_eq!(count("b", "b"), 0, "labelled with the client's name");
        }
    }

    #[test]
    fn removed_breaker_config_resets_breaker_on_activation() {
        let runtime = Runtime::new();
        let s = guarded(30_000);
        let routing_table = RoutingTable::compile(&s, AuthMode::Open, &runtime).unwrap();
        let a = routing_table.names["public"].pools[0].models[0].clone();
        open(&a, clock::mono_ms());
        let gauge = |runtime: &Arc<Runtime>| {
            runtime
                .encode_metrics()
                .unwrap()
                .lines()
                .find_map(|l| {
                    l.strip_prefix("lumewarp_upstream_circuit_state{model=\"a\"} ")
                        .map(str::to_owned)
                })
                .unwrap()
        };
        assert_eq!(gauge(&runtime), "2");
        let mut next = s.clone();
        next.version = 2;
        next.upstreams[0].circuit_breaker = None;
        next.required_capabilities = vec!["strategy:weighted".into()];
        let swapped = RoutingTable::compile(&next, AuthMode::Open, &runtime).unwrap();
        // Compiling (which may still be NACKed) changes nothing.
        assert!(!a.breaker.available(clock::mono_ms()));
        assert_eq!(swapped.activate_health(), 0);
        assert!(a.breaker.available(clock::mono_ms()));
        assert_eq!(gauge(&runtime), "0");
    }

    #[tokio::test]
    async fn key_only_deltas_keep_probe_state() {
        // A port nothing listens on: every probe fails at once.
        let port = std::net::TcpListener::bind("127.0.0.1:0")
            .unwrap()
            .local_addr()
            .unwrap()
            .port();
        let runtime = Runtime::new();
        let mut s = base();
        s.required_capabilities.push("health_check".into());
        s.upstreams[0].base_url = format!("http://127.0.0.1:{port}/v1");
        s.upstreams[0].health_check = Some(pb::HealthCheck {
            interval_ms: 1000,
            timeout_ms: 100,
            unhealthy_threshold: 3,
            ..Default::default()
        });
        let routing_table = RoutingTable::compile(&s, AuthMode::RequireKey, &runtime).unwrap();
        assert_eq!(routing_table.activate_health(), 1);
        let healthy = runtime.health.upstream(&s.upstreams[0].name);
        // Key-only deltas every 250 ms. Restarting the probe on each would
        // reset its failure count (and probe at once), so the upstream would
        // never reach three consecutive failures.
        let started = std::time::Instant::now();
        let mut version = s.version;
        while healthy.load(Ordering::Acquire) {
            assert!(
                started.elapsed() < Duration::from_secs(5),
                "upstream never became unhealthy"
            );
            tokio::time::sleep(Duration::from_millis(250)).await;
            version += 1;
            s.version = version;
            s.api_keys.push(key(
                &format!("delta-{version}"),
                &format!("secret-{version}"),
                "t1",
                &[],
            ));
            let next = RoutingTable::compile(&s, AuthMode::RequireKey, &runtime).unwrap();
            assert_eq!(next.activate_health(), 0, "unchanged probe kept running");
        }
        assert!(version >= 3, "several deltas were applied while probing");
        // A changed probe is restarted; a removed one is stopped.
        s.upstreams[0]
            .health_check
            .as_mut()
            .unwrap()
            .unhealthy_threshold = 4;
        let changed = RoutingTable::compile(&s, AuthMode::RequireKey, &runtime).unwrap();
        assert_eq!(changed.activate_health(), 1);
        s.upstreams[0].health_check = None;
        let removed = RoutingTable::compile(&s, AuthMode::RequireKey, &runtime).unwrap();
        assert_eq!(removed.activate_health(), 0);
        assert!(
            healthy.load(Ordering::Acquire),
            "unprobed upstreams count as up"
        );
    }

    /// Two routers over the same two models, opposite weights; an alias of one
    /// and an alias of a model.
    fn catalog() -> Snapshot {
        let mut s = snapshot("weighted");
        s.routers = vec![router("mostly-a", &[]), router("mostly-b", &[])];
        s.routers[0].members = vec![member("a", 9), member("b", 1)];
        s.routers[1].members = vec![member("a", 1), member("b", 9)];
        s.aliases = vec![
            pb::Alias {
                name: "to-router".into(),
                target: "mostly-a".into(),
            },
            pb::Alias {
                name: "to-model".into(),
                target: "b".into(),
            },
        ];
        s
    }

    #[test]
    fn same_model_in_two_routers_has_independent_weights() {
        fastrand::seed(5);
        let routing_table = compile(&catalog());
        let share_of_a = |name: &str| {
            let called = &routing_table.names[name];
            (0..10_000)
                .filter(|_| {
                    routing_table
                        .acquire(called, 0, &Caller::Anonymous)
                        .model
                        .name
                        == "a"
                })
                .count()
        };
        let mostly_a = share_of_a("mostly-a");
        assert!((8700..9300).contains(&mostly_a), "{mostly_a}");
        let mostly_b = share_of_a("mostly-b");
        assert!((700..1300).contains(&mostly_b), "{mostly_b}");
        // A direct call ignores every router's weight.
        assert_eq!(share_of_a("a"), 10_000);
        assert_eq!(share_of_a("b"), 0);
    }

    #[test]
    fn alias_shares_the_target_and_keeps_its_own_name() {
        let runtime = Runtime::new();
        let table = RoutingTable::compile(&catalog(), AuthMode::Open, &runtime).unwrap();
        let (via, router) = (&table.names["to-router"], &table.names["mostly-a"]);
        assert!(Arc::ptr_eq(&via.target, &router.target), "same Arc");
        assert_eq!(&*via.name, "to-router");
        assert_eq!(via.alias.as_deref(), Some("to-router"));
        assert_eq!(via.router.as_deref(), Some("mostly-a"));
        assert_eq!(&*router.name, "mostly-a");
        assert_eq!(
            (router.alias.as_deref(), router.router.as_deref()),
            (None, Some("mostly-a"))
        );

        let (via, model) = (&table.names["to-model"], &table.names["b"]);
        assert!(Arc::ptr_eq(&via.target, &model.target), "same Arc");
        assert_eq!(
            (via.alias.as_deref(), via.router.as_deref()),
            (Some("to-model"), None)
        );
        assert_eq!(
            (model.alias.as_deref(), model.router.as_deref()),
            (None, None)
        );

        // Metrics carry the called name, not the target's.
        let guard = table.acquire(via, 0, &Caller::Anonymous);
        assert_eq!(guard.metrics.requested, "to-model");
        assert_eq!(guard.metrics.model, "b");
        // The rejection path too.
        let rejected = table.rejected_model(&Caller::Anonymous, "to-model", 0);
        assert_eq!(rejected.requested, "to-model");
        let unknown = table.rejected_model(&Caller::Anonymous, "nope", 0);
        assert_eq!(unknown.requested, "unknown");
    }

    #[test]
    fn a_model_is_a_single_attempt_pool_and_breakers_are_shared_by_model() {
        let mut s = guarded(30_000);
        s.routers[0].retry = Some(RetryPolicy::default());
        s.routers[0].fallbacks = vec!["b".into()];
        s.required_capabilities
            .extend(["retry".into(), "fallbacks".into()]);
        let table = compile(&s);
        let direct = &table.names["a"];
        assert!(direct.retry.is_none());
        assert!(direct.fallbacks.is_empty());
        assert_eq!(direct.pools.len(), 1);
        assert_eq!(direct.pools[0].models.len(), 1);
        // One breaker per model, whichever pool holds it.
        let public = &table.names["public"];
        let member = public.pools[0]
            .models
            .iter()
            .find(|d| d.name == "a")
            .unwrap();
        assert!(Arc::ptr_eq(
            &member.breaker,
            &direct.pools[0].models[0].breaker
        ));
        assert!(Arc::ptr_eq(
            &public.fallbacks[0].0.pools[0].models[0],
            &table.names["b"].pools[0].models[0]
        ));
        // A retry of a direct call has nowhere else to go but the same model.
        let tried = [direct.pools[0].models[0].label];
        let (again, fallback) = table
            .retry(
                direct,
                0,
                &Caller::Anonymous,
                &HeaderMap::new(),
                &tried,
                0,
                &mut Skipped::default(),
            )
            .unwrap();
        assert_eq!(again.model.name, "a");
        assert!(fallback.is_none());
    }

    #[test]
    fn fallback_metric_names_the_router_and_the_real_model() {
        let runtime = Runtime::new();
        let mut s = guarded(30_000);
        s.routers = vec![router("public", &["a"])];
        s.routers[0].fallbacks = vec!["b".into()];
        s.required_capabilities.push("fallbacks".into());
        s.aliases = vec![pb::Alias {
            name: "nick".into(),
            target: "public".into(),
        }];
        let table = RoutingTable::compile(&s, AuthMode::Open, &runtime).unwrap();
        open(&table.names["public"].pools[0].models[0], clock::mono_ms());
        let (guard, counter, _) = table
            .acquire_initial(
                &table.names["nick"],
                0,
                &Caller::Anonymous,
                &HeaderMap::new(),
                &mut Skipped::default(),
            )
            .unwrap();
        assert_eq!(guard.model.name, "b");
        assert_eq!(guard.metrics.requested, "nick");
        counter.unwrap().inc();
        let text = runtime.encode_metrics().unwrap();
        assert!(
            text.contains("lumewarp_fallbacks_total{router=\"public\",model=\"b\"} 1"),
            "{text}"
        );
    }

    #[test]
    fn keys_list_and_reach_names_of_every_kind_by_name_only() {
        let mut s = catalog();
        s.teams = vec![crate::snapshot::tests::team("t1")];
        s.api_keys = vec![key("k", "s", "t1", &["mostly-a", "to-model"])];
        let table = RoutingTable::compile(&s, AuthMode::RequireKey, &Runtime::new()).unwrap();
        let caller = Caller::Key(table.keys[&hash("s")].clone());
        let allowed = |name| crate::auth::check_model(&caller, name).is_ok();
        assert!(allowed("mostly-a"));
        assert!(!allowed("a"), "a router's member needs its own grant");
        assert!(allowed("to-model"));
        assert!(!allowed("b"), "an alias's target needs its own grant");
        assert!(
            !allowed("to-router"),
            "a target's grant does not reach an alias"
        );
        let body = table.models_for(&caller);
        let v: serde_json::Value = serde_json::from_slice(&body).unwrap();
        let ids: Vec<_> = v["data"]
            .as_array()
            .unwrap()
            .iter()
            .map(|m| m["id"].as_str().unwrap())
            .collect();
        assert_eq!(ids, ["mostly-a", "to-model"]);
    }

    #[test]
    fn the_name_lookup_and_the_pick_do_not_allocate() {
        use crate::test_alloc::count;
        let table = compile(&catalog());
        for name in ["a", "to-model", "mostly-a", "to-router"] {
            // Metric handles are created on first use of each model.
            for _ in 0..200 {
                drop(table.acquire(&table.names[name], 0, &Caller::Anonymous));
            }
            let (allocations, guard) = count(|| {
                let called = table.lookup(name).unwrap();
                table.acquire(called, 0, &Caller::Anonymous)
            });
            drop(guard);
            assert_eq!(allocations, (0, 0), "{name}");
        }
    }
}
