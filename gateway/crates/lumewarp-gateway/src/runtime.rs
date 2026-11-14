//! Process-lifetime state shared by every compiled `RoutingTable`. Snapshot swaps
//! replace the routing table, never this: counters keyed by stable names/ids survive.

use std::{
    collections::HashMap,
    sync::{atomic::AtomicUsize, atomic::Ordering, Arc, Mutex},
};

use crate::{
    a2a_ids, balance, catalog, health, metrics::Metrics, proxy, ratelimit, usage::UsageSink,
};

/// Process-wide settings an operator gives the gateway through the environment
/// (`docs/plugins.md`), read once at startup and kept here as options: a
/// gateway without them still serves LLM traffic, but the A2A endpoint answers
/// 503 (P3).
#[derive(Debug, Default)]
pub struct EnvOptions {
    /// `LUMEWARP_GATEWAY_ID_KEY`: seals the A2A task/context ids (and, later,
    /// MCP session ids) so any gateway can verify them without shared state.
    pub id_key: Option<a2a_ids::IdKey>,
    /// `LUMEWARP_GATEWAY_PUBLIC_URL`, trailing slashes stripped: the base a
    /// rewritten Agent Card points at (`<public_url>/a2a/<agent>`, L6). Never
    /// taken from the `Host` header. (The control plane's own
    /// `LUMEWARP_PUBLIC_URL` is its OIDC redirect base, a different setting.)
    pub public_url: Option<Arc<str>>,
}

impl EnvOptions {
    /// Reads both variables. A malformed value is a misconfiguration: it is
    /// logged and treated as unset, so only the A2A endpoint answers 503
    /// (`a2a_ids::IdKey::from_env`, P3) — LLM and MCP traffic keep working.
    pub fn from_env() -> Self {
        let id_key = match a2a_ids::IdKey::from_env() {
            Ok(id_key) => id_key,
            Err(error) => {
                tracing::error!(error = %error, env = a2a_ids::ENV, "ignoring an invalid gateway id key; the A2A endpoint will answer 503");
                None
            }
        };
        let public_url = match std::env::var(PUBLIC_URL_ENV) {
            Ok(value) => match public_url(&value) {
                Ok(url) => url,
                Err(error) => {
                    tracing::error!(%error, "ignoring an invalid public URL; the A2A endpoint will answer 503");
                    None
                }
            },
            Err(_) => None,
        };
        Self { id_key, public_url }
    }
}

/// The variable holding the gateway's own external base URL (L6). Distinct
/// from the control plane's `LUMEWARP_PUBLIC_URL` (its OIDC redirect base), so
/// one deployment can set both.
pub const PUBLIC_URL_ENV: &str = "LUMEWARP_GATEWAY_PUBLIC_URL";

/// Normalizes `LUMEWARP_GATEWAY_PUBLIC_URL`: an absolute http(s) origin (and
/// optional path prefix) without a query string or userinfo, trailing slashes
/// stripped.
fn public_url(value: &str) -> Result<Option<Arc<str>>, String> {
    let normalized = value.trim().trim_end_matches('/');
    if normalized.is_empty() {
        return Ok(None);
    }
    crate::router::plain_http_uri(normalized)
        .filter(|uri| uri.query().is_none())
        .ok_or_else(|| {
            format!("{PUBLIC_URL_ENV} must be an absolute http(s) URL without a query or userinfo")
        })?;
    Ok(Some(Arc::from(normalized)))
}

struct PluginMetrics(HashMap<String, HashMap<String, crate::metrics::PluginHandles>>);
impl lumewarp_plugin_host::Metrics for PluginMetrics {
    fn record(&self, kind: &str, plugin: &str, result: &str, seconds: f64) {
        if let Some(handle) = self.0.get(kind).and_then(|plugins| plugins.get(plugin)) {
            handle.record(result, std::time::Duration::from_secs_f64(seconds));
        }
    }
}

