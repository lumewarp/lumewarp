use std::{
    convert::Infallible,
    sync::Arc,
    time::{Duration, Instant},
};

use anyhow::Context;
use arc_swap::{ArcSwap, ArcSwapOption};
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
    metrics::{self, HandleKey, Observation, ROUTES},
    proxy::{self, full_body, ProxyBody, UpstreamClient},
    router::Router,
    runtime::Runtime,
    telemetry,
};

/// Standalone handle: a router is always present.
pub type Snapshot = Arc<ArcSwap<Router>>;

/// The active router, absent until the first snapshot is applied (control-
/// plane cold boot without an on-disk snapshot). The config stream stores
/// into it; every request loads it exactly once.
pub type RouterSlot = Arc<ArcSwapOption<Router>>;

/// Readiness: a router is loaded. Without one, `/v1/*` returns 503
/// `not_ready` and the admin `/readyz` returns 503; nothing falls back to an
/// open router.
#[allow(dead_code)] // For the control-plane boot path (G2).
pub fn ready(slot: &RouterSlot) -> bool {
    slot.load().is_some()
}

/// Standalone entry point: serves a router that is always present.
pub async fn serve(
    config: &lumewarp_config::LocalSettings,
    snapshot: Snapshot,
    otel: bool,
) -> anyhow::Result<()> {
    let router = snapshot.load_full();
    let runtime = router.runtime.clone();
    let slot = Arc::new(ArcSwapOption::new(Some(router)));
    serve_with(config, slot, runtime, otel).await
}

