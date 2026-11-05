//! Load-aware selection state: per-model latency EWMA for
//! `LOWEST_LATENCY`, scraped vLLM load for `VLLM_LOAD`, and the fixed-seed
//! hash used for sticky traffic splits.
//!
//! Everything a request touches here is a relaxed atomic on state cached in
//! its `Model`; registries are keyed by model name (so state
//! survives snapshot swaps) and are only locked at compile/activation time.
//! Scraping runs in background tasks and never on the request path.

use std::{
    collections::HashMap,
    sync::{
        atomic::{AtomicU32, AtomicU64, Ordering},
        Arc, Mutex,
    },
    time::Duration,
};

use bytes::Bytes;
use http_body_util::{BodyExt, Full, Limited};
use hyper::{header::HeaderValue, Request, Uri};
use prometheus_client::metrics::{counter::Counter, gauge::Gauge};
use tokio::task::JoinHandle;

use crate::{metrics::Metrics, proxy::UpstreamClient, ratelimit::clock};

/// EWMA weight of a new sample: `new = old + (sample - old) / EWMA_DIVISOR`.
const EWMA_DIVISOR: u64 = 5;
/// A failed or timed-out attempt counts as at least this slow (and at least
/// twice the current average).
const FAILURE_PENALTY_US: u64 = 1_000_000;
/// vLLM load older than this is ignored (the model is compared by
/// in-flight requests instead).
pub const LOAD_STALE_MS: u64 = 3_000;
pub const SCRAPE_INTERVAL: Duration = Duration::from_secs(1);
const SCRAPE_TIMEOUT: Duration = Duration::from_millis(900);
const MAX_METRICS_BYTES: usize = 8 * 1024 * 1024;
/// KV-cache usage is compared in 5% buckets so noise falls through to
/// in-flight requests.
#[cfg(test)]
const KV_BUCKET_BP: u32 = 500;

/// Time-to-response-headers EWMA in microseconds; 0 = no sample yet.
#[derive(Default)]
pub struct Latency {
    ewma_us: AtomicU64,
}

impl Latency {
    pub fn get_us(&self) -> u64 {
        self.ewma_us.load(Ordering::Relaxed)
    }

    /// A successful attempt that got response headers after `elapsed`.
    pub fn success(&self, elapsed: Duration) {
        let sample = u64::try_from(elapsed.as_micros())
            .unwrap_or(u64::MAX)
            .max(1);
        self.update(|_| sample);
    }

    /// A failed attempt (connect error, timeout, 5xx/429): penalized.
    pub fn failure(&self, elapsed: Duration) {
        let elapsed = u64::try_from(elapsed.as_micros()).unwrap_or(u64::MAX);
        self.update(|old| elapsed.max(old.saturating_mul(2)).max(FAILURE_PENALTY_US));
    }

    fn update(&self, sample: impl Fn(u64) -> u64) {
        let mut old = self.ewma_us.load(Ordering::Relaxed);
        loop {
            let sample = sample(old);
            let next = if old == 0 {
                sample
            } else if sample >= old {
                old + (sample - old) / EWMA_DIVISOR
            } else {
                (old - (old - sample) / EWMA_DIVISOR).max(1)
            };
            match self.ewma_us.compare_exchange_weak(
                old,
                next,
                Ordering::Relaxed,
                Ordering::Relaxed,
            ) {
                Ok(_) => return,
                Err(current) => old = current,
            }
        }
    }
}

/// Last scraped vLLM load of one model.
pub struct VllmLoad {
    waiting: AtomicU64,
    /// KV-cache usage in basis points (0..=10000).
    kv_bp: AtomicU32,
    /// Monotonic ms of the last good scrape, plus one; 0 = never or failed.
    updated: AtomicU64,
    queue_gauge: Gauge,
    kv_gauge: Gauge<f64, AtomicU64>,
}

impl VllmLoad {
    pub fn view(&self, now: u64) -> lumewarp_router_api::Load {
        let updated = self.updated.load(Ordering::Acquire);
        lumewarp_router_api::Load {
            waiting: self.waiting.load(Ordering::Relaxed),
            kv_usage_bp: self.kv_bp.load(Ordering::Relaxed),
            fresh: updated != 0 && now.saturating_sub(updated - 1) <= LOAD_STALE_MS,
        }
    }

