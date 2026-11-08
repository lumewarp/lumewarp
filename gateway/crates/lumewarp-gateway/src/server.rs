use std::{
    convert::Infallible,
    sync::Arc,
    time::{Duration, Instant},
};

use anyhow::Context;
use arc_swap::ArcSwapOption;
use bytes::Bytes;
use hyper::{
    body::Incoming,
    header::{self, HeaderName, HeaderValue},
    server::conn::http1,
    service::service_fn,
    Method, Request, Response, StatusCode,
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

const REQUEST_ID_HEADER: HeaderName = HeaderName::from_static("x-lumewarp-request-id");

/// The active routing table, absent until the first snapshot is applied (control-
/// plane cold boot without an on-disk snapshot). The config stream stores
/// into it; every request loads it exactly once, so a request in progress
/// keeps the routing table it started with across swaps.
pub type Snapshot = Arc<ArcSwapOption<RoutingTable>>;

/// Readiness: a routing table is loaded. Without one, `/v1/*` returns 503
/// `not_ready` (before authentication) and the admin `/readyz` returns 503;
/// nothing falls back to an open routing table.
#[cfg(test)]
pub fn ready(snapshot: &Snapshot) -> bool {
    snapshot.load().is_some()
}

/// Serves whatever routing table `snapshot` holds. `runtime` is the process-lifetime
/// state (metrics, limiters, usage sink), usable while no routing table is loaded;
/// every routing table stored in `snapshot` must be compiled with this `runtime`.
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
    // Card fetching is owned by the process, not by a request: an agent's card
    // exists (and reaches the control plane) before the first client asks for
    // it, in both standalone and control-plane mode. The supervisor follows
    // snapshot changes and reconciles one fetch task per agent.
    let card_fetcher = tokio::spawn(crate::a2a::supervise_cards(
        runtime.clone(),
        snapshot.clone(),
    ));
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
    card_fetcher.abort();
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
        ..Default::default()
    }
}

/// `x-lumewarp-request-id`: the usage event's `request_id`, the key to look a
/// request up in OTel. Not `x-request-id`, which upstreams set themselves.
fn request_id_header(request: &RequestCtx) -> HeaderValue {
    let mut buffer = [0u8; uuid::fmt::Hyphenated::LENGTH];
    HeaderValue::from_str(request.request_id.hyphenated().encode_lower(&mut buffer))
        .expect("a UUID is a valid header value")
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
    // `/v1/` first: the LLM path matches its prefix before any other route is
    // looked at, so the MCP/A2A routes below cost it nothing (no allocation,
    // no name parsing).
    let llm = req.uri().path().starts_with("/v1/");
    let request = RequestCtx::new(observation.started);
    // Version 0: nothing loaded yet.
    observation.identified(&request.request_id, routing_table.map_or(0, |t| t.version));
    let mut response = if llm {
        match routing_table {
            Some(routing_table) => {
                serve_api(
                    routing_table,
                    runtime,
                    client,
                    req,
                    observation,
                    endpoint,
                    request,
                    body_read_timeout,
                )
                .await
            }
            // Checked before authentication: without a snapshot there are no keys,
            // and serving anything would mean an open routing table. For the same
            // reason there is no usage event: the key is unidentified.
            None => error::json(
                StatusCode::SERVICE_UNAVAILABLE,
                "not_ready",
                "Gateway has no config snapshot yet",
            ),
        }
    } else {
        non_llm(
            routing_table,
            runtime,
            req,
            observation,
            &request,
            body_read_timeout,
        )
        .await
    };
    // Replaces whatever an upstream may have sent under this name.
    response
        .headers_mut()
        .insert(REQUEST_ID_HEADER, request_id_header(&request));
    response
}