/// Serves whatever router `slot` holds; `runtime` provides metrics while it
/// is empty. Routers stored in `slot` must share this `runtime`.
pub async fn serve_with(
    config: &lumewarp_config::LocalSettings,
    snapshot: RouterSlot,
    runtime: Arc<Runtime>,
    otel: bool,
) -> anyhow::Result<()> {
    let listen = config.listen;
    let admin_listen = config.admin_listen;
    let header_read_timeout = Duration::from_millis(config.header_read_timeout_ms);
    let body_read_timeout = Duration::from_millis(config.body_read_timeout_ms);
    let shutdown_grace = Duration::from_millis(config.shutdown_grace_ms);
    let client = proxy::client();
    let listener = TcpListener::bind(listen)
        .await
        .with_context(|| format!("binding {listen}"))?;
    let admin_listener = TcpListener::bind(admin_listen)
        .await
        .with_context(|| format!("binding admin {admin_listen}"))?;
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
    snapshot: RouterSlot,
    runtime: Arc<Runtime>,
    client: UpstreamClient,
    req: Request<Incoming>,
    admin: bool,
    otel: bool,
    body_read_timeout: Duration,
) -> Result<Response<ProxyBody>, Infallible> {
    let started = Instant::now();
    let router = snapshot.load_full();
    let endpoint = metrics::route_index(req.uri().path());
    let span = telemetry::request_span(otel, req.headers(), ROUTES[endpoint]);
    let handles = match &router {
        Some(router) => router.rejected[endpoint].clone(),
        None => runtime.metrics.cached_handles(
            HandleKey {
                team: 0,
                model: 0,
                deployment: 0,
                route: endpoint as u8,
            },
            "",
            "",
            "",
        ),
    };
    let mut observation = Observation::new(handles, span.clone(), otel);
    observation.started = started;
    let response = dispatch(
        router.as_deref(),
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
    router: Option<&Router>,
    runtime: &Runtime,
    req: &Request<Incoming>,
) -> Response<ProxyBody> {
    match (req.method(), req.uri().path()) {
        (&Method::GET, "/healthz") => Response::new(full_body(Bytes::from_static(b"ok"))),
        (&Method::GET, "/readyz") => match router {
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

#[allow(clippy::too_many_arguments)]
async fn dispatch(
    router: Option<&Router>,
    runtime: &Runtime,
    client: &UpstreamClient,
    req: Request<Incoming>,
    admin: bool,
    observation: &mut Observation,
    endpoint: usize,
    body_read_timeout: Duration,
) -> Response<ProxyBody> {
    if admin {
        return admin_response(router, runtime, &req);
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
    // and serving anything would mean an open router.
    let Some(router) = router else {
        return error::json(
            StatusCode::SERVICE_UNAVAILABLE,
            "not_ready",
            "Gateway has no config snapshot yet",
        );
    };
    let caller = match auth::authenticate(router, req.headers()) {
        Ok(caller) => caller,
        Err(err) => return err.into(),
    };
    // Authenticated traffic carries the caller's team even when it never
    // reaches a model (/v1/models, malformed bodies); failed auth stays "".
    if caller.team_label() != 0 {
        observation.handles = router.caller_handles(&caller, endpoint);
    }
    match (req.method(), path) {
        (&Method::GET, "/v1/models") => Response::builder()
            .header(header::CONTENT_TYPE, "application/json")
            .body(full_body(router.models_for(&caller)))
            .expect("static response parts are valid"),
        (&Method::POST, "/v1/chat/completions" | "/v1/completions" | "/v1/embeddings") => {
            proxy::forward(
                router,
                client,
                req,
                observation,
                endpoint,
                caller,
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
    use http_body_util::{BodyExt, Full};
    use hyper_util::{client::legacy::Client, rt::TokioExecutor};

    fn free_addr() -> std::net::SocketAddr {
        std::net::TcpListener::bind("127.0.0.1:0")
            .unwrap()
            .local_addr()
            .unwrap()
    }

    #[tokio::test]
    async fn cold_boot_is_not_ready_until_a_router_is_stored() {
        let (_, snapshot) = lumewarp_config::Config::parse(
            "routes: []\nupstreams: []\nkeys: [{key_id: k, key_sha256: '2bb80d537b1da3e38bd30361aa855686bde0eacd7162fef6a25fe97bf527a25b', allow_all_models: true}]",
        )
        .unwrap();
        let runtime = Runtime::new();
        let slot: RouterSlot = Arc::new(ArcSwapOption::empty());
        let settings = lumewarp_config::LocalSettings {
            listen: free_addr(),
            admin_listen: free_addr(),
            shutdown_grace_ms: 1000,
            header_read_timeout_ms: 1000,
            body_read_timeout_ms: 1000,
        };
        let server = tokio::spawn({
            let (slot, runtime, settings) = (slot.clone(), runtime.clone(), settings.clone());
            async move { serve_with(&settings, slot, runtime, false).await }
        });
        let client = Client::builder(TokioExecutor::new()).build_http::<Full<Bytes>>();
        let get = |addr: std::net::SocketAddr, path: &str, auth: Option<&str>| {
            let mut request = Request::builder().uri(format!("http://{addr}{path}"));
            if let Some(auth) = auth {
                request = request.header(header::AUTHORIZATION, auth);
            }
            let request = request.body(Full::new(Bytes::new())).unwrap();
            let client = client.clone();
            async move {
                for _ in 0..200 {
                    match client.request(request.clone()).await {
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
        };
        assert_eq!(get(settings.admin_listen, "/readyz", None).await.0, 503);
        assert_eq!(get(settings.admin_listen, "/healthz", None).await.0, 200);
        assert_eq!(get(settings.listen, "/healthz", None).await.0, 200);
        let (status, body) = get(settings.listen, "/v1/models", None).await;
        assert_eq!(status, 503);
        assert!(String::from_utf8_lossy(&body).contains("not_ready"));
        // Even with what would become a valid key: never an open router.
        let auth = Some("Bearer secret");
        assert_eq!(get(settings.listen, "/v1/models", auth).await.0, 503);
        let (status, metrics) = get(settings.admin_listen, "/metrics", None).await;
        assert_eq!(status, 200);
        assert!(String::from_utf8_lossy(&metrics).contains(
            "lumewarp_requests_total{model=\"\",route=\"models\",deployment=\"\",team=\"\",status=\"503\"} 2"
        ));
        let router =
            Router::compile(&snapshot, lumewarp_config::AuthMode::RequireKey, &runtime).unwrap();
        slot.store(Some(Arc::new(router)));
        assert!(ready(&slot));
        assert_eq!(get(settings.admin_listen, "/readyz", None).await.0, 200);
        assert_eq!(get(settings.listen, "/v1/models", None).await.0, 401);
        assert_eq!(get(settings.listen, "/v1/models", auth).await.0, 200);
        server.abort();
    }
}
