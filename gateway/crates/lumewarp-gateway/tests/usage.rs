//! Usage metering end to end: the gateway binary in control-plane mode against
//! an in-process fake control plane (ConfigService + UsageService, hand-written
//! tonic services since the proto crate only generates clients) and a scripted
//! upstream that controls every response frame.

mod common;

use std::{
    collections::VecDeque,
    convert::Infallible,
    net::{SocketAddr, TcpListener as StdListener},
    path::PathBuf,
    pin::Pin,
    process::{Child, Command, Stdio},
    sync::{
        atomic::{AtomicU32, AtomicU64, Ordering},
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
        config_update::Update, ApiKey, ConfigUpdate, Deployment, EffectivePolicy, GatewayMessage,
        KeyStatus, ModelRoute, Price, RateLimit, RateLimitScope, Snapshot, StreamUsageMode, Team,
        Upstream as UpstreamConfig, UpstreamKind,
    },
    usage::v1::{ReportRequest, ReportResponse, RequestOutcome, UsageEvent, UsageState},
};
use serde_json::Value;
use sha2::{Digest, Sha256};
use tokio::{net::TcpListener, sync::mpsc, task::JoinHandle};
use tokio_stream::{wrappers::TcpListenerStream, Stream};
use tonic::{
    codegen::{http, BoxFuture, Service},
    server::NamedService,
    Status,
};

const SCOPE: &str = "usage-test";
const TOKEN_ENV: &str = "LUMEWARP_USAGE_TEST_TOKEN";
const KEY: &str = "lw-usage-key";
const LIMITED_KEY: &str = "lw-limited-key";

const CONTENT: &[u8] = b"data: {\"id\":\"c\",\"object\":\"chat.completion.chunk\",\"choices\":[{\"index\":0,\"delta\":{\"content\":\"hi\"},\"finish_reason\":null}],\"usage\":null}\n\n";
const FINISH: &[u8] = b"data: {\"id\":\"c\",\"object\":\"chat.completion.chunk\",\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"stop\"}],\"usage\":null}\n\n";
const USAGE: &[u8] = b"data: {\"id\":\"c\",\"object\":\"chat.completion.chunk\",\"choices\":[],\"usage\":{\"prompt_tokens\":11,\"completion_tokens\":7,\"total_tokens\":18}}\n\n";
const DONE: &[u8] = b"data: [DONE]\n\n";
const CHAT_JSON: &[u8] = br#"{"id":"c","object":"chat.completion","choices":[{"index":0,"message":{"role":"assistant","content":"hi"},"finish_reason":"stop"}],"usage":{"prompt_tokens":5,"completion_tokens":3,"total_tokens":8}}"#;
const EMBEDDINGS_JSON: &[u8] = br#"{"object":"list","data":[{"object":"embedding","index":0,"embedding":[0.5]}],"model":"m","usage":{"prompt_tokens":8,"total_tokens":8}}"#;
/// $1 / Mtok input, $2 / Mtok output.
const PRICE: Price = Price {
    input_nano_usd_per_mtok: 1_000_000_000,
    output_nano_usd_per_mtok: 2_000_000_000,
};

// ---------------------------------------------------------------------------
// Fake control plane
// ---------------------------------------------------------------------------

struct CpState {
    snapshot: Snapshot,
    /// Keeps config streams open.
    streams: Mutex<Vec<mpsc::UnboundedSender<Result<ConfigUpdate, Status>>>>,
    events: mpsc::UnboundedSender<(Option<String>, UsageEvent)>,
    reports: AtomicU64,
    /// Next Report calls answered UNAVAILABLE.
    fail: AtomicU32,
}

/// Decrements `counter` if positive; whether it did.
fn take_one(counter: &AtomicU32) -> bool {
    let mut n = counter.load(Ordering::SeqCst);
    while n > 0 {
        match counter.compare_exchange(n, n - 1, Ordering::SeqCst, Ordering::SeqCst) {
            Ok(_) => return true,
            Err(current) => n = current,
        }
    }
    false
}

struct Updates(mpsc::UnboundedReceiver<Result<ConfigUpdate, Status>>);
impl Stream for Updates {
    type Item = Result<ConfigUpdate, Status>;
    fn poll_next(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        self.0.poll_recv(cx)
    }
}

struct StreamImpl(Arc<CpState>);
impl tonic::server::StreamingService<GatewayMessage> for StreamImpl {
    type Response = ConfigUpdate;
    type ResponseStream = Updates;
    type Future = BoxFuture<tonic::Response<Updates>, Status>;

    fn call(&mut self, request: tonic::Request<tonic::Streaming<GatewayMessage>>) -> Self::Future {
        let state = self.0.clone();
        Box::pin(async move {
            let mut incoming = request.into_inner();
            tokio::spawn(async move { while let Ok(Some(_)) = incoming.message().await {} });
            let (tx, rx) = mpsc::unbounded_channel();
            tx.send(Ok(ConfigUpdate {
                update: Some(Update::Full(state.snapshot.clone())),
                target_digest: b"usage-test-digest".to_vec(),
            }))
            .unwrap();
            state.streams.lock().unwrap().push(tx);
            Ok(tonic::Response::new(Updates(rx)))
        })
    }
}

