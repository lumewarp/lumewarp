//! Real MCP mock binary → gateway child processes, without a control-plane dependency.
#[path = "../src/a2a_ids.rs"]
mod a2a_ids;
mod common;
#[path = "mcp/regressions.rs"]
mod regressions;
#[allow(dead_code)]
#[path = "../src/mcp/session.rs"]
mod sealed_session;
use bytes::Bytes;
use common::{bounded, bytes, client, TestClient};
use http_body_util::{BodyExt, Full};
use hyper::{body::Incoming, Request, Response};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    net::{SocketAddr, TcpListener},
    path::PathBuf,
    process::{Child, Command, Stdio},
    time::Duration,
};
const KEY: &str = "bHVtZXdhcnAtdGVzdC1nYXRld2F5LWlkLWtleS0zMmJ5";
fn address() -> SocketAddr {
    TcpListener::bind("127.0.0.1:0")
        .unwrap()
        .local_addr()
        .unwrap()
}
struct Process(Child);
impl Drop for Process {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}
struct Mock {
    _process: Process,
    addr: SocketAddr,
}
impl Mock {
    async fn start(page: usize) -> Self {
        Self::start_auth(page, "none").await
    }
    async fn start_auth(page: usize, auth: &str) -> Self {
        let addr = address();
        let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../../bench/mock-upstream/target/debug/mcp-a2a-mock");
        assert!(path.exists(), "Build the mock first: cargo build --manifest-path bench/mock-upstream/Cargo.toml --bin mcp-a2a-mock");
        let child = Command::new(path)
            .args([
                "mcp",
                "--listen",
                &addr.to_string(),
                "--tools-page-size",
                &page.to_string(),
                "--auth",
                auth,
                "--hmac-secret",
                "mock-hmac-secret",
            ])
            .stdout(Stdio::null())
            .stderr(Stdio::inherit())
            .spawn()
            .unwrap();
        let mock = Self {
            _process: Process(child),
            addr,
        };
        ready(addr).await;
        mock
    }
}
struct Gw {
    _process: Process,
    _dir: tempfile::TempDir,
    addr: SocketAddr,
    admin: SocketAddr,
    http: TestClient,
}
impl Gw {
    async fn start(servers: &[(&str, SocketAddr)], rpm: u64) -> Self {
        Self::start_auth(servers, rpm, "{plugin: none}").await
    }
    async fn start_auth(servers: &[(&str, SocketAddr)], rpm: u64, auth: &str) -> Self {
        Self::start_auth_with_first_admin(servers, rpm, auth, None).await
    }
    async fn start_auth_with_first_admin(
        servers: &[(&str, SocketAddr)],
        rpm: u64,
        auth: &str,
        first_admin: Option<SocketAddr>,
    ) -> Self {
        let mut conflict = String::new();
        for attempt in 0..5 {
            match Self::try_start_auth(
                servers,
                rpm,
                auth,
                if attempt == 0 { first_admin } else { None },
            )
            .await
            {
                Ok(gw) => return gw,
                Err(error) => conflict = error,
            }
        }
        panic!("gateway listener ports repeatedly claimed before child bind: {conflict}");
    }
    async fn try_start_auth(
        servers: &[(&str, SocketAddr)],
        rpm: u64,
        auth: &str,
        forced_admin: Option<SocketAddr>,
    ) -> Result<Self, String> {
        // Reserve both ports together through config preparation. A concurrent
        // process may still claim one after release while WASM prewarms, so
        // only an explicit EADDRINUSE startup failure is retryable.
        let listen = TcpListener::bind("127.0.0.1:0").unwrap();
        let admin_listen = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listen.local_addr().unwrap();
        let admin = forced_admin.unwrap_or_else(|| admin_listen.local_addr().unwrap());
        let dir = tempfile::tempdir().unwrap();
        let servers = servers
            .iter()
            .map(|(name, addr)| {
                format!("  - name: {name}\n    url: http://{addr}/mcp\n    timeout_ms: 1000\n    auth: {auth}\n")
            })
            .collect::<String>();
        let config = format!("server:\n  listen: '{addr}'\n  admin_listen: '{admin}'\nmcp_servers:\n{servers}keys:\n  - key_id: one\n    key_sha256: '{}'\n    allow_all_mcp: true\n    rate_limits: {{requests_per_minute: {rpm}}}\n  - key_id: two\n    key_sha256: '{}'\n    mcp_grants: [{{server: s1, tools: [echo]}}]\n", hex::encode(Sha256::digest(b"secret")),hex::encode(Sha256::digest(b"other")));
        let path = dir.path().join("gateway.yaml");
        std::fs::write(&path, config).unwrap();
        let plugins =
            PathBuf::from(env!("CARGO_MANIFEST_DIR")).join(if auth.contains("auth_fixture") {
                "../../target/plugins-test/auth"
            } else {
                "../../target/plugins/auth"
            });
        if auth.contains("auth_fixture") {
            assert!(
                plugins.join("fixture.wasm").exists(),
                "Build auth fixtures: bash gateway/plugins/build.sh --tests"
            );
        }
        let log = dir.path().join("gateway.log");
        let stderr = std::fs::File::create(&log).unwrap();
        drop((listen, admin_listen));
        let child = Command::new(env!("CARGO_BIN_EXE_lumewarp"))
            .arg("--config")
            .arg(path)
            .env("LUMEWARP_GATEWAY_ID_KEY", KEY)
            .env_remove("LUMEWARP_GATEWAY_PUBLIC_URL")
            .env_remove("OTEL_EXPORTER_OTLP_ENDPOINT")
            .env_remove("LUMEWARP_ROUTER_PLUGIN_DIR")
            .env("LUMEWARP_AUTH_PLUGIN_DIR", plugins)
            .env("MCP_TEST_STATIC_TOKEN", "static-test-token")
            .env("LUMEWARP_PLUGIN_SECRET_MCP_TEST", "mock-hmac-secret")
            .env_remove("LUMEWARP_A2A_MIDDLEWARE_DIR")
            .stdout(Stdio::null())
            .stderr(Stdio::from(stderr))
            .spawn()
            .unwrap();
        let mut gw = Self {
            _process: Process(child),
            _dir: dir,
            addr,
            admin,
            http: client(),
        };
        let deadline = tokio::time::Instant::now() + Duration::from_secs(20);
        while tokio::time::Instant::now() < deadline {
            if let Some(status) = gw._process.0.try_wait().unwrap() {
                let text = std::fs::read_to_string(&log).unwrap();
                if text.contains("Address already in use") && text.contains("binding") {
                    return Err(text);
                }
                panic!("gateway exited during startup ({status}): {text}");
            }
            if tokio::net::TcpStream::connect(addr).await.is_ok() {
                let request = Request::get(format!("http://{admin}/healthz"))
                    .body(Full::new(Bytes::new()))
                    .unwrap();
                if matches!(tokio::time::timeout(Duration::from_millis(250), gw.http.request(request)).await,
                    Ok(Ok(response)) if response.status() == 200)
                {
                    return Ok(gw);
                }
            }
            tokio::time::sleep(Duration::from_millis(25)).await;
        }
        panic!(
            "gateway not ready: {}",
            std::fs::read_to_string(log).unwrap()
        );
    }
    async fn metrics(&self) -> String {
        let request = Request::get(format!("http://{}/metrics", self.admin))
            .body(Full::new(Bytes::new()))
            .unwrap();
        String::from_utf8(
            bytes(bounded(self.http.request(request)).await.unwrap())
                .await
                .to_vec(),
        )
        .unwrap()
    }
    async fn raw(
        &self,
        path: &str,
        method: &str,
        session: Option<&str>,
        secret: &str,
        body: Bytes,
    ) -> Response<Incoming> {
        let mut req = Request::builder()
            .uri(format!("http://{}{path}", self.addr))
            .method(method)
            .header("x-lumewarp-key", secret)
            .header("authorization", "Bearer client-test-token")
            .header("cookie", "session=private")
            .header("accept", "application/json, text/event-stream")
            .header("content-type", "application/json");
        if let Some(session) = session {
            req = req
                .header("mcp-session-id", session)
                .header("mcp-protocol-version", "2025-11-25");
        }
        bounded(self.http.request(req.body(Full::new(body)).unwrap()))
            .await
            .unwrap()
    }
    async fn rpc(
        &self,
        path: &str,
        session: Option<&str>,
        secret: &str,
        value: Value,
    ) -> (u16, Value) {
        let response = self
            .raw(
                path,
                "POST",
                session,
                secret,
                Bytes::from(value.to_string()),
            )
            .await;
        let status = response.status().as_u16();
        let body = bytes(response).await;
        (
            status,
            if body.is_empty() {
                Value::Null
            } else {
                serde_json::from_slice(&body).unwrap()
            },
        )
    }
    async fn initialize(&self, path: &str, secret: &str) -> String {
        let response = self.raw(path,"POST",None,secret,Bytes::from(json!({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{"elicitation":{}},"clientInfo":{"name":"test","version":"1"}}}).to_string())).await;
        assert_eq!(response.status(), 200, "{}", String::from_utf8_lossy(&[]));
        let session = response.headers()["mcp-session-id"]
            .to_str()
            .unwrap()
            .to_owned();
        let _ = bytes(response).await;
        assert_eq!(
            self.rpc(
                path,
                Some(&session),
                secret,
                json!({"jsonrpc":"2.0","method":"notifications/initialized"})
            )
            .await
            .0,
            202
        );
        session
    }
}
async fn ready(addr: SocketAddr) {
    // Wasmtime compiles the larger test auth fixture during process startup.
    for _ in 0..800 {
        if tokio::net::TcpStream::connect(addr).await.is_ok() {
            return;
        }
        tokio::time::sleep(Duration::from_millis(25)).await;
    }
    panic!("process not ready at {addr}");
}
async fn list(gw: &Gw, session: &str, secret: &str) -> Vec<String> {
    let mut names = vec![];
    let mut cursor = None;
    for _ in 0..30 {
        let mut params = json!({});
        if let Some(c) = cursor.take() {
            params["cursor"] = c;
        }
        let (status, v) = gw
            .rpc(
                "/mcp",
                Some(session),
                secret,
                json!({"jsonrpc":"2.0","id":2,"method":"tools/list","params":params}),
            )
            .await;
        assert_eq!(status, 200, "{v}");
        names.extend(
            v["result"]["tools"]
                .as_array()
                .unwrap()
                .iter()
                .map(|v| v["name"].as_str().unwrap().to_owned()),
        );
        cursor = v["result"].get("nextCursor").cloned();
        if cursor.is_none() {
            return names;
        }
    }
    panic!("cursor did not terminate");
}
#[tokio::test]
async fn server_discover_advertises_the_handshake_era_without_a_session() {
    let mock = Mock::start(2).await;
    let gw = Gw::start(&[("s1", mock.addr)], 0).await;
    let discover = json!({"jsonrpc":"2.0","id":"d1","method":"server/discover","params":{}});
    for route in ["/mcp", "/mcp/servers/s1"] {
        let (status, body) = gw.rpc(route, None, "secret", discover.clone()).await;
        assert_eq!(status, 200, "{route}: {body}");
        assert_eq!(body["id"], "d1");
        let versions = body["result"]["supportedVersions"].as_array().unwrap();
        assert!(versions.contains(&json!("2025-11-25")), "{body}");
        assert!(!versions.contains(&json!("2026-07-28")), "{body}");
        assert!(body["result"]["capabilities"].is_object(), "{body}");
    }
    let (status, _) = gw.rpc("/mcp/servers/nope", None, "secret", discover).await;
    assert_eq!(status, 404);
    // The probe leaves no state: the handshake still works afterwards.
    let s = gw.initialize("/mcp", "secret").await;
    assert!(list(&gw, &s, "secret").await.contains(&"s1__echo".into()));
}

