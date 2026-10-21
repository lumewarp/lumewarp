use std::{
    convert::Infallible,
    sync::Arc,
    time::{Duration, Instant},
};

use anyhow::Context;
use arc_swap::ArcSwap;
use bytes::Bytes;
use hyper::{
    body::Incoming, header, server::conn::http1, service::service_fn, Method, Request, Response,
    StatusCode,
};
use hyper_util::rt::{TokioIo, TokioTimer};
use tokio::{net::TcpListener, sync::watch, task::JoinSet};
use tracing::Instrument;

use crate::{
    error,
    metrics::{self, Observation, ROUTES},
    proxy::{self, full_body, ProxyBody, UpstreamClient},
    router::Router,
    telemetry,
};

pub type Snapshot = Arc<ArcSwap<Router>>;

pub async fn serve(
    config: &lumewarp_config::Server,
    snapshot: Snapshot,
    otel: bool,
) -> anyhow::Result<()> {
    let listen = config.listen;
    let admin_listen = config.admin_listen;
    let header_read_timeout = Duration::from_millis(config.header_read_timeout_ms);
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
                let client = client.clone();
                let mut draining = drain.subscribe();
                connections.spawn(async move {
                    let svc = service_fn(move |req| handle(snapshot.clone(), client.clone(), req, admin, otel));
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

async fn handle(
    snapshot: Snapshot,
    client: UpstreamClient,
    req: Request<Incoming>,
    admin: bool,
    otel: bool,
) -> Result<Response<ProxyBody>, Infallible> {
    let started = Instant::now();
    let router = snapshot.load_full();
    let endpoint = metrics::route_index(req.uri().path());
    let span = telemetry::request_span(otel, req.headers(), ROUTES[endpoint]);
    let mut observation = Observation::new(router.rejected[endpoint].clone(), span.clone(), otel);
    observation.started = started;
    let response = dispatch(&router, &client, req, admin, &mut observation, endpoint)
        .instrument(span)
        .await;
    Ok(observation.wrap(response))
}

async fn dispatch(
    router: &Router,
    client: &UpstreamClient,
    req: Request<Incoming>,
    admin: bool,
    observation: &mut Observation,
    endpoint: usize,
) -> Response<ProxyBody> {
    let path = req.uri().path();
    if admin {
        return match (req.method(), path) {
            (&Method::GET, "/healthz") => Response::new(full_body(Bytes::from_static(b"ok"))),
            (&Method::GET, "/metrics") => match router.metrics.encode() {
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
        };
    }
    if path.starts_with("/v1/") && !router.auth.allows(req.headers()) {
        return error::json(
            StatusCode::UNAUTHORIZED,
            "invalid_api_key",
            "Invalid or missing API key",
        );
    }
    let response = match (req.method(), path) {
        (&Method::GET, "/healthz") => Response::builder()
            .header(header::CONTENT_TYPE, "text/plain")
            .body(full_body(Bytes::from_static(b"ok")))
            .expect("static response parts are valid"),
        (&Method::GET, "/v1/models") => Response::builder()
            .header(header::CONTENT_TYPE, "application/json")
            .body(full_body(router.models.clone()))
            .expect("static response parts are valid"),
        (&Method::POST, "/v1/chat/completions" | "/v1/completions" | "/v1/embeddings") => {
            proxy::forward(router, client, req, observation, endpoint).await
        }
        _ => error::json(StatusCode::NOT_FOUND, "not_found", "Not found"),
    };
    response
}
