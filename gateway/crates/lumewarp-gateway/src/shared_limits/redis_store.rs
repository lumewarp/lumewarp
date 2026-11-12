//! Redis implementation of [`Store`].
//!
//! One endpoint (Redis or Valkey 6.2+). Every key carries the hash tag
//! `{lw:<namespace>}` so a later cluster client only has to change this file.
//! Commands go through one pipelined `EVALSHA` per 250 scopes, with an `EVAL`
//! fallback when Redis no longer has the script (a restart, `SCRIPT FLUSH`,
//! failover in a replica).
//!
//! The connection is established in the background: the per-sync timeout must
//! not cover the TCP/TLS handshake, otherwise a link slower than the sync
//! interval never connects. A timed-out or failed connect is reported as a
//! `connect` error and the next tick retries.

use std::{
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        Arc, Mutex as StdMutex,
    },
    time::Duration,
};

use redis::{
    aio::{ConnectionManager, ConnectionManagerConfig},
    Client, ErrorKind as RedisErrorKind, Script,
};
use tokio::sync::{Mutex, Notify};

use crate::instance;

use super::{Error, ErrorKind, ScopeCounts, ScopeView, Store, SCOPES_PER_CHUNK};

const SCRIPT: &str = include_str!("sync.lua");
/// The background connect attempt's own budget.
const CONNECT_TIMEOUT: Duration = Duration::from_secs(1);

pub struct RedisStore {
    client: Client,
    namespace: String,
    instance: String,
    script_sha: String,
    /// The live manager and the generation it was created with, once the
    /// background connect succeeds.
    connection: Arc<Mutex<Option<(u64, ConnectionManager)>>>,
    connected: Arc<AtomicBool>,
    /// One background connect at a time; it survives sync timeouts.
    connecting: Arc<AtomicBool>,
    /// Wakes a sync waiting for an in-flight connect.
    connect_done: Arc<Notify>,
    /// Why the last connect did not produce a manager (safe to log).
    last_error: Arc<StdMutex<Option<String>>>,
    /// The generation of the manager the current sync used (0 = none). A
    /// late reset must not clear a manager created after the timed-out one.
    sync_generation: Arc<AtomicU64>,
    /// Monotonic generation for each new manager.
    generation: Arc<AtomicU64>,
}

impl RedisStore {
    /// Parses the URL; it does not connect, so a gateway can start while Redis
    /// is down (it falls back to per-process limits until the first sync).
    pub fn new(redis_url: &str, namespace: &str) -> Result<Self, Error> {
        Self::with_instance(redis_url, namespace, instance::instance_id())
    }

    /// The instance id is normally this process's; tests set it explicitly to
    /// act as several gateways in one process.
    pub fn with_instance(
        redis_url: &str,
        namespace: &str,
        instance: impl Into<String>,
    ) -> Result<Self, Error> {
        // The URL may carry a password; never echo it in an error.
        let client = Client::open(redis_url)
            .map_err(|_| Error::connect("invalid or unsupported Redis URL"))?;
        Ok(Self {
            client,
            namespace: namespace.to_owned(),
            instance: instance.into(),
            script_sha: Script::new(SCRIPT).get_hash().to_owned(),
            connection: Arc::new(Mutex::new(None)),
            connected: Arc::new(AtomicBool::new(false)),
            connecting: Arc::new(AtomicBool::new(false)),
            connect_done: Arc::new(Notify::new()),
            last_error: Arc::new(StdMutex::new(None)),
            sync_generation: Arc::new(AtomicU64::new(0)),
            generation: Arc::new(AtomicU64::new(0)),
        })
    }

    fn connect_error(&self) -> Error {
        let message = self
            .last_error
            .lock()
            .unwrap()
            .clone()
            .unwrap_or_else(|| "connect: Redis connection is not established".to_owned());
        Error::connect(message)
    }

