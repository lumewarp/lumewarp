//! Local per-instance rate limiting by `RateLimitScope.id`.
//!
//! State lives in `Runtime` for the life of the process and is keyed by scope
//! id, so snapshot swaps keep it; limits are read from the caller's compiled
//! policy on every request, so a new snapshot changes them in place. N
//! gateway instances allow up to N times each limit (P1 decision).
//!
//! Per scope, all lock-free atomics:
//! - rpm: GCRA (equivalent to a token bucket with capacity `rpm`, refilled at
//!   `rpm / 60` per second), one `AtomicU64` theoretical arrival time.
//! - max_parallel: an atomic counter; `Permit` holds a slot until it is
//!   dropped, i.e. until the response body ends, errors or is dropped.
//! - tpm: retrospective fixed one-minute window (on the gateway's monotonic
//!   clock). `charge_tokens` debits it after usage is known; once a window is
//!   over the limit, admits fail until the next window starts.
//!
//! A key's scopes are acquired all-or-nothing: if one rejects, everything
//! already taken for that request is given back.

use std::sync::{
    atomic::{AtomicU32, AtomicU64, AtomicUsize, Ordering},
    Arc,
};

use lumewarp_proto::config::v1::RateLimit;

use crate::auth::Caller;

/// Cheap clocks for the request path: one atomic load instead of a clock
/// read. A background thread refreshes them every `TICK`, so values are at
/// most one tick stale.
pub mod clock {
    use std::{
        sync::{
            atomic::{AtomicI64, AtomicU64, Ordering},
            OnceLock,
        },
        time::{Duration, Instant, SystemTime, UNIX_EPOCH},
    };

    const TICK: Duration = Duration::from_millis(5);

    static START: OnceLock<Instant> = OnceLock::new();
    static MONO_MS: AtomicU64 = AtomicU64::new(0);
    static UNIX_MS: AtomicI64 = AtomicI64::new(0);

    fn unix_now() -> i64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_or(0, |d| d.as_millis() as i64)
    }

    fn update(start: Instant) {
        MONO_MS.store(start.elapsed().as_millis() as u64, Ordering::Relaxed);
        UNIX_MS.store(unix_now(), Ordering::Relaxed);
    }

    fn start() {
        START.get_or_init(|| {
            let start = Instant::now();
            update(start);
            std::thread::Builder::new()
                .name("lumewarp-clock".into())
                .spawn(move || loop {
                    std::thread::sleep(TICK);
                    update(start);
                })
                .expect("spawning the coarse clock thread");
            start
        });
    }

    /// Milliseconds on a monotonic clock starting near process start.
    pub fn mono_ms() -> u64 {
        start();
        MONO_MS.load(Ordering::Relaxed)
    }

    /// Wall-clock Unix milliseconds.
    pub fn unix_ms() -> i64 {
        start();
        UNIX_MS.load(Ordering::Relaxed)
    }
}

const TOKEN_BITS: u32 = 40;
const TOKEN_MASK: u64 = (1 << TOKEN_BITS) - 1;
const US_PER_MINUTE: u64 = 60_000_000;
/// Run an idle-entry sweep after this many new scope entries.
const PRUNE_EVERY: usize = 1024;

#[derive(Default)]
struct ScopeState {
    /// GCRA theoretical arrival time, microseconds on `clock::mono_ms`.
    tat_us: AtomicU64,
    /// The rpm `tat_us` was last advanced with (0 = never).
    rpm: AtomicU32,
    parallel: AtomicU32,
    /// `(minute << TOKEN_BITS) | tokens` for the current tpm window.
    tpm_window: AtomicU64,
}

fn interval_us(rpm: u32) -> u64 {
    (US_PER_MINUTE / u64::from(rpm)).max(1)
}

fn secs_ceil(us: u64) -> u64 {
    us.div_ceil(1_000_000).max(1)
}

