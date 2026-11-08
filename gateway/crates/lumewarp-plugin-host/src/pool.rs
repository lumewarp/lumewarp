//! Pools of configured plugin instances.
//!
//! A pool is created when a configuration is applied and reused across
//! configurations whose router settings did not change, so warm instances (and
//! whatever a guest kept between calls) survive a snapshot swap. Instances are
//! never created while a request waits for a slot on the *event* thread beyond
//! the async instantiation itself: a checkout that finds the pool empty awaits a
//! new instance or a returned one, bounded by the call's own timeout.
//!
//! Each call takes one permit from the process memory gate
//! ([`crate::limits::Permits`]): a burst larger than the budget waits for a
//! permit instead of failing. A pool never owns more than its share of them, so
//! a stuck plugin delays only the requests that call it. A call that ends by
//! timeout, trap or error drops its Store, which gives the reserved memory back.

use std::{
    collections::HashMap,
    sync::{
        atomic::{AtomicUsize, Ordering},
        Arc, Mutex, OnceLock, Weak,
    },
    time::{Duration, Instant},
};

use lumewarp_router_api::{CandidateSource, Candidates, RequestInfo};
use sha2::{Digest, Sha256};
use tokio::sync::{Notify, Semaphore};
use wasmtime::{component::ResourceAny, Store};

use crate::{
    limits::{self, MemoryReservation},
    router, types, Plugin, Prepared, State,
};

/// How one pool is sized and how its calls are admitted.
#[derive(Clone, Debug)]
pub struct PoolConfig {
    /// Instances the pool may own (idle or in use), and its pre-warm target.
    /// Capped at the pool's share of the call permits (M2).
    pub target: usize,
    /// Instances created at configure time. Defaults to `target`.
    pub prewarm: usize,
    /// Call-permit gate; `None` uses the process gate.
    pub permits: Option<Arc<limits::Permits>>,
    /// Wasm memory budget the pool's Stores count against; `None` uses the
    /// process budget (`LUMEWARP_PLUGIN_MEMORY_LIMIT_BYTES`).
    pub budget: Option<Arc<limits::Budget>>,
    /// How long an apply-time `configure`/pre-warm may take. Defaults to
    /// [`crate::CONFIGURE_TIMEOUT`]; expiry refuses the version.
    pub configure_timeout: Duration,
}

impl Default for PoolConfig {
    fn default() -> Self {
        let target = std::thread::available_parallelism().map_or(1, usize::from);
        Self {
            target,
            // One warm instance per possible concurrent pick: a burst right
            // after an apply then never pays for instantiation. The memory
            // budget stops the pre-warm when a deployment cannot afford it.
            prewarm: target,
            permits: None,
            budget: None,
            configure_timeout: crate::CONFIGURE_TIMEOUT,
        }
    }
}

impl PoolConfig {
    fn permits(&self) -> Arc<limits::Permits> {
        self.permits
            .clone()
            .unwrap_or_else(|| Arc::clone(limits::memory_permits_gate()))
    }
}

/// One live instance's claim on the pool's target and on the process-wide
/// instance budget; released when the instance dies.
struct LiveGuard(Arc<PoolLive>);

struct PoolLive {
    live: AtomicUsize,
    notify: Notify,
}

impl Drop for LiveGuard {
    fn drop(&mut self) {
        self.0.live.fetch_sub(1, Ordering::AcqRel);
        self.0.notify.notify_one();
    }
}

/// The two reservations an instance needs before it may be created.
pub(crate) struct Slot {
    _live: LiveGuard,
    _idle: limits::IdleSlot,
}

/// What a failed [`InstancePool::reserve`] is waiting for.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Blocked {
    /// Every instance this pool may own is in use: wait for one to come back.
    Pool,
    /// The process-wide instance cap is full: wait for any pool to free one.
    Budget,
}

/// The parts of a pool every adapter shares: idle instances, the live cap and a
/// wake-up for callers waiting for either.
pub(crate) struct InstancePool<I> {
    idle: Mutex<Vec<I>>,
    live: Arc<PoolLive>,
    budget: Arc<limits::IdleBudget>,
    target: usize,
}