/// Everything that is not `/v1/*`: the health probe, the MCP endpoints and the
/// A2A endpoints. Only MCP and A2A requests authenticate (with
/// `X-Lumewarp-Key`, `auth::authenticate_mcp`).
async fn non_llm(
    routing_table: Option<&RoutingTable>,
    runtime: &Runtime,
    req: Request<Incoming>,
    observation: &mut Observation,
    request: &RequestCtx,
    body_read_timeout: Duration,
) -> Response<ProxyBody> {
    if req.method() == Method::GET && req.uri().path() == "/healthz" {
        return Response::builder()
            .header(header::CONTENT_TYPE, "text/plain")
            .body(full_body(Bytes::from_static(b"ok")))
            .expect("static response parts are valid");
    }
    // One owned copy of the path so the route may borrow it while `req` is
    // moved into the handler. Only non-`/v1/*` requests get here.
    let path: String = req.uri().path().into();
    let Some(route) = endpoint(&path) else {
        return error::json(StatusCode::NOT_FOUND, "not_found", "Not found");
    };
    // Same rule as `/v1/*`: no snapshot, no keys, nothing served.
    let Some(routing_table) = routing_table else {
        return error::json(
            StatusCode::SERVICE_UNAVAILABLE,
            "not_ready",
            "Gateway has no config snapshot yet",
        );
    };
    let caller = match auth::authenticate_mcp(routing_table, req.headers()) {
        Ok(caller) => caller,
        Err(rejection) => {
            if let Some(key) = &rejection.key {
                observation.caller(&key.key_id, &key.team_id);
            }
            return rejection.into();
        }
    };
    if let auth::Caller::Key(key) = &caller {
        observation.caller(&key.key_id, &key.team_id);
    }
    let method = req.method().clone();
    match route {
        Endpoint::Mcp(mcp_route) => {
            // MCP and A2A requests count under the generic request metrics as
            // `endpoint="other"` until T4/T6 use T1b's `lumewarp_mcp_*` and
            // `lumewarp_a2a_*` families.
            crate::mcp::handle(crate::mcp::Call {
                route: mcp_route,
                method,
                http: req,
                caller: &caller,
                routing_table,
                targets: &routing_table.targets,
                runtime,
                request,
                body_read_timeout,
            })
            .await
        }
        Endpoint::A2a(a2a_route) => {
            crate::a2a::handle(crate::a2a::Call {
                route: a2a_route,
                method,
                http: req,
                caller: &caller,
                routing_table,
                targets: &routing_table.targets,
                runtime,
                request,
                body_read_timeout,
            })
            .await
        }
    }
}

enum Endpoint<'a> {
    Mcp(crate::mcp::Route<'a>),
    A2a(crate::a2a::Route<'a>),
}

/// Matches the MCP and A2A paths. All methods are accepted: the proxies decide
/// how a method is handled (MCP `GET` on a single server streams, A2A JSON-RPC
/// is a `POST`), and a wrong one is their error to answer.
fn endpoint(path: &str) -> Option<Endpoint<'_>> {
    if path == "/mcp" {
        return Some(Endpoint::Mcp(crate::mcp::Route::Aggregate));
    }
    if let Some(name) = path.strip_prefix("/mcp/servers/") {
        // Exactly one non-empty segment: `/mcp/servers/a/b` is not a route.
        if !name.is_empty() && !name.contains('/') {
            return Some(Endpoint::Mcp(crate::mcp::Route::Server { name }));
        }
        return None;
    }
    if path == "/a2a" {
        return Some(Endpoint::A2a(crate::a2a::Route::List));
    }
    if let Some(rest) = path.strip_prefix("/a2a/") {
        if let Some(agent) = rest.strip_suffix("/.well-known/agent-card.json") {
            if !agent.is_empty() && !agent.contains('/') {
                return Some(Endpoint::A2a(crate::a2a::Route::Card { agent }));
            }
            return None;
        }
        if !rest.is_empty() && !rest.contains('/') {
            return Some(Endpoint::A2a(crate::a2a::Route::Rpc { agent: rest }));
        }
    }
    None
}

