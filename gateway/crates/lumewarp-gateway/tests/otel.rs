//! A real gateway process exporting OTLP to an in-process sink. OTel is where a
//! single request is looked up, so the fields and what stays out of them are
//! part of the contract. The OTLP protobuf is walked by hand: only the handful
//! of fields asserted here are needed and no dependency is added for them.
mod common;
#[path = "common/usage_cp.rs"]
mod usage_cp;

use bytes::Bytes;
use clap::Parser;
use common::{bounded, bytes, client, TestClient};
use http_body_util::{BodyExt, Full};
use hyper::{service::service_fn, Request, Response};
use hyper_util::rt::TokioIo;
use lumewarp_proto::{
    config::v1::{
        Alias, ApiKey, EffectivePolicy, KeyStatus, Model, RateLimit, RateLimitScope, RetryPolicy,
        Router, RouterMember, Snapshot, Team, Upstream, UpstreamKind,
    },
    usage::v1::{RequestOutcome, UsageEvent},
};
use mock_upstream::{App, Options};
use sha2::{Digest, Sha256};
use std::{
    collections::HashMap,
    io::{BufRead, BufReader},
    net::SocketAddr,
    path::Path,
    process::{Child, Command, Stdio},
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::{
    io::AsyncWriteExt,
    net::{TcpListener, TcpStream},
    sync::{mpsc, Notify},
};
use usage_cp::FakeCp;

const SECRET: &str = "sk-otel-secret-key-do-not-leak";
const PROMPT: &str = "the-prompt-must-not-be-exported";
const SCOPE: &str = "otel-test";
const TOKEN_ENV: &str = "LUMEWARP_OTEL_TEST_TOKEN";

#[derive(Debug, Default, Clone)]
struct Span {
    name: String,
    trace_id: String,
    parent: String,
    attributes: HashMap<String, String>,
    /// Keys exported more than once. The SDK appends each record and does not
    /// replace the earlier value, and what a backend then shows is undefined.
    repeated: Vec<String>,
}

impl Span {
    fn get(&self, key: &str) -> Option<&str> {
        self.attributes.get(key).map(String::as_str)
    }
}

/// (field number, wire type, value or length-delimited bytes)
fn fields(mut data: &[u8]) -> Vec<(u64, u8, u64, &[u8])> {
    fn varint(data: &mut &[u8]) -> u64 {
        let (mut value, mut shift) = (0u64, 0);
        loop {
            let byte = data[0];
            *data = &data[1..];
            value |= u64::from(byte & 0x7f) << shift;
            if byte & 0x80 == 0 {
                return value;
            }
            shift += 7;
        }
    }
    let mut out = Vec::new();
    while !data.is_empty() {
        let tag = varint(&mut data);
        let (number, wire) = (tag >> 3, (tag & 7) as u8);
        match wire {
            0 => out.push((number, wire, varint(&mut data), &[][..])),
            1 => {
                out.push((number, wire, 0, &data[..8]));
                data = &data[8..];
            }
            2 => {
                let len = varint(&mut data) as usize;
                out.push((number, wire, 0, &data[..len]));
                data = &data[len..];
            }
            5 => {
                out.push((number, wire, 0, &data[..4]));
                data = &data[4..];
            }
            other => panic!("wire type {other}"),
        }
    }
    out
}

fn text(data: &[u8]) -> String {
    String::from_utf8_lossy(data).into_owned()
}

fn hex_id(data: &[u8]) -> String {
    data.iter().map(|b| format!("{b:02x}")).collect()
}

/// ExportTraceServiceRequest.resource_spans(1).scope_spans(2).spans(2).
fn decode(request: &[u8]) -> Vec<Span> {
    let mut spans = Vec::new();
    for (_, _, _, resource) in fields(request).into_iter().filter(|f| f.0 == 1) {
        for (_, _, _, scope) in fields(resource).into_iter().filter(|f| f.0 == 2) {
            for (_, _, _, raw) in fields(scope).into_iter().filter(|f| f.0 == 2) {
                let mut span = Span::default();
                for (number, _, _, value) in fields(raw) {
                    match number {
                        1 => span.trace_id = hex_id(value),
                        4 => span.parent = hex_id(value),
                        5 => span.name = text(value),
                        9 => {
                            let (mut key, mut attr) = (String::new(), String::new());
                            for (n, _, _, v) in fields(value) {
                                match n {
                                    1 => key = text(v),
                                    2 => {
                                        for (kind, _, number, s) in fields(v) {
                                            attr = match kind {
                                                1 => text(s),
                                                2 => (number != 0).to_string(),
                                                3 => number.to_string(),
                                                _ => attr,
                                            };
                                        }
                                    }
                                    _ => {}
                                }
                            }
                            if span.attributes.insert(key.clone(), attr).is_some() {
                                span.repeated.push(key);
                            }
                        }
                        _ => {}
                    }
                }
                spans.push(span);
            }
        }
    }
    spans
}

#[derive(Default)]
struct Sink {
    spans: Mutex<Vec<Span>>,
    raw: Mutex<Vec<u8>>,
    arrived: Notify,
}

impl Sink {
    async fn start() -> (Arc<Self>, SocketAddr) {
        let sink = Arc::new(Self::default());
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let shared = sink.clone();
        tokio::spawn(async move {
            while let Ok((stream, _)) = listener.accept().await {
                let sink = shared.clone();
                tokio::spawn(hyper::server::conn::http1::Builder::new().serve_connection(
                    TokioIo::new(stream),
                    service_fn(move |request: Request<hyper::body::Incoming>| {
                        let sink = sink.clone();
                        async move {
                            assert_eq!(request.uri().path(), "/v1/traces");
                            let body = request.into_body().collect().await?.to_bytes();
                            sink.raw.lock().unwrap().extend_from_slice(&body);
                            sink.spans.lock().unwrap().extend(decode(&body));
                            sink.arrived.notify_waiters();
                            Ok::<_, hyper::Error>(Response::new(Full::new(Bytes::new())))
                        }
                    }),
                ));
            }
        });
        (sink, addr)
    }

    /// The spans of the request whose id the response header gave.
    async fn wait_request(&self, request_id: &str) -> Vec<Span> {
        bounded(async {
            loop {
                let arrived = self.arrived.notified();
                let found = {
                    let spans = self.spans.lock().unwrap();
                    spans
                        .iter()
                        .find(|s| s.name == "request" && s.get("request_id") == Some(request_id))
                        .map(|root| {
                            spans
                                .iter()
                                .filter(|s| s.trace_id == root.trace_id)
                                .cloned()
                                .collect::<Vec<_>>()
                        })
                };
                if let Some(found) = found {
                    return found;
                }
                arrived.await;
            }
        })
        .await
    }
}

struct Gateway {
    addr: SocketAddr,
    child: Child,
    http: TestClient,
    _dir: tempfile::TempDir,
}

impl Gateway {
    async fn start(sink: SocketAddr, good: SocketAddr, bad: SocketAddr) -> Self {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("config.yaml");
        let hash = hex::encode(Sha256::digest(SECRET));
        std::fs::write(
            &path,
            format!(
                r#"server:
  listen: '127.0.0.1:0'
  admin_listen: '127.0.0.2:0'
upstreams:
  - {{name: good-up, kind: vllm, base_url: 'http://{good}/v1', timeout_ms: 2000}}
  - {{name: bad-up, kind: vllm, base_url: 'http://{bad}/v1', timeout_ms: 2000}}
models:
  - {{type: llm, name: good, upstream: good-up, upstream_model: actual-good}}
  - {{type: llm, name: bad, upstream: bad-up, upstream_model: actual-bad}}
routers:
  - {{name: pool, members: [{{model: bad}}], retry: {{max_attempts: 2}}, fallbacks: [good]}}
aliases:
  - {{name: nick, target: pool}}
teams:
  - {{id: team-a}}
keys:
  - {{key_id: key-a, key_sha256: '{hash}', team_id: team-a, allowed_models: [nick, good]}}
"#
            ),
        )
        .unwrap();
        let mut command = Command::new(env!("CARGO_BIN_EXE_lumewarp"));
        command.arg("--config").arg(path);
        Self::spawn(command, sink, dir, false).await
    }

    /// Control-plane mode. `applied`: wait for the first snapshot; otherwise the
    /// gateway is only listening, as on a cold start without a control plane.
    async fn start_cp(
        sink: SocketAddr,
        control_plane: SocketAddr,
        cached: Option<&[u8]>,
        applied: bool,
    ) -> Self {
        let dir = tempfile::tempdir().unwrap();
        let local = dir.path().join("local.yaml");
        std::fs::write(
            &local,
            "server:\n  listen: '127.0.0.1:0'\n  admin_listen: '127.0.0.2:0'\n",
        )
        .unwrap();
        let state = dir.path().join("state");
        if let Some(cached) = cached {
            write_cache(&state, cached);
        }
        let mut command = Command::new(env!("CARGO_BIN_EXE_lumewarp"));
        command
            .args(["--control-plane", &format!("http://{control_plane}")])
            .args(["--gateway-token-env", TOKEN_ENV])
            .arg("--state-dir")
            .arg(state)
            .args(["--scope", SCOPE])
            .arg("--local-config")
            .arg(local)
            .env(TOKEN_ENV, "gw-secret");
        Self::spawn(command, sink, dir, applied).await
    }

    async fn spawn(
        mut command: Command,
        sink: SocketAddr,
        dir: tempfile::TempDir,
        wait_applied: bool,
    ) -> Self {
        let mut child = command
            .env("RUST_LOG", "info")
            .env("NO_COLOR", "1")
            .env("OTEL_EXPORTER_OTLP_ENDPOINT", format!("http://{sink}"))
            .env("OTEL_BSP_SCHEDULE_DELAY", "50")
            .env_remove("OTEL_TRACES_SAMPLER")
            .env_remove("OTEL_TRACES_SAMPLER_ARG")
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .unwrap();
        let stdout = child.stdout.take().unwrap();
        let (tx, rx) = tokio::sync::oneshot::channel();
        std::thread::spawn(move || {
            let mut tx = Some(tx);
            let mut applied = !wait_applied;
            let mut addr = None;
            for line in BufReader::new(stdout).lines().map_while(Result::ok) {
                if line.contains("WARN") || line.contains("ERROR") {
                    eprintln!("{line}");
                }
                applied |= line.contains("config applied");
                if line.contains("lumewarp gateway listening") {
                    addr = line
                        .split_whitespace()
                        .find_map(|part| part.strip_prefix("listen="))
                        .map(|addr| addr.parse::<SocketAddr>().unwrap());
                }
                if let (true, Some(addr)) = (applied, addr) {
                    if let Some(tx) = tx.take() {
                        let _ = tx.send(addr);
                    }
                }
            }
        });
        // Built first, so a failed wait still kills the child on drop.
        let mut gateway = Self {
            addr: ([127, 0, 0, 1], 0).into(),
            child,
            http: client(),
            _dir: dir,
        };
        gateway.addr = tokio::time::timeout(Duration::from_secs(10), rx)
            .await
            .expect("gateway listening")
            .expect("gateway exited");
        gateway
    }

    /// Returns the response status and its request id.
    async fn call(&self, model: &str, traceparent: Option<&str>) -> (u16, String) {
        let (status, id, _) = self.call_as(SECRET, model, traceparent).await;
        (status, id)
    }

    /// Returns the response status, its request id and its body.
    async fn call_as(
        &self,
        secret: &str,
        model: &str,
        traceparent: Option<&str>,
    ) -> (u16, String, Bytes) {
        let mut request = Request::builder()
            .method("POST")
            .uri(format!("http://{}/v1/chat/completions", self.addr))
            .header("authorization", format!("Bearer {secret}"));
        if let Some(traceparent) = traceparent {
            request = request.header("traceparent", traceparent);
        }
        let body =
            format!(r#"{{"model":"{model}","messages":[{{"role":"user","content":"{PROMPT}"}}]}}"#);
        let response = bounded(
            self.http
                .request(request.body(Full::new(Bytes::from(body))).unwrap()),
        )
        .await
        .unwrap();
        let status = response.status().as_u16();
        let id = response.headers()["x-lumewarp-request-id"]
            .to_str()
            .unwrap()
            .to_owned();
        (status, id, bytes(response).await)
    }

    /// Sends a request over a raw socket, for a client that goes away mid-request.
    async fn send_raw(&self, model: &str) -> TcpStream {
        let body = format!(r#"{{"model":"{model}"}}"#);
        let mut socket = TcpStream::connect(self.addr).await.unwrap();
        socket
            .write_all(
                format!(
                    "POST /v1/chat/completions HTTP/1.1\r\nhost: gw\r\nauthorization: Bearer {SECRET}\r\ncontent-type: application/json\r\ncontent-length: {}\r\n\r\n{body}",
                    body.len()
                )
                .as_bytes(),
            )
            .await
            .unwrap();
        socket
    }
}

fn write_cache(state: &Path, contents: &[u8]) {
    std::fs::create_dir_all(state).unwrap();
    std::fs::write(state.join("snapshot.pb"), contents).unwrap();
}

impl Drop for Gateway {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

async fn mock(extra: &[&str]) -> SocketAddr {
    let mut args = vec!["mock", "--latency-ms", "0"];
    args.extend_from_slice(extra);
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(mock_upstream::serve(
        listener,
        App::new(Options::parse_from(args)),
    ));
    addr
}

async fn setup() -> (Arc<Sink>, Gateway) {
    let (sink, sink_addr) = Sink::start().await;
    let good = mock(&[]).await;
    let bad = mock(&["--fail-every", "1"]).await;
    let gateway = Gateway::start(sink_addr, good, bad).await;
    (sink, gateway)
}

#[tokio::test]
async fn request_span_has_the_whole_story_and_no_secrets() {
    let (sink, gateway) = setup().await;
    let (status, id) = gateway.call("nick", None).await;
    assert_eq!(status, 200);
    let spans = sink.wait_request(&id).await;
    let root = spans.iter().find(|s| s.name == "request").unwrap();
    for (field, want) in [
        ("request_id", id.as_str()),
        ("team_id", "team-a"),
        ("key_id", "key-a"),
        ("requested", "nick"),
        ("alias", "nick"),
        ("router", "pool"),
        ("model", "good"),
        ("upstream", "good-up"),
        ("upstream_model", "actual-good"),
        ("attempts", "2"),
        ("fallback", "true"),
        ("status", "200"),
    ] {
        assert_eq!(root.get(field), Some(want), "{field}: {root:?}");
    }
    assert!(root.get("config_version").is_some(), "{root:?}");
    // A retried request records the most, and each key still appears once.
    assert_eq!(root.repeated, Vec::<String>::new(), "{root:?}");

    // upstream_attempt spans are children of it, one per try. Retry budget 2:
    // the bad model, then the fallback.
    let mut attempts: Vec<_> = spans
        .iter()
        .filter(|s| s.name == "upstream_attempt")
        .collect();
    assert_eq!(attempts.len(), 2, "{spans:?}");
    attempts.sort_by_key(|s| s.get("attempt").map(str::to_owned));
    let first = attempts
        .iter()
        .find(|s| s.get("upstream") == Some("bad-up"))
        .unwrap();
    assert_eq!(first.get("upstream_model"), Some("actual-bad"));
    assert_eq!(first.get("upstream_status"), Some("503"));
    assert_eq!(first.get("fallback"), Some("false"));
    let second = attempts
        .iter()
        .find(|s| s.get("upstream") == Some("good-up"))
        .unwrap();
    assert_eq!(second.get("upstream_model"), Some("actual-good"));
    assert_eq!(second.get("upstream_status"), Some("200"));
    assert_eq!(second.get("fallback"), Some("true"));

    let raw = sink.raw.lock().unwrap();
    for secret in [SECRET, PROMPT, hex::encode(Sha256::digest(SECRET)).as_str()] {
        assert!(
            !raw.windows(secret.len()).any(|w| w == secret.as_bytes()),
            "{secret} was exported"
        );
    }
}

#[tokio::test]
async fn direct_call_has_empty_provenance() {
    let (sink, gateway) = setup().await;
    let (status, id) = gateway.call("good", None).await;
    assert_eq!(status, 200);
    let spans = sink.wait_request(&id).await;
    let root = spans.iter().find(|s| s.name == "request").unwrap();
    assert_eq!(root.get("requested"), Some("good"));
    assert_eq!(root.get("alias"), None);
    assert_eq!(root.get("router"), None);
    assert_eq!(root.get("model"), Some("good"));
    assert_eq!(root.get("attempts"), Some("1"));
    assert_eq!(root.get("fallback"), Some("false"));
}

#[tokio::test]
async fn rejection_is_recorded_with_the_requested_name() {
    let (sink, gateway) = setup().await;
    let (status, id) = gateway.call("bad", None).await;
    assert_eq!(status, 403);
    let spans = sink.wait_request(&id).await;
    let root = spans.iter().find(|s| s.name == "request").unwrap();
    assert_eq!(root.get("requested"), Some("bad"));
    assert_eq!(root.get("key_id"), Some("key-a"));
    assert_eq!(root.get("status"), Some("403"));
    assert_eq!(root.get("model"), Some(""));
}

/// A client asking not to be sampled still gets a record, in its own trace.
#[tokio::test]
async fn a_client_cannot_suppress_its_own_record() {
    let (sink, gateway) = setup().await;
    let trace_id = "0af7651916cd43dd8448eb211c80319c";
    let parent = "b7ad6b7169203331";
    let (status, id) = gateway
        .call("good", Some(&format!("00-{trace_id}-{parent}-00")))
        .await;
    assert_eq!(status, 200);
    let spans = sink.wait_request(&id).await;
    let root = spans.iter().find(|s| s.name == "request").unwrap();
    assert_eq!(root.trace_id, trace_id);
    assert_eq!(root.parent, parent);
}

// ---------------------------------------------------------------------------
// Span and usage event of the same request
// ---------------------------------------------------------------------------

/// Accepts requests and never answers: a client waiting on upstream headers.
struct GatedUpstream {
    addr: SocketAddr,
    arrived: mpsc::UnboundedReceiver<()>,
}

impl GatedUpstream {
    async fn start() -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let (tx, arrived) = mpsc::unbounded_channel();
        tokio::spawn(async move {
            while let Ok((stream, _)) = listener.accept().await {
                let tx = tx.clone();
                tokio::spawn(hyper::server::conn::http1::Builder::new().serve_connection(
                    TokioIo::new(stream),
                    service_fn(move |_: Request<hyper::body::Incoming>| {
                        let tx = tx.clone();
                        async move {
                            let _ = tx.send(());
                            std::future::pending::<()>().await;
                            Ok::<_, hyper::Error>(Response::new(Full::new(Bytes::new())))
                        }
                    }),
                ));
            }
        });
        Self { addr, arrived }
    }

    async fn request_arrived(&mut self) {
        bounded(self.arrived.recv())
            .await
            .expect("upstream request");
    }
}

const RESTRICTED: &str = "sk-otel-restricted";
const LIMITED: &str = "sk-otel-limited";

fn api_key(id: &str, secret: &str, models: &[&str], rate_limits: Vec<RateLimitScope>) -> ApiKey {
    ApiKey {
        key_id: id.into(),
        key_hash: Sha256::digest(secret).to_vec(),
        team_id: "team-a".into(),
        status: KeyStatus::Active as i32,
        expires_at_unix_ms: 0,
        policy: Some(EffectivePolicy {
            allow_all_models: models.is_empty(),
            allowed_models: models.iter().map(|m| m.to_string()).collect(),
            rate_limit_scopes: rate_limits,
            budget_window_end_unix_ms: 0,
            ..Default::default()
        }),
        ..Default::default()
    }
}

/// `to-held` -> router `hold` -> a model that never answers.
/// `to-failover` -> router `failover`: a dead model, then fallback to the held one.
/// `to-good` -> router `quick` -> a mock that answers.
fn catalog(good: SocketAddr, held: SocketAddr, dead: SocketAddr) -> Snapshot {
    let upstream = |name: &str, addr: SocketAddr| Upstream {
        name: name.into(),
        kind: UpstreamKind::Vllm as i32,
        base_url: format!("http://{addr}/v1"),
        timeout_ms: 20_000,
        ..Default::default()
    };
    let model = |name: &str, upstream: &str, actual: &str| Model {
        r#type: 1,
        name: name.into(),
        upstream: upstream.into(),
        upstream_model: actual.into(),
        ..Default::default()
    };
    let router = |name: &str, member: &str| Router {
        name: name.into(),
        members: vec![RouterMember {
            model: member.into(),
            weight: 1,
            ..Default::default()
        }],
        strategy: "weighted".into(),
        ..Default::default()
    };
    let alias = |name: &str, target: &str| Alias {
        name: name.into(),
        target: target.into(),
    };
    Snapshot {
        mcp_servers: vec![],
        a2a_agents: vec![],
        version: 7,
        upstreams: vec![
            upstream("good-up", good),
            upstream("held-up", held),
            upstream("dead-up", dead),
        ],
        models: vec![
            model("good", "good-up", "actual-good"),
            model("held", "held-up", "actual-held"),
            model("dead", "dead-up", "actual-dead"),
        ],
        routers: vec![
            router("quick", "good"),
            router("hold", "held"),
            Router {
                retry: Some(RetryPolicy {
                    max_attempts: 2,
                    ..Default::default()
                }),
                fallbacks: vec!["held".into()],
                ..router("failover", "dead")
            },
        ],
        aliases: vec![
            alias("to-good", "quick"),
            alias("to-held", "hold"),
            alias("to-failover", "failover"),
        ],
        api_keys: vec![
            api_key("key-all", SECRET, &[], Vec::new()),
            api_key("key-restricted", RESTRICTED, &["good"], Vec::new()),
            api_key(
                "key-limited",
                LIMITED,
                &["to-good"],
                vec![RateLimitScope {
                    id: "key:limited".into(),
                    limit: Some(RateLimit {
                        requests_per_minute: 1,
                        tokens_per_minute: 0,
                        max_parallel_requests: 0,
                    }),
                }],
            ),
        ],
        teams: vec![Team {
            id: "team-a".into(),
            name: "Team A".into(),
        }],
        created_at_unix_ms: 1,
        required_capabilities: vec![
            "strategy:weighted".into(),
            "retry".into(),
            "fallbacks".into(),
        ],
        scope: SCOPE.into(),
    }
}

struct Catalog {
    sink: Arc<Sink>,
    cp: FakeCp,
    held: GatedUpstream,
    gateway: Gateway,
}

async fn catalog_setup() -> Catalog {
    let (sink, sink_addr) = Sink::start().await;
    let good = mock(&[]).await;
    let held = GatedUpstream::start().await;
    let dead = std::net::TcpListener::bind("127.0.0.1:0")
        .unwrap()
        .local_addr()
        .unwrap();
    let cp = FakeCp::start(catalog(good, held.addr, dead)).await;
    let gateway = Gateway::start_cp(sink_addr, cp.addr, None, true).await;
    Catalog {
        sink,
        cp,
        held,
        gateway,
    }
}

/// The request span of `event`, checked field by field against it. An empty
/// usage field is a span field never recorded, except `model`, recorded empty on
/// a rejection.
async fn span_agrees_with(sink: &Sink, event: &UsageEvent) -> Span {
    let spans = sink.wait_request(&event.request_id).await;
    let root = spans.iter().find(|s| s.name == "request").unwrap().clone();
    for (field, value) in [
        ("requested", &event.requested),
        ("alias", &event.alias),
        ("router", &event.router),
        ("upstream", &event.upstream),
        ("upstream_model", &event.upstream_model),
    ] {
        let want = (!value.is_empty()).then_some(value.as_str());
        assert_eq!(root.get(field), want, "{field}: {root:?} {event:?}");
    }
    assert_eq!(root.get("model"), Some(event.model.as_str()), "{root:?}");
    let attempts = event.attempts.to_string();
    assert_eq!(root.get("attempts"), Some(attempts.as_str()), "{root:?}");
    let fallback = event.fallback.to_string();
    assert_eq!(root.get("fallback"), Some(fallback.as_str()), "{root:?}");
    let status = event.status.to_string();
    assert_eq!(root.get("status"), Some(status.as_str()), "{root:?}");
    assert_eq!(root.get("key_id"), Some(event.key_id.as_str()), "{root:?}");
    assert_eq!(root.repeated, Vec::<String>::new(), "{root:?}");
    root
}

#[tokio::test]
async fn forbidden_alias_keeps_provenance_in_the_trace_only() {
    let mut c = catalog_setup().await;
    let (status, id, known) = c.gateway.call_as(RESTRICTED, "to-held", None).await;
    assert_eq!(status, 403);
    let event = c.cp.event().await;
    assert_eq!(event.request_id, id);
    assert_eq!(event.outcome, RequestOutcome::Rejected as i32);
    assert_eq!(
        (event.alias.as_str(), event.router.as_str(), event.attempts),
        ("to-held", "hold", 0)
    );
    assert!(!event.fallback);
    let root = span_agrees_with(&c.sink, &event).await;
    assert_eq!(root.get("alias"), Some("to-held"));
    assert_eq!(root.get("router"), Some("hold"));
    assert_eq!(root.get("attempts"), Some("0"));
    assert_eq!(root.get("fallback"), Some("false"));
    // The caller learns nothing: a name that does not exist looks the same.
    let (status, id, unknown) = c.gateway.call_as(RESTRICTED, "no-such-name", None).await;
    assert_eq!(status, 403);
    assert_eq!(known, unknown);
    let event = c.cp.event().await;
    assert_eq!(event.request_id, id);
    let root = span_agrees_with(&c.sink, &event).await;
    assert_eq!(root.get("alias"), None);
    assert_eq!(root.get("router"), None);
}

#[tokio::test]
async fn unknown_name_records_no_attempts() {
    let mut c = catalog_setup().await;
    let (status, id, _) = c.gateway.call_as(SECRET, "no-such-name", None).await;
    assert_eq!(status, 404);
    let event = c.cp.event().await;
    assert_eq!(event.request_id, id);
    assert_eq!((event.attempts, event.fallback), (0, false));
    let root = span_agrees_with(&c.sink, &event).await;
    assert_eq!(root.get("attempts"), Some("0"));
    assert_eq!(root.get("fallback"), Some("false"));
}

#[tokio::test]
async fn rate_limited_call_records_no_attempts() {
    let mut c = catalog_setup().await;
    assert_eq!(c.gateway.call_as(LIMITED, "to-good", None).await.0, 200);
    c.cp.event().await;
    let (status, id, _) = c.gateway.call_as(LIMITED, "to-good", None).await;
    assert_eq!(status, 429);
    let event = c.cp.event().await;
    assert_eq!(event.request_id, id);
    assert_eq!((event.attempts, event.fallback), (0, false));
    assert_eq!(
        (event.alias.as_str(), event.router.as_str()),
        ("to-good", "quick")
    );
    let root = span_agrees_with(&c.sink, &event).await;
    assert_eq!(root.get("attempts"), Some("0"));
    assert_eq!(root.get("fallback"), Some("false"));
}

#[tokio::test]
async fn client_abort_during_the_first_attempt_keeps_that_attempt() {
    let mut c = catalog_setup().await;
    let socket = c.gateway.send_raw("to-held").await;
    c.held.request_arrived().await;
    drop(socket);
    let event = c.cp.event().await;
    assert_eq!(event.status, 499);
    assert_eq!(event.outcome, RequestOutcome::ClientCancelled as i32);
    assert_eq!(
        (event.upstream.as_str(), event.attempts, event.fallback),
        ("held-up", 1, false)
    );
    let root = span_agrees_with(&c.sink, &event).await;
    assert_eq!(root.get("upstream"), Some("held-up"));
    assert_eq!(root.get("upstream_model"), Some("actual-held"));
    assert_eq!(root.get("attempts"), Some("1"));
    assert_eq!(root.get("fallback"), Some("false"));
}

#[tokio::test]
async fn client_abort_during_the_fallback_attempt_keeps_that_attempt() {
    let mut c = catalog_setup().await;
    let socket = c.gateway.send_raw("to-failover").await;
    // The dead model failed; the fallback holds its headers.
    c.held.request_arrived().await;
    drop(socket);
    let event = c.cp.event().await;
    assert_eq!(event.status, 499);
    assert_eq!(
        (
            event.model.as_str(),
            event.upstream.as_str(),
            event.attempts,
            event.fallback
        ),
        ("held", "held-up", 2, true)
    );
    let root = span_agrees_with(&c.sink, &event).await;
    assert_eq!(root.get("upstream"), Some("held-up"));
    assert_eq!(root.get("upstream_model"), Some("actual-held"));
    assert_eq!(root.get("attempts"), Some("2"));
    assert_eq!(root.get("fallback"), Some("true"));
}

/// Without a snapshot: the response still leads to its trace, and no usage event
/// is made up for a key nobody could identify.
async fn not_ready_response_finds_its_trace(cached: Option<&[u8]>) {
    let (sink, sink_addr) = Sink::start().await;
    let mut cp = FakeCp::without_config().await;
    let gateway = Gateway::start_cp(sink_addr, cp.addr, cached, false).await;
    let (status, id) = gateway.call("anything", None).await;
    assert_eq!(status, 503);
    uuid::Uuid::parse_str(&id).unwrap();
    let spans = sink.wait_request(&id).await;
    let root = spans.iter().find(|s| s.name == "request").unwrap();
    assert_eq!(root.get("status"), Some("503"));
    assert_eq!(root.get("config_version"), Some("0"));
    assert_eq!(root.get("key_id"), None);
    assert_eq!(root.get("attempts"), Some("0"));
    assert_eq!(root.get("fallback"), Some("false"));
    assert_eq!(root.repeated, Vec::<String>::new(), "{root:?}");
    cp.no_more_events().await;
}

#[tokio::test]
async fn not_ready_with_no_cache() {
    not_ready_response_finds_its_trace(None).await;
}

#[tokio::test]
async fn not_ready_with_a_corrupt_cache() {
    not_ready_response_finds_its_trace(Some(b"LWSNAP02 this is not a snapshot")).await;
}