impl<I> InstancePool<I> {
    pub(crate) fn new(target: usize) -> Self {
        Self::with_budget(target, Arc::clone(limits::idle_budget()))
    }

    /// A pool drawing from `budget` instead of the process-wide instance cap.
    pub(crate) fn with_budget(target: usize, budget: Arc<limits::IdleBudget>) -> Self {
        Self {
            idle: Mutex::new(Vec::new()),
            live: Arc::new(PoolLive {
                live: AtomicUsize::new(0),
                notify: Notify::new(),
            }),
            budget,
            target,
        }
    }

    pub(crate) fn pop(&self) -> Option<I> {
        self.idle
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .pop()
    }

    /// Returns an instance to the pool, or drops it when the pool is full.
    pub(crate) fn push(&self, instance: I) {
        let mut idle = self.idle.lock().unwrap_or_else(|error| error.into_inner());
        if idle.len() < self.target {
            idle.push(instance);
            drop(idle);
            self.live.notify.notify_one();
        }
        // Else: dropped here, outside the lock, which frees its Store.
    }

    /// Claims one live slot and one process-wide instance slot, or says what the
    /// pool is waiting for when either is exhausted.
    ///
    /// The target check comes **first**: a cap slot this pool cannot use is
    /// never taken, so another pool's `reserve` cannot fail against a
    /// reservation that is about to be dropped (m2). Returning here never
    /// notifies, so a waiter is never handed a slot it cannot have.
    pub(crate) fn reserve(&self) -> Result<Slot, Blocked> {
        if self.live.live.load(Ordering::Acquire) >= self.target {
            return Err(Blocked::Pool);
        }
        let Some(idle) = self.budget.acquire() else {
            return Err(Blocked::Budget);
        };
        self.claim(idle).ok_or(Blocked::Pool)
    }

    /// Claims the live slot with `idle` (a process-wide instance slot) in hand.
    /// `None` when the target filled up meanwhile; `idle` is then dropped, which
    /// hands the cap slot to the next waiting pool.
    fn claim(&self, idle: limits::IdleSlot) -> Option<Slot> {
        let mut live = self.live.live.load(Ordering::Acquire);
        loop {
            if live >= self.target {
                return None;
            }
            match self.live.live.compare_exchange_weak(
                live,
                live + 1,
                Ordering::AcqRel,
                Ordering::Acquire,
            ) {
                Ok(_) => {
                    return Some(Slot {
                        _live: LiveGuard(Arc::clone(&self.live)),
                        _idle: idle,
                    })
                }
                Err(current) => live = current,
            }
        }
    }

    /// Waits for whatever [`Self::reserve`] said is missing. A slot freed in the
    /// process-wide cap is **claimed here**, so the pool that woke for it is the
    /// one that gets it, and one that cannot use it drops it for the next waiter
    /// (m2). `Ok(None)` means an instance came back: try again.
    pub(crate) async fn wait(&self, blocked: Blocked) -> Result<Option<Slot>, Error> {
        match blocked {
            Blocked::Pool => {
                self.live.notify.notified().await;
                Ok(None)
            }
            Blocked::Budget => {
                let idle = self.budget.wait().await.ok_or_else(|| {
                    Error::Instantiate("the instance budget was closed".to_owned())
                })?;
                Ok(self.claim(idle))
            }
        }
    }

    pub(crate) fn live(&self) -> usize {
        self.live.live.load(Ordering::Acquire)
    }

    pub(crate) fn idle_len(&self) -> usize {
        self.idle
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .len()
    }

    /// Drops idle instances beyond `keep` (memory pressure, and tests).
    pub(crate) fn shrink(&self, keep: usize) {
        let mut dropped = Vec::new();
        {
            let mut idle = self.idle.lock().unwrap_or_else(|error| error.into_inner());
            while idle.len() > keep {
                if let Some(instance) = idle.pop() {
                    dropped.push(instance);
                }
            }
        }
        drop(dropped);
        self.live.notify.notify_one();
    }
}

