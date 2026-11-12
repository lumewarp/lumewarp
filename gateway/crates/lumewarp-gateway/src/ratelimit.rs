//! Local rate limiting by `RateLimitScope.id`, split into an own and an
//! others half so a shared store can be added without changing the algorithm.
//!
//! rpm and tpm are sliding-window counters on wall-clock minutes: counts for
//! the current and the previous minute, the previous one weighted by how much
//! of the current minute is left. rpm admits while `estimate + 1 <= rpm`; tpm
//! is retrospective (`charge_tokens` after usage is known) and rejects while
//! `estimate >= tpm`. max_parallel is an in-flight gauge whose `Permit` holds
//! a slot until the response body ends, errors or is dropped.
//!
//! Every count is split into own (this process, exact) and others (the other
//! instances, from the last sync). `pending_sync` hands the unsynced own
//! deltas to a store and `apply_sync` writes the others' half back; without a
//! store others stays 0, which is exactly per-instance limiting. A failed
//! sync hands its deltas back with `restore`, and `enter_fallback` drops the
//! others' half entirely when the store is unreachable. `apply_sync` also
//! divides the remaining headroom into a fair share per instance, so N
//! instances do not each admit the whole limit between two syncs; a view the
//! store stops refreshing expires after the previous minute and the shares go
//! back to unlimited.
//!
//! State lives in `Runtime` for the life of the process and is keyed by scope
//! id, so snapshot swaps keep it; limits are read from the caller's compiled
//! policy on every request, so a new snapshot changes them in place. Counts
//! are whole requests and tokens rather than rate-relative debt, so a limit
//! change needs no rescale.
//!
//! A key's scopes are acquired all-or-nothing: if one rejects, everything
//! already taken for that request is given back.

