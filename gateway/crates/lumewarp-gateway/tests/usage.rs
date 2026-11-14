//! Usage metering end to end: the gateway binary in control-plane mode against
//! an in-process fake control plane (ConfigService + UsageService, hand-written
//! tonic services since the proto crate only generates clients) and a scripted
//! upstream that controls every response frame.

mod common;
#[path = "common/model_types.rs"]
mod model_types;
#[path = "common/usage_cp.rs"]
mod usage_cp;

use std::{
    collections::VecDeque,
    convert::Infallible,
    io::{BufRead, BufReader},
    net::{SocketAddr, TcpListener as StdListener},
    path::PathBuf,
    pin::Pin,
    process::{Child, Command, Stdio},
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc, Mutex,
    },
    task::{Context, Poll},
    time::Duration,
};

use bytes::Bytes;
use common::{bounded, bytes, client, Prometheus, TestClient};
use http_body_util::{combinators::UnsyncBoxBody, BodyExt, Full};
use hyper::{
    body::{Body, Frame, Incoming},
    service::service_fn,
    Request, Response,
};
use hyper_util::rt::TokioIo;
use lumewarp_proto::{
    config::v1::{
        Alias, ApiKey, EffectivePolicy, KeyStatus, Model, Price, RateLimit, RateLimitScope, Router,
        RouterMember, Snapshot, StreamUsageMode, Team, Upstream as UpstreamConfig, UpstreamKind,
    },
    usage::v1::{RequestOutcome, UsageEvent, UsageState},
};
use serde_json::Value;
use sha2::{Digest, Sha256};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream},
    sync::mpsc,
    task::JoinHandle,
};
use usage_cp::FakeCp;

const SCOPE: &str = "usage-test";
const TOKEN_ENV: &str = "LUMEWARP_USAGE_TEST_TOKEN";
const KEY: &str = "lw-usage-key";
const LIMITED_KEY: &str = "lw-limited-key";

const CONTENT: &[u8] = b"data: {\"id\":\"c\",\"object\":\"chat.completion.chunk\",\"choices\":[{\"index\":0,\"delta\":{\"content\":\"hi\"},\"finish_reason\":null}],\"usage\":null}\n\n";
const FINISH: &[u8] = b"data: {\"id\":\"c\",\"object\":\"chat.completion.chunk\",\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"stop\"}],\"usage\":null}\n\n";
const USAGE: &[u8] = b"data: {\"id\":\"c\",\"object\":\"chat.completion.chunk\",\"choices\":[],\"usage\":{\"prompt_tokens\":11,\"completion_tokens\":7,\"total_tokens\":18}}\n\n";
const DONE: &[u8] = b"data: [DONE]\n\n";
const CHAT_JSON: &[u8] = br#"{"id":"c","object":"chat.completion","choices":[{"index":0,"message":{"role":"assistant","content":"hi"},"finish_reason":"stop"}],"usage":{"prompt_tokens":5,"completion_tokens":3,"total_tokens":8}}"#;
const NAMED_CHAT_JSON: &[u8] = br#"{"id":"c","object":"chat.completion","model":"actual","choices":[],"usage":{"prompt_tokens":5,"completion_tokens":3,"total_tokens":8}}"#;
const EMBEDDINGS_JSON: &[u8] = br#"{"object":"list","data":[{"object":"embedding","index":0,"embedding":[0.5]}],"model":"m","usage":{"prompt_tokens":8,"total_tokens":8}}"#;
/// $1 / Mtok input, $2 / Mtok output.
const PRICE: Price = Price {
    input_nano_usd_per_mtok: 1_000_000_000,
    output_nano_usd_per_mtok: 2_000_000_000,
    nano_usd_per_audio_second: 0,
};

// ---------------------------------------------------------------------------
// Scripted upstream
// ---------------------------------------------------------------------------

type Frames = mpsc::Receiver<Result<Bytes, std::io::Error>>;

struct Script {
    status: u16,
    headers: Vec<(&'static str, &'static str)>,
    frames: Frames,
    /// Headers are sent only after this fires (or its sender is dropped).
    gate: Option<tokio::sync::oneshot::Receiver<()>>,
}

/// A response body fed by the test; reports its own drop.
struct FrameBody {
    frames: Frames,
    dropped: Option<tokio::sync::oneshot::Sender<()>>,
}
impl Body for FrameBody {
    type Data = Bytes;
    type Error = std::io::Error;
    fn poll_frame(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
    ) -> Poll<Option<Result<Frame<Bytes>, std::io::Error>>> {
        self.frames
            .poll_recv(cx)
            .map(|frame| frame.map(|frame| frame.map(Frame::data)))
    }
}
impl Drop for FrameBody {
    fn drop(&mut self) {
        if let Some(dropped) = self.dropped.take() {
            let _ = dropped.send(());
        }
    }
}

struct Received {
    path: String,
    headers: hyper::HeaderMap,
    body: Bytes,
    /// Fires when the upstream response body is dropped (stream closed).
    closed: tokio::sync::oneshot::Receiver<()>,
}

struct Upstream {
    addr: SocketAddr,
    scripts: Arc<Mutex<VecDeque<Script>>>,
    received: mpsc::UnboundedReceiver<Received>,
    task: JoinHandle<()>,
}

impl Upstream {
    async fn start() -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let scripts: Arc<Mutex<VecDeque<Script>>> = Arc::default();
        let (tx, received) = mpsc::unbounded_channel();
        let queue = scripts.clone();
        let task = tokio::spawn(async move {
            loop {
                let (socket, _) = listener.accept().await.unwrap();
                let (queue, tx) = (queue.clone(), tx.clone());
                tokio::spawn(async move {
                    let service = service_fn(move |request: Request<Incoming>| {
                        let (queue, tx) = (queue.clone(), tx.clone());
                        async move {
                            let path = request.uri().path().to_owned();
                            let headers = request.headers().clone();
                            let body = request.into_body().collect().await.unwrap().to_bytes();
                            let script = queue
                                .lock()
                                .unwrap()
                                .pop_front()
                                .expect("a scripted upstream response");
                            let (dropped, closed) = tokio::sync::oneshot::channel();
                            let _ = tx.send(Received {
                                path,
                                headers,
                                body,
                                closed,
                            });
                            if let Some(gate) = script.gate {
                                let _ = gate.await;
                            }
                            let mut response = Response::builder().status(script.status);
                            for (name, value) in script.headers {
                                response = response.header(name, value);
                            }
                            let body = FrameBody {
                                frames: script.frames,
                                dropped: Some(dropped),
                            };
                            Ok::<_, Infallible>(
                                response
                                    .body(UnsyncBoxBody::new(body))
                                    .expect("valid scripted response"),
                            )
                        }
                    });
                    let _ = hyper::server::conn::http1::Builder::new()
                        .serve_connection(TokioIo::new(socket), service)
                        .await;
                });
            }
        });
        Self {
            addr,
            scripts,
            received,
            task,
        }
    }

    /// Queues a response whose frames are all known up front.
    fn respond(&self, status: u16, content_type: &'static str, frames: &[&[u8]]) {
        let (tx, rx) = mpsc::channel(frames.len().max(1));
        for frame in frames {
            tx.try_send(Ok(Bytes::copy_from_slice(frame))).unwrap();
        }
        self.push(status, vec![("content-type", content_type)], rx);
    }

    /// Queues a response whose frames the test sends later.
    fn respond_live(
        &self,
        content_type: &'static str,
    ) -> mpsc::Sender<Result<Bytes, std::io::Error>> {
        let (tx, rx) = mpsc::channel(16);
        self.push(200, vec![("content-type", content_type)], rx);
        tx
    }

    fn push(&self, status: u16, headers: Vec<(&'static str, &'static str)>, frames: Frames) {
        self.scripts.lock().unwrap().push_back(Script {
            status,
            headers,
            frames,
            gate: None,
        });
    }

    /// Queues a JSON response held back until the returned sender fires.
    fn respond_gated(&self) -> tokio::sync::oneshot::Sender<()> {
        let (open, gate) = tokio::sync::oneshot::channel();
        let (tx, rx) = mpsc::channel(1);
        tx.try_send(Ok(Bytes::from_static(CHAT_JSON))).unwrap();
        self.scripts.lock().unwrap().push_back(Script {
            status: 200,
            headers: vec![("content-type", "application/json")],
            frames: rx,
            gate: Some(gate),
        });
        open
    }

    async fn received(&mut self) -> Received {
        bounded(self.received.recv())
            .await
            .expect("upstream request")
    }
}

impl Drop for Upstream {
    fn drop(&mut self) {
        self.task.abort();
    }
}

// ---------------------------------------------------------------------------
// Gateway in control-plane mode
// ---------------------------------------------------------------------------

fn key(id: &str, secret: &str, models: &[&str], scopes: Vec<RateLimitScope>) -> ApiKey {
    ApiKey {
        key_id: id.into(),
        key_hash: Sha256::digest(secret.as_bytes()).to_vec(),
        team_id: "t1".into(),
        status: KeyStatus::Active as i32,
        expires_at_unix_ms: 0,
        policy: Some(EffectivePolicy {
            allow_all_models: false,
            allowed_models: models.iter().map(|m| m.to_string()).collect(),
            rate_limit_scopes: scopes,
            budget_window_end_unix_ms: 0,
            ..Default::default()
        }),
        ..Default::default()
    }
}

