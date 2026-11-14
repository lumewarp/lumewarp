//! Unit tests for the shared-limits plumbing, with the in-memory store.
//!
//! The Redis integration tests live next to the store (they are gated on
//! `LUMEWARP_TEST_REDIS_URL`); these tests never need a server.

use std::{
    collections::HashSet,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    time::Duration,
};

use crate::{
    auth::Caller,
    metrics::Metrics,
    ratelimit::{Registry, ScopeCounts, ScopeView},
    router::KeyContext,
    runtime::{EnvOptions, Runtime},
    usage::UsageSink,
};
use lumewarp_proto::config::v1::{KeyStatus, RateLimit};

use super::{Config, Error, MemoryHub, MemoryStore, Store, SyncTask};

fn counts(id: &str, minute: u64) -> ScopeCounts {
    ScopeCounts {
        id: id.into(),
        minute,
        rpm_prev: 0,
        rpm_cur: 0,
        tpm_prev: 0,
        tpm_cur: 0,
        rpm: true,
        tpm: true,
        in_flight: 0,
        write: true,
        refresh_ttl: true,
        generation: 1,
    }
}

fn caller(rpm: u32, parallel: u32) -> Caller {
    Caller::Key(Arc::new(KeyContext {
        key_id: "k".into(),
        team_id: "".into(),
        status: KeyStatus::Active,
        expires_at_ms: 0,
        allow_all_models: true,
        allowed_models: HashSet::new(),
        scopes: vec![(
            "team:t".into(),
            RateLimit {
                requests_per_minute: rpm,
                tokens_per_minute: 0,
                max_parallel_requests: parallel,
            },
        )],
        team_label: 0,
        grants: crate::grants::Grants::none(),
    }))
}

#[tokio::test]
async fn memory_store_sums_the_other_gateways() {
    let hub = MemoryHub::default();
    let a = hub.store("ns", "a");
    let b = hub.store("ns", "b");
    let minute = 100;

    let mut first = counts("team:t", minute);
    first.rpm_cur = 2;
    first.tpm_prev = 5;
    first.in_flight = 3;
    let views = a.sync(&[first.clone()]).await.unwrap();
    assert_eq!(views[0].rpm_cur, 0, "a sees no one else yet");
    assert_eq!(views[0].rpm_gateways, 1, "self counts for n");
    assert_eq!(views[0].in_flight_gateways, 1);
    assert_eq!(views[0].in_flight, 0, "self does not count as others");

    let mut second = counts("team:t", minute);
    second.rpm_prev = 1;
    second.rpm_cur = 4;
    second.in_flight = 2;
    let views = b.sync(&[second]).await.unwrap();
    assert_eq!((views[0].rpm_prev, views[0].rpm_cur), (0, 2));
    assert_eq!((views[0].tpm_prev, views[0].tpm_cur), (5, 0));
    assert_eq!(views[0].rpm_gateways, 2);
    assert_eq!(views[0].in_flight, 3);
    assert_eq!(views[0].in_flight_gateways, 2);

    let mut read = counts("team:t", minute);
    read.write = false;
    read.refresh_ttl = false;
    let views = a.sync(&[read.clone()]).await.unwrap();
    assert_eq!((views[0].rpm_prev, views[0].rpm_cur), (1, 4));
    assert_eq!(views[0].rpm_gateways, 2);

    // A resend with the same absolute counts overwrites, never adds.
    let views = a.sync(&[first]).await.unwrap();
    assert_eq!(views[0].rpm_cur, 4, "b's count, not a's doubled count");

    // In-flight entries older than 3 s stop counting.
    hub.advance(3_001);
    let views = a.sync(&[read]).await.unwrap();
    assert_eq!(views[0].in_flight, 0);
    assert_eq!(views[0].in_flight_gateways, 1, "only a is fresh");
}

#[tokio::test]
async fn namespaces_do_not_see_each_other() {
    let hub = MemoryHub::default();
    let a = hub.store("ns-1", "a");
    let b = hub.store("ns-2", "b");
    let mut first = counts("team:t", 100);
    first.rpm_cur = 3;
    a.sync(&[first]).await.unwrap();
    let mut second = counts("team:t", 100);
    second.rpm_cur = 1;
    let views = b.sync(&[second]).await.unwrap();
    assert_eq!(views[0].rpm_cur, 0, "the other namespace is invisible");
    assert_eq!(views[0].rpm_gateways, 1, "only b used it in ns-2");
}