pub struct Runtime {
    /// Shared with the usage reporter, which exists before the runtime.
    pub metrics: Arc<Metrics>,
    pub inflight: InFlightRegistry,
    pub limiters: ratelimit::Registry,
    /// Fixed at construction, before serving: `UsageSink::disabled()` in
    /// standalone mode, the `spawn_reporter` sink in control-plane mode.
    pub usage: UsageSink,
    pub health: health::Registry,
    pub retry_budget: health::RetryBudget,
    /// Latency EWMAs, scraped vLLM load and scrape tasks.
    pub balance: balance::Registry,
    pub client: proxy::UpstreamClient,
    /// Observed definitions and agent cards waiting for the next
    /// `CatalogReport` on the config stream (`catalog::Registry`).
    pub catalog: catalog::Registry,
    /// The last definition hash the upstream sent per (server, kind, name): the
    /// MCP call gate (L1), pruned on every snapshot apply.
    #[allow(dead_code)] // T4 reads it; unit tests do not.
    pub observed: catalog::Observed,
    /// `LUMEWARP_GATEWAY_ID_KEY`, when set and valid: A2A id wrapping
    /// (`a2a_ids`) and, from T4, MCP session sealing (`derive_hmac_key`).
    pub id_key: Option<a2a_ids::IdKey>,
    /// `LUMEWARP_GATEWAY_PUBLIC_URL`, when set: `<public_url>/a2a/<agent>` is what a
    /// rewritten Agent Card points at (L6). Trailing slashes are stripped; the
    /// value is never taken from the `Host` header.
    pub public_url: Option<Arc<str>>,
}

impl Runtime {
    /// Standalone: fresh metrics, usage disabled, no environment options.
    /// Tests use this so a developer's environment never changes behaviour;
    /// a real process calls [`Self::from_env`].
    #[cfg_attr(not(test), allow(dead_code))]
    pub fn new() -> Arc<Self> {
        Self::with_usage(Arc::new(Metrics::new()), UsageSink::disabled())
    }

    /// Standalone with shared limits: registers the Redis-sync metrics when a
    /// Redis URL is configured (the task itself is spawned by `main`).
    pub fn from_env_shared_limits(shared_limits: bool) -> Arc<Self> {
        Self::with_usage_and_env(
            Arc::new(Metrics::with_shared_limits(shared_limits)),
            UsageSink::disabled(),
            EnvOptions::from_env(),
        )
    }

    /// Control plane: `metrics` is the `Arc` given to `spawn_reporter`.
    #[cfg_attr(not(test), allow(dead_code))] // tests only; the process uses `with_usage_and_env`.
    pub fn with_usage(metrics: Arc<Metrics>, usage: UsageSink) -> Arc<Self> {
        Self::with_usage_and_env(metrics, usage, EnvOptions::default())
    }

    pub fn with_usage_and_env(
        metrics: Arc<Metrics>,
        usage: UsageSink,
        env: EnvOptions,
    ) -> Arc<Self> {
        let mut handles: HashMap<String, HashMap<String, crate::metrics::PluginHandles>> =
            HashMap::new();
        for descriptor in lumewarp_plugin_host::global()
            .descriptors()
            .into_iter()
            .filter(|d| !d.builtin)
        {
            let kind = match descriptor.kind {
                1 => "router-strategy",
                2 => "upstream-auth",
                _ => "a2a-middleware",
            };
            handles.entry(kind.into()).or_default().insert(
                descriptor.name.clone(),
                metrics.plugin_handles(kind, &descriptor.name),
            );
        }
        // One runtime per serving process; tests may construct several native-only runtimes.
        let _ = lumewarp_plugin_host::install_metrics(Arc::new(PluginMetrics(handles)));
        Arc::new(Self {
            catalog: catalog::Registry::with_drops(metrics.clone()),
            observed: catalog::Observed::new(),
            id_key: env.id_key,
            public_url: env.public_url,
            metrics,
            inflight: InFlightRegistry::default(),
            limiters: ratelimit::Registry::new(),
            usage,
            health: health::Registry::default(),
            retry_budget: health::RetryBudget::new(),
            balance: balance::Registry::default(),
            client: proxy::client(),
        })
    }