    fn new(queue_gauge: Gauge, kv_gauge: Gauge<f64, AtomicU64>) -> Self {
        Self {
            waiting: AtomicU64::new(0),
            kv_bp: AtomicU32::new(0),
            updated: AtomicU64::new(0),
            queue_gauge,
            kv_gauge,
        }
    }

    /// `(waiting, kv bucket)` when the last scrape is fresh at `now`.
    #[cfg(test)]
    pub fn score(&self, now: u64) -> Option<(u64, u32)> {
        let updated = self.updated.load(Ordering::Acquire);
        if updated == 0 || now.saturating_sub(updated - 1) > LOAD_STALE_MS {
            return None;
        }
        Some((
            self.waiting.load(Ordering::Relaxed),
            self.kv_bp.load(Ordering::Relaxed) / KV_BUCKET_BP,
        ))
    }

    pub fn set(&self, waiting: f64, kv: f64, now: u64) {
        let waiting = waiting.max(0.0).round() as u64;
        let kv = kv.clamp(0.0, 1.0);
        self.waiting.store(waiting, Ordering::Relaxed);
        self.kv_bp
            .store((kv * 10_000.0).round() as u32, Ordering::Relaxed);
        self.updated.store(now + 1, Ordering::Release);
        self.queue_gauge
            .set(i64::try_from(waiting).unwrap_or(i64::MAX));
        self.kv_gauge.set(kv);
    }

    pub fn fail(&self) {
        self.updated.store(0, Ordering::Release);
    }
}

/// One background scrape target: a metrics URL and the models it feeds.
#[derive(Clone)]
pub struct Scrape {
    pub upstream: String,
    pub uri: Uri,
    pub authorization: Option<HeaderValue>,
    /// (upstream_model, load) of each model on this upstream.
    pub targets: Vec<(String, Arc<VllmLoad>)>,
    pub failures: Counter,
}

impl Scrape {
    /// Same metrics URL, credentials and models fed. `load` and
    /// `failures` are shared by name, so a kept task keeps feeding them.
    fn same_target(&self, other: &Scrape) -> bool {
        self.upstream == other.upstream
            && self.uri == other.uri
            && self.authorization == other.authorization
            && self.targets.len() == other.targets.len()
            && self
                .targets
                .iter()
                .zip(&other.targets)
                .all(|((a, la), (b, lb))| a == b && Arc::ptr_eq(la, lb))
    }
}

#[derive(Default)]
pub struct Registry {
    latency: Mutex<HashMap<String, Arc<Latency>>>,
    load: Mutex<HashMap<String, Arc<VllmLoad>>>,
    /// Running scrape tasks with the target each one was started for.
    tasks: Mutex<Vec<(Scrape, JoinHandle<()>)>>,
}

impl Registry {
    pub fn latency(&self, model: &str) -> Arc<Latency> {
        self.latency
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .entry(model.into())
            .or_default()
            .clone()
    }

    pub fn load(&self, model: &str, metrics: &Metrics) -> Arc<VllmLoad> {
        self.load
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .entry(model.into())
            .or_insert_with(|| {
                let (queue, kv) = metrics.vllm_gauges(model);
                Arc::new(VllmLoad::new(queue, kv))
            })
            .clone()
    }

    /// Latency EWMAs with at least one sample, for the metrics endpoint.
    pub fn latencies(&self) -> Vec<(String, u64)> {
        self.latency
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .iter()
            .map(|(name, latency)| (name.clone(), latency.get_us()))
            .filter(|(_, us)| *us > 0)
            .collect()
    }

    /// Called only after a snapshot is accepted. Diffs by target: an
    /// unchanged scrape keeps its task; only removed or changed ones are
    /// aborted and only new or changed ones spawned. Returns the spawn count.
    pub fn configure(&self, scrapes: &[Scrape], client: &UpstreamClient) -> usize {
        let mut tasks = self.tasks.lock().unwrap_or_else(|e| e.into_inner());
        let mut old = std::mem::take(&mut *tasks);
        let mut spawned = 0;
        for scrape in scrapes {
            match old
                .iter()
                .position(|(running, _)| running.same_target(scrape))
            {
                Some(i) => tasks.push(old.swap_remove(i)),
                None => {
                    spawned += 1;
                    tasks.push((scrape.clone(), spawn_scrape(scrape.clone(), client.clone())));
                }
            }
        }
        for (_, task) in old {
            task.abort();
        }
        spawned
    }
}

