//! Control-plane config stream client (`docs/config-protocol.md`). One
//! background task owns the session loop and applies updates strictly one at
//! a time: apply -> validate -> compile -> persist -> swap -> ACK. Nothing here
//! runs on the request path; the only shared state is the `ArcSwapOption`.

use std::{
    path::PathBuf,
    sync::Arc,
    time::{Duration, Instant},
};

use lumewarp_config::AuthMode;
use lumewarp_proto::config::v1::{
    config_service_client::ConfigServiceClient, config_update::Update, gateway_message::Msg, Ack,
    ConfigUpdate, GatewayMessage, Hello, Nack, NackCode, Snapshot, StrategyDescriptor,
};
use tokio::{sync::mpsc, task::JoinHandle};
use tokio_stream::wrappers::ReceiverStream;
use tonic::{
    metadata::MetadataValue,
    transport::{ClientTlsConfig, Endpoint},
    Request,
};

use crate::{
    router::RoutingTable,
    runtime::Runtime,
    server,
    snapshot::{self, ApplyError},
};

const MAX_MESSAGE_BYTES: usize = snapshot::MAX_SNAPSHOT_BYTES;
const BACKOFF_MIN: Duration = Duration::from_millis(500);
const BACKOFF_MAX: Duration = Duration::from_secs(30);

pub struct StreamConfig {
    pub url: String,
    /// Read from the environment by the caller; never persisted or logged.
    pub token: String,
    pub gateway_id: String,
    pub scope: String,
    pub state_dir: PathBuf,
}

/// The compiled-in strategies as Hello reports them.
fn strategy_descriptors() -> Vec<StrategyDescriptor> {
    lumewarp_router_api::descriptors()
        .into_iter()
        .map(|d| StrategyDescriptor {
            name: d.name.into(),
            title: d.title.into(),
            description: d.description.into(),
            uses_weight: d.uses_weight,
            needs: d.needs.into_iter().map(Into::into).collect(),
            params_schema_json: d.params_schema.to_string(),
            member_schema_json: d.member_schema.to_string(),
            digest: d.digest,
        })
        .collect()
}

/// Compiles a snapshot loaded from disk for immediate serving.
pub fn compile(snapshot: &Snapshot, runtime: &Arc<Runtime>) -> Result<RoutingTable, ApplyError> {
    RoutingTable::compile(snapshot, AuthMode::RequireKey, runtime)
}

/// Starts the stream task. `current` is the snapshot already active in
/// `slot` (an empty one carrying only the scope when cold-booting).
/// `has_base` says whether `current` is a real, complete snapshot (loaded
/// cache); without one every delta is refused until a full has been applied.
pub fn spawn(
    config: StreamConfig,
    current: Snapshot,
    digest: Vec<u8>,
    has_base: bool,
    slot: server::Snapshot,
    runtime: Arc<Runtime>,
) -> anyhow::Result<JoinHandle<()>> {
    let mut endpoint = Endpoint::from_shared(config.url.clone())?
        .connect_timeout(Duration::from_secs(10))
        .tcp_nodelay(true)
        .http2_keep_alive_interval(Duration::from_secs(30))
        .keep_alive_timeout(Duration::from_secs(10))
        .keep_alive_while_idle(true);
    if config.url.starts_with("https://") {
        endpoint = endpoint.tls_config(ClientTlsConfig::new().with_webpki_roots())?;
    }
    let bearer: MetadataValue<_> = format!("Bearer {}", config.token)
        .parse()
        .map_err(|_| anyhow::anyhow!("gateway token is not a valid header value"))?;
    let mut bearer = bearer;
    bearer.set_sensitive(true);
    let client = Client {
        endpoint,
        bearer,
        config,
        current: Arc::new(current),
        digest,
        has_base,
        slot,
        runtime,
        strategies: strategy_descriptors(),
    };
    Ok(tokio::spawn(client.run()))
}

struct Client {
    endpoint: Endpoint,
    bearer: MetadataValue<tonic::metadata::Ascii>,
    config: StreamConfig,
    /// The snapshot that is active and durable; base of the next update.
    current: Arc<Snapshot>,
    /// Opaque control-plane digest of `current`; echoed in Hello.
    digest: Vec<u8>,
    /// A valid complete base exists: a loaded cache or an applied full. Never
    /// inferred from version numbers (the empty cold-boot base has version 0).
    has_base: bool,
    slot: server::Snapshot,
    runtime: Arc<Runtime>,
    /// Sent in every Hello; fixed at build time.
    strategies: Vec<StrategyDescriptor>,
}

/// Exponential backoff with full jitter.
fn backoff(attempt: u32) -> Duration {
    let ceiling = BACKOFF_MIN
        .saturating_mul(1u32.checked_shl(attempt).unwrap_or(u32::MAX))
        .min(BACKOFF_MAX);
    Duration::from_secs_f64(fastrand::f64() * ceiling.as_secs_f64())
}

