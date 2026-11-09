//! MCP and A2A in the real gateway binary against an in-process fake control
//! plane: a snapshot with an MCP server and an A2A agent is ACKed, requests
//! reach the proxies through `X-Lumewarp-Key`, a bad name is NACKed, a delta
//! removes the targets, and nothing is sent on the stream that was not asked
//! for.
//!
//! `CatalogReport` (a report after registry entries appear) is covered where
//! entries can be produced: `src/config_stream.rs`'s `catalog` test drives the
//! real session loop against a fake `ConfigService`. W2a has no code path that
//! observes upstream yet (the MCP proxy is T4's, the A2A fetch T6's), so a child
//! process has no way to fill the registry. What this file can check is that no
//! spurious report is sent, which it does.

mod common;
#[path = "common/fake_cp.rs"]
mod fake_cp;
#[path = "common/usage_cp.rs"]
mod usage_cp;

use std::{
    net::{SocketAddr, TcpListener as StdListener},
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    time::Duration,
};

use bytes::Bytes;
use common::{bounded, bytes, client, TestClient};
use fake_cp::{Conn, FakeCp};
use http_body_util::{BodyExt, Full};
use hyper::Request;
use lumewarp_proto::config::v1::{
    config_update::Update, gateway_message::Msg, A2aAgent, ApiKey, ConfigUpdate, Delta,
    EffectivePolicy, KeyStatus, McpGrant, McpServer, NackCode, PluginRef, Snapshot, UpstreamAuth,
};
use lumewarp_proto::usage::v1::{RequestOutcome, TrafficKind, UsageEvent};
use prost::Message;
use sha2::{Digest, Sha256};

const SCOPE: &str = "scope-mcp";
/// base64 of a 32-byte `LUMEWARP_GATEWAY_ID_KEY` (`a2a_ids::MIN_KEY_BYTES`).
const ID_KEY: &str = "bHVtZXdhcnAtdGVzdC1nYXRld2F5LWlkLWtleS0zMmJ5";

fn key(id: &str, secret: &str, mcp: &[(&str, &[&str])], agents: &[&str]) -> ApiKey {
    ApiKey {
        key_id: id.into(),
        key_hash: Sha256::digest(secret.as_bytes()).to_vec(),
        team_id: String::new(),
        status: KeyStatus::Active as i32,
        expires_at_unix_ms: 0,
        policy: Some(EffectivePolicy {
            allow_all_models: true,
            mcp_grants: mcp
                .iter()
                .map(|(server, tools)| McpGrant {
                    server: (*server).into(),
                    all_tools: tools.is_empty(),
                    tools: tools.iter().map(|tool| (*tool).to_owned()).collect(),
                })
                .collect(),
            allowed_agents: agents.iter().map(|agent| (*agent).to_owned()).collect(),
            ..Default::default()
        }),
        ..Default::default()
    }
}

fn mcp_server(name: &str) -> McpServer {
    McpServer {
        id: format!("id-{name}"),
        name: name.into(),
        url: "http://127.0.0.1:1/mcp".into(),
        auth: None,
        timeout_ms: 0,
        review_mode: 0,
        definitions: Vec::new(),
    }
}

fn a2a_agent(name: &str) -> A2aAgent {
    A2aAgent {
        id: format!("id-{name}"),
        name: name.into(),
        url: "http://127.0.0.1:1/rpc".into(),
        card_url: String::new(),
        auth: None,
        middleware: None,
        timeout_ms: 0,
        card_json: String::new(),
    }
}

fn snapshot(version: u64) -> Snapshot {
    Snapshot {
        version,
        required_capabilities: vec!["mcp".into(), "a2a".into()],
        api_keys: vec![key("k1", "secret-1", &[("s1", &["echo"])], &["a1"])],
        mcp_servers: vec![mcp_server("s1")],
        a2a_agents: vec![a2a_agent("a1")],
        created_at_unix_ms: 1,
        scope: SCOPE.into(),
        ..Default::default()
    }
}