struct PooledInstance {
    store: Store<State>,
    binding: router::Router,
    resource: ResourceAny,
    _slot: Slot,
}

/// Why a pool could not produce an instance.
#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error("instantiating the component failed: {0}")]
    Instantiate(String),
    #[error("configuring the strategy failed: {0}")]
    Configure(String),
    /// Apply-time `configure` did not finish inside
    /// [`crate::CONFIGURE_TIMEOUT`]; the version is refused.
    #[error("configuring the strategy timed out")]
    ConfigureTimeout,
}

/// One router's configured strategy instances.
pub struct RouterPool {
    plugin: Arc<Plugin>,
    info: types::RouterInfo,
    pool: InstancePool<PooledInstance>,
    memory: Arc<Semaphore>,
    budget: Arc<limits::Budget>,
    prewarm: usize,
    configure_timeout: Duration,
}

impl RouterPool {
    fn new(plugin: Arc<Plugin>, info: types::RouterInfo, config: PoolConfig) -> Self {
        let permits = config.permits();
        // A quarter of the call permits at most, so a stuck strategy holds only
        // its own requests' permits and every other plugin still runs (M2).
        let target = config.target.min(permits.pool_share()).max(1);
        Self {
            plugin,
            info,
            pool: InstancePool::new(target),
            memory: permits.semaphore(),
            budget: config
                .budget
                .clone()
                .unwrap_or_else(|| Arc::clone(limits::memory_budget())),
            prewarm: config.prewarm.min(target),
            configure_timeout: config.configure_timeout,
        }
    }

    /// The plugin this pool runs.
    pub fn plugin(&self) -> &Arc<Plugin> {
        &self.plugin
    }

    /// Idle instances right now (diagnostics and tests).
    pub fn idle(&self) -> usize {
        self.pool.idle_len()
    }

    /// Live instances (idle and in use).
    pub fn live(&self) -> usize {
        self.pool.live()
    }

    /// Drops idle instances beyond `keep`.
    pub fn shrink(&self, keep: usize) {
        self.pool.shrink(keep);
    }

    /// Creates one configured instance while holding a memory permit. Async:
    /// instantiation and `configure` run on the caller's executor, never
    /// blocking a thread.
    async fn create(
        &self,
        slot: Slot,
        _permit: MemoryReservation,
        budget: Option<Duration>,
    ) -> Result<PooledInstance, Error> {
        let mut store = crate::store_with_budget(self.plugin.engine(), Arc::clone(&self.budget));
        // Apply-time `configure` gets a hard epoch budget; a request-path
        // instance yields for fairness only (M4).
        if let Some(budget) = budget {
            crate::set_apply_deadline(&mut store, budget);
        }
        let Prepared::Router(pre) = self.plugin.prepared() else {
            return Err(Error::Instantiate(
                "the component does not implement the router world".to_owned(),
            ));
        };
        let binding = pre
            .instantiate_async(&mut store)
            .await
            .map_err(|error| Error::Instantiate(format!("{error:#}")))?;
        let resource = binding
            .lumewarp_plugin_router_strategy()
            .strategy()
            .call_configure(&mut store, &self.info)
            .await
            .map_err(|error| {
                if budget.is_some() && crate::is_epoch_trap(&error) {
                    Error::ConfigureTimeout
                } else {
                    Error::Instantiate(format!("{error:#}"))
                }
            })?
            .map_err(Error::Configure)?;
        if budget.is_some() {
            // Back to yielding: a request-path call is cancelled by its own
            // timeout, never trapped by the configure budget.
            crate::set_fair_epoch(&mut store);
        }
        Ok(PooledInstance {
            store,
            binding,
            resource,
            _slot: slot,
        })
    }