    async fn sync_connected(
        &self,
        connection: &mut ConnectionManager,
        batch: &[ScopeCounts],
    ) -> Result<Vec<ScopeView>, Error> {
        if batch.is_empty() {
            // An idle gateway still has to prove the store is reachable, or
            // it would sit in fallback and trigger the fallback alert.
            redis::cmd("PING")
                .query_async::<String>(connection)
                .await
                .map_err(|error| classify(error, "ping"))?;
            return Ok(Vec::new());
        }
        let replies = match self.run_pipeline(connection, batch, false).await {
            Ok(replies) => replies,
            Err(error) if error.kind() == RedisErrorKind::NoScriptError => {
                // The script cache was lost (restart, SCRIPT FLUSH, failover).
                // Every EVALSHA in the batch failed without writing, so the
                // EVAL retry cannot double-apply.
                let _ = redis::cmd("SCRIPT")
                    .arg("LOAD")
                    .arg(SCRIPT)
                    .query_async::<String>(connection)
                    .await;
                self.run_pipeline(connection, batch, true)
                    .await
                    .map_err(|error| classify(error, "EVAL"))?
            }
            Err(error) => return Err(classify(error, "EVALSHA")),
        };
        if replies.len() != batch.chunks(SCOPES_PER_CHUNK).count() {
            return Err(Error::script("Redis returned a different number of chunks"));
        }
        let mut views = Vec::with_capacity(batch.len());
        for (chunk, reply) in batch.chunks(SCOPES_PER_CHUNK).zip(replies) {
            if reply.len() != chunk.len() * 8 {
                return Err(Error::script("Redis returned a malformed scope view"));
            }
            for (counts, values) in chunk.iter().zip(reply.as_chunks::<8>().0) {
                let total = |value: i64| u64::try_from(value.max(0)).unwrap_or(0);
                let count = |value: i64| u32::try_from(value.max(0)).unwrap_or(u32::MAX);
                views.push(ScopeView {
                    id: counts.id.clone(),
                    minute: counts.minute,
                    rpm_prev: total(values[0]),
                    rpm_cur: total(values[1]),
                    tpm_prev: total(values[2]),
                    tpm_cur: total(values[3]),
                    rpm_instances: count(values[4]),
                    tpm_instances: count(values[5]),
                    in_flight: count(values[6]),
                    in_flight_instances: count(values[7]),
                });
            }
        }
        Ok(views)
    }

    /// One round trip for all chunks. `eval` selects EVAL over EVALSHA for the
    /// NOSCRIPT fallback.
    async fn run_pipeline(
        &self,
        connection: &mut ConnectionManager,
        batch: &[ScopeCounts],
        eval: bool,
    ) -> Result<Vec<Vec<i64>>, redis::RedisError> {
        let mut pipeline = redis::pipe();
        for chunk in batch.chunks(SCOPES_PER_CHUNK) {
            let mut command = if eval {
                redis::cmd("EVAL")
            } else {
                redis::cmd("EVALSHA")
            };
            if eval {
                command.arg(SCRIPT);
            } else {
                command.arg(&self.script_sha);
            }
            command.arg(chunk.len() * 5);
            for counts in chunk {
                let base = format!("{{lw:{}}}:{}", self.namespace, counts.id);
                let previous = counts.minute.saturating_sub(1);
                command.arg(format!("{base}:r:{previous}"));
                command.arg(format!("{base}:r:{}", counts.minute));
                command.arg(format!("{base}:t:{previous}"));
                command.arg(format!("{base}:t:{}", counts.minute));
                command.arg(format!("{base}:p"));
            }
            command.arg(&self.instance);
            command.arg(chunk.len());
            for counts in chunk {
                command.arg(u8::from(counts.write));
                command.arg(u8::from(counts.refresh_ttl));
                command.arg(u8::from(counts.rpm));
                command.arg(u8::from(counts.tpm));
                command.arg(counts.rpm_prev);
                command.arg(counts.rpm_cur);
                command.arg(counts.tpm_prev);
                command.arg(counts.tpm_cur);
                command.arg(i64::from(counts.in_flight));
            }
            pipeline.add_command(command);
        }
        pipeline.query_async(connection).await
    }
}

impl RedisStore {
    /// Drops the manager only when it is still the generation that failed, so
    /// a late reset cannot wipe a manager the next connect already stored.
    fn drop_connection(&self, generation: u64) {
        match self.connection.try_lock() {
            Ok(mut connection) => {
                if matches!(connection.as_ref(), Some((current, _)) if *current == generation) {
                    *connection = None;
                    self.connected.store(false, Ordering::Release);
                }
            }
            Err(_) => {
                let connection = self.connection.clone();
                let connected = self.connected.clone();
                tokio::spawn(async move {
                    let mut connection = connection.lock().await;
                    if matches!(connection.as_ref(), Some((current, _)) if *current == generation) {
                        *connection = None;
                        connected.store(false, Ordering::Release);
                    }
                });
            }
        }
    }
}