fn full(snapshot: Snapshot) -> ConfigUpdate {
    ConfigUpdate {
        target_digest: format!("digest-{}", snapshot.version).into_bytes(),
        update: Some(Update::Full(snapshot)),
    }
}

/// The persisted snapshot, without its file header.
fn on_disk(dir: &Path) -> Snapshot {
    let data = std::fs::read(dir.join("snapshot.pb")).expect("snapshot.pb");
    let (body, checksum) = data.split_at(data.len() - 32);
    assert_eq!(Sha256::digest(body).as_slice(), checksum);
    let (rest, len) = body.split_at(body.len() - 4);
    let rest = rest.strip_prefix(b"LWSNAP02").expect("magic");
    let len = u32::from_be_bytes(len.try_into().unwrap()) as usize;
    let (payload, _) = rest.split_at(rest.len() - len);
    Snapshot::decode(payload).unwrap()
}

struct Gw {
    child: Child,
    addr: SocketAddr,
    admin: SocketAddr,
    http: TestClient,
    dir: PathBuf,
}

impl Gw {
    fn spawn(control_plane: &str, state: &Path, work: &Path) -> Self {
        Self::spawn_with_env(control_plane, state, work, &[])
    }

    fn spawn_with_env(
        control_plane: &str,
        state: &Path,
        work: &Path,
        vars: &[(&str, &str)],
    ) -> Self {
        let listen = StdListener::bind("127.0.0.1:0").unwrap();
        let admin = StdListener::bind("127.0.0.1:0").unwrap();
        let (addr, admin_addr) = (listen.local_addr().unwrap(), admin.local_addr().unwrap());
        let local = work.join("local.yaml");
        std::fs::write(
            &local,
            format!("server:\n  listen: '{addr}'\n  admin_listen: '{admin_addr}'\n"),
        )
        .unwrap();
        drop((listen, admin));
        let child = Command::new(env!("CARGO_BIN_EXE_lumewarp"))
            .args(["--control-plane", control_plane])
            .args(["--gateway-token-env", "LUMEWARP_MCP_TEST_TOKEN"])
            .arg("--state-dir")
            .arg(state)
            .args(["--scope", SCOPE, "--gateway-id", "gw-mcp"])
            .arg("--local-config")
            .arg(local)
            .env("LUMEWARP_MCP_TEST_TOKEN", "gw-token")
            // The A2A endpoint needs both, or it answers 503 (P3).
            .env("LUMEWARP_GATEWAY_ID_KEY", ID_KEY)
            .env("LUMEWARP_GATEWAY_PUBLIC_URL", "https://gw.example.test")
            .env_remove("OTEL_EXPORTER_OTLP_ENDPOINT")
            .env_remove("LUMEWARP_AUTH_PLUGIN_DIR")
            .env_remove("LUMEWARP_A2A_MIDDLEWARE_DIR")
            .env_remove("LUMEWARP_ROUTER_PLUGIN_DIR")
            // Last, so a caller-supplied plugin directory is not removed above.
            .envs(vars.iter().copied())
            .env("RUST_LOG", "error")
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap();
        Self {
            child,
            addr,
            admin: admin_addr,
            http: client(),
            dir: state.to_owned(),
        }
    }

    async fn request(
        &self,
        admin: bool,
        method: &str,
        path: &str,
        headers: &[(&str, &str)],
    ) -> (u16, hyper::HeaderMap, Bytes) {
        let addr = if admin { self.admin } else { self.addr };
        let mut request = Request::builder()
            .method(method)
            .uri(format!("http://{addr}{path}"));
        for (name, value) in headers {
            request = request.header(*name, *value);
        }
        let request = request.body(Full::new(Bytes::new())).unwrap();
        let deadline = tokio::time::Instant::now() + Duration::from_secs(10);
        loop {
            match bounded(self.http.request(request.clone())).await {
                Ok(response) => {
                    let status = response.status().as_u16();
                    let headers = response.headers().clone();
                    return (status, headers, bytes(response).await);
                }
                Err(err) if tokio::time::Instant::now() > deadline => {
                    panic!("gateway unreachable: {err}")
                }
                Err(_) => tokio::time::sleep(Duration::from_millis(20)).await,
            }
        }
    }
}