    /// Creates an instance, waiting for a memory permit (the caller's timeout
    /// bounds the wait).
    async fn create_waiting(
        &self,
        slot: Slot,
        budget: Option<Duration>,
    ) -> Result<PooledInstance, Error> {
        let permit = MemoryReservation::acquire(Arc::clone(&self.memory))
            .await
            .ok_or_else(|| Error::Instantiate("the memory budget was closed".to_owned()))?;
        self.create(slot, permit, budget).await
    }

    /// Creates an instance only when a memory permit is free right now.
    async fn create_now(
        &self,
        slot: Slot,
        budget: Option<Duration>,
    ) -> Result<Option<PooledInstance>, Error> {
        let Some(permit) = MemoryReservation::try_acquire(Arc::clone(&self.memory)) else {
            return Ok(None);
        };
        self.create(slot, permit, budget).await.map(Some)
    }

    /// An instance from the pool, a new one, or one that becomes free — waiting
    /// counts towards the caller's timeout.
    async fn checkout(&self) -> Result<PooledInstance, Error> {
        let mut retried = false;
        loop {
            if let Some(instance) = self.pool.pop() {
                return Ok(instance);
            }
            let slot = match self.pool.reserve() {
                Ok(slot) => slot,
                Err(blocked) => match self.pool.wait(blocked).await? {
                    Some(slot) => slot,
                    // An instance came back; take it if it is still there.
                    None => continue,
                },
            };
            // The memory permit covers instantiation and the call only; a
            // request queueing for this pool never holds one, so a stuck
            // plugin cannot starve the others (M2).
            match self.create_waiting(slot, None).await {
                Ok(instance) => return Ok(instance),
                // Out of budget: give this pool's own idle instances back
                // and try once more before failing the call.
                Err(_) if !retried && self.pool.idle_len() > 0 => {
                    retried = true;
                    self.pool.shrink(0);
                    continue;
                }
                Err(error) => return Err(error),
            }
        }
    }

    /// Creates the configured instances. The first one is mandatory: its
    /// `configure` error — or a `configure` that outlives
    /// [`crate::CONFIGURE_TIMEOUT`] — refuses the version. The rest are best
    /// effort, so a tight memory budget leaves a smaller warm pool instead of
    /// failing.
    fn prewarm(&self) -> Result<(), Error> {
        if self.prewarm == 0 {
            return Ok(());
        }
        let Some(slot) = self.pool.reserve().ok() else {
            return Ok(());
        };
        let first = crate::block_on_timeout(
            self.create_waiting(slot, Some(self.configure_timeout)),
            self.configure_timeout,
        )
        .ok_or(Error::ConfigureTimeout)??;
        self.pool.push(first);
        for _ in 1..self.prewarm {
            let Ok(slot) = self.pool.reserve() else {
                break;
            };
            match crate::block_on_timeout(
                self.create_now(slot, Some(self.configure_timeout)),
                self.configure_timeout,
            ) {
                Some(Ok(Some(instance))) => self.pool.push(instance),
                Some(Ok(None)) => break,
                None => {
                    // An extra instance is not worth wedging the apply: the
                    // pool keeps what it already has.
                    tracing::warn!(plugin = %self.plugin.descriptor.name, "pre-warming a router strategy instance timed out");
                    break;
                }
                Some(Err(error)) => {
                    // m1: a configuration that fails while warming up is a
                    // counted failure, not a silently smaller pool.
                    tracing::warn!(%error, plugin = %self.plugin.descriptor.name, "pre-warming a router strategy instance failed");
                    self.plugin.traps.fetch_add(1, Ordering::Relaxed);
                    self.plugin.record_failure();
                    crate::record(
                        "router-strategy",
                        &self.plugin.descriptor.name,
                        "trap",
                        Duration::ZERO,
                    );
                    break;
                }
            }
        }
        Ok(())
    }