impl ScopeState {
    fn tpm_exceeded(&self, limit: u64, minute: u64) -> bool {
        let window = self.tpm_window.load(Ordering::Relaxed);
        limit != 0 && window >> TOKEN_BITS == minute && window & TOKEN_MASK >= limit
    }

    /// A new snapshot changed this scope's rpm: keep the number of requests
    /// already counted (not the fraction of the minute), so raising a limit
    /// admits more at once and lowering it does not forgive recent traffic.
    fn rescale(&self, rpm: u32, now_us: u64) {
        let old = self.rpm.swap(rpm, Ordering::Relaxed);
        if old == 0 || old == rpm {
            return;
        }
        let (old_interval, new_interval) = (interval_us(old), interval_us(rpm));
        let mut tat = self.tat_us.load(Ordering::Relaxed);
        loop {
            let debt = u128::from(tat.saturating_sub(now_us));
            let scaled = debt * u128::from(new_interval) / u128::from(old_interval);
            let next = now_us.saturating_add(u64::try_from(scaled).unwrap_or(u64::MAX));
            match self
                .tat_us
                .compare_exchange_weak(tat, next, Ordering::Relaxed, Ordering::Relaxed)
            {
                Ok(_) => return,
                Err(actual) => tat = actual,
            }
        }
    }

    /// Takes one rpm token; returns the GCRA increment to refund on rollback.
    fn take_rpm(&self, rpm: u32, now_us: u64) -> Result<u64, RateLimited> {
        if self.rpm.load(Ordering::Relaxed) != rpm {
            self.rescale(rpm, now_us);
        }
        let interval = interval_us(rpm);
        let tolerance = interval * u64::from(rpm - 1);
        let mut tat = self.tat_us.load(Ordering::Relaxed);
        loop {
            let base = tat.max(now_us);
            if base - now_us > tolerance {
                return Err(RateLimited {
                    retry_after_secs: secs_ceil(base - now_us - tolerance),
                });
            }
            match self.tat_us.compare_exchange_weak(
                tat,
                base + interval,
                Ordering::Relaxed,
                Ordering::Relaxed,
            ) {
                Ok(_) => return Ok(interval),
                Err(actual) => tat = actual,
            }
        }
    }

    fn take_parallel(&self, max: u32) -> bool {
        let mut n = self.parallel.load(Ordering::Relaxed);
        while n < max {
            match self
                .parallel
                .compare_exchange_weak(n, n + 1, Ordering::AcqRel, Ordering::Relaxed)
            {
                Ok(_) => return true,
                Err(actual) => n = actual,
            }
        }
        false
    }

    fn charge(&self, tokens: u64, minute: u64) {
        let mut window = self.tpm_window.load(Ordering::Relaxed);
        loop {
            let used = if window >> TOKEN_BITS == minute {
                window & TOKEN_MASK
            } else {
                0
            };
            let next = minute << TOKEN_BITS | used.saturating_add(tokens).min(TOKEN_MASK);
            match self.tpm_window.compare_exchange_weak(
                window,
                next,
                Ordering::Relaxed,
                Ordering::Relaxed,
            ) {
                Ok(_) => return,
                Err(actual) => window = actual,
            }
        }
    }

    fn idle(&self, now_us: u64, minute: u64) -> bool {
        let window = self.tpm_window.load(Ordering::Relaxed);
        self.tat_us.load(Ordering::Relaxed) <= now_us
            && self.parallel.load(Ordering::Relaxed) == 0
            && (window >> TOKEN_BITS != minute || window & TOKEN_MASK == 0)
    }
}

/// Limiter state keyed by scope id; lives in `Runtime`, so it survives swaps.
pub struct Registry {
    scopes: papaya::HashMap<Arc<str>, Arc<ScopeState>>,
    inserts: AtomicUsize,
}

/// One scope's share of an admitted request.
struct Taken {
    state: Arc<ScopeState>,
    /// GCRA increment, refunded only if the request is rolled back.
    rpm_us: u64,
    parallel: bool,
}