#[tokio::test]
async fn a_failed_sync_keeps_the_absolute_counts_dirty() {
    let hub = MemoryHub::default();
    let store = hub.store("ns", "a");
    let registry = Registry::new();
    let key = caller(10, 0);
    drop(registry.admit(&key).unwrap());
    let batch = registry.pending_sync();
    assert_eq!(batch[0].rpm_cur, 1);
    assert!(batch[0].write);

    hub.fail(true);
    assert!(store.sync(&batch).await.is_err());
    hub.fail(false);

    // There is nothing to restore: the scope stayed dirty and the retry
    // carries the same absolute count.
    let retry = registry.pending_sync();
    assert_eq!(retry[0].rpm_cur, 1);
    assert!(
        retry[0].write,
        "a failed sync does not clear the dirty flag"
    );
    let views = store.sync(&retry).await.unwrap();
    registry.apply_sync(&views);
    registry.mark_synced(&retry);

    // The scope is now clean; it is still read-only while its count is in the
    // window, so a joining gateway is noticed, but nothing is written.
    let after = registry.pending_sync();
    assert_eq!(after.len(), 1);
    assert!(!after[0].write, "a clean scope is not rewritten");
    assert_eq!(after[0].rpm_cur, 1);
}

#[tokio::test]
async fn fallback_and_recovery() {
    let hub = MemoryHub::default();
    let store_a = hub.store("ns", "a");
    let store_b = hub.store("ns", "b");
    let registry_b = Registry::new();
    let key = caller(2, 0);

    let metrics = Arc::new(Metrics::with_shared_limits(true));
    let runtime =
        Runtime::with_usage_and_env(metrics, UsageSink::disabled(), EnvOptions::default());
    let registry_a = &runtime.limiters;
    let mut task = SyncTask::new(
        runtime.clone(),
        Duration::from_millis(100),
        Arc::from("ns"),
        Duration::ZERO,
        store_a.clone(),
    );

    // B has one request; A has one and learns about B through the task.
    drop(registry_b.admit(&key).unwrap());
    let batch = registry_b.pending_sync();
    let views = store_b.sync(&batch).await.unwrap();
    registry_b.apply_sync(&views);
    registry_b.mark_synced(&batch);
    drop(registry_a.admit(&key).unwrap());
    task.sync_once().await;
    assert!(registry_a.admit(&key).is_err(), "B's request is shared");

    // A failure clears the shared half; the local limit (2) applies again.
    hub.fail(true);
    task.sync_once().await;
    drop(registry_a.admit(&key).unwrap());
    assert!(registry_a.admit(&key).is_err());
    let exported = runtime.encode_metrics().unwrap();
    assert!(
        exported.contains("lumewarp_shared_limits_fallback 1"),
        "{exported}"
    );
    assert!(exported.contains("lumewarp_shared_limits_sync_errors_total"));

    // The next success restores the shared view.
    hub.fail(false);
    task.sync_once().await;
    assert!(registry_a.admit(&key).is_err());
    let exported = runtime.encode_metrics().unwrap();
    assert!(
        exported.contains("lumewarp_shared_limits_fallback 0"),
        "{exported}"
    );
}

#[tokio::test]
async fn a_second_sync_task_sees_the_first() {
    // The task path as production runs it: two registries, two stores, one
    // hub. Both push their counts and both end up refusing a third request.
    let hub = MemoryHub::default();
    let key = caller(2, 0);
    let runtime = |store: MemoryStore| {
        let runtime = Runtime::new();
        let task = SyncTask::new(
            runtime.clone(),
            // Short interval so the clean read-only cadence (five intervals)
            // is due quickly in this test.
            Duration::from_millis(10),
            Arc::from("ns"),
            Duration::ZERO,
            store,
        );
        (runtime, task)
    };
    let (runtime_a, mut task_a) = runtime(hub.store("ns", "a"));
    let (runtime_b, mut task_b) = runtime(hub.store("ns", "b"));
    let registry_a = &runtime_a.limiters;
    let registry_b = &runtime_b.limiters;

    drop(registry_a.admit(&key).unwrap());
    task_a.sync_once().await;
    drop(registry_b.admit(&key).unwrap());
    task_b.sync_once().await;
    // A's scope is clean now; its read-only cadence is five 10 ms intervals.
    tokio::time::sleep(Duration::from_millis(60)).await;
    task_a.sync_once().await;
    assert!(registry_a.admit(&key).is_err());
    assert!(registry_b.admit(&key).is_err());
}

