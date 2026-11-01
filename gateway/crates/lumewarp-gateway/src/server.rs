use std::{
    convert::Infallible,
    sync::Arc,
    time::{Duration, Instant},
};

use anyhow::Context;
use arc_swap::ArcSwapOption;
use bytes::Bytes;
use hyper::{
    body::Incoming, header, server::conn::http1, service::service_fn, Method, Request, Response,
    StatusCode,
};
use hyper_util::rt::{TokioIo, TokioTimer};
use tokio::{net::TcpListener, sync::watch, task::JoinSet};
use tracing::Instrument;

use crate::{
    auth, error,
    metrics::{self, HandleKey, Observation, API_ENDPOINTS},
    proxy::{self, full_body, ProxyBody, UpstreamClient},
    router::{KeyContext, RoutingTable},
    runtime::Runtime,
    telemetry,
    usage::{RequestCtx, UsageEvent},
};

/// The active routing_table, absent until the first snapshot is applied (control-
/// plane cold boot without an on-disk snapshot). The config stream stores
/// into it; every request loads it exactly once, so a request in progress
/// keeps the routing_table it started with across swaps.
pub type Snapshot = Arc<ArcSwapOption<RoutingTable>>;

/// Readiness: a routing_table is loaded. Without one, `/v1/*` returns 503
/// `not_ready` (before authentication) and the admin `/readyz` returns 503;
/// nothing falls back to an open routing_table.
#[cfg(test)]
pub fn ready(snapshot: &Snapshot) -> bool {
    snapshot.load().is_some()
}

/// Serves whatever routing_table `snapshot` holds. `runtime` is the process-lifetime
/// state (metrics, limiters, usage sink), usable while no routing_table is loaded;
/// every routing_table stored in `snapshot` must be compiled with this `runtime`.
pub async fn serve(
    config: &lumewarp_config::LocalSettings,
    snapshot: Snapshot,
    runtime: Arc<Runtime>,
    otel: bool,
) -> anyhow::Result<()> {
    let listen = config.listen;
    let admin_listen = config.admin_listen;
    let header_read_timeout = Duration::from_millis(config.header_read_timeout_ms);
    let body_read_timeout = Duration::from_millis(config.body_read_timeout_ms);
    let shutdown_grace = Duration::from_millis(config.shutdown_grace_ms);
    let client = runtime.client.clone();
    runtime.retry_budget.configure(config.retry_budget_percent);
    let listener = TcpListener::bind(listen)
        .await
        .with_context(|| format!("binding {listen}"))?;
    let admin_listener = TcpListener::bind(admin_listen)
        .await
        .with_context(|| format!("binding admin {admin_listen}"))?;
    let listen = listener.local_addr()?;
    let admin_listen = admin_listener.local_addr()?;
    tracing::info!(%listen, %admin_listen, "lumewarp gateway listening");
    let shutdown = shutdown_signal();
    tokio::pin!(shutdown);
    let (drain, _) = watch::channel(false);
    let mut connections = JoinSet::new();
    loop {
        tokio::select! {
            accepted = async {
                tokio::select! {
                    result = listener.accept() => (result, false),
                    result = admin_listener.accept() => (result, true),
                }
            } => {
                let (accepted, admin) = accepted;
                let (stream, _) = match accepted {
                    Ok(conn) => conn,
                    Err(err) => {
                        tracing::warn!(%err, "accept failed");
                        tokio::time::sleep(Duration::from_millis(10)).await;
                        continue;
                    }
                };
                let _ = stream.set_nodelay(true);
                let snapshot = snapshot.clone();
                let runtime = runtime.clone();
                let client = client.clone();
                let mut draining = drain.subscribe();
                connections.spawn(async move {
                    let svc = service_fn(move |req| handle(snapshot.clone(), runtime.clone(), client.clone(), req, admin, otel, body_read_timeout));
                    let mut builder = http1::Builder::new();
                    builder.timer(TokioTimer::new()).header_read_timeout(header_read_timeout);
                    let connection = builder.serve_connection(TokioIo::new(stream), svc);
                    tokio::pin!(connection);
                    let result = tokio::select! {
                        result = &mut connection => result,
                        _ = draining.changed() => {
                            connection.as_mut().graceful_shutdown();
                            connection.await
                        }
                    };
                    if let Err(err) = result { tracing::debug!(%err, "connection error"); }
                });
            }
            Some(result) = connections.join_next(), if !connections.is_empty() => {
                if let Err(err) = result { tracing::warn!(%err, "connection task failed"); }
            }
            result = &mut shutdown => {
                result?;
                tracing::info!(connections = connections.len(), "stopping accepts and draining connections");
                break;
            }
        }
    }
    drop(listener);
    drop(admin_listener);
    let _ = drain.send(true);
    if tokio::time::timeout(shutdown_grace, async {
        while let Some(result) = connections.join_next().await {
            if let Err(err) = result {
                tracing::warn!(%err, "connection task failed during drain");
            }
        }
    })
    .await
    .is_err()
    {
        tracing::warn!(
            connections = connections.len(),
            "drain deadline exceeded; closing connections"
        );
        connections.abort_all();
        while connections.join_next().await.is_some() {}
    }
    tracing::info!("shutdown complete");
    Ok(())
}

