//! Local per-instance rate limiting by `RateLimitScope.id`.
//!
//! State lives in `Runtime` for the life of the process and is keyed by scope
//! id, so snapshot swaps keep it; limits are read from the caller's compiled
//! policy on every request, so a new snapshot changes them in place. N
//! gateway instances allow up to N times each limit (P1 decision).
//!
//! Per scope:
//! - rpm: GCRA (equivalent to a token bucket with capacity `rpm`, refilled at
//!   `rpm / 60` per second). A short mutex protects the rate and theoretical
//!   arrival time together, including rescale, take and rollback.
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
    Arc, Mutex,
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
    rpm: Mutex<RpmBucket>,
    parallel: AtomicU32,
    /// `(minute << TOKEN_BITS) | tokens` for the current tpm window.
    tpm_window: AtomicU64,
}

#[derive(Default)]
struct RpmBucket {
    /// GCRA theoretical arrival time, microseconds on `clock::mono_ms`.
    tat_us: u64,
    /// The rate the debt is expressed in (0 = never).
    rate: u32,
}

impl RpmBucket {
    /// Preserve counted requests, not the fraction of the minute. Round debt
    /// up so repeated snapshot swaps cannot gradually forgive traffic.
    fn rescale(&mut self, rpm: u32, now_us: u64) {
        if self.rate != 0 && self.rate != rpm {
            let debt = u128::from(self.tat_us.saturating_sub(now_us));
            let scaled =
                (debt * u128::from(interval_us(rpm))).div_ceil(u128::from(interval_us(self.rate)));
            self.tat_us = now_us.saturating_add(u64::try_from(scaled).unwrap_or(u64::MAX));
        }
        self.rate = rpm;
    }

    fn take(&mut self, rpm: u32, now_us: u64) -> Result<u64, RateLimited> {
        self.rescale(rpm, now_us);
        let interval = interval_us(rpm);
        let tolerance = interval * u64::from(rpm - 1);
        let base = self.tat_us.max(now_us);
        if base - now_us > tolerance {
            return Err(RateLimited {
                retry_after_secs: secs_ceil(base - now_us - tolerance),
            });
        }
        self.tat_us = base + interval;
        Ok(interval)
    }
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

    /// Gives back one rpm token taken by a request that is being rolled back.
    /// The amount is one interval of the bucket's *current* rpm, not what the
    /// request paid: a snapshot swap may have rescaled the bucket in between,
    /// and refunding stale units could underflow or over-credit it. Saturates
    /// at zero (a TAT in the past is an idle bucket anyway).
    fn refund_rpm(&self) {
        self.refund_rpm_with(|| {});
    }

    /// The no-op hook is optimized away in production; tests pause at the
    /// reviewer's exact stale-interval interleaving point.
    fn refund_rpm_with(&self, after_read: impl FnOnce()) {
        let mut bucket = self.rpm.lock().unwrap();
        if bucket.rate == 0 {
            return;
        }
        let interval = interval_us(bucket.rate);
        let tat = bucket.tat_us;
        after_read();
        bucket.tat_us = tat.saturating_sub(interval);
    }

