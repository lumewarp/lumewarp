//! Session affinity: one session id, one member model.
//!
//! The gateway picks the member by weighted rendezvous hashing, so every
//! replica agrees without any shared state. When the hashed-best member is
//! ineligible (unhealthy, breaker open, already tried), the session lands on
//! another member and that move is remembered per `(router, session hash)` in
//! a bounded process-local table; later requests keep using the new member
//! while it is eligible, so a session does not flip back when its original
//! member recovers. With a shared-limits Redis the same table is exchanged
//! between gateways once per sync tick, which lets replicas converge on a move
//! they did not see themselves.
//!
//! Nothing here is on the request path except the lookup and the (rare) write
//! of a move: both are lock-free on the read side and never await. Only the
//! 64-bit session hash is stored, never the session id. Redis is reached only
//! by the background task in [`sync`].

use std::{
    borrow::Cow,
    collections::{HashMap, VecDeque},
    sync::{
        atomic::{AtomicUsize, Ordering},
        Arc, Mutex, MutexGuard,
    },
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use hyper::{header::HeaderName, HeaderMap};

use crate::balance;

pub mod sync;
pub use sync::spawn;

/// The session id read before any configured header.
pub const SESSION_HEADER: &str = "x-lumewarp-id";
/// A longer session id is ignored (treated as no session).
pub const MAX_SESSION_BYTES: usize = 256;

pub const MAX_ENTRIES_ENV: &str = "LUMEWARP_SESSION_AFFINITY_MAX_ENTRIES";
pub const TTL_SECONDS_ENV: &str = "LUMEWARP_SESSION_AFFINITY_TTL_SECONDS";
/// Bounded LRU default, per process.
pub const DEFAULT_MAX_ENTRIES: usize = 100_000;
/// Idle TTL default: an override not touched for an hour is forgotten.
pub const DEFAULT_TTL: Duration = Duration::from_secs(3600);

/// How many `(router, session)` overrides wait to be published at most.
const PENDING_MAX: usize = 4096;
/// Overrides are spread over this many independently locked maps.
const SHARDS: usize = 16;

/// Wall-clock milliseconds. Overrides carry an absolute expiry that peers
/// compare, so this cannot be a process-local monotonic clock.
pub fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|elapsed| elapsed.as_millis() as u64)
        .unwrap_or(0)
}

#[derive(Debug, Clone, Copy)]
pub struct Config {
    pub max_entries: usize,
    pub ttl: Duration,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            max_entries: DEFAULT_MAX_ENTRIES,
            ttl: DEFAULT_TTL,
        }
    }
}

impl Config {
    /// `LUMEWARP_SESSION_AFFINITY_MAX_ENTRIES` and
    /// `LUMEWARP_SESSION_AFFINITY_TTL_SECONDS`. A malformed value is logged and
    /// the default is kept: affinity must not depend on a typo to work.
    pub fn from_env() -> Self {
        let mut config = Self::default();
        if let Ok(raw) = std::env::var(MAX_ENTRIES_ENV) {
            match raw.trim().parse::<usize>() {
                Ok(value) if value > 0 => config.max_entries = value,
                _ => tracing::error!(
                    env = MAX_ENTRIES_ENV,
                    value = %raw,
                    "ignoring an invalid session-affinity entry bound"
                ),
            }
        }
        if let Ok(raw) = std::env::var(TTL_SECONDS_ENV) {
            match raw.trim().parse::<u64>() {
                Ok(value) if value > 0 => config.ttl = Duration::from_secs(value),
                _ => tracing::error!(
                    env = TTL_SECONDS_ENV,
                    value = %raw,
                    "ignoring an invalid session-affinity TTL"
                ),
            }
        }
        config
    }

    /// At least one entry per shard, so bounded really is bounded.
    fn per_shard(&self) -> usize {
        (self.max_entries / SHARDS).max(1)
    }
}