async fn shutdown_signal() -> anyhow::Result<()> {
    #[cfg(unix)]
    {
        let mut terminate =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
                .context("installing SIGTERM handler")?;
        tokio::select! {
            result = tokio::signal::ctrl_c() => result.context("waiting for ctrl_c")?,
            _ = terminate.recv() => {},
        }
    }
    #[cfg(not(unix))]
    tokio::signal::ctrl_c()
        .await
        .context("waiting for ctrl_c")?;
    Ok(())
}

#[allow(clippy::too_many_arguments)]
async fn handle(
    snapshot: Snapshot,
    runtime: Arc<Runtime>,
    client: UpstreamClient,
    req: Request<Incoming>,
    admin: bool,
    otel: bool,
    body_read_timeout: Duration,
) -> Result<Response<ProxyBody>, Infallible> {
    let started = Instant::now();
    let routing_table = snapshot.load_full();
    let endpoint = metrics::endpoint_index(req.uri().path());
    let span = telemetry::request_span(otel, req.headers(), API_ENDPOINTS[endpoint]);
    let handles = match &routing_table {
        Some(routing_table) => routing_table.rejected[endpoint].clone(),
        None => runtime.metrics.cached_handles(
            HandleKey {
                team: 0,
                requested: 0,
                model: 0,
                endpoint: endpoint as u8,
            },
            "",
            "",
            "",
        ),
    };
    let mut observation = Observation::new(handles, span.clone(), otel);
    observation.started = started;
    let response = dispatch(
        routing_table.as_deref(),
        &runtime,
        &client,
        req,
        admin,
        &mut observation,
        endpoint,
        body_read_timeout,
    )
    .instrument(span)
    .await;
    Ok(observation.wrap(response))
}

fn admin_response(
    routing_table: Option<&RoutingTable>,
    runtime: &Runtime,
    req: &Request<Incoming>,
) -> Response<ProxyBody> {
    match (req.method(), req.uri().path()) {
        (&Method::GET, "/healthz") => Response::new(full_body(Bytes::from_static(b"ok"))),
        (&Method::GET, "/readyz") => match routing_table {
            Some(_) => Response::new(full_body(Bytes::from_static(b"ready"))),
            None => error::json(
                StatusCode::SERVICE_UNAVAILABLE,
                "not_ready",
                "No config snapshot loaded yet",
            ),
        },
        (&Method::GET, "/metrics") => match runtime.encode_metrics() {
            Ok(text) => Response::builder()
                .header(
                    header::CONTENT_TYPE,
                    "application/openmetrics-text; version=1.0.0; charset=utf-8",
                )
                .body(full_body(Bytes::from(text)))
                .expect("static response parts are valid"),
            Err(_) => error::json(
                StatusCode::INTERNAL_SERVER_ERROR,
                "metrics_error",
                "Could not encode metrics",
            ),
        },
        _ => error::json(StatusCode::NOT_FOUND, "not_found", "Not found"),
    }
}

/// A known key was rejected before the request reached a model: report it so
/// the control plane sees blocked traffic. Only runs on rejections.
fn rejected_event(
    routing_table: &RoutingTable,
    key: &KeyContext,
    request: &RequestCtx,
    endpoint: usize,
    status: StatusCode,
) -> UsageEvent {
    use lumewarp_proto::usage::v1::{RequestOutcome, UsageState};
    UsageEvent {
        request_id: request.request_id.to_string(),
        timestamp_unix_ms: request.started_at_unix_ms
            + request.started.elapsed().as_millis() as i64,
        key_id: key.key_id.to_string(),
        team_id: key.team_id.to_string(),
        // The body is not read before authentication.
        requested: String::new(),
        model: String::new(),
        endpoint: API_ENDPOINTS[endpoint].into(),
        status: status.as_u16().into(),
        stream: false,
        prompt_tokens: 0,
        completion_tokens: 0,
        duration_ms: request.started.elapsed().as_millis() as u32,
        config_version: routing_table.version,
        usage_state: UsageState::Missing as i32,
        outcome: RequestOutcome::Rejected as i32,
        started_at_unix_ms: request.started_at_unix_ms,
        cost_nano_usd: 0,
        cost_known: false,
    }
}