#[tokio::test]
async fn aggregate_sessions_grants_pagination_rug_pull_and_reverse_rpc() {
    let mock1 = Mock::start(2).await;
    let mock2 = Mock::start(2).await;
    let servers = [
        ("s1", mock1.addr),
        ("s2", mock2.addr),
        ("broken", address()),
    ];
    let gw = Gw::start(&servers, 0).await;
    let replica = Gw::start(&servers, 0).await;
    let s = gw.initialize("/mcp", "secret").await;
    let names = list(&replica, &s, "secret").await;
    assert!(names.contains(&"s1__echo".into()) && names.contains(&"s2__echo".into()));
    assert!(names.iter().all(|n| !n.starts_with("broken__")));
    let (status, prompts) = replica
        .rpc(
            "/mcp",
            Some(&s),
            "secret",
            json!({"jsonrpc":"2.0","id":20,"method":"prompts/list"}),
        )
        .await;
    assert_eq!(status, 200);
    assert_eq!(prompts["result"]["prompts"][0]["name"], "s1__greet");
    let (status,prompt) = replica.rpc("/mcp",Some(&s),"secret",json!({"jsonrpc":"2.0","id":21,"method":"prompts/get","params":{"name":"s1__greet","arguments":{"name":"Andy"}}})).await;
    assert_eq!(status, 200);
    assert!(prompt["result"]["messages"].is_array(), "{prompt}");
    assert_eq!(
        replica
            .rpc(
                "/mcp",
                Some(&s),
                "other",
                json!({"jsonrpc":"2.0","id":1,"method":"ping"})
            )
            .await
            .0,
        404
    );
    let expired = sealed_session::Session {
        owner: "one".into(),
        credential: hex::encode(Sha256::digest(b"Bearer client-test-token")),
        route: "".into(),
        issued: sealed_session::now() - 86401,
        nonce: "expired".into(),
        version: "2025-11-25".into(),
        legs: vec![],
    };
    let expired = sealed_session::seal(
        &a2a_ids::IdKey::from_base64(KEY).unwrap(),
        sealed_session::SESSION,
        &expired,
    )
    .unwrap();
    assert_eq!(
        replica
            .rpc(
                "/mcp",
                Some(&expired),
                "secret",
                json!({"jsonrpc":"2.0","id":1,"method":"ping"})
            )
            .await
            .0,
        404
    );
    assert_eq!(
        replica
            .rpc(
                "/mcp",
                Some("bogus"),
                "secret",
                json!({"jsonrpc":"2.0","id":1,"method":"ping"})
            )
            .await
            .0,
        404
    );
    let limited = gw.initialize("/mcp", "other").await;
    assert_eq!(list(&gw, &limited, "other").await, vec!["s1__echo"]);
    assert_eq!(
        gw.rpc(
            "/mcp",
            Some(&limited),
            "other",
            json!({"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"s1__add"}})
        )
        .await
        .0,
        403
    );
    let response = gw
        .raw(
            "/mcp",
            "POST",
            Some(&s),
            "secret",
            Bytes::from(
                json!({"jsonrpc":"2.0","id":40,"method":"tools/call","params":{"name":"s1__ask","arguments":{"question":"Ready?"}}})
                    .to_string(),
            ),
        )
        .await;
    assert_eq!(response.status(), 200);
    let mut body = response.into_body();
    let mut seen = String::new();
    let mut reverse = None;
    for _ in 0..6 {
        let frame = bounded(body.frame()).await.unwrap().unwrap();
        if let Ok(data) = frame.into_data() {
            seen.push_str(std::str::from_utf8(&data).unwrap());
        }
        for line in seen.lines() {
            if let Some(data) = line.strip_prefix("data: ") {
                if let Ok(v) = serde_json::from_str::<Value>(data) {
                    if v["method"] == "elicitation/create" {
                        reverse = Some(v["id"].clone());
                    }
                }
            }
        }
        if reverse.is_some() {
            break;
        }
    }
    let reverse = reverse.expect("elicitation request");
    assert!(reverse.as_str().unwrap().starts_with("lw:s1:"));
    assert_eq!(replica.rpc("/mcp",Some(&s),"secret",json!({"jsonrpc":"2.0","id":reverse,"result":{"action":"accept","content":{"name":"Andy"}}})).await.0,202);
    let mut final_text = String::new();
    while let Some(frame) = bounded(body.frame()).await {
        if let Ok(data) = frame.unwrap().into_data() {
            final_text.push_str(std::str::from_utf8(&data).unwrap());
        }
    }
    assert!(final_text.contains("40"));
    let progress = replica.raw("/mcp","POST",Some(&s),"secret",Bytes::from(json!({"jsonrpc":"2.0","id":41,"method":"tools/call","params":{"name":"s2__slow","arguments":{"delay_ms":1,"steps":2},"_meta":{"progressToken":"track"}}}).to_string())).await;
    let progress = String::from_utf8(bytes(progress).await.to_vec()).unwrap();
    assert!(
        progress.contains("notifications/progress") && progress.contains("track"),
        "{progress}"
    );
    let control = Request::post(format!("http://{}/_mock/tool/description", mock1.addr))
        .header("content-type", "application/json")
        .body(Full::new(Bytes::from(
            json!({"name":"echo","description":"Changed definition"}).to_string(),
        )))
        .unwrap();
    assert_eq!(client().request(control).await.unwrap().status(), 200);
    let _ = list(&gw, &s, "secret").await;
    let (_, v) = gw
        .rpc(
            "/mcp",
            Some(&s),
            "secret",
            json!({"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"s1__echo"}}),
        )
        .await;
    assert!(v.get("error").is_some(), "{v}");
    assert_eq!(
        gw.raw("/mcp", "GET", Some(&s), "secret", Bytes::new())
            .await
            .status(),
        405
    );
    assert_eq!(
        gw.raw("/mcp", "DELETE", Some(&s), "secret", Bytes::new())
            .await
            .status(),
        204
    );
    assert_eq!(
        gw.rpc(
            "/mcp",
            Some(&s),
            "secret",
            json!({"jsonrpc":"2.0","id":5,"method":"tools/list"})
        )
        .await
        .0,
        404
    );
}
fn test_session(addr: SocketAddr, route: &str, plugin: &str, params: Option<Value>) -> String {
    let mut auth = Sha256::new();
    auth.update(plugin.as_bytes());
    if let Some(params) = params {
        // This fixture's string-only object has the same bytes under JCS.
        auth.update(Sha256::digest(params.to_string().as_bytes()));
    }
    sealed_session::seal(
        &a2a_ids::IdKey::from_base64(KEY).unwrap(),
        sealed_session::SESSION,
        &sealed_session::Session {
            owner: "one".into(),
            credential: hex::encode(Sha256::digest(b"Bearer client-test-token")),
            route: route.into(),
            issued: sealed_session::now(),
            nonce: "timeout-test".into(),
            version: "2025-11-25".into(),
            legs: vec![sealed_session::Leg {
                name: "s1".into(),
                target: "s1".into(),
                url: format!("http://{addr}/mcp"),
                auth: hex::encode(auth.finalize()),
                session: None,
                version: "2025-11-25".into(),
            }],
        },
    )
    .unwrap()
}
fn initialize_request() -> Value {
    json!({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"timeout-test","version":"1"}}})
}

/// A `snapshot.pb` the gateway boots from, laid out like
/// `snapshot::store_with_digest`: `LWSNAP02 || payload || digest ||
/// len(digest) u32 BE || sha256(everything before)`.
fn cached_snapshot(probe: SocketAddr) -> Vec<u8> {
    use lumewarp_proto::config::v1 as pb;
    use prost::Message;
    let snapshot = pb::Snapshot {
        version: 5,
        scope: "scope-a".into(),
        required_capabilities: vec!["mcp".into(), "auth:auth_fixture".into()],
        api_keys: vec![pb::ApiKey {
            key_id: "one".into(),
            key_hash: Sha256::digest(b"secret").to_vec(),
            status: pb::KeyStatus::Active as i32,
            policy: Some(pb::EffectivePolicy {
                allow_all_mcp: true,
                ..Default::default()
            }),
            ..Default::default()
        }],
        mcp_servers: vec![pb::McpServer {
            id: "mcp-1".into(),
            name: "s1".into(),
            // Nothing listens there: a request must reach the auth plugin first.
            url: "http://127.0.0.1:1/mcp".into(),
            timeout_ms: 1000,
            auth: Some(pb::UpstreamAuth {
                plugin: Some(pb::PluginRef {
                    plugin: "auth_fixture".into(),
                    params_json: json!({
                        "mode": "configure_fetch",
                        "url": format!("http://{probe}/configure"),
                        "allowed_origins": [format!("http://{probe}")],
                    })
                    .to_string(),
                    ..Default::default()
                }),
                ..Default::default()
            }),
            ..Default::default()
        }],
        created_at_unix_ms: 1,
        ..Default::default()
    };
    let mut file = b"LWSNAP02".to_vec();
    file.extend_from_slice(&snapshot.encode_to_vec());
    let digest = b"n1-digest";
    file.extend_from_slice(digest);
    file.extend_from_slice(&(digest.len() as u32).to_be_bytes());
    let checksum = Sha256::digest(&file);
    file.extend_from_slice(&checksum);
    file
}
#[tokio::test]
async fn auth_timeout_is_504_and_counted_without_breaking_aggregate_initialization() {
    let mock = Mock::start(0).await;
    let gw = Gw::start_auth(
        &[("s1", mock.addr)],
        0,
        "{plugin: auth_fixture, params: {mode: loop}, timeout_ms: 100}",
    )
    .await;
    assert_eq!(
        gw.rpc("/mcp/servers/s1", None, "secret", initialize_request())
            .await
            .0,
        504
    );
    // Aggregate initialize still excludes a failed leg instead of failing the virtual server.
    assert_eq!(
        gw.rpc("/mcp", None, "secret", initialize_request()).await.0,
        200
    );
    // A pre-existing session must also preserve auth timeout provenance on forwards.
    let direct = test_session(
        mock.addr,
        "s1",
        "auth_fixture",
        Some(json!({"mode":"loop"})),
    );
    assert_eq!(
        gw.rpc(
            "/mcp/servers/s1",
            Some(&direct),
            "secret",
            json!({"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"echo"}})
        )
        .await
        .0,
        504
    );
    assert_eq!(
        gw.rpc(
            "/mcp/servers/s1",
            Some(&direct),
            "secret",
            json!({"jsonrpc":"2.0","id":3,"method":"tools/list"})
        )
        .await
        .0,
        504
    );
    let aggregate = test_session(mock.addr, "", "auth_fixture", Some(json!({"mode":"loop"})));
    assert_eq!(
        gw.rpc(
            "/mcp",
            Some(&aggregate),
            "secret",
            json!({"jsonrpc":"2.0","id":4,"method":"tools/list"})
        )
        .await
        .0,
        504
    );
    let metrics = gw.metrics().await;
    assert!(
        metrics
            .lines()
            .any(|line| line.starts_with("lumewarp_plugin_calls_total{")
                && line.contains("plugin=\"auth_fixture\"")
                && line.contains("result=\"timeout\"")
                && line
                    .split_whitespace()
                    .last()
                    .is_some_and(|v| v.parse::<f64>().is_ok_and(|v| v > 0.0))),
        "{metrics}"
    );
    assert!(
        metrics
            .lines()
            .any(|line| line.starts_with("lumewarp_mcp_requests_total{")
                && line.contains("method=\"tools/call\"")
                && line.contains("status=\"504\"")),
        "{metrics}"
    );
    assert!(
        metrics
            .lines()
            .any(|line| line.starts_with("lumewarp_mcp_tool_calls_total{")
                && line.contains("status=\"504\"")),
        "{metrics}"
    );
}
#[tokio::test]
async fn other_auth_failures_remain_502() {
    let mock = Mock::start(0).await;
    let gw = Gw::start_auth(
        &[("s1", mock.addr)],
        0,
        "{plugin: auth_fixture, params: {mode: error}, timeout_ms: 100}",
    )
    .await;
    assert_eq!(
        gw.rpc("/mcp/servers/s1", None, "secret", initialize_request())
            .await
            .0,
        502
    );
    let session = test_session(
        mock.addr,
        "s1",
        "auth_fixture",
        Some(json!({"mode":"error"})),
    );
    assert_eq!(
        gw.rpc(
            "/mcp/servers/s1",
            Some(&session),
            "secret",
            json!({"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"echo"}})
        )
        .await
        .0,
        502
    );
}
/// N1 (review-w2-gw): booting from the standalone YAML must publish the plugin
/// sets the apply staged, or every plugin is configured again cold on the first
/// request. The fixture's `configure_fetch` mode fetches once per configured
/// instance, so a request that configures again is visible as one more fetch.
#[tokio::test]
async fn a_boot_publishes_the_staged_plugins_and_the_first_request_reuses_them() {
    use std::sync::atomic::Ordering;
    let mock = Mock::start(0).await;
    // Counts the `configure`-time fetches of the auth fixture below.
    let (probe, configures) = common::counter().await;
    let gw = Gw::start_auth(
        &[("s1", mock.addr)],
        0,
        &format!(
            "{{plugin: auth_fixture, params: {{mode: configure_fetch, url: 'http://{probe}/configure', allowed_origins: ['http://{probe}'], headers: ['authorization: Bearer boot-probe']}}}}"
        ),
    )
    .await;
    let booted = configures.load(Ordering::SeqCst);
    assert!(
        booted > 0,
        "the apply-time configure configured the plugin while booting"
    );
    let session = gw.initialize("/mcp/servers/s1", "secret").await;
    let (status, response) = gw
        .rpc(
            "/mcp/servers/s1",
            Some(&session),
            "secret",
            json!({"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"whoami"}}),
        )
        .await;
    assert_eq!(status, 200, "{response}");
    assert_eq!(
        response["result"]["structuredContent"]["authorization"],
        json!("Bearer boot-probe"),
        "the request ran with the credentials of the published authenticator"
    );
    assert_eq!(
        configures.load(Ordering::SeqCst),
        booted,
        "the first request configured the plugin again (N1)"
    );
}

/// N1 (review-w2-gw): the cached-snapshot boot publishes the staged plugin sets
/// too. The cache file is written here, so the boot needs no control plane; the
/// fixture's `configure_fetch` mode counts its configure-time fetches.
#[tokio::test]
async fn a_cached_boot_publishes_the_staged_plugins_too() {
    use std::sync::atomic::Ordering;
    let plugins = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../target/plugins-test/auth");
    assert!(
        plugins.join("fixture.wasm").exists(),
        "Build auth fixtures: bash gateway/plugins/build.sh --tests"
    );
    let (probe, configures) = common::counter().await;
    let dir = tempfile::tempdir().unwrap();
    let state = dir.path().join("state");
    std::fs::create_dir_all(&state).unwrap();
    std::fs::write(state.join("snapshot.pb"), cached_snapshot(probe)).unwrap();
    let local = dir.path().join("local.yaml");
    // The child compiles the wasm fixtures before it binds, so a port reserved
    // here can be claimed by a parallel test meanwhile: a startup exit is
    // retried with fresh ports instead of failing for a lost race.
    let mut running = None;
    let (mut listen, mut admin) = (address(), address());
    for attempt in 0..5 {
        std::fs::write(
            &local,
            format!("server: {{listen: '{listen}', admin_listen: '{admin}'}}\n"),
        )
        .unwrap();
        let mut child = Process(
            Command::new(env!("CARGO_BIN_EXE_lumewarp"))
                .args(["--control-plane", &format!("http://{}", address())])
                .args(["--gateway-token-env", "LUMEWARP_N1_TOKEN"])
                .arg("--state-dir")
                .arg(&state)
                .args(["--scope", "scope-a", "--gateway-id", "gw-cached"])
                .arg("--local-config")
                .arg(&local)
                .env("LUMEWARP_N1_TOKEN", "gw-token")
                .env("LUMEWARP_AUTH_PLUGIN_DIR", &plugins)
                .env("LUMEWARP_GATEWAY_ID_KEY", KEY)
                .env_remove("OTEL_EXPORTER_OTLP_ENDPOINT")
                .stdout(Stdio::null())
                .stderr(Stdio::inherit())
                .spawn()
                .unwrap(),
        );
        let deadline = tokio::time::Instant::now() + Duration::from_secs(30);
        loop {
            if child.0.try_wait().unwrap().is_some() {
                break;
            }
            if tokio::net::TcpStream::connect(listen).await.is_ok() {
                running = Some(child);
                break;
            }
            assert!(
                tokio::time::Instant::now() < deadline,
                "the gateway did not start"
            );
            tokio::time::sleep(Duration::from_millis(25)).await;
        }
        if running.is_some() {
            break;
        }
        assert!(attempt < 4, "the gateway never bound a free port");
        (listen, admin) = (address(), address());
    }
    let _process = running.expect("the gateway is running");
    let booted = configures.load(Ordering::SeqCst);
    assert!(
        booted > 0,
        "booting from the cached snapshot configured the plugin"
    );
    let request = Request::builder()
        .method("POST")
        .uri(format!("http://{listen}/mcp/servers/s1"))
        .header("x-lumewarp-key", "secret")
        .header("content-type", "application/json")
        .header("accept", "application/json, text/event-stream")
        .body(Full::new(Bytes::from(initialize_request().to_string())))
        .unwrap();
    let response = bounded(client().request(request)).await.unwrap();
    assert_eq!(
        response.status(),
        502,
        "the request passed the plugin auth and reached the dead upstream"
    );
    assert_eq!(
        configures.load(Ordering::SeqCst),
        booted,
        "the first request after a cached boot configured the plugin again (N1)"
    );
}

#[tokio::test]
async fn gateway_startup_retries_an_actual_listener_bind_conflict() {
    let occupied = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = occupied.local_addr().unwrap();
    // The first child must fail to bind this held socket; the normal startup
    // path then allocates fresh ports instead of waiting on a dead process.
    let gw = Gw::start_auth_with_first_admin(&[("s1", address())], 0, "{plugin: none}", Some(port))
        .await;
    assert_ne!(gw.admin, port);
    let response = gw
        .http
        .request(
            Request::get(format!("http://{}/healthz", gw.admin))
                .body(Full::new(Bytes::new()))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), 200);
}
#[tokio::test]
async fn upstream_timeout_is_504_on_initialize_and_forward() {
    use hyper::service::service_fn;
    use hyper_util::rt::TokioIo;
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let server = tokio::spawn(async move {
        loop {
            let (socket, _) = listener.accept().await.unwrap();
            tokio::spawn(async move {
                let service = service_fn(|_: Request<Incoming>| async {
                    tokio::time::sleep(Duration::from_secs(2)).await;
                    Ok::<_, std::convert::Infallible>(Response::new(Full::new(Bytes::new())))
                });
                let _ = hyper::server::conn::http1::Builder::new()
                    .serve_connection(TokioIo::new(socket), service)
                    .await;
            });
        }
    });
    let gw = Gw::start(&[("s1", addr)], 0).await;
    assert_eq!(
        gw.rpc("/mcp/servers/s1", None, "secret", initialize_request())
            .await
            .0,
        504
    );
    let session = test_session(addr, "s1", "none", None);
    assert_eq!(
        gw.rpc(
            "/mcp/servers/s1",
            Some(&session),
            "secret",
            json!({"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"echo"}})
        )
        .await
        .0,
        504
    );
    let (status, response) = gw
        .rpc(
            "/mcp/servers/s1",
            Some(&session),
            "secret",
            json!({"jsonrpc":"2.0","id":3,"method":"tools/list"}),
        )
        .await;
    let decoded: sealed_session::Session = sealed_session::open(
        &a2a_ids::IdKey::from_base64(KEY).unwrap(),
        sealed_session::SESSION,
        &session,
    )
    .unwrap();
    assert_eq!(
        status,
        504,
        "{response}; issued={}, test_now={}",
        decoded.issued,
        sealed_session::now()
    );
    server.abort();
}
#[tokio::test]
async fn ambiguous_server_namespace_is_rejected_at_startup() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("bad.yaml");
    std::fs::write(&path,format!("server:\n  listen: '{}'\n  admin_listen: '{}'\nmcp_servers:\n  - name: s1__bad\n    url: http://127.0.0.1:1/mcp\n",address(),address())).unwrap();
    let mut process = Process(
        Command::new(env!("CARGO_BIN_EXE_lumewarp"))
            .arg("--config")
            .arg(path)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap(),
    );
    for _ in 0..100 {
        if let Some(status) = process.0.try_wait().unwrap() {
            assert!(!status.success());
            return;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    panic!("ambiguous namespace was accepted");
}
#[tokio::test]
async fn non_streaming_response_limit() {
    use hyper::{service::service_fn, StatusCode};
    use hyper_util::rt::TokioIo;
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let server = tokio::spawn(async move {
        loop {
            let (socket, _) = listener.accept().await.unwrap();
            tokio::spawn(async move {
                let service = service_fn(|request: Request<Incoming>| async move {
                    let body = request.into_body().collect().await.unwrap().to_bytes();
                    let v: Value = serde_json::from_slice(&body).unwrap();
                    let bytes = if v["method"] == "initialize" {
                        Bytes::from(json!({"jsonrpc":"2.0","id":v["id"],"result":{"protocolVersion":"2025-11-25","capabilities":{},"serverInfo":{"name":"bounds","version":"1"}}}).to_string())
                    } else if v["method"] == "notifications/initialized" {
                        Bytes::new()
                    } else {
                        Bytes::from(vec![b'x'; 16 * 1024 * 1024 + 1])
                    };
                    Ok::<_, std::convert::Infallible>(
                        Response::builder()
                            .status(if bytes.is_empty() {
                                StatusCode::ACCEPTED
                            } else {
                                StatusCode::OK
                            })
                            .header("content-type", "application/json")
                            .body(Full::new(bytes))
                            .unwrap(),
                    )
                });
                let _ = hyper::server::conn::http1::Builder::new()
                    .serve_connection(TokioIo::new(socket), service)
                    .await;
            });
        }
    });
    let gw = Gw::start(&[("s1", addr)], 0).await;
    let s = gw.initialize("/mcp/servers/s1", "secret").await;
    assert_eq!(
        gw.rpc(
            "/mcp/servers/s1",
            Some(&s),
            "secret",
            json!({"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"huge"}})
        )
        .await
        .0,
        502
    );
    server.abort();
}
#[tokio::test]
async fn outbound_credentials_and_wasm_signature() {
    for (auth, expected, mock_auth) in [
        ("{plugin: none}", Value::Null, "none"),
        (
            "{plugin: passthrough}",
            json!("Bearer client-test-token"),
            "none",
        ),
        (
            "{plugin: static_bearer, secret_env: MCP_TEST_STATIC_TOKEN}",
            json!("Bearer static-test-token"),
            "none",
        ),
        (
            "{plugin: hmac_signature, params: {secret_name: MCP_TEST}}",
            Value::Null,
            "hmac",
        ),
    ] {
        let mock = Mock::start_auth(0, mock_auth).await;
        let gw = Gw::start_auth(&[("s1", mock.addr)], 0, auth).await;
        let session = gw.initialize("/mcp/servers/s1", "secret").await;
        let (status, v) = gw
            .rpc(
                "/mcp/servers/s1",
                Some(&session),
                "secret",
                json!({"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"whoami"}}),
            )
            .await;
        assert_eq!(status, 200, "{v}");
        let summary = &v["result"]["structuredContent"];
        assert_eq!(summary["authorization"], expected, "{v}");
        assert_eq!(summary["saw_lumewarp_key"], false);
        assert!(summary["headers"].get("cookie").is_none());
    }
}
#[tokio::test]
async fn single_server_rpm_and_request_limit() {
    let mock = Mock::start(0).await;
    let gw = Gw::start(&[("s1", mock.addr)], 1).await;
    let s = gw.initialize("/mcp/servers/s1", "secret").await;
    let call = json!({"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"echo","arguments":{"message":"hi"}}});
    assert_eq!(
        gw.rpc("/mcp/servers/s1", Some(&s), "secret", call.clone())
            .await
            .0,
        200
    );
    let response = gw
        .raw(
            "/mcp/servers/s1",
            "POST",
            Some(&s),
            "secret",
            Bytes::from(call.to_string()),
        )
        .await;
    assert_eq!(response.status(), 429);
    assert!(response.headers().contains_key("retry-after"));
    let get = gw
        .raw("/mcp/servers/s1", "GET", Some(&s), "secret", Bytes::new())
        .await;
    assert_eq!(get.status(), 200);
    drop(get);
    assert_eq!(
        gw.raw(
            "/mcp/servers/s1",
            "DELETE",
            Some(&s),
            "secret",
            Bytes::new()
        )
        .await
        .status(),
        204
    );
    assert_eq!(
        gw.raw(
            "/mcp",
            "POST",
            Some(&s),
            "secret",
            Bytes::from(vec![b' '; 4 * 1024 * 1024 + 1])
        )
        .await
        .status(),
        413
    );
}
