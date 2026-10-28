use std::{
    collections::{HashMap, HashSet},
    sync::{
        atomic::{AtomicUsize, Ordering},
        Arc,
    },
    time::Duration,
};

use bytes::Bytes;
use hyper::{header::HeaderValue, Uri};
use lumewarp_config::AuthMode;
use lumewarp_proto::config::v1::{
    KeyStatus, Price, RateLimit, RoutingStrategy, Snapshot, StreamUsageMode,
};

use crate::{
    auth::Caller,
    metrics::{HandleKey, LabelId, RequestMetrics, ROUTES},
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

pub struct Route {
    model: Box<str>,
    model_label: LabelId,
    deployments: Vec<Arc<Deployment>>,
    cumulative_weights: Vec<u64>,
    total_weight: u64,
    strategy: RoutingStrategy,
}

impl Route {
    fn pick(&self) -> usize {
        match self.strategy {
            RoutingStrategy::LeastInFlight => {
                let len = self.deployments.len();
                let start = fastrand::usize(0..len);
                (0..len)
                    .map(|offset| (start + offset) % len)
                    .min_by_key(|&i| self.deployments[i].in_flight.load(Ordering::Relaxed))
                    .expect("validated nonempty route")
            }
            // Validation rejects UNSPECIFIED and LOWEST_LATENCY.
            _ => self.weighted_index(fastrand::u64(0..self.total_weight)),
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
}

impl Drop for InFlight {
    fn drop(&mut self) {
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
                ),
            );
        }
        let metrics = &runtime.metrics;
        let mut deployments = HashMap::new();
        for d in &snapshot.deployments {
            let (endpoints, host, authorization, timeout, stream_usage_mode) =
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
        let mut routes = HashMap::new();
        for r in &snapshot.routes {
            let mut resolved = Vec::new();
            let mut cumulative_weights = Vec::new();
            let mut total_weight = 0u64;
            for name in &r.deployments {
                let (deployment, weight) = &deployments[name.as_str()];
                resolved.push(deployment.clone());
                total_weight = total_weight
                    .checked_add(u64::from(*weight))
                    .ok_or_else(|| invalid(format!("route {:?} weights overflow", r.model)))?;
                cumulative_weights.push(total_weight);
            }
            let strategy = RoutingStrategy::try_from(r.strategy).expect("validated strategy");
            if strategy == RoutingStrategy::Weighted && total_weight == 0 {
                return Err(invalid(format!(
                    "route {:?} must have deployments with positive total weight",
                    r.model
                )));
            }
            for deployment in std::iter::once("").chain(resolved.iter().map(|d| d.name.as_str())) {
                for endpoint in 0..ENDPOINTS {
                    drop(metrics.handles(&r.model, deployment, endpoint, ""));
                }
            }
            routes.insert(
                r.model.clone(),
                Route {
                    model: r.model.as_str().into(),
                    model_label: metrics.label_id(&r.model),
                    deployments: resolved,
                    cumulative_weights,
                    total_weight,
                    strategy,
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
    pub fn acquire(&self, route: &Route, endpoint: usize, caller: &Caller) -> InFlight {
        let deployment = route.deployments[route.pick()].clone();
        deployment.in_flight.fetch_add(1, Ordering::Relaxed);
        let metrics = self.handles(
            caller,
            (route.model_label, &route.model),
            (deployment.label, &deployment.name),
            endpoint,
        );
        InFlight {
            deployment,
            metrics,
        }
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
        Snapshot {
            upstreams: vec![upstream("u")],
            deployments: vec![a, b],
            routes: vec![public],
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
            a += usize::from(route.weighted_index(rng.u64(0..route.total_weight)) == 0);
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
        assert!(route
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
}