#[allow(clippy::too_many_arguments)]
async fn dispatch(
    routing_table: Option<&RoutingTable>,
    runtime: &Runtime,
    client: &UpstreamClient,
    req: Request<Incoming>,
    admin: bool,
    observation: &mut Observation,
    endpoint: usize,
    body_read_timeout: Duration,
) -> Response<ProxyBody> {
    if admin {
        return admin_response(routing_table, runtime, &req);
    }
    let path = req.uri().path();
    if !path.starts_with("/v1/") {
        return match (req.method(), path) {
            (&Method::GET, "/healthz") => Response::builder()
                .header(header::CONTENT_TYPE, "text/plain")
                .body(full_body(Bytes::from_static(b"ok")))
                .expect("static response parts are valid"),
            _ => error::json(StatusCode::NOT_FOUND, "not_found", "Not found"),
        };
    }
    // Checked before authentication: without a snapshot there are no keys,
    // and serving anything would mean an open routing_table.
    let Some(routing_table) = routing_table else {
        return error::json(
            StatusCode::SERVICE_UNAVAILABLE,
            "not_ready",
            "Gateway has no config snapshot yet",
        );
    };
    let request = RequestCtx::new(observation.started);
    let caller = match auth::authenticate(routing_table, req.headers()) {
        Ok(caller) => caller,
        Err(rejection) => {
            if let Some(key) = &rejection.key {
                runtime.usage.try_record(rejected_event(
                    routing_table,
                    key,
                    &request,
                    endpoint,
                    rejection.error.status(),
                ));
            }
            return rejection.into();
        }
    };
    // Authenticated traffic carries the caller's team even when it never
    // reaches a model (/v1/models, malformed bodies); failed auth stays "".
    if caller.team_label() != 0 {
        observation.handles = routing_table.caller_handles(&caller, endpoint);
    }
    match (req.method(), path) {
        (&Method::GET, "/v1/models") => Response::builder()
            .header(header::CONTENT_TYPE, "application/json")
            .body(full_body(routing_table.models_for(&caller)))
            .expect("static response parts are valid"),
        (&Method::POST, "/v1/chat/completions" | "/v1/completions" | "/v1/embeddings") => {
            proxy::forward(
                routing_table,
                client,
                req,
                observation,
                endpoint,
                caller,
                request,
                body_read_timeout,
            )
            .await
        }
        _ => error::json(StatusCode::NOT_FOUND, "not_found", "Not found"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::usage::UsageSink;
    use http_body_util::{BodyExt, Full};
    use hyper_util::{client::legacy::Client, rt::TokioExecutor};
    use lumewarp_proto::usage::v1::{RequestOutcome, UsageState};

    fn free_addr() -> std::net::SocketAddr {
        std::net::TcpListener::bind("127.0.0.1:0")
            .unwrap()
            .local_addr()
            .unwrap()
    }

    fn sha256(secret: &str) -> String {
        let digest = <sha2::Sha256 as sha2::Digest>::digest(secret.as_bytes());
        digest.iter().map(|b| format!("{b:02x}")).collect()
    }

    struct TestServer {
        settings: lumewarp_config::LocalSettings,
        client: Client<hyper_util::client::legacy::connect::HttpConnector, Full<Bytes>>,
        task: tokio::task::JoinHandle<anyhow::Result<()>>,
    }

    impl TestServer {
        fn start(snapshot: Snapshot, runtime: Arc<Runtime>) -> Self {
            let settings = lumewarp_config::LocalSettings {
                listen: free_addr(),
                admin_listen: free_addr(),
                shutdown_grace_ms: 1000,
                header_read_timeout_ms: 1000,
                body_read_timeout_ms: 1000,
                retry_budget_percent: 20,
            };
            let task = tokio::spawn({
                let settings = settings.clone();
                async move { serve(&settings, snapshot, runtime, false).await }
            });
            let client = Client::builder(TokioExecutor::new()).build_http();
            Self {
                settings,
                client,
                task,
            }
        }

        async fn get(&self, admin: bool, path: &str, auth: Option<&str>) -> (u16, Bytes) {
            let addr = if admin {
                self.settings.admin_listen
            } else {
                self.settings.listen
            };
            let mut request = Request::builder().uri(format!("http://{addr}{path}"));
            if let Some(auth) = auth {
                request = request.header(header::AUTHORIZATION, auth);
            }
            let request = request.body(Full::new(Bytes::new())).unwrap();
            for _ in 0..200 {
                match self.client.request(request.clone()).await {
                    Ok(response) => {
                        let status = response.status().as_u16();
                        let body = response.into_body().collect().await.unwrap().to_bytes();
                        return (status, body);
                    }
                    Err(_) => tokio::time::sleep(Duration::from_millis(10)).await,
                }
            }
            panic!("server did not start");
        }
    }

    impl Drop for TestServer {
        fn drop(&mut self) {
            self.task.abort();
        }
    }

    #[tokio::test]
    async fn cold_boot_is_not_ready_until_a_router_is_stored() {
        let (_, snapshot) = lumewarp_config::Config::parse(&format!(
            "routers: []\nupstreams: []\nkeys: [{{key_id: k, key_sha256: '{}', allow_all_models: true}}]",
            sha256("secret")
        ))
        .unwrap();
        let runtime = Runtime::new();
        let slot: Snapshot = Arc::new(ArcSwapOption::empty());
        assert!(!ready(&slot));
        let server = TestServer::start(slot.clone(), runtime.clone());
        assert_eq!(server.get(true, "/readyz", None).await.0, 503);
        assert_eq!(server.get(true, "/healthz", None).await.0, 200);
        assert_eq!(server.get(false, "/healthz", None).await.0, 200);
        let (status, body) = server.get(false, "/v1/models", None).await;
        assert_eq!(status, 503);
        assert!(String::from_utf8_lossy(&body).contains("not_ready"));
        // Even with what would become a valid key: never an open routing_table.
        let auth = Some("Bearer secret");
        assert_eq!(server.get(false, "/v1/models", auth).await.0, 503);
        let (status, metrics) = server.get(true, "/metrics", None).await;
        assert_eq!(status, 200);
        assert!(String::from_utf8_lossy(&metrics).contains(
            "lumewarp_requests_total{requested=\"\",endpoint=\"models\",model=\"\",team=\"\",status=\"503\"} 2"
        ));
        let routing_table =
            RoutingTable::compile(&snapshot, lumewarp_config::AuthMode::RequireKey, &runtime)
                .unwrap();
        slot.store(Some(Arc::new(routing_table)));
        assert!(ready(&slot));
        assert_eq!(server.get(true, "/readyz", None).await.0, 200);
        assert_eq!(server.get(false, "/v1/models", None).await.0, 401);
        assert_eq!(server.get(false, "/v1/models", auth).await.0, 200);
    }

    #[tokio::test]
    async fn known_key_rejections_emit_usage_events() {
        let key = |id: &str, extra: &str| {
            format!(
                "  - {{key_id: {id}, key_sha256: '{}', team_id: t1, allow_all_models: true{extra}}}\n",
                sha256(id)
            )
        };
        let yaml = format!(
            "routers: []\nupstreams: []\nteams: [{{id: t1}}]\nkeys:\n{}{}{}{}",
            key("ok", ""),
            key("revoked", ", status: revoked"),
            key("expired", ", expires_at_unix_ms: 1000"),
            key("budget", ", status: budget_exceeded"),
        );
        let (_, snapshot) = lumewarp_config::Config::parse(&yaml).unwrap();
        let (sink, mut events) = UsageSink::for_test();
        let runtime = Runtime::with_usage(Arc::new(crate::metrics::Metrics::new()), sink);
        let routing_table =
            RoutingTable::compile(&snapshot, lumewarp_config::AuthMode::RequireKey, &runtime)
                .unwrap();
        let version = routing_table.version;
        let slot: Snapshot = Arc::new(ArcSwapOption::from_pointee(routing_table));
        let server = TestServer::start(slot, runtime);

        // Unknown, missing and accepted keys report nothing here.
        assert_eq!(server.get(false, "/v1/models", None).await.0, 401);
        let unknown = server.get(false, "/v1/models", Some("Bearer nope")).await;
        assert_eq!(unknown.0, 401);
        assert_eq!(
            server.get(false, "/v1/models", Some("Bearer ok")).await.0,
            200
        );
        assert!(events.try_recv().is_err());

        for (secret, status) in [("revoked", 401), ("expired", 401), ("budget", 429)] {
            let before = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_millis() as i64;
            let auth = format!("Bearer {secret}");
            assert_eq!(
                server.get(false, "/v1/models", Some(&auth)).await.0,
                status,
                "{secret}"
            );
            let event = events.try_recv().expect("rejection event");
            assert!(events.try_recv().is_err(), "one event per request");
            assert_eq!(event.status, status as u32, "{secret}");
            assert_eq!(event.outcome, RequestOutcome::Rejected as i32);
            assert_eq!(event.usage_state, UsageState::Missing as i32);
            assert_eq!(event.key_id, secret);
            assert_eq!(event.team_id, "t1");
            assert_eq!(event.requested, "");
            assert_eq!(event.endpoint, "models");
            assert_eq!(event.config_version, version);
            assert_eq!((event.prompt_tokens, event.completion_tokens), (0, 0));
            let id = uuid::Uuid::parse_str(&event.request_id).unwrap();
            assert_eq!(id.get_version_num(), 7);
            assert!(event.started_at_unix_ms >= before - 5, "{secret}");
            assert!(event.timestamp_unix_ms >= event.started_at_unix_ms);
        }
    }
}
