//! Process-wide bounds for plugin Stores and instance pools.
//!
//! Three separate limits, because they protect different things:
//!
//! - [`store_limits`] caps one Store: linear memory, tables, instances. Without
//!   the table limits a guest's `table.grow` could allocate host memory far past
//!   the memory limit (wasmtime's default `table_elements` is unlimited).
//! - [`Budget`] caps the linear memory of *all* Stores in the process, which is
//!   what grows with the number of routers, of pooled instances and of snapshot
//!   generations alive at once. Default 1 GiB, `LUMEWARP_PLUGIN_MEMORY_LIMIT_BYTES`.
//! - [`memory_permits`] is the async admission gate for plugin calls: one permit
//!   per [`MEMORY_PER_STORE`] the budget allows. A call that cannot get one
//!   waits (bounded by its own timeout) instead of failing.
//! - [`IdleBudget`] caps how many instances the pools may own (idle + checked
//!   out), so a snapshot with many routers cannot pin unbounded Stores.
//!
//! Wasm memory never shrinks, so a Store's reservation only grows; it is
//! released when the Store (and with it [`crate::State`]) is dropped.

use std::{
    future::Future,
    pin::Pin,
    sync::{
        atomic::{AtomicUsize, Ordering},
        Arc, OnceLock,
    },
};

use tokio::sync::{OwnedSemaphorePermit, Semaphore};
use wasmtime::{ResourceLimiter, StoreLimits, StoreLimitsBuilder};

/// Linear memory one Store may reach.
pub const MEMORY_PER_STORE: usize = 32 * 1024 * 1024;

/// Linear memory every Store together may reach, unless the environment says
/// otherwise.
pub const DEFAULT_MEMORY_LIMIT: usize = 1024 * 1024 * 1024;

/// `LUMEWARP_PLUGIN_MEMORY_LIMIT_BYTES`: the process-wide wasm memory budget.
pub const MEMORY_LIMIT_ENV: &str = "LUMEWARP_PLUGIN_MEMORY_LIMIT_BYTES";

/// Table elements one Store may hold (`table.grow` past it returns -1).
pub const TABLE_ELEMENTS: usize = 10_000;

/// Tables one Store may create.
pub const TABLES: usize = 4;

/// Core and component instances one Store may create. A component world is one
/// or two instances; the rest is headroom for instantiation and teardown.
pub const INSTANCES: usize = 8;

/// Instances all pools together may own, idle or checked out. The memory budget
/// (1 GiB by default) binds long before this: it is the backstop that keeps a
/// pathological plugin set from pinning host structures.
pub const IDLE_INSTANCES: usize = 4096;

/// The static limits of one Store.
pub fn store_limits() -> StoreLimits {
    StoreLimitsBuilder::new()
        .memory_size(MEMORY_PER_STORE)
        .memories(1)
        .table_elements(TABLE_ELEMENTS)
        .tables(TABLES)
        .instances(INSTANCES)
        .build()
}

/// One permit per [`MEMORY_PER_STORE`] the process budget allows: the number of
/// plugin calls that may run at the same time.
pub fn memory_permits(limit: usize) -> Arc<Semaphore> {
    Arc::new(Semaphore::new((limit / MEMORY_PER_STORE).max(1)))
}

/// The process semaphore from the process budget.
pub fn memory_semaphore() -> &'static Arc<Semaphore> {
    static PERMITS: OnceLock<Arc<Semaphore>> = OnceLock::new();
    PERMITS.get_or_init(|| memory_permits(memory_budget().limit()))
}

/// A permit that counts towards one plugin call's timeout while it waits.
pub struct MemoryReservation(Option<OwnedSemaphorePermit>);

impl MemoryReservation {
    /// Takes a permit, waiting as long as the caller's future lives.
    pub async fn acquire(permits: Arc<Semaphore>) -> Option<Self> {
        permits
            .acquire_owned()
            .await
            .ok()
            .map(|permit| Self(Some(permit)))
    }

    /// Takes a permit when one is free (pre-warming must not wait).
    pub fn try_acquire(permits: Arc<Semaphore>) -> Option<Self> {
        permits
            .try_acquire_owned()
            .ok()
            .map(|permit| Self(Some(permit)))
    }
}

impl Drop for MemoryReservation {
    fn drop(&mut self) {
        drop(self.0.take());
    }
}

/// A future that is [`Send`] and boxed, so a plugin call can be awaited
/// without holding a non-`Send` borrow across the await point.
pub type BoxFuture<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;

/// The linear memory of every plugin Store in the process.
#[derive(Debug)]
pub struct Budget {
    limit: usize,
    used: AtomicUsize,
}

impl Budget {
    pub fn new(limit: usize) -> Arc<Self> {
        Arc::new(Self {
            limit,
            used: AtomicUsize::new(0),
        })
    }

    pub fn limit(&self) -> usize {
        self.limit
    }

    pub fn used(&self) -> usize {
        self.used.load(Ordering::Relaxed)
    }

    /// Reserves `bytes` for one Store's growth; false when that would exceed
    /// the budget.
    pub fn try_reserve(&self, bytes: usize) -> bool {
        let mut used = self.used.load(Ordering::Relaxed);
        loop {
            let next = used.saturating_add(bytes);
            if next > self.limit {
                return false;
            }
            match self
                .used
                .compare_exchange_weak(used, next, Ordering::AcqRel, Ordering::Relaxed)
            {
                Ok(_) => return true,
                Err(current) => used = current,
            }
        }
    }

