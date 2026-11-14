//! Sharing rate-limit counters between gateways through Redis.
//!
//! The request path stays local: the limiter never touches Redis. A background
//! task pushes this process's dirty absolute counts every sync interval and
//! pulls the other gateways' counts back, so rpm, tpm and max_parallel hold
//! across the gateways that share one Redis and namespace. Without the URL
//! this module does nothing and the limiter behaves exactly as before.
//!
//! The store is deliberately tiny: one batch operation per sync. Its layout
//! (per-scope, per-dimension, per-minute hashes and one in-flight hash) is in
//! `sync.lua`; a namespace is a prefix, so two installations can share a Redis.
//!
//! Redis being unreachable never stops the gateway: after one second without
//! a successful sync the task drops the shared half
//! ([`crate::ratelimit::Registry::enter_fallback`]) and each process counts for
//! itself again, which is the "N gateways allow up to N times" behaviour. The
//! next successful sync resumes sharing; counts admitted in the meantime stay
//! dirty and are written as absolute values then.

use std::{
    fmt,
    future::Future,
    sync::Arc,
    time::{Duration, Instant},
};

use anyhow::{bail, Context};

use crate::{
    ratelimit::{ScopeCounts, ScopeView},
    runtime::Runtime,
};

#[cfg(test)]
mod memory;
mod redis_store;

#[cfg(test)]
pub use memory::{MemoryHub, MemoryStore};
pub use redis_store::RedisStore;

/// Sync interval bounds and default, shared with the CLI parser.
pub const SYNC_MS_MIN: u64 = 10;
pub const SYNC_MS_MAX: u64 = 1000;
pub const SYNC_MS_DEFAULT: u64 = 100;

/// How long without a successful sync before the process stops trusting the
/// shared half. The design's D3: fail open to full local limits.
const FALLBACK_AFTER: Duration = Duration::from_secs(1);

/// Scopes per `EVALSHA`; the store pipelines the chunks.
const SCOPES_PER_CHUNK: usize = 250;
/// The adaptive sync interval never grows beyond this.
const MAX_INTERVAL: Duration = Duration::from_secs(1);

/// A validated shared-limits configuration. The Redis URL may contain a
/// password, so its `Debug` never prints the value.
#[derive(Clone)]
pub struct Config {
    pub redis_url: String,
    pub sync_interval: Duration,
    pub namespace: String,
}

impl fmt::Debug for Config {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Config")
            .field("redis_url", &"<redacted>")
            .field("sync_interval", &self.sync_interval)
            .field("namespace", &self.namespace)
            .finish()
    }
}

impl Config {
    /// Builds the configuration from the CLI values. `None` means sharing is
    /// off; an empty URL (the Compose template passes one) counts as absent.
    ///
    /// In control-plane mode `default_namespace` is the config scope; in
    /// standalone mode there is no such value, so the namespace is required
    /// with a URL and a clear error is better than silently mixing two
    /// installations' counters under an empty prefix.
    pub fn from_args(
        url: Option<&str>,
        sync_ms: u64,
        namespace: Option<&str>,
        default_namespace: Option<&str>,
    ) -> anyhow::Result<Option<Self>> {
        let url = url.map(str::trim).filter(|url| !url.is_empty());
        let Some(url) = url else {
            return Ok(None);
        };
        if !(url.starts_with("redis://") || url.starts_with("rediss://")) {
            bail!("--shared-limits-redis-url must start with redis:// or rediss://");
        }
        let namespace = namespace
            .map(str::trim)
            .filter(|namespace| !namespace.is_empty())
            .or(default_namespace.map(str::trim).filter(|n| !n.is_empty()))
            .context(
                "--shared-limits-namespace is required with --shared-limits-redis-url in standalone mode",
            )?;
        if namespace.len() > 200 {
            bail!("--shared-limits-namespace must be at most 200 characters");
        }
        // Redis key structure: `{lw:<namespace>}:...`. Braces and controls
        // would break the hash tag or make the key unreadable.
        if namespace
            .chars()
            .any(|c| c.is_control() || matches!(c, '{' | '}'))
        {
            bail!("--shared-limits-namespace may not contain braces or control characters");
        }
        Ok(Some(Self {
            redis_url: url.to_owned(),
            sync_interval: Duration::from_millis(sync_ms.clamp(SYNC_MS_MIN, SYNC_MS_MAX)),
            namespace: namespace.to_owned(),
        }))
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ErrorKind {
    Connect,
    Timeout,
    Script,
}

impl ErrorKind {
    pub fn label(self) -> &'static str {
        match self {
            Self::Connect => "connect",
            Self::Timeout => "timeout",
            Self::Script => "script",
        }
    }
}

/// A sync failure; the message is safe to log (it never contains the URL).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Error {
    pub kind: ErrorKind,
    pub message: String,
}