    /// One `pick`, cut short by `timeout`. A timeout drops the call's future and
    /// with it the Store; the caller falls through as after a trap.
    pub async fn pick(
        self: &Arc<Self>,
        request: types::RequestInfo,
        candidates: Vec<types::Candidate>,
        seed: u64,
        timeout: Duration,
    ) -> Option<u32> {
        let name = self.plugin.descriptor.name.clone();
        if self.plugin.paused() {
            crate::record("router-strategy", &name, "error", Duration::ZERO);
            return None;
        }
        let span = tracing::info_span!("plugin", kind = "router-strategy", plugin = %name);
        match tokio::time::timeout(timeout, self.answer(request, candidates, seed)).await {
            Ok(answer) => answer,
            Err(_) => {
                let _entered = span.enter();
                self.plugin.timeouts.fetch_add(1, Ordering::Relaxed);
                self.plugin.record_failure();
                crate::record("router-strategy", &name, "timeout", timeout);
                tracing::warn!(plugin = %name, "router strategy pick timed out");
                None
            }
        }
    }

    async fn answer(
        &self,
        request: types::RequestInfo,
        candidates: Vec<types::Candidate>,
        seed: u64,
    ) -> Option<u32> {
        let name = self.plugin.descriptor.name.as_str();
        let started = Instant::now();
        // The instance comes first: queueing for a pool never holds a
        // process-wide memory permit (M2).
        let mut instance = match self.checkout().await {
            Ok(instance) => instance,
            Err(error) => {
                tracing::warn!(%error, plugin = %name, "router strategy instance failed");
                self.plugin.traps.fetch_add(1, Ordering::Relaxed);
                self.plugin.record_failure();
                crate::record("router-strategy", name, "trap", started.elapsed());
                return None;
            }
        };
        // The permit is held for the call only; the instance is already ours.
        let Some(_permit) = MemoryReservation::acquire(Arc::clone(&self.memory)).await else {
            self.pool.push(instance);
            return None;
        };
        let result = instance
            .binding
            .lumewarp_plugin_router_strategy()
            .strategy()
            .call_pick(
                &mut instance.store,
                instance.resource,
                &request,
                &candidates,
                seed,
            )
            .await;
        let elapsed = started.elapsed();
        match result {
            // A guest answer must name one of the eligible candidates it was
            // given; anything else is a misbehaving plugin, counted as a trap,
            // and the caller falls through to the next pool (M1).
            Ok(Some(index)) if !candidates.iter().any(|candidate| candidate.index == index) => {
                tracing::warn!(plugin = %name, index, "router strategy picked an ineligible candidate");
                self.plugin.traps.fetch_add(1, Ordering::Relaxed);
                self.plugin.record_failure();
                crate::record("router-strategy", name, "trap", elapsed);
                self.pool.push(instance);
                None
            }
            Ok(answer) => {
                self.plugin.record_success();
                crate::record("router-strategy", name, "ok", elapsed);
                self.pool.push(instance);
                answer
            }
            Err(error) => {
                // A trapped Store is dropped, never pooled.
                tracing::warn!(plugin = %name, error = %error, "router strategy trapped");
                self.plugin.traps.fetch_add(1, Ordering::Relaxed);
                self.plugin.record_failure();
                crate::record("router-strategy", name, "trap", elapsed);
                None
            }
        }
    }
}

/// The strategy of one router, as the gateway holds it.
pub struct RouterInstance {
    pool: Arc<RouterPool>,
}

impl RouterInstance {
    /// Configures a router with the default pool sizing.
    pub fn configure(plugin: Arc<Plugin>, info: types::RouterInfo) -> Result<Self, Error> {
        Self::configure_with(plugin, info, PoolConfig::default())
    }

