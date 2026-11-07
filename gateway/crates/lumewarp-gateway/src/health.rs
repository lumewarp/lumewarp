//! Passive per-model breakers and off-path active upstream probes.
use std::{
    collections::HashMap,
    sync::{
        atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering},
        Arc, Mutex,
    },
    time::Duration,
};

use bytes::Bytes;
use http_body_util::{BodyExt, Full};
use hyper::{header::HeaderValue, Request, Uri};
use lumewarp_proto::config::v1::{CircuitBreaker, HealthCheck};
use prometheus_client::metrics::gauge::Gauge;
use tokio::{sync::Notify, task::JoinHandle};

use crate::{proxy::UpstreamClient, ratelimit::clock};

pub struct Breaker {
    // 0 closed, 1 half-open, otherwise open until (value - 2) monotonic ms.
    state: AtomicU64,
    failures: AtomicU32,
    pub retry_after: AtomicU64,
    gauge: Gauge,
}

impl Breaker {
    pub fn new(gauge: Gauge) -> Self {
        Self {
            state: AtomicU64::new(0),
            failures: AtomicU32::new(0),
            retry_after: AtomicU64::new(0),
            gauge,
        }
    }

    pub fn available(&self, now: u64) -> bool {
        let state = self.state.load(Ordering::Acquire);
        state == 0 || (state > 1 && now >= state - 2)
    }

    pub fn trial_in_progress(&self) -> bool {
        self.state.load(Ordering::Acquire) == 1
    }

    pub fn abandon_trial(&self, now: u64) {
        if self
            .state
            .compare_exchange(
                1,
                now.saturating_add(2),
                Ordering::AcqRel,
                Ordering::Relaxed,
            )
            .is_ok()
        {
            self.gauge.set(2);
        }
    }

    /// Returns whether this reservation owns the single half-open trial.
    pub fn reserve(&self, now: u64) -> Option<bool> {
        let state = self.state.load(Ordering::Acquire);
        if state == 0 {
            return Some(false);
        }
        if state > 1
            && now >= state - 2
            && self
                .state
                .compare_exchange(state, 1, Ordering::AcqRel, Ordering::Relaxed)
                .is_ok()
        {
            self.gauge.set(1);
            return Some(true);
        }
        None
    }

    pub fn finish(&self, config: CircuitBreaker, trial: bool, failed: bool, now: u64) {
        if !failed {
            if trial {
                // Only the trial owner moves half-open to closed.
                if self
                    .state
                    .compare_exchange(1, 0, Ordering::AcqRel, Ordering::Relaxed)
                    .is_ok()
                {
                    self.failures.store(0, Ordering::Relaxed);
                    self.gauge.set(0);
                }
            } else if self.state.load(Ordering::Acquire) == 0 {
                self.failures.store(0, Ordering::Relaxed);
            }
            return;
        }
        let open_until = now.saturating_add(Self::open_ms(&config)).saturating_add(2);
        if trial {
            if self
                .state
                .compare_exchange(1, open_until, Ordering::AcqRel, Ordering::Relaxed)
                .is_ok()
            {
                self.gauge.set(2);
            }
            return;
        }
        // A late failure of a request admitted while closed says nothing new
        // once the breaker is open or half-open: it must not overwrite the
        // trial's state or push the open deadline out.
        if self.state.load(Ordering::Acquire) != 0 {
            return;
        }
        let threshold = if config.failure_threshold == 0 {
            5
        } else {
            config.failure_threshold
        };
        if self.failures.fetch_add(1, Ordering::Relaxed) + 1 >= threshold
            && self
                .state
                .compare_exchange(0, open_until, Ordering::AcqRel, Ordering::Relaxed)
                .is_ok()
        {
            self.failures.store(0, Ordering::Relaxed);
            self.gauge.set(2);
        }
    }

    /// The model's upstream no longer has a breaker: forget its state.
    pub fn reset(&self) {
        self.state.store(0, Ordering::Release);
        self.failures.store(0, Ordering::Relaxed);
        self.gauge.set(0);
    }

    /// Open-interval length in ms for `config` (0 = the 30 s default).
    pub fn open_ms(config: &CircuitBreaker) -> u64 {
        if config.open_ms == 0 {
            30_000
        } else {
            config.open_ms.into()
        }
    }
}

