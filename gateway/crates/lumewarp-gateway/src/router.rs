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
    CircuitBreaker, KeyStatus, Price, RateLimit, RetryPolicy, RoutingStrategy, Snapshot, StickyBy,
    StreamUsageMode,
};
use prometheus_client::metrics::{counter::Counter, gauge::Gauge};

use crate::{
    auth::Caller,
    balance::{self, Latency, Scrape, VllmLoad},
    health::{Breaker, Probe},
    metrics::{HandleKey, LabelId, RequestMetrics, ROUTES},
    ratelimit::clock,
    runtime::Runtime,
    snapshot::{self, ApplyError},
};

/// Proxied endpoints: chat_completions, completions, embeddings.
const ENDPOINTS: usize = 3;

pub struct Deployment {
    pub name: String,
    // Each absolute URI contains its precompiled endpoint PathAndQuery.
    pub endpoints: [Uri; ENDPOINTS],
    pub host: HeaderValue,
    pub authorization: Option<HeaderValue>,
    pub upstream_model_json: Bytes,
    pub timeout: Duration,
    /// Shared through `Runtime::inflight` by deployment name, so it survives swaps.
    pub in_flight: Arc<AtomicUsize>,
    /// Metering context, immutable for every request holding this deployment:
    /// a request keeps the price it started with across snapshot swaps.
    /// `None` = price unknown (cost reported unknown, never 0).
    pub price: Option<Price>,
    /// Resolved: UNSPECIFIED is stored as `IncludeUsage`.
    pub stream_usage_mode: StreamUsageMode,
    /// Interned deployment name for metric handle lookups.
    pub label: LabelId,
    pub breaker: Arc<Breaker>,
    pub circuit: Option<CircuitBreaker>,
    pub healthy: Arc<AtomicBool>,
    /// Shared by deployment name through `Runtime::balance` (survive swaps).
    pub latency: Arc<Latency>,
    /// Feed `latency` only when a `LOWEST_LATENCY` route uses this deployment.
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

/// The deployments of one traffic split (or of the whole route without one).
struct Pool {
    deployments: Vec<Arc<Deployment>>,
    cumulative_weights: Vec<u64>,
    total_weight: u64,
}

pub struct Route {
    model: Box<str>,
    model_label: LabelId,
    /// One pool per traffic split, in declaration order; exactly one pool
    /// when the route has no split.
    pools: Vec<Pool>,
    split_cumulative: Vec<u64>,
    split_total: u64,
    /// `lumewarp_split_requests_total{model,split}`; empty without splits.
    split_requests: Vec<Counter>,
    sticky: StickyBy,
    sticky_header: Option<HeaderName>,
    strategy: RoutingStrategy,
    pub retry: Option<RetryPolicy>,
    pub fallbacks: Vec<(String, Counter)>,
    pub budget_exhausted: Counter,
    /// `lumewarp_upstream_attempts_total{outcome="circuit_open"}` labelled
    /// with this (client-facing) model, for every breaker-guarded deployment
    /// of this route and of its fallbacks.
    circuit_open: Vec<(LabelId, Counter)>,
}

/// Deployments already counted as `circuit_open` for one request, so each
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

impl Route {
    /// Picks the split for a request: by the sticky hash when the route is
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
        strategy: RoutingStrategy,
        excluded: Option<&[LabelId]>,
        now: u64,
        strict: bool,
    ) -> Option<usize> {
        let eligible = |i: usize| {
            let d = &self.deployments[i];
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
        let in_flight = |i: usize| self.deployments[i].in_flight.load(Ordering::Relaxed);
        match strategy {
            RoutingStrategy::LeastInFlight => {
                let len = self.deployments.len();
                let start = fastrand::usize(0..len);
                (0..len)
                    .map(|offset| (start + offset) % len)
                    .filter(|&i| eligible(i))
                    .min_by_key(|&i| in_flight(i))
            }
            RoutingStrategy::LowestLatency => {
                if fastrand::u32(0..100) < balance::EXPLORE_PERCENT {
                    return self.random_eligible(&eligible);
                }
                // Peak-EWMA style cost: latency scaled by queued work here.
                // No samples yet = cost 0, so new deployments get explored.
                let cost = |i: usize| {
                    u128::from(self.deployments[i].latency.get_us()) * (in_flight(i) as u128 + 1)
                };
                self.two_choices(&eligible, |a, b| cost(a) < cost(b))
            }
            RoutingStrategy::VllmLoad => self.two_choices(&eligible, |a, b| {
                let (da, db) = (&self.deployments[a], &self.deployments[b]);
                match (da.load.score(now), db.load.score(now)) {
                    (Some((wa, ka)), Some((wb, kb))) => {
                        (wa, ka, in_flight(a)) < (wb, kb, in_flight(b))
                    }
                    // Stale or failed scrape on either side: in-flight only.
                    _ => in_flight(a) < in_flight(b),
                }
            }),
            _ => {
                let weight = |i: usize| {
                    self.cumulative_weights[i]
                        - if i == 0 {
                            0
                        } else {
                            self.cumulative_weights[i - 1]
                        }
                };
                let total: u64 = (0..self.deployments.len())
                    .filter(|&i| eligible(i))
                    .map(weight)
                    .sum();
                if total == 0 {
                    return None;
                }
                // Keep the original fast cumulative draw when nothing is filtered.
                if total == self.total_weight {
                    return Some(self.weighted_index(fastrand::u64(0..total)));
                }
                let mut draw = fastrand::u64(0..total);
                (0..self.deployments.len()).find(|&i| {
                    if !eligible(i) {
                        return false;
                    }
                    let w = weight(i);
                    if draw < w {
                        return true;
                    }
                    draw -= w;
                    false
                })
            }
        }
    }

    fn nth_eligible(&self, eligible: &impl Fn(usize) -> bool, n: usize) -> Option<usize> {
        (0..self.deployments.len()).filter(|&i| eligible(i)).nth(n)
    }

    fn random_eligible(&self, eligible: &impl Fn(usize) -> bool) -> Option<usize> {
        let count = (0..self.deployments.len()).filter(|&i| eligible(i)).count();
        if count == 0 {
            return None;
        }
        self.nth_eligible(eligible, fastrand::usize(0..count))
    }

    /// Power of two choices: two distinct random eligible deployments, keep
    /// the one `better` prefers (ties keep the first, itself random).
    fn two_choices(
        &self,
        eligible: &impl Fn(usize) -> bool,
        better: impl Fn(usize, usize) -> bool,
    ) -> Option<usize> {
        let count = (0..self.deployments.len()).filter(|&i| eligible(i)).count();
        match count {
            0 => None,
            1 => self.nth_eligible(eligible, 0),
            _ => {
                let a = fastrand::usize(0..count);
                let mut b = fastrand::usize(0..count - 1);
                if b >= a {
                    b += 1;
                }
                let a = self.nth_eligible(eligible, a)?;
                let b = self.nth_eligible(eligible, b)?;
                Some(if better(b, a) { b } else { a })
            }
        }
    }

    fn weighted_index(&self, draw: u64) -> usize {
        self.cumulative_weights
            .partition_point(|&weight| weight <= draw)
    }
}

pub struct InFlight {
    pub deployment: Arc<Deployment>,
    pub metrics: Arc<RequestMetrics>,
    trial: bool,
    finished: bool,
}

impl InFlight {
    pub fn complete(&mut self, retryable_failure: bool) {
        self.finished = true;
        if let Some(config) = self.deployment.circuit {
            self.deployment
                .breaker
                .finish(config, self.trial, retryable_failure, clock::mono_ms());
        }
    }
}

impl Drop for InFlight {
    fn drop(&mut self) {
        if self.trial && !self.finished {
            self.deployment.breaker.abandon_trial(clock::mono_ms());
        }
        self.deployment.in_flight.fetch_sub(1, Ordering::Relaxed);
    }
}

pub struct Router {
    pub version: u64,
    pub keys: HashMap<[u8; 32], Arc<KeyContext>>,
    pub routes: HashMap<String, Route>,
    pub models_json: Bytes,
    pub auth_mode: AuthMode,
    pub runtime: Arc<Runtime>,
    /// Pre-routing rejections and local endpoints (model "", team "").
    pub rejected: [Arc<RequestMetrics>; ROUTES.len()],
    /// Route aliases in snapshot order, for filtered `/v1/models`.
    models: Box<[Box<str>]>,
    unknown_label: LabelId,
    probes: Vec<Probe>,
    unprobed: Vec<(Arc<AtomicBool>, Gauge)>,
    scrapes: Vec<Scrape>,
    /// Breakers of deployments whose upstream has no `circuit_breaker`
    /// (any more): reset to closed when this snapshot is activated.
    unbroken: Vec<Arc<Breaker>>,
}

fn invalid(reason: String) -> ApplyError {
    ApplyError::invalid(reason)
}

impl Router {
    /// Compiles a validated snapshot. Fails closed: with `RequireKey` and no
    /// keys every `/v1/*` request is rejected.
    pub fn compile(
        snapshot: &Snapshot,
        auth_mode: AuthMode,
        runtime: &Arc<Runtime>,
    ) -> Result<Self, ApplyError> {
        snapshot::validate(snapshot, snapshot::SUPPORTED_CAPABILITIES)?;
        let mut upstreams = HashMap::new();
        let mut probes = Vec::new();
        let mut unprobed = Vec::new();
        let mut metrics_uris = HashMap::new();
        // Which deployments need latency samples, which upstreams scraping.
        let upstream_of: HashMap<&str, &str> = snapshot
            .deployments
            .iter()
            .map(|d| (d.name.as_str(), d.upstream.as_str()))
            .collect();
        let mut latency_tracked = HashSet::new();
        let mut scraped_upstreams = HashSet::new();
        for r in &snapshot.routes {
            let names = route_deployment_names(r);
            match RoutingStrategy::try_from(r.strategy) {
                Ok(RoutingStrategy::LowestLatency) => latency_tracked.extend(names),
                Ok(RoutingStrategy::VllmLoad) => {
                    scraped_upstreams.extend(names.filter_map(|d| upstream_of.get(d).copied()))
                }
                _ => {}
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
        let mut deployments = HashMap::new();
        for d in &snapshot.deployments {
            let (endpoints, host, authorization, timeout, stream_usage_mode, healthy, circuit) =
                &upstreams[d.upstream.as_str()];
            metrics.register_deployment(&d.name);
            deployments.insert(
                d.name.as_str(),
                (
                    Arc::new(Deployment {
                        name: d.name.clone(),
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
                    d.weight,
                ),
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
                    .deployments
                    .iter()
                    .filter(|d| d.upstream == name)
                    .map(|d| {
                        (
                            d.upstream_model.clone(),
                            deployments[d.name.as_str()].0.load.clone(),
                        )
                    })
                    .collect(),
                failures: metrics.scrape_failures(name),
            });
        }

        let mut routes = HashMap::new();
        for r in &snapshot.routes {
            let strategy = RoutingStrategy::try_from(r.strategy).expect("validated strategy");
            let groups: Vec<(&str, &[String], u32)> = if r.traffic_split.is_empty() {
                vec![("", r.deployments.as_slice(), 1)]
            } else {
                r.traffic_split
                    .iter()
                    .map(|s| (s.name.as_str(), s.deployments.as_slice(), s.weight))
                    .collect()
            };
            let mut pools = Vec::with_capacity(groups.len());
            let mut split_cumulative = Vec::with_capacity(groups.len());
            let mut split_total = 0u64;
            for (split, names, split_weight) in groups {
                let mut resolved = Vec::new();
                let mut cumulative_weights = Vec::new();
                let mut total_weight = 0u64;
                for name in names {
                    let (deployment, weight) = &deployments[name.as_str()];
                    resolved.push(deployment.clone());
                    total_weight = total_weight
                        .checked_add(u64::from(*weight))
                        .ok_or_else(|| invalid(format!("route {:?} weights overflow", r.model)))?;
                    cumulative_weights.push(total_weight);
                }
                if strategy == RoutingStrategy::Weighted && total_weight == 0 {
                    return Err(invalid(format!(
                        "route {:?}{}{split} must have deployments with positive total weight",
                        r.model,
                        if split.is_empty() { "" } else { " split " },
                    )));
                }
                for deployment in
                    std::iter::once("").chain(resolved.iter().map(|d| d.name.as_str()))
                {
                    for endpoint in 0..ENDPOINTS {
                        drop(metrics.handles(&r.model, deployment, endpoint, ""));
                    }
                }
                split_total += u64::from(split_weight);
                split_cumulative.push(split_total);
                // Pre-register the attempt series at zero.
                for name in names {
                    drop(metrics.attempt_handles(&r.model, name));
                }
                pools.push(Pool {
                    deployments: resolved,
                    cumulative_weights,
                    total_weight,
                });
            }
            let mut circuit_open: Vec<(LabelId, Counter)> = Vec::new();
            let fallback_names = r.fallbacks.iter().flat_map(|f| {
                snapshot
                    .routes
                    .iter()
                    .filter(move |x| &x.model == f)
                    .flat_map(route_deployment_names)
            });
            for name in route_deployment_names(r).chain(fallback_names) {
                let d = &deployments[name].0;
                if d.circuit.is_some() && !circuit_open.iter().any(|(l, _)| *l == d.label) {
                    let [_, _, _, _, open, _] = metrics.attempt_handles(&r.model, name);
                    circuit_open.push((d.label, open));
                }
            }
            let sticky = StickyBy::try_from(r.sticky).expect("validated sticky");
            routes.insert(
                r.model.clone(),
                Route {
                    model: r.model.as_str().into(),
                    model_label: metrics.label_id(&r.model),
                    pools,
                    split_cumulative,
                    split_total,
                    split_requests: r
                        .traffic_split
                        .iter()
                        .map(|s| metrics.split_handle(&r.model, &s.name))
                        .collect(),
                    sticky,
                    sticky_header: (sticky == StickyBy::Header).then(|| {
                        HeaderName::from_bytes(r.sticky_header.as_bytes())
                            .expect("validated sticky_header")
                    }),
                    strategy,
                    retry: r.retry.clone(),
                    fallbacks: r
                        .fallbacks
                        .iter()
                        .map(|f| (f.clone(), metrics.fallback_handle(&r.model, f)))
                        .collect(),
                    budget_exhausted: metrics.budget_handle(&r.model),
                    circuit_open,
                },
            );
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

        let models: Box<[Box<str>]> = snapshot
            .routes
            .iter()
            .map(|r| r.model.as_str().into())
            .collect();
        Ok(Self {
            version: snapshot.version,
            keys,
            routes,
            models_json: models_json(models.iter()),
            auth_mode,
            rejected: std::array::from_fn(|route| metrics.handles("", "", route, "")),
            runtime: runtime.clone(),
            models,
            unknown_label: metrics.label_id("unknown"),
            probes,
            unprobed,
            scrapes,
            unbroken: deployments
                .values()
                .filter(|(d, _)| d.circuit.is_none())
                .map(|(d, _)| d.breaker.clone())
                .collect(),
        })
    }

    fn handles(
        &self,
        caller: &Caller,
        model: (LabelId, &str),
        deployment: (LabelId, &str),
        route: usize,
    ) -> Arc<RequestMetrics> {
        let (team, team_id) = match caller {
            Caller::Anonymous => (0, ""),
            Caller::Key(key) => (key.team_label, &*key.team_id),
        };
        self.runtime.metrics.cached_handles(
            HandleKey {
                team,
                model: model.0,
                deployment: deployment.0,
                route: route as u8,
            },
            team_id,
            model.1,
            deployment.1,
        )
    }

    /// Handles for an authenticated request before (or without) a model:
    /// model and deployment `""`, the caller's team. `route` indexes `ROUTES`.
    pub fn caller_handles(&self, caller: &Caller, route: usize) -> Arc<RequestMetrics> {
        self.handles(caller, (0, ""), (0, ""), route)
    }

    /// Handles for a request whose model has no route.
    pub fn unknown(&self, caller: &Caller, endpoint: usize) -> Arc<RequestMetrics> {
        self.handles(caller, (self.unknown_label, "unknown"), (0, ""), endpoint)
    }

    /// Handles for a request rejected after its model was parsed: the route's
    /// alias when it exists, `unknown` otherwise.
    pub fn rejected_model(
        &self,
        caller: &Caller,
        model: &str,
        endpoint: usize,
    ) -> Arc<RequestMetrics> {
        match self.routes.get(model) {
            Some(route) => {
                self.handles(caller, (route.model_label, &route.model), (0, ""), endpoint)
            }
            None => self.unknown(caller, endpoint),
        }
    }

    /// Picks a deployment and reserves an in-flight slot on it.
    #[cfg(test)]
    pub fn acquire(&self, route: &Route, endpoint: usize, caller: &Caller) -> InFlight {
        self.acquire_initial(
            route,
            endpoint,
            caller,
            &HeaderMap::new(),
            &mut Skipped::default(),
        )
        .expect("test route has an available deployment")
        .0
    }

    /// Chooses the request's split (counted once), then its own split, the
    /// other splits, the fallbacks, and finally fails open on its own route.
    /// Returns the guard, the fallback counter if one was used, and the split.
    pub fn acquire_initial<'a>(
        &'a self,
        route: &'a Route,
        endpoint: usize,
        caller: &Caller,
        headers: &HeaderMap,
        skipped: &mut Skipped,
    ) -> Option<(InFlight, Option<&'a Counter>, usize)> {
        let split = route.split_for(headers, caller);
        if let Some(counter) = route.split_requests.get(split) {
            counter.inc();
        }
        if let Some(guard) =
            self.acquire_from(route, route, split, endpoint, caller, None, false, skipped)
        {
            return Some((guard, None, split));
        }
        for (name, counter) in &route.fallbacks {
            let fallback = &self.routes[name];
            let first = fallback.split_for(headers, caller);
            if let Some(guard) = self.acquire_from(
                route, fallback, first, endpoint, caller, None, false, skipped,
            ) {
                return Some((guard, Some(counter), split));
            }
        }
        self.acquire_from(route, route, split, endpoint, caller, None, true, skipped)
            .map(|guard| (guard, None, split))
    }

    /// First exhaust untried deployments (the chosen split, then the other
    /// splits), then fallbacks in declaration order. Only when every eligible
    /// candidate is unhealthy/open do we fail open.
    #[allow(clippy::too_many_arguments)]
    pub fn retry<'a>(
        &'a self,
        root: &'a Route,
        endpoint: usize,
        caller: &Caller,
        headers: &HeaderMap,
        tried: &[LabelId],
        split: usize,
        skipped: &mut Skipped,
    ) -> Option<(InFlight, Option<&'a Counter>)> {
        for strict in [true, false] {
            for excluded in [tried, &[][..]] {
                if let Some(guard) = self.acquire_from(
                    root,
                    root,
                    split,
                    endpoint,
                    caller,
                    Some(excluded),
                    !strict,
                    skipped,
                ) {
                    return Some((guard, None));
                }
                for (name, counter) in &root.fallbacks {
                    let fallback = &self.routes[name];
                    if let Some(guard) = self.acquire_from(
                        root,
                        fallback,
                        fallback.split_for(headers, caller),
                        endpoint,
                        caller,
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
        root: &Route,
        route: &Route,
        split: usize,
        endpoint: usize,
        caller: &Caller,
        excluded: Option<&[LabelId]>,
        fail_open: bool,
        skipped: &mut Skipped,
    ) -> Option<InFlight> {
        let now = clock::mono_ms();
        for d in route.pools.iter().flat_map(|pool| &pool.deployments) {
            if d.circuit.is_some() && !d.breaker.available(now) && skipped.first(d.label) {
                if let Some((_, counter)) = root.circuit_open.iter().find(|(l, _)| *l == d.label) {
                    counter.inc();
                }
            }
        }
        let pick = |strict: bool| {
            route.pool_order(split).find_map(|pool| {
                pool.pick(route.strategy, excluded, now, strict)
                    .map(|i| &pool.deployments[i])
            })
        };
        let deployment = pick(true)
            .or_else(|| fail_open.then(|| pick(false)).flatten())?
            .clone();
        let trial = if deployment.circuit.is_some() {
            match deployment.breaker.reserve(now) {
                Some(trial) => trial,
                None if fail_open && !deployment.breaker.trial_in_progress() => false,
                None => return None,
            }
        } else {
            false
        };
        deployment.in_flight.fetch_add(1, Ordering::Relaxed);
        let metrics = self.handles(
            caller,
            (root.model_label, &root.model),
            (deployment.label, &deployment.name),
            endpoint,
        );
        Some(InFlight {
            deployment,
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

    /// `/v1/models` body: every alias, or only those the caller's key allows.
    pub fn models_for(&self, caller: &Caller) -> Bytes {
        match caller {
            Caller::Key(key) if !key.allow_all_models => models_json(
                self.models
                    .iter()
                    .filter(|m| key.allowed_models.contains(m.as_ref())),
            ),
            _ => self.models_json.clone(),
        }
    }
}

/// Every deployment a route can use: its splits' or its own list.
fn route_deployment_names(
    r: &lumewarp_proto::config::v1::ModelRoute,
) -> Box<dyn Iterator<Item = &str> + '_> {
    if r.traffic_split.is_empty() {
        Box::new(r.deployments.iter().map(String::as_str))
    } else {
        Box::new(
            r.traffic_split
                .iter()
                .flat_map(|s| s.deployments.iter().map(String::as_str)),
        )
    }
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
    use crate::snapshot::tests::{base, deployment, key, route, upstream};
    use lumewarp_proto::config::v1 as pb;

    fn snapshot(strategy: RoutingStrategy) -> Snapshot {
        let mut a = deployment("a", "u");
        a.weight = 1;
        let mut b = deployment("b", "u");
        b.weight = 3;
        let mut public = route("public", &["a", "b"]);
        public.strategy = strategy as i32;
        let required_capabilities = match strategy {
            RoutingStrategy::LowestLatency => vec!["lowest_latency".into()],
            RoutingStrategy::VllmLoad => vec!["vllm_load".into()],
            _ => vec![],
        };
        Snapshot {
            upstreams: vec![upstream("u")],
            deployments: vec![a, b],
            routes: vec![public],
            required_capabilities,
            ..Default::default()
        }
    }

    fn compile(s: &Snapshot) -> Router {
        Router::compile(s, AuthMode::Open, &Runtime::new()).unwrap()
    }

    #[test]
    fn weighted_distribution() {
        let router = compile(&snapshot(RoutingStrategy::Weighted));
        let route = &router.routes["public"];
        let mut a = 0;
        // Seeded draws make this deterministic while exercising cumulative selection.
        let mut rng = fastrand::Rng::with_seed(42);
        for _ in 0..20000 {
            let pool = &route.pools[0];
            a += usize::from(pool.weighted_index(rng.u64(0..pool.total_weight)) == 0);
        }
        assert!((4500..5500).contains(&a), "{a}");
    }

    #[test]
    fn least_in_flight_and_guard_release() {
        let router = compile(&snapshot(RoutingStrategy::LeastInFlight));
        let route = &router.routes["public"];
        let anonymous = Caller::Anonymous;
        let first = router.acquire(route, 0, &anonymous);
        let first_name = first.deployment.name.clone();
        let second = router.acquire(route, 0, &anonymous);
        assert_ne!(second.deployment.name, first_name);
        drop(first);
        assert_eq!(
            router.acquire(route, 0, &anonymous).deployment.name,
            first_name
        );
        drop(second);
        assert!(route.pools[0]
            .deployments
            .iter()
            .all(|d| d.in_flight.load(Ordering::Relaxed) == 0));
    }

    #[test]
    fn least_in_flight_ties_spread() {
        let router = compile(&snapshot(RoutingStrategy::LeastInFlight));
        let route = &router.routes["public"];
        let mut a = 0;
        for _ in 0..10000 {
            a += usize::from(router.acquire(route, 0, &Caller::Anonymous).deployment.name == "a");
        }
        assert!((4500..5500).contains(&a), "ties selected a {a} times");
    }

    #[test]
    fn in_flight_and_metrics_survive_swaps() {
        let runtime = Runtime::new();
        let s = snapshot(RoutingStrategy::LeastInFlight);
        let old = Router::compile(&s, AuthMode::Open, &runtime).unwrap();
        let guard = old.acquire(&old.routes["public"], 0, &Caller::Anonymous);
        let busy = guard.deployment.name.clone();
        let mut next = s.clone();
        next.version = 2;
        let new = Router::compile(&next, AuthMode::Open, &runtime).unwrap();
        drop(old);
        // The stream still running on the old snapshot steers the new one away.
        for _ in 0..100 {
            assert_ne!(
                new.acquire(&new.routes["public"], 0, &Caller::Anonymous)
                    .deployment
                    .name,
                busy
            );
        }
        let text = runtime.encode_metrics().unwrap();
        assert!(text.contains(&format!(
            "lumewarp_in_flight_requests{{deployment=\"{busy}\"}} 1"
        )));
        drop(guard);
        let text = runtime.encode_metrics().unwrap();
        assert!(text.contains(&format!(
            "lumewarp_in_flight_requests{{deployment=\"{busy}\"}} 0"
        )));
        // Counters are not reset by recompiling: same label set, same counter.
        crate::metrics::tests::finish_with(&new.rejected[0], 404);
        let third = Router::compile(&next, AuthMode::Open, &runtime).unwrap();
        drop(new);
        crate::metrics::tests::finish_with(&third.rejected[0], 404);
        assert!(runtime.encode_metrics().unwrap().contains(
            "{model=\"\",route=\"chat_completions\",deployment=\"\",team=\"\",status=\"404\"} 2"
        ));
    }

    #[test]
    fn breaker_and_health_survive_swaps_without_hot_path_allocations() {
        let runtime = Runtime::new();
        let mut s = snapshot(RoutingStrategy::Weighted);
        s.required_capabilities = vec!["circuit_breaker".into(), "retry".into()];
        s.upstreams[0].circuit_breaker = Some(CircuitBreaker {
            failure_threshold: 1,
            open_ms: 30_000,
        });
        s.routes[0].retry = Some(RetryPolicy::default());
        let old = Router::compile(&s, AuthMode::Open, &runtime).unwrap();
        let mut first = old.acquire(&old.routes["public"], 0, &Caller::Anonymous);
        let name = first.deployment.name.clone();
        let breaker = first.deployment.breaker.clone();
        first.complete(true);
        drop(first);
        let next = Router::compile(&s, AuthMode::Open, &runtime).unwrap();
        let guard = next.acquire(&next.routes["public"], 0, &Caller::Anonymous);
        assert_ne!(guard.deployment.name, name);
        assert!(Arc::ptr_eq(
            &breaker,
            &next.routes["public"].pools[0]
                .deployments
                .iter()
                .find(|d| d.name == name)
                .unwrap()
                .breaker
        ));
        drop(guard);
        let ((allocations, bytes), guard) = crate::test_alloc::count(|| {
            runtime.retry_budget.request();
            next.acquire(&next.routes["public"], 0, &Caller::Anonymous)
        });
        assert_eq!((allocations, bytes), (0, 0));
        drop(guard);
        // The only remaining deployment is also down: routing fails open
        // rather than rejecting all traffic on passive health alone.
        next.routes["public"].pools[0].deployments[0]
            .healthy
            .store(false, Ordering::Release);
        assert!(next
            .acquire_initial(
                &next.routes["public"],
                0,
                &Caller::Anonymous,
                &HeaderMap::new(),
                &mut Skipped::default()
            )
            .is_some());
    }

    #[test]
    fn missing_api_key_env_is_load_error() {
        let mut s = snapshot(RoutingStrategy::Weighted);
        s.upstreams[0].api_key_ref = "LUMEWARP_TEST_MISSING_KEY_8B3F590E".into();
        let err = Router::compile(&s, AuthMode::Open, &Runtime::new())
            .err()
            .unwrap();
        assert_eq!(err.code, pb::NackCode::InvalidConfig);
        assert!(err.reason.contains("api_key_env"));
    }

    #[test]
    fn rejects_invalid_snapshots_and_bad_urls() {
        let mut s = snapshot(RoutingStrategy::Weighted);
        s.upstreams[0].base_url = "ftp://x/v1".into();
        assert!(Router::compile(&s, AuthMode::Open, &Runtime::new()).is_err());
        let mut s = snapshot(RoutingStrategy::Weighted);
        s.deployments[0].weight = 0;
        s.deployments[1].weight = 0;
        assert!(Router::compile(&s, AuthMode::Open, &Runtime::new()).is_err());
        let mut s = snapshot(RoutingStrategy::Weighted);
        s.routes[0].strategy = 0;
        assert!(Router::compile(&s, AuthMode::Open, &Runtime::new()).is_err());
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
        let router = Router::compile(&s, AuthMode::RequireKey, &Runtime::new()).unwrap();
        assert_eq!(router.keys.len(), 3);
        let hash = |secret: &str| -> [u8; 32] {
            <sha2::Sha256 as sha2::Digest>::digest(secret.as_bytes()).into()
        };
        let k1 = router.keys[&hash("s1")].clone();
        assert_eq!(&*k1.team_id, "t1");
        assert_ne!(k1.team_label, 0);
        let k2 = router.keys[&hash("s2")].clone();
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
        assert_eq!(ids(router.models_for(&Caller::Key(k1))), ["m1"]);
        assert!(ids(router.models_for(&Caller::Key(k2))).is_empty());
        let k3 = router.keys[&hash("s3")].clone();
        assert_eq!(ids(router.models_for(&Caller::Key(k3))), ["m1", "m2"]);
        assert_eq!(ids(router.models_for(&Caller::Anonymous)), ["m1", "m2"]);
    }

    #[test]
    fn team_handles_are_lazy() {
        let runtime = Runtime::new();
        let router = Router::compile(&base(), AuthMode::RequireKey, &runtime).unwrap();
        assert!(!runtime.encode_metrics().unwrap().contains("team=\"t1\""));
        let k1 = Caller::Key(
            router.keys[&<[u8; 32]>::from(<sha2::Sha256 as sha2::Digest>::digest(b"s1"))].clone(),
        );
        let guard = router.acquire(&router.routes["m1"], 0, &k1);
        crate::metrics::tests::finish_with(&guard.metrics, 200);
        let text = runtime.encode_metrics().unwrap();
        assert!(text.contains(
            "lumewarp_requests_total{model=\"m1\",route=\"chat_completions\",deployment=\"a\",team=\"t1\",status=\"200\"} 1"
        ));
        assert!(!text.contains("team=\"t2\""));
    }

    fn hash(secret: &str) -> [u8; 32] {
        <sha2::Sha256 as sha2::Digest>::digest(secret.as_bytes()).into()
    }

    /// `n` aliases on one deployment; team t1's key may only call `m0`.
    fn many_routes(n: usize) -> Snapshot {
        Snapshot {
            upstreams: vec![upstream("u")],
            deployments: vec![deployment("a", "u")],
            routes: (0..n).map(|i| route(&format!("m{i}"), &["a"])).collect(),
            teams: vec![crate::snapshot::tests::team("t1")],
            api_keys: vec![key("k1", "s1", "t1", &["m0"])],
            ..Default::default()
        }
    }

    #[test]
    fn handle_cache_is_sparse_and_survives_swaps() {
        use crate::test_alloc::count;
        let mut first_use = Vec::new();
        for n in [1, 2000] {
            let runtime = Runtime::new();
            let s = many_routes(n);
            let router = Router::compile(&s, AuthMode::RequireKey, &runtime).unwrap();
            let caller = Caller::Key(router.keys[&hash("s1")].clone());
            // Warm this thread's lock-free map state with an unrelated entry.
            drop(router.acquire(&router.routes["m0"], 2, &Caller::Anonymous));
            // Create the registry series up front: their cost depends on the
            // registry's size (map resizes), not on the handle cache, which is
            // what this test isolates.
            drop(runtime.metrics.handles("m0", "a", 0, "t1"));
            let (first, guard) = count(|| router.acquire(&router.routes["m0"], 0, &caller));
            drop(guard);
            first_use.push(first);
            let (warm, guard) = count(|| router.acquire(&router.routes["m0"], 0, &caller));
            drop(guard);
            assert_eq!(warm, (0, 0), "warm acquire allocated ({n} routes)");
            let (cold, handles) = count(|| router.rejected_model(&caller, "m0", 0));
            drop(handles);
            assert!(cold.0 > 0, "first use of another label set creates it");
            // Key-only swap: already-used pairs need no allocation at all.
            let mut next = s.clone();
            next.version = 2;
            next.api_keys.push(key("k2", "s2", "t1", &["m0"]));
            let swapped = Router::compile(&next, AuthMode::RequireKey, &runtime).unwrap();
            for secret in ["s1", "s2"] {
                let caller = Caller::Key(swapped.keys[&hash(secret)].clone());
                let (after, guard) = count(|| swapped.acquire(&swapped.routes["m0"], 0, &caller));
                drop(guard);
                assert_eq!(after, (0, 0), "acquire after swap allocated ({n} routes)");
                let (after, handles) = count(|| swapped.rejected_model(&caller, "m0", 0));
                drop(handles);
                assert_eq!(after, (0, 0));
            }
        }
        assert!(first_use[0].0 > 0);
        // Same allocations and bytes for 1 and 2000 routes: nothing per route.
        assert_eq!(
            first_use[0], first_use[1],
            "first use (allocations, bytes): 1 route vs 2000 routes"
        );
    }

    #[test]
    fn deployments_keep_their_metering_context() {
        let runtime = Runtime::new();
        let mut s = snapshot(RoutingStrategy::Weighted);
        let mut passthrough = upstream("p");
        passthrough.stream_usage_mode = pb::StreamUsageMode::Passthrough as i32;
        s.upstreams.push(passthrough);
        s.deployments[0].price = Some(pb::Price {
            input_nano_usd_per_mtok: 150_000_000,
            output_nano_usd_per_mtok: 600_000_000,
        });
        s.deployments[1].upstream = "p".into();
        s.routes = vec![route("paid", &["a"]), route("free", &["b"])];
        let router = Router::compile(&s, AuthMode::Open, &runtime).unwrap();
        let paid = router.acquire(&router.routes["paid"], 0, &Caller::Anonymous);
        assert_eq!(paid.deployment.price, s.deployments[0].price);
        assert_eq!(
            paid.deployment.stream_usage_mode,
            pb::StreamUsageMode::IncludeUsage,
            "UNSPECIFIED resolves to INCLUDE_USAGE"
        );
        let other = router.acquire(&router.routes["free"], 0, &Caller::Anonymous);
        assert_eq!(other.deployment.price, None);
        assert_eq!(
            other.deployment.stream_usage_mode,
            pb::StreamUsageMode::Passthrough
        );
        // A new price applies to new requests only; the in-flight guard keeps
        // the price its request started with.
        let mut next = s.clone();
        next.version = 2;
        next.deployments[0].price = Some(pb::Price {
            input_nano_usd_per_mtok: 1,
            output_nano_usd_per_mtok: 2,
        });
        let swapped = Router::compile(&next, AuthMode::Open, &runtime).unwrap();
        drop(router);
        assert_eq!(paid.deployment.price, s.deployments[0].price);
        let fresh = swapped.acquire(&swapped.routes["paid"], 0, &Caller::Anonymous);
        assert_eq!(fresh.deployment.price, next.deployments[0].price);
        assert_eq!(fresh.deployment.in_flight.load(Ordering::Relaxed), 2);
    }

    fn split_snapshot(sticky: pb::StickyBy, strategy: RoutingStrategy) -> Snapshot {
        let mut s = snapshot(strategy);
        s.deployments.push(deployment("c", "u"));
        let r = &mut s.routes[0];
        r.deployments.clear();
        r.traffic_split = vec![
            crate::snapshot::tests::split("stable", &["a"], 9),
            crate::snapshot::tests::split("canary", &["b", "c"], 1),
        ];
        r.sticky = sticky as i32;
        if sticky == pb::StickyBy::Header {
            r.sticky_header = "x-session".into();
        }
        s.required_capabilities = vec!["traffic_split".into()];
        for cap in ["lowest_latency", "vllm_load"] {
            s.required_capabilities.push(cap.into());
        }
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
        let router = Router::compile(
            &split_snapshot(pb::StickyBy::None, RoutingStrategy::Weighted),
            AuthMode::Open,
            &runtime,
        )
        .unwrap();
        let route = &router.routes["public"];
        let mut canary = 0;
        for _ in 0..10_000 {
            let (guard, _, split) = router
                .acquire_initial(
                    route,
                    0,
                    &Caller::Anonymous,
                    &HeaderMap::new(),
                    &mut Skipped::default(),
                )
                .unwrap();
            assert_eq!(split == 1, guard.deployment.name != "a");
            canary += usize::from(split == 1);
        }
        assert!((900..=1100).contains(&canary), "canary got {canary}");
        let text = runtime.encode_metrics().unwrap();
        assert!(text.contains(&format!(
            "lumewarp_split_requests_total{{model=\"public\",split=\"canary\"}} {canary}"
        )));
    }

    #[test]
    fn sticky_splits_are_stable_and_follow_weights() {
        let router = compile(&split_snapshot(
            pb::StickyBy::Header,
            RoutingStrategy::Weighted,
        ));
        let route = &router.routes["public"];
        let anonymous = Caller::Anonymous;
        let mut canary = 0;
        for user in 0..10_000 {
            let h = headers(&format!("session-{user}"));
            let first = route.split_for(&h, &anonymous);
            for _ in 0..3 {
                assert_eq!(route.split_for(&h, &anonymous), first);
            }
            canary += usize::from(first == 1);
        }
        assert!((850..=1150).contains(&canary), "canary got {canary}");
        // Fixed-seed hash: the same session lands in the same split in every
        // process (golden value).
        assert_eq!(route.split_for(&headers("session-1"), &anonymous), 0);
        // Missing header: not sticky, but still split by weight.
        let mut seen = [false; 2];
        for _ in 0..1000 {
            seen[route.split_for(&HeaderMap::new(), &anonymous)] = true;
        }
        assert_eq!(seen, [true, true]);
    }

    #[test]
    fn sticky_by_team_and_key() {
        let mut s = split_snapshot(pb::StickyBy::Team, RoutingStrategy::Weighted);
        s.teams = vec![crate::snapshot::tests::team("t1")];
        s.api_keys = vec![
            key("k1", "s1", "t1", &["public"]),
            key("k2", "s2", "t1", &["public"]),
        ];
        let router = Router::compile(&s, AuthMode::RequireKey, &Runtime::new()).unwrap();
        let k1 = Caller::Key(router.keys[&hash("s1")].clone());
        let k2 = Caller::Key(router.keys[&hash("s2")].clone());
        let route = &router.routes["public"];
        let none = HeaderMap::new();
        let team = route.split_for(&none, &k1);
        for _ in 0..100 {
            assert_eq!(route.split_for(&none, &k1), team);
            assert_eq!(route.split_for(&none, &k2), team, "same team, same split");
        }
        s.routes[0].sticky = pb::StickyBy::Key as i32;
        let router = Router::compile(&s, AuthMode::RequireKey, &Runtime::new()).unwrap();
        let route = &router.routes["public"];
        let k1 = Caller::Key(router.keys[&hash("s1")].clone());
        let first = route.split_for(&none, &k1);
        for _ in 0..100 {
            assert_eq!(route.split_for(&none, &k1), first);
        }
    }

    #[test]
    fn retries_stay_in_the_split_then_move_to_other_splits() {
        let router = compile(&split_snapshot(
            pb::StickyBy::Header,
            RoutingStrategy::Weighted,
        ));
        let route = &router.routes["public"];
        let anonymous = Caller::Anonymous;
        // Find a session that the hash puts into the canary split.
        let h = (0..)
            .map(|i| headers(&format!("u{i}")))
            .find(|h| route.split_for(h, &anonymous) == 1)
            .unwrap();
        let (first, _, split) = router
            .acquire_initial(route, 0, &anonymous, &h, &mut Skipped::default())
            .unwrap();
        assert_eq!(split, 1);
        let label = |name: &str| router.runtime.metrics.label_id(name);
        let other = if first.deployment.name == "b" {
            "c"
        } else {
            "b"
        };
        assert_ne!(first.deployment.name, "a");
        let tried = [first.deployment.label];
        for _ in 0..50 {
            let (next, fallback) = router
                .retry(
                    route,
                    0,
                    &anonymous,
                    &h,
                    &tried,
                    split,
                    &mut Skipped::default(),
                )
                .unwrap();
            assert!(fallback.is_none());
            assert_eq!(next.deployment.name, other, "stays inside the canary split");
        }
        let tried = [label("b"), label("c")];
        let (next, _) = router
            .retry(
                route,
                0,
                &anonymous,
                &h,
                &tried,
                split,
                &mut Skipped::default(),
            )
            .unwrap();
        assert_eq!(next.deployment.name, "a", "then the other split");
    }

    #[test]
    fn lowest_latency_prefers_fast_and_explores_unmeasured() {
        fastrand::seed(11);
        let router = compile(&snapshot(RoutingStrategy::LowestLatency));
        let route = &router.routes["public"];
        let pool = &route.pools[0];
        assert!(pool.deployments.iter().all(|d| d.track_latency));
        // Nothing measured: both are "best" and get explored.
        let mut a = 0;
        for _ in 0..1000 {
            a += usize::from(router.acquire(route, 0, &Caller::Anonymous).deployment.name == "a");
        }
        assert!((350..650).contains(&a), "{a}");
        let count = |name: &str| {
            (0..1000)
                .filter(|_| router.acquire(route, 0, &Caller::Anonymous).deployment.name == name)
                .count()
        };
        pool.deployments[0]
            .latency
            .success(Duration::from_millis(5));
        // b is unmeasured, so it still wins against a measured deployment;
        // only exploration (5%, half of it to a) reaches a.
        assert!(count("b") >= 940);
        pool.deployments[1]
            .latency
            .success(Duration::from_millis(50));
        let fast = count("a");
        assert!(fast >= 940, "fast deployment got {fast}/1000");
        assert!(fast < 1000, "exploration keeps measuring the slow one");
        // Cost scales with queued work (peak-EWMA style): concurrent load
        // spreads roughly inversely to latency (10:1 here) instead of piling
        // everything onto the fastest deployment.
        let held: Vec<_> = (0..44)
            .map(|_| router.acquire(route, 0, &Caller::Anonymous))
            .collect();
        let busy = |i: usize| pool.deployments[i].in_flight.load(Ordering::Relaxed);
        assert!((1..=7).contains(&busy(1)), "a={} b={}", busy(0), busy(1));
        drop(held);
    }

    #[test]
    fn vllm_load_avoids_congestion_and_falls_back_when_stale() {
        fastrand::seed(3);
        let router = compile(&snapshot(RoutingStrategy::VllmLoad));
        let route = &router.routes["public"];
        let pool = &route.pools[0];
        let count = |name: &str| {
            (0..1000)
                .filter(|_| router.acquire(route, 0, &Caller::Anonymous).deployment.name == name)
                .count()
        };
        let now = clock::mono_ms();
        pool.deployments[0].load.set(50.0, 0.5, now);
        pool.deployments[1].load.set(0.0, 0.5, now);
        let congested = count("a");
        assert!(
            congested <= 100,
            "congested deployment got {congested}/1000"
        );
        // Equal queues: KV-cache usage decides.
        pool.deployments[0].load.set(0.0, 0.95, now);
        pool.deployments[1].load.set(0.0, 0.10, now);
        assert_eq!(count("b"), 1000);
        // Equal load: in-flight decides.
        pool.deployments[0].load.set(0.0, 0.1, now);
        pool.deployments[1]
            .in_flight
            .fetch_add(3, Ordering::Relaxed);
        assert_eq!(count("a"), 1000);
        // a's scrape failed: comparisons with it use in-flight only, so the
        // (stale) queue no longer protects it.
        pool.deployments[0].load.set(99.0, 1.0, now);
        pool.deployments[0].load.fail();
        assert_eq!(count("a"), 1000);
        pool.deployments[1]
            .in_flight
            .fetch_sub(3, Ordering::Relaxed);
        // Fresh again but old: older than the staleness bound is ignored.
        pool.deployments[0]
            .load
            .set(99.0, 1.0, now.saturating_sub(balance::LOAD_STALE_MS + 1));
        pool.deployments[0]
            .in_flight
            .fetch_add(1, Ordering::Relaxed);
        assert_eq!(count("b"), 1000);
        pool.deployments[0]
            .in_flight
            .fetch_sub(1, Ordering::Relaxed);
    }

    #[test]
    fn scrape_targets_and_metrics_urls() {
        let mut s = snapshot(RoutingStrategy::VllmLoad);
        s.upstreams.push(upstream("idle"));
        s.deployments.push(deployment("c", "idle"));
        let router = compile(&s);
        assert_eq!(
            router.scrapes.len(),
            1,
            "only upstreams behind vllm_load routes"
        );
        let scrape = &router.scrapes[0];
        assert_eq!(scrape.uri, "http://localhost:8000/metrics");
        assert_eq!(scrape.targets.len(), 2);
        assert!(!router.routes["public"].pools[0].deployments[0].track_latency);
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
        assert!(Router::compile(&s, AuthMode::Open, &Runtime::new()).is_err());
    }

    #[test]
    fn load_aware_and_sticky_picks_do_not_allocate() {
        use crate::test_alloc::count;
        for (sticky, strategy) in [
            (pb::StickyBy::None, RoutingStrategy::LowestLatency),
            (pb::StickyBy::None, RoutingStrategy::VllmLoad),
            (pb::StickyBy::Header, RoutingStrategy::LowestLatency),
            (pb::StickyBy::Header, RoutingStrategy::VllmLoad),
        ] {
            let router = compile(&split_snapshot(sticky, strategy));
            let route = &router.routes["public"];
            let h = headers("session-1");
            for d in route.pools.iter().flat_map(|p| &p.deployments) {
                d.latency.success(Duration::from_millis(1));
                d.load.set(1.0, 0.5, clock::mono_ms());
            }
            // Warm every handle the picks below can reach.
            for _ in 0..200 {
                drop(router.acquire_initial(
                    route,
                    0,
                    &Caller::Anonymous,
                    &h,
                    &mut Skipped::default(),
                ));
                drop(router.acquire(route, 0, &Caller::Anonymous));
            }
            let (warm, guard) = count(|| {
                router.acquire_initial(route, 0, &Caller::Anonymous, &h, &mut Skipped::default())
            });
            drop(guard);
            assert_eq!(warm, (0, 0), "{sticky:?} {strategy:?}");
            let tried = [route.pools[1].deployments[0].label];
            let (retry, guard) = count(|| {
                router.retry(
                    route,
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

    /// Two breaker-guarded deployments on upstream `u` (threshold 1).
    fn guarded(open_ms: u32) -> Snapshot {
        let mut s = snapshot(RoutingStrategy::Weighted);
        s.required_capabilities = vec!["circuit_breaker".into()];
        s.upstreams[0].circuit_breaker = Some(CircuitBreaker {
            failure_threshold: 1,
            open_ms,
        });
        s
    }

    fn open(d: &Deployment, now: u64) {
        d.breaker.finish(d.circuit.unwrap(), false, true, now);
    }

    #[test]
    fn fail_open_skips_a_deployment_whose_trial_is_running() {
        let router = compile(&guarded(30_000));
        let route = &router.routes["public"];
        let (a, b) = (
            &route.pools[0].deployments[0],
            &route.pools[0].deployments[1],
        );
        // a: open since t=0, its single half-open trial now in progress.
        open(a, 0);
        assert_eq!(a.breaker.reserve(30_002), Some(true));
        // b: freshly open. Neither is eligible for strict selection.
        open(b, clock::mono_ms());
        for _ in 0..200 {
            let (guard, fallback, _) = router
                .acquire_initial(
                    route,
                    0,
                    &Caller::Anonymous,
                    &HeaderMap::new(),
                    &mut Skipped::default(),
                )
                .expect("fails open onto the deployment without a running trial");
            assert!(fallback.is_none());
            assert_eq!(guard.deployment.name, "b");
        }
        assert!(a.breaker.trial_in_progress(), "the trial is left alone");
    }

    #[test]
    fn circuit_open_is_counted_once_per_request_under_the_root_model() {
        let runtime = Runtime::new();
        let mut s = guarded(30_000);
        s.required_capabilities
            .extend(["retry".into(), "fallbacks".into()]);
        s.routes = vec![route("public", &["a"]), route("backup", &["b"])];
        s.routes[0].fallbacks = vec!["backup".into()];
        s.routes[0].retry = Some(RetryPolicy::default());
        let router = Router::compile(&s, AuthMode::Open, &runtime).unwrap();
        let route = &router.routes["public"];
        let now = clock::mono_ms();
        open(&route.pools[0].deployments[0], now);
        open(&router.routes["backup"].pools[0].deployments[0], now);
        let count = |model: &str, deployment: &str| {
            let series = format!(
                "lumewarp_upstream_attempts_total{{model=\"{model}\",deployment=\"{deployment}\",outcome=\"circuit_open\"}} "
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
            let (guard, _, split) = router
                .acquire_initial(route, 0, &anonymous, &h, &mut skipped)
                .unwrap();
            let tried = [guard.deployment.label];
            for _ in 0..3 {
                drop(router.retry(route, 0, &anonymous, &h, &tried, split, &mut skipped));
            }
            drop(guard);
            assert_eq!(count("public", "a"), request);
            assert_eq!(count("public", "b"), request, "fallback deployments too");
            assert_eq!(count("backup", "b"), 0, "labelled with the client model");
        }
    }

    #[test]
    fn removed_breaker_config_resets_breaker_on_activation() {
        let runtime = Runtime::new();
        let s = guarded(30_000);
        let router = Router::compile(&s, AuthMode::Open, &runtime).unwrap();
        let a = router.routes["public"].pools[0].deployments[0].clone();
        open(&a, clock::mono_ms());
        let gauge = |runtime: &Arc<Runtime>| {
            runtime
                .encode_metrics()
                .unwrap()
                .lines()
                .find_map(|l| {
                    l.strip_prefix("lumewarp_upstream_circuit_state{deployment=\"a\"} ")
                        .map(str::to_owned)
                })
                .unwrap()
        };
        assert_eq!(gauge(&runtime), "2");
        let mut next = s.clone();
        next.version = 2;
        next.upstreams[0].circuit_breaker = None;
        next.required_capabilities.clear();
        let swapped = Router::compile(&next, AuthMode::Open, &runtime).unwrap();
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
        s.required_capabilities = vec!["health_check".into()];
        s.upstreams[0].base_url = format!("http://127.0.0.1:{port}/v1");
        s.upstreams[0].health_check = Some(pb::HealthCheck {
            interval_ms: 1000,
            timeout_ms: 100,
            unhealthy_threshold: 3,
            ..Default::default()
        });
        let router = Router::compile(&s, AuthMode::RequireKey, &runtime).unwrap();
        assert_eq!(router.activate_health(), 1);
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
            let next = Router::compile(&s, AuthMode::RequireKey, &runtime).unwrap();
            assert_eq!(next.activate_health(), 0, "unchanged probe kept running");
        }
        assert!(version >= 3, "several deltas were applied while probing");
        // A changed probe is restarted; a removed one is stopped.
        s.upstreams[0]
            .health_check
            .as_mut()
            .unwrap()
            .unhealthy_threshold = 4;
        let changed = Router::compile(&s, AuthMode::RequireKey, &runtime).unwrap();
        assert_eq!(changed.activate_health(), 1);
        s.upstreams[0].health_check = None;
        let removed = Router::compile(&s, AuthMode::RequireKey, &runtime).unwrap();
        assert_eq!(removed.activate_health(), 0);
        assert!(
            healthy.load(Ordering::Acquire),
            "unprobed upstreams count as up"
        );
    }
}