    /// Takes one rpm token; returns the GCRA increment (nonzero) on success.
    fn take_rpm(&self, rpm: u32, now_us: u64) -> Result<u64, RateLimited> {
        self.rpm.lock().unwrap().take(rpm, now_us)
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
        self.rpm.lock().unwrap().tat_us <= now_us
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
    /// Nonzero when an rpm token was taken; refunded only on rollback.
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
                taken.state.refund_rpm();
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
            team_id: "".into(),
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
    fn rollback_after_rescale_does_not_underflow() {
        let now = 10_000_000_000;
        let state = ScopeState::default();
        // A admits under rpm=10 (interval 6s) and will roll back later.
        let a = state.take_rpm(10, now).unwrap();
        assert_eq!(a, 6_000_000);
        // B's snapshot raises the limit to rpm=100 and rescales the bucket.
        state.take_rpm(100, now).unwrap();
        let before = state.rpm.lock().unwrap().tat_us;
        assert!(before < now + 2_000_000, "debt is in new units: {before}");
        // A is rejected by another scope and rolls back.
        state.refund_rpm();
        let tat = state.rpm.lock().unwrap().tat_us;
        assert!(tat <= before, "refund never charges");
        assert!(tat < now + 2_000_000, "no underflow: {tat}");
        // The bucket is still usable and still limits at the new rate.
        for _ in 0..99 {
            state.take_rpm(100, now).unwrap();
        }
        assert!(state.take_rpm(100, now).is_err());

        // A refund larger than the whole debt saturates at zero.
        let fresh = ScopeState::default();
        fresh.take_rpm(10, 1000).unwrap();
        fresh.rpm.lock().unwrap().tat_us = 5;
        fresh.refund_rpm();
        assert_eq!(fresh.rpm.lock().unwrap().tat_us, 0);
        fresh.take_rpm(10, 1000).unwrap();
    }

    #[test]
    fn r3_refund_racing_with_rescale_keeps_499_requests_charged() {
        use std::sync::Barrier;

        let state = ScopeState::default();
        let now = 100_000_000_000;
        state.take_rpm(1, now).unwrap();
        let entered = Barrier::new(2);
        let release = Barrier::new(2);
        let taking = Barrier::new(2);
        std::thread::scope(|s| {
            let refund = s.spawn(|| {
                state.refund_rpm_with(|| {
                    // Pause after reading the old interval and TAT, exactly
                    // where the reviewer forced the original CAS retry bug.
                    entered.wait();
                    release.wait();
                });
            });
            entered.wait();
            // Rescale/take cannot publish anything while refund holds this
            // state. The old interleaving is now impossible, not just unlikely.
            assert!(matches!(
                state.rpm.try_lock(),
                Err(std::sync::TryLockError::WouldBlock)
            ));
            let taker = s.spawn(|| {
                taking.wait();
                for _ in 0..499 {
                    state.take_rpm(1000, now).unwrap();
                }
            });
            taking.wait();
            release.wait();
            refund.join().unwrap();
            taker.join().unwrap();
        });
        let remaining = (0..1001)
            .take_while(|_| state.take_rpm(1000, now).is_ok())
            .count();
        assert_eq!(
            remaining, 501,
            "499 successful requests must remain charged"
        );
    }

    #[test]
    fn refund_waiting_for_rescale_uses_the_new_rate() {
        use std::sync::Barrier;

        let state = ScopeState::default();
        let now = 100_000_000_000;
        state.take_rpm(1, now).unwrap();
        let mut bucket = state.rpm.lock().unwrap();
        let entered = Barrier::new(2);
        std::thread::scope(|s| {
            let refund = s.spawn(|| {
                entered.wait();
                state.refund_rpm();
            });
            entered.wait();
            for _ in 0..499 {
                bucket.take(1000, now).unwrap();
            }
            drop(bucket);
            refund.join().unwrap();
        });
        let remaining = (0..1001)
            .take_while(|_| state.take_rpm(1000, now).is_ok())
            .count();
        assert_eq!(remaining, 501);
    }

    #[test]
    fn concurrent_snapshot_rates_and_refunds_never_over_admit() {
        use std::sync::Barrier;

        const MAX: u32 = 1000;
        // Freeze time: no refill can obscure an over-admission. Threads use
        // overlapping old/new snapshot policies on the same persistent scope.
        let now = 100_000_000_000;
        for round in 0..8 {
            let state = ScopeState::default();
            let committed = AtomicUsize::new(0);
            let start = Barrier::new(8);
            std::thread::scope(|s| {
                for thread in 0..8 {
                    let (state, committed, start) = (&state, &committed, &start);
                    s.spawn(move || {
                        let mut rng = fastrand::Rng::with_seed(round * 8 + thread);
                        start.wait();
                        for _ in 0..10_000 {
                            let rate = [1, 7, MAX][rng.usize(0..3)];
                            if state.take_rpm(rate, now).is_ok() {
                                if rng.usize(0..8) == 0 {
                                    committed.fetch_add(1, Ordering::Relaxed);
                                } else {
                                    std::thread::yield_now();
                                    state.refund_rpm();
                                }
                            }
                        }
                    });
                }
            });
            let admitted = committed.load(Ordering::Relaxed);
            assert!(admitted <= MAX as usize, "round {round}: {admitted}");
            let remaining = (0..=MAX)
                .take_while(|_| state.take_rpm(MAX, now).is_ok())
                .count();
            assert_eq!(admitted + remaining, MAX as usize, "round {round}");
        }
    }

    #[test]
    fn rescale_never_rounds_away_fractional_debt() {
        let now = 100_000_000_000;
        let mut bucket = RpmBucket {
            rate: 7,
            tat_us: now + 1,
        };
        for _ in 0..100 {
            bucket.rescale(1000, now);
            bucket.rescale(7, now);
            assert!(bucket.tat_us > now);
        }
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

    /// Micro-measure only, not the HTTP benchmark suite:
    /// `cargo test --release --bin lumewarp bench_rpm_state_cost -- --ignored --nocapture`
    #[test]
    #[ignore = "timing micro-measure; run explicitly in release mode"]
    fn bench_rpm_state_cost() {
        use std::{hint::black_box, time::Instant};

        // Previous split-atomic fast path, as a performance baseline only:
        // it is not a safe implementation when rates change concurrently.
        struct SplitAtomic {
            rate: AtomicU32,
            tat: AtomicU64,
        }
        impl SplitAtomic {
            fn take(&self, rpm: u32, now: u64) -> bool {
                assert_eq!(self.rate.load(Ordering::Relaxed), rpm);
                let interval = interval_us(rpm);
                let tolerance = interval * u64::from(rpm - 1);
                let mut tat = self.tat.load(Ordering::Relaxed);
                loop {
                    let base = tat.max(now);
                    if base - now > tolerance {
                        return false;
                    }
                    match self.tat.compare_exchange_weak(
                        tat,
                        base + interval,
                        Ordering::Relaxed,
                        Ordering::Relaxed,
                    ) {
                        Ok(_) => return true,
                        Err(actual) => tat = actual,
                    }
                }
            }
        }
        const N: u32 = 2_000_000;
        let report = |name: &str, f: &mut dyn FnMut(u64)| {
            for i in 0..10_000u32 {
                f(u64::from(i));
            }
            let start = Instant::now();
            for i in 0..N {
                f(black_box(u64::from(i) + US_PER_MINUTE));
            }
            println!(
                "{name}: {:.1} ns/op",
                start.elapsed().as_nanos() as f64 / f64::from(N)
            );
        };
        let old = SplitAtomic {
            rate: AtomicU32::new(u32::MAX),
            tat: AtomicU64::new(0),
        };
        report("split atomic take (unsafe baseline)", &mut |now| {
            black_box(old.take(black_box(u32::MAX), now));
        });
        let state = ScopeState::default();
        report("consistent mutex take", &mut |now| {
            black_box(state.take_rpm(black_box(u32::MAX), now)).unwrap();
        });
        let state = ScopeState::default();
        report("consistent mutex take + refund", &mut |now| {
            black_box(state.take_rpm(black_box(u32::MAX), now)).unwrap();
            state.refund_rpm();
        });
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