    pub fn encode_metrics(&self) -> Result<String, std::fmt::Error> {
        self.metrics.export_latency(self.balance.latencies());
        self.metrics.encode(&self.inflight)
    }
}

/// Per-model in-flight counters keyed by model name. A recompiled
/// model with the same name shares the counter, so streams still running
/// on an old snapshot keep counting toward least-in-flight decisions.
///
/// Entries are never removed: the set is bounded by the model names ever
/// configured, and a removed model's counter drains to zero as its
/// remaining requests finish.
#[derive(Default)]
pub struct InFlightRegistry {
    counters: Mutex<HashMap<Arc<str>, Arc<AtomicUsize>>>,
}

impl InFlightRegistry {
    /// Compile-time only; requests use the `Arc` cached in their model.
    pub fn counter(&self, model: &str) -> Arc<AtomicUsize> {
        let mut counters = self.counters.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(counter) = counters.get(model) {
            return counter.clone();
        }
        let counter = Arc::new(AtomicUsize::new(0));
        counters.insert(model.into(), counter.clone());
        counter
    }

    pub fn counts(&self) -> Vec<(Arc<str>, usize)> {
        self.counters
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .iter()
            .map(|(name, counter)| (name.clone(), counter.load(Ordering::Relaxed)))
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn public_url_is_an_absolute_origin_without_a_query() {
        assert_eq!(public_url("").unwrap(), None);
        assert_eq!(public_url("  ").unwrap(), None);
        assert_eq!(
            &*public_url("https://gw.example.test/").unwrap().unwrap(),
            "https://gw.example.test"
        );
        assert_eq!(
            &*public_url("http://127.0.0.1:4000").unwrap().unwrap(),
            "http://127.0.0.1:4000"
        );
        // A path prefix is allowed (the gateway may sit behind one).
        assert_eq!(
            &*public_url("https://example.test/lumewarp/")
                .unwrap()
                .unwrap(),
            "https://example.test/lumewarp"
        );
        for bad in [
            "gw.example.test",
            "/a2a",
            "ftp://gw.example.test",
            "https://user:pw@gw.example.test",
            "https://gw.example.test/?x=1",
        ] {
            assert!(public_url(bad).is_err(), "{bad}");
        }
    }

    /// F2: the gateway's variable is `LUMEWARP_GATEWAY_PUBLIC_URL`. The control
    /// plane's `LUMEWARP_PUBLIC_URL` (its OIDC redirect base) must not be read
    /// by a gateway that shares the process environment.
    #[test]
    fn only_the_gateway_public_url_variable_is_read() {
        std::env::set_var("LUMEWARP_PUBLIC_URL", "https://control-plane.example.test");
        std::env::set_var(PUBLIC_URL_ENV, "https://gateway.example.test/");
        let options = EnvOptions::from_env();
        assert_eq!(
            &*options.public_url.expect("the gateway variable is set"),
            "https://gateway.example.test"
        );
        std::env::remove_var(PUBLIC_URL_ENV);
        let options = EnvOptions::from_env();
        assert!(
            options.public_url.is_none(),
            "the control plane's variable is not the gateway's"
        );
        std::env::remove_var("LUMEWARP_PUBLIC_URL");
    }

    #[test]
    fn counters_are_shared_by_name() {
        let registry = InFlightRegistry::default();
        let a = registry.counter("d");
        a.fetch_add(2, Ordering::Relaxed);
        assert_eq!(registry.counter("d").load(Ordering::Relaxed), 2);
        assert_eq!(registry.counter("e").load(Ordering::Relaxed), 0);
        let mut counts = registry.counts();
        counts.sort();
        assert_eq!(counts, [("d".into(), 2), ("e".into(), 0)]);
    }
}
