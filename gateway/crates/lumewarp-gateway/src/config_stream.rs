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
    CatalogReport, ConfigUpdate, GatewayMessage, HealthReport, Hello, Nack, NackCode,
    PluginDescriptor, SharedLimits, Snapshot, TargetHealth,
};
use tokio::{sync::mpsc, task::JoinHandle};
use tokio_stream::wrappers::ReceiverStream;
use tonic::{
    metadata::MetadataValue,
    transport::{ClientTlsConfig, Endpoint},
    Request,
};

use crate::{
    catalog, gateway_id,
    router::RoutingTable,
    runtime::Runtime,
    server,
    snapshot::{self, ApplyError},
};

const MAX_MESSAGE_BYTES: usize = snapshot::MAX_SNAPSHOT_BYTES;
const BACKOFF_MIN: Duration = Duration::from_millis(500);
const BACKOFF_MAX: Duration = Duration::from_secs(30);
/// Probe state changes are coalesced into at most one report per interval.
const HEALTH_REPORT_INTERVAL: Duration = Duration::from_secs(1);
/// Catalog observations are coalesced the same way; the registry already drops
/// repeats within its own 10-minute window, so this only avoids one message per
/// observed tool while `tools/list` answers.
const CATALOG_REPORT_INTERVAL: Duration = Duration::from_secs(1);

pub struct StreamConfig {
    pub url: String,
    /// Read from the environment by the caller; never persisted or logged.
    pub token: String,
    pub scope: String,
    pub state_dir: PathBuf,
    /// `Some` when this gateway shares its limiter through Redis; reported in
    /// `Hello.shared_limits` for the Gateways page.
    pub shared_limits_namespace: Option<String>,
}