impl Drop for Gw {
    fn drop(&mut self) {
        let _ = std::fs::set_permissions(&self.dir, std::fs::Permissions::from_mode(0o700));
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

/// The gateway compiles its WASM components at startup (seconds in a debug
/// build), so the first stream can take longer than the shared 5 s bound.
async fn accept(cp: &mut FakeCp) -> Conn {
    cp.accept_within(Duration::from_secs(120)).await
}

async fn hello(conn: &mut Conn) -> lumewarp_proto::config::v1::Hello {
    match conn.recv().await.msg {
        Some(Msg::Hello(hello)) => hello,
        other => panic!("expected Hello, got {other:?}"),
    }
}

async fn ack(conn: &mut Conn, version: u64) {
    match conn.recv().await.msg {
        Some(Msg::Ack(ack)) => assert_eq!(ack.version, version),
        other => panic!("expected Ack({version}), got {other:?}"),
    }
}

async fn nack(conn: &mut Conn, code: NackCode) {
    match conn.recv().await.msg {
        Some(Msg::Nack(nack)) => assert_eq!(nack.code(), code, "{}", nack.reason),
        other => panic!("expected Nack, got {other:?}"),
    }
}

struct Env {
    _tmp: tempfile::TempDir,
    state: PathBuf,
    work: PathBuf,
}

fn env() -> Env {
    let tmp = tempfile::tempdir().unwrap();
    let work = tmp.path().to_owned();
    let state = work.join("state");
    Env {
        _tmp: tmp,
        state,
        work,
    }
}

/// The `upstream-auth` fixture components, built by
/// `bash gateway/plugins/build.sh --tests`.
fn auth_plugin_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../target/plugins-test/auth")
}

/// F2: T5's compile hook runs at apply time, so a plugin `configure` that fails
/// is a NACK for the version instead of a 502 on the first request.
#[tokio::test]
async fn a_refused_auth_configure_nacks_the_version() {
    let dir = auth_plugin_dir();
    if !dir.is_dir() {
        eprintln!("SKIP: build components with bash gateway/plugins/build.sh --tests");
        return;
    }
    let dir = dir.to_str().unwrap().to_owned();
    let env = env();
    let mut cp = FakeCp::start().await;
    let gw = Gw::spawn_with_env(
        &cp.url(),
        &env.state,
        &env.work,
        &[("LUMEWARP_AUTH_PLUGIN_DIR", &dir)],
    );
    let mut conn = accept(&mut cp).await;
    let hello = hello(&mut conn).await;
    assert!(
        hello
            .supported_capabilities
            .iter()
            .any(|cap| cap == "auth:auth_fixture"),
        "the loaded plugin is reported: {:?}",
        hello.supported_capabilities
    );

    // A snapshot without the plugin configures fine.
    conn.send(full(snapshot(1)));
    ack(&mut conn, 1).await;

    // `fixture` in `fetch` mode needs a `url`; its `configure` refuses.
    let with_plugin = |version: u64, params: &str| {
        let mut s = snapshot(version);
        s.required_capabilities.push("auth:auth_fixture".into());
        s.mcp_servers[0].auth = Some(UpstreamAuth {
            plugin: Some(PluginRef {
                plugin: "auth_fixture".into(),
                params_json: params.into(),
                ..Default::default()
            }),
            ..Default::default()
        });
        s
    };
    conn.send(full(with_plugin(2, r#"{"mode":"fetch"}"#)));
    nack(&mut conn, NackCode::InvalidConfig).await;
    assert_eq!(on_disk(&env.state).version, 1, "nothing was swapped");
    // The previous configuration keeps serving (the MCP transport rejects a
    // bare request; it is not an authentication failure).
    assert_ne!(
        gw.request(false, "POST", "/mcp", &[("x-lumewarp-key", "secret-1")])
            .await
            .0,
        401,
        "the previous configuration keeps serving"
    );

    // A plugin that configures is accepted, so the hook is not refusing
    // everything.
    conn.send(full(with_plugin(3, r#"{"mode":"fixed"}"#)));
    ack(&mut conn, 3).await;
    assert_eq!(on_disk(&env.state).version, 3);
}

// ---------------------------------------------------------------------------
// Streaming usage (I2): one final event per RPC
// ---------------------------------------------------------------------------

/// `mcp-a2a-mock`, built by `cargo build` in `bench/mock-upstream`.
fn a2a_mock_bin() -> Option<PathBuf> {
    let root =
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../bench/mock-upstream/target");
    ["release", "debug"]
        .iter()
        .map(|profile| root.join(profile).join("mcp-a2a-mock"))
        .find(|path| path.exists())
}

struct Mock(Child);
impl Drop for Mock {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

fn free_addr() -> SocketAddr {
    let listener = StdListener::bind("127.0.0.1:0").unwrap();
    listener.local_addr().unwrap()
}

/// An A2A mock agent that streams several events `step` apart.
fn start_mock(bin: &Path, addr: SocketAddr, step_ms: u32) -> Mock {
    let child = Command::new(bin)
        .args([
            "a2a",
            "--listen",
            &addr.to_string(),
            "--auth",
            "none",
            "--stream-step-ms",
            &step_ms.to_string(),
        ])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .expect("start mcp-a2a-mock");
    Mock(child)
}

/// A snapshot with one streaming A2A agent and a key that may call it.
fn streaming_snapshot(agent: SocketAddr) -> Snapshot {
    Snapshot {
        version: 1,
        required_capabilities: vec!["a2a".into()],
        api_keys: vec![key("k1", "secret-1", &[], &["helper"])],
        a2a_agents: vec![A2aAgent {
            id: "id-helper".into(),
            name: "helper".into(),
            url: format!("http://{agent}/a2a"),
            ..Default::default()
        }],
        created_at_unix_ms: 1,
        scope: SCOPE.into(),
        ..Default::default()
    }
}

fn streaming_request(gw: &Gw) -> Request<Full<Bytes>> {
    Request::builder()
        .method("POST")
        .uri(format!("http://{}/a2a/helper", gw.addr))
        .header("x-lumewarp-key", "secret-1")
        .header("content-type", "application/json")
        .header("a2a-version", "1.0")
        .body(Full::new(Bytes::from_static(
            br#"{"jsonrpc":"2.0","id":1,"method":"SendStreamingMessage","params":{"message":{"messageId":"m1","role":"ROLE_USER","parts":[{"text":"hi","mediaType":"text/plain"}]}}}"#,
        )))
        .unwrap()
}

/// Waits until the gateway has applied the snapshot (the proxy itself needs it).
async fn wait_ready(gw: &Gw) {
    let deadline = tokio::time::Instant::now() + Duration::from_secs(60);
    loop {
        let (status, _, _) = gw
            .request(
                false,
                "GET",
                "/v1/models",
                &[("authorization", "Bearer secret-1")],
            )
            .await;
        if status == 200 {
            return;
        }
        assert!(
            tokio::time::Instant::now() < deadline,
            "the gateway never applied the snapshot"
        );
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
}

async fn next_usage_event(cp: &mut usage_cp::FakeCp, what: &str) -> UsageEvent {
    let (authorization, event) = tokio::time::timeout(Duration::from_secs(10), cp.events.recv())
        .await
        .unwrap_or_else(|_| panic!("no usage event for the {what} stream"))
        .expect("fake control plane stopped");
    assert_eq!(authorization.as_deref(), Some("Bearer gw-token"));
    event
}

/// I2: a stream is reported when it ends, with the real outcome and duration —
/// not at handoff with `status: 200, COMPLETED`. A finished stream is one
/// `COMPLETED` event after the last byte; a client that goes away mid-stream is
/// `CLIENT_CANCELLED` with the LLM path's 499, also exactly once.
#[tokio::test]
async fn a_stream_reports_one_final_usage_event() {
    let Some(bin) = a2a_mock_bin() else {
        eprintln!("SKIP: build the mock with cargo build in bench/mock-upstream");
        return;
    };
    let agent = free_addr();
    let _mock = start_mock(&bin, agent, 60);
    let env = env();
    let mut cp = usage_cp::FakeCp::start(streaming_snapshot(agent)).await;
    let url = format!("http://{}", cp.addr);
    let gw = Gw::spawn_with_env(&url, &env.state, &env.work, &[]);
    // The config stream ACKs the snapshot before the proxy is ready.
    wait_ready(&gw).await;

    // A stream read to the end: one event, after the stream, `COMPLETED`.
    let response = bounded(gw.http.request(streaming_request(&gw)))
        .await
        .expect("streaming request");
    assert_eq!(response.status(), 200);
    let body = bounded(response.into_body().collect())
        .await
        .expect("read the stream")
        .to_bytes();
    assert!(
        String::from_utf8_lossy(&body).contains("TASK_STATE_COMPLETED"),
        "the mock finished its stream: {}",
        String::from_utf8_lossy(&body)
    );
    let event = next_usage_event(&mut cp, "completed").await;
    assert_eq!(event.kind, TrafficKind::A2a as i32);
    assert_eq!(event.endpoint, "a2a");
    assert_eq!(event.target, "helper");
    assert_eq!(event.method, "SendStreamingMessage");
    assert!(event.stream, "a stream, not a handoff record");
    assert_eq!(event.status, 200);
    assert_eq!(event.outcome, RequestOutcome::Completed as i32);
    assert!(event.duration_ms > 0, "the duration is the stream's");
    assert_eq!(event.key_id, "k1");
    cp.no_more_events().await;

    // A client that stops reading mid-stream: the record is the LLM path's
    // cancellation, with the synthetic 499, and it is the only one.
    let mut response = bounded(gw.http.request(streaming_request(&gw)))
        .await
        .expect("streaming request");
    assert_eq!(response.status(), 200);
    // Read the first chunk, then drop the body without draining it.
    let _ = tokio::time::timeout(Duration::from_secs(5), response.body_mut().frame()).await;
    drop(response);
    let event = next_usage_event(&mut cp, "cancelled").await;
    assert_eq!(event.kind, TrafficKind::A2a as i32);
    assert_eq!(event.target, "helper");
    assert!(event.stream);
    assert_eq!(
        (event.status, event.outcome),
        (499, RequestOutcome::ClientCancelled as i32)
    );
    cp.no_more_events().await;
}

/// The MCP mock server of the same binary.
fn start_mcp_mock(bin: &Path, addr: SocketAddr) -> Mock {
    let child = Command::new(bin)
        .args(["mcp", "--listen", &addr.to_string(), "--auth", "none"])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .expect("start mcp-a2a-mock mcp");
    Mock(child)
}

fn mcp_snapshot(server: SocketAddr) -> Snapshot {
    Snapshot {
        version: 1,
        required_capabilities: vec!["mcp".into()],
        api_keys: vec![key("k1", "secret-1", &[("s1", &[])], &[])],
        mcp_servers: vec![McpServer {
            id: "id-s1".into(),
            name: "s1".into(),
            url: format!("http://{server}/mcp"),
            ..Default::default()
        }],
        created_at_unix_ms: 1,
        scope: SCOPE.into(),
        ..Default::default()
    }
}

/// A `/mcp` request with the headers the transport requires.
fn mcp_request(gw: &Gw, session: Option<&str>, value: serde_json::Value) -> Request<Full<Bytes>> {
    let mut request = Request::builder()
        .method("POST")
        .uri(format!("http://{}/mcp", gw.addr))
        .header("x-lumewarp-key", "secret-1")
        .header("accept", "application/json, text/event-stream")
        .header("content-type", "application/json");
    if let Some(session) = session {
        request = request
            .header("mcp-session-id", session)
            .header("mcp-protocol-version", "2025-11-25");
    }
    request
        .body(Full::new(Bytes::from(value.to_string())))
        .unwrap()
}

/// `initialize` + `notifications/initialized`; returns the gateway session id.
async fn mcp_initialize(gw: &Gw) -> String {
    let response = bounded(gw.http.request(mcp_request(
        gw,
        None,
        serde_json::json!({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        }),
    )))
    .await
    .expect("initialize");
    assert_eq!(response.status(), 200);
    let session = response.headers()["mcp-session-id"]
        .to_str()
        .unwrap()
        .to_owned();
    let _ = bounded(response.into_body().collect()).await;
    let initialized = bounded(gw.http.request(mcp_request(
        gw,
        Some(&session),
        serde_json::json!({"jsonrpc": "2.0", "method": "notifications/initialized"}),
    )))
    .await
    .expect("initialized");
    assert_eq!(initialized.status(), 202);
    session
}

/// `tools/call` on the aggregate endpoint, streaming progress over SSE.
fn mcp_slow_call(gw: &Gw, session: &str) -> Request<Full<Bytes>> {
    mcp_request(
        gw,
        Some(session),
        serde_json::json!({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "s1__slow",
                "arguments": {"delay_ms": 60, "steps": 4},
                "_meta": {"progressToken": "p1"},
            },
        }),
    )
}

/// I2: the aggregate MCP SSE is reported when the stream ends too — a finished
/// stream is one `COMPLETED` event (target/tool are the granted server and the
/// upstream tool, not the qualified `<server>__<tool>` name), and a client that
/// goes away mid-stream is one `CLIENT_CANCELLED`.
#[tokio::test]
async fn an_mcp_stream_reports_one_final_usage_event() {
    let Some(bin) = a2a_mock_bin() else {
        eprintln!("SKIP: build the mock with cargo build in bench/mock-upstream");
        return;
    };
    let server = free_addr();
    let _mock = start_mcp_mock(&bin, server);
    let env = env();
    let mut cp = usage_cp::FakeCp::start(mcp_snapshot(server)).await;
    let url = format!("http://{}", cp.addr);
    let gw = Gw::spawn_with_env(&url, &env.state, &env.work, &[]);
    wait_ready(&gw).await;
    let session = mcp_initialize(&gw).await;
    // `initialize` is not a stream: it is still reported at handoff.
    let event = next_usage_event(&mut cp, "initialize").await;
    assert_eq!(event.method, "initialize");
    assert!(!event.stream);
    assert_eq!(event.outcome, RequestOutcome::Completed as i32);

    let response = bounded(gw.http.request(mcp_slow_call(&gw, &session)))
        .await
        .expect("tools/call");
    assert_eq!(response.status(), 200);
    let body = bounded(response.into_body().collect())
        .await
        .expect("read the stream")
        .to_bytes();
    assert!(
        String::from_utf8_lossy(&body).contains("slept"),
        "the mock finished its stream: {}",
        String::from_utf8_lossy(&body)
    );
    let event = next_usage_event(&mut cp, "completed").await;
    assert_eq!(event.kind, TrafficKind::Mcp as i32);
    assert_eq!(event.endpoint, "mcp");
    assert_eq!(event.target, "s1");
    assert_eq!(event.method, "tools/call");
    assert_eq!(event.tool, "slow");
    assert!(event.stream);
    assert_eq!(
        (event.status, event.outcome),
        (200, RequestOutcome::Completed as i32)
    );
    assert!(event.duration_ms > 0);
    cp.no_more_events().await;

    // Drop the body after the first chunk: the pump notices the client is gone
    // and records the cancellation, once.
    let mut response = bounded(gw.http.request(mcp_slow_call(&gw, &session)))
        .await
        .expect("tools/call");
    assert_eq!(response.status(), 200);
    let _ = tokio::time::timeout(Duration::from_secs(5), response.body_mut().frame()).await;
    drop(response);
    let event = next_usage_event(&mut cp, "cancelled").await;
    assert_eq!(event.method, "tools/call");
    assert_eq!(event.tool, "slow");
    assert!(event.stream);
    assert_eq!(
        (event.status, event.outcome),
        (499, RequestOutcome::ClientCancelled as i32)
    );
    cp.no_more_events().await;
}

/// The A2A middleware fixture, built by `bash gateway/plugins/build.sh --tests`.
fn middleware_plugin_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../target/plugins-test/a2a-middleware")
}

/// I2: the middleware compile hook runs at apply time too, so a refused
/// `configure` is a NACK for the version instead of a 502 on the first request.
#[tokio::test]
async fn a_refused_middleware_configure_nacks_the_version() {
    let dir = middleware_plugin_dir();
    if !dir.join("misbehave.wasm").exists() {
        eprintln!("SKIP: build components with bash gateway/plugins/build.sh --tests");
        return;
    }
    let dir = dir.to_str().unwrap().to_owned();
    let env = env();
    let mut cp = FakeCp::start().await;
    let gw = Gw::spawn_with_env(
        &cp.url(),
        &env.state,
        &env.work,
        &[("LUMEWARP_A2A_MIDDLEWARE_DIR", &dir)],
    );
    let mut conn = accept(&mut cp).await;
    let hello = hello(&mut conn).await;
    assert!(
        hello
            .supported_capabilities
            .iter()
            .any(|cap| cap == "a2a_middleware:misbehave_test"),
        "the loaded middleware is reported: {:?}",
        hello.supported_capabilities
    );
    conn.send(full(snapshot(1)));
    ack(&mut conn, 1).await;

    let with_middleware = |version: u64, mode: &str| {
        let mut s = snapshot(version);
        s.required_capabilities
            .push("a2a_middleware:misbehave_test".into());
        s.a2a_agents[0].middleware = Some(PluginRef {
            plugin: "misbehave_test".into(),
            params_json: format!(r#"{{"mode":"{mode}"}}"#),
            ..Default::default()
        });
        s
    };
    // The fixture refuses an unknown mode in `configure`.
    conn.send(full(with_middleware(2, "bogus")));
    nack(&mut conn, NackCode::InvalidConfig).await;
    assert_eq!(on_disk(&env.state).version, 1, "nothing was swapped");
    assert_ne!(
        gw.request(false, "POST", "/a2a/a1", &[("x-lumewarp-key", "secret-1")])
            .await
            .0,
        401,
        "the previous configuration keeps serving"
    );
    // One that configures is accepted.
    conn.send(full(with_middleware(3, "pass")));
    ack(&mut conn, 3).await;
    assert_eq!(on_disk(&env.state).version, 3);
}

#[tokio::test]
async fn mcp_and_a2a_targets_round_trip_through_the_config_stream() {
    let env = env();
    let mut cp = FakeCp::start().await;
    let gw = Gw::spawn(&cp.url(), &env.state, &env.work);
    let mut conn = accept(&mut cp).await;
    let hello = hello(&mut conn).await;
    // Both capabilities this gateway serves are advertised in Hello.
    for cap in ["mcp", "a2a"] {
        assert!(
            hello.supported_capabilities.iter().any(|c| c == cap),
            "{cap} missing from {:?}",
            hello.supported_capabilities
        );
    }

    conn.send(full(snapshot(1)));
    ack(&mut conn, 1).await;
    let persisted = on_disk(&env.state);
    assert_eq!(persisted.mcp_servers.len(), 1);
    assert_eq!(persisted.a2a_agents.len(), 1);

    // A keyed request reaches the MCP proxy; the key is `X-Lumewarp-Key`, not a
    // bearer. Without one, 401.
    let key = ("x-lumewarp-key", "secret-1");
    let (status, headers, _) = gw.request(false, "POST", "/mcp", &[key]).await;
    assert_ne!(status, 401, "a valid key is not rejected");
    assert!(headers.contains_key("x-lumewarp-request-id"));
    assert_eq!(gw.request(false, "POST", "/mcp", &[]).await.0, 401);
    assert_eq!(
        gw.request(
            false,
            "POST",
            "/mcp",
            &[("authorization", "Bearer secret-1")]
        )
        .await
        .0,
        401
    );
    // Every MCP and A2A route, every method.
    for (method, path) in [
        ("POST", "/mcp/servers/s1"),
        ("GET", "/a2a"),
        ("POST", "/a2a/a1"),
        ("GET", "/a2a/a1/.well-known/agent-card.json"),
    ] {
        let (status, headers, _) = gw.request(false, method, path, &[key]).await;
        assert_ne!(status, 401, "{method} {path} with a valid key");
        assert!(headers.contains_key("x-lumewarp-request-id"));
        assert_eq!(gw.request(false, method, path, &[]).await.0, 401);
    }
    // The LLM path is untouched by the new routes.
    assert_eq!(
        gw.request(false, "GET", "/v1/models", &[key]).await.0,
        401,
        "X-Lumewarp-Key is not the LLM credential"
    );
    assert_eq!(
        gw.request(
            false,
            "GET",
            "/v1/models",
            &[("authorization", "Bearer secret-1")]
        )
        .await
        .0,
        200
    );

    // A server name with `__` is INVALID_CONFIG (L4) and nothing is swapped.
    let mut bad = snapshot(2);
    bad.mcp_servers[0].name = "s__1".into();
    conn.send(full(bad));
    nack(&mut conn, NackCode::InvalidConfig).await;
    assert_ne!(gw.request(false, "POST", "/mcp", &[key]).await.0, 401);
    assert_eq!(on_disk(&env.state).version, 1);

    // A capability the gateway cannot serve (a plugin it has not loaded) is
    // UNSUPPORTED_FEATURE rather than a target being silently dropped.
    let mut unsupported = snapshot(2);
    unsupported
        .required_capabilities
        .push("auth:hmac_signature".into());
    unsupported.mcp_servers[0].auth = Some(UpstreamAuth {
        plugin: Some(PluginRef {
            plugin: "hmac_signature".into(),
            params_json: String::new(),
            ..Default::default()
        }),
        secret_ref: String::new(),
        secret_ciphertext: Vec::new(),
    });
    conn.send(full(unsupported));
    nack(&mut conn, NackCode::UnsupportedFeature).await;

    // A delta removes both targets; the gateway keeps serving and the persisted
    // snapshot no longer configures them.
    conn.send(ConfigUpdate {
        target_digest: b"digest-3".to_vec(),
        update: Some(Update::Delta(Delta {
            base_version: 1,
            version: 3,
            remove_mcp_servers: vec!["s1".into()],
            remove_a2a_agents: vec!["a1".into()],
            required_capabilities: Vec::new(),
            created_at_unix_ms: 3,
            ..Default::default()
        })),
    });
    ack(&mut conn, 3).await;
    let persisted = on_disk(&env.state);
    assert!(persisted.mcp_servers.is_empty());
    assert!(persisted.a2a_agents.is_empty());
    // Routes still answer (the endpoint does not depend on a target existing;
    // resolving a name is the proxy's job).
    assert_ne!(gw.request(false, "POST", "/mcp", &[key]).await.0, 401);
    assert_ne!(
        gw.request(false, "GET", "/a2a/a1/.well-known/agent-card.json", &[key])
            .await
            .0,
        401
    );

    // Nothing was ever reported that was not observed (no empty report, no
    // report from applying a snapshot).
    assert!(conn.catalog.is_empty(), "{:?}", conn.catalog);
}