impl Client {
    async fn run(mut self) {
        let mut attempt = 0u32;
        loop {
            let started = Instant::now();
            match self.session().await {
                Ok(received) if received => attempt = 0,
                Ok(_) => {}
                Err(err) => tracing::warn!(error = %err, "config stream ended"),
            }
            self.runtime.metrics.config_stream_connected(false);
            // A session that stayed up was healthy; start from the minimum.
            if started.elapsed() > BACKOFF_MAX {
                attempt = 0;
            }
            let delay = backoff(attempt);
            attempt = attempt.saturating_add(1);
            tokio::time::sleep(delay).await;
        }
    }

    /// Returns whether at least one update was received.
    async fn session(&mut self) -> anyhow::Result<bool> {
        let channel = self.endpoint.connect().await?;
        let mut client = ConfigServiceClient::new(channel)
            .max_decoding_message_size(MAX_MESSAGE_BYTES)
            .max_encoding_message_size(MAX_MESSAGE_BYTES);
        let (tx, rx) = mpsc::channel::<GatewayMessage>(4);
        let have = self.current.version;
        tx.send(GatewayMessage {
            msg: Some(Msg::Hello(Hello {
                gateway_id: self.config.gateway_id.clone(),
                have_version: have,
                gateway_version: env!("CARGO_PKG_VERSION").into(),
                supported_capabilities: snapshot::supported_capabilities(),
                have_digest: if have > 0 {
                    self.digest.clone()
                } else {
                    Vec::new()
                },
                scope: if have > 0 {
                    self.config.scope.clone()
                } else {
                    String::new()
                },
                strategies: self.strategies.clone(),
            })),
        })
        .await?;
        let mut request = Request::new(ReceiverStream::new(rx));
        request
            .metadata_mut()
            .insert("authorization", self.bearer.clone());
        let mut inbound = client.stream(request).await?.into_inner();
        self.runtime.metrics.config_stream_connected(true);
        tracing::info!(have_version = have, "config stream connected");

        let mut received = false;
        while let Some(update) = inbound.message().await? {
            received = true;
            let reply = self.handle(update).await;
            if tx.send(reply).await.is_err() {
                break;
            }
        }
        Ok(received)
    }

    /// Applies one update and returns the ACK or NACK to send.
    async fn handle(&mut self, update: ConfigUpdate) -> GatewayMessage {
        let version = match &update.update {
            Some(Update::Full(full)) => full.version,
            Some(Update::Delta(delta)) => delta.version,
            None => 0,
        };
        let target_digest = update.target_digest.clone();
        let refuse_delta = !self.has_base && matches!(update.update, Some(Update::Delta(_)));
        let base = self.current.clone();
        let runtime = self.runtime.clone();
        let dir = self.config.state_dir.clone();
        let applied = tokio::task::spawn_blocking(move || {
            if refuse_delta {
                return Err(ApplyError {
                    code: NackCode::BaseMismatch,
                    reason: "delta received but no complete base snapshot is held".into(),
                });
            }
            apply_update(&base, update, &target_digest, &dir, &runtime)
                .map(|(s, r)| (s, r, target_digest))
        })
        .await
        .unwrap_or_else(|_| Err(ApplyError::invalid("apply task failed")));
        match applied {
            Ok((next, routing_table, digest)) => {
                routing_table.activate_health();
                self.slot.store(Some(Arc::new(routing_table)));
                self.runtime.metrics.config_applied(next.version);
                tracing::info!(version = next.version, "config applied");
                self.current = Arc::new(next);
                self.digest = digest;
                self.has_base = true;
                GatewayMessage {
                    msg: Some(Msg::Ack(Ack { version })),
                }
            }
            Err(err) => {
                self.runtime.metrics.config_apply_failed(err.code);
                tracing::warn!(version, code = ?err.code, reason = %err.reason, "config update rejected");
                GatewayMessage {
                    msg: Some(Msg::Nack(Nack {
                        version,
                        reason: err.reason,
                        have_version: self.current.version,
                        code: err.code as i32,
                    })),
                }
            }
        }
    }
}

/// Blocking: apply on a private copy, validate and compile, then persist.
/// The caller swaps only on `Ok`.
fn apply_update(
    base: &Snapshot,
    update: ConfigUpdate,
    digest: &[u8],
    dir: &std::path::Path,
    runtime: &Arc<Runtime>,
) -> Result<(Snapshot, RoutingTable), ApplyError> {
    let next = snapshot::apply(base, update)?;
    let routing_table = compile(&next, runtime)?;
    snapshot::store_with_digest(dir, &next, digest).map_err(|err| ApplyError {
        code: NackCode::PersistFailed,
        reason: format!("persisting snapshot failed: {:?}", err.kind()),
    })?;
    Ok((next, routing_table))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn backoff_stays_within_bounds() {
        for attempt in 0..70 {
            assert!(backoff(attempt) <= BACKOFF_MAX);
        }
        assert!(backoff(0) <= BACKOFF_MIN);
    }
}