#[derive(Default)]
pub struct Registry {
    breakers: Mutex<HashMap<String, Arc<Breaker>>>,
    upstreams: Mutex<HashMap<String, Arc<AtomicBool>>>,
    /// Running probe tasks with the probe each one was started for.
    tasks: Mutex<Vec<(Probe, JoinHandle<()>)>>,
    /// Signalled when a probe changes state or the probed set changes (the
    /// config stream reports it to the control plane).
    changed: Arc<Notify>,
}

impl Registry {
    pub fn breaker(&self, name: &str, gauge: Gauge) -> Arc<Breaker> {
        self.breakers
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .entry(name.into())
            .or_insert_with(|| Arc::new(Breaker::new(gauge)))
            .clone()
    }

    pub fn upstream(&self, name: &str) -> Arc<AtomicBool> {
        self.upstreams
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .entry(name.into())
            .or_insert_with(|| Arc::new(AtomicBool::new(true)))
            .clone()
    }

    /// Called only after a snapshot is accepted, never during compilation.
    /// Diffs by (upstream, uri, config, auth): an unchanged probe keeps its
    /// task, with its consecutive failure/success counts and its tick phase
    /// (no immediate re-probe); only removed or changed probes are aborted
    /// and only new or changed ones are spawned. Returns how many tasks
    /// were spawned.
    /// Waits until a probe changed state since the last call (one stored
    /// permit, so changes before the call are not lost and bursts coalesce).
    pub async fn changed(&self) {
        self.changed.notified().await;
    }

    /// (upstream, healthy) for every probed upstream, sorted by name.
    pub fn probed(&self) -> Vec<(String, bool)> {
        let tasks = self.tasks.lock().unwrap_or_else(|e| e.into_inner());
        let mut out: Vec<_> = tasks
            .iter()
            .map(|(p, _)| (p.name.clone(), p.healthy.load(Ordering::Acquire)))
            .collect();
        out.sort();
        out.dedup_by(|a, b| a.0 == b.0);
        out
    }

    pub fn configure(&self, probes: &[Probe], client: &UpstreamClient) -> usize {
        self.changed.notify_one();
        let mut tasks = self.tasks.lock().unwrap_or_else(|e| e.into_inner());
        let mut old = std::mem::take(&mut *tasks);
        let mut spawned = 0;
        for probe in probes {
            match old
                .iter()
                .position(|(running, _)| running.same_target(probe))
            {
                Some(i) => tasks.push(old.swap_remove(i)),
                None => {
                    spawned += 1;
                    tasks.push((
                        probe.clone(),
                        spawn_probe(probe.clone(), client.clone(), self.changed.clone()),
                    ));
                }
            }
        }
        for (_, task) in old {
            task.abort();
        }
        spawned
    }
}

fn spawn_probe(probe: Probe, client: UpstreamClient, changed: Arc<Notify>) -> JoinHandle<()> {
    tokio::spawn(async move {
        let mut failures = 0u32;
        let mut successes = 0u32;
        let mut interval =
            tokio::time::interval(Duration::from_millis(probe.config.interval_ms.into()));
        interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        loop {
            interval.tick().await;
            let mut request = Request::new(Full::new(Bytes::new()));
            *request.uri_mut() = probe.uri.clone();
            if let Some(value) = &probe.authorization {
                request
                    .headers_mut()
                    .insert(hyper::header::AUTHORIZATION, value.clone());
            }
            let ok = tokio::time::timeout(
                Duration::from_millis(probe.config.timeout_ms.into()),
                async {
                    let response = client.request(request).await?;
                    let ok = response.status().is_success();
                    // Drain each frame, without buffering the probe body.
                    let mut body = response.into_body();
                    while let Some(frame) = body.frame().await {
                        if frame.is_err() {
                            return Ok::<_, hyper_util::client::legacy::Error>(false);
                        }
                    }
                    Ok(ok)
                },
            )
            .await
            .is_ok_and(|result| result.unwrap_or(false));
            if ok {
                failures = 0;
                successes = successes.saturating_add(1);
                if successes >= probe.config.healthy_threshold.max(1) {
                    let was_healthy = probe.healthy.swap(true, Ordering::AcqRel);
                    probe.gauge.set(1);
                    if !was_healthy {
                        tracing::info!(upstream = %probe.name, healthy = true, "upstream health changed");
                        changed.notify_one();
                    }
                }
            } else {
                successes = 0;
                failures = failures.saturating_add(1);
                let threshold = if probe.config.unhealthy_threshold == 0 {
                    3
                } else {
                    probe.config.unhealthy_threshold
                };
                if failures >= threshold {
                    let was_healthy = probe.healthy.swap(false, Ordering::AcqRel);
                    probe.gauge.set(0);
                    if was_healthy {
                        tracing::info!(upstream = %probe.name, healthy = false, "upstream health changed");
                        changed.notify_one();
                    }
                }
            }
        }
    })
}