struct ReportImpl(Arc<CpState>);
impl tonic::server::UnaryService<ReportRequest> for ReportImpl {
    type Response = ReportResponse;
    type Future = BoxFuture<tonic::Response<ReportResponse>, Status>;

    fn call(&mut self, request: tonic::Request<ReportRequest>) -> Self::Future {
        let state = self.0.clone();
        Box::pin(async move {
            if take_one(&state.fail) {
                return Err(Status::unavailable("control plane down"));
            }
            let authorization = request
                .metadata()
                .get("authorization")
                .and_then(|v| v.to_str().ok())
                .map(str::to_owned);
            let request = request.into_inner();
            state.reports.fetch_add(1, Ordering::SeqCst);
            let accepted = request.events.len() as u32;
            for event in request.events {
                let _ = state.events.send((authorization.clone(), event));
            }
            Ok(tonic::Response::new(ReportResponse { accepted }))
        })
    }
}

macro_rules! grpc_service {
    ($name:ident, $service:literal, $path:literal, $method:ident, $imp:ident) => {
        #[derive(Clone)]
        struct $name(Arc<CpState>);
        impl NamedService for $name {
            const NAME: &'static str = $service;
        }
        impl Service<http::Request<tonic::body::Body>> for $name {
            type Response = http::Response<tonic::body::Body>;
            type Error = Infallible;
            type Future = BoxFuture<Self::Response, Self::Error>;

            fn poll_ready(&mut self, _: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
                Poll::Ready(Ok(()))
            }

            fn call(&mut self, req: http::Request<tonic::body::Body>) -> Self::Future {
                let state = self.0.clone();
                Box::pin(async move {
                    if req.uri().path() != $path {
                        return Ok(Status::unimplemented("unknown method").into_http());
                    }
                    let mut grpc = tonic::server::Grpc::new(tonic_prost::ProstCodec::default());
                    Ok(grpc.$method($imp(state), req).await)
                })
            }
        }
    };
}
grpc_service!(
    ConfigSvc,
    "lumewarp.config.v1.ConfigService",
    "/lumewarp.config.v1.ConfigService/Stream",
    streaming,
    StreamImpl
);
grpc_service!(
    UsageSvc,
    "lumewarp.usage.v1.UsageService",
    "/lumewarp.usage.v1.UsageService/Report",
    unary,
    ReportImpl
);

struct FakeCp {
    addr: SocketAddr,
    state: Arc<CpState>,
    events: mpsc::UnboundedReceiver<(Option<String>, UsageEvent)>,
    task: JoinHandle<()>,
}

impl FakeCp {
    async fn start(snapshot: Snapshot) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let (tx, events) = mpsc::unbounded_channel();
        let state = Arc::new(CpState {
            snapshot,
            streams: Mutex::new(Vec::new()),
            events: tx,
            reports: AtomicU64::new(0),
            fail: AtomicU32::new(0),
        });
        let (config, usage) = (ConfigSvc(state.clone()), UsageSvc(state.clone()));
        let task = tokio::spawn(async move {
            let _ = tonic::transport::Server::builder()
                .add_service(config)
                .add_service(usage)
                .serve_with_incoming(TcpListenerStream::new(listener))
                .await;
        });
        Self {
            addr,
            state,
            events,
            task,
        }
    }

    /// The next reported event (reports are batched up to 1 s apart).
    async fn event(&mut self) -> UsageEvent {
        let (authorization, event) =
            tokio::time::timeout(Duration::from_secs(10), self.events.recv())
                .await
                .expect("usage event within 10 s")
                .expect("fake control plane stopped");
        assert_eq!(authorization.as_deref(), Some("Bearer gw-secret"));
        event
    }

    /// No further event arrives within two flush intervals.
    async fn no_more_events(&mut self) {
        if let Ok(Some((_, event))) =
            tokio::time::timeout(Duration::from_millis(2200), self.events.recv()).await
        {
            panic!("unexpected extra usage event: {event:?}");
        }
    }
}