    /// Configures a router, reusing the pool of an unchanged configuration (same
    /// plugin binary, same router name, settings and members).
    pub fn configure_with(
        plugin: Arc<Plugin>,
        info: types::RouterInfo,
        config: PoolConfig,
    ) -> Result<Self, Error> {
        let key = (
            plugin.descriptor.name.clone(),
            plugin.descriptor.wasm_sha256.clone(),
            router_info_digest(&info),
        );
        {
            let mut pools = shared_pools()
                .lock()
                .unwrap_or_else(|error| error.into_inner());
            pools.retain(|_, weak| weak.strong_count() > 0);
            if let Some(pool) = pools.get(&key).and_then(Weak::upgrade) {
                return Ok(Self { pool });
            }
        }
        // Pre-warm **outside** the lock: a `configure` that hangs for its 60 s
        // budget must not block another apply or a lookup (M4).
        let pool = Arc::new(RouterPool::new(plugin, info, config));
        pool.prewarm()?;
        let mut pools = shared_pools()
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        // Another apply may have published the same pool meanwhile.
        if let Some(existing) = pools.get(&key).and_then(Weak::upgrade) {
            return Ok(Self { pool: existing });
        }
        pools.insert(key, Arc::downgrade(&pool));
        Ok(Self { pool })
    }

    /// Runs one `pick` with wire-level inputs.
    pub async fn pick(
        &self,
        request: &types::RequestInfo,
        candidates: &[types::Candidate],
        seed: u64,
        timeout: Duration,
    ) -> Option<u32> {
        self.pool
            .pick(request.clone(), candidates.to_vec(), seed, timeout)
            .await
    }

    /// The shared pool, for diagnostics and tests.
    pub fn pool(&self) -> &Arc<RouterPool> {
        &self.pool
    }

    /// Runs one `pick` as an owned, `Send` future so the gateway can await it
    /// without holding a non-`Send` borrow of the request across the await.
    pub fn pick_task<S, E>(
        &self,
        req: &RequestInfo<'_>,
        candidates: &Candidates<'_, S, E>,
        seed: u64,
        timeout: Duration,
    ) -> limits::BoxFuture<'static, Option<usize>>
    where
        S: CandidateSource + ?Sized,
        E: Fn(usize) -> bool + ?Sized,
    {
        let signals = &self
            .pool
            .plugin
            .descriptor
            .router
            .as_ref()
            .expect("a loaded router plugin always has router metadata; checked when it is loaded")
            .needs;
        let latency = signals.iter().any(|need| need == "latency");
        let load = signals.iter().any(|need| need == "vllm_load");
        let request = types::RequestInfo {
            requested: req.requested.into(),
            team_id: req.team_id.into(),
            key_id: req.key_id.into(),
            estimated_input_tokens: req.estimated_input_tokens,
            headers: req
                .headers()
                .map(|(name, value)| (name.into(), value.to_vec()))
                .collect(),
        };
        let candidates: Vec<_> = candidates
            .iter()
            .map(|candidate| types::Candidate {
                index: candidate.index as u32,
                member_index: candidate.member_index() as u32,
                weight: candidate.weight(),
                in_flight: candidate.in_flight().min(u32::MAX as usize) as u32,
                latency_ewma_us: if latency {
                    candidate.latency_ewma_us()
                } else {
                    None
                },
                load: load.then(|| {
                    let load = candidate.load();
                    types::Load {
                        waiting: load.waiting,
                        kv_usage_bp: load.kv_usage_bp,
                        fresh: load.fresh,
                    }
                }),
            })
            .collect();
        let pool = Arc::clone(&self.pool);
        Box::pin(async move {
            pool.pick(request, candidates, seed, timeout)
                .await
                .map(|index| index as usize)
        })
    }
}

/// Pools by (plugin name, plugin binary digest, router settings digest).
type PoolKey = (String, String, [u8; 32]);

struct SharedPools {
    pools: Mutex<HashMap<PoolKey, Weak<RouterPool>>>,
}

fn shared_pools() -> &'static Mutex<HashMap<PoolKey, Weak<RouterPool>>> {
    static POOLS: OnceLock<SharedPools> = OnceLock::new();
    &POOLS
        .get_or_init(|| SharedPools {
            pools: Mutex::new(HashMap::new()),
        })
        .pools
}

