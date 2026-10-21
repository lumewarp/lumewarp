use std::{convert::Infallible, path::PathBuf, sync::Arc};

use anyhow::Context;
use arc_swap::ArcSwap;
use bytes::Bytes;
use clap::Parser;
use http_body_util::Full;
use hyper::{
    body::Incoming, header, server::conn::http1, service::service_fn, Method, Request, Response,
    StatusCode,
};
use hyper_util::rt::TokioIo;
use lumewarp_config::Config;
use tokio::net::TcpListener;
use tracing_subscriber::EnvFilter;

#[derive(Parser)]
#[command(name = "lumewarp", version, about = "Lumewarp LLM gateway")]
struct Args {
    /// Standalone YAML config.
    #[arg(long, short, env = "LUMEWARP_CONFIG")]
    config: PathBuf,
}

type Snapshot = Arc<ArcSwap<Config>>;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()))
        .init();

    let args = Args::parse();
    let config = Config::load(&args.config)
        .with_context(|| format!("loading {}", args.config.display()))?;
    let listen = config.server.listen;
    let snapshot: Snapshot = Arc::new(ArcSwap::from_pointee(config));

    let listener = TcpListener::bind(listen)
        .await
        .with_context(|| format!("binding {listen}"))?;
    tracing::info!(%listen, "lumewarp gateway listening");

    let shutdown = tokio::signal::ctrl_c();
    tokio::pin!(shutdown);
    loop {
        tokio::select! {
            accepted = listener.accept() => {
                let (stream, _) = match accepted {
                    Ok(conn) => conn,
                    Err(err) => {
                        tracing::warn!(%err, "accept failed");
                        continue;
                    }
                };
                let _ = stream.set_nodelay(true);
                let snapshot = snapshot.clone();
                tokio::spawn(async move {
                    let svc = service_fn(move |req| handle(snapshot.clone(), req));
                    if let Err(err) = http1::Builder::new()
                        .serve_connection(TokioIo::new(stream), svc)
                        .await
                    {
                        tracing::debug!(%err, "connection error");
                    }
                });
            }
            _ = &mut shutdown => {
                tracing::info!("shutting down");
                return Ok(());
            }
        }
    }
}

async fn handle(
    snapshot: Snapshot,
    req: Request<Incoming>,
) -> Result<Response<Full<Bytes>>, Infallible> {
    let resp = match (req.method(), req.uri().path()) {
        (&Method::GET, "/healthz") => text(StatusCode::OK, "ok"),
        (&Method::GET, "/v1/models") => list_models(&snapshot.load()),
        // TODO(P0): /v1/chat/completions, /v1/completions, /v1/embeddings proxying with SSE.
        _ => text(StatusCode::NOT_FOUND, "not found"),
    };
    Ok(resp)
}

fn list_models(config: &Config) -> Response<Full<Bytes>> {
    let data: Vec<_> = config
        .routes
        .iter()
        .map(|r| {
            serde_json::json!({
                "id": r.model,
                "object": "model",
                "created": 0,
                "owned_by": "lumewarp",
            })
        })
        .collect();
    let body = serde_json::json!({ "object": "list", "data": data });
    Response::builder()
        .status(StatusCode::OK)
        .header(header::CONTENT_TYPE, "application/json")
        .body(Full::new(Bytes::from(body.to_string())))
        .expect("static response parts are valid")
}

fn text(status: StatusCode, body: &'static str) -> Response<Full<Bytes>> {
    Response::builder()
        .status(status)
        .header(header::CONTENT_TYPE, "text/plain")
        .body(Full::new(Bytes::from_static(body.as_bytes())))
        .expect("static response parts are valid")
}