impl Drop for FakeCp {
    fn drop(&mut self) {
        self.task.abort();
    }
}

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
                            let body = request.into_body().collect().await.unwrap().to_bytes();
                            let script = queue
                                .lock()
                                .unwrap()
                                .pop_front()
                                .expect("a scripted upstream response");
                            let (dropped, closed) = tokio::sync::oneshot::channel();
                            let _ = tx.send(Received { path, body, closed });
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
        }),
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
    };
    let deployment = |name: &str, upstream: &str, price: Option<Price>| Deployment {
        name: name.into(),
        upstream: upstream.into(),
        upstream_model: "actual".into(),
        weight: 1,
        price,
    };
    let route = |model: &str, deployment: &str| ModelRoute {
        model: model.into(),
        deployments: vec![deployment.into()],
        strategy: 1,
    };
    Snapshot {
        version: 7,
        upstreams: vec![
            config("u", upstream, StreamUsageMode::Unspecified),
            config("passthrough", upstream, StreamUsageMode::Passthrough),
            config("dead", dead, StreamUsageMode::IncludeUsage),
        ],
        deployments: vec![
            deployment("d", "u", Some(PRICE)),
            deployment("dp", "passthrough", None),
            deployment("dd", "dead", Some(PRICE)),
        ],
        routes: vec![
            route("public", "d"),
            route("pass", "dp"),
            route("down", "dd"),
            route("secret", "d"),
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
        required_capabilities: Vec::new(),
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
        let listen = StdListener::bind("127.0.0.1:0").unwrap();
        let admin = StdListener::bind("127.0.0.1:0").unwrap();
        let (addr, admin_addr) = (listen.local_addr().unwrap(), admin.local_addr().unwrap());
        let local = dir.join("local.yaml");
        std::fs::write(
            &local,
            format!("server:\n  listen: '{addr}'\n  admin_listen: '{admin_addr}'\n"),
        )
        .unwrap();
        drop((listen, admin));
        let child = Command::new(env!("CARGO_BIN_EXE_lumewarp"))
            .args(["--control-plane", &format!("http://{control_plane}")])
            .args(["--gateway-token-env", TOKEN_ENV])
            .arg("--state-dir")
            .arg(dir.join("state"))
            .args(["--scope", SCOPE, "--gateway-id", "gw-usage"])
            .arg("--local-config")
            .arg(&local)
            .env(TOKEN_ENV, "gw-secret")
            .env_remove("OTEL_EXPORTER_OTLP_ENDPOINT")
            .env("RUST_LOG", "error")
            .stdout(Stdio::null())
            .stderr(Stdio::from(
                std::fs::File::create(dir.join("stderr.log")).unwrap(),
            ))
            .spawn()
            .unwrap();
        let mut gateway = Self {
            child,
            addr,
            admin: admin_addr,
            http: client(),
            dir,
        };
        let ready = tokio::time::timeout(Duration::from_secs(10), async {
            loop {
                if let Some(status) = gateway.child.try_wait().unwrap() {
                    panic!("gateway exited {status}: {}", gateway.logs());
                }
                let request = Request::builder()
                    .uri(format!("http://{}/readyz", gateway.admin))
                    .body(Full::new(Bytes::new()))
                    .unwrap();
                if let Ok(response) = gateway.http.request(request).await {
                    if response.status() == 200 {
                        break;
                    }
                }
                tokio::time::sleep(Duration::from_millis(20)).await;
            }
        })
        .await;
        assert!(ready.is_ok(), "gateway not ready: {}", gateway.logs());
        gateway
    }

    async fn post(&self, path: &str, secret: &str, body: &str) -> Response<Incoming> {
        let request = Request::builder()
            .method("POST")
            .uri(format!("http://{}{path}", self.addr))
            .header("authorization", format!("Bearer {secret}"))
            .header("content-type", "application/json")
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
            event.model.as_str(),
            event.deployment.as_str(),
            event.route.as_str()
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
    assert_eq!((event.deployment.as_str(), event.cost_known), ("dp", false));
    cp.no_more_events().await;

    let metrics = gateway.metrics().await;
    let tokens = |direction| {
        metrics.value(
            "lumewarp_tokens_total",
            &[
                ("model", "public"),
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
    assert_eq!((event.status, event.deployment.as_str()), (502, "dd"));
    cp.no_more_events().await;
    let metrics = gateway.metrics().await;
    assert_eq!(
        metrics.value("lumewarp_usage_capture_total", &[("state", "partial")]),
        2.0
    );
}

#[tokio::test]
async fn non_stream_and_embeddings_usage() {
    let (mut cp, mut upstream, gateway) = setup().await;
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
        .post("/v1/embeddings", KEY, r#"{"model":"public","input":"hi"}"#)
        .await;
    assert_eq!(json(&bytes(response).await)["object"], "list");
    assert_eq!(upstream.received().await.path, "/v1/embeddings");
    let event = cp.event().await;
    assert_eq!(event.usage_state, UsageState::Reported as i32);
    assert_eq!(
        (
            event.prompt_tokens,
            event.completion_tokens,
            event.route.as_str()
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
        (
            event.status,
            event.model.as_str(),
            event.deployment.as_str()
        ),
        (403, "secret", "")
    );
    // Unknown aliases are not echoed into events.
    let response = gateway
        .post("/v1/chat/completions", KEY, r#"{"model":"client-made-up"}"#)
        .await;
    assert_eq!(response.status(), 403);
    assert_eq!(cp.event().await.model, "unknown");

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
        (
            event.status,
            event.model.as_str(),
            event.deployment.as_str()
        ),
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
