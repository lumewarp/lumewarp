//! Passive per-deployment breakers and off-path active upstream probes.
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
use tokio::task::JoinHandle;

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
                self.failures.store(0, Ordering::Relaxed);
                self.state.store(0, Ordering::Release);
                self.gauge.set(0);
            } else if self.state.load(Ordering::Acquire) == 0 {
                self.failures.store(0, Ordering::Relaxed);
            }
            return;
        }
        let threshold = if config.failure_threshold == 0 {
            5
        } else {
            config.failure_threshold
        };
        if trial || self.failures.fetch_add(1, Ordering::Relaxed) + 1 >= threshold {
            let open_ms = if config.open_ms == 0 {
                30_000
            } else {
                config.open_ms
            };
            self.state.store(
                now.saturating_add(u64::from(open_ms)).saturating_add(2),
                Ordering::Release,
            );
            self.gauge.set(2);
        }
    }
}

#[derive(Default)]
pub struct Registry {
    breakers: Mutex<HashMap<String, Arc<Breaker>>>,
    upstreams: Mutex<HashMap<String, Arc<AtomicBool>>>,
    tasks: Mutex<Vec<JoinHandle<()>>>,
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
    pub fn configure(&self, probes: &[Probe], client: &UpstreamClient) {
        let mut tasks = self.tasks.lock().unwrap_or_else(|e| e.into_inner());
        for task in tasks.drain(..) {
            task.abort();
        }
        for probe in probes {
            let probe = probe.clone();
            let client = client.clone();
            tasks.push(tokio::spawn(async move {
                let mut failures = 0u32;
                let mut successes = 0u32;
                let mut interval = tokio::time::interval(Duration::from_millis(probe.config.interval_ms.into()));
                interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
                loop {
                    interval.tick().await;
                    let mut request = Request::new(Full::new(Bytes::new()));
                    *request.uri_mut() = probe.uri.clone();
                    if let Some(value) = &probe.authorization {
                        request.headers_mut().insert(hyper::header::AUTHORIZATION, value.clone());
                    }
                    let ok = tokio::time::timeout(
                        Duration::from_millis(probe.config.timeout_ms.into()),
                        async {
                            let response = client.request(request).await?;
                            let ok = response.status().is_success();
                            // Drain each frame, without buffering the probe body.
                            let mut body = response.into_body();
                            while let Some(frame) = body.frame().await {
                                if frame.is_err() { return Ok::<_, hyper_util::client::legacy::Error>(false); }
                            }
                            Ok(ok)
                        },
                    ).await.is_ok_and(|result| result.unwrap_or(false));
                    if ok {
                        failures = 0;
                        successes = successes.saturating_add(1);
                        if successes >= probe.config.healthy_threshold.max(1) {
                            let was_healthy = probe.healthy.swap(true, Ordering::AcqRel);
                            probe.gauge.set(1);
                            if !was_healthy { tracing::info!(upstream = %probe.name, healthy = true, "upstream health changed"); }
                        }
                    } else {
                        successes = 0;
                        failures = failures.saturating_add(1);
                        let threshold = if probe.config.unhealthy_threshold == 0 { 3 } else { probe.config.unhealthy_threshold };
                        if failures >= threshold {
                            let was_healthy = probe.healthy.swap(false, Ordering::AcqRel);
                            probe.gauge.set(0);
                            if was_healthy { tracing::info!(upstream = %probe.name, healthy = false, "upstream health changed"); }
                        }
                    }
                }
            }));
        }
    }
}

impl Drop for Registry {
    fn drop(&mut self) {
        for task in self
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

/// Request-funded token bucket: one retry costs 100 units; each client
/// request adds `percent`. A three-retry burst permits low-traffic recovery.
/// Ten idle seconds restore that minimum. No mutex or allocation on admission.
pub struct RetryBudget {
    percent: AtomicU32,
    state: AtomicU64,
}

impl RetryBudget {
    pub fn new() -> Self {
        Self {
            percent: AtomicU32::new(20),
            state: AtomicU64::new(300),
        }
    }

    pub fn configure(&self, percent: u32) {
        self.percent.store(percent, Ordering::Relaxed);
    }

    pub fn request(&self) {
        self.request_at(clock::mono_ms());
    }

    fn request_at(&self, now: u64) {
        let percent = self.percent.load(Ordering::Relaxed);
        let tick = (now / 1000) as u32;
        let mut state = self.state.load(Ordering::Relaxed);
        loop {
            let last = (state >> 32) as u32;
            let tokens = if tick.saturating_sub(last) >= 10 {
                300
            } else {
                state as u32
            };
            let next = (u64::from(tick.max(last)) << 32)
                | u64::from(tokens.saturating_add(percent).min(300));
            match self.state.compare_exchange_weak(
                state,
                next,
                Ordering::Relaxed,
                Ordering::Relaxed,
            ) {
                Ok(_) => break,
                Err(current) => state = current,
            }
        }
    }

    pub fn take(&self) -> bool {
        if self.percent.load(Ordering::Relaxed) == 0 {
            return false;
        }
        let mut state = self.state.load(Ordering::Relaxed);
        while state as u32 >= 100 {
            match self.state.compare_exchange_weak(
                state,
                state - 100,
                Ordering::Relaxed,
                Ordering::Relaxed,
            ) {
                Ok(_) => return true,
                Err(current) => state = current,
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
        assert_eq!(budget.state.load(Ordering::Relaxed) >> 32, 2);
    }
}