/// The session hash, computed once per request from the first source that
/// carries a usable value: `x-lumewarp-id`, then the router's configured
/// header, then the JSON body's top-level `session_id`. Empty or over
/// [`MAX_SESSION_BYTES`] bytes is no session. Allocation-free for headers; an
/// escaped body string (a `Cow::Owned`) only allocates what serde already did.
pub fn session_hash<'a>(
    headers: &HeaderMap,
    custom: Option<&HeaderName>,
    body: impl FnOnce() -> Option<Cow<'a, str>>,
) -> Option<u64> {
    let raw = headers.get(SESSION_HEADER).map(|value| value.as_bytes());
    if let Some(hash) = raw.and_then(hash_session) {
        return Some(hash);
    }
    if let Some(name) = custom {
        if let Some(hash) = headers
            .get(name)
            .map(|value| value.as_bytes())
            .and_then(hash_session)
        {
            return Some(hash);
        }
    }
    body().and_then(|text| hash_session(text.as_bytes()))
}

fn hash_session(bytes: &[u8]) -> Option<u64> {
    (!bytes.is_empty() && bytes.len() <= MAX_SESSION_BYTES).then(|| balance::sticky_hash(bytes))
}

/// One override on its way to Redis.
#[derive(Debug, Clone)]
pub struct Publish {
    pub router: Arc<str>,
    pub session: u64,
    pub model: Arc<str>,
    pub expires_ms: u64,
}

/// One override read back from Redis.
#[derive(Debug, Clone)]
pub struct Remote {
    pub router: Arc<str>,
    pub session: u64,
    pub model: Arc<str>,
    pub expires_ms: u64,
}

struct Entry {
    model: Arc<str>,
    expires_ms: u64,
}

/// Entries compared when a full shard evicts one.
const EVICTION_SAMPLE: usize = 8;

#[derive(Default)]
struct Shard {
    entries: HashMap<(u32, u64), Entry>,
}

/// Process-lifetime override table shared by every compiled routing table.
pub struct Overrides {
    config: Config,
    shards: Box<[Mutex<Shard>]>,
    /// Router name interning: the request path keys by a `u32`.
    ids: Mutex<Ids>,
    /// New local overrides waiting for the Redis task.
    pending: Mutex<VecDeque<Publish>>,
    entries: AtomicUsize,
}

#[derive(Default)]
struct Ids {
    by_name: HashMap<Arc<str>, u32>,
    names: Vec<Arc<str>>,
}

impl Overrides {
    pub fn new(config: Config) -> Self {
        Self {
            config,
            shards: (0..SHARDS).map(|_| Mutex::new(Shard::default())).collect(),
            ids: Mutex::new(Ids::default()),
            pending: Mutex::new(VecDeque::new()),
            entries: AtomicUsize::new(0),
        }
    }

    pub fn ttl_ms(&self) -> u64 {
        self.config.ttl.as_millis() as u64
    }

    /// Compile-time interning of a router with affinity.
    pub fn router_id(&self, name: &str) -> u32 {
        let mut ids = lock(&self.ids);
        if let Some(&id) = ids.by_name.get(name) {
            return id;
        }
        let id = u32::try_from(ids.names.len()).expect("fewer than 2^32 routers");
        let shared: Arc<str> = name.into();
        ids.by_name.insert(shared.clone(), id);
        ids.names.push(shared);
        id
    }

    /// The id of a router this process still knows, for merging remote
    /// overrides; an unknown name is another gateway's removed router.
    pub fn known_router_id(&self, name: &str) -> Option<u32> {
        lock(&self.ids).by_name.get(name).copied()
    }

    pub fn router_names(&self) -> Vec<Arc<str>> {
        lock(&self.ids).names.clone()
    }

    fn shard_of(&self, router: u32, session: u64) -> usize {
        ((session ^ u64::from(router).wrapping_mul(0x9E37_79B9_7F4A_7C15)) as usize)
            % self.shards.len()
    }