fn snapshot(upstream: SocketAddr) -> Snapshot {
    let dead = {
        let listener = StdListener::bind("127.0.0.1:0").unwrap();
        listener.local_addr().unwrap()
    };
    let config = |name: &str, addr: SocketAddr, mode: StreamUsageMode| UpstreamConfig {
        name: name.into(),
        kind: UpstreamKind::Vllm as i32,
        base_url: format!("http://{addr}/v1"),
        api_key_ref: String::new(),
        timeout_ms: 5000,
        stream_usage_mode: mode as i32,
        ..Default::default()
    };
    let model = |name: &str, upstream: &str, price: Option<Price>| Model {
        r#type: 1,
        name: name.into(),
        upstream: upstream.into(),
        upstream_model: "actual".into(),
        price,
        ..Default::default()
    };
    let router = |name: &str, model: &str| Router {
        name: name.into(),
        members: vec![RouterMember {
            model: model.into(),
            weight: 1,
            ..Default::default()
        }],
        strategy: "weighted".into(),
        ..Default::default()
    };
    Snapshot {
        mcp_servers: vec![],
        a2a_agents: vec![],
        version: 7,
        upstreams: vec![
            config("u", upstream, StreamUsageMode::Unspecified),
            config("passthrough", upstream, StreamUsageMode::Passthrough),
            config("dead", dead, StreamUsageMode::IncludeUsage),
        ],
        models: vec![
            model("d", "u", Some(PRICE)),
            model("dp", "passthrough", None),
            model("dd", "dead", Some(PRICE)),
        ],
        routers: vec![
            router("public", "d"),
            router("pass", "dp"),
            router("down", "dd"),
            router("secret", "d"),
        ],
        api_keys: vec![
            key("k1", KEY, &["public", "pass", "down"], Vec::new()),
            key(
                "k2",
                LIMITED_KEY,
                &["public"],
                vec![RateLimitScope {
                    id: "key:k2".into(),
                    limit: Some(RateLimit {
                        requests_per_minute: 1,
                        tokens_per_minute: 0,
                        max_parallel_requests: 0,
                    }),
                }],
            ),
        ],
        teams: vec![Team {
            id: "t1".into(),
            name: "Team one".into(),
        }],
        created_at_unix_ms: 1,
        aliases: vec![],
        required_capabilities: vec!["strategy:weighted".into()],
        scope: SCOPE.into(),
    }
}

static NEXT_DIR: AtomicU64 = AtomicU64::new(0);

struct Gateway {
    child: Child,
    addr: SocketAddr,
    admin: SocketAddr,
    http: TestClient,
    dir: PathBuf,
}