impl Drop for Registry {
    fn drop(&mut self) {
        for (_, task) in self
            .tasks
            .get_mut()
            .unwrap_or_else(|e| e.into_inner())
            .drain(..)
        {
            task.abort();
        }
    }
}

#[derive(Clone)]
pub struct Probe {
    pub name: String,
    pub uri: Uri,
    pub authorization: Option<HeaderValue>,
    pub config: HealthCheck,
    pub healthy: Arc<AtomicBool>,
    pub gauge: Gauge,
}

impl Probe {
    /// Same upstream probed the same way. `healthy` and `gauge` are shared
    /// by upstream name, so a kept task keeps updating the right state.
    fn same_target(&self, other: &Probe) -> bool {
        self.name == other.name
            && self.uri == other.uri
            && self.config == other.config
            && self.authorization == other.authorization
    }
}

/// Request-funded token bucket: one retry costs 100 units; each client
/// request on a router with a retry policy adds `percent` (routers without one
/// neither fund nor touch the budget, so retries stay bounded by about
/// `percent`% of the traffic that may retry). A three-retry burst permits
/// low-traffic recovery; ten idle seconds restore that minimum.
///
/// Funding is read-mostly: one load of the current second, one load of the
/// tokens, and a single `fetch_add` only while the bucket is below its cap.
/// The cap is therefore approximate under concurrency (it may overshoot by
/// one deposit per racing request). No mutex or allocation on admission.
pub struct RetryBudget {
    percent: AtomicU32,
    /// Monotonic second of the latest deposit.
    tick: AtomicU32,
    tokens: AtomicU32,
}

const BUDGET_CAP: u32 = 300;

impl RetryBudget {
    pub fn new() -> Self {
        Self {
            percent: AtomicU32::new(20),
            tick: AtomicU32::new(0),
            tokens: AtomicU32::new(BUDGET_CAP),
        }
    }

    pub fn configure(&self, percent: u32) {
        self.percent.store(percent, Ordering::Relaxed);
    }

    /// Funds the budget for one client request on a retrying router.
    pub fn request(&self) {
        self.request_at(clock::mono_ms());
    }

    fn request_at(&self, now: u64) {
        let percent = self.percent.load(Ordering::Relaxed);
        if percent == 0 {
            return;
        }
        let tick = (now / 1000) as u32;
        let last = self.tick.load(Ordering::Relaxed);
        if tick > last {
            if tick - last >= 10 {
                // Idle: only the request that advances the clock restores.
                if self
                    .tick
                    .compare_exchange(last, tick, Ordering::Relaxed, Ordering::Relaxed)
                    .is_ok()
                {
                    self.tokens.store(BUDGET_CAP, Ordering::Relaxed);
                }
            } else {
                // A stale (earlier) clock sample never moves this backwards.
                self.tick.fetch_max(tick, Ordering::Relaxed);
            }
        }
        if self.tokens.load(Ordering::Relaxed) < BUDGET_CAP {
            self.tokens.fetch_add(percent, Ordering::Relaxed);
        }
    }