    /// The override for `(router, session)`, if it is still fresh. Refreshes
    /// an almost-expired entry (idle TTL, not absolute); a contended shard
    /// answers `None` and the request falls back to the stateless hash.
    pub fn lookup(&self, router: u32, session: u64, now_ms: u64) -> Option<Arc<str>> {
        // Most sessions never moved: an empty table takes no lock at all.
        if self.entries.load(Ordering::Relaxed) == 0 {
            return None;
        }
        let Ok(mut shard) = self.shards[self.shard_of(router, session)].try_lock() else {
            return None;
        };
        let entry = shard.entries.get_mut(&(router, session))?;
        if entry.expires_ms <= now_ms {
            shard.entries.remove(&(router, session));
            self.entries.fetch_sub(1, Ordering::Relaxed);
            return None;
        }
        if entry.expires_ms <= now_ms + self.ttl_ms() / 2 {
            entry.expires_ms = now_ms + self.ttl_ms();
        }
        Some(entry.model.clone())
    }

    /// Records a move. Contended or over the bound, the stateless hash simply
    /// serves the next request; nothing awaits.
    pub fn remember(
        &self,
        router: u32,
        router_name: &Arc<str>,
        session: u64,
        model: &str,
        now_ms: u64,
    ) {
        let model: Arc<str> = model.into();
        self.record(
            router,
            Some(router_name),
            session,
            model,
            now_ms + self.ttl_ms(),
            true,
        );
    }

    /// Merges a peer's override, newest expiry wins. Only the background task
    /// calls this, so it may block.
    pub fn merge(&self, router: u32, session: u64, model: Arc<str>, expires_ms: u64, now_ms: u64) {
        if expires_ms <= now_ms {
            return;
        }
        self.record(router, None, session, model, expires_ms, false);
    }

    fn record(
        &self,
        router: u32,
        router_name: Option<&Arc<str>>,
        session: u64,
        model: Arc<str>,
        expires_ms: u64,
        publish: bool,
    ) {
        let index = self.shard_of(router, session);
        let mut shard = if publish {
            match self.shards[index].try_lock() {
                Ok(shard) => shard,
                Err(_) => return,
            }
        } else {
            lock(&self.shards[index])
        };
        if let Some(entry) = shard.entries.get_mut(&(router, session)) {
            if expires_ms <= entry.expires_ms {
                return;
            }
            entry.model = model.clone();
            entry.expires_ms = expires_ms;
        } else {
            let cap = self.config.per_shard();
            if shard.entries.len() >= cap {
                // Approximate LRU: of a few entries (hash order is effectively
                // random), evict the one closest to expiry, i.e. untouched
                // longest. Bounded work under the lock, even when the table is
                // full during a mass failover.
                if let Some(oldest) = shard
                    .entries
                    .iter()
                    .take(EVICTION_SAMPLE)
                    .min_by_key(|(_, entry)| entry.expires_ms)
                    .map(|(key, _)| *key)
                {
                    shard.entries.remove(&oldest);
                    self.entries.fetch_sub(1, Ordering::Relaxed);
                }
            }
            shard.entries.insert(
                (router, session),
                Entry {
                    model: model.clone(),
                    expires_ms,
                },
            );
            self.entries.fetch_add(1, Ordering::Relaxed);
        }
        drop(shard);
        if publish {
            let name = router_name.expect("publishes carry a router name").clone();
            if let Ok(mut pending) = self.pending.try_lock() {
                if pending.len() >= PENDING_MAX {
                    pending.pop_front();
                }
                pending.push_back(Publish {
                    router: name,
                    session,
                    model,
                    expires_ms,
                });
            }
        }
    }

    /// Takes up to `limit` overrides for publication.
    pub fn take_pending(&self, limit: usize) -> Vec<Publish> {
        let mut pending = lock(&self.pending);
        let take = pending.len().min(limit);
        pending.drain(..take).collect()
    }

    /// Drops expired entries; scrape-time only. Returns the live count.
    pub fn prune(&self, now_ms: u64) -> usize {
        let mut live = 0;
        for shard in &self.shards {
            let mut shard = lock(shard);
            shard.entries.retain(|_, entry| entry.expires_ms > now_ms);
            live += shard.entries.len();
        }
        self.entries.store(live, Ordering::Relaxed);
        live
    }

    #[cfg(test)]
    pub fn len(&self) -> usize {
        self.entries.load(Ordering::Relaxed)
    }
}

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

#[cfg(test)]
mod tests;