    pub fn release(&self, bytes: usize) {
        // A release never wraps: a confused caller must not free more than was
        // ever reserved.
        let mut used = self.used.load(Ordering::Relaxed);
        loop {
            let next = used.saturating_sub(bytes);
            match self
                .used
                .compare_exchange_weak(used, next, Ordering::AcqRel, Ordering::Relaxed)
            {
                Ok(_) => return,
                Err(current) => used = current,
            }
        }
    }
}

/// The process budget, from the environment or the default.
pub fn memory_budget() -> &'static Arc<Budget> {
    static BUDGET: OnceLock<Arc<Budget>> = OnceLock::new();
    BUDGET.get_or_init(|| Budget::new(limit_from_env()))
}

fn limit_from_env() -> usize {
    match std::env::var(MEMORY_LIMIT_ENV) {
        Ok(text) => match text.trim().parse::<usize>() {
            Ok(bytes) if bytes > 0 => bytes,
            _ => DEFAULT_MEMORY_LIMIT,
        },
        Err(_) => DEFAULT_MEMORY_LIMIT,
    }
}

/// Counts the instances the pools own. Nothing is created past the cap; a pool
/// that cannot get a slot waits for a returned instance instead.
#[derive(Debug)]
pub struct IdleBudget {
    limit: usize,
    used: AtomicUsize,
}

impl IdleBudget {
    pub fn new(limit: usize) -> Arc<Self> {
        Arc::new(Self {
            limit,
            used: AtomicUsize::new(0),
        })
    }

    /// Claims one instance slot, or `None` when the cap is reached.
    pub fn acquire(self: &Arc<Self>) -> Option<IdleSlot> {
        let mut used = self.used.load(Ordering::Relaxed);
        loop {
            if used >= self.limit {
                return None;
            }
            match self.used.compare_exchange_weak(
                used,
                used + 1,
                Ordering::AcqRel,
                Ordering::Relaxed,
            ) {
                Ok(_) => return Some(IdleSlot(Arc::clone(self))),
                Err(current) => used = current,
            }
        }
    }

    pub fn used(&self) -> usize {
        self.used.load(Ordering::Relaxed)
    }

    pub fn limit(&self) -> usize {
        self.limit
    }
}

/// One instance's claim on the [`IdleBudget`]; released when the instance dies.
#[derive(Debug)]
pub struct IdleSlot(Arc<IdleBudget>);

impl Drop for IdleSlot {
    fn drop(&mut self) {
        let mut used = self.0.used.load(Ordering::Relaxed);
        loop {
            let next = used.saturating_sub(1);
            match self
                .0
                .used
                .compare_exchange_weak(used, next, Ordering::AcqRel, Ordering::Relaxed)
            {
                Ok(_) => return,
                Err(current) => used = current,
            }
        }
    }
}

pub fn idle_budget() -> &'static Arc<IdleBudget> {
    static BUDGET: OnceLock<Arc<IdleBudget>> = OnceLock::new();
    BUDGET.get_or_init(|| IdleBudget::new(IDLE_INSTANCES))
}

/// Process-relative monotonic milliseconds, for the plugin breaker.
pub fn mono_ms() -> u64 {
    static START: OnceLock<std::time::Instant> = OnceLock::new();
    let start = START.get_or_init(std::time::Instant::now);
    u64::try_from(start.elapsed().as_millis()).unwrap_or(u64::MAX)
}

/// Keeps [`ResourceLimiter`] in scope for the `StoreLimits` delegation in
/// [`crate::State`].
pub fn delegate_limits() -> StoreLimits {
    store_limits()
}

#[allow(dead_code)]
fn assert_limiter_object_safe(limiter: &mut dyn ResourceLimiter) -> usize {
    limiter.memories()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_budget_never_overcommits() {
        let budget = Budget::new(100);
        assert!(budget.try_reserve(60));
        assert!(!budget.try_reserve(60));
        assert_eq!(budget.used(), 60);
        assert!(budget.try_reserve(40));
        assert_eq!(budget.used(), 100);
        assert!(!budget.try_reserve(1));
        budget.release(100);
        assert_eq!(budget.used(), 0);
        // A release larger than the reservation saturates instead of wrapping.
        budget.release(1);
        assert_eq!(budget.used(), 0);
    }

    #[test]
    fn the_idle_budget_caps_the_pool_count() {
        let budget = IdleBudget::new(2);
        let first = budget.acquire().unwrap();
        let second = budget.acquire().unwrap();
        assert!(budget.acquire().is_none());
        assert_eq!(budget.used(), 2);
        drop(first);
        assert_eq!(budget.used(), 1);
        assert!(budget.acquire().is_some());
        drop(second);
    }

    #[test]
    fn the_store_limits_are_bounded() {
        let limits = store_limits();
        let mut limits = limits;
        assert_eq!(limits.memories(), 1);
        assert_eq!(limits.tables(), TABLES);
        assert_eq!(limits.instances(), INSTANCES);
        // A table of more elements than the cap is refused by the limiter.
        assert!(!limits.table_growing(0, TABLE_ELEMENTS + 1, None).unwrap());
        assert!(limits.table_growing(0, TABLE_ELEMENTS, None).unwrap());
        // And so is linear memory past one Store's size.
        assert!(!limits
            .memory_growing(0, MEMORY_PER_STORE + 1, None)
            .unwrap());
        assert!(limits.memory_growing(0, MEMORY_PER_STORE, None).unwrap());
    }
}
