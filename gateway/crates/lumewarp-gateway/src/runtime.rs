//! Process-lifetime state shared by every compiled `RoutingTable`. Snapshot swaps
//! replace the routing table, never this: counters keyed by stable names/ids survive.

use std::{
    collections::HashMap,
    sync::{atomic::AtomicUsize, atomic::Ordering, Arc, Mutex},
};

use crate::{balance, health, metrics::Metrics, proxy, ratelimit, usage::UsageSink};

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
}

impl Runtime {
    /// Standalone: fresh metrics, usage disabled.
    pub fn new() -> Arc<Self> {
        Self::with_usage(Arc::new(Metrics::new()), UsageSink::disabled())
    }

    /// Control plane: `metrics` is the `Arc` given to `spawn_reporter`.
    pub fn with_usage(metrics: Arc<Metrics>, usage: UsageSink) -> Arc<Self> {
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