impl Error {
    pub fn connect(message: impl Into<String>) -> Self {
        Self {
            kind: ErrorKind::Connect,
            message: message.into(),
        }
    }

    pub fn script(message: impl Into<String>) -> Self {
        Self {
            kind: ErrorKind::Script,
            message: message.into(),
        }
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.message)
    }
}

impl std::error::Error for Error {}

/// One batch operation against the shared store. An empty batch is a liveness
/// check (the implementation still reaches Redis) so an idle gateway does not
/// sit in fallback and page on the fallback alert.
pub trait Store: Send + Sync + 'static {
    fn sync(
        &self,
        batch: &[ScopeCounts],
    ) -> impl Future<Output = Result<Vec<ScopeView>, Error>> + Send;

    /// Starts (or keeps waiting for) the connection in the background. The
    /// per-sync timeout must not cover the handshake: a slow TLS connection
    /// has to survive across ticks, and a timed-out connect is a `connect`
    /// failure, not a sync timeout.
    fn prepare(&self) {}

    /// The last sync timed out; drop the cached connection the sync used so
    /// the next attempt cannot reuse a silently dead socket.
    fn reset_connection(&self) {}
}

/// Turns on sharing: parses the URL (no connection yet), then starts the sync
/// task. A malformed URL is a startup error; an unreachable Redis is not.
pub fn spawn(runtime: Arc<Runtime>, config: Config) -> anyhow::Result<tokio::task::JoinHandle<()>> {
    let store = RedisStore::new(&config.redis_url, &config.namespace)
        .map_err(|error| anyhow::anyhow!("invalid shared-limits Redis URL: {error}"))?;
    tracing::info!(
        namespace = %config.namespace,
        interval_ms = config.sync_interval.as_millis(),
        "shared rate limits enabled"
    );
    Ok(spawn_with_store(
        runtime,
        config.sync_interval,
        Arc::from(config.namespace),
        FALLBACK_AFTER,
        store,
    ))
}

/// Starts the loop with any store; unit tests use the in-memory one and a
/// zero fallback delay.
fn spawn_with_store<S: Store>(
    runtime: Arc<Runtime>,
    interval: Duration,
    namespace: Arc<str>,
    fallback_after: Duration,
    store: S,
) -> tokio::task::JoinHandle<()> {
    tokio::spawn(run(SyncTask::new(
        runtime,
        interval,
        namespace,
        fallback_after,
        store,
    )))
}

async fn run<S: Store>(mut task: SyncTask<S>) {
    loop {
        // Sleeping between syncs (rather than a fixed ticker) lets the
        // interval back off when Redis is slow; syncs still never overlap.
        tokio::time::sleep(task.interval).await;
        task.sync_once().await;
    }
}

struct SyncTask<S> {
    runtime: Arc<Runtime>,
    /// The operator's interval; the adaptive interval never goes below it.
    configured_interval: Duration,
    /// Doubles when a sync takes more than half of it, back down when fast.
    interval: Duration,
    namespace: Arc<str>,
    fallback_after: Duration,
    store: S,
    started: Instant,
    last_success: Option<Instant>,
    fallback: bool,
}

impl<S: Store> SyncTask<S> {
    fn new(
        runtime: Arc<Runtime>,
        interval: Duration,
        namespace: Arc<str>,
        fallback_after: Duration,
        store: S,
    ) -> Self {
        // Until the first success this process has no shared state. Report
        // fallback from boot, as the design asks for a gateway starting with
        // Redis unreachable; the first sync clears it.
        runtime.metrics.shared_limits_fallback(true);
        Self {
            runtime,
            configured_interval: interval,
            interval,
            namespace,
            fallback_after,
            store,
            started: Instant::now(),
            last_success: None,
            fallback: true,
        }
    }

    fn timeout(&self) -> Duration {
        self.interval.max(Duration::from_millis(50))
    }