/// The immutable startup plugin set as Hello reports it.
fn plugin_descriptors() -> Vec<PluginDescriptor> {
    lumewarp_plugin_host::global().descriptors()
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
    tracing::info!(gateway_id = gateway_id::gateway_id(), "gateway process");
    let client = Client {
        endpoint,
        bearer,
        config,
        current: Arc::new(current),
        digest,
        has_base,
        slot,
        runtime,
        plugins: plugin_descriptors(),
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
    /// Sent in every Hello; fixed at startup.
    plugins: Vec<PluginDescriptor>,
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
                gateway_id: gateway_id::gateway_id().to_owned(),
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
                plugins: self.plugins.clone(),
                plugin_abi: lumewarp_plugin_host::ABI,
                shared_limits: Some(SharedLimits {
                    enabled: self.config.shared_limits_namespace.is_some(),
                    namespace: self
                        .config
                        .shared_limits_namespace
                        .clone()
                        .unwrap_or_default(),
                }),
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
        // Report once right away: the state may come from the on-disk snapshot.
        let mut report_at = Some(tokio::time::Instant::now());
        // Catalog observations wake the loop like probe changes do; a report is
        // only sent when something was queued (`drain_report` is lossless).
        let mut catalog_at: Option<tokio::time::Instant> = None;
        loop {
            let report_due = async {
                match report_at {
                    Some(at) => tokio::time::sleep_until(at).await,
                    None => std::future::pending().await,
                }
            };
            let catalog_due = async {
                match catalog_at {
                    Some(at) => tokio::time::sleep_until(at).await,
                    None => std::future::pending().await,
                }
            };
            tokio::select! {
                update = inbound.message() => {
                    let Some(update) = update? else { break };
                    received = true;
                    let reply = self.handle(update).await;
                    let acked = matches!(reply.msg, Some(Msg::Ack(_)));
                    if tx.send(reply).await.is_err() {
                        break;
                    }
                    if acked {
                        report_at.get_or_insert_with(tokio::time::Instant::now);
                        // An apply can change what is reportable only through
                        // the proxies, but a fresh ACK is a natural flush point.
                        if catalog_at.is_none() && self.runtime.catalog.pending() > 0 {
                            catalog_at = Some(tokio::time::Instant::now());
                        }
                    }
                }
                () = self.runtime.health.changed(), if report_at.is_none() => {
                    report_at = Some(tokio::time::Instant::now() + HEALTH_REPORT_INTERVAL);
                }
                () = self.runtime.catalog.changed(), if catalog_at.is_none() => {
                    catalog_at = Some(tokio::time::Instant::now() + CATALOG_REPORT_INTERVAL);
                }
                () = report_due => {
                    report_at = None;
                    if tx.send(self.health_report()).await.is_err() {
                        break;
                    }
                }
                () = catalog_due => {
                    catalog_at = None;
                    if let Some(report) = self.catalog_report() {
                        let message = GatewayMessage {
                            msg: Some(Msg::Catalog(report)),
                        };
                        if let Err(error) = tx.send(message).await {
                            // The message never left this gateway: put what it
                            // carried back so the next session reports it
                            // (M5). The queue is bounded, so this cannot grow
                            // without limit.
                            if let Some(Msg::Catalog(report)) = error.0.msg {
                                self.runtime.catalog.restore(report);
                            }
                            break;
                        }
                        // A batch may have been size- or count-limited; drain
                        // the rest right away instead of waiting a second.
                        if self.runtime.catalog.pending() > 0 && catalog_at.is_none() {
                            catalog_at = Some(tokio::time::Instant::now());
                        }
                    }
                }
            }
        }
        Ok(received)
    }

    fn health_report(&self) -> GatewayMessage {
        let (upstreams, models) = self.runtime.health.probed();
        let wire = |probed: Vec<(String, bool)>| {
            probed
                .into_iter()
                .map(|(name, healthy)| TargetHealth { name, healthy })
                .collect()
        };
        GatewayMessage {
            msg: Some(Msg::Health(HealthReport {
                upstreams: wire(upstreams),
                models: wire(models),
            })),
        }
    }

    /// The next queued catalog batch, or `None` when there is nothing to
    /// report. Unlike `HealthReport`, a `CatalogReport` adds to what was
    /// reported before (`config_service.proto`, "Each report adds to what was
    /// reported before"), so only non-empty reports are sent. The batch is
    /// bounded (`MAX_REPORT_ITEMS`, `MAX_REPORT_BYTES`); anything that did not
    /// fit stays queued and is sent next (M5).
    fn catalog_report(&self) -> Option<CatalogReport> {
        let report = self
            .runtime
            .catalog
            .drain_report(catalog::MAX_REPORT_ITEMS, catalog::MAX_REPORT_BYTES);
        (!report.definitions.is_empty() || !report.cards.is_empty()).then_some(report)
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
            Ok((next, mut routing_table, digest)) => {
                routing_table.activate();
                // L1: observations of a server this snapshot no longer
                // configures are dropped with it.
                self.runtime
                    .observed
                    .retain_servers(routing_table.targets.servers().map(|s| s.name.as_ref()));
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

    /// The session loop against an in-process `ConfigService`: a queued
    /// observation becomes one `CatalogReport` on the stream, and nothing is
    /// reported while the registry is empty.
    mod catalog {
        use super::super::*;
        use lumewarp_proto::config::v1::{config_update::Update, gateway_message::Msg};
        use std::{
            convert::Infallible,
            net::SocketAddr,
            pin::Pin,
            task::{Context, Poll},
        };
        use tokio::sync::mpsc;
        use tokio_stream::{wrappers::TcpListenerStream, Stream};
        use tonic::{
            codegen::{http, BoxFuture, Service},
            server::NamedService,
            Request, Response, Status, Streaming,
        };

        struct Conn {
            inbound: mpsc::UnboundedReceiver<GatewayMessage>,
            outbound: mpsc::UnboundedSender<Result<ConfigUpdate, Status>>,
        }

        impl Conn {
            fn send(&self, update: ConfigUpdate) {
                self.outbound
                    .send(Ok(update))
                    .expect("gateway stream closed");
            }

            /// The next Hello/ACK/NACK/Catalog; health reports are skipped.
            async fn recv(&mut self) -> Msg {
                loop {
                    let message = tokio::time::timeout(Duration::from_secs(5), self.inbound.recv())
                        .await
                        .expect("gateway sent nothing")
                        .expect("gateway closed the stream");
                    match message.msg {
                        Some(Msg::Health(_)) => continue,
                        Some(msg) => return msg,
                        None => continue,
                    }
                }
            }
        }

        type Conns = mpsc::UnboundedSender<Conn>;

        #[derive(Clone)]
        struct Svc(Conns);

        impl NamedService for Svc {
            const NAME: &'static str = "lumewarp.config.v1.ConfigService";
        }

        struct Updates(mpsc::UnboundedReceiver<Result<ConfigUpdate, Status>>);
        impl Stream for Updates {
            type Item = Result<ConfigUpdate, Status>;
            fn poll_next(
                mut self: Pin<&mut Self>,
                cx: &mut Context<'_>,
            ) -> Poll<Option<Self::Item>> {
                self.0.poll_recv(cx)
            }
        }

        struct StreamImpl(Conns);
        impl tonic::server::StreamingService<GatewayMessage> for StreamImpl {
            type Response = ConfigUpdate;
            type ResponseStream = Updates;
            type Future = BoxFuture<Response<Updates>, Status>;

            fn call(&mut self, request: Request<Streaming<GatewayMessage>>) -> Self::Future {
                let conns = self.0.clone();
                Box::pin(async move {
                    let mut incoming = request.into_inner();
                    let (in_tx, in_rx) = mpsc::unbounded_channel();
                    let (out_tx, out_rx) = mpsc::unbounded_channel();
                    tokio::spawn(async move {
                        while let Ok(Some(message)) = incoming.message().await {
                            if in_tx.send(message).is_err() {
                                break;
                            }
                        }
                    });
                    let _ = conns.send(Conn {
                        inbound: in_rx,
                        outbound: out_tx,
                    });
                    Ok(Response::new(Updates(out_rx)))
                })
            }
        }

        impl Service<http::Request<tonic::body::Body>> for Svc {
            type Response = http::Response<tonic::body::Body>;
            type Error = Infallible;
            type Future = BoxFuture<Self::Response, Self::Error>;

            fn poll_ready(&mut self, _: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
                Poll::Ready(Ok(()))
            }

            fn call(&mut self, req: http::Request<tonic::body::Body>) -> Self::Future {
                let conns = self.0.clone();
                Box::pin(async move {
                    let mut grpc = tonic::server::Grpc::new(tonic_prost::ProstCodec::default());
                    Ok(grpc.streaming(StreamImpl(conns), req).await)
                })
            }
        }

        async fn fake_cp() -> (SocketAddr, mpsc::UnboundedReceiver<Conn>) {
            let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
            let addr = listener.local_addr().unwrap();
            let (tx, conns) = mpsc::unbounded_channel();
            tokio::spawn(async move {
                // The test aborts nothing: the fake control plane lives until
                // the process ends.
                let _ = tonic::transport::Server::builder()
                    .add_service(Svc(tx))
                    .serve_with_incoming_shutdown(TcpListenerStream::new(listener), async {
                        std::future::pending::<()>().await
                    })
                    .await;
            });
            (addr, conns)
        }

        fn full(version: u64, scope: &str) -> ConfigUpdate {
            ConfigUpdate {
                target_digest: vec![version as u8],
                update: Some(Update::Full(Snapshot {
                    version,
                    scope: scope.into(),
                    ..Default::default()
                })),
            }
        }

        #[tokio::test]
        async fn a_queued_observation_is_sent_as_one_catalog_report() {
            let (addr, mut conns) = fake_cp().await;
            let dir = tempfile::tempdir().unwrap();
            let runtime = Runtime::new();
            let slot: server::Snapshot = Arc::new(arc_swap::ArcSwapOption::empty());
            let config = StreamConfig {
                url: format!("http://{addr}"),
                token: "gw-token".into(),
                scope: "scope-a".into(),
                state_dir: dir.path().to_owned(),
                shared_limits_namespace: None,
            };
            let handle = spawn(
                config,
                Snapshot {
                    scope: "scope-a".into(),
                    ..Default::default()
                },
                Vec::new(),
                false,
                slot,
                runtime.clone(),
            )
            .unwrap();
            let mut conn = tokio::time::timeout(Duration::from_secs(5), conns.recv())
                .await
                .unwrap()
                .expect("fake control plane accepted no stream");
            match conn.recv().await {
                Msg::Hello(hello) => {
                    assert_eq!(hello.gateway_id, crate::gateway_id::gateway_id())
                }
                other => panic!("expected Hello, got {other:?}"),
            }
            conn.send(full(1, "scope-a"));
            match conn.recv().await {
                Msg::Ack(ack) => assert_eq!(ack.version, 1),
                other => panic!("expected Ack, got {other:?}"),
            }

            // Nothing is reported while nothing was observed.
            let quiet = tokio::time::timeout(Duration::from_millis(300), conn.recv()).await;
            assert!(quiet.is_err(), "an empty catalog report is not sent");

            runtime.catalog.observe(&crate::catalog::Observation {
                server: "s1",
                server_id: "id-s1",
                kind: lumewarp_proto::config::v1::McpDefinitionKind::Tool,
                name: "echo",
                canonical_json: r#"{"name":"echo"}"#,
                hash: [7u8; 32],
                auto_approved: true,
                config_version: 1,
            });
            let report = match conn.recv().await {
                Msg::Catalog(report) => report,
                other => panic!("expected Catalog, got {other:?}"),
            };
            assert_eq!(report.definitions.len(), 1);
            let definition = &report.definitions[0];
            assert_eq!(definition.server, "s1");
            assert_eq!(definition.server_id, "id-s1");
            assert_eq!(definition.config_version, 1, "L3 provenance");
            assert_eq!(definition.name, "echo");
            assert_eq!(definition.hash, vec![7; 32]);
            assert!(definition.auto_approved);
            assert!(report.cards.is_empty());
            assert_eq!(runtime.catalog.pending(), 0, "the report is drained");

            // A card is reported the same way.
            runtime.catalog.card(&crate::catalog::CardObservation {
                agent: "a1",
                agent_id: "id-a1",
                card_json: r#"{"name":"a1"}"#,
                error: "",
                config_version: 1,
            });
            let report = match conn.recv().await {
                Msg::Catalog(report) => report,
                other => panic!("expected Catalog, got {other:?}"),
            };
            assert_eq!(report.cards.len(), 1);
            assert_eq!(report.cards[0].agent_id, "id-a1");
            assert_eq!(report.cards[0].config_version, 1);
            handle.abort();
        }

        #[tokio::test]
        async fn hello_reports_the_shared_limits_namespace() {
            let (addr, mut conns) = fake_cp().await;
            let dir = tempfile::tempdir().unwrap();
            let runtime = Runtime::new();
            let slot: server::Snapshot = Arc::new(arc_swap::ArcSwapOption::empty());
            let config = StreamConfig {
                url: format!("http://{addr}"),
                token: "gw-token".into(),
                scope: "scope-a".into(),
                state_dir: dir.path().to_owned(),
                shared_limits_namespace: Some("team-ns".into()),
            };
            let handle = spawn(
                config,
                Snapshot {
                    scope: "scope-a".into(),
                    ..Default::default()
                },
                Vec::new(),
                false,
                slot,
                runtime,
            )
            .unwrap();
            let mut conn = tokio::time::timeout(Duration::from_secs(5), conns.recv())
                .await
                .unwrap()
                .expect("fake control plane accepted no stream");
            match conn.recv().await {
                Msg::Hello(hello) => {
                    let shared = hello.shared_limits.expect("shared limits in Hello");
                    assert!(shared.enabled);
                    assert_eq!(shared.namespace, "team-ns");
                }
                other => panic!("expected Hello, got {other:?}"),
            }
            handle.abort();
        }
    }
}
