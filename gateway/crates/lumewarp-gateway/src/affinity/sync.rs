//! Redis exchange for session-affinity overrides.
//!
//! The request path never reaches Redis. One task ticks beside the shared-limits
//! task on the same URL, namespace and interval: it publishes the overrides this
//! process recorded and reads every router's hash back. Redis down changes
//! nothing on the request path (each replica keeps its own moves) and is logged
//! once at warn.
//!
//! Key layout: `{lw:<namespace>}:affinity:<router>`, a hash whose field is the
//! session hash in 16 lowercase hex characters and whose value is
//! `{"model":"...","expires_ms":...}`. The key carries the shared-limits hash
//! tag, so a cluster client can be dropped in without changing the format.

use std::{collections::HashMap, future::Future, sync::Arc, time::Duration};

use redis::{
    aio::{ConnectionManager, ConnectionManagerConfig},
    Client, ErrorKind as RedisErrorKind,
};

use super::{Publish, Remote};
use crate::runtime::Runtime;

/// The background connect attempt's own budget.
const CONNECT_TIMEOUT: Duration = Duration::from_secs(1);
/// Overrides that failed to publish stay queued for this many ticks' worth of
/// entries (a bounded retry across a short outage).
const BACKLOG_MAX: usize = 8192;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ErrorKind {
    Connect,
    Timeout,
    Script,
}

impl ErrorKind {
    fn label(self) -> &'static str {
        match self {
            Self::Connect => "connect",
            Self::Timeout => "timeout",
            Self::Script => "script",
        }
    }
}

#[derive(Debug)]
pub struct Error {
    pub kind: ErrorKind,
    pub message: String,
}

impl Error {
    fn connect(message: impl Into<String>) -> Self {
        Self {
            kind: ErrorKind::Connect,
            message: message.into(),
        }
    }
}

/// One exchange: publish local overrides, then read every known router's hash.
pub trait Store: Send + Sync + 'static {
    fn exchange(
        &self,
        pushes: &[Publish],
        routers: &[Arc<str>],
    ) -> impl Future<Output = Result<Vec<Remote>, Error>> + Send;
}

/// Starts the sync task. A malformed URL is a startup error; an unreachable
/// Redis is not.
pub fn spawn(
    runtime: Arc<Runtime>,
    redis_url: &str,
    namespace: &str,
    interval: Duration,
) -> anyhow::Result<tokio::task::JoinHandle<()>> {
    let store = RedisStore::new(redis_url, namespace, runtime.affinity.ttl_ms())?;
    tracing::info!(
        namespace = %namespace,
        interval_ms = interval.as_millis(),
        "session-affinity Redis sync enabled"
    );
    Ok(tokio::spawn(run(runtime, interval, store)))
}

async fn run<S: Store>(runtime: Arc<Runtime>, interval: Duration, store: S) {
    let mut task = SyncTask {
        runtime,
        interval,
        store,
        backlog: Vec::new(),
        failing: false,
    };
    loop {
        tokio::time::sleep(task.interval).await;
        task.sync_once().await;
    }
}

struct SyncTask<S> {
    runtime: Arc<Runtime>,
    interval: Duration,
    store: S,
    /// Overrides that could not be published yet; newest entries win room.
    backlog: Vec<Publish>,
    failing: bool,
}

impl<S: Store> SyncTask<S> {
    fn timeout(&self) -> Duration {
        self.interval.max(Duration::from_millis(50))
    }

    async fn sync_once(&mut self) {
        let pending = self.runtime.affinity.take_pending(BACKLOG_MAX);
        self.backlog.extend(pending);
        if self.backlog.len() > BACKLOG_MAX {
            let excess = self.backlog.len() - BACKLOG_MAX;
            self.backlog.drain(..excess);
        }
        let routers = self.runtime.affinity.router_names();
        let timeout = self.timeout();
        let result =
            tokio::time::timeout(timeout, self.store.exchange(&self.backlog, &routers)).await;
        match result {
            Ok(Ok(remotes)) => {
                for remote in remotes {
                    if let Some(router) = self.runtime.affinity.known_router_id(&remote.router) {
                        self.runtime.affinity.merge(
                            router,
                            remote.session,
                            remote.model,
                            remote.expires_ms,
                            super::now_ms(),
                        );
                    }
                }
                self.backlog.clear();
                if self.failing {
                    self.failing = false;
                    tracing::info!("session-affinity Redis sync recovered");
                }
            }
            Ok(Err(error)) => self.failure(error.kind, &error.message),
            Err(_) => self.failure(ErrorKind::Timeout, "sync exceeded its timeout"),
        }
    }

    fn failure(&mut self, kind: ErrorKind, message: &str) {
        self.runtime
            .metrics
            .session_affinity_sync_error(kind.label());
        if !self.failing {
            self.failing = true;
            tracing::warn!(
                reason = kind.label(),
                error = message,
                "session-affinity Redis sync failed; keeping overrides per gateway"
            );
        }
    }
}

#[derive(serde::Serialize, serde::Deserialize)]
struct Value {
    model: String,
    expires_ms: u64,
}