/// A store whose sync can be made slow, to exercise the adaptive interval.
struct SlowStore {
    inner: MemoryStore,
    slow: Arc<AtomicBool>,
}

impl Store for SlowStore {
    async fn sync(&self, batch: &[ScopeCounts]) -> Result<Vec<ScopeView>, Error> {
        if self.slow.load(Ordering::Relaxed) {
            tokio::time::sleep(Duration::from_millis(150)).await;
        }
        self.inner.sync(batch).await
    }
}

/// A store slower than the configured interval. The first sync times out;
/// the interval must grow until the store fits, and stay there with successes.
struct SteadyStore {
    inner: MemoryStore,
    delay: Duration,
    completed: Arc<std::sync::atomic::AtomicUsize>,
}

impl Store for SteadyStore {
    async fn sync(&self, batch: &[ScopeCounts]) -> Result<Vec<ScopeView>, Error> {
        tokio::time::sleep(self.delay).await;
        self.completed
            .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        self.inner.sync(batch).await
    }
}

#[tokio::test]
async fn a_slow_store_reaches_steady_successes() {
    let completed = Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let runtime = Runtime::new();
    let hub = MemoryHub::default();
    let mut task = SyncTask::new(
        runtime,
        Duration::from_millis(100),
        Arc::from("ns"),
        Duration::from_secs(1),
        SteadyStore {
            inner: hub.store("ns", "a"),
            // 1.5 x the configured interval.
            delay: Duration::from_millis(150),
            completed: completed.clone(),
        },
    );
    for _ in 0..6 {
        task.sync_once().await;
    }
    assert!(
        task.interval >= Duration::from_millis(200),
        "the interval must grow past the configured value"
    );
    assert!(!task.fallback, "the successes must leave fallback");
    assert!(task.last_success.is_some());
    // The first sync is cancelled by the first timeout; once the interval has
    // grown the store's 150 ms fits and every later sync completes.
    assert!(
        completed.load(std::sync::atomic::Ordering::Relaxed) >= 4,
        "steady-state syncs must complete"
    );
}

#[tokio::test]
async fn the_interval_backs_off_when_syncs_are_slow() {
    let hub = MemoryHub::default();
    let slow = Arc::new(AtomicBool::new(true));
    let runtime = Runtime::new();
    let mut task = SyncTask::new(
        runtime,
        Duration::from_millis(100),
        Arc::from("ns"),
        Duration::from_secs(1),
        SlowStore {
            inner: hub.store("ns", "a"),
            slow: slow.clone(),
        },
    );
    task.sync_once().await;
    assert_eq!(task.interval, Duration::from_millis(200));
    task.sync_once().await;
    assert_eq!(task.interval, Duration::from_millis(400));
    slow.store(false, Ordering::Relaxed);
    task.sync_once().await;
    assert_eq!(task.interval, Duration::from_millis(200));
    task.sync_once().await;
    assert_eq!(task.interval, Duration::from_millis(100));
    task.sync_once().await;
    assert_eq!(
        task.interval,
        Duration::from_millis(100),
        "never below configured"
    );
}

#[test]
fn configuration_is_validated() {
    assert!(Config::from_args(None, 100, None, None).unwrap().is_none());
    assert!(Config::from_args(Some("  "), 100, Some("ns"), None)
        .unwrap()
        .is_none());
    let config = Config::from_args(Some("redis://127.0.0.1:6379"), 100, None, Some("scope"))
        .unwrap()
        .unwrap();
    assert_eq!(config.namespace, "scope");
    assert_eq!(config.sync_interval, Duration::from_millis(100));
    assert!(Config::from_args(Some("redis://localhost"), 100, None, None).is_err());
    assert!(Config::from_args(Some("http://localhost"), 100, Some("ns"), None).is_err());
    assert!(Config::from_args(Some("rediss://localhost"), 100, Some("a{b}"), None).is_err());
}