use std::sync::{
    atomic::{AtomicI64, AtomicU32, AtomicUsize, Ordering},
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

const MS_PER_MINUTE: i64 = 60_000;
/// No sync has divided the headroom yet: admits are bounded by the estimate
/// alone, exactly local behaviour.
const UNLIMITED: i64 = i64::MAX;
/// Run an idle-entry sweep after this many new scope entries.
const PRUNE_EVERY: usize = 1024;

/// One dimension's counts for the current and the previous wall-clock minute.
#[derive(Default)]
struct Sliding {
    cur: u64,
    prev: u64,
}

impl Sliding {
    /// A minute is over: its count becomes the previous window, unless a
    /// clock jump skipped whole minutes and the old count is no longer one
    /// minute old.
    fn advance(&mut self, consecutive: bool) {
        self.prev = if consecutive { self.cur } else { 0 };
        self.cur = 0;
    }
}

/// Unsynced own counts for the current and the previous minute. Counts from
/// an older minute are dropped: they no longer take part in any estimate.
#[derive(Default)]
struct DeltaWindow {
    cur: i64,
    prev: i64,
}

impl DeltaWindow {
    fn advance(&mut self, consecutive: bool) {
        self.prev = if consecutive { self.cur } else { 0 };
        self.cur = 0;
    }

    fn is_zero(&self) -> bool {
        self.cur == 0 && self.prev == 0
    }
}

/// The counters an admit needs to see together. One short lock per scope
/// keeps the own and others windows consistent; a packed atomic count would
/// cap tpm at 32 bits per minute.
struct Counters {
    /// The wall-clock minute every `cur` field belongs to.
    minute: u64,
    /// The minute of the last applied view (0 = never). A view older than the
    /// previous minute no longer describes the others' half.
    view_minute: u64,
    rpm: Sliding,
    tpm: Sliding,
    /// The other instances' counts, as of the last sync.
    others_rpm: Sliding,
    others_tpm: Sliding,
    /// Counts taken here but not pushed to the store yet, per minute so a
    /// late sync or an outage cannot move them into the wrong window.
    rpm_delta: DeltaWindow,
    tpm_delta: DeltaWindow,
    /// Limits last seen in a caller's policy. `apply_sync` divides their
    /// headroom without waiting for the next request to arrive.
    rpm_limit: u32,
    tpm_limit: u64,
    parallel_limit: u32,
    /// Requests this process may still admit before the next sync. Not an
    /// atomic: every admit already holds this scope's lock.
    share_rpm: i64,
}

impl Default for Counters {
    fn default() -> Self {
        Self {
            minute: 0,
            view_minute: 0,
            rpm: Sliding::default(),
            tpm: Sliding::default(),
            others_rpm: Sliding::default(),
            others_tpm: Sliding::default(),
            rpm_delta: DeltaWindow::default(),
            tpm_delta: DeltaWindow::default(),
            rpm_limit: 0,
            tpm_limit: 0,
            parallel_limit: 0,
            share_rpm: UNLIMITED,
        }
    }
}

impl Counters {
    /// Advances the windows. Returns true when the last applied view is now
    /// older than the previous minute: its counts and the fair share derived
    /// from them no longer describe the other instances, so the caller must
    /// fall back to local limiting. The `Counters` half is reset here;
    /// `ScopeState::roll` resets the atomics.
    fn roll(&mut self, minute: u64) -> bool {
        if minute > self.minute {
            let consecutive = self.minute + 1 == minute;
            self.rpm.advance(consecutive);
            self.tpm.advance(consecutive);
            self.others_rpm.advance(consecutive);
            self.others_tpm.advance(consecutive);
            self.rpm_delta.advance(consecutive);
            self.tpm_delta.advance(consecutive);
            self.minute = minute;
        }
        if self.view_minute != 0 && self.view_minute < self.minute.saturating_sub(1) {
            self.view_minute = 0;
            self.others_rpm = Sliding::default();
            self.others_tpm = Sliding::default();
            self.share_rpm = UNLIMITED;
            return true;
        }
        false
    }

    /// Local-only mode: nothing another instance reported is trusted.
    fn enter_fallback(&mut self) {
        self.view_minute = 0;
        self.others_rpm = Sliding::default();
        self.others_tpm = Sliding::default();
        self.share_rpm = UNLIMITED;
    }

    /// Puts a delta handed back by a failed sync into the window it belongs
    /// to. A delta older than the previous minute is dropped: it cannot
    /// change an estimate any more.
    fn restore(&mut self, delta: &ScopeDelta) {
        let rpm_prev = delta.rpm_prev;
        let rpm_cur = delta.rpm_cur;
        let tpm_prev = delta.tpm_prev;
        let tpm_cur = delta.tpm_cur;
        if delta.minute == self.minute {
            self.rpm_delta.prev = self.rpm_delta.prev.saturating_add(rpm_prev);
            self.rpm_delta.cur = self.rpm_delta.cur.saturating_add(rpm_cur);
            self.tpm_delta.prev = self.tpm_delta.prev.saturating_add(tpm_prev);
            self.tpm_delta.cur = self.tpm_delta.cur.saturating_add(tpm_cur);
        } else if delta.minute.saturating_add(1) == self.minute {
            self.rpm_delta.prev = self.rpm_delta.prev.saturating_add(rpm_cur);
            self.tpm_delta.prev = self.tpm_delta.prev.saturating_add(tpm_cur);
        } else if delta.minute > self.minute {
            // The clock moved back between the sync and its failure. Keep the
            // counts in the current window rather than lose them.
            self.rpm_delta.cur = self
                .rpm_delta
                .cur
                .saturating_add(rpm_prev.saturating_add(rpm_cur));
            self.tpm_delta.cur = self
                .tpm_delta
                .cur
                .saturating_add(tpm_prev.saturating_add(tpm_cur));
        }
    }

    fn rpm_totals(&self) -> (u64, u64) {
        (
            self.rpm.prev.saturating_add(self.others_rpm.prev),
            self.rpm.cur.saturating_add(self.others_rpm.cur),
        )
    }

    fn tpm_totals(&self) -> (u64, u64) {
        (
            self.tpm.prev.saturating_add(self.others_tpm.prev),
            self.tpm.cur.saturating_add(self.others_tpm.cur),
        )
    }

    /// Stores the others' half of a sync and returns the new parallel share.
    /// The caller has already rolled the counters and rejected views older
    /// than the previous minute.
    fn apply(&mut self, view: &ScopeView, elapsed_ms: u64, own_parallel: u32) -> i64 {
        self.view_minute = view.minute;
        if view.minute == self.minute {
            self.others_rpm = Sliding {
                cur: view.rpm_cur,
                prev: view.rpm_prev,
            };
            self.others_tpm = Sliding {
                cur: view.tpm_cur,
                prev: view.tpm_prev,
            };
        } else {
            // The store answered just before our minute rolled: its current
            // minute is now our previous one.
            self.others_rpm = Sliding {
                cur: 0,
                prev: view.rpm_cur,
            };
            self.others_tpm = Sliding {
                cur: 0,
                prev: view.tpm_cur,
            };
        }
        let (prev, cur) = self.rpm_totals();
        self.share_rpm = fair_share(
            prev,
            cur,
            elapsed_ms,
            u64::from(self.rpm_limit),
            view.rpm_instances,
        );
        let in_flight = u64::from(own_parallel) + u64::from(view.in_flight);
        let headroom = u64::from(self.parallel_limit).saturating_sub(in_flight);
        if self.parallel_limit == 0 || view.in_flight_instances <= 1 {
            UNLIMITED
        } else {
            uniform_share(headroom, view.in_flight_instances)
        }
    }

    /// Some(true) when a finite share slot is now held, Some(false) when no
    /// share is in force, None when the local share is spent.
    fn take_rpm_share(&mut self) -> Option<bool> {
        if self.share_rpm == UNLIMITED {
            return Some(false);
        }
        if self.share_rpm <= 0 {
            return None;
        }
        self.share_rpm -= 1;
        Some(true)
    }
}

fn uniform_share(headroom: u64, instances: u32) -> i64 {
    i64::try_from(u128::from(headroom).div_ceil(u128::from(instances))).unwrap_or(UNLIMITED)
}

/// Sliding-window estimate in whole counts: the previous minute is weighted
/// by how much of it is still inside the one-minute window.
fn estimate(prev: u64, cur: u64, elapsed_ms: u64) -> u128 {
    let permille = u128::from(elapsed_ms) * 1000 / 60_000;
    u128::from(prev) * (1000 - permille) / 1000 + u128::from(cur)
}

fn fair_share(prev: u64, cur: u64, elapsed_ms: u64, limit: u64, instances: u32) -> i64 {
    if limit == 0 || instances <= 1 {
        return UNLIMITED;
    }
    let headroom = u128::from(limit).saturating_sub(estimate(prev, cur, elapsed_ms));
    i64::try_from(headroom.div_ceil(u128::from(instances))).unwrap_or(UNLIMITED)
}

/// Seconds until the sliding estimate is under `limit`, at least 1 so a full
/// window does not invite a hot retry, at most 60 so a client cannot be asked
/// to wait more than one window.
fn retry_after_secs(prev: u64, cur: u64, limit: u64, elapsed_ms: u64) -> u64 {
    let ms = ms_until_below(
        u128::from(prev),
        u128::from(cur),
        u128::from(limit),
        u128::from(elapsed_ms),
    )
    .unwrap_or_else(|| {
        // This minute cannot dip below the limit. From the next boundary on,
        // this minute's count is the previous window and the current window
        // starts empty.
        let next = ms_until_below(u128::from(cur), 0, u128::from(limit), 0).unwrap_or(60_000);
        u128::from(60_000u64.saturating_sub(elapsed_ms)) + next
    });
    u64::try_from(ms.div_ceil(1000)).unwrap_or(60).clamp(1, 60)
}

/// Milliseconds until `prev * (1000 - f) / 1000 + cur < limit` first holds,
/// if it holds before this minute ends (f = elapsed / 60 per mille).
fn ms_until_below(prev: u128, cur: u128, limit: u128, elapsed_ms: u128) -> Option<u128> {
    if cur >= limit {
        return None;
    }
    if prev == 0 {
        return Some(0);
    }
    // floor(prev * k / 1000) < limit - cur exactly when prev * k < room, for
    // k = 1000 - f. The largest k that fits gives the smallest f.
    let room = (limit - cur) * 1000;
    let max_k = (room - 1) / prev;
    if max_k >= 1000 {
        return Some(0);
    }
    let f_min = 1000 - max_k;
    if f_min >= 1000 {
        return None;
    }
    Some((f_min * 60).saturating_sub(elapsed_ms))
}

struct ScopeState {
    counters: Mutex<Counters>,
    /// In-flight requests served by this process.
    parallel: AtomicU32,
    /// In-flight requests at the other instances, from the last sync.
    others_parallel: AtomicU32,
    /// In-flight slots this process may still take before the next sync.
    share_parallel: AtomicI64,
}

impl Default for ScopeState {
    fn default() -> Self {
        Self {
            counters: Mutex::default(),
            parallel: AtomicU32::default(),
            others_parallel: AtomicU32::default(),
            share_parallel: AtomicI64::new(UNLIMITED),
        }
    }
}

impl ScopeState {
    /// Rolls the counters and, when the last view has gone stale, drops the
    /// parallel half of it too, so a scope the store stopped reporting falls
    /// back to local limiting instead of freezing at its old share.
    fn roll(&self, counters: &mut Counters, minute: u64) {
        if counters.roll(minute) {
            self.others_parallel.store(0, Ordering::Relaxed);
            self.share_parallel.store(UNLIMITED, Ordering::Relaxed);
        }
    }

    fn take_parallel(&self, max: u32) -> bool {
        loop {
            let others = self.others_parallel.load(Ordering::Relaxed);
            let own = self.parallel.load(Ordering::Relaxed);
            if own.saturating_add(others) >= max {
                return false;
            }
            // Both halves are re-read on a lost race, so a sync that lands
            // between two attempts is respected.
            if self
                .parallel
                .compare_exchange_weak(own, own + 1, Ordering::AcqRel, Ordering::Relaxed)
                .is_ok()
            {
                return true;
            }
        }
    }

    fn try_take_parallel_share(&self) -> Option<bool> {
        let mut left = self.share_parallel.load(Ordering::Relaxed);
        loop {
            if left == UNLIMITED {
                return Some(false);
            }
            if left <= 0 {
                return None;
            }
            match self.share_parallel.compare_exchange_weak(
                left,
                left - 1,
                Ordering::AcqRel,
                Ordering::Relaxed,
            ) {
                Ok(_) => return Some(true),
                Err(actual) => left = actual,
            }
        }
    }

    fn release_parallel_share(&self) {
        let mut left = self.share_parallel.load(Ordering::Relaxed);
        loop {
            match self.share_parallel.compare_exchange_weak(
                left,
                left.saturating_add(1),
                Ordering::Relaxed,
                Ordering::Relaxed,
            ) {
                Ok(_) => return,
                Err(actual) => left = actual,
            }
        }
    }

    /// Gives back one rpm count taken for a request that is rolled back. The
    /// count may have moved to the previous window if the minute rolled in
    /// between; the delta of the minute it was taken in is reduced either
    /// way, so the store stays consistent. A count older than the previous
    /// minute has no window and no delta left to give back.
    fn refund_rpm(&self, now_minute: u64, taken_minute: u64, share: bool) {
        let mut counters = self.counters.lock().unwrap();
        self.roll(&mut counters, now_minute);
        if taken_minute == counters.minute {
            counters.rpm.cur = counters.rpm.cur.saturating_sub(1);
            counters.rpm_delta.cur = counters.rpm_delta.cur.saturating_sub(1);
        } else if taken_minute.saturating_add(1) == counters.minute {
            counters.rpm.prev = counters.rpm.prev.saturating_sub(1);
            counters.rpm_delta.prev = counters.rpm_delta.prev.saturating_sub(1);
        }
        if share && counters.share_rpm != UNLIMITED {
            counters.share_rpm = counters.share_rpm.saturating_add(1);
        }
    }

    /// Nothing here is worth keeping: no live permit, no unsynced delta, no
    /// count in the current or previous window.
    fn idle(&self, minute: u64) -> bool {
        if self.parallel.load(Ordering::Relaxed) != 0 {
            return false;
        }
        let mut counters = self.counters.lock().unwrap();
        self.roll(&mut counters, minute);
        if !counters.rpm_delta.is_zero() || !counters.tpm_delta.is_zero() {
            return false;
        }
        counters.rpm.cur == 0
            && counters.rpm.prev == 0
            && counters.tpm.cur == 0
            && counters.tpm.prev == 0
            && counters.others_rpm.cur == 0
            && counters.others_rpm.prev == 0
            && counters.others_tpm.cur == 0
            && counters.others_tpm.prev == 0
    }
}

/// The counts one scope owes the shared store, and its own in-flight count.
/// Each delta belongs to the minute its count was taken in, so a late sync
/// (or a sync replayed after an outage) cannot move counts into a window
/// where they would carry the wrong weight.
#[cfg_attr(not(test), allow(dead_code))] // The Redis sync task is the first user.
#[derive(Debug, Clone)]
pub struct ScopeDelta {
    pub id: Arc<str>,
    /// The current wall-clock minute; the `prev` deltas are for `minute - 1`.
    pub minute: u64,
    pub rpm_prev: i64,
    pub rpm_cur: i64,
    pub tpm_prev: i64,
    pub tpm_cur: i64,
    pub in_flight: u32,
}

/// One scope's view from the shared store: the other instances' counts for
/// `minute` and `minute - 1`, how many instances were active in each
/// dimension, and their in-flight requests.
///
/// `in_flight_instances` counts every instance with a fresh in-flight entry
/// for the scope, including this one and including instances whose value is
/// zero: every instance writes its entry for every active scope on every
/// sync, so `n` is the number of live instances that know the scope. The
/// limiter divides the parallel headroom by it.
#[cfg_attr(not(test), allow(dead_code))] // The Redis sync task is the first user.
#[derive(Debug, Clone)]
pub struct ScopeView {
    pub id: Arc<str>,
    pub minute: u64,
    pub rpm_prev: u64,
    pub rpm_cur: u64,
    pub tpm_prev: u64,
    pub tpm_cur: u64,
    pub rpm_instances: u32,
    pub tpm_instances: u32,
    pub in_flight: u32,
    pub in_flight_instances: u32,
}

/// Wall-clock source for the windows. Tests replace it so no test has to
/// sleep across a minute boundary.
enum Clock {
    Real,
    #[cfg(test)]
    Fake(Arc<AtomicI64>),
}

impl Clock {
    fn now_ms(&self) -> i64 {
        match self {
            Self::Real => clock::unix_ms(),
            #[cfg(test)]
            Self::Fake(ms) => ms.load(Ordering::Relaxed),
        }
    }
}

/// Limiter state keyed by scope id; lives in `Runtime`, so it survives swaps.
pub struct Registry {
    scopes: papaya::HashMap<Arc<str>, Arc<ScopeState>>,
    clock: Clock,
    inserts: AtomicUsize,
}

/// One scope's share of an admitted request.
struct Taken {
    state: Arc<ScopeState>,
    /// The minute the rpm count was taken in.
    minute: u64,
    rpm: bool,
    rpm_share: bool,
    parallel: bool,
    parallel_share: bool,
}

/// Held for the lifetime of a request (until its response body ends or is
/// dropped); releases parallel-request slots on drop.
pub struct Permit {
    // Keys have at most org/team/member/key scopes; more spill to the heap.
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

    /// Gives back the rpm counts and shares this request took; dropping the
    /// permit on return releases the parallel slots and their shares.
    fn rollback(self, registry: &Registry) {
        let minute = registry.minute_now();
        for taken in self.inline.iter().flatten().chain(&self.overflow) {
            if taken.rpm {
                taken
                    .state
                    .refund_rpm(minute, taken.minute, taken.rpm_share);
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
            if taken.parallel_share {
                taken.state.release_parallel_share();
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
            clock: Clock::Real,
            inserts: AtomicUsize::new(0),
        }
    }

    #[cfg(test)]
    fn with_clock(ms: Arc<AtomicI64>) -> Self {
        Self {
            scopes: papaya::HashMap::new(),
            clock: Clock::Fake(ms),
            inserts: AtomicUsize::new(0),
        }
    }

    fn now(&self) -> (u64, u64) {
        let ms = self.clock.now_ms();
        (minute_of(ms), elapsed_of(ms))
    }

    fn minute_now(&self) -> u64 {
        minute_of(self.clock.now_ms())
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

    /// Checks and takes rpm, max_parallel and the tpm estimate for every scope
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
        let (mut minute, mut elapsed) = self.now();
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
            let mut counters = state.counters.lock().unwrap();
            state.roll(&mut counters, minute);
            // Another request may have rolled this scope to a newer minute
            // while we waited for the lock. The captured elapsed would then
            // weight the wrong window (up to a whole minute of counts at
            // ~1/1000), so re-derive both and stay strict with elapsed 0.
            if counters.minute > minute {
                minute = counters.minute;
                elapsed = 0;
            }
            counters.rpm_limit = rpm;
            counters.tpm_limit = tpm;
            counters.parallel_limit = max_parallel;
            if tpm != 0 {
                let (prev, cur) = counters.tpm_totals();
                if estimate(prev, cur, elapsed) >= u128::from(tpm) {
                    drop(counters);
                    permit.rollback(self);
                    return Err(RateLimited {
                        retry_after_secs: retry_after_secs(prev, cur, tpm, elapsed),
                    });
                }
            }
            let mut rpm_taken = false;
            let mut rpm_share = false;
            if rpm != 0 {
                let (prev, cur) = counters.rpm_totals();
                if estimate(prev, cur, elapsed) + 1 > u128::from(rpm) {
                    drop(counters);
                    permit.rollback(self);
                    return Err(RateLimited {
                        retry_after_secs: retry_after_secs(prev, cur, u64::from(rpm), elapsed),
                    });
                }
                match counters.take_rpm_share() {
                    Some(share) => rpm_share = share,
                    None => {
                        drop(counters);
                        permit.rollback(self);
                        // The share is redivided at the next sync.
                        return Err(RateLimited {
                            retry_after_secs: 1,
                        });
                    }
                }
                counters.rpm.cur = counters.rpm.cur.saturating_add(1);
                counters.rpm_delta.cur = counters.rpm_delta.cur.saturating_add(1);
                rpm_taken = true;
            }
            let taken_minute = counters.minute;
            drop(counters);
            let mut taken = Taken {
                state,
                minute: taken_minute,
                rpm: rpm_taken,
                rpm_share,
                parallel: false,
                parallel_share: false,
            };
            if max_parallel != 0 {
                if !taken.state.take_parallel(max_parallel) {
                    permit.push(taken);
                    permit.rollback(self);
                    // Unknown when a slot frees up; ask the client to retry.
                    return Err(RateLimited {
                        retry_after_secs: 1,
                    });
                }
                taken.parallel = true;
                match taken.state.try_take_parallel_share() {
                    Some(share) => taken.parallel_share = share,
                    None => {
                        permit.push(taken);
                        permit.rollback(self);
                        return Err(RateLimited {
                            retry_after_secs: 1,
                        });
                    }
                }
            }
            permit.push(taken);
        }
        Ok(permit)
    }

    /// Debits the tpm window of every scope of the caller's key that has a
    /// tokens-per-minute limit. Called once usage is known (off the hot path).
    pub fn charge_tokens(&self, caller: &Caller, tokens: u64) {
        let Caller::Key(key) = caller else {
            return;
        };
        let (mut minute, _) = self.now();
        for (id, limit) in &key.scopes {
            if limit.tokens_per_minute == 0 {
                continue;
            }
            let state = self.state(id);
            let mut counters = state.counters.lock().unwrap();
            state.roll(&mut counters, minute);
            if counters.minute > minute {
                minute = counters.minute;
            }
            counters.tpm_limit = limit.tokens_per_minute;
            counters.tpm.cur = counters.tpm.cur.saturating_add(tokens);
            let delta = i64::try_from(tokens).unwrap_or(i64::MAX);
            counters.tpm_delta.cur = counters.tpm_delta.cur.saturating_add(delta);
        }
    }

    /// D20: charges one request against every scope's `requests_per_minute`
    /// only, and reports whether it was allowed.
    ///
    /// The MCP proxy calls this for `tools/call` (D20 amended D16): an MCP call
    /// is not billed, takes no parallel slot and consumes no tokens, but it
    /// counts as a request against the scopes the key already has, so a team's
    /// RPM limit covers tool calls too. A2A is not limited. Either every scope
    /// accepts the request or none is charged. Nothing is held afterwards (an
    /// MCP call may run long and never holds a parallel slot).
    pub fn charge_request(&self, caller: &Caller) -> Result<(), RateLimited> {
        let Caller::Key(key) = caller else {
            return Ok(());
        };
        if key.scopes.is_empty() {
            return Ok(());
        }
        let (mut minute, mut elapsed) = self.now();
        let mut charged = Permit::empty();
        for (id, limit) in &key.scopes {
            let rpm = limit.requests_per_minute;
            if rpm == 0 {
                continue;
            }
            let state = self.state(id);
            let mut counters = state.counters.lock().unwrap();
            state.roll(&mut counters, minute);
            if counters.minute > minute {
                minute = counters.minute;
                elapsed = 0;
            }
            counters.rpm_limit = rpm;
            let (prev, cur) = counters.rpm_totals();
            if estimate(prev, cur, elapsed) + 1 > u128::from(rpm) {
                drop(counters);
                charged.rollback(self);
                return Err(RateLimited {
                    retry_after_secs: retry_after_secs(prev, cur, u64::from(rpm), elapsed),
                });
            }
            let Some(share) = counters.take_rpm_share() else {
                drop(counters);
                charged.rollback(self);
                return Err(RateLimited {
                    retry_after_secs: 1,
                });
            };
            counters.rpm.cur = counters.rpm.cur.saturating_add(1);
            counters.rpm_delta.cur = counters.rpm_delta.cur.saturating_add(1);
            let taken_minute = counters.minute;
            drop(counters);
            charged.push(Taken {
                state,
                minute: taken_minute,
                rpm: true,
                rpm_share: share,
                parallel: false,
                parallel_share: false,
            });
        }
        Ok(())
    }

    /// Hands the unsynced counts of every scope with local activity to the
    /// caller and resets them, leaving increments made during the call for
    /// the next one. Deltas carry both the current and the previous minute so
    /// a late sync keeps each count in the window where it was taken.
    ///
    /// A scope with a finite share or a non-zero others' half is reported
    /// even without own traffic: the store has to keep returning a view for
    /// it, otherwise `apply_sync` would never rewrite the share and a scope
    /// whose own counts aged out would reject forever. A scope whose limits
    /// are not known yet is skipped; there is nothing to divide or enforce.
    #[cfg_attr(not(test), allow(dead_code))] // The Redis sync task is the first user.
    pub fn pending_sync(&self) -> Vec<ScopeDelta> {
        let (minute, _) = self.now();
        let mut deltas = Vec::new();
        for (id, state) in self.scopes.pin().iter() {
            let mut counters = state.counters.lock().unwrap();
            state.roll(&mut counters, minute);
            if counters.rpm_limit == 0 && counters.tpm_limit == 0 && counters.parallel_limit == 0 {
                continue;
            }
            let in_flight = state.parallel.load(Ordering::Relaxed);
            let active = in_flight != 0
                || counters.share_rpm != UNLIMITED
                || state.share_parallel.load(Ordering::Relaxed) != UNLIMITED
                || state.others_parallel.load(Ordering::Relaxed) != 0
                || !counters.rpm_delta.is_zero()
                || !counters.tpm_delta.is_zero()
                || counters.rpm.cur != 0
                || counters.rpm.prev != 0
                || counters.tpm.cur != 0
                || counters.tpm.prev != 0
                || counters.others_rpm.cur != 0
                || counters.others_rpm.prev != 0
                || counters.others_tpm.cur != 0
                || counters.others_tpm.prev != 0;
            if !active {
                continue;
            }
            let rpm = std::mem::take(&mut counters.rpm_delta);
            let tpm = std::mem::take(&mut counters.tpm_delta);
            deltas.push(ScopeDelta {
                id: id.clone(),
                minute: counters.minute,
                rpm_prev: rpm.prev,
                rpm_cur: rpm.cur,
                tpm_prev: tpm.prev,
                tpm_cur: tpm.cur,
                in_flight,
            });
        }
        deltas
    }

    /// Stores one sync's view of the other instances per scope and redivides
    /// the remaining headroom. Results for a minute that is no longer the
    /// current or the previous one are ignored: the windows have moved on.
    #[cfg_attr(not(test), allow(dead_code))] // The Redis sync task is the first user.
    pub fn apply_sync(&self, results: &[ScopeView]) {
        let (minute, elapsed) = self.now();
        for view in results {
            if view.minute > minute || view.minute < minute.saturating_sub(1) {
                continue;
            }
            let Some(state) = self.scopes.pin().get(&*view.id).cloned() else {
                continue;
            };
            let own_parallel = state.parallel.load(Ordering::Relaxed);
            let share_parallel = {
                let mut counters = state.counters.lock().unwrap();
                state.roll(&mut counters, minute);
                // An admit may have rolled this scope past the minute we read
                // the clock in; the old elapsed would weight the wrong window.
                let elapsed = if counters.minute > minute { 0 } else { elapsed };
                counters.apply(view, elapsed, own_parallel)
            };
            state
                .others_parallel
                .store(view.in_flight, Ordering::Relaxed);
            state
                .share_parallel
                .store(share_parallel, Ordering::Relaxed);
        }
    }

    /// Drops every others' count and every fair share: the store has been
    /// unreachable long enough that this process counts locally only, as the
    /// design's fallback requires. Own counts and deltas are kept, so they
    /// are still pushed when the store comes back.
    #[cfg_attr(not(test), allow(dead_code))] // The Redis sync task is the first user.
    pub fn enter_fallback(&self) {
        let minute = self.minute_now();
        for (_, state) in self.scopes.pin().iter() {
            let mut counters = state.counters.lock().unwrap();
            state.roll(&mut counters, minute);
            counters.enter_fallback();
            state.others_parallel.store(0, Ordering::Relaxed);
            state.share_parallel.store(UNLIMITED, Ordering::Relaxed);
        }
    }

    /// Puts deltas from a sync that failed back into their windows. A delta
    /// from a minute older than the previous one is dropped: its window no
    /// longer takes part in any estimate, so pushing it later would only
    /// shift the count into a minute where it was not taken.
    #[cfg_attr(not(test), allow(dead_code))] // The Redis sync task is the first user.
    pub fn restore(&self, deltas: &[ScopeDelta]) {
        let minute = self.minute_now();
        for delta in deltas {
            let state = self.state(&delta.id);
            let mut counters = state.counters.lock().unwrap();
            state.roll(&mut counters, minute);
            counters.restore(delta);
        }
    }

    /// Drops entries that hold no state worth keeping: no live permits, no
    /// unsynced delta and no counts in the current or previous window. An
    /// entry referenced by a `Permit` (or an in-progress admit) is never
    /// removed.
    ///
    /// `retain` can remove an idle entry between the `get` and the clone in
    /// `state`: the request that won that race counts into an orphaned entry
    /// whose delta is never synced and whose permit releases into the orphan.
    /// The lost count is one request, and only for an entry that was already
    /// idle, so it is accepted rather than serializing every admit behind
    /// prune.
    pub fn prune(&self) {
        let minute = self.minute_now();
        self.scopes
            .pin()
            .retain(|_, state| Arc::strong_count(state) > 1 || !state.idle(minute));
    }

    #[cfg(test)]
    fn len(&self) -> usize {
        self.scopes.len()
    }
}

impl Default for Registry {
    fn default() -> Self {
        Self::new()
    }
}

fn minute_of(ms: i64) -> u64 {
    u64::try_from(ms.div_euclid(MS_PER_MINUTE)).unwrap_or(0)
}

fn elapsed_of(ms: i64) -> u64 {
    u64::try_from(ms.rem_euclid(MS_PER_MINUTE)).unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::router::KeyContext;
    use lumewarp_proto::config::v1::KeyStatus;
    use std::collections::{HashMap, HashSet};

    /// An exact minute boundary, so a test can pick the elapsed part cleanly.
    const BASE_MINUTE: u64 = 28_333_334;

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
            grants: crate::grants::Grants::none(),
        }))
    }

    fn at(minute: u64, elapsed_ms: i64) -> Arc<AtomicI64> {
        let minute = i64::try_from(minute).unwrap();
        Arc::new(AtomicI64::new(minute * MS_PER_MINUTE + elapsed_ms))
    }

    fn registry_at(minute: u64, elapsed_ms: i64) -> (Registry, Arc<AtomicI64>) {
        let now = at(minute, elapsed_ms);
        (Registry::with_clock(now.clone()), now)
    }

    fn set(now: &AtomicI64, minute: u64, elapsed_ms: i64) {
        let minute = i64::try_from(minute).unwrap();
        now.store(minute * MS_PER_MINUTE + elapsed_ms, Ordering::Relaxed);
    }

    fn view(id: &str, minute: u64) -> ScopeView {
        ScopeView {
            id: id.into(),
            minute,
            rpm_prev: 0,
            rpm_cur: 0,
            tpm_prev: 0,
            tpm_cur: 0,
            rpm_instances: 0,
            tpm_instances: 0,
            in_flight: 0,
            in_flight_instances: 0,
        }
    }

    #[test]
    fn unlimited_callers_pass() {
        let (registry, _now) = registry_at(BASE_MINUTE, 0);
        for _ in 0..1000 {
            drop(registry.admit(&Caller::Anonymous).unwrap());
            drop(registry.admit(&caller(&[])).unwrap());
            drop(registry.admit(&caller(&[("s", limit(0, 0, 0))])).unwrap());
        }
        assert_eq!(registry.len(), 0);
    }

    #[test]
    fn rpm_burst_then_retry_after() {
        let (registry, now) = registry_at(BASE_MINUTE, 1_000);
        let key = caller(&[("key:a", limit(5, 0, 0))]);
        for _ in 0..5 {
            drop(registry.admit(&key).unwrap());
        }
        let limited = registry.admit(&key).err().unwrap();
        assert_eq!(limited.retry_after_secs, 60, "a full minute must pass");
        // The burst counts fully for one more minute, then decays evenly:
        // one slot is back after 60 ms.
        set(&now, BASE_MINUTE + 1, 0);
        assert_eq!(registry.admit(&key).err().unwrap().retry_after_secs, 1);
        set(&now, BASE_MINUTE + 1, 60);
        drop(registry.admit(&key).unwrap());
        assert!(registry.admit(&key).is_err());
    }

    #[test]
    fn sliding_window_ages_the_previous_minute() {
        let (registry, now) = registry_at(BASE_MINUTE, 0);
        let key = caller(&[("key:a", limit(10, 0, 0))]);
        for _ in 0..10 {
            drop(registry.admit(&key).unwrap());
        }
        set(&now, BASE_MINUTE + 1, 0);
        assert!(registry.admit(&key).is_err(), "the old burst still counts");
        // At 12 s the previous minute is 20% gone: two slots are back.
        set(&now, BASE_MINUTE + 1, 12_000);
        drop(registry.admit(&key).unwrap());
        drop(registry.admit(&key).unwrap());
        assert!(registry.admit(&key).is_err());
        // Two minutes later only the minute 1 counts (2) remain.
        set(&now, BASE_MINUTE + 2, 0);
        for _ in 0..8 {
            drop(registry.admit(&key).unwrap());
        }
        assert!(registry.admit(&key).is_err());
        // A skipped minute (no traffic) drops even those.
        set(&now, BASE_MINUTE + 4, 0);
        for _ in 0..10 {
            drop(registry.admit(&key).unwrap());
        }
        assert!(registry.admit(&key).is_err());
    }

    #[test]
    fn tpm_is_retrospective_and_recovers() {
        let (registry, now) = registry_at(BASE_MINUTE, 0);
        let key = caller(&[("t", limit(0, 100, 0))]);
        drop(registry.admit(&key).unwrap());
        registry.charge_tokens(&key, 60);
        drop(registry.admit(&key).unwrap());
        registry.charge_tokens(&key, 60);
        let limited = registry.admit(&key).err().unwrap();
        assert_eq!(limited.retry_after_secs, 60);
        // Next minute the whole 120 is the previous window: rejected at the
        // boundary, and under the limit once 167 per mille has elapsed.
        set(&now, BASE_MINUTE + 1, 0);
        let limited = registry.admit(&key).err().unwrap();
        assert_eq!(limited.retry_after_secs, 11);
        set(&now, BASE_MINUTE + 1, 10_020);
        drop(registry.admit(&key).unwrap());
        // Two minutes later nothing is left.
        set(&now, BASE_MINUTE + 3, 0);
        drop(registry.admit(&key).unwrap());
        // Anonymous charges are ignored.
        registry.charge_tokens(&Caller::Anonymous, 1_000_000);
    }

    #[test]
    fn max_parallel_permits_release_on_drop() {
        let (registry, _now) = registry_at(BASE_MINUTE, 0);
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
        let (registry, _now) = registry_at(BASE_MINUTE, 0);
        // team allows 1 in parallel, key allows 10 rpm.
        let a = caller(&[("key:a", limit(10, 0, 0)), ("team:t", limit(0, 0, 1))]);
        let held = registry.admit(&a).unwrap();
        for _ in 0..5 {
            assert!(registry.admit(&a).is_err());
        }
        // The rejected attempts refunded their key rpm counts: 9 remain.
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
        let (registry, _now) = registry_at(BASE_MINUTE, 0);
        let team = limit(0, 0, 1);
        let a = caller(&[("team:t", team), ("key:a", limit(0, 0, 5))]);
        let b = caller(&[("team:t", team), ("key:b", limit(0, 0, 5))]);
        let held = registry.admit(&a).unwrap();
        assert!(registry.admit(&b).is_err());
        drop(held);
        drop(registry.admit(&b).unwrap());
    }

    #[test]
    fn limits_change_in_place() {
        let (registry, _now) = registry_at(BASE_MINUTE, 0);
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
    fn prune_keeps_entries_with_live_state() {
        let (registry, now) = registry_at(BASE_MINUTE, 0);
        let held = registry
            .admit(&caller(&[("held", limit(0, 0, 1))]))
            .unwrap();
        drop(registry.admit(&caller(&[("rpm", limit(1, 0, 0))])).unwrap());
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
        for id in ["held", "rpm", "tokens"] {
            assert!(scopes.get(id).is_some(), "{id} kept");
        }
        assert!(scopes.get("idle").is_none(), "idle dropped");
        drop(scopes);
        // Clear the unsynced deltas, then two minutes later the old counts
        // have aged out too and only the live permit keeps its entry.
        registry.pending_sync();
        set(&now, BASE_MINUTE + 2, 0);
        registry.prune();
        let scopes = registry.scopes.pin();
        assert!(scopes.get("rpm").is_none());
        assert!(scopes.get("tokens").is_none());
        assert!(scopes.get("held").is_some(), "live permit kept");
        drop(scopes);
        // A delta left by a refund keeps an entry alive even after its count
        // is gone and the window rolled.
        drop(
            registry
                .admit(&caller(&[("delta", limit(1, 0, 0))]))
                .unwrap(),
        );
        registry.pending_sync();
        set(&now, BASE_MINUTE + 3, 0);
        registry
            .state(&"delta".into())
            .refund_rpm(BASE_MINUTE + 3, BASE_MINUTE + 2, false);
        registry.prune();
        assert!(
            registry.scopes.pin().get("delta").is_some(),
            "unsynced delta"
        );
        assert_eq!(registry.pending_sync().len(), 2, "held plus delta");
        registry.prune();
        assert!(registry.scopes.pin().get("delta").is_none());
        drop(held);
        registry.prune();
        assert!(registry.scopes.pin().get("held").is_none());
    }

    /// D20: one MCP `tools/call` costs one request on every scope, and only
    /// the requests-per-minute dimension applies (no parallel slot, no tokens).
    #[test]
    fn charge_request_counts_rpm_only() {
        let (registry, _now) = registry_at(BASE_MINUTE, 0);
        let key = caller(&[("key:a", limit(3, 0, 0))]);
        for _ in 0..3 {
            registry.charge_request(&key).unwrap();
        }
        let limited = registry.charge_request(&key).err().unwrap();
        assert!((1..=60).contains(&limited.retry_after_secs));
        // Anonymous callers and keys without scopes are never limited.
        assert!(registry.charge_request(&Caller::Anonymous).is_ok());
        assert!(registry.charge_request(&caller(&[])).is_ok());

        // tokens_per_minute and max_parallel_requests do not apply, and a
        // charge leaves no parallel slot behind.
        let other = caller(&[("key:b", limit(0, 1, 1))]);
        for _ in 0..10 {
            registry.charge_request(&other).unwrap();
        }
        drop(registry.admit(&other).unwrap());
    }

    #[test]
    fn charge_request_is_all_or_nothing_across_scopes() {
        let (registry, _now) = registry_at(BASE_MINUTE, 0);
        let both = caller(&[("a", limit(3, 0, 0)), ("b", limit(2, 0, 0))]);
        registry.charge_request(&both).unwrap();
        registry.charge_request(&both).unwrap();
        // The third call takes a's count, then b refuses: a's count must be
        // given back, which the next caller (scope a only) proves.
        assert!(registry.charge_request(&both).is_err());
        let only_a = caller(&[("a", limit(3, 0, 0))]);
        registry.charge_request(&only_a).unwrap();
        assert!(registry.charge_request(&only_a).is_err());
    }

    #[test]
    fn pending_sync_returns_and_resets_deltas() {
        let (registry, now) = registry_at(BASE_MINUTE, 0);
        let key = caller(&[("s", limit(10, 100, 0))]);
        drop(registry.admit(&key).unwrap());
        drop(registry.admit(&key).unwrap());
        registry.charge_tokens(&key, 30);
        let deltas = registry.pending_sync();
        assert_eq!(deltas.len(), 1);
        assert_eq!(&*deltas[0].id, "s");
        assert_eq!(deltas[0].minute, BASE_MINUTE);
        assert_eq!((deltas[0].rpm_prev, deltas[0].rpm_cur), (0, 2));
        assert_eq!((deltas[0].tpm_prev, deltas[0].tpm_cur), (0, 30));
        assert_eq!(deltas[0].in_flight, 0);
        // The deltas are handed over once; the window keeps the counts.
        let again = registry.pending_sync();
        assert_eq!(again.len(), 1);
        assert_eq!((again[0].rpm_prev, again[0].rpm_cur), (0, 0));
        assert_eq!((again[0].tpm_prev, again[0].tpm_cur), (0, 0));
        // A charge from the minute that just ended belongs to `prev`, not to
        // the minute the sync happens to run in.
        registry.charge_tokens(&key, 5);
        set(&now, BASE_MINUTE + 1, 0);
        let deltas = registry.pending_sync();
        assert_eq!(deltas[0].minute, BASE_MINUTE + 1);
        assert_eq!((deltas[0].rpm_prev, deltas[0].rpm_cur), (0, 0));
        assert_eq!((deltas[0].tpm_prev, deltas[0].tpm_cur), (5, 0));
    }

    #[test]
    fn pending_sync_skips_scopes_whose_limits_are_unknown() {
        let (registry, _now) = registry_at(BASE_MINUTE, 0);
        let state = registry.state(&"s".into());
        state.counters.lock().unwrap().rpm.cur = 1;
        assert!(registry.pending_sync().is_empty(), "nothing to enforce");
        drop(registry.admit(&caller(&[("s", limit(5, 0, 0))])).unwrap());
        assert_eq!(registry.pending_sync().len(), 1);
    }

    #[test]
    fn stale_view_falls_back_to_local_limits() {
        let (registry, now) = registry_at(BASE_MINUTE, 0);
        let key = caller(&[("s", limit(10, 0, 10))]);
        let state = registry.state(&"s".into());
        {
            let mut counters = state.counters.lock().unwrap();
            counters.rpm_limit = 10;
            counters.parallel_limit = 10;
        }
        // Another instance reports a full window and a full in-flight gauge.
        let mut others = view("s", BASE_MINUTE);
        others.rpm_cur = 10;
        others.rpm_instances = 2;
        others.in_flight = 10;
        others.in_flight_instances = 2;
        registry.apply_sync(&[others]);
        assert!(registry.admit(&key).is_err(), "the share is zero");
        // No own counts or deltas exist, but the finite share and the others'
        // half keep the scope in the sync, so a fresh view can still arrive.
        let deltas = registry.pending_sync();
        assert_eq!(deltas.len(), 1);
        assert_eq!(&*deltas[0].id, "s");
        assert_eq!((deltas[0].rpm_prev, deltas[0].rpm_cur), (0, 0));
        // One minute later the view is still valid, so the scope must keep
        // being reported even though its own count is now in the previous
        // window and its share is still zero.
        set(&now, BASE_MINUTE + 1, 0);
        assert_eq!(registry.pending_sync().len(), 1);
        // Two idle minutes make the view older than the previous minute: the
        // others' half and both shares are dropped and local limiting resumes.
        set(&now, BASE_MINUTE + 2, 0);
        drop(registry.admit(&key).unwrap());
    }

    #[test]
    fn finite_parallel_share_keeps_the_scope_in_the_sync() {
        let (registry, _now) = registry_at(BASE_MINUTE, 0);
        let state = registry.state(&"p".into());
        state.counters.lock().unwrap().parallel_limit = 8;
        // Two instances, nobody in flight: the headroom is divided (4 each)
        // and the share is the only thing keeping the scope active.
        let mut others = view("p", BASE_MINUTE);
        others.in_flight_instances = 2;
        registry.apply_sync(&[others]);
        let deltas = registry.pending_sync();
        assert_eq!(deltas.len(), 1);
        assert_eq!(deltas[0].in_flight, 0);
        assert_eq!(state.share_parallel.load(Ordering::Relaxed), 4);
    }

    #[test]
    fn stale_parallel_view_releases_its_slots() {
        let (registry, now) = registry_at(BASE_MINUTE, 0);
        let key = caller(&[("p", limit(0, 0, 4))]);
        let state = registry.state(&"p".into());
        state.counters.lock().unwrap().parallel_limit = 4;
        let mut others = view("p", BASE_MINUTE);
        others.in_flight = 4;
        others.in_flight_instances = 2;
        registry.apply_sync(&[others]);
        assert!(registry.admit(&key).is_err(), "others fill the gauge");
        assert_eq!(
            registry.pending_sync().len(),
            1,
            "the finite share and others keep it synced"
        );
        // Two idle minutes drop the stale in-flight report and its share.
        set(&now, BASE_MINUTE + 2, 0);
        let mut permits = Vec::new();
        for _ in 0..4 {
            permits.push(registry.admit(&key).unwrap());
        }
        assert!(registry.admit(&key).is_err());
        drop(permits);
    }

    #[test]
    fn fallback_drops_others_and_shares() {
        let (registry, _now) = registry_at(BASE_MINUTE, 0);
        let key = caller(&[("s", limit(10, 0, 0))]);
        drop(registry.admit(&key).unwrap());
        let mut others = view("s", BASE_MINUTE);
        others.rpm_cur = 10;
        others.rpm_instances = 2;
        registry.apply_sync(&[others]);
        assert!(registry.admit(&key).is_err(), "others used the limit");
        registry.enter_fallback();
        // Local limiting at the full limit: one request already counted,
        // nine more fit.
        for _ in 0..9 {
            drop(registry.admit(&key).unwrap());
        }
        assert!(registry.admit(&key).is_err());
    }

    #[test]
    fn fallback_releases_parallel_slots() {
        let (registry, _now) = registry_at(BASE_MINUTE, 0);
        let key = caller(&[("p", limit(0, 0, 2))]);
        let state = registry.state(&"p".into());
        state.counters.lock().unwrap().parallel_limit = 2;
        let mut others = view("p", BASE_MINUTE);
        others.in_flight = 2;
        others.in_flight_instances = 2;
        registry.apply_sync(&[others]);
        assert!(
            registry.admit(&key).is_err(),
            "a vanished instance fills it"
        );
        registry.enter_fallback();
        let a = registry.admit(&key).unwrap();
        let b = registry.admit(&key).unwrap();
        assert!(registry.admit(&key).is_err());
        drop((a, b));
    }

    #[test]
    fn restore_keeps_the_minute_of_each_delta() {
        let (registry, now) = registry_at(BASE_MINUTE, 0);
        let key = caller(&[("s", limit(10, 100, 0))]);
        drop(registry.admit(&key).unwrap());
        drop(registry.admit(&key).unwrap());
        registry.charge_tokens(&key, 30);
        let sent = registry.pending_sync();
        assert_eq!((sent[0].rpm_cur, sent[0].tpm_cur), (2, 30));
        // A failed sync hands the deltas back unchanged.
        registry.restore(&sent);
        let again = registry.pending_sync();
        assert_eq!((again[0].rpm_cur, again[0].tpm_cur), (2, 30));

        // A delta from the minute that is now the previous one lands in prev.
        registry.restore(&again);
        let sent = registry.pending_sync();
        set(&now, BASE_MINUTE + 1, 0);
        registry.restore(&sent);
        let restored = registry.pending_sync();
        assert_eq!(restored[0].minute, BASE_MINUTE + 1);
        assert_eq!((restored[0].rpm_prev, restored[0].rpm_cur), (2, 0));
        assert_eq!((restored[0].tpm_prev, restored[0].tpm_cur), (30, 0));

        // Anything older than the previous minute is dropped: it can no
        // longer change an estimate.
        let sent = registry.pending_sync();
        set(&now, BASE_MINUTE + 3, 0);
        registry.restore(&sent);
        assert!(registry.pending_sync().is_empty());
    }

    #[test]
    fn refund_lands_in_the_minute_the_count_was_taken() {
        let (registry, now) = registry_at(BASE_MINUTE, 0);
        let key = caller(&[("s", limit(5, 0, 0))]);
        drop(registry.admit(&key).unwrap());
        registry.pending_sync();
        set(&now, BASE_MINUTE + 1, 0);
        let state = registry.state(&"s".into());
        state.refund_rpm(BASE_MINUTE + 1, BASE_MINUTE, false);
        let deltas = registry.pending_sync();
        assert_eq!(deltas[0].minute, BASE_MINUTE + 1);
        assert_eq!((deltas[0].rpm_prev, deltas[0].rpm_cur), (-1, 0));
        assert_eq!(
            state.counters.lock().unwrap().rpm.prev,
            0,
            "the previous window's count is given back"
        );
    }

    #[test]
    fn pending_sync_keeps_concurrent_increments() {
        let (registry, _now) = registry_at(BASE_MINUTE, 0);
        let registry = Arc::new(registry);
        let key = caller(&[("s", limit(u32::MAX, 0, 0))]);
        let threads: u64 = 8;
        let per_thread: u64 = 5_000;
        let key_ref = &key;
        let mut synced = 0i64;
        std::thread::scope(|scope| {
            let handles: Vec<_> = (0..threads)
                .map(|_| {
                    let registry = &registry;
                    scope.spawn(move || {
                        for _ in 0..per_thread {
                            drop(registry.admit(key_ref).unwrap());
                        }
                    })
                })
                .collect();
            while !handles.iter().all(|handle| handle.is_finished()) {
                synced += registry
                    .pending_sync()
                    .iter()
                    .map(|d| d.rpm_prev.saturating_add(d.rpm_cur))
                    .sum::<i64>();
            }
            for handle in handles {
                handle.join().unwrap();
            }
        });
        synced += registry
            .pending_sync()
            .iter()
            .map(|d| d.rpm_prev.saturating_add(d.rpm_cur))
            .sum::<i64>();
        let expected = i64::try_from(threads * per_thread).unwrap();
        assert_eq!(synced, expected);
        let state = registry.state(&"s".into());
        assert_eq!(state.counters.lock().unwrap().rpm.cur, threads * per_thread);
    }

    #[test]
    fn apply_sync_adds_others_to_the_estimate() {
        let (registry, _now) = registry_at(BASE_MINUTE, 0);
        registry.state(&"s".into());
        let key = caller(&[("s", limit(10, 0, 0))]);
        let mut others = view("s", BASE_MINUTE);
        others.rpm_cur = 6;
        others.rpm_instances = 1;
        registry.apply_sync(&[others]);
        // estimate = 6, so only four more fit before the window is full.
        for _ in 0..4 {
            drop(registry.admit(&key).unwrap());
        }
        assert!(registry.admit(&key).is_err());
    }

    #[test]
    fn apply_sync_ignores_stale_minutes() {
        let (registry, _now) = registry_at(BASE_MINUTE, 0);
        registry.state(&"s".into());
        let key = caller(&[("s", limit(5, 0, 0))]);
        for minute in [BASE_MINUTE - 2, BASE_MINUTE + 1] {
            let mut stale = view("s", minute);
            stale.rpm_cur = 5;
            registry.apply_sync(&[stale]);
        }
        for _ in 0..5 {
            drop(registry.admit(&key).unwrap());
        }
        assert!(registry.admit(&key).is_err());
    }

    #[test]
    fn fair_share_caps_admits_between_syncs() {
        let (registry, _now) = registry_at(BASE_MINUTE, 0);
        let key = caller(&[("s", limit(10, 0, 0))]);
        // A first request stores the limit the sync divides.
        drop(registry.admit(&key).unwrap());
        let mut shared = view("s", BASE_MINUTE);
        shared.rpm_instances = 2;
        registry.apply_sync(&[shared.clone()]);
        // estimate 1, headroom 9 over two instances: five more before the
        // next sync.
        for _ in 0..5 {
            drop(registry.admit(&key).unwrap());
        }
        assert_eq!(registry.admit(&key).err().unwrap().retry_after_secs, 1);
        // The next sync sees the six local counts and redivides what is left:
        // headroom 4 over two instances is two.
        registry.apply_sync(&[shared]);
        for _ in 0..2 {
            drop(registry.admit(&key).unwrap());
        }
        assert!(registry.admit(&key).is_err());

        // One active instance is exactly local behaviour.
        let (solo, _now) = registry_at(BASE_MINUTE, 0);
        drop(solo.admit(&key).unwrap());
        let mut alone = view("s", BASE_MINUTE);
        alone.rpm_instances = 1;
        solo.apply_sync(&[alone]);
        for _ in 0..9 {
            drop(solo.admit(&key).unwrap());
        }
        assert!(solo.admit(&key).is_err());
    }

    #[test]
    fn parallel_fair_share_is_released_on_drop() {
        let (registry, _now) = registry_at(BASE_MINUTE, 0);
        registry.state(&"p".into());
        let key = caller(&[("p", limit(0, 0, 10))]);
        // Establish the limit the way a request would before the sync runs.
        let first = registry.admit(&key).unwrap();
        let mut shared = view("p", BASE_MINUTE);
        shared.in_flight_instances = 2;
        registry.apply_sync(&[shared]);
        // Headroom 9 over two instances: five more before the next sync.
        let mut permits = vec![first];
        for _ in 0..5 {
            permits.push(registry.admit(&key).unwrap());
        }
        assert_eq!(registry.admit(&key).err().unwrap().retry_after_secs, 1);
        // Giving a slot back returns its share too.
        drop(permits.pop());
        permits.push(registry.admit(&key).unwrap());
        assert!(registry.admit(&key).is_err());
        drop(permits);
    }

    #[test]
    fn concurrent_parallel_limit_is_exact() {
        let registry = Arc::new(Registry::new());
        let key = caller(&[("p", limit(0, 0, 8))]);
        let admitted = Arc::new(AtomicUsize::new(0));
        let permits: Vec<_> = std::thread::scope(|scope| {
            let handles: Vec<_> = (0..8)
                .map(|_| {
                    let registry = &registry;
                    let key = &key;
                    let admitted = &admitted;
                    scope.spawn(move || {
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
                .flat_map(|handle| handle.join().unwrap())
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
            router::RoutingTable,
            runtime::Runtime,
            snapshot::tests::{base, key},
        };
        use lumewarp_config::AuthMode;
        use sha2::{Digest, Sha256};
        let scope = |rpm| lumewarp_proto::config::v1::RateLimitScope {
            id: "team:t1".into(),
            limit: Some(limit(rpm, 0, 0)),
        };
        let mut snapshot = base();
        snapshot.api_keys[0]
            .policy
            .as_mut()
            .unwrap()
            .rate_limit_scopes = vec![scope(2)];
        let runtime = Runtime::new();
        let hash: [u8; 32] = Sha256::digest(b"s1").into();
        let old = RoutingTable::compile(&snapshot, AuthMode::RequireKey, &runtime).unwrap();
        let caller = Caller::Key(old.keys[&hash].clone());
        drop(runtime.limiters.admit(&caller).unwrap());
        drop(runtime.limiters.admit(&caller).unwrap());
        // A key-only delta adds another key of the same team scope.
        let mut next = snapshot.clone();
        next.version = 2;
        let mut k3 = key("k3", "s3", "t1", &["m1"]);
        k3.policy.as_mut().unwrap().rate_limit_scopes = vec![scope(2)];
        next.api_keys.push(k3);
        let new = RoutingTable::compile(&next, AuthMode::RequireKey, &runtime).unwrap();
        drop(old);
        let hash3: [u8; 32] = Sha256::digest(b"s3").into();
        for secret in [hash, hash3] {
            let caller = Caller::Key(new.keys[&secret].clone());
            assert!(runtime.limiters.admit(&caller).is_err());
        }
    }

    /// In-test store: each instance's deltas accumulate per minute, and a
    /// view returns the other instances' totals and instance counts. Every
    /// instance writes its in-flight field on each sync, even when zero, so
    /// the parallel `n` covers the live instances (the Redis hash keeps a
    /// field fresh for 3 s, which is 12 ticks here).
    #[derive(Default)]
    struct FakeStore {
        rpm: HashMap<(String, u64), Vec<i64>>,
        tpm: HashMap<(String, u64), Vec<i64>>,
        in_flight: HashMap<(String, usize), (u32, u64)>,
    }

    impl FakeStore {
        fn push(&mut self, tick: u64, instance: usize, delta: &ScopeDelta, instances: usize) {
            let id = delta.id.to_string();
            let mut minutes = vec![(delta.minute, delta.rpm_cur, delta.tpm_cur)];
            if delta.minute > 0 {
                minutes.push((delta.minute - 1, delta.rpm_prev, delta.tpm_prev));
            }
            for (minute, rpm, tpm) in minutes {
                for (counts, amount) in [(&mut self.rpm, rpm), (&mut self.tpm, tpm)] {
                    let entry = counts
                        .entry((id.clone(), minute))
                        .or_insert_with(|| vec![0; instances]);
                    entry.resize(instances, 0);
                    entry[instance] += amount;
                }
            }
            self.in_flight
                .insert((id, instance), (delta.in_flight, tick));
        }

        fn counts(&self, id: &str, minute: u64, instances: usize, own: usize) -> (u64, u64, u32) {
            let per = |counts: &HashMap<(String, u64), Vec<i64>>, minute: u64| {
                counts
                    .get(&(id.to_string(), minute))
                    .cloned()
                    .unwrap_or_else(|| vec![0; instances])
            };
            let cur = per(&self.rpm, minute);
            let prev = per(&self.rpm, minute.saturating_sub(1));
            let mut other_cur = 0u64;
            let mut other_prev = 0u64;
            let mut active = 0u32;
            for i in 0..instances {
                let cur = u64::try_from(cur.get(i).copied().unwrap_or(0).max(0)).unwrap_or(0);
                let prev = u64::try_from(prev.get(i).copied().unwrap_or(0).max(0)).unwrap_or(0);
                if i != own {
                    other_cur += cur;
                    other_prev += prev;
                }
                if cur > 0 || prev > 0 {
                    active += 1;
                }
            }
            (other_prev, other_cur, active)
        }

        fn view(
            &self,
            id: &str,
            instance: usize,
            instances: usize,
            minute: u64,
            tick: u64,
        ) -> ScopeView {
            let (rpm_prev, rpm_cur, rpm_instances) = self.counts(id, minute, instances, instance);
            let mut view = view(id, minute);
            view.rpm_prev = rpm_prev;
            view.rpm_cur = rpm_cur;
            view.rpm_instances = rpm_instances;
            view.tpm_instances = rpm_instances;
            for i in 0..instances {
                let Some(&(in_flight, stamped)) = self.in_flight.get(&(id.to_string(), i)) else {
                    continue;
                };
                if tick.saturating_sub(stamped) > 12 {
                    continue;
                }
                if i != instance {
                    view.in_flight += in_flight;
                }
                view.in_flight_instances += 1;
            }
            view
        }
    }

    fn run_sync(
        registries: &[Registry],
        store: &mut FakeStore,
        instances: usize,
        id: &str,
        tick: u64,
    ) -> u64 {
        let minute = registries[0].minute_now();
        // One batch round-trip: every instance pushes its deltas before any
        // instance reads a view, like one pipelined store call.
        let deltas: Vec<_> = registries
            .iter()
            .map(|registry| registry.pending_sync())
            .collect();
        for (i, deltas) in deltas.iter().enumerate() {
            for delta in deltas {
                store.push(tick, i, delta, instances);
            }
        }
        for (i, registry) in registries.iter().enumerate() {
            let view = store.view(id, i, instances, minute, tick);
            registry.apply_sync(&[view]);
        }
        minute
    }

    #[test]
    fn simulation_rpm_holds_across_instances() {
        for instances in 2..=8usize {
            let rpm = 30u32;
            let now = at(BASE_MINUTE, 30_000);
            let registries: Vec<_> = (0..instances)
                .map(|_| Registry::with_clock(now.clone()))
                .collect();
            let key = caller(&[("shared", limit(rpm, 0, 0))]);
            let mut store = FakeStore::default();
            let mut admitted: HashMap<u64, u32> = HashMap::new();
            for tick in 0..480u64 {
                let minute = run_sync(&registries, &mut store, instances, "shared", tick);
                for (i, registry) in registries.iter().enumerate() {
                    for _ in 0..(1 + i % 3) {
                        if registry.admit(&key).is_ok() {
                            *admitted.entry(minute).or_insert(0) += 1;
                        }
                    }
                }
                now.fetch_add(250, Ordering::Relaxed);
            }
            let bound = rpm + u32::try_from(instances).unwrap();
            for (minute, count) in admitted {
                assert!(
                    count <= bound,
                    "{instances} instances, minute {minute}: {count} > {bound}"
                );
            }
        }
    }

    #[test]
    fn simulation_parallel_holds_across_instances() {
        for instances in 2..=8usize {
            let max = 16u32;
            let now = at(BASE_MINUTE, 30_000);
            let registries: Vec<_> = (0..instances)
                .map(|_| Registry::with_clock(now.clone()))
                .collect();
            let key = caller(&[("shared", limit(0, 0, max))]);
            let mut store = FakeStore::default();
            let mut held: Vec<Vec<Permit>> = (0..instances).map(|_| Vec::new()).collect();
            let mut peak = 0usize;
            for tick in 0..480u64 {
                run_sync(&registries, &mut store, instances, "shared", tick);
                for (i, registry) in registries.iter().enumerate() {
                    // Keep one slot in use so the in-flight fields stay fresh,
                    // as continuous traffic would.
                    if held[i].len() > 1 && (tick + u64::try_from(i).unwrap()) % 2 == 0 {
                        held[i].pop();
                    }
                    for _ in 0..(1 + i % 3) {
                        if let Ok(permit) = registry.admit(&key) {
                            held[i].push(permit);
                        }
                    }
                }
                peak = peak.max(held.iter().map(Vec::len).sum());
                now.fetch_add(250, Ordering::Relaxed);
            }
            let bound = usize::try_from(max).unwrap() + instances;
            assert!(
                peak <= bound,
                "{instances} instances: peak {peak} > {bound}"
            );
        }
    }

    /// Micro-measure only, not the HTTP benchmark suite:
    /// `cargo test --release --bin lumewarp bench_admit_cost -- --ignored --nocapture`
    #[test]
    #[ignore = "timing micro-measure; run explicitly in release mode"]
    fn bench_admit_cost() {
        use std::{hint::black_box, time::Instant};

        const N: u32 = 2_000_000;
        let (registry, _now) = registry_at(BASE_MINUTE, 0);
        let key = caller(&[("s", limit(u32::MAX, 0, 0))]);
        for _ in 0..10_000 {
            drop(registry.admit(&key).unwrap());
        }
        let start = Instant::now();
        for _ in 0..N {
            drop(registry.admit(black_box(&key)).unwrap());
        }
        println!(
            "admit (one rpm scope): {:.1} ns/op",
            start.elapsed().as_nanos() as f64 / f64::from(N)
        );
        let start = Instant::now();
        for i in 0..N {
            black_box(estimate(black_box(u64::from(i)), black_box(7), 30_000));
        }
        println!(
            "estimate: {:.1} ns/op",
            start.elapsed().as_nanos() as f64 / f64::from(N)
        );
    }
}