fn encode(session: u64, model: &str, expires_ms: u64) -> (String, String) {
    (
        format!("{session:016x}"),
        serde_json::json!({ "model": model, "expires_ms": expires_ms }).to_string(),
    )
}

fn decode(field: &str, value: &str) -> Option<(u64, String, u64)> {
    let session = u64::from_str_radix(field, 16).ok()?;
    let value: Value = serde_json::from_str(value).ok()?;
    Some((session, value.model, value.expires_ms))
}

/// Redis implementation of [`Store`]. The manager is created lazily and
/// carries its own reconnect; one failed exchange drops it so the next tick
/// cannot reuse a dead socket.
pub struct RedisStore {
    client: Client,
    namespace: String,
    ttl_ms: u64,
    connection: tokio::sync::Mutex<Option<ConnectionManager>>,
}

impl RedisStore {
    pub fn new(redis_url: &str, namespace: &str, ttl_ms: u64) -> anyhow::Result<Self> {
        // The URL may carry a password; never echo it in an error.
        let client = Client::open(redis_url)
            .map_err(|_| anyhow::anyhow!("invalid session-affinity Redis URL"))?;
        Ok(Self {
            client,
            namespace: namespace.to_owned(),
            ttl_ms,
            connection: tokio::sync::Mutex::new(None),
        })
    }

    fn key(&self, router: &str) -> String {
        format!("{{lw:{}}}:affinity:{}", self.namespace, router)
    }

    async fn connection(&self) -> Result<ConnectionManager, Error> {
        let mut slot = self.connection.lock().await;
        if let Some(connection) = slot.as_ref() {
            return Ok(connection.clone());
        }
        let config = ConnectionManagerConfig::new()
            .set_number_of_retries(0)
            .set_connection_timeout(CONNECT_TIMEOUT);
        let connection = match tokio::time::timeout(
            CONNECT_TIMEOUT,
            self.client.get_connection_manager_with_config(config),
        )
        .await
        {
            Ok(Ok(connection)) => connection,
            Ok(Err(error)) => return Err(Error::connect(format!("connect: {error}"))),
            Err(_) => return Err(Error::connect("connect: timed out")),
        };
        *slot = Some(connection.clone());
        Ok(connection)
    }

    async fn reset(&self) {
        *self.connection.lock().await = None;
    }
}

impl Store for RedisStore {
    async fn exchange(
        &self,
        pushes: &[Publish],
        routers: &[Arc<str>],
    ) -> Result<Vec<Remote>, Error> {
        if pushes.is_empty() && routers.is_empty() {
            // A gateway with shared limits but no affinity routers has nothing
            // to exchange; do not open a connection for it.
            return Ok(Vec::new());
        }
        let mut connection = self.connection().await?;
        let ttl = i64::try_from(self.ttl_ms).unwrap_or(i64::MAX);
        let now = super::now_ms();
        let result: Result<Vec<Remote>, redis::RedisError> = async {
            // One round trip: every push (one HSET per router with all its
            // fields, then a TTL), then every router's hash.
            let mut by_router: HashMap<&str, Vec<(String, String)>> = HashMap::new();
            for push in pushes.iter().filter(|push| push.expires_ms > now) {
                by_router.entry(&push.router).or_default().push(encode(
                    push.session,
                    &push.model,
                    push.expires_ms,
                ));
            }
            let mut pipe = redis::pipe();
            for (router, fields) in &by_router {
                let key = self.key(router);
                pipe.hset_multiple(&key, fields).ignore();
                // The hash outlives an idle override by at most one TTL; expired
                // fields inside a busy hash are deleted below.
                pipe.pexpire(&key, ttl).ignore();
            }
            for router in routers {
                pipe.hgetall(self.key(router));
            }
            let hashes: Vec<HashMap<String, String>> = pipe.query_async(&mut connection).await?;
            let mut remotes = Vec::new();
            let mut cleanup = redis::pipe();
            let mut stale = false;
            for (router, fields) in routers.iter().zip(hashes) {
                let mut expired = Vec::new();
                for (field, value) in fields {
                    match decode(&field, &value) {
                        Some((session, model, expires_ms)) if expires_ms > now => {
                            remotes.push(Remote {
                                router: router.clone(),
                                session,
                                model: model.into(),
                                expires_ms,
                            });
                        }
                        // Expired or unreadable: nobody can use it.
                        _ => expired.push(field),
                    }
                }
                if !expired.is_empty() {
                    cleanup.hdel(self.key(router), expired).ignore();
                    stale = true;
                }
            }
            if stale {
                let _: () = cleanup.query_async(&mut connection).await?;
            }
            Ok(remotes)
        }
        .await;
        match result {
            Ok(remotes) => Ok(remotes),
            Err(error) => {
                // A response/type error is not a dead connection; keep it.
                if !matches!(
                    error.kind(),
                    RedisErrorKind::ResponseError | RedisErrorKind::TypeError
                ) {
                    self.reset().await;
                }
                let kind = match error.kind() {
                    RedisErrorKind::ResponseError | RedisErrorKind::TypeError => ErrorKind::Script,
                    _ => ErrorKind::Connect,
                };
                Err(Error {
                    kind,
                    message: format!("affinity: {error}"),
                })
            }
        }
    }
}

#[cfg(test)]
pub mod tests;