/// The identity of a router's settings: everything `configure` was given.
fn router_info_digest(info: &types::RouterInfo) -> [u8; 32] {
    fn field(hasher: &mut Sha256, bytes: &[u8]) {
        hasher.update((bytes.len() as u64).to_le_bytes());
        hasher.update(bytes);
    }
    let mut hasher = Sha256::new();
    field(&mut hasher, info.name.as_bytes());
    field(&mut hasher, info.params_json.as_bytes());
    for member in &info.members {
        field(&mut hasher, member.model.as_bytes());
        hasher.update(member.weight.to_le_bytes());
        match member.split.as_deref() {
            Some(split) => {
                hasher.update([1]);
                field(&mut hasher, split.as_bytes());
            }
            None => hasher.update([0]),
        }
        field(&mut hasher, member.params_json.as_bytes());
    }
    hasher.finalize().into()
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A reservation that fails on the process-wide instance cap must not
    /// notify the waiters: with the old order it took a live slot and dropped
    /// it, so `wait()` returned at once and the worker spun (m2).
    #[tokio::test]
    async fn a_full_instance_budget_waits_instead_of_spinning() {
        let pool: InstancePool<()> = InstancePool::with_budget(1, limits::IdleBudget::new(0));
        assert_eq!(
            pool.reserve().err(),
            Some(Blocked::Budget),
            "the process-wide cap is full"
        );
        assert!(
            tokio::time::timeout(Duration::from_millis(50), pool.wait(Blocked::Budget))
                .await
                .is_err(),
            "wait() returned although nothing was returned or freed"
        );
    }

    /// A slot freed in the process-wide cap wakes a pool waiting on it, and the
    /// waking pool claims the slot instead of racing for it (m2).
    #[tokio::test]
    async fn a_freed_process_slot_wakes_a_waiting_pool() {
        let budget = limits::IdleBudget::new(1);
        let holder: InstancePool<()> = InstancePool::with_budget(1, Arc::clone(&budget));
        let waiting: InstancePool<()> = InstancePool::with_budget(1, Arc::clone(&budget));
        let held = holder.reserve().expect("the holder takes the only slot");
        assert_eq!(waiting.reserve().err(), Some(Blocked::Budget));
        let mut wait = Box::pin(waiting.wait(Blocked::Budget));
        assert!(
            tokio::time::timeout(Duration::from_millis(50), &mut wait)
                .await
                .is_err(),
            "nothing was freed yet"
        );
        drop(held);
        let slot = tokio::time::timeout(Duration::from_millis(500), wait)
            .await
            .expect("a slot freed in the process-wide cap wakes the waiter")
            .expect("the budget is never closed");
        assert!(slot.is_some(), "the waking pool claims the freed slot");
    }

    /// A pool whose own target is full reports that and touches nothing: the
    /// process-wide cap slot it would have taken is never held, so another
    /// pool's reservation cannot fail against it (m2).
    #[test]
    fn a_full_pool_does_not_take_a_cap_slot() {
        let budget = limits::IdleBudget::new(2);
        let pool: InstancePool<()> = InstancePool::with_budget(1, Arc::clone(&budget));
        let held = pool.reserve().expect("one instance fits the target");
        assert_eq!(budget.used(), 1);
        assert_eq!(pool.reserve().err(), Some(Blocked::Pool));
        assert_eq!(budget.used(), 1, "no cap slot was taken and released");
        drop(held);
        assert_eq!(budget.used(), 0);
    }

    #[test]
    fn the_router_digest_sees_every_setting() {
        let info = |name: &str, params: &str, model: &str| types::RouterInfo {
            name: name.into(),
            params_json: params.into(),
            members: vec![types::MemberInfo {
                model: model.into(),
                weight: 1,
                split: None,
                params_json: "{}".into(),
            }],
        };
        let base = router_info_digest(&info("r", "{}", "a"));
        assert_eq!(base, router_info_digest(&info("r", "{}", "a")));
        assert_ne!(base, router_info_digest(&info("r2", "{}", "a")));
        assert_ne!(base, router_info_digest(&info("r", "{\"x\":1}", "a")));
        assert_ne!(base, router_info_digest(&info("r", "{}", "b")));
    }
}
