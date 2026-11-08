//! A2A v1.0 proxy against the real `mcp-a2a-mock` agent (plan acceptance 5).
//!
//! One gateway process with two keys drives all scenarios against real mock
//! agents on loopback:
//!
//! - the rewritten Agent Card (gateway URL, gateway auth scheme, no signatures,
//!   push off) and `GET /a2a`;
//! - `SendMessage`/`GetTask` with wrapped ids, key B and a tampered MAC refused
//!   as task-not-found, and a task minted by one gateway read by a second one
//!   (both share `LUMEWARP_GATEWAY_ID_KEY`);
//! - `A2A-Version` (absent and non-1.0), push-notification refusal, `ListTasks`
//!   only for a `passthrough` agent, and the `strip-artifacts` middleware over
//!   SSE and a blocking response;
//! - the L5 hook re-checks: a hook that changes the method, adds an id, adds
//!   push configuration, or drops a non-artifact stream event is refused; a
//!   slow hook hits the per-target timeout (504) and counts `timeout`;
//! - a gateway without `LUMEWARP_GATEWAY_ID_KEY` answers 503.
//!
//! The middleware components are built by `bash gateway/plugins/build.sh
//! --tests`; without them the test prints a SKIP line (like the strategy
//! fixtures). `mcp-a2a-mock` must be built in `bench/mock-upstream`.

mod common;

use std::{
    collections::HashMap,
    net::{SocketAddr, TcpListener as StdListener},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    time::Duration,
};

use bytes::Bytes;
use common::{bounded, bytes, client, TestClient};
use http_body_util::Full;
use hyper::{Request, StatusCode};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

/// base64 of a 32-byte `LUMEWARP_GATEWAY_ID_KEY` (`a2a_ids::MIN_KEY_BYTES`).
const ID_KEY: &str = "bHVtZXdhcnAtdGVzdC1nYXRld2F5LWlkLWtleS0zMmJ5";
const SECRET_A: &str = "a2a-secret-a";
const SECRET_B: &str = "a2a-secret-b";
const SECRET_MEDIA: &str = "application/x-mock-secret";

// ---------------------------------------------------------------------------
// Processes
// ---------------------------------------------------------------------------