#[allow(clippy::too_many_arguments)]
async fn serve_api(
    routing_table: &RoutingTable,
    runtime: &Runtime,
    client: &UpstreamClient,
    req: Request<Incoming>,
    observation: &mut Observation,
    endpoint: usize,
    request: RequestCtx,
    body_read_timeout: Duration,
) -> Response<ProxyBody> {
    let path = req.uri().path();
    let caller = match auth::authenticate(routing_table, req.headers()) {
        Ok(caller) => caller,
        Err(rejection) => {
            if let Some(key) = &rejection.key {
                observation.caller(&key.key_id, &key.team_id);
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
    if let auth::Caller::Key(key) = &caller {
        observation.caller(&key.key_id, &key.team_id);
    }
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
            let headers: Vec<(&str, &str)> = match auth {
                Some(auth) => vec![("authorization", auth)],
                None => Vec::new(),
            };
            let (status, _, body) = self.request(admin, "GET", path, &headers).await;
            (status, body)
        }

        async fn request(
            &self,
            admin: bool,
            method: &str,
            path: &str,
            headers: &[(&str, &str)],
        ) -> (u16, hyper::HeaderMap, Bytes) {
            let addr = if admin {
                self.settings.admin_listen
            } else {
                self.settings.listen
            };
            let mut request = Request::builder()
                .method(method)
                .uri(format!("http://{addr}{path}"));
            for (name, value) in headers {
                request = request.header(*name, *value);
            }
            let request = request.body(Full::new(Bytes::new())).unwrap();
            for _ in 0..200 {
                match self.client.request(request.clone()).await {
                    Ok(response) => {
                        let status = response.status().as_u16();
                        let headers = response.headers().clone();
                        let body = response.into_body().collect().await.unwrap().to_bytes();
                        return (status, headers, body);
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

    #[test]
    fn mcp_and_a2a_paths_match_whole_segments_only() {
        use crate::a2a::Route as A2a;
        use crate::mcp::Route as Mcp;
        assert!(matches!(
            endpoint("/mcp"),
            Some(Endpoint::Mcp(Mcp::Aggregate))
        ));
        assert!(matches!(
            endpoint("/mcp/servers/s1"),
            Some(Endpoint::Mcp(Mcp::Server { name: "s1" }))
        ));
        assert!(matches!(endpoint("/a2a"), Some(Endpoint::A2a(A2a::List))));
        assert!(matches!(
            endpoint("/a2a/a1"),
            Some(Endpoint::A2a(A2a::Rpc { agent: "a1" }))
        ));
        assert!(matches!(
            endpoint("/a2a/a1/.well-known/agent-card.json"),
            Some(Endpoint::A2a(A2a::Card { agent: "a1" }))
        ));
        for other in [
            "/v1/models",
            "/healthz",
            "/mcp/",
            "/mcp/other",
            "/mcp/servers",
            "/mcp/servers/",
            "/mcp/servers/a/b",
            "/a2a/",
            "/a2a/a1/x",
            "/a2a//x/agent-card.json",
            "/a2a/a1/.well-known/agent-card.json/extra",
        ] {
            assert!(endpoint(other).is_none(), "{other}");
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
        // Even with what would become a valid key: never an open routing table.
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

    /// The MCP and A2A routes take `X-Lumewarp-Key` only (L8), need a loaded
    /// snapshot like `/v1/*`, and reach the proxies with a request id. What the
    /// proxies answer is their own contract; this test is about routing and
    /// authentication.
    #[tokio::test]
    async fn mcp_and_a2a_routes_authenticate_then_reach_the_proxies() {
        let yaml = format!(
            "server:\n  api_keys: ['{}']\nmcp_servers:\n  - {{name: s1, url: 'http://127.0.0.1:1/mcp'}}\na2a_agents:\n  - {{name: a1, url: 'http://127.0.0.1:1/rpc'}}\n",
            sha256("secret")
        );
        let (_, snapshot) = lumewarp_config::Config::parse(&yaml).unwrap();
        let runtime = Runtime::with_usage_and_env(
            Arc::new(crate::metrics::Metrics::new()),
            crate::usage::UsageSink::disabled(),
            crate::runtime::EnvOptions {
                id_key: Some(crate::a2a_ids::IdKey::from_secret(vec![7u8; 32]).unwrap()),
                public_url: Some("https://gw.example.test".into()),
            },
        );
        let slot: Snapshot = Arc::new(ArcSwapOption::empty());
        let server = TestServer::start(slot.clone(), runtime.clone());
        let key = ("x-lumewarp-key", "secret");
        // Not ready before a snapshot, exactly like the LLM endpoints.
        assert_eq!(server.request(false, "POST", "/mcp", &[key]).await.0, 503);
        let routing_table = RoutingTable::compile(
            &snapshot,
            lumewarp_config::AuthMode::standalone(&snapshot),
            &runtime,
        )
        .unwrap();
        slot.store(Some(Arc::new(routing_table)));

        // Authentication (L8): one X-Lumewarp-Key; the LLM bearer is not it.
        for (method, path) in [
            ("POST", "/mcp"),
            ("POST", "/mcp/servers/s1"),
            ("GET", "/a2a"),
            ("GET", "/a2a/a1/.well-known/agent-card.json"),
            ("POST", "/a2a/a1"),
        ] {
            assert_eq!(
                server.request(false, method, path, &[]).await.0,
                401,
                "{method} {path} without a key"
            );
            assert_eq!(
                server
                    .request(false, method, path, &[("authorization", "Bearer secret")])
                    .await
                    .0,
                401,
                "{method} {path} with an LLM bearer"
            );
            let (status, headers, _) = server.request(false, method, path, &[key]).await;
            assert_ne!(status, 401, "{method} {path} with a valid key");
            assert!(
                headers.contains_key("x-lumewarp-request-id"),
                "{method} {path} has a request id"
            );
        }
        // A bare `POST /mcp` reaches the proxy's own protocol checks: the MCP
        // transport rejects a request that does not accept its response formats.
        assert_eq!(
            server.request(false, "POST", "/mcp", &[key]).await.0,
            406,
            "no Accept header"
        );
        // Unknown shapes are 404.
        for path in [
            "/mcp/other",
            "/mcp/servers/",
            "/mcp/servers/a/b",
            "/a2a/",
            "/a2a/a/b",
        ] {
            assert_eq!(
                server.request(false, "GET", path, &[key]).await.0,
                404,
                "{path}"
            );
        }
        // The LLM path is unchanged: an X-Lumewarp-Key alone does not
        // authenticate it, and MCP names are not models.
        assert_eq!(
            server.request(false, "GET", "/v1/models", &[key]).await.0,
            401
        );
        let (status, _, models) = server
            .request(
                false,
                "GET",
                "/v1/models",
                &[("authorization", "Bearer secret")],
            )
            .await;
        assert_eq!(status, 200);
        assert!(!String::from_utf8_lossy(&models).contains("s1"));
    }

    /// F2 (P3): without the process settings the endpoints cannot work. With an
    /// id key but no public URL only A2A is unavailable; the LLM path and MCP
    /// keep working.
    #[tokio::test]
    async fn a2a_is_503_without_its_process_settings() {
        let yaml = format!(
            "server:\n  api_keys: ['{}']\nmcp_servers:\n  - {{name: s1, url: 'http://127.0.0.1:1/mcp'}}\na2a_agents:\n  - {{name: a1, url: 'http://127.0.0.1:1/rpc'}}\n",
            sha256("secret")
        );
        let (_, snapshot) = lumewarp_config::Config::parse(&yaml).unwrap();
        let runtime = Runtime::with_usage_and_env(
            Arc::new(crate::metrics::Metrics::new()),
            crate::usage::UsageSink::disabled(),
            crate::runtime::EnvOptions {
                id_key: Some(crate::a2a_ids::IdKey::from_secret(vec![7u8; 32]).unwrap()),
                public_url: None,
            },
        );
        let routing_table = RoutingTable::compile(
            &snapshot,
            lumewarp_config::AuthMode::standalone(&snapshot),
            &runtime,
        )
        .unwrap();
        let slot: Snapshot = Arc::new(ArcSwapOption::from_pointee(routing_table));
        let server = TestServer::start(slot, runtime);
        let key = ("x-lumewarp-key", "secret");
        let a2a = server.request(false, "POST", "/a2a/a1", &[key]).await;
        assert_eq!(a2a.0, 503);
        assert!(String::from_utf8_lossy(&a2a.2).contains("a2a_not_configured"));
        assert_eq!(server.request(false, "GET", "/a2a", &[key]).await.0, 503);
        assert_eq!(
            server
                .request(false, "GET", "/a2a/a1/.well-known/agent-card.json", &[key])
                .await
                .0,
            503
        );
        // MCP (it has the id key) and the LLM path are unaffected.
        assert_ne!(server.request(false, "POST", "/mcp", &[key]).await.0, 503);
        assert_eq!(
            server
                .request(
                    false,
                    "GET",
                    "/v1/models",
                    &[("authorization", "Bearer secret")]
                )
                .await
                .0,
            200
        );

        // With no id key at all, MCP cannot seal sessions either; only the LLM
        // path is left.
        let bare = Runtime::new();
        let routing_table = RoutingTable::compile(
            &snapshot,
            lumewarp_config::AuthMode::standalone(&snapshot),
            &bare,
        )
        .unwrap();
        let slot: Snapshot = Arc::new(ArcSwapOption::from_pointee(routing_table));
        let server = TestServer::start(slot, bare);
        let mcp = server.request(false, "POST", "/mcp", &[key]).await;
        assert_eq!(mcp.0, 503);
        assert!(String::from_utf8_lossy(&mcp.2).contains("mcp_not_configured"));
        assert_eq!(server.request(false, "GET", "/a2a", &[key]).await.0, 503);
        assert_eq!(
            server
                .request(
                    false,
                    "GET",
                    "/v1/models",
                    &[("authorization", "Bearer secret")]
                )
                .await
                .0,
            200
        );
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
            assert_eq!(event.requested, "", "authentication comes before the body");
            assert_eq!(event.endpoint, "models");
            assert_eq!(event.config_version, version);
            assert_eq!((event.prompt_tokens, event.completion_tokens), (0, 0));
            assert_eq!(event.attempts, 0);
            let id = uuid::Uuid::parse_str(&event.request_id).unwrap();
            assert_eq!(id.get_version_num(), 7);
            assert!(event.started_at_unix_ms >= before - 5, "{secret}");
            assert!(event.timestamp_unix_ms >= event.started_at_unix_ms);
        }
    }
}