/// Held for the lifetime of a request (until its response body ends or is
/// dropped); releases parallel-request slots on drop.
pub struct Permit {
    // Keys have at most org/team/project/key scopes; more spill to the heap.
    inline: [Option<Taken>; 4],
    overflow: Vec<Taken>,
}

impl Permit {
    fn empty() -> Self {
        Self {
            inline: [None, None, None, None],
            overflow: Vec::new(),
        }
    }

    fn push(&mut self, taken: Taken) {
        match self.inline.iter_mut().find(|slot| slot.is_none()) {
            Some(slot) => *slot = Some(taken),
            None => self.overflow.push(taken),
        }
    }

    /// Gives back everything this request took (rpm tokens and, via drop,
    /// parallel slots).
    fn rollback(self) {
        for taken in self.inline.iter().flatten().chain(&self.overflow) {
            if taken.rpm_us != 0 {
                taken
                    .state
                    .tat_us
                    .fetch_sub(taken.rpm_us, Ordering::Relaxed);
            }
        }
    }
}

impl Drop for Permit {
    fn drop(&mut self) {
        for taken in self.inline.iter().flatten().chain(&self.overflow) {
            if taken.parallel {
                taken.state.parallel.fetch_sub(1, Ordering::AcqRel);
            }
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RateLimited {
    pub retry_after_secs: u64,
}

impl Registry {
    pub fn new() -> Self {
        Self {
            scopes: papaya::HashMap::new(),
            inserts: AtomicUsize::new(0),
        }
    }

    fn state(&self, id: &Arc<str>) -> Arc<ScopeState> {
        let scopes = self.scopes.pin();
        if let Some(state) = scopes.get(&**id) {
            return state.clone();
        }
        let state = scopes
            .get_or_insert_with(id.clone(), Default::default)
            .clone();
        drop(scopes);
        if self.inserts.fetch_add(1, Ordering::Relaxed) % PRUNE_EVERY == PRUNE_EVERY - 1 {
            self.prune();
        }
        state
    }

    /// Checks and takes rpm, max_parallel and the tpm window for every scope
    /// of the caller's key, all or nothing. Anonymous callers and keys without
    /// scopes are never limited (and allocate nothing).
    pub fn admit(&self, caller: &Caller) -> Result<Permit, RateLimited> {
        let mut permit = Permit::empty();
        let Caller::Key(key) = caller else {
            return Ok(permit);
        };
        if key.scopes.is_empty() {
            return Ok(permit);
        }
        let now_us = clock::mono_ms() * 1000;
        let minute = now_us / US_PER_MINUTE;
        for (id, limit) in &key.scopes {
            let RateLimit {
                requests_per_minute: rpm,
                tokens_per_minute: tpm,
                max_parallel_requests: max_parallel,
            } = *limit;
            if rpm == 0 && tpm == 0 && max_parallel == 0 {
                continue;
            }
            let state = self.state(id);
            if state.tpm_exceeded(tpm, minute) {
                permit.rollback();
                return Err(RateLimited {
                    retry_after_secs: secs_ceil(US_PER_MINUTE - now_us % US_PER_MINUTE),
                });
            }
            let rpm_us = if rpm == 0 {
                0
            } else {
                match state.take_rpm(rpm, now_us) {
                    Ok(rpm_us) => rpm_us,
                    Err(limited) => {
                        permit.rollback();
                        return Err(limited);
                    }
                }
            };
            let parallel = max_parallel != 0;
            let taken = Taken {
                state,
                rpm_us,
                parallel: false,
            };
            if parallel && !taken.state.take_parallel(max_parallel) {
                permit.push(taken);
                permit.rollback();
                // Unknown when a slot frees up; ask the client to retry soon.
                return Err(RateLimited {
                    retry_after_secs: 1,
                });
            }
            permit.push(Taken { parallel, ..taken });
        }
        Ok(permit)
    }

    /// Debits the tpm window of every scope of the caller's key that has a
    /// tokens-per-minute limit. Called once usage is known (off the hot path).
    #[allow(dead_code)] // Called from usage finalization (W2, G4).
    pub fn charge_tokens(&self, caller: &Caller, tokens: u64) {
        let Caller::Key(key) = caller else {
            return;
        };
        let minute = clock::mono_ms() * 1000 / US_PER_MINUTE;
        for (id, limit) in &key.scopes {
            if limit.tokens_per_minute != 0 {
                self.state(id).charge(tokens, minute);
            }
        }
    }

    /// Drops entries that hold no state worth keeping: no live permits, a
    /// full rpm bucket and no tokens in the current tpm window. An entry
    /// referenced by a `Permit` (or an in-progress admit) is never removed.
    pub fn prune(&self) {
        let now_us = clock::mono_ms() * 1000;
        let minute = now_us / US_PER_MINUTE;
        self.scopes
            .pin()
            .retain(|_, state| Arc::strong_count(state) > 1 || !state.idle(now_us, minute));
    }

    #[cfg(test)]
    fn len(&self) -> usize {
        self.scopes.len()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::router::KeyContext;
    use lumewarp_proto::config::v1::KeyStatus;
    use std::collections::HashSet;

    fn limit(rpm: u32, tpm: u64, parallel: u32) -> RateLimit {
        RateLimit {
            requests_per_minute: rpm,
            tokens_per_minute: tpm,
            max_parallel_requests: parallel,
        }
    }

    fn caller(scopes: &[(&str, RateLimit)]) -> Caller {
        Caller::Key(Arc::new(KeyContext {
            key_id: "k".into(),
            org_id: "".into(),
            team_id: "".into(),
            project_id: "".into(),
            status: KeyStatus::Active,
            expires_at_ms: 0,
            allow_all_models: true,
            allowed_models: HashSet::new(),
            scopes: scopes.iter().map(|(id, l)| ((*id).into(), *l)).collect(),
            team_label: 0,
        }))
    }

    #[test]
    fn unlimited_callers_pass() {
        let registry = Registry::new();
        for _ in 0..1000 {
            drop(registry.admit(&Caller::Anonymous).unwrap());
            drop(registry.admit(&caller(&[])).unwrap());
            drop(registry.admit(&caller(&[("s", limit(0, 0, 0))])).unwrap());
        }
        assert_eq!(registry.len(), 0);
    }

    #[test]
    fn rpm_burst_then_refill() {
        let registry = Registry::new();
        let state = registry.state(&"s".into());
        // Capacity = rpm, deterministic clock.
        let now = 1_000_000_000;
        for _ in 0..3 {
            state.take_rpm(3, now).unwrap();
        }
        let limited = state.take_rpm(3, now).unwrap_err();
        assert_eq!(limited.retry_after_secs, 20, "one token every 20 s");
        // One token after one interval, not more.
        state.take_rpm(3, now + 20_000_000).unwrap();
        assert!(state.take_rpm(3, now + 20_000_000).is_err());
        // A long idle period never stores more than `rpm` tokens.
        let later = now + 3_600_000_000;
        for _ in 0..3 {
            state.take_rpm(3, later).unwrap();
        }
        assert!(state.take_rpm(3, later).is_err());
    }

    #[test]
    fn rpm_exhaustion_through_admit() {
        let registry = Registry::new();
        let key = caller(&[("key:a", limit(5, 0, 0))]);
        for _ in 0..5 {
            drop(registry.admit(&key).unwrap());
        }
        let limited = registry.admit(&key).err().unwrap();
        assert!((1..=12).contains(&limited.retry_after_secs));
    }

    #[test]
    fn parallel_permits_release_on_drop() {
        let registry = Registry::new();
        let key = caller(&[("p", limit(0, 0, 2))]);
        let a = registry.admit(&key).unwrap();
        let b = registry.admit(&key).unwrap();
        assert_eq!(registry.admit(&key).err().unwrap().retry_after_secs, 1);
        drop(a);
        let c = registry.admit(&key).unwrap();
        drop((b, c));
        assert_eq!(
            registry.state(&"p".into()).parallel.load(Ordering::Relaxed),
            0
        );
    }

    #[test]
    fn all_or_nothing_across_scopes() {
        let registry = Registry::new();
        // team allows 1 in parallel, key allows 10 rpm.
        let a = caller(&[("key:a", limit(10, 0, 0)), ("team:t", limit(0, 0, 1))]);
        let held = registry.admit(&a).unwrap();
        for _ in 0..5 {
            assert!(registry.admit(&a).is_err());
        }
        // The rejected attempts refunded their key rpm tokens: 9 remain.
        drop(held);
        for i in 0..9 {
            drop(registry.admit(&a).unwrap_or_else(|_| panic!("admit {i}")));
        }
        assert!(registry.admit(&a).is_err());
        assert_eq!(
            registry
                .state(&"team:t".into())
                .parallel
                .load(Ordering::Relaxed),
            0
        );
        // Rejection by the first scope never touches the second.
        let b = caller(&[("key:b", limit(1, 0, 0)), ("team:u", limit(0, 0, 5))]);
        drop(registry.admit(&b).unwrap());
        assert!(registry.admit(&b).is_err());
        assert_eq!(
            registry
                .state(&"team:u".into())
                .parallel
                .load(Ordering::Relaxed),
            0
        );
    }

    #[test]
    fn shared_scope_across_keys() {
        let registry = Registry::new();
        let team = limit(0, 0, 1);
        let a = caller(&[("team:t", team), ("key:a", limit(0, 0, 5))]);
        let b = caller(&[("team:t", team), ("key:b", limit(0, 0, 5))]);
        let held = registry.admit(&a).unwrap();
        assert!(registry.admit(&b).is_err());
        drop(held);
        drop(registry.admit(&b).unwrap());
    }

    #[test]
    fn tpm_is_retrospective_per_window() {
        let registry = Registry::new();
        let key = caller(&[("t", limit(0, 100, 0))]);
        drop(registry.admit(&key).unwrap());
        registry.charge_tokens(&key, 60);
        drop(registry.admit(&key).unwrap());
        registry.charge_tokens(&key, 60);
        let limited = registry.admit(&key).err().unwrap();
        assert!((1..=60).contains(&limited.retry_after_secs));
        // The next window starts empty.
        let state = registry.state(&"t".into());
        let minute = state.tpm_window.load(Ordering::Relaxed) >> TOKEN_BITS;
        assert!(!state.tpm_exceeded(100, minute + 1));
        state.charge(5, minute + 1);
        assert_eq!(
            state.tpm_window.load(Ordering::Relaxed),
            (minute + 1) << TOKEN_BITS | 5
        );
        // Anonymous charges are ignored.
        registry.charge_tokens(&Caller::Anonymous, 1_000_000);
    }

    #[test]
    fn limits_change_in_place_and_state_survives() {
        let registry = Registry::new();
        // Same scope id, as two consecutive snapshots would compile it.
        let old = caller(&[("team:t", limit(2, 0, 0))]);
        drop(registry.admit(&old).unwrap());
        drop(registry.admit(&old).unwrap());
        assert!(registry.admit(&old).is_err());
        let same = caller(&[("team:t", limit(2, 0, 0))]);
        assert!(registry.admit(&same).is_err(), "state survives recompile");
        // Raising the limit keeps the 2 requests already counted.
        let raised = caller(&[("team:t", limit(4, 0, 0))]);
        drop(registry.admit(&raised).expect("new limit applies at once"));
        drop(registry.admit(&raised).unwrap());
        assert!(registry.admit(&raised).is_err());
        // Lowering it does not forgive them.
        let lowered = caller(&[("team:t", limit(3, 0, 0))]);
        assert!(registry.admit(&lowered).is_err());
    }

    #[test]
    fn prune_keeps_entries_with_live_permits_or_debt() {
        let registry = Registry::new();
        let held = registry
            .admit(&caller(&[("held", limit(0, 0, 1))]))
            .unwrap();
        drop(
            registry
                .admit(&caller(&[("debt", limit(1, 0, 0))]))
                .unwrap(),
        );
        drop(
            registry
                .admit(&caller(&[("idle", limit(0, 0, 1))]))
                .unwrap(),
        );
        let tokens = caller(&[("tokens", limit(0, 10, 0))]);
        registry.charge_tokens(&tokens, 1);
        assert_eq!(registry.len(), 4);
        registry.prune();
        let scopes = registry.scopes.pin();
        assert!(scopes.get("held").is_some());
        assert!(scopes.get("debt").is_some());
        assert!(scopes.get("tokens").is_some());
        assert!(scopes.get("idle").is_none());
        drop(scopes);
        drop(held);
        registry.prune();
        assert!(registry.scopes.pin().get("held").is_none());
    }

    #[test]
    fn concurrent_parallel_limit_is_exact() {
        let registry = Arc::new(Registry::new());
        let key = caller(&[("p", limit(0, 0, 8))]);
        let admitted = Arc::new(AtomicUsize::new(0));
        let permits: Vec<_> = std::thread::scope(|s| {
            let handles: Vec<_> = (0..8)
                .map(|_| {
                    let registry = &registry;
                    let key = &key;
                    let admitted = &admitted;
                    s.spawn(move || {
                        (0..100)
                            .filter_map(|_| registry.admit(key).ok())
                            .inspect(|_| {
                                admitted.fetch_add(1, Ordering::Relaxed);
                            })
                            .collect::<Vec<_>>()
                    })
                })
                .collect();
            handles
                .into_iter()
                .flat_map(|h| h.join().unwrap())
                .collect()
        });
        assert_eq!(permits.len(), 8);
        assert_eq!(admitted.load(Ordering::Relaxed), 8);
        drop(permits);
        assert_eq!(
            registry.state(&"p".into()).parallel.load(Ordering::Relaxed),
            0
        );
    }

    #[test]
    fn coarse_clock_moves() {
        let start = clock::mono_ms();
        let unix = clock::unix_ms();
        assert!(unix > 1_600_000_000_000);
        std::thread::sleep(std::time::Duration::from_millis(30));
        assert!(clock::mono_ms() >= start + 20);
    }

    #[test]
    fn snapshot_swap_keeps_limiter_state() {
        use crate::{
            router::Router,
            runtime::Runtime,
            snapshot::tests::{base, key},
        };
        use lumewarp_config::AuthMode;
        use sha2::{Digest, Sha256};
        let scope = |rpm| lumewarp_proto::config::v1::RateLimitScope {
            id: "team:t1".into(),
            limit: Some(limit(rpm, 0, 0)),
        };
        let mut s = base();
        s.api_keys[0].policy.as_mut().unwrap().rate_limit_scopes = vec![scope(2)];
        let runtime = Runtime::new();
        let hash: [u8; 32] = Sha256::digest(b"s1").into();
        let old = Router::compile(&s, AuthMode::RequireKey, &runtime).unwrap();
        let caller = Caller::Key(old.keys[&hash].clone());
        drop(runtime.limiters.admit(&caller).unwrap());
        drop(runtime.limiters.admit(&caller).unwrap());
        // A key-only delta adds another key of the same team scope.
        let mut next = s.clone();
        next.version = 2;
        let mut k3 = key("k3", "s3", "t1", &["m1"]);
        k3.policy.as_mut().unwrap().rate_limit_scopes = vec![scope(2)];
        next.api_keys.push(k3);
        let new = Router::compile(&next, AuthMode::RequireKey, &runtime).unwrap();
        drop(old);
        let hash3: [u8; 32] = Sha256::digest(b"s3").into();
        for secret in [hash, hash3] {
            let caller = Caller::Key(new.keys[&secret].clone());
            assert!(runtime.limiters.admit(&caller).is_err());
        }
    }
}