struct Process(Child);
impl Drop for Process {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

fn free_addr() -> SocketAddr {
    let listener = StdListener::bind("127.0.0.1:0").unwrap();
    listener.local_addr().unwrap()
}

fn mock_bin() -> Option<PathBuf> {
    // CARGO_MANIFEST_DIR is gateway/crates/lumewarp-gateway.
    let root =
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../bench/mock-upstream/target");
    ["release", "debug"]
        .iter()
        .map(|profile| root.join(profile).join("mcp-a2a-mock"))
        .find(|path| path.exists())
}

fn plugin_dir() -> Option<PathBuf> {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../target");
    let example = root
        .join("plugins/a2a-middleware/strip-artifacts.wasm")
        .exists();
    let fixture = root
        .join("plugins-test/a2a-middleware/misbehave.wasm")
        .exists();
    let auth = root.join("plugins-test/auth/fixture.wasm").exists();
    if example && fixture && auth {
        Some(root)
    } else {
        None
    }
}

async fn wait_http(client: &TestClient, url: &str) {
    tokio::time::timeout(Duration::from_secs(20), async {
        loop {
            let request = Request::builder()
                .uri(url)
                .body(Full::new(Bytes::new()))
                .unwrap();
            if let Ok(response) = client.request(request).await {
                if response.status().is_success() {
                    return;
                }
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    })
    .await
    .unwrap_or_else(|_| panic!("{url} did not become ready"));
}

fn start_mock(bin: &Path, auth: &str) -> (Process, SocketAddr) {
    let addr = free_addr();
    let child = Command::new(bin)
        .args([
            "a2a",
            "--listen",
            &addr.to_string(),
            "--auth",
            auth,
            "--stream-step-ms",
            "20",
        ])
        .stderr(Stdio::null())
        .spawn()
        .expect("start mcp-a2a-mock");
    (Process(child), addr)
}

/// A tiny upstream that answers every request with an SSE body. Used to prove
/// that a non-streaming method answering `text/event-stream` is refused.
fn start_sse_agent() -> SocketAddr {
    use std::io::{Read, Write};
    let listener = StdListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    std::thread::spawn(move || {
        for stream in listener.incoming().flatten() {
            std::thread::spawn(move || {
                let mut stream = stream;
                let mut raw = Vec::new();
                let mut chunk = [0u8; 512];
                while !raw.windows(4).any(|window| window == b"\r\n\r\n") {
                    match stream.read(&mut chunk) {
                        Ok(0) | Err(_) => break,
                        Ok(read) => raw.extend_from_slice(&chunk[..read]),
                    }
                }
                let body = "data: {\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{\"task\":{\"id\":\"t\",\"status\":{\"state\":\"TASK_STATE_WORKING\"}}}}\n\n";
                let response = format!(
                    "HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{}",
                    body.len(),
                    body
                );
                let _ = stream.write_all(response.as_bytes());
            });
        }
    });
    addr
}

struct Gateway {
    _process: Process,
    _dir: tempfile::TempDir,
    addr: SocketAddr,
    admin: SocketAddr,
}

fn config(
    listen: SocketAddr,
    admin: SocketAddr,
    helper: SocketAddr,
    pt: SocketAddr,
    sse: SocketAddr,
) -> String {
    let agent = |name: &str, port: SocketAddr, middleware: &str| {
        format!("  - name: {name}\n    url: 'http://{port}/a2a'\n    auth: {{plugin: none}}\n{middleware}")
    };
    let strip = "    middleware: {plugin: strip_artifacts, params: {media_types: ['application/x-mock-secret']}}\n";
    let misbehave = |mode: &str| {
        format!(
            "    middleware: {{plugin: misbehave_test, params: {{mode: {mode}, spin: 4000000000}}, timeout_ms: {} }}\n",
            if mode == "slow" { 50 } else { 5000 }
        )
    };
    let mut yaml =
        format!("server:\n  listen: '{listen}'\n  admin_listen: '{admin}'\na2a_agents:\n");
    for (name, middleware) in [
        ("helper", strip.to_owned()),
        ("bad_method", misbehave("method")),
        ("bad_add_id", misbehave("add_id")),
        ("bad_push", misbehave("push")),
        ("bad_drop", misbehave("drop_status")),
        ("bad_snake_id", misbehave("add_snake_id")),
        ("bad_snake_response", misbehave("add_snake_response")),
        ("bad_snake_stream", misbehave("add_snake_stream")),
        ("slow", misbehave("slow")),
    ] {
        yaml.push_str(&agent(name, helper, &middleware));
    }
    yaml.push_str(&format!(
        "  - name: pt\n    url: 'http://{pt}/a2a'\n    auth: {{plugin: passthrough}}\n"
    ));
    // Outbound-auth failures: a looping plugin hits its 50 ms per-call timeout;
    // an erroring one is a plain plugin failure.
    yaml.push_str(&format!(
        "  - name: slow_auth\n    url: 'http://{helper}/a2a'\n    auth: {{plugin: auth_fixture, params: {{mode: loop}}, timeout_ms: 50}}\n"
    ));
    yaml.push_str(&format!(
        "  - name: error_auth\n    url: 'http://{helper}/a2a'\n    auth: {{plugin: auth_fixture, params: {{mode: error}}}}\n"
    ));
    // Answers even a non-streaming method with SSE; the gateway must refuse it.
    yaml.push_str(&format!(
        "  - name: sse-agent\n    url: 'http://{sse}/a2a'\n    auth: {{plugin: none}}\n"
    ));
    yaml.push_str("keys:\n");
    yaml.push_str(&format!(
        "  - key_id: key-a\n    key_sha256: '{}'\n    allow_all_models: true\n    allowed_agents: [helper, pt, bad_method, bad_add_id, bad_push, bad_drop, bad_snake_id, bad_snake_response, bad_snake_stream, slow, slow_auth, error_auth, sse-agent]\n",
        hex::encode(Sha256::digest(SECRET_A.as_bytes())),
    ));
    yaml.push_str(&format!(
        "  - key_id: key-b\n    key_sha256: '{}'\n    allow_all_models: true\n    allowed_agents: [helper]\n",
        hex::encode(Sha256::digest(SECRET_B.as_bytes())),
    ));
    yaml
}

/// A snapshot with no middleware: used when the middleware directory is not
/// loaded, so snapshot validation does not need those capabilities.
fn config_without_middleware(listen: SocketAddr, admin: SocketAddr, agent: SocketAddr) -> String {
    format!(
        "server:\n  listen: '{listen}'\n  admin_listen: '{admin}'\na2a_agents:\n  - name: helper\n    url: 'http://{agent}/a2a'\n    auth: {{plugin: none}}\nkeys:\n  - key_id: key-a\n    key_sha256: '{}'\n    allow_all_models: true\n    allowed_agents: [helper]\n",
        hex::encode(Sha256::digest(SECRET_A.as_bytes())),
    )
}

fn start_gateway(
    listen: SocketAddr,
    admin: SocketAddr,
    config: &str,
    plugin_root: Option<&Path>,
    id_key: Option<&str>,
    public_url: Option<&str>,
) -> Gateway {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("config.yaml");
    std::fs::write(&path, config).unwrap();
    let mut command = Command::new(env!("CARGO_BIN_EXE_lumewarp"));
    command.arg("--config").arg(&path);
    for var in [
        "LUMEWARP_ROUTER_PLUGIN_DIR",
        "LUMEWARP_AUTH_PLUGIN_DIR",
        "LUMEWARP_A2A_MIDDLEWARE_DIR",
        "LUMEWARP_GATEWAY_ID_KEY",
        "LUMEWARP_GATEWAY_PUBLIC_URL",
        "LUMEWARP_CONFIG",
        "LUMEWARP_CONTROL_PLANE",
    ] {
        command.env_remove(var);
    }
    if let Some(root) = plugin_root {
        // One `.wasm` per kind world: the example and the fixture share the
        // middleware directory, the auth fixture gets its own.
        let middleware = dir.path().join("middleware");
        std::fs::create_dir_all(&middleware).unwrap();
        std::fs::copy(
            root.join("plugins/a2a-middleware/strip-artifacts.wasm"),
            middleware.join("strip-artifacts.wasm"),
        )
        .unwrap();
        std::fs::copy(
            root.join("plugins-test/a2a-middleware/misbehave.wasm"),
            middleware.join("misbehave.wasm"),
        )
        .unwrap();
        command.env("LUMEWARP_A2A_MIDDLEWARE_DIR", &middleware);
        let auth = dir.path().join("auth");
        std::fs::create_dir_all(&auth).unwrap();
        std::fs::copy(
            root.join("plugins-test/auth/fixture.wasm"),
            auth.join("fixture.wasm"),
        )
        .unwrap();
        command.env("LUMEWARP_AUTH_PLUGIN_DIR", &auth);
    }
    if let Some(id_key) = id_key {
        command.env("LUMEWARP_GATEWAY_ID_KEY", id_key);
    }
    if let Some(public_url) = public_url {
        command.env("LUMEWARP_GATEWAY_PUBLIC_URL", public_url);
    }
    let child = command
        .stdout(Stdio::null())
        .stderr(Stdio::inherit())
        .spawn()
        .expect("start gateway");
    Gateway {
        _process: Process(child),
        _dir: dir,
        addr: listen,
        admin,
    }
}

// ---------------------------------------------------------------------------
// HTTP helpers
// ---------------------------------------------------------------------------

fn rpc_body(method: &str, params: Value) -> Value {
    json!({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
}

fn message(text: &str) -> Value {
    json!({
        "messageId": "msg-1",
        "role": "ROLE_USER",
        "parts": [{"text": text, "mediaType": "text/plain"}],
    })
}

async fn post(
    client: &TestClient,
    gateway: &Gateway,
    agent: &str,
    key: &str,
    body: &Value,
    version: Option<&str>,
    bearer: Option<&str>,
) -> (StatusCode, Value) {
    let mut request = Request::builder()
        .method("POST")
        .uri(format!("http://{}/a2a/{agent}", gateway.addr))
        .header("content-type", "application/json")
        .header("x-lumewarp-key", key);
    if let Some(version) = version {
        request = request.header("a2a-version", version);
    }
    if let Some(bearer) = bearer {
        request = request.header("authorization", format!("Bearer {bearer}"));
    }
    let request = request
        .body(Full::new(Bytes::from(serde_json::to_vec(body).unwrap())))
        .unwrap();
    let response = bounded(client.request(request)).await.unwrap();
    let status = response.status();
    let bytes = bytes(response).await;
    let value = serde_json::from_slice(&bytes)
        .unwrap_or_else(|_| Value::String(String::from_utf8_lossy(&bytes).into()));
    (status, value)
}

async fn get(
    client: &TestClient,
    gateway: &Gateway,
    path: &str,
    key: &str,
) -> (StatusCode, Vec<u8>, HashMap<String, String>) {
    let request = Request::builder()
        .uri(format!("http://{}{path}", gateway.addr))
        .header("x-lumewarp-key", key)
        .body(Full::new(Bytes::new()))
        .unwrap();
    let response = bounded(client.request(request)).await.unwrap();
    let status = response.status();
    let headers = response
        .headers()
        .iter()
        .filter_map(|(name, value)| {
            value
                .to_str()
                .ok()
                .map(|value| (name.as_str().to_owned(), value.to_owned()))
        })
        .collect();
    (status, bytes(response).await.to_vec(), headers)
}

/// Reads a finite SSE stream and returns the JSON `data:` payloads.
async fn stream(
    client: &TestClient,
    gateway: &Gateway,
    agent: &str,
    key: &str,
    bearer: Option<&str>,
) -> (StatusCode, Vec<Value>) {
    let body = rpc_body(
        "SendStreamingMessage",
        json!({"message": message("stream please")}),
    );
    let mut request = Request::builder()
        .method("POST")
        .uri(format!("http://{}/a2a/{agent}", gateway.addr))
        .header("content-type", "application/json")
        .header("accept", "text/event-stream")
        .header("a2a-version", "1.0")
        .header("x-lumewarp-key", key);
    if let Some(bearer) = bearer {
        request = request.header("authorization", format!("Bearer {bearer}"));
    }
    let request = request
        .body(Full::new(Bytes::from(serde_json::to_vec(&body).unwrap())))
        .unwrap();
    let response = bounded(client.request(request)).await.unwrap();
    let status = response.status();
    let raw = String::from_utf8_lossy(&bytes(response).await).into_owned();
    let events = raw
        .split("\n\n")
        .filter_map(|event| {
            let data: Vec<&str> = event
                .lines()
                .filter_map(|line| line.strip_prefix("data:"))
                .map(str::trim)
                .collect();
            if data.is_empty() {
                return None;
            }
            serde_json::from_str::<Value>(&data.join("\n")).ok()
        })
        .collect();
    (status, events)
}

fn wrapped(id: &Value) -> &str {
    id.as_str().expect("an id string")
}

// ---------------------------------------------------------------------------
// The suite
// ---------------------------------------------------------------------------

#[tokio::test]
async fn a2a_proxy_end_to_end() {
    let Some(bin) = mock_bin() else {
        eprintln!("SKIP: build bench/mock-upstream's mcp-a2a-mock first");
        return;
    };
    let Some(plugin_root) = plugin_dir() else {
        eprintln!(
            "SKIP: build the middleware components with bash gateway/plugins/build.sh --tests"
        );
        return;
    };
    let client = client();
    let (_helper_process, helper) = start_mock(&bin, "none");
    let (_pt_process, pt) = start_mock(&bin, "any-bearer");
    let sse = start_sse_agent();
    wait_http(&client, &format!("http://{helper}/health")).await;
    wait_http(&client, &format!("http://{pt}/health")).await;

    let addr = free_addr();
    let admin = free_addr();
    let public_url = format!("http://{addr}");
    let gateway = start_gateway(
        addr,
        admin,
        &config(addr, admin, helper, pt, sse),
        Some(&plugin_root),
        Some(ID_KEY),
        Some(&public_url),
    );
    wait_http(&client, &format!("http://{admin}/readyz")).await;

    // GET /a2a lists what key A may call, with the gateway card URL.
    let (status, body, _) = get(&client, &gateway, "/a2a", SECRET_A).await;
    assert_eq!(status, StatusCode::OK, "{}", String::from_utf8_lossy(&body));
    let list: Value = serde_json::from_slice(&body).unwrap();
    let names: Vec<&str> = list
        .as_array()
        .unwrap()
        .iter()
        .map(|entry| entry["name"].as_str().unwrap())
        .collect();
    assert!(
        names.contains(&"helper") && names.contains(&"pt"),
        "{names:?}"
    );
    assert!(!names.contains(&"not-granted"));
    let helper_card = list
        .as_array()
        .unwrap()
        .iter()
        .find(|entry| entry["name"] == "helper")
        .unwrap();
    assert_eq!(
        helper_card["cardUrl"],
        format!("{public_url}/a2a/helper/.well-known/agent-card.json")
    );
    // Key B may only see helper.
    let (_, body, _) = get(&client, &gateway, "/a2a", SECRET_B).await;
    let list: Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(list.as_array().unwrap().len(), 1);

    // The startup supervisor fetches the card with no A2A request at all. Only
    // the admin metrics endpoint is polled until the fetch has happened; the
    // first A2A card request must then already be served.
    let deadline = std::time::Instant::now() + Duration::from_secs(20);
    loop {
        let metrics = get_metrics(&client, &gateway).await;
        if metrics.contains("lumewarp_a2a_upstream_duration_seconds_count{agent=\"helper\"}") {
            break;
        }
        assert!(
            std::time::Instant::now() < deadline,
            "the card fetch did not happen without an A2A request:\n{metrics}"
        );
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    tokio::time::sleep(Duration::from_millis(100)).await;
    let (status, body, headers) = get(
        &client,
        &gateway,
        "/a2a/helper/.well-known/agent-card.json",
        SECRET_A,
    )
    .await;
    assert_eq!(
        status,
        StatusCode::OK,
        "the card was not fetched without a request: {}",
        String::from_utf8_lossy(&body)
    );
    let card: Value = serde_json::from_slice(&body).unwrap();
    let etag = headers.get("etag").cloned().expect("an ETag");
    assert!(etag.starts_with('"'));
    assert_eq!(
        card["supportedInterfaces"].as_array().unwrap().len(),
        1,
        "one gateway interface"
    );
    assert_eq!(
        card["supportedInterfaces"][0]["url"],
        format!("{public_url}/a2a/helper")
    );
    assert_eq!(card["supportedInterfaces"][0]["protocolBinding"], "JSONRPC");
    assert_eq!(card["supportedInterfaces"][0]["protocolVersion"], "1.0");
    assert!(
        card.get("signatures").is_none(),
        "upstream signatures must be removed"
    );
    assert_eq!(card["capabilities"]["pushNotifications"], false);
    assert_eq!(
        card["securitySchemes"]["lumewarp"]["apiKeySecurityScheme"]["name"],
        "X-Lumewarp-Key"
    );
    // If-None-Match with the served ETag returns 304.
    let request = Request::builder()
        .uri(format!(
            "http://{}/a2a/helper/.well-known/agent-card.json",
            gateway.addr
        ))
        .header("x-lumewarp-key", SECRET_A)
        .header("if-none-match", etag)
        .body(Full::new(Bytes::new()))
        .unwrap();
    assert_eq!(
        bounded(client.request(request)).await.unwrap().status(),
        StatusCode::NOT_MODIFIED
    );

    // SendMessage (blocking) returns wrapped ids and the strip middleware has
    // already removed the secret artifact from the task.
    let (status, response) = post(
        &client,
        &gateway,
        "helper",
        SECRET_A,
        &rpc_body("SendMessage", json!({"message": message("hello")})),
        Some("1.0"),
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{response}");
    let task = &response["result"]["task"];
    let task_id = wrapped(&task["id"]).to_owned();
    let context_id = wrapped(&task["contextId"]).to_owned();
    assert!(task_id.starts_with("lwt.key-a."), "{task_id}");
    assert!(context_id.starts_with("lwt.key-a."));
    let artifacts = task["artifacts"].as_array().unwrap();
    assert!(!artifacts.is_empty(), "the text artifact survives");
    assert!(
        !artifacts.iter().any(|artifact| {
            artifact["parts"]
                .as_array()
                .unwrap()
                .iter()
                .any(|part| part["mediaType"].as_str() == Some(SECRET_MEDIA))
        }),
        "the secret artifact was stripped"
    );

    // GetTask with key A works; key B and a tampered MAC are task-not-found.
    let (status, response) = post(
        &client,
        &gateway,
        "helper",
        SECRET_A,
        &rpc_body("GetTask", json!({"id": task_id})),
        Some("1.0"),
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(response["result"]["id"], task_id);
    assert_eq!(response["result"]["contextId"], context_id);
    let (status, response) = post(
        &client,
        &gateway,
        "helper",
        SECRET_B,
        &rpc_body("GetTask", json!({"id": task_id})),
        Some("1.0"),
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(response["error"]["code"], -32001, "{response}");
    let mut tampered = task_id.clone();
    let last = tampered.pop().unwrap();
    tampered.push(if last == 'A' { 'B' } else { 'A' });
    let (_, response) = post(
        &client,
        &gateway,
        "helper",
        SECRET_A,
        &rpc_body("GetTask", json!({"id": tampered})),
        Some("1.0"),
        None,
    )
    .await;
    assert_eq!(response["error"]["code"], -32001);

    // A2A-Version: absent and non-1.0 are -32009.
    for version in [None, Some("0.3"), Some("")] {
        let (status, response) = post(
            &client,
            &gateway,
            "helper",
            SECRET_A,
            &rpc_body("SendMessage", json!({"message": message("x")})),
            version,
            None,
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(response["error"]["code"], -32009, "{version:?}");
    }

    // B2: only the 11 v1.0 methods are forwarded; v0.3 names and unknown names
    // are -32601, and a non-object `params` is -32602, before any upstream call.
    for method in [
        "tasks/get",
        "message/send",
        "tasks/pushNotificationConfig/set",
        "nope",
    ] {
        let (status, response) = post(
            &client,
            &gateway,
            "helper",
            SECRET_A,
            &rpc_body(method, json!({"id": "raw-task-id"})),
            Some("1.0"),
            None,
        )
        .await;
        assert_eq!(status, StatusCode::OK, "{method}: {response}");
        assert_eq!(response["error"]["code"], -32601, "{method}: {response}");
    }
    let (status, response) = post(
        &client,
        &gateway,
        "helper",
        SECRET_A,
        &json!({"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": []}),
        Some("1.0"),
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(response["error"]["code"], -32602, "{response}");

    // N2: a ProtoJSON snake_case alias of an id or push-bearing field would
    // bypass unwrapping/the D12 refusal, so it is -32602 before any upstream
    // call. Arbitrary metadata keys are not typed fields and stay allowed.
    for params in [
        json!({"message": {"messageId": "m", "role": "ROLE_USER",
                           "parts": [{"text": "x"}], "task_id": "raw"}}),
        json!({"message": message("x"),
               "configuration": {"task_push_notification_config": {"url": "https://x"}}}),
    ] {
        let (status, response) = post(
            &client,
            &gateway,
            "helper",
            SECRET_A,
            &json!({"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": params}),
            Some("1.0"),
            None,
        )
        .await;
        assert_eq!(status, StatusCode::OK, "{response}");
        assert_eq!(response["error"]["code"], -32602, "{params}: {response}");
    }
    let (status, response) = post(
        &client,
        &gateway,
        "helper",
        SECRET_A,
        &json!({"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {
            "message": {"messageId": "m", "role": "ROLE_USER",
                        "parts": [{"text": "x"}],
                        "metadata": {"task_id": "business data"}}
        }}),
        Some("1.0"),
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{response}");
    assert!(response["result"]["task"].is_object(), "{response}");

    // N2 on hook output: a request hook, a response hook and a stream hook that
    // add a snake_case alias are refused with -32602.
    for agent in ["bad_snake_id", "bad_snake_response"] {
        let (status, response) = post(
            &client,
            &gateway,
            agent,
            SECRET_A,
            &rpc_body("SendMessage", json!({"message": message("x")})),
            Some("1.0"),
            None,
        )
        .await;
        assert_eq!(status, StatusCode::OK, "{agent}: {response}");
        assert_eq!(response["error"]["code"], -32602, "{agent}: {response}");
    }
    let (status, events) = stream(&client, &gateway, "bad_snake_stream", SECRET_A, None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        events.last().unwrap()["error"]["code"],
        -32602,
        "{events:?}"
    );

    // m5: a non-streaming method that answers SSE must be refused, not relayed
    // (the extended card rewrite and the response hook live on the non-stream path).
    let (status, response) = post(
        &client,
        &gateway,
        "sse-agent",
        SECRET_A,
        &rpc_body("SendMessage", json!({"message": message("sse")})),
        Some("1.0"),
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{response}");
    assert_eq!(
        response["error"]["code"], -32006,
        "a non-streaming method answered SSE: {response}"
    );

    // Push notification configuration is refused in every entry point (D12).
    let (_, response) = post(
        &client,
        &gateway,
        "helper",
        SECRET_A,
        &rpc_body(
            "SendMessage",
            json!({"message": message("x"), "configuration": {
                "taskPushNotificationConfig": {"url": "https://callback.example.test"}
            }}),
        ),
        Some("1.0"),
        None,
    )
    .await;
    assert_eq!(response["error"]["code"], -32003);
    let (_, response) = post(
        &client,
        &gateway,
        "helper",
        SECRET_A,
        &rpc_body(
            "CreateTaskPushNotificationConfig",
            json!({"url": "https://x.test"}),
        ),
        Some("1.0"),
        None,
    )
    .await;
    assert_eq!(response["error"]["code"], -32003);

    // ListTasks: unsupported without passthrough (D18), forwarded with it.
    let (status, response) = post(
        &client,
        &gateway,
        "helper",
        SECRET_A,
        &rpc_body("ListTasks", json!({})),
        Some("1.0"),
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(response["error"]["code"], -32004);

    let (status, pt_task) = post(
        &client,
        &gateway,
        "pt",
        SECRET_A,
        &rpc_body("SendMessage", json!({"message": message("pt hello")})),
        Some("1.0"),
        Some("user-token"),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{pt_task}");
    assert!(wrapped(&pt_task["result"]["task"]["id"]).starts_with("lwt.key-a."));
    let (status, listed) = post(
        &client,
        &gateway,
        "pt",
        SECRET_A,
        &rpc_body("ListTasks", json!({})),
        Some("1.0"),
        Some("user-token"),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{listed}");
    let tasks = listed["result"]["tasks"].as_array().unwrap();
    assert_eq!(tasks.len(), 1, "{listed}");
    assert!(wrapped(&tasks[0]["id"]).starts_with("lwt.key-a."));

    // Streaming through strip-artifacts: no secret media type, a text artifact,
    // and a terminal status at the end.
    let (status, events) = stream(&client, &gateway, "helper", SECRET_A, None).await;
    assert_eq!(status, StatusCode::OK);
    assert!(events.len() > 2, "{events:?}");
    assert!(events[0]["result"]["task"].is_object(), "{events:?}");
    assert!(
        events.iter().any(|event| {
            event["result"]["artifactUpdate"]["artifact"]["parts"]
                .as_array()
                .is_some_and(|parts| parts.iter().any(|part| part["text"].is_string()))
        }),
        "the text artifact survives: {events:?}"
    );
    for event in &events {
        let text = serde_json::to_string(event).unwrap();
        assert!(
            !text.contains(SECRET_MEDIA),
            "the secret artifact leaked: {event}"
        );
    }
    let last = events.last().unwrap();
    assert_eq!(
        last["result"]["statusUpdate"]["status"]["state"], "TASK_STATE_COMPLETED",
        "{last}"
    );
    assert!(wrapped(&last["result"]["statusUpdate"]["taskId"]).starts_with("lwt.key-a."));

    // L5: a hook that changes the method, adds an id or adds push config is
    // refused before the upstream sees the request.
    for agent in ["bad_method", "bad_add_id", "bad_push"] {
        let (status, response) = post(
            &client,
            &gateway,
            agent,
            SECRET_A,
            &rpc_body("SendMessage", json!({"message": message("x")})),
            Some("1.0"),
            None,
        )
        .await;
        assert_eq!(status, StatusCode::BAD_GATEWAY, "{agent}: {response}");
        assert_eq!(response["error"]["code"], -32603, "{agent}: {response}");
    }

    // L5: a stream hook may only drop an artifact update.
    let (status, events) = stream(&client, &gateway, "bad_drop", SECRET_A, None).await;
    assert_eq!(status, StatusCode::OK);
    let last = events.last().unwrap();
    assert_eq!(last["error"]["code"], -32603, "{events:?}");
    assert!(
        !events
            .iter()
            .any(|event| event["result"]["statusUpdate"].is_object()),
        "status updates were dropped before the error: {events:?}"
    );

    // Per-target timeout: the slow hook is cancelled, the answer is 504.
    let (status, response) = post(
        &client,
        &gateway,
        "slow",
        SECRET_A,
        &rpc_body("SendMessage", json!({"message": message("x")})),
        Some("1.0"),
        None,
    )
    .await;
    assert_eq!(status, StatusCode::GATEWAY_TIMEOUT, "{response}");
    assert_eq!(response["error"]["code"], -32603);
    let metrics = get_metrics(&client, &gateway).await;
    assert!(
        metrics.contains("lumewarp_plugin_calls_total{kind=\"a2a-middleware\",plugin=\"misbehave_test\",result=\"timeout\"}"),
        "{metrics}"
    );

    // Outbound auth failures map like MCP: plugin timeout ⇒ 504, other failures
    // ⇒ 502.
    let (status, response) = post(
        &client,
        &gateway,
        "slow_auth",
        SECRET_A,
        &rpc_body("SendMessage", json!({"message": message("x")})),
        Some("1.0"),
        None,
    )
    .await;
    assert_eq!(status, StatusCode::GATEWAY_TIMEOUT, "{response}");
    assert_eq!(response["error"]["code"], -32603);
    let (status, response) = post(
        &client,
        &gateway,
        "error_auth",
        SECRET_A,
        &rpc_body("SendMessage", json!({"message": message("x")})),
        Some("1.0"),
        None,
    )
    .await;
    assert_eq!(status, StatusCode::BAD_GATEWAY, "{response}");

    // Metrics for a streamed call and the example plugin.
    assert!(
        metrics.contains("lumewarp_a2a_requests_total{agent=\"helper\",method=\"message_stream\",status=\"200\"}"),
        "{metrics}"
    );
    assert!(
        metrics.contains("lumewarp_plugin_calls_total{kind=\"a2a-middleware\",plugin=\"strip_artifacts\",result=\"ok\"}"),
        "{metrics}"
    );

    // The same wrapped id works on a second gateway process sharing the key.
    let addr2 = free_addr();
    let admin2 = free_addr();
    let gateway2 = start_gateway(
        addr2,
        admin2,
        &config(addr2, admin2, helper, pt, sse),
        Some(&plugin_root),
        Some(ID_KEY),
        Some(&format!("http://{addr2}")),
    );
    wait_http(&client, &format!("http://{admin2}/readyz")).await;
    let (status, response) = post(
        &client,
        &gateway2,
        "helper",
        SECRET_A,
        &rpc_body("GetTask", json!({"id": task_id})),
        Some("1.0"),
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{response}");
    assert_eq!(response["result"]["id"], task_id);
    let (_, response) = post(
        &client,
        &gateway2,
        "helper",
        SECRET_B,
        &rpc_body("GetTask", json!({"id": task_id})),
        Some("1.0"),
        None,
    )
    .await;
    assert_eq!(response["error"]["code"], -32001);
}

#[tokio::test]
async fn a2a_without_process_settings_is_503() {
    let client = client();
    let addr = free_addr();
    let admin = free_addr();
    // No mock is needed: the 503 check precedes any upstream call.
    let unused: SocketAddr = "127.0.0.1:1".parse().unwrap();
    let gateway = start_gateway(
        addr,
        admin,
        &config_without_middleware(addr, admin, unused),
        None,
        None,
        Some(&format!("http://{addr}")),
    );
    wait_http(&client, &format!("http://{admin}/readyz")).await;
    let (status, response) = post(
        &client,
        &gateway,
        "helper",
        SECRET_A,
        &rpc_body("SendMessage", json!({"message": message("x")})),
        Some("1.0"),
        None,
    )
    .await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(response["error"]["code"], "a2a_not_configured");
    let (status, _, _) = get(&client, &gateway, "/a2a", SECRET_A).await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
}

async fn get_metrics(client: &TestClient, gateway: &Gateway) -> String {
    let request = Request::builder()
        .uri(format!("http://{}/metrics", gateway.admin))
        .body(Full::new(Bytes::new()))
        .unwrap();
    let response = bounded(client.request(request)).await.unwrap();
    String::from_utf8_lossy(&bytes(response).await).into_owned()
}