fn spawn_scrape(scrape: Scrape, client: UpstreamClient) -> JoinHandle<()> {
    tokio::spawn(async move {
        let mut interval = tokio::time::interval(SCRAPE_INTERVAL);
        interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        loop {
            interval.tick().await;
            let text = tokio::time::timeout(SCRAPE_TIMEOUT, fetch(&client, &scrape))
                .await
                .ok()
                .flatten();
            let ok = text.is_some_and(|text| apply(&scrape, &text, clock::mono_ms()));
            if !ok {
                scrape.failures.inc();
                for (_, load) in &scrape.targets {
                    load.fail();
                }
                tracing::debug!(upstream = %scrape.upstream, "vllm metrics scrape failed");
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

async fn fetch(client: &UpstreamClient, scrape: &Scrape) -> Option<String> {
    let mut request = Request::new(Full::new(Bytes::new()));
    *request.uri_mut() = scrape.uri.clone();
    if let Some(value) = &scrape.authorization {
        request
            .headers_mut()
            .insert(hyper::header::AUTHORIZATION, value.clone());
    }
    let response = client.request(request).await.ok()?;
    if !response.status().is_success() {
        return None;
    }
    let body = Limited::new(response.into_body(), MAX_METRICS_BYTES)
        .collect()
        .await
        .ok()?
        .to_bytes();
    String::from_utf8(body.into()).ok()
}

/// Applies one scrape to its targets; false when a target's queue depth is
/// missing (the whole scrape then counts as failed).
fn apply(scrape: &Scrape, text: &str, now: u64) -> bool {
    let load = parse_vllm(text);
    let mut ok = true;
    for (model, target) in &scrape.targets {
        match load.for_model(model) {
            Some((waiting, kv)) => target.set(waiting, kv, now),
            None => {
                target.fail();
                ok = false;
            }
        }
    }
    ok
}

/// Selected vLLM series: (model_name label, value).
#[derive(Debug, Default, PartialEq)]
pub struct VllmSeries {
    pub waiting: Vec<(Option<String>, f64)>,
    pub kv: Vec<(Option<String>, f64)>,
}

impl VllmSeries {
    /// (waiting, kv usage) for `model`: series labelled with that model when
    /// any exist, else every series (vLLM serving one model under another
    /// name). Waiting sums across engines; KV usage takes the maximum.
    pub fn for_model(&self, model: &str) -> Option<(f64, f64)> {
        fn select<'a>(
            series: &'a [(Option<String>, f64)],
            model: &'a str,
        ) -> impl Iterator<Item = f64> + 'a {
            let matched = series.iter().any(|(m, _)| m.as_deref() == Some(model));
            series
                .iter()
                .filter(move |(m, _)| !matched || m.as_deref() == Some(model))
                .map(|(_, v)| *v)
        }
        if self.waiting.is_empty() {
            return None;
        }
        let waiting = select(&self.waiting, model).sum();
        let kv = select(&self.kv, model).fold(0.0, f64::max);
        Some((waiting, kv))
    }
}

/// Minimal Prometheus text-format reader for the vLLM series we use.
pub fn parse_vllm(text: &str) -> VllmSeries {
    let mut out = VllmSeries::default();
    for line in text.lines() {
        let Some((name, labels, value)) = parse_sample(line) else {
            continue;
        };
        let target = match name {
            "vllm:num_requests_waiting" => &mut out.waiting,
            "vllm:gpu_cache_usage_perc" | "vllm:kv_cache_usage_perc" => &mut out.kv,
            _ => continue,
        };
        target.push((label(labels, "model_name"), value));
    }
    out
}

/// `name{labels} value [timestamp]` -> (name, raw labels, finite value).
pub fn parse_sample(line: &str) -> Option<(&str, &str, f64)> {
    let line = line.trim_start();
    if line.is_empty() || line.starts_with('#') {
        return None;
    }
    let name_end = line
        .find(|c: char| c == '{' || c.is_ascii_whitespace())
        .unwrap_or(line.len());
    let name = &line[..name_end];
    let mut rest = &line[name_end..];
    let mut labels = "";
    if rest.starts_with('{') {
        let mut quoted = false;
        let mut escaped = false;
        let mut close = None;
        for (i, c) in rest.char_indices().skip(1) {
            match c {
                _ if escaped => escaped = false,
                '\\' if quoted => escaped = true,
                '"' => quoted = !quoted,
                '}' if !quoted => {
                    close = Some(i);
                    break;
                }
                _ => {}
            }
        }
        let close = close?;
        labels = &rest[1..close];
        rest = &rest[close + 1..];
    }
    let value: f64 = rest.split_ascii_whitespace().next()?.parse().ok()?;
    value.is_finite().then_some((name, labels, value))
}

/// Value of label `key` in a raw `k="v",...` list, unescaped.
pub fn label(raw: &str, key: &str) -> Option<String> {
    let mut rest = raw;
    loop {
        rest = rest.trim_start_matches(|c: char| c == ',' || c.is_ascii_whitespace());
        if rest.is_empty() {
            return None;
        }
        let eq = rest.find('=')?;
        let name = rest[..eq].trim();
        rest = rest[eq + 1..].trim_start().strip_prefix('"')?;
        let mut value = String::new();
        let mut escaped = false;
        let mut end = None;
        for (i, c) in rest.char_indices() {
            if escaped {
                value.push(if c == 'n' { '\n' } else { c });
                escaped = false;
            } else if c == '\\' {
                escaped = true;
            } else if c == '"' {
                end = Some(i);
                break;
            } else {
                value.push(c);
            }
        }
        let end = end?;
        if name == key {
            return Some(value);
        }
        rest = &rest[end + 1..];
    }
}

/// Fixed-seed 64-bit hash for sticky split assignment: FNV-1a, then the
/// SplitMix64 finalizer for avalanche. Stable across processes, restarts and
/// platforms (never `RandomState`).
pub fn sticky_hash(bytes: &[u8]) -> u64 {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for &b in bytes {
        h ^= u64::from(b);
        h = h.wrapping_mul(0x0000_0100_0000_01b3);
    }
    h ^= h >> 30;
    h = h.wrapping_mul(0xbf58_476d_1ce4_e5b9);
    h ^= h >> 27;
    h = h.wrapping_mul(0x94d0_49bb_1331_11eb);
    h ^ (h >> 31)
}

/// Maps a hash uniformly onto `0..total` without modulo bias.
pub fn scale(hash: u64, total: u64) -> u64 {
    ((u128::from(hash) * u128::from(total)) >> 64) as u64
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ewma_tracks_samples_and_penalizes_failures() {
        let l = Latency::default();
        assert_eq!(l.get_us(), 0);
        l.success(Duration::from_millis(10));
        assert_eq!(l.get_us(), 10_000);
        l.success(Duration::from_millis(20));
        assert_eq!(l.get_us(), 12_000);
        for _ in 0..50 {
            l.success(Duration::from_millis(20));
        }
        let before = l.get_us();
        assert!((19_900..=20_000).contains(&before), "{before}");
        l.failure(Duration::from_millis(1));
        // Sample = max(1 ms, 2 x 20 ms, 1 s) = 1 s.
        assert_eq!(l.get_us(), before + (1_000_000 - before) / 5);
        let slow = Latency::default();
        slow.success(Duration::from_secs(2));
        slow.failure(Duration::from_millis(1));
        // Sample = 2 x 2 s, so a slow model is still penalized.
        assert_eq!(slow.get_us(), 2_000_000 + 2_000_000 / 5);
        // Never returns to "no sample" through decay.
        let l = Latency::default();
        l.success(Duration::ZERO);
        assert_eq!(l.get_us(), 1);
        l.success(Duration::ZERO);
        assert_eq!(l.get_us(), 1);
    }

    fn load() -> VllmLoad {
        VllmLoad::new(Gauge::default(), Gauge::default())
    }

    #[test]
    fn vllm_load_goes_stale_and_fails() {
        let l = load();
        assert_eq!(l.score(0), None);
        l.set(3.4, 0.56, 1_000);
        // 0.56 -> 5600 bp -> 5% bucket 11.
        assert_eq!(l.score(1_000), Some((3, 11)));
        assert_eq!(l.score(4_000), Some((3, 11)));
        assert_eq!(l.score(4_001), None);
        assert_eq!(l.queue_gauge.get(), 3);
        assert!((l.kv_gauge.get() - 0.56).abs() < 1e-9);
        l.set(-1.0, 7.0, 0);
        assert_eq!(l.score(0), Some((0, 20)));
        l.fail();
        assert_eq!(l.score(0), None);
    }

    const VLLM: &str = r#"
# HELP vllm:num_requests_waiting Number of requests waiting to be processed.
# TYPE vllm:num_requests_waiting gauge
vllm:num_requests_waiting{engine="0",model_name="llama"} 4.0
vllm:num_requests_waiting{engine="1",model_name="llama"} 2.0
vllm:num_requests_waiting{engine="0",model_name="qwen \"x\", y}"} 7.0 1700000000000
vllm:num_requests_waiting_total{model_name="llama"} 99
vllm:num_requests_running{model_name="llama"} 5
vllm:gpu_cache_usage_perc{model_name="llama",engine="0"} 0.25
vllm:kv_cache_usage_perc{engine="1",model_name="llama"} 0.5
vllm:kv_cache_usage_perc{model_name="qwen \"x\", y}"} 0.75
vllm:kv_cache_usage_perc{model_name="nan"} NaN
"#;

    #[test]
    fn prometheus_text_parser_handles_names_labels_and_escapes() {
        assert_eq!(
            parse_sample(r#"a_b:c{x="1",y="}\"\\"} +Inf"#),
            None,
            "non-finite values are dropped"
        );
        assert_eq!(
            parse_sample(r#"  a{x="}\"",y="2"} 3 123"#),
            Some(("a", r#"x="}\"",y="2""#, 3.0))
        );
        assert_eq!(parse_sample("plain 1e3"), Some(("plain", "", 1000.0)));
        assert_eq!(parse_sample("# TYPE plain gauge"), None);
        assert_eq!(parse_sample("broken{x=\"1\" 1"), None);
        assert_eq!(parse_sample("novalue"), None);
        assert_eq!(label(r#"x="}\"",y="a\\b\nc""#, "y").unwrap(), "a\\b\nc");
        assert_eq!(label(r#"x="}\"", y = "2""#, "x").unwrap(), "}\"");
        assert_eq!(label(r#"x="1""#, "z"), None);
        let series = parse_vllm(VLLM);
        assert_eq!(series.waiting.len(), 3);
        assert_eq!(series.kv.len(), 3);
        assert_eq!(series.for_model("llama"), Some((6.0, 0.5)));
        assert_eq!(series.for_model("qwen \"x\", y}"), Some((7.0, 0.75)));
        // Unknown model name: everything, summed / max.
        assert_eq!(series.for_model("other"), Some((13.0, 0.75)));
        assert_eq!(
            parse_vllm("vllm:kv_cache_usage_perc 0.5").for_model("m"),
            None
        );
        assert_eq!(
            parse_vllm("vllm:num_requests_waiting 2").for_model("m"),
            Some((2.0, 0.0))
        );
    }

    #[test]
    fn scrape_apply_marks_missing_targets_failed() {
        let llama = Arc::new(load());
        let other = Arc::new(load());
        let scrape = Scrape {
            upstream: "u".into(),
            uri: "http://x/metrics".parse().unwrap(),
            authorization: None,
            targets: vec![("llama".into(), llama.clone()), ("x".into(), other.clone())],
            failures: Counter::default(),
        };
        assert!(apply(&scrape, VLLM, 10));
        assert_eq!(llama.score(10), Some((6, 10)));
        assert_eq!(other.score(10), Some((13, 15)));
        assert!(!apply(&scrape, "garbage", 20));
        assert_eq!(llama.score(20), None);
    }

    #[test]
    fn sticky_hash_is_stable_and_spreads() {
        // Golden values: changing the hash re-shuffles every sticky user.
        assert_eq!(sticky_hash(b""), 17_665_956_581_633_026_203);
        assert_eq!(sticky_hash(b"session-1"), sticky_hash(b"session-1"));
        assert_ne!(sticky_hash(b"session-1"), sticky_hash(b"session-2"));
        assert_eq!(scale(0, 10), 0);
        assert_eq!(scale(u64::MAX, 10), 9);
        let mut buckets = [0u32; 10];
        for i in 0..100_000u32 {
            buckets[scale(sticky_hash(format!("user-{i}").as_bytes()), 10) as usize] += 1;
        }
        for count in buckets {
            assert!((9_500..10_500).contains(&count), "{buckets:?}");
        }
    }
}
