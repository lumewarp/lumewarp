use std::{
    collections::HashMap,
    sync::{
        atomic::{AtomicUsize, Ordering},
        Arc,
    },
    time::Duration,
};

use anyhow::{bail, Context};
use bytes::Bytes;
use hyper::{header::HeaderValue, Uri};
use lumewarp_config::{Config, RoutingStrategy};

use crate::{
    auth::Auth,
    metrics::{Metrics, RequestMetrics, ROUTES},
};

pub struct Deployment {
    pub name: String,
    // Each absolute URI contains its precompiled endpoint PathAndQuery.
    pub endpoints: [Uri; 3],
    pub host: HeaderValue,
    pub authorization: Option<HeaderValue>,
    pub upstream_model_json: Bytes,
    pub timeout: Duration,
    pub in_flight: AtomicUsize,
}

pub struct Route {
    deployments: Vec<Arc<Deployment>>,
    cumulative_weights: Vec<u64>,
    total_weight: u64,
    strategy: RoutingStrategy,
    metrics: Vec<[Arc<RequestMetrics>; 3]>,
}

impl Route {
    #[cfg(test)]
    pub fn acquire(&self) -> InFlight {
        self.acquire_for(0)
    }

    pub fn acquire_for(&self, endpoint: usize) -> InFlight {
        let index = match self.strategy {
            RoutingStrategy::Weighted => self.weighted_index(fastrand::u64(0..self.total_weight)),
            RoutingStrategy::LeastInFlight => {
                let len = self.deployments.len();
                let start = fastrand::usize(0..len);
                (0..len)
                    .map(|offset| (start + offset) % len)
                    .min_by_key(|&i| self.deployments[i].in_flight.load(Ordering::Relaxed))
                    .expect("validated nonempty route")
            }
            RoutingStrategy::LowestLatency => unreachable!("rejected at load time"),
        };
        let deployment = self.deployments[index].clone();
        deployment.in_flight.fetch_add(1, Ordering::Relaxed);
        InFlight {
            deployment,
            metrics: self.metrics[index][endpoint].clone(),
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
    pub routes: HashMap<String, Route>,
    pub auth: Auth,
    pub models: Bytes,
    pub metrics: Arc<Metrics>,
    pub rejected: [Arc<RequestMetrics>; ROUTES.len()],
    pub unknown: [Arc<RequestMetrics>; 4],
    pub body_read_timeout: Duration,
}

impl Router {
    pub fn compile(config: &Config) -> anyhow::Result<Self> {
        let mut metrics = Metrics::new();
        let mut upstreams = HashMap::new();
        for upstream in &config.upstreams {
            let base: Uri = upstream
                .base_url
                .parse()
                .context("invalid upstream base URL")?;
            if !matches!(base.scheme_str(), Some("http" | "https"))
                || base.authority().is_none()
                || base.authority().is_some_and(|a| a.as_str().contains('@'))
                || base.query().is_some()
            {
                bail!(
                    "upstream {:?} requires an http(s) base URL without userinfo or query",
                    upstream.name
                );
            }
            let authorization = upstream
                .api_key_env
                .as_ref()
                .map(|name| {
                    // Do not retain VarError::NotUnicode: its Display contains the secret.
                    let key = std::env::var(name)
                        .map_err(|_| anyhow::anyhow!("missing or invalid api_key_env {name:?}"))?;
                    let mut value = HeaderValue::from_str(&format!("Bearer {key}"))
                        .context("invalid upstream authorization header")?;
                    value.set_sensitive(true);
                    Ok::<_, anyhow::Error>(value)
                })
                .transpose()?;
            let host =
                HeaderValue::from_str(base.authority().expect("validated authority").as_str())
                    .context("invalid upstream Host header")?;
            let endpoint = |suffix: &str| -> anyhow::Result<Uri> {
                let mut parts = base.clone().into_parts();
                parts.path_and_query =
                    Some(format!("{}{suffix}", base.path().trim_end_matches('/')).parse()?);
                Ok(Uri::from_parts(parts)?)
            };
            let endpoints = [
                endpoint("/chat/completions")?,
                endpoint("/completions")?,
                endpoint("/embeddings")?,
            ];
            upstreams.insert(
                upstream.name.as_str(),
                (
                    endpoints,
                    host,
                    authorization,
                    Duration::from_millis(upstream.timeout_ms.into()),
                ),
            );
        }
        let mut deployments = HashMap::new();
        for d in &config.deployments {
            let (endpoints, host, authorization, timeout) = upstreams
                .get(d.upstream.as_str())
                .context("unknown upstream")?;
            deployments.insert(
                d.name.as_str(),
                Arc::new(Deployment {
                    name: d.name.clone(),
                    endpoints: endpoints.clone(),
                    host: host.clone(),
                    authorization: authorization.clone(),
                    upstream_model_json: Bytes::from(serde_json::to_vec(&d.upstream_model)?),
                    timeout: *timeout,
                    // TODO(P1): reuse counters from a deployment-name registry across snapshot
                    // swaps; fresh counters otherwise ignore streams still using the old snapshot.
                    in_flight: AtomicUsize::new(0),
                }),
            );
        }
        for deployment in deployments.values() {
            metrics.register_deployment(deployment.clone());
        }
        let rejected = std::array::from_fn(|route| metrics.handles("", "", route));
        let unknown = std::array::from_fn(|route| metrics.handles("unknown", "", route));
        let mut routes = HashMap::new();
        for r in &config.routes {
            let mut resolved = Vec::new();
            let mut cumulative_weights = Vec::new();
            let mut total_weight = 0u64;
            for name in &r.deployments {
                resolved.push(
                    deployments
                        .get(name.as_str())
                        .context("unknown deployment")?
                        .clone(),
                );
                let weight = config
                    .deployments
                    .iter()
                    .find(|d| d.name == *name)
                    .context("unknown deployment weight")?
                    .weight;
                total_weight = total_weight
                    .checked_add(u64::from(weight))
                    .context("route weights overflow")?;
                cumulative_weights.push(total_weight);
            }
            if resolved.is_empty() || (r.strategy == RoutingStrategy::Weighted && total_weight == 0)
            {
                bail!(
                    "route {:?} must have deployments with positive total weight",
                    r.model
                );
            }
            let handles = resolved
                .iter()
                .map(|d| {
                    std::array::from_fn(|endpoint| metrics.handles(&r.model, &d.name, endpoint))
                })
                .collect();
            routes.insert(
                r.model.clone(),
                Route {
                    deployments: resolved,
                    cumulative_weights,
                    total_weight,
                    strategy: r.strategy,
                    metrics: handles,
                },
            );
        }
        let data: Vec<_> = config
            .routes
            .iter()
            .map(|r| {
                serde_json::json!({
                    "id": r.model, "object": "model", "created": 0, "owned_by": "lumewarp",
                })
            })
            .collect();
        Ok(Self {
            routes,
            metrics: Arc::new(metrics),
            rejected,
            unknown,
            body_read_timeout: Duration::from_millis(config.server.body_read_timeout_ms),
            auth: Auth::compile(&config.server.api_keys)?,
            models: Bytes::from(serde_json::to_vec(
                &serde_json::json!({"object": "list", "data": data}),
            )?),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn config(strategy: &str) -> Config {
        Config::from_yaml(&format!(
            r#"
upstreams:
  - {{name: u, kind: vllm, base_url: 'http://localhost:8000/v1'}}
deployments:
  - {{name: a, upstream: u, upstream_model: actual, weight: 1}}
  - {{name: b, upstream: u, upstream_model: actual, weight: 3}}
routes:
  - {{model: public, deployments: [a, b], strategy: {strategy}}}
"#
        ))
        .unwrap()
    }

    #[test]
    fn weighted_distribution() {
        let router = Router::compile(&config("weighted")).unwrap();
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
        let router = Router::compile(&config("least_in_flight")).unwrap();
        let route = &router.routes["public"];
        let first = route.acquire();
        let first_name = first.deployment.name.clone();
        let second = route.acquire();
        assert_ne!(second.deployment.name, first_name);
        drop(first);
        assert_eq!(route.acquire().deployment.name, first_name);
        drop(second);
        assert!(route
            .deployments
            .iter()
            .all(|d| d.in_flight.load(Ordering::Relaxed) == 0));
    }

    #[test]
    fn least_in_flight_ties_spread() {
        let router = Router::compile(&config("least_in_flight")).unwrap();
        let route = &router.routes["public"];
        let mut a = 0;
        for _ in 0..10000 {
            a += usize::from(route.acquire().deployment.name == "a");
        }
        assert!((4500..5500).contains(&a), "ties selected a {a} times");
    }

    #[test]
    fn missing_api_key_env_is_load_error() {
        let mut config = config("weighted");
        config.upstreams[0].api_key_env = Some("LUMEWARP_TEST_MISSING_KEY_8B3F590E".into());
        let err = Router::compile(&config).err().unwrap();
        assert!(err.to_string().contains("api_key_env"));
    }
}