impl Store for RedisStore {
    async fn sync(&self, batch: &[ScopeCounts]) -> Result<Vec<ScopeView>, Error> {
        self.sync_generation.store(0, Ordering::Release);
        if !self.connected.load(Ordering::Acquire) {
            // A sync may arrive while the handshake is still in flight (the
            // task's connect has its own budget). Wait for it inside this
            // sync's timeout instead of failing before it has a chance.
            self.prepare();
            loop {
                let notified = self.connect_done.notified();
                if self.connected.load(Ordering::Acquire) {
                    break;
                }
                if !self.connecting.load(Ordering::Acquire) {
                    return Err(self.connect_error());
                }
                notified.await;
            }
        }
        let Some((generation, mut connection)) = self.connection.lock().await.clone() else {
            self.connected.store(false, Ordering::Release);
            return Err(self.connect_error());
        };
        self.sync_generation.store(generation, Ordering::Release);
        let result = self.sync_connected(&mut connection, batch).await;
        if let Err(error) = &result {
            if error.kind != ErrorKind::Script {
                // A dead or refused connection is rebuilt by the next
                // background connect; only clear the generation we used.
                self.drop_connection(generation);
            }
        }
        result
    }

    fn prepare(&self) {
        if self.connected.load(Ordering::Acquire) {
            return;
        }
        if self.connecting.swap(true, Ordering::AcqRel) {
            return;
        }
        let generation = self.generation.fetch_add(1, Ordering::AcqRel) + 1;
        let client = self.client.clone();
        let connection = self.connection.clone();
        let connected = self.connected.clone();
        let connecting = self.connecting.clone();
        let connect_done = self.connect_done.clone();
        let last_error = self.last_error.clone();
        tokio::spawn(async move {
            // No response timeout: the sync task's outer timeout bounds every
            // command and `reset_connection` drops a manager whose command
            // hung, so the manager does not need a frozen per-command limit.
            let config = ConnectionManagerConfig::new()
                .set_number_of_retries(0)
                .set_connection_timeout(CONNECT_TIMEOUT);
            let result = tokio::time::timeout(
                CONNECT_TIMEOUT,
                client.get_connection_manager_with_config(config),
            )
            .await;
            match result {
                Ok(Ok(manager)) => {
                    *connection.lock().await = Some((generation, manager));
                    *last_error.lock().unwrap() = None;
                    connected.store(true, Ordering::Release);
                }
                Ok(Err(error)) => {
                    *last_error.lock().unwrap() = Some(format!("connect: {error}"));
                }
                Err(_) => {
                    *last_error.lock().unwrap() = Some("connect: timed out".to_owned());
                }
            }
            connecting.store(false, Ordering::Release);
            connect_done.notify_one();
        });
    }

    fn reset_connection(&self) {
        // `sync` records the manager generation it used before issuing any
        // command. Zero means the timeout happened while waiting for a
        // connect, so there is nothing to clear.
        let generation = self.sync_generation.load(Ordering::Acquire);
        if generation != 0 {
            self.drop_connection(generation);
        }
    }
}

fn classify(error: redis::RedisError, operation: &str) -> Error {
    let kind = match error.kind() {
        RedisErrorKind::ResponseError
        | RedisErrorKind::ExecAbortError
        | RedisErrorKind::NoScriptError
        | RedisErrorKind::TypeError => ErrorKind::Script,
        _ => ErrorKind::Connect,
    };
    // Redis errors do not include the connection URL (and therefore never the
    // password); the scope id may appear in a script error, which is not
    // secret.
    Error {
        kind,
        message: format!("{operation}: {error}"),
    }
}

#[cfg(test)]
mod tests {
    use std::time::Instant;

    use super::*;
    use crate::ratelimit::clock;

    fn redis_url() -> Option<String> {
        std::env::var("LUMEWARP_TEST_REDIS_URL")
            .ok()
            .filter(|url| !url.trim().is_empty())
    }

    fn namespace() -> String {
        format!("test-{}", uuid::Uuid::now_v7())
    }

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

    async fn command(url: &str) -> redis::aio::MultiplexedConnection {
        let client = redis::Client::open(url).expect("test Redis URL");
        client
            .get_multiplexed_async_connection()
            .await
            .expect("test Redis connection")
    }