    async fn sync_once(&mut self) {
        self.store.prepare();
        let batch = self.runtime.limiters.pending_sync_interval(self.interval);
        self.runtime.metrics.shared_limits_scopes(batch.len());
        let started = Instant::now();
        let result = tokio::time::timeout(self.timeout(), self.store.sync(&batch)).await;
        let elapsed = started.elapsed();
        match result {
            Ok(Ok(views)) => {
                self.runtime.limiters.apply_sync(&views);
                self.runtime.limiters.mark_synced(&batch);
                self.success(batch.len(), elapsed);
                self.adjust_after_success(elapsed);
            }
            Ok(Err(error)) => {
                self.failure(error.kind, &error.message, batch.len(), elapsed);
                self.adjust_after_failure(error.kind, elapsed);
            }
            Err(_) => {
                // The reply may never arrive on a dead connection; reset it.
                self.store.reset_connection();
                self.failure(
                    ErrorKind::Timeout,
                    "sync exceeded its timeout",
                    batch.len(),
                    elapsed,
                );
                self.adjust_after_failure(ErrorKind::Timeout, elapsed);
            }
        }
    }

    /// A successful sync that takes more than half the interval doubles it (up
    /// to 1 s); four fast successes in a row step it back down. Only success
    /// proves the shorter interval works.
    fn adjust_after_success(&mut self, elapsed: Duration) {
        if elapsed * 2 > self.interval {
            self.interval = (self.interval * 2).min(MAX_INTERVAL);
        } else if elapsed * 4 < self.interval {
            self.interval = (self.interval / 2).max(self.configured_interval);
        }
    }

    /// A slow non-connect failure grows the interval; a connect failure did
    /// no Redis work (the sync may have waited for the handshake and timed
    /// out), so leave it alone. Never shrink on failure: the reconnect tick
    /// would otherwise undo a backoff it has not tested yet.
    fn adjust_after_failure(&mut self, kind: ErrorKind, elapsed: Duration) {
        if kind != ErrorKind::Connect && elapsed * 2 > self.interval {
            self.interval = (self.interval * 2).min(MAX_INTERVAL);
        }
    }

    fn success(&mut self, scopes: usize, elapsed: Duration) {
        self.runtime.metrics.shared_limits_sync(elapsed, "success");
        self.last_success = Some(Instant::now());
        self.runtime
            .metrics
            .shared_limits_last_sync_age(Duration::ZERO);
        if self.fallback {
            self.fallback = false;
            self.runtime.metrics.shared_limits_fallback(false);
            tracing::info!(
                namespace = %self.namespace,
                scopes,
                "shared rate limits recovered"
            );
        }
        if elapsed > self.interval {
            let span = tracing::info_span!(
                "shared_limits_sync",
                result = "success",
                namespace = %self.namespace,
                scopes,
                elapsed_ms = elapsed.as_millis() as u64,
            );
            let _entered = span.enter();
            tracing::info!("shared-limits sync took longer than the interval");
        }
    }

    fn failure(&mut self, kind: ErrorKind, message: &str, scopes: usize, elapsed: Duration) {
        // The batch stays dirty (no `mark_synced`), so the next successful
        // sync writes the same absolute counts. Absolute writes make a late
        // reply harmless: it overwrites, never adds.
        self.runtime.metrics.shared_limits_sync(elapsed, "error");
        self.runtime.metrics.shared_limits_sync_error(kind.label());
        let age = self
            .last_success
            .map_or_else(|| self.started.elapsed(), |at| at.elapsed());
        self.runtime.metrics.shared_limits_last_sync_age(age);
        let entering = !self.fallback && age >= self.fallback_after;
        if entering {
            self.fallback = true;
            self.runtime.limiters.enter_fallback();
            self.runtime.metrics.shared_limits_fallback(true);
            tracing::warn!(
                namespace = %self.namespace,
                reason = kind.label(),
                error = message,
                "shared rate limits fell back to per-gateway counting"
            );
        }
        let span = tracing::info_span!(
            "shared_limits_sync",
            result = "error",
            reason = kind.label(),
            namespace = %self.namespace,
            scopes,
            elapsed_ms = elapsed.as_millis() as u64,
        );
        let _entered = span.enter();
        tracing::debug!(error = message, "shared-limits sync failed");
    }
}

#[cfg(test)]
mod tests;