impl Gateway {
    async fn start(control_plane: SocketAddr) -> Self {
        let dir = std::env::temp_dir().join(format!(
            "lumewarp-usage-test-{}-{}",
            std::process::id(),
            NEXT_DIR.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let addr: SocketAddr = "127.0.0.1:0".parse().unwrap();
        let admin_addr: SocketAddr = "127.0.0.2:0".parse().unwrap();
        let local = dir.join("local.yaml");
        std::fs::write(
            &local,
            format!("server:\n  listen: '{addr}'\n  admin_listen: '{admin_addr}'\n"),
        )
        .unwrap();
        let mut child = Command::new(env!("CARGO_BIN_EXE_lumewarp"))
            .args(["--control-plane", &format!("http://{control_plane}")])
            .args(["--gateway-token-env", TOKEN_ENV])
            .arg("--state-dir")
            .arg(dir.join("state"))
            .args(["--scope", SCOPE])
            .arg("--local-config")
            .arg(&local)
            .env(TOKEN_ENV, "gw-secret")
            .env_remove("OTEL_EXPORTER_OTLP_ENDPOINT")
            .env(
                "RUST_LOG",
                "lumewarp::config_stream=info,lumewarp::server=info",
            )
            .env("NO_COLOR", "1")
            .stdout(Stdio::piped())
            .stderr(Stdio::from(
                std::fs::File::create(dir.join("stderr.log")).unwrap(),
            ))
            .spawn()
            .unwrap();
        let stdout = child.stdout.take().unwrap();
        let (ready_tx, ready_rx) = tokio::sync::oneshot::channel();
        std::thread::spawn(move || {
            let mut ready_tx = Some(ready_tx);
            let (mut applied, mut listening) = (false, false);
            let mut addresses = None;
            for line in BufReader::new(stdout).lines() {
                let Ok(line) = line else {
                    break;
                };
                applied |= line.contains("config applied");
                listening |= line.contains("lumewarp gateway listening");
                if line.contains("lumewarp gateway listening") {
                    let addr = line
                        .split_whitespace()
                        .find_map(|p| p.strip_prefix("listen="))
                        .unwrap()
                        .parse::<SocketAddr>()
                        .unwrap();
                    let admin = line
                        .split_whitespace()
                        .find_map(|p| p.strip_prefix("admin_listen="))
                        .unwrap()
                        .parse::<SocketAddr>()
                        .unwrap();
                    addresses = Some((addr, admin));
                }
                if applied && listening {
                    if let Some(tx) = ready_tx.take() {
                        let _ = tx.send(addresses.unwrap());
                    }
                }
            }
        });
        let mut gateway = Self {
            child,
            addr,
            admin: admin_addr,
            http: client(),
            dir,
        };
        let (addr, admin) = tokio::time::timeout(Duration::from_secs(10), ready_rx)
            .await
            .unwrap_or_else(|_| panic!("gateway config not applied: {}", gateway.logs()))
            .expect("gateway exited before config was applied");
        gateway.addr = addr;
        gateway.admin = admin;
        gateway
    }

    async fn post(&self, path: &str, secret: &str, body: &str) -> Response<Incoming> {
        self.post_with(path, secret, body, &[]).await
    }

    async fn post_with(
        &self,
        path: &str,
        secret: &str,
        body: &str,
        headers: &[(&str, &str)],
    ) -> Response<Incoming> {
        let mut request = Request::builder()
            .method("POST")
            .uri(format!("http://{}{path}", self.addr))
            .header("authorization", format!("Bearer {secret}"))
            .header("content-type", "application/json");
        for &(name, value) in headers {
            request = request.header(name, value);
        }
        let request = request
            .body(Full::new(Bytes::from(body.to_owned())))
            .unwrap();
        bounded(self.http.request(request))
            .await
            .expect("gateway response")
    }

    async fn metrics(&self) -> Prometheus {
        let request = Request::builder()
            .uri(format!("http://{}/metrics", self.admin))
            .body(Full::new(Bytes::new()))
            .unwrap();
        let response = bounded(self.http.request(request)).await.unwrap();
        Prometheus::parse(std::str::from_utf8(&bytes(response).await).unwrap())
    }

    fn logs(&self) -> String {
        std::fs::read_to_string(self.dir.join("stderr.log")).unwrap_or_default()
    }
}

impl Drop for Gateway {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

async fn setup() -> (FakeCp, Upstream, Gateway) {
    let upstream = Upstream::start().await;
    let cp = FakeCp::start(snapshot(upstream.addr)).await;
    let gateway = Gateway::start(cp.addr).await;
    (cp, upstream, gateway)
}

#[tokio::test]
async fn retried_request_reports_only_final_model_usage() {
    let mut upstream = Upstream::start().await;
    let mut snapshot = snapshot(upstream.addr);
    snapshot.required_capabilities.push("retry".into());
    let mut second = snapshot.models[0].clone();
    second.name = "second".into();
    snapshot.models.push(second);
    snapshot.routers[0].members.push(RouterMember {
        model: "second".into(),
        weight: 1,
        ..Default::default()
    });
    snapshot.routers[0].retry = Some(lumewarp_proto::config::v1::RetryPolicy::default());
    upstream.respond(503, "application/json", &[b"{\"error\":\"retry\"}"]);
    upstream.respond(200, "application/json", &[CHAT_JSON]);
    let mut cp = FakeCp::start(snapshot).await;
    let gateway = Gateway::start(cp.addr).await;
    let response = gateway
        .post("/v1/chat/completions", KEY, r#"{"model":"public"}"#)
        .await;
    assert_eq!(response.status(), 200);
    bytes(response).await;
    upstream.received().await;
    upstream.received().await;
    let event = cp.event().await;
    assert_eq!(event.requested, "public");
    assert_eq!((event.prompt_tokens, event.completion_tokens), (5, 3));
    assert!(["d", "second"].contains(&event.model.as_str()));
    let metrics = gateway.metrics().await;
    assert_eq!(metrics.sum("lumewarp_usage_capture_total", &[]), 1.);
    assert_eq!(
        metrics.sum("lumewarp_requests_total", &[("requested", "public")]),
        1.
    );
    assert_eq!(
        metrics.sum(
            "lumewarp_upstream_attempts_total",
            &[("model", &event.model), ("outcome", "success")]
        ),
        1.
    );
    assert_eq!(
        metrics.sum(
            "lumewarp_upstream_attempts_total",
            &[("model", &event.model), ("outcome", "retryable_status")]
        ),
        0.
    );
    assert_eq!(metrics.sum("lumewarp_in_flight_requests", &[]), 0.);
    assert!(cp.events.try_recv().is_err());
}

fn state(event: &UsageEvent) -> (RequestOutcome, UsageState) {
    (
        RequestOutcome::try_from(event.outcome).unwrap(),
        UsageState::try_from(event.usage_state).unwrap(),
    )
}

fn json(bytes: &[u8]) -> Value {
    serde_json::from_slice(bytes).unwrap()
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[tokio::test]
async fn injected_usage_is_stripped_from_the_stream_and_reported() {
    let (mut cp, mut upstream, gateway) = setup().await;
    // The usage event arrives split across frames, glued to its neighbours.
    let usage_split = USAGE.len() / 2;
    upstream.respond(
        200,
        "text/event-stream",
        &[
            CONTENT,
            &[FINISH, &USAGE[..usage_split]].concat(),
            &[&USAGE[usage_split..], DONE].concat(),
        ],
    );
    let body = r#"{"model":"public","stream":true,"messages":[{"role":"user","content":"hi"}],"extra_body":{"guided_choice":["a"]}}"#;
    let response = gateway.post("/v1/chat/completions", KEY, body).await;
    assert_eq!(response.status(), 200);
    let received = bytes(response).await;
    assert_eq!(
        received,
        [CONTENT, FINISH, DONE].concat(),
        "client did not ask for usage"
    );
    let sent = upstream.received().await;
    assert_eq!(sent.path, "/v1/chat/completions");
    assert_eq!(
        std::str::from_utf8(&sent.body).unwrap(),
        r#"{"stream_options":{"include_usage":true},"model":"actual","stream":true,"messages":[{"role":"user","content":"hi"}],"extra_body":{"guided_choice":["a"]}}"#
    );
    let event = cp.event().await;
    assert_eq!(
        state(&event),
        (RequestOutcome::Completed, UsageState::Reported)
    );
    let request_id = uuid::Uuid::parse_str(&event.request_id).unwrap();
    assert_eq!(request_id.get_version_num(), 7);
    assert_eq!((event.prompt_tokens, event.completion_tokens), (11, 7));
    // 11 * $1/Mtok + 7 * $2/Mtok = 25_000 nano-USD.
    assert_eq!((event.cost_nano_usd, event.cost_known), (25_000, true));
    assert_eq!(
        (event.key_id.as_str(), event.team_id.as_str()),
        ("k1", "t1")
    );
    assert_eq!(
        (
            event.requested.as_str(),
            event.model.as_str(),
            event.endpoint.as_str()
        ),
        ("public", "d", "chat_completions")
    );
    assert_eq!(
        (event.status, event.stream, event.config_version),
        (200, true, 7)
    );
    assert!(event.started_at_unix_ms > 0 && event.timestamp_unix_ms >= event.started_at_unix_ms);

    // The client asks for usage: forwarded untouched and metered once.
    upstream.respond(200, "text/event-stream", &[CONTENT, FINISH, USAGE, DONE]);
    let body = r#"{"model":"public","stream":true,"stream_options":{"include_usage":true}}"#;
    let response = gateway.post("/v1/chat/completions", KEY, body).await;
    assert_eq!(
        bytes(response).await,
        [CONTENT, FINISH, USAGE, DONE].concat()
    );
    assert_eq!(
        upstream.received().await.body,
        r#"{"model":"actual","stream":true,"stream_options":{"include_usage":true}}"#
    );
    let event = cp.event().await;
    assert_eq!(event.usage_state, UsageState::Reported as i32);
    assert_eq!((event.prompt_tokens, event.completion_tokens), (11, 7));

    // Passthrough upstream: no injection, no usage, reported MISSING.
    upstream.respond(200, "text/event-stream", &[CONTENT, DONE]);
    let body = r#"{"model":"pass","stream":true}"#;
    let response = gateway.post("/v1/chat/completions", KEY, body).await;
    assert_eq!(bytes(response).await, [CONTENT, DONE].concat());
    assert_eq!(
        upstream.received().await.body,
        r#"{"model":"actual","stream":true}"#
    );
    let event = cp.event().await;
    assert_eq!(
        state(&event),
        (RequestOutcome::Completed, UsageState::Missing)
    );
    assert_eq!((event.model.as_str(), event.cost_known), ("dp", false));
    cp.no_more_events().await;

    let metrics = gateway.metrics().await;
    let tokens = |direction| {
        metrics.value(
            "lumewarp_tokens_total",
            &[
                ("requested", "public"),
                ("team", "t1"),
                ("direction", direction),
            ],
        )
    };
    assert_eq!(tokens("input"), 22.0);
    assert_eq!(tokens("output"), 14.0);
    assert!((metrics.value("lumewarp_spend_total", &[("team", "t1")]) - 50e-6).abs() < 1e-12);
    assert_eq!(
        metrics.value("lumewarp_usage_capture_total", &[("state", "reported")]),
        2.0
    );
    assert_eq!(
        metrics.value("lumewarp_usage_capture_total", &[("state", "missing")]),
        1.0
    );
    assert_eq!(
        metrics.value("lumewarp_usage_events_reported_total", &[]),
        3.0
    );
    assert_eq!(
        metrics.value("lumewarp_usage_events_dropped_total", &[]),
        0.0
    );
}

#[tokio::test]
async fn cancelled_and_failed_streams_are_reported_once() {
    let (mut cp, mut upstream, gateway) = setup().await;

    // Client cancels mid-stream.
    let frames = upstream.respond_live("text/event-stream");
    frames.send(Ok(Bytes::from_static(CONTENT))).await.unwrap();
    let response = gateway
        .post(
            "/v1/chat/completions",
            KEY,
            r#"{"model":"public","stream":true}"#,
        )
        .await;
    let received = upstream.received().await;
    let mut body = response.into_body();
    let first = bounded(body.frame()).await.unwrap().unwrap();
    assert_eq!(first.into_data().unwrap(), CONTENT);
    drop(body);
    bounded(received.closed).await.unwrap();
    let event = cp.event().await;
    assert_eq!(
        state(&event),
        (RequestOutcome::ClientCancelled, UsageState::Partial)
    );
    assert_eq!((event.prompt_tokens, event.cost_known), (0, false));
    drop(frames);

    // Upstream body fails mid-stream.
    let frames = upstream.respond_live("text/event-stream");
    frames.send(Ok(Bytes::from_static(CONTENT))).await.unwrap();
    let response = gateway
        .post(
            "/v1/chat/completions",
            KEY,
            r#"{"model":"public","stream":true}"#,
        )
        .await;
    upstream.received().await;
    let mut body = response.into_body();
    let first = bounded(body.frame()).await.unwrap().unwrap();
    assert_eq!(first.into_data().unwrap(), CONTENT);
    // Fail only once the headers are out (otherwise it is a 502).
    frames
        .send(Err(std::io::Error::other("upstream broke")))
        .await
        .unwrap();
    let collected = bounded(body.collect()).await;
    assert!(collected.is_err(), "{:?}", collected.map(|c| c.to_bytes()));
    let event = cp.event().await;
    assert_eq!(
        state(&event),
        (RequestOutcome::UpstreamError, UsageState::Partial)
    );

    // Upstream answers 500.
    upstream.respond(
        500,
        "application/json",
        &[br#"{"error":{"message":"boom"}}"#],
    );
    let response = gateway
        .post("/v1/chat/completions", KEY, r#"{"model":"public"}"#)
        .await;
    assert_eq!(response.status(), 500);
    bytes(response).await;
    upstream.received().await;
    let event = cp.event().await;
    assert_eq!(
        state(&event),
        (RequestOutcome::UpstreamError, UsageState::Missing)
    );
    assert_eq!(event.status, 500);

    // Upstream unreachable.
    let response = gateway
        .post(
            "/v1/chat/completions",
            KEY,
            r#"{"model":"down","stream":true}"#,
        )
        .await;
    assert_eq!(response.status(), 502);
    let event = cp.event().await;
    assert_eq!(
        state(&event),
        (RequestOutcome::UpstreamError, UsageState::Missing)
    );
    assert_eq!((event.status, event.model.as_str()), (502, "dd"));
    cp.no_more_events().await;
    let metrics = gateway.metrics().await;
    assert_eq!(
        metrics.value("lumewarp_usage_capture_total", &[("state", "partial")]),
        2.0
    );
}

#[tokio::test]
async fn non_stream_and_embeddings_usage() {
    let mut upstream = Upstream::start().await;
    let mut s = snapshot(upstream.addr);
    let mut embedding = s.models[0].clone();
    embedding.name = "embedding".into();
    embedding.r#type = 2;
    embedding.price.as_mut().unwrap().output_nano_usd_per_mtok = 0;
    s.models.push(embedding);
    s.api_keys[0]
        .policy
        .as_mut()
        .unwrap()
        .allowed_models
        .push("embedding".into());
    s.required_capabilities.push("model_types".into());
    let mut cp = FakeCp::start(s).await;
    let gateway = Gateway::start(cp.addr).await;
    // Split mid-object: the tee reassembles it.
    upstream.respond(
        200,
        "application/json",
        &[&CHAT_JSON[..40], &CHAT_JSON[40..]],
    );
    let response = gateway
        .post(
            "/v1/chat/completions",
            KEY,
            r#"{"model":"public","stream":false}"#,
        )
        .await;
    assert_eq!(bytes(response).await, CHAT_JSON);
    assert_eq!(
        upstream.received().await.body,
        r#"{"model":"actual","stream":false}"#
    );
    let event = cp.event().await;
    assert_eq!(
        state(&event),
        (RequestOutcome::Completed, UsageState::Reported)
    );
    assert_eq!((event.prompt_tokens, event.completion_tokens), (5, 3));
    assert_eq!((event.cost_nano_usd, event.stream), (11_000, false));

    upstream.respond(200, "application/json", &[EMBEDDINGS_JSON]);
    let response = gateway
        .post(
            "/v1/embeddings",
            KEY,
            r#"{"model":"embedding","input":"hi"}"#,
        )
        .await;
    assert_eq!(json(&bytes(response).await)["object"], "list");
    assert_eq!(upstream.received().await.path, "/v1/embeddings");
    let event = cp.event().await;
    assert_eq!(event.usage_state, UsageState::Reported as i32);
    assert_eq!(
        (
            event.prompt_tokens,
            event.completion_tokens,
            event.endpoint.as_str()
        ),
        (8, 0, "embeddings")
    );
    assert_eq!(event.cost_nano_usd, 8_000);

    // Compressed bodies are not decompressed: usage INVALID, cost unknown.
    let (tx, rx) = mpsc::channel(1);
    tx.try_send(Ok(Bytes::from_static(b"\x1f\x8b not really gzip")))
        .unwrap();
    drop(tx);
    upstream.push(
        200,
        vec![
            ("content-type", "application/json"),
            ("content-encoding", "gzip"),
        ],
        rx,
    );
    let response = gateway
        .post("/v1/completions", KEY, r#"{"model":"public","prompt":"x"}"#)
        .await;
    assert_eq!(response.headers()["content-encoding"], "gzip");
    bytes(response).await;
    upstream.received().await;
    let event = cp.event().await;
    assert_eq!(
        state(&event),
        (RequestOutcome::Completed, UsageState::Invalid)
    );
    assert!(!event.cost_known);

    // Missing usage in a 200 JSON body.
    upstream.respond(200, "application/json", &[br#"{"id":"x","choices":[]}"#]);
    let response = gateway
        .post("/v1/completions", KEY, r#"{"model":"public","prompt":"x"}"#)
        .await;
    bytes(response).await;
    upstream.received().await;
    assert_eq!(cp.event().await.usage_state, UsageState::Missing as i32);
}

#[tokio::test]
async fn keyed_rejections_are_reported_without_spam() {
    let (mut cp, mut upstream, gateway) = setup().await;
    // 403: alias known but not allowed for this key.
    let response = gateway
        .post("/v1/chat/completions", KEY, r#"{"model":"secret"}"#)
        .await;
    assert_eq!(response.status(), 403);
    let event = cp.event().await;
    assert_eq!(
        state(&event),
        (RequestOutcome::Rejected, UsageState::Missing)
    );
    assert_eq!(
        (event.status, event.requested.as_str(), event.model.as_str()),
        (403, "secret", "")
    );
    // A name that does not exist is what the owner of the key looks for: it is
    // kept in the event, bounded. It stays out of the metric labels.
    let response = gateway
        .post("/v1/chat/completions", KEY, r#"{"model":"client-made-up"}"#)
        .await;
    assert_eq!(response.status(), 403);
    assert_eq!(cp.event().await.requested, "client-made-up");
    let long = "n".repeat(1000);
    let response = gateway
        .post(
            "/v1/chat/completions",
            KEY,
            &format!(r#"{{"model":"{long}"}}"#),
        )
        .await;
    assert_eq!(response.status(), 403);
    assert_eq!(cp.event().await.requested, "n".repeat(400));
    assert_eq!(
        gateway.metrics().await.sum(
            "lumewarp_requests_total",
            &[("requested", "client-made-up")]
        ),
        0.
    );

    // 429: the second request in the minute.
    upstream.respond(200, "application/json", &[CHAT_JSON]);
    let response = gateway
        .post("/v1/chat/completions", LIMITED_KEY, r#"{"model":"public"}"#)
        .await;
    assert_eq!(response.status(), 200);
    bytes(response).await;
    upstream.received().await;
    assert_eq!(cp.event().await.key_id, "k2");
    let response = gateway
        .post("/v1/chat/completions", LIMITED_KEY, r#"{"model":"public"}"#)
        .await;
    assert_eq!(response.status(), 429);
    let event = cp.event().await;
    assert_eq!(
        (event.key_id.as_str(), event.status, event.outcome),
        ("k2", 429, RequestOutcome::Rejected as i32)
    );

    // Unknown keys and malformed bodies produce no events.
    let response = gateway
        .post(
            "/v1/chat/completions",
            "lw-unknown",
            r#"{"model":"public"}"#,
        )
        .await;
    assert_eq!(response.status(), 401);
    let response = gateway
        .post(
            "/v1/chat/completions",
            KEY,
            r#"{"model":"public","stream_options":1}"#,
        )
        .await;
    assert_eq!(response.status(), 400);
    cp.no_more_events().await;
}

/// These keys are refused by what the snapshot says about them, from the headers alone. The
/// body is never sent here: an answer proves it was not read, so no called name can be known,
/// and the event is still reported for the key's owner.
#[tokio::test]
async fn keys_refused_before_the_body_report_an_event_with_no_name() {
    let mut upstream = Upstream::start().await;
    let mut snapshot = snapshot(upstream.addr);
    let refused = |id: &str, secret: &str, change: &dyn Fn(&mut ApiKey)| {
        let mut key = key(id, secret, &["public"], vec![]);
        change(&mut key);
        key
    };
    snapshot.api_keys.extend([
        refused("expired", "lw-expired", &|k| k.expires_at_unix_ms = 1),
        refused("suspended", "lw-suspended", &|k| {
            k.status = KeyStatus::Suspended as i32
        }),
        refused("revoked", "lw-revoked", &|k| {
            k.status = KeyStatus::Revoked as i32
        }),
        refused("budget", "lw-budget", &|k| {
            k.status = KeyStatus::BudgetExceeded as i32
        }),
    ]);
    let mut cp = FakeCp::start(snapshot).await;
    let gateway = Gateway::start(cp.addr).await;

    for (id, secret, status) in [
        ("expired", "lw-expired", 401),
        ("suspended", "lw-suspended", 401),
        ("revoked", "lw-revoked", 401),
        ("budget", "lw-budget", 429),
    ] {
        let mut socket = TcpStream::connect(gateway.addr).await.unwrap();
        socket
            .write_all(
                format!(
                    "POST /v1/chat/completions HTTP/1.1\r\nhost: gw\r\n\
                     authorization: Bearer {secret}\r\ncontent-type: application/json\r\n\
                     content-length: 1000\r\n\r\n"
                )
                .as_bytes(),
            )
            .await
            .unwrap();
        let mut head = Vec::new();
        let mut chunk = [0u8; 1024];
        while !head.windows(4).any(|w| w == b"\r\n\r\n") {
            let read = bounded(socket.read(&mut chunk)).await.unwrap();
            assert_ne!(read, 0, "{id}: closed without an answer");
            head.extend_from_slice(&chunk[..read]);
        }
        let head = String::from_utf8_lossy(&head);
        assert!(
            head.starts_with(&format!("HTTP/1.1 {status} ")),
            "{id}: {head}"
        );
        let event = cp.event().await;
        assert_eq!(event.key_id, id);
        assert_eq!(event.status, status);
        assert_eq!(event.outcome, RequestOutcome::Rejected as i32, "{id}");
        assert_eq!(event.requested, "", "{id}: the body was never read");
        assert_eq!(event.model, "", "{id}");
        assert_eq!(event.endpoint, "chat_completions", "{id}");
        assert_eq!(event.attempts, 0, "{id}");
    }
    cp.no_more_events().await;
    assert!(
        upstream.received.try_recv().is_err(),
        "nothing went upstream"
    );
}

#[tokio::test]
async fn report_retries_until_the_control_plane_accepts() {
    let (mut cp, mut upstream, gateway) = setup().await;
    cp.state.fail.store(2, Ordering::SeqCst);
    upstream.respond(200, "application/json", &[CHAT_JSON]);
    let response = gateway
        .post("/v1/chat/completions", KEY, r#"{"model":"public"}"#)
        .await;
    bytes(response).await;
    upstream.received().await;
    let event = cp.event().await;
    assert_eq!(event.usage_state, UsageState::Reported as i32);
    assert_eq!(
        cp.state.fail.load(Ordering::SeqCst),
        0,
        "both failures retried"
    );
    cp.no_more_events().await;
    assert_eq!(cp.state.reports.load(Ordering::SeqCst), 1, "delivered once");
    let metrics = gateway.metrics().await;
    assert_eq!(
        metrics.value("lumewarp_usage_events_failed_total", &[]),
        2.0
    );
    assert_eq!(
        metrics.value("lumewarp_usage_events_reported_total", &[]),
        1.0
    );
}

#[tokio::test]
async fn client_abort_before_upstream_headers_is_reported_once() {
    use tokio::io::AsyncWriteExt;
    let (mut cp, mut upstream, gateway) = setup().await;
    let open = upstream.respond_gated();
    let body = r#"{"model":"public","stream":true}"#;
    let mut socket = tokio::net::TcpStream::connect(gateway.addr).await.unwrap();
    socket
        .write_all(
            format!(
                "POST /v1/chat/completions HTTP/1.1\r\nhost: gw\r\nauthorization: Bearer {KEY}\r\ncontent-type: application/json\r\ncontent-length: {}\r\n\r\n{body}",
                body.len()
            )
            .as_bytes(),
        )
        .await
        .unwrap();
    let received = upstream.received().await;
    // The upstream holds its headers; the client goes away (TCP close).
    drop(socket);
    let event = cp.event().await;
    assert_eq!(
        state(&event),
        (RequestOutcome::ClientCancelled, UsageState::Missing)
    );
    assert_eq!(
        (event.status, event.requested.as_str(), event.model.as_str()),
        (499, "public", "d")
    );
    assert!(!event.cost_known);
    // Releasing the upstream afterwards must not produce a second event.
    let _ = open.send(());
    let _ = bounded(received.closed).await;
    cp.no_more_events().await;
}

#[tokio::test]
async fn compressed_streams_pass_through_unparsed() {
    let (mut cp, mut upstream, gateway) = setup().await;
    // Not real gzip: what matters is that the bytes are never parsed or
    // filtered, even though they look like a usage-only event.
    let encoded: &[u8] = &[CONTENT, USAGE, DONE].concat();
    let (tx, rx) = mpsc::channel(2);
    tx.try_send(Ok(Bytes::copy_from_slice(&encoded[..30])))
        .unwrap();
    tx.try_send(Ok(Bytes::copy_from_slice(&encoded[30..])))
        .unwrap();
    drop(tx);
    upstream.push(
        200,
        vec![
            ("content-type", "text/event-stream"),
            ("content-encoding", "gzip"),
        ],
        rx,
    );
    let response = gateway
        .post(
            "/v1/chat/completions",
            KEY,
            r#"{"model":"public","stream":true}"#,
        )
        .await;
    assert_eq!(response.headers()["content-encoding"], "gzip");
    assert_eq!(bytes(response).await, encoded);
    upstream.received().await;
    let event = cp.event().await;
    assert_eq!(
        state(&event),
        (RequestOutcome::Completed, UsageState::Invalid)
    );
    assert_eq!((event.prompt_tokens, event.cost_known), (0, false));
}

/// `flaky` always fails its only member, the model on the dead upstream, and
/// falls back to `d`, which is the live one.
async fn catalog_setup() -> (FakeCp, Upstream, Gateway) {
    catalog_setup_kind(1).await
}

async fn catalog_setup_kind(kind: i32) -> (FakeCp, Upstream, Gateway) {
    let upstream = Upstream::start().await;
    let mut snapshot = snapshot(upstream.addr);
    snapshot.aliases = vec![
        Alias {
            name: "to-model".into(),
            target: "d".into(),
        },
        Alias {
            name: "to-router".into(),
            target: "public".into(),
        },
        Alias {
            name: "to-flaky".into(),
            target: "flaky".into(),
        },
    ];
    // Called by the same name as its upstream_model: the fast path.
    let mut same = snapshot.models[0].clone();
    same.name = "actual".into();
    snapshot.models.push(same);
    let mut flaky = snapshot.routers[0].clone();
    flaky.name = "flaky".into();
    flaky.members[0].model = "dd".into();
    flaky.retry = Some(lumewarp_proto::config::v1::RetryPolicy {
        max_attempts: 2,
        ..Default::default()
    });
    flaky.fallbacks = vec!["d".into()];
    snapshot.routers.push(flaky);
    snapshot
        .required_capabilities
        .extend(["retry".into(), "fallbacks".into()]);
    snapshot.api_keys[0].policy.as_mut().unwrap().allowed_models = [
        "public",
        "d",
        "dd",
        "to-model",
        "to-router",
        "to-flaky",
        "flaky",
        "actual",
    ]
    .map(String::from)
    .to_vec();
    for model in &mut snapshot.models {
        model.r#type = kind;
        if kind == 2 {
            if let Some(price) = &mut model.price {
                price.output_nano_usd_per_mtok = 0;
            }
        }
    }
    if kind != 1 {
        snapshot.required_capabilities.push("model_types".into());
    }
    let cp = FakeCp::start(snapshot).await;
    let gateway = Gateway::start(cp.addr).await;
    (cp, upstream, gateway)
}

#[tokio::test]
async fn usage_event_for_each_way_of_calling_a_name() {
    let (mut cp, mut upstream, gateway) = catalog_setup().await;
    // called, alias, router, final model, upstream, fallback, attempts
    let rows: &[(&str, &str, &str, &str, &str, bool, u32)] = &[
        ("d", "", "", "d", "u", false, 1),
        ("to-model", "to-model", "", "d", "u", false, 1),
        ("public", "", "public", "d", "u", false, 1),
        ("to-router", "to-router", "public", "d", "u", false, 1),
        ("to-flaky", "to-flaky", "flaky", "d", "u", true, 2),
    ];
    for &(called, alias, router, model, upstream_name, fallback, attempts) in rows {
        upstream.respond(200, "application/json", &[NAMED_CHAT_JSON]);
        let response = gateway
            .post(
                "/v1/chat/completions",
                KEY,
                &format!(r#"{{"model":"{called}"}}"#),
            )
            .await;
        assert_eq!(response.status(), 200, "{called}");
        let header = response.headers()["x-lumewarp-request-id"]
            .to_str()
            .unwrap()
            .to_owned();
        let body = json(&bytes(response).await);
        let event = cp.event().await;
        assert_eq!(event.requested, called);
        assert_eq!(event.alias, alias, "{called}");
        assert_eq!(event.router, router, "{called}");
        assert_eq!(event.model, model, "{called}");
        assert_eq!(event.upstream, upstream_name, "{called}");
        assert_eq!(event.upstream_model, "actual", "{called}");
        assert_eq!(event.fallback, fallback, "{called}");
        assert_eq!(event.attempts, attempts, "{called}");
        assert_eq!(event.request_id, header, "{called}");
        assert_eq!(body["model"], called, "the client sees the name it called");
        // The body reaches the upstream with its upstream_model.
        let received = upstream.received().await;
        assert_eq!(json(&received.body)["model"], "actual");
    }
    let metrics = gateway.metrics().await;
    assert_eq!(
        metrics.sum(
            "lumewarp_fallbacks_total",
            &[("router", "flaky"), ("model", "d")]
        ),
        1.
    );
    assert_eq!(
        metrics.sum(
            "lumewarp_requests_total",
            &[("requested", "to-flaky"), ("model", "d"), ("status", "200")]
        ),
        1.
    );
}

#[tokio::test]
async fn direct_model_is_one_attempt_even_when_a_router_retries_it() {
    let (mut cp, _upstream, gateway) = catalog_setup().await;
    let response = gateway
        .post("/v1/chat/completions", KEY, r#"{"model":"dd"}"#)
        .await;
    assert_eq!(response.status(), 502);
    bytes(response).await;
    let event = cp.event().await;
    assert_eq!(
        (event.requested.as_str(), event.model.as_str()),
        ("dd", "dd")
    );
    assert_eq!((event.alias.as_str(), event.router.as_str()), ("", ""));
    assert_eq!((event.attempts, event.fallback), (1, false));
    let metrics = gateway.metrics().await;
    assert_eq!(
        metrics.sum("lumewarp_upstream_attempts_total", &[("model", "dd")]),
        1.
    );
    assert_eq!(metrics.sum("lumewarp_fallbacks_total", &[]), 0.);
}

#[tokio::test]
async fn a_grant_covers_the_called_name_only() {
    let upstream = Upstream::start().await;
    let mut snapshot = snapshot(upstream.addr);
    snapshot.aliases = vec![
        Alias {
            name: "nick".into(),
            target: "d".into(),
        },
        Alias {
            name: "to-public".into(),
            target: "public".into(),
        },
    ];
    // k1 holds a router and an alias, and none of what they point to by name.
    snapshot.api_keys[0].policy.as_mut().unwrap().allowed_models =
        vec!["public".into(), "nick".into()];
    let mut all = key("k3", "lw-all", &[], Vec::new());
    all.policy.as_mut().unwrap().allow_all_models = true;
    snapshot.api_keys.push(all);
    let mut cp = FakeCp::start(snapshot).await;
    let gateway = Gateway::start(cp.addr).await;
    for (secret, name, want) in [
        (KEY, "d", 403),
        (KEY, "to-public", 403),
        (KEY, "dd", 403),
        (KEY, "nope", 403),
        ("lw-all", "nope", 404),
    ] {
        let response = gateway
            .post(
                "/v1/chat/completions",
                secret,
                &format!(r#"{{"model":"{name}"}}"#),
            )
            .await;
        assert_eq!(response.status(), want, "{name}");
        assert!(response.headers().contains_key("x-lumewarp-request-id"));
        bytes(response).await;
        let event = cp.event().await;
        assert_eq!(event.requested, name, "named even on rejection");
        assert_eq!(event.model, "", "{name}");
        assert_eq!(event.status as u16, want, "{name}");
        assert_eq!(event.attempts, 0, "{name}");
        assert_eq!(state(&event).0, RequestOutcome::Rejected, "{name}");
    }
    // Provenance of a known name is known on rejection too.
    let response = gateway
        .post("/v1/chat/completions", KEY, r#"{"model":"to-public"}"#)
        .await;
    bytes(response).await;
    let event = cp.event().await;
    assert_eq!(
        (event.alias.as_str(), event.router.as_str()),
        ("to-public", "public")
    );

    // Unknown names stay out of the metric labels, whatever the client sends.
    let metrics = gateway.metrics().await;
    assert_eq!(
        metrics.sum("lumewarp_requests_total", &[("requested", "nope")]),
        0.
    );
    assert_eq!(
        metrics.sum("lumewarp_requests_total", &[("requested", "unknown")]),
        2.
    );

    // /v1/models lists exactly what the key can call.
    let request = Request::builder()
        .uri(format!("http://{}/v1/models", gateway.addr))
        .header("authorization", format!("Bearer {KEY}"))
        .body(Full::new(Bytes::new()))
        .unwrap();
    let body = json(&bytes(bounded(gateway.http.request(request)).await.unwrap()).await);
    let ids: Vec<_> = body["data"]
        .as_array()
        .unwrap()
        .iter()
        .map(|m| m["id"].as_str().unwrap())
        .collect();
    assert_eq!(ids, ["nick", "public"]);
}

// ---------------------------------------------------------------------------
// Response `model` rewrite
// ---------------------------------------------------------------------------

/// Every way of calling a name that needs the rewrite.
const REWRITTEN: [&str; 5] = ["d", "to-model", "public", "to-router", "to-flaky"];
const STREAM_HEAD: &[u8] =
    b"data: {\"id\":\"c\",\"object\":\"chat.completion.chunk\",\"model\":\"act";
const STREAM_TAIL: &[u8] = b"ual\",\"choices\":[{\"delta\":{\"content\":\"hi\"}}],\"usage\":null}\r\n\r\n: ping\r\n\r\ndata: {\"model\":\"actual\",\"choices\":[],\"usage\":{\"prompt_tokens\":11,\"completion_tokens\":7,\"total_tokens\":18}}\n\ndata: [DONE]\n\n";

fn sized_headers(
    content_type: &'static str,
    body: &[u8],
    extra: &[(&'static str, &'static str)],
) -> Vec<(&'static str, &'static str)> {
    let length: &'static str = body.len().to_string().leak();
    let mut headers = vec![("content-type", content_type), ("content-length", length)];
    headers.extend_from_slice(extra);
    headers
}

fn frames(chunks: &[&[u8]]) -> Frames {
    let (tx, rx) = mpsc::channel(chunks.len().max(1));
    for chunk in chunks {
        tx.try_send(Ok(Bytes::copy_from_slice(chunk))).unwrap();
    }
    rx
}

#[tokio::test]
async fn non_stream_responses_carry_the_called_name_and_are_chunked() {
    let (mut cp, mut upstream, gateway) = catalog_setup().await;
    for called in REWRITTEN {
        upstream.push(
            200,
            sized_headers("application/json", NAMED_CHAT_JSON, &[]),
            frames(&[NAMED_CHAT_JSON]),
        );
        let response = gateway
            .post(
                "/v1/chat/completions",
                KEY,
                &format!(r#"{{"model":"{called}"}}"#),
            )
            .await;
        assert_eq!(response.status(), 200, "{called}");
        assert!(
            !response.headers().contains_key("content-length"),
            "{called}"
        );
        assert_eq!(
            response.headers()["transfer-encoding"],
            "chunked",
            "{called}"
        );
        let body = bytes(response).await;
        let expected = std::str::from_utf8(NAMED_CHAT_JSON)
            .unwrap()
            .replace(r#""model":"actual""#, &format!(r#""model":"{called}""#));
        assert_eq!(body, expected.as_bytes(), "only the value changes");
        let event = cp.event().await;
        assert_eq!(event.model, "d", "{called}");
        assert_eq!(
            (event.prompt_tokens, event.completion_tokens),
            (5, 3),
            "{called}"
        );
        assert_eq!(event.usage_state(), UsageState::Reported, "{called}");
        upstream.received().await;
    }
    assert_eq!(
        gateway
            .metrics()
            .await
            .sum("lumewarp_response_model_rewrite_compressed_total", &[]),
        0.
    );
}

#[tokio::test]
async fn streams_carry_the_called_name_with_any_split_and_keep_usage() {
    let (mut cp, mut upstream, gateway) = catalog_setup().await;
    for called in REWRITTEN {
        upstream.respond(200, "text/event-stream", &[STREAM_HEAD, STREAM_TAIL]);
        let response = gateway
            .post(
                "/v1/chat/completions",
                KEY,
                &format!(r#"{{"model":"{called}","stream":true}}"#),
            )
            .await;
        assert_eq!(response.status(), 200, "{called}");
        let body = bytes(response).await;
        let text = std::str::from_utf8(&body).unwrap();
        // The forced usage event is stripped; the comment and CRLF stay.
        assert_eq!(
            text,
            format!(
                "data: {{\"id\":\"c\",\"object\":\"chat.completion.chunk\",\"model\":\"{called}\",\"choices\":[{{\"delta\":{{\"content\":\"hi\"}}}}],\"usage\":null}}\r\n\r\n: ping\r\n\r\ndata: [DONE]\n\n"
            ),
            "{called}"
        );
        let event = cp.event().await;
        assert_eq!(
            (event.prompt_tokens, event.completion_tokens),
            (11, 7),
            "{called}"
        );
        assert_eq!(event.usage_state(), UsageState::Reported, "{called}");
        upstream.received().await;
    }
}

#[tokio::test]
async fn a_client_asking_for_usage_gets_that_event_rewritten_too() {
    let (mut cp, upstream, gateway) = catalog_setup().await;
    upstream.respond(200, "text/event-stream", &[STREAM_HEAD, STREAM_TAIL]);
    let response = gateway
        .post(
            "/v1/chat/completions",
            KEY,
            r#"{"model":"to-model","stream":true,"stream_options":{"include_usage":true}}"#,
        )
        .await;
    let body = bytes(response).await;
    let text = std::str::from_utf8(&body).unwrap();
    assert_eq!(text.matches(r#""model":"to-model""#).count(), 2, "{text}");
    assert!(!text.contains("actual"), "{text}");
    let event = cp.event().await;
    assert_eq!((event.prompt_tokens, event.completion_tokens), (11, 7));
}

#[tokio::test]
async fn embeddings_with_model_after_data_are_rewritten() {
    let (mut cp, upstream, gateway) = catalog_setup_kind(2).await;
    let at = EMBEDDINGS_JSON.len() / 2;
    upstream.respond(
        200,
        "application/json",
        &[&EMBEDDINGS_JSON[..at], &EMBEDDINGS_JSON[at..]],
    );
    let response = gateway
        .post(
            "/v1/embeddings",
            KEY,
            r#"{"model":"to-router","input":"x"}"#,
        )
        .await;
    assert_eq!(response.status(), 200);
    let body = bytes(response).await;
    let expected = std::str::from_utf8(EMBEDDINGS_JSON)
        .unwrap()
        .replace(r#""model":"m""#, r#""model":"to-router""#);
    assert_eq!(body, expected.as_bytes());
    let event = cp.event().await;
    assert_eq!((event.prompt_tokens, event.completion_tokens), (8, 0));
    assert_eq!(event.usage_state(), UsageState::Reported);
}

#[tokio::test]
async fn the_fast_path_keeps_length_encoding_and_bytes() {
    let (mut cp, mut upstream, gateway) = catalog_setup().await;
    upstream.push(
        200,
        sized_headers("application/json", NAMED_CHAT_JSON, &[]),
        frames(&[NAMED_CHAT_JSON]),
    );
    let response = gateway
        .post_with(
            "/v1/chat/completions",
            KEY,
            r#"{"model":"actual"}"#,
            &[("accept-encoding", "gzip")],
        )
        .await;
    assert_eq!(
        response.headers()["content-length"],
        NAMED_CHAT_JSON.len().to_string().as_str()
    );
    assert_eq!(bytes(response).await, NAMED_CHAT_JSON);
    assert_eq!(
        upstream.received().await.headers["accept-encoding"],
        "gzip",
        "no rewrite needed: the client's header goes through"
    );
    assert_eq!(cp.event().await.prompt_tokens, 5);

    // A compressed answer is no exception here: nothing needed rewriting.
    upstream.push(
        200,
        vec![
            ("content-type", "application/json"),
            ("content-encoding", "gzip"),
        ],
        frames(&[b"gzipped"]),
    );
    let response = gateway
        .post_with(
            "/v1/chat/completions",
            KEY,
            r#"{"model":"actual"}"#,
            &[("accept-encoding", "gzip")],
        )
        .await;
    assert_eq!(bytes(response).await, &b"gzipped"[..]);
    assert_eq!(cp.event().await.usage_state(), UsageState::Invalid);
    assert_eq!(
        gateway
            .metrics()
            .await
            .sum("lumewarp_response_model_rewrite_compressed_total", &[]),
        0.
    );
}

#[tokio::test]
async fn accept_encoding_is_removed_when_the_response_will_be_rewritten() {
    let (mut cp, mut upstream, gateway) = catalog_setup().await;
    upstream.respond(200, "application/json", &[NAMED_CHAT_JSON]);
    let response = gateway
        .post_with(
            "/v1/chat/completions",
            KEY,
            r#"{"model":"to-model"}"#,
            &[("accept-encoding", "gzip, br")],
        )
        .await;
    assert_eq!(
        json(&bytes(response).await)["model"],
        "to-model",
        "the upstream honoured the removal"
    );
    assert!(!upstream
        .received()
        .await
        .headers
        .contains_key("accept-encoding"));
    let event = cp.event().await;
    assert_eq!((event.prompt_tokens, event.completion_tokens), (5, 3));
    assert_eq!(event.usage_state(), UsageState::Reported);
}

#[tokio::test]
async fn an_upstream_compressing_anyway_passes_through_with_the_metric() {
    let (mut cp, mut upstream, gateway) = catalog_setup().await;
    const GZIPPED: &[u8] = b"\x1f\x8b\x08\x00not-really-gzip\"model\":\"actual\"";
    upstream.push(
        200,
        sized_headers("application/json", GZIPPED, &[("content-encoding", "gzip")]),
        frames(&[GZIPPED]),
    );
    let response = gateway
        .post_with(
            "/v1/chat/completions",
            KEY,
            r#"{"model":"to-model"}"#,
            &[("accept-encoding", "gzip")],
        )
        .await;
    assert_eq!(response.status(), 200);
    assert_eq!(response.headers()["content-encoding"], "gzip");
    assert_eq!(
        response.headers()["content-length"],
        GZIPPED.len().to_string().as_str()
    );
    assert_eq!(bytes(response).await, GZIPPED);
    assert!(!upstream
        .received()
        .await
        .headers
        .contains_key("accept-encoding"));
    // Nothing can read the tokens of a body the gateway does not decompress.
    let event = cp.event().await;
    assert_eq!(event.usage_state(), UsageState::Invalid);
    assert_eq!(event.model, "d");
    let metrics = gateway.metrics().await;
    assert_eq!(
        metrics.value(
            "lumewarp_response_model_rewrite_compressed_total",
            &[("model", "d")]
        ),
        1.
    );

    // Also for a stream.
    upstream.push(
        200,
        vec![
            ("content-type", "text/event-stream"),
            ("content-encoding", "gzip"),
        ],
        frames(&[GZIPPED]),
    );
    let response = gateway
        .post(
            "/v1/chat/completions",
            KEY,
            r#"{"model":"to-model","stream":true}"#,
        )
        .await;
    assert_eq!(bytes(response).await, GZIPPED);
    cp.event().await;
    assert_eq!(
        gateway.metrics().await.value(
            "lumewarp_response_model_rewrite_compressed_total",
            &[("model", "d")]
        ),
        2.
    );
}

#[tokio::test]
async fn error_responses_are_not_rewritten() {
    let (mut cp, mut upstream, gateway) = catalog_setup().await;
    const ERROR: &[u8] = br#"{"error":{"message":"no such model"},"model":"actual"}"#;
    upstream.push(
        400,
        sized_headers("application/json", ERROR, &[]),
        frames(&[ERROR]),
    );
    let response = gateway
        .post("/v1/chat/completions", KEY, r#"{"model":"to-model"}"#)
        .await;
    assert_eq!(response.status(), 400);
    assert_eq!(
        response.headers()["content-length"],
        ERROR.len().to_string().as_str()
    );
    assert_eq!(bytes(response).await, ERROR);
    upstream.received().await;
    assert_eq!(cp.event().await.status, 400);
}

#[tokio::test]
async fn a_retry_decides_rewrite_and_encoding_per_attempt() {
    let mut upstream = Upstream::start().await;
    let mut snapshot = snapshot(upstream.addr);
    // One member needs no rewrite for the name `same`, the other does.
    let mut fast = snapshot.models[0].clone();
    fast.name = "fast".into();
    fast.upstream_model = "same".into();
    snapshot.models.push(fast);
    let mut router = snapshot.routers[0].clone();
    router.name = "same".into();
    router.members[0].model = "d".into();
    router.members.push(RouterMember {
        model: "fast".into(),
        weight: 1,
        ..Default::default()
    });
    router.retry = Some(lumewarp_proto::config::v1::RetryPolicy::default());
    snapshot.routers.push(router);
    snapshot.required_capabilities.push("retry".into());
    snapshot.api_keys[0]
        .policy
        .as_mut()
        .unwrap()
        .allowed_models
        .push("same".into());
    upstream.respond(503, "application/json", &[b"{\"error\":\"retry\"}"]);
    upstream.respond(200, "application/json", &[NAMED_CHAT_JSON]);
    let mut cp = FakeCp::start(snapshot).await;
    let gateway = Gateway::start(cp.addr).await;
    let response = gateway
        .post_with(
            "/v1/chat/completions",
            KEY,
            r#"{"model":"same"}"#,
            &[("accept-encoding", "gzip")],
        )
        .await;
    assert_eq!(response.status(), 200);
    let body = json(&bytes(response).await);
    let first = upstream.received().await;
    let second = upstream.received().await;
    for attempt in [&first, &second] {
        assert_eq!(
            attempt.headers.contains_key("accept-encoding"),
            json(&attempt.body)["model"] == "same",
            "kept only for the attempt that needs no rewrite"
        );
    }
    assert_ne!(json(&first.body)["model"], json(&second.body)["model"]);
    let event = cp.event().await;
    let (expected, model) = if json(&second.body)["model"] == "same" {
        ("actual", "fast")
    } else {
        ("same", "d")
    };
    assert_eq!(body["model"], expected, "by the model that served");
    assert_eq!(event.model, model);
    assert_eq!((event.prompt_tokens, event.completion_tokens), (5, 3));
}

#[tokio::test]
async fn a_response_above_max_body_is_rewritten_without_breaking() {
    const FRAME: usize = 1024 * 1024;
    const FRAMES: usize = 33;
    let (mut cp, mut upstream, gateway) = catalog_setup_kind(2).await;
    let frames_tx = upstream.respond_live("application/json");
    let response = gateway
        .post("/v1/embeddings", KEY, r#"{"model":"to-model","input":"x"}"#)
        .await;
    upstream.received().await;
    assert_eq!(response.status(), 200);
    assert!(!response.headers().contains_key("content-length"));
    tokio::spawn(async move {
        let send = |bytes: Bytes| frames_tx.send(Ok(bytes));
        send(Bytes::from_static(br#"{"object":"list","data":[[0.5"#))
            .await
            .unwrap();
        let filler = Bytes::from(",0.5".repeat(FRAME / 4));
        for _ in 0..FRAMES {
            send(filler.clone()).await.unwrap();
        }
        send(Bytes::from_static(
            br#"]],"model":"actual","usage":{"prompt_tokens":8,"total_tokens":8}}"#,
        ))
        .await
        .unwrap();
    });
    let body = tokio::time::timeout(Duration::from_secs(120), response.into_body().collect())
        .await
        .expect("body in time")
        .expect("connection not broken")
        .to_bytes();
    assert!(body.len() > 32 * 1024 * 1024);
    assert!(
        body.ends_with(br#"]],"model":"to-model","usage":{"prompt_tokens":8,"total_tokens":8}}"#),
        "the tail arrives rewritten"
    );
    // The usage copy is bounded (`MAX_JSON_CAPTURE`), the forwarded bytes are not.
    let event = cp.event().await;
    assert_eq!(event.outcome(), RequestOutcome::Completed);
    assert_eq!(event.usage_state(), UsageState::Invalid);
}

#[tokio::test]
async fn invalid_json_from_an_upstream_reaches_the_client_unchanged() {
    let (mut cp, upstream, gateway) = catalog_setup().await;
    const BROKEN: &[u8] = br#"{"a":[},"model":"actual"} not json"#;
    upstream.respond(200, "application/json", &[BROKEN]);
    let response = gateway
        .post("/v1/chat/completions", KEY, r#"{"model":"to-model"}"#)
        .await;
    assert_eq!(response.status(), 200);
    assert_eq!(bytes(response).await, BROKEN);
    assert_eq!(cp.event().await.usage_state(), UsageState::Invalid);
}

/// RFC 1952 gzip around one stored DEFLATE block, so `payload` is readable in
/// the wire bytes and any edit there breaks the block's CRC.
fn gzip_stored(payload: &[u8]) -> Vec<u8> {
    let crc = !payload.iter().fold(u32::MAX, |crc, &byte| {
        (0..8).fold(crc ^ u32::from(byte), |crc, _| {
            (crc >> 1) ^ (0xedb8_8320 & (crc & 1).wrapping_neg())
        })
    });
    let length = u16::try_from(payload.len()).unwrap();
    let mut out = vec![0x1f, 0x8b, 8, 0, 0, 0, 0, 0, 0, 0xff, 1];
    out.extend_from_slice(&length.to_le_bytes());
    out.extend_from_slice(&(!length).to_le_bytes());
    out.extend_from_slice(payload);
    out.extend_from_slice(&crc.to_le_bytes());
    out.extend_from_slice(&u32::from(length).to_le_bytes());
    out
}

#[tokio::test]
async fn every_content_encoding_field_and_list_item_counts() {
    let (mut cp, mut upstream, gateway) = catalog_setup().await;
    // A real gzip SSE stream whose wire bytes contain a line a scanner would edit.
    let gzipped = gzip_stored(b"\ndata: {\"model\":\"up\"}\n\ndata: [DONE]\n\n");
    assert!(gzipped.windows(4).any(|window| window == b"\"up\""));
    let cases: [&[&'static str]; 4] = [
        &["identity", "gzip"],
        &["gzip", "identity"],
        &["identity, gzip"],
        &["identity,\tGZIP"],
    ];
    for (index, codings) in cases.into_iter().enumerate() {
        let mut headers = vec![("content-type", "text/event-stream")];
        headers.extend(codings.iter().map(|coding| ("content-encoding", *coding)));
        upstream.push(200, headers, frames(&[&gzipped]));
        let response = gateway
            .post(
                "/v1/chat/completions",
                KEY,
                r#"{"model":"to-model","stream":true,"stream_options":{"include_usage":true}}"#,
            )
            .await;
        assert_eq!(response.status(), 200);
        assert_eq!(
            response
                .headers()
                .get_all("content-encoding")
                .iter()
                .count(),
            codings.len(),
            "{codings:?}"
        );
        assert_eq!(bytes(response).await, gzipped.as_slice(), "{codings:?}");
        assert_eq!(
            cp.event().await.usage_state(),
            UsageState::Invalid,
            "{codings:?}"
        );
        upstream.received().await;
        assert_eq!(
            gateway.metrics().await.value(
                "lumewarp_response_model_rewrite_compressed_total",
                &[("model", "d")]
            ),
            (index + 1) as f64,
            "{codings:?}"
        );
    }

    // `identity` in any form is not an encoding: the rewrite goes on.
    upstream.push(
        200,
        vec![
            ("content-type", "application/json"),
            ("content-encoding", "identity"),
            ("content-encoding", "Identity, identity"),
        ],
        frames(&[NAMED_CHAT_JSON]),
    );
    let response = gateway
        .post("/v1/chat/completions", KEY, r#"{"model":"to-model"}"#)
        .await;
    assert_eq!(json(&bytes(response).await)["model"], "to-model");
    assert_eq!(cp.event().await.usage_state(), UsageState::Reported);
}

const CHECKSUMS: [(&str, &str); 6] = [
    ("content-md5", "stale"),
    ("digest", "sha-256=stale"),
    ("content-digest", "sha-256=:stale:"),
    ("repr-digest", "sha-256=:stale:"),
    ("etag", "\"stale\""),
    ("x-upstream", "kept"),
];

fn assert_checksums(response: &Response<Incoming>, kept: bool, case: &str) {
    for (name, value) in CHECKSUMS {
        if name == "x-upstream" || kept {
            assert_eq!(response.headers()[name], value, "{case}: {name}");
        } else {
            assert!(!response.headers().contains_key(name), "{case}: {name}");
        }
    }
}

#[tokio::test]
async fn checksums_are_stripped_only_when_the_body_is_rewritten() {
    let (mut cp, upstream, gateway) = catalog_setup().await;
    let with_checksums = |pairs: &[(&'static str, &'static str)]| {
        let mut headers = pairs.to_vec();
        headers.extend_from_slice(&CHECKSUMS);
        headers
    };
    let json_type = [("content-type", "application/json")];

    upstream.push(200, with_checksums(&json_type), frames(&[NAMED_CHAT_JSON]));
    let response = gateway
        .post("/v1/chat/completions", KEY, r#"{"model":"to-model"}"#)
        .await;
    assert_checksums(&response, false, "rewritten");
    assert_eq!(json(&bytes(response).await)["model"], "to-model");
    cp.event().await;

    upstream.push(200, with_checksums(&json_type), frames(&[NAMED_CHAT_JSON]));
    let response = gateway
        .post("/v1/chat/completions", KEY, r#"{"model":"actual"}"#)
        .await;
    assert_checksums(&response, true, "fast path");
    assert_eq!(bytes(response).await, NAMED_CHAT_JSON);
    cp.event().await;

    upstream.push(400, with_checksums(&json_type), frames(&[NAMED_CHAT_JSON]));
    let response = gateway
        .post("/v1/chat/completions", KEY, r#"{"model":"to-model"}"#)
        .await;
    assert_eq!(response.status(), 400);
    assert_checksums(&response, true, "error");
    assert_eq!(bytes(response).await, NAMED_CHAT_JSON);
    cp.event().await;

    upstream.push(
        200,
        with_checksums(&[
            ("content-type", "application/json"),
            ("content-encoding", "gzip"),
        ]),
        frames(&[b"gzipped"]),
    );
    let response = gateway
        .post("/v1/chat/completions", KEY, r#"{"model":"to-model"}"#)
        .await;
    assert_checksums(&response, true, "compressed");
    assert_eq!(bytes(response).await, &b"gzipped"[..]);
    cp.event().await;

    upstream.push(
        200,
        with_checksums(&[("content-type", "application/octet-stream")]),
        frames(&[NAMED_CHAT_JSON]),
    );
    let response = gateway
        .post("/v1/chat/completions", KEY, r#"{"model":"to-model"}"#)
        .await;
    assert_checksums(&response, true, "opaque");
    assert_eq!(bytes(response).await, NAMED_CHAT_JSON);
    cp.event().await;
}

#[tokio::test]
async fn only_json_and_sse_media_types_are_rewritten() {
    let (mut cp, upstream, gateway) = catalog_setup().await;
    const BINARY: &[u8] = b"{\"model\":\"up\"}\x00\xff";
    const TEXT: &[u8] = b"{\"model\":\"up\"}";
    const EVENTS: &[u8] = b"data: {\"model\":\"up\"}\n\n";
    let non_stream = r#"{"model":"to-model"}"#;
    let stream = r#"{"model":"to-model","stream":true}"#;
    // Declared as something else: byte for byte, with its length.
    for (content_type, body, request, path) in [
        (
            "application/octet-stream",
            BINARY,
            non_stream,
            "/v1/chat/completions",
        ),
        ("text/plain", TEXT, non_stream, "/v1/chat/completions"),
        (
            "text/plain; charset=utf-8",
            EVENTS,
            stream,
            "/v1/chat/completions",
        ),
        (
            "application/octet-stream",
            EVENTS,
            stream,
            "/v1/chat/completions",
        ),
        (
            "multipart/form-data; boundary=x",
            TEXT,
            non_stream,
            "/v1/chat/completions",
        ),
        (
            "multipart/mixed; boundary=x",
            EVENTS,
            stream,
            "/v1/chat/completions",
        ),
    ] {
        upstream.push(200, sized_headers(content_type, body, &[]), frames(&[body]));
        let response = gateway.post(path, KEY, request).await;
        assert_eq!(response.status(), 200, "{content_type}");
        assert_eq!(
            response.headers()["content-length"],
            body.len().to_string().as_str(),
            "{content_type}"
        );
        assert_eq!(bytes(response).await, body, "{content_type}");
        cp.event().await;
    }
    // Opaque bodies were never going to be rewritten: not a compressed exception.
    upstream.push(
        200,
        vec![
            ("content-type", "application/octet-stream"),
            ("content-encoding", "gzip"),
        ],
        frames(&[BINARY]),
    );
    let response = gateway.post("/v1/chat/completions", KEY, non_stream).await;
    assert_eq!(bytes(response).await, BINARY);
    cp.event().await;
    assert_eq!(
        gateway
            .metrics()
            .await
            .sum("lumewarp_response_model_rewrite_compressed_total", &[]),
        0.
    );

    // Declared JSON or SSE, whatever the case and parameters.
    for (content_type, body, request, expected) in [
        (
            "Application/JSON; charset=utf-8",
            TEXT,
            non_stream,
            &br#"{"model":"to-model"}"#[..],
        ),
        (
            "application/vnd.api+json",
            TEXT,
            non_stream,
            br#"{"model":"to-model"}"#,
        ),
        ("application/json", TEXT, stream, br#"{"model":"to-model"}"#),
        (
            "TEXT/EVENT-STREAM; charset=utf-8",
            EVENTS,
            stream,
            b"data: {\"model\":\"to-model\"}\n\n",
        ),
        (
            "text/event-stream",
            EVENTS,
            non_stream,
            b"data: {\"model\":\"to-model\"}\n\n",
        ),
    ] {
        upstream.push(200, vec![("content-type", content_type)], frames(&[body]));
        let response = gateway.post("/v1/chat/completions", KEY, request).await;
        assert_eq!(bytes(response).await, expected, "{content_type}");
        cp.event().await;
    }

    // No Content-Type: the request's flag decides.
    upstream.push(200, Vec::new(), frames(&[TEXT]));
    let response = gateway.post("/v1/chat/completions", KEY, non_stream).await;
    assert_eq!(bytes(response).await, &br#"{"model":"to-model"}"#[..]);
    cp.event().await;
    upstream.push(200, Vec::new(), frames(&[EVENTS]));
    let response = gateway.post("/v1/chat/completions", KEY, stream).await;
    assert_eq!(
        bytes(response).await,
        &b"data: {\"model\":\"to-model\"}\n\n"[..]
    );
    cp.event().await;
}

#[tokio::test]
async fn opaque_answers_to_a_stream_are_observed_but_never_filtered() {
    let (mut cp, upstream, gateway) = catalog_setup().await;
    // This request has forced usage: USAGE is what the filter strips from SSE.
    let stream = r#"{"model":"to-model","stream":true}"#;
    let body = [CONTENT, USAGE, DONE].concat();
    for content_type in ["text/plain", "application/octet-stream"] {
        upstream.push(
            200,
            sized_headers(content_type, &body, &[]),
            frames(&[&body]),
        );
        let response = gateway.post("/v1/chat/completions", KEY, stream).await;
        assert_eq!(
            response.headers()["content-length"],
            body.len().to_string().as_str(),
            "{content_type}"
        );
        assert_eq!(bytes(response).await, body.as_slice(), "{content_type}");
        // The tokens are read all the same: billing does not depend on the label.
        let event = cp.event().await;
        assert_eq!(event.usage_state(), UsageState::Reported, "{content_type}");
        assert_eq!(
            (event.prompt_tokens, event.completion_tokens),
            (11, 7),
            "{content_type}"
        );
    }

    // The same bytes declared as SSE still lose the event the gateway forced.
    upstream.push(
        200,
        vec![("content-type", "text/event-stream")],
        frames(&[&body]),
    );
    let response = gateway.post("/v1/chat/completions", KEY, stream).await;
    assert_eq!(bytes(response).await, [CONTENT, DONE].concat().as_slice());
    assert_eq!(cp.event().await.prompt_tokens, 11);
}

#[tokio::test]
async fn the_meter_reads_the_declared_media_type_like_the_rewrite() {
    let (mut cp, upstream, gateway) = catalog_setup().await;
    // A JSON answer to a stream request, however the type is written.
    for content_type in [
        "application/json",
        "Application/JSON",
        "APPLICATION/JSON; charset=utf-8",
        "application/vnd.api+json",
    ] {
        upstream.push(
            200,
            vec![("content-type", content_type)],
            frames(&[NAMED_CHAT_JSON]),
        );
        let response = gateway
            .post(
                "/v1/chat/completions",
                KEY,
                r#"{"model":"to-model","stream":true}"#,
            )
            .await;
        assert_eq!(json(&bytes(response).await)["model"], "to-model");
        let event = cp.event().await;
        assert_eq!(event.usage_state(), UsageState::Reported, "{content_type}");
        assert_eq!(
            (event.prompt_tokens, event.completion_tokens),
            (5, 3),
            "{content_type}"
        );
    }

    // SSE answering a non-stream request: read as SSE, in any case, and nothing
    // is stripped since no usage was forced.
    let body = [CONTENT, USAGE, DONE].concat();
    for content_type in ["text/event-stream", "Text/Event-Stream; charset=utf-8"] {
        upstream.push(200, vec![("content-type", content_type)], frames(&[&body]));
        let response = gateway
            .post("/v1/chat/completions", KEY, r#"{"model":"actual"}"#)
            .await;
        assert_eq!(bytes(response).await, body.as_slice(), "{content_type}");
        let event = cp.event().await;
        assert_eq!(event.usage_state(), UsageState::Reported, "{content_type}");
        assert_eq!(event.prompt_tokens, 11, "{content_type}");
    }

    // No Content-Type: the request's flag decides, for the meter as well.
    upstream.push(200, Vec::new(), frames(&[NAMED_CHAT_JSON]));
    let response = gateway
        .post("/v1/chat/completions", KEY, r#"{"model":"to-model"}"#)
        .await;
    bytes(response).await;
    assert_eq!(cp.event().await.prompt_tokens, 5);
    upstream.push(200, Vec::new(), frames(&[&body]));
    let response = gateway
        .post(
            "/v1/chat/completions",
            KEY,
            r#"{"model":"to-model","stream":true}"#,
        )
        .await;
    bytes(response).await;
    assert_eq!(cp.event().await.prompt_tokens, 11);
}