    async fn ready(store: &RedisStore) {
        store.prepare();
        for _ in 0..200 {
            if store.sync(&[]).await.is_ok() {
                return;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        panic!("Redis connection was not established");
    }

    async fn cleanup(connection: &mut redis::aio::MultiplexedConnection, namespace: &str) {
        let pattern = format!("{{lw:{namespace}}}:*");
        let mut cursor = 0u64;
        loop {
            let (next, keys): (u64, Vec<String>) = redis::cmd("SCAN")
                .arg(cursor)
                .arg("MATCH")
                .arg(&pattern)
                .arg("COUNT")
                .arg(1000)
                .query_async(connection)
                .await
                .unwrap();
            if !keys.is_empty() {
                let _: i64 = redis::cmd("DEL")
                    .arg(&keys)
                    .query_async(connection)
                    .await
                    .unwrap();
            }
            cursor = next;
            if cursor == 0 {
                break;
            }
        }
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn script_semantics_expiry_and_idempotent_writes() {
        let Some(url) = redis_url() else {
            return;
        };
        let namespace = namespace();
        let minute = u64::try_from(clock::unix_ms() / 60_000).unwrap();
        let mut connection = command(&url).await;
        let a = RedisStore::with_instance(&url, &namespace, "a").unwrap();
        let b = RedisStore::with_instance(&url, &namespace, "b").unwrap();
        ready(&a).await;
        ready(&b).await;

        let mut first = counts("team:t", minute);
        first.rpm_cur = 3;
        first.rpm_prev = 2;
        first.tpm_cur = 40;
        first.in_flight = 2;
        let views = a.sync(&[first.clone()]).await.unwrap();
        assert_eq!(views[0].rpm_cur, 0, "no other instance yet");
        assert_eq!(views[0].rpm_instances, 1, "self counts");
        assert_eq!(views[0].in_flight_instances, 1);

        let mut second = counts("team:t", minute);
        second.rpm_cur = 4;
        second.rpm_prev = 1;
        second.tpm_prev = 10;
        second.in_flight = 1;
        let views = b.sync(&[second]).await.unwrap();
        assert_eq!(
            (views[0].rpm_prev, views[0].rpm_cur),
            (2, 3),
            "the other instance's counts"
        );
        assert_eq!((views[0].tpm_prev, views[0].tpm_cur), (0, 40));
        assert_eq!(views[0].rpm_instances, 2);
        assert_eq!(views[0].in_flight, 2, "the other instance's slots");
        assert_eq!(views[0].in_flight_instances, 2);

        // Resending the same absolute counts overwrites, never adds.
        let mut repeat = first;
        repeat.in_flight = 0;
        a.sync(&[repeat]).await.unwrap();
        let base = format!("{{lw:{namespace}}}:team:t");
        let own: i64 = redis::cmd("HGET")
            .arg(format!("{base}:r:{minute}"))
            .arg("a")
            .query_async(&mut connection)
            .await
            .unwrap();
        assert_eq!(own, 3, "an idempotent retry must not double-count");

        let ttl: i64 = redis::cmd("PTTL")
            .arg(format!("{base}:r:{minute}"))
            .query_async(&mut connection)
            .await
            .unwrap();
        assert!((0..=150_000).contains(&ttl), "counter TTL {ttl}");
        let ttl: i64 = redis::cmd("PTTL")
            .arg(format!("{base}:p"))
            .query_async(&mut connection)
            .await
            .unwrap();
        assert!((0..=10_000).contains(&ttl), "in-flight TTL {ttl}");

        // A field with an ancient Redis-time stamp is ignored and deleted.
        let _: i64 = redis::cmd("HSET")
            .arg(format!("{base}:p"))
            .arg("stale")
            .arg("5:0")
            .query_async(&mut connection)
            .await
            .unwrap();
        let mut read_only = counts("team:t", minute);
        read_only.write = false;
        read_only.refresh_ttl = false;
        let views = a.sync(&[read_only]).await.unwrap();
        assert_eq!(views[0].in_flight, 1, "only the fresh b slot");
        assert_eq!(views[0].in_flight_instances, 2, "a and b, not stale");
        let exists: i64 = redis::cmd("HEXISTS")
            .arg(format!("{base}:p"))
            .arg("stale")
            .query_async(&mut connection)
            .await
            .unwrap();
        assert_eq!(exists, 0, "the stale field was deleted");
        let views = b.sync(&[counts("team:t", minute)]).await.unwrap();
        assert_eq!(views[0].rpm_cur, 3);
        cleanup(&mut connection, &namespace).await;
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn keys_created_by_a_write_always_have_a_ttl() {
        let Some(url) = redis_url() else {
            return;
        };
        let namespace = namespace();
        let mut connection = command(&url).await;
        let minute = u64::try_from(clock::unix_ms() / 60_000).unwrap();
        async fn pttl(connection: &mut redis::aio::MultiplexedConnection, key: String) -> i64 {
            redis::cmd("PTTL")
                .arg(key)
                .query_async::<i64>(connection)
                .await
                .unwrap()
        }
        let base = format!("{{lw:{namespace}}}:team:t");

        let store = RedisStore::with_instance(&url, &namespace, "a").unwrap();
        ready(&store).await;
        let mut first = counts("team:t", minute);
        first.rpm_cur = 5;
        first.tpm = false;
        // No periodic refresh: creating the key alone must set the TTL.
        first.refresh_ttl = false;
        store.sync(&[first]).await.unwrap();
        // The process "dies" right after the write.
        drop(store);

        let ttl = pttl(&mut connection, format!("{base}:r:{minute}")).await;
        assert!((0..=150_000).contains(&ttl) && ttl > 0, "counter TTL {ttl}");
        let ttl = pttl(&mut connection, format!("{base}:p")).await;
        assert!(
            (0..=10_000).contains(&ttl) && ttl > 0,
            "in-flight TTL {ttl}"
        );
        let exists: i64 = redis::cmd("EXISTS")
            .arg(format!("{base}:r:{}", minute - 1))
            .query_async(&mut connection)
            .await
            .unwrap();
        assert_eq!(exists, 0, "a zero previous count creates no key");
        let exists: i64 = redis::cmd("EXISTS")
            .arg(format!("{base}:t:{minute}"))
            .query_async(&mut connection)
            .await
            .unwrap();
        assert_eq!(exists, 0, "an unused dimension creates no key");

        // A zero count is an HDEL, not a zero-valued field or a new key.
        let store = RedisStore::with_instance(&url, &namespace, "a").unwrap();
        ready(&store).await;
        let mut zero = counts("team:zero", minute);
        zero.rpm_cur = 0;
        zero.tpm = false;
        zero.refresh_ttl = false;
        store.sync(&[zero]).await.unwrap();
        let exists: i64 = redis::cmd("EXISTS")
            .arg(format!("{{lw:{namespace}}}:team:zero:r:{minute}"))
            .query_async(&mut connection)
            .await
            .unwrap();
        assert_eq!(exists, 0, "a zero write leaves no counter key");
        cleanup(&mut connection, &namespace).await;
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn a_flushed_script_falls_back_and_reloads() {
        let Some(url) = redis_url() else {
            return;
        };
        let namespace = namespace();
        let mut connection = command(&url).await;
        let store = RedisStore::with_instance(&url, &namespace, "a").unwrap();
        ready(&store).await;
        let minute = u64::try_from(clock::unix_ms() / 60_000).unwrap();
        let mut first = counts("team:t", minute);
        first.rpm_cur = 1;
        store.sync(&[first]).await.unwrap();

        let _: redis::Value = redis::cmd("SCRIPT")
            .arg("FLUSH")
            .query_async(&mut connection)
            .await
            .unwrap();
        let mut second = counts("team:t", minute);
        second.rpm_cur = 2;
        let views = store.sync(&[second]).await.expect("EVAL fallback");
        assert_eq!(views[0].rpm_instances, 1);
        // A later sync uses EVALSHA again because the fallback reloaded it.
        let mut third = counts("team:t", minute);
        third.rpm_cur = 3;
        store.sync(&[third]).await.unwrap();
        let own: i64 = redis::cmd("HGET")
            .arg(format!("{{lw:{namespace}}}:team:t:r:{minute}"))
            .arg("a")
            .query_async(&mut connection)
            .await
            .unwrap();
        assert_eq!(own, 3);
        cleanup(&mut connection, &namespace).await;
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn two_redis_backed_registries_converge() {
        use std::sync::Arc;

        use crate::auth::Caller;
        use crate::ratelimit::Registry;
        use crate::router::KeyContext;
        use lumewarp_proto::config::v1::{KeyStatus, RateLimit};

        async fn push(registry: &Registry, store: &RedisStore) -> Vec<ScopeCounts> {
            let batch = registry.pending_sync();
            let views = store.sync(&batch).await.unwrap();
            registry.apply_sync(&views);
            registry.mark_synced(&batch);
            batch
        }

        let Some(url) = redis_url() else {
            return;
        };
        let namespace = namespace();
        let mut connection = command(&url).await;
        let registry_a = Registry::new();
        let registry_b = Registry::new();
        let store_a = RedisStore::with_instance(&url, &namespace, "a").unwrap();
        let store_b = RedisStore::with_instance(&url, &namespace, "b").unwrap();
        ready(&store_a).await;
        ready(&store_b).await;
        let key = Caller::Key(Arc::new(KeyContext {
            key_id: "k".into(),
            team_id: "".into(),
            status: KeyStatus::Active,
            expires_at_ms: 0,
            allow_all_models: true,
            allowed_models: Default::default(),
            scopes: vec![(
                "team:t".into(),
                RateLimit {
                    requests_per_minute: 2,
                    tokens_per_minute: 0,
                    max_parallel_requests: 0,
                },
            )],
            team_label: 0,
            grants: crate::grants::Grants::none(),
        }));

        // A admits one and syncs; B admits one, syncs and applies A's count;
        // A then refreshes its read-only view and both reject a third request.
        drop(registry_a.admit(&key).unwrap());
        push(&registry_a, &store_a).await;
        drop(registry_b.admit(&key).unwrap());
        push(&registry_b, &store_b).await;
        push(&registry_a, &store_a).await;
        assert!(registry_a.admit(&key).is_err());
        assert!(registry_b.admit(&key).is_err());
        cleanup(&mut connection, &namespace).await;
    }

    /// Timing only; run explicitly against a local Redis:
    /// `LUMEWARP_TEST_REDIS_URL=… cargo test -p lumewarp-gateway --bin lumewarp
    ///  timing_10k -- --ignored --nocapture`
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    #[ignore = "timing; needs LUMEWARP_TEST_REDIS_URL"]
    async fn timing_10k_scopes() {
        let Some(url) = redis_url() else {
            return;
        };
        let namespace = namespace();
        let mut connection = command(&url).await;
        let store = RedisStore::with_instance(&url, &namespace, "timing").unwrap();
        ready(&store).await;
        let minute = u64::try_from(clock::unix_ms() / 60_000).unwrap();
        let batch: Vec<_> = (0..10_000)
            .map(|i| {
                let mut counts = counts(&format!("team:{i}"), minute);
                counts.rpm_cur = 1;
                counts
            })
            .collect();
        let chunks = batch.len().div_ceil(SCOPES_PER_CHUNK);
        let start = Instant::now();
        store.sync(&batch).await.unwrap();
        let elapsed = start.elapsed();
        println!(
            "10k dirty scopes: {:.1} ms total, {:.2} ms per {} scope chunk",
            elapsed.as_secs_f64() * 1000.0,
            elapsed.as_secs_f64() * 1000.0 / chunks as f64,
            SCOPES_PER_CHUNK
        );
        let mut read_only = batch.clone();
        for counts in &mut read_only {
            counts.write = false;
            counts.refresh_ttl = false;
        }
        let start = Instant::now();
        store.sync(&read_only).await.unwrap();
        let elapsed = start.elapsed();
        println!(
            "10k read-only scopes: {:.1} ms total, {:.2} ms per {} scope chunk",
            elapsed.as_secs_f64() * 1000.0,
            elapsed.as_secs_f64() * 1000.0 / chunks as f64,
            SCOPES_PER_CHUNK
        );
        // The common single-dimension case skips the tpm hashes entirely.
        let mut rpm_only = read_only;
        for counts in &mut rpm_only {
            counts.tpm = false;
        }
        let start = Instant::now();
        store.sync(&rpm_only).await.unwrap();
        let elapsed = start.elapsed();
        println!(
            "10k read-only rpm-only scopes: {:.1} ms total, {:.2} ms per {} scope chunk",
            elapsed.as_secs_f64() * 1000.0,
            elapsed.as_secs_f64() * 1000.0 / chunks as f64,
            SCOPES_PER_CHUNK
        );
        cleanup(&mut connection, &namespace).await;
    }
}