    pub fn take(&self) -> bool {
        if self.percent.load(Ordering::Relaxed) == 0 {
            return false;
        }
        let mut tokens = self.tokens.load(Ordering::Relaxed);
        while tokens >= 100 {
            match self.tokens.compare_exchange_weak(
                tokens,
                tokens.min(BUDGET_CAP) - 100,
                Ordering::Relaxed,
                Ordering::Relaxed,
            ) {
                Ok(_) => return true,
                Err(current) => tokens = current,
            }
        }
        false
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn breaker_opens_allows_one_trial_and_recovers() {
        let gauge = Gauge::default();
        let b = Breaker::new(gauge.clone());
        let config = CircuitBreaker {
            failure_threshold: 2,
            open_ms: 100,
        };
        b.finish(config, false, true, 0);
        assert!(b.available(0));
        b.finish(config, false, true, 0);
        assert_eq!(gauge.get(), 2);
        assert!(!b.available(99));
        assert_eq!(b.reserve(100), Some(true));
        assert_eq!(gauge.get(), 1);
        assert_eq!(b.reserve(100), None);
        b.finish(config, true, true, 100);
        assert_eq!(b.reserve(199), None);
        assert_eq!(b.reserve(200), Some(true));
        b.finish(config, true, false, 200);
        assert_eq!(gauge.get(), 0);
        assert_eq!(b.reserve(200), Some(false));
    }

    #[test]
    fn late_non_trial_failure_does_not_override_half_open_or_open() {
        let gauge = Gauge::default();
        let b = Breaker::new(gauge.clone());
        let config = CircuitBreaker {
            failure_threshold: 1,
            open_ms: 100,
        };
        b.finish(config, false, true, 0);
        assert_eq!(gauge.get(), 2);
        // Open: a late failure from a request admitted while closed does not
        // push the deadline out.
        b.finish(config, false, true, 50);
        assert!(b.available(100));
        // Half-open: the trial owns the state; a late failure leaves it.
        assert_eq!(b.reserve(100), Some(true));
        b.finish(config, false, true, 150);
        assert!(b.trial_in_progress());
        assert_eq!(gauge.get(), 1);
        // A late success does not close it either; only the trial does.
        b.finish(config, false, false, 150);
        assert!(b.trial_in_progress());
        b.finish(config, true, false, 160);
        assert_eq!(gauge.get(), 0);
        assert_eq!(b.reserve(160), Some(false));
        // Reset forgets an open breaker.
        b.finish(config, false, true, 200);
        assert!(!b.available(250));
        b.reset();
        assert!(b.available(250));
        assert_eq!(gauge.get(), 0);
    }

    #[tokio::test]
    async fn configure_keeps_unchanged_probes_and_restarts_changed_ones() {
        let registry = Registry::default();
        let client = crate::proxy::client();
        let probe = Probe {
            name: "u".into(),
            // Nothing is ever sent: the first tick fires after the asserts.
            uri: "http://127.0.0.1:9/v1/models".parse().unwrap(),
            authorization: None,
            config: HealthCheck {
                interval_ms: 60_000,
                timeout_ms: 1,
                ..Default::default()
            },
            healthy: registry.upstream("u"),
            gauge: Gauge::default(),
        };
        assert_eq!(registry.configure(std::slice::from_ref(&probe), &client), 1);
        assert_eq!(registry.configure(std::slice::from_ref(&probe), &client), 0);
        let mut changed = probe.clone();
        changed.authorization = Some(HeaderValue::from_static("Bearer x"));
        assert_eq!(registry.configure(&[changed.clone()], &client), 1);
        changed.config.unhealthy_threshold = 2;
        let mut other = probe.clone();
        other.name = "v".into();
        assert_eq!(registry.configure(&[changed, other], &client), 2);
        assert_eq!(registry.configure(&[], &client), 0);
        assert!(registry.tasks.lock().unwrap().is_empty());
    }

    #[test]
    fn budget_bounds_retries_and_restores_idle_minimum() {
        let budget = RetryBudget::new();
        for _ in 0..3 {
            assert!(budget.take());
        }
        assert!(!budget.take());
        for i in 0..100 {
            budget.request_at(i);
            if i % 5 == 4 {
                assert!(budget.take());
            }
            assert!(!budget.take());
        }
        budget.request_at(10_000);
        assert!(budget.take());
        budget.configure(0);
        assert!(!budget.take());
        // Disabled: deposits do not touch the shared state at all.
        let before = budget.tokens.load(Ordering::Relaxed);
        budget.request_at(10_500);
        assert_eq!(budget.tokens.load(Ordering::Relaxed), before);
    }

    #[test]
    fn stale_clock_sample_does_not_restore_budget() {
        let budget = RetryBudget::new();
        for _ in 0..3 {
            assert!(budget.take());
        }
        budget.request_at(2000);
        budget.request_at(1000);
        assert!(!budget.take());
        assert_eq!(budget.tick.load(Ordering::Relaxed), 2);
    }
}
