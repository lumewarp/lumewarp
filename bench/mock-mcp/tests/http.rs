//! The mock's contract tests. Assertions come from the MCP specification text,
//! not from this mock's own client: every era, header rule, fault and record.

use base64::Engine as _;
use clap::Parser;
use flate2::read::GzDecoder;
use mock_mcp::{
    Auth, Behaviour, Era, Fault, FaultKind, InstanceConfig, Options, RecordFilter, RequestRecord,
    SessionMode, Shape, ToolConfig,
};
use reqwest::{Client, RequestBuilder, Response, StatusCode};
use serde_json::{json, Value};
use std::io::Read;
use std::sync::Arc;
use std::time::Duration;
use tokio::net::TcpListener;

const MODERN: &str = "2026-07-28";
const LEGACY: &str = "2025-11-25";

struct Mock {
    url: String,
    task: tokio::task::JoinHandle<()>,
}

impl Drop for Mock {
    fn drop(&mut self) {
        self.task.abort();
    }
}

fn options(args: &[&str]) -> Options {
    Options::parse_from(std::iter::once("mock-mcp").chain(args.iter().copied()))
}

async fn start(app: Arc<mock_mcp::App>) -> Mock {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let serving = app.clone();
    let task = tokio::spawn(async move {
        let _ = mock_mcp::serve(listener, serving).await;
    });
    let _ = &app;
    Mock { url, task }
}

fn tool(name: &str) -> ToolConfig {
    ToolConfig {
        name: name.into(),
        ..Default::default()
    }
}

fn server(name: &str, era: Era, tools: Vec<ToolConfig>) -> InstanceConfig {
    InstanceConfig {
        name: name.into(),
        era,
        tools,
        ..Default::default()
    }
}

fn modern_params(params: Value) -> Value {
    let mut params = params;
    params["_meta"] = json!({
        "io.modelcontextprotocol/protocolVersion": MODERN,
        "io.modelcontextprotocol/clientInfo": {"name": "tests", "version": "1"},
        "io.modelcontextprotocol/clientCapabilities": {},
    });
    params
}

fn modern_body(method: &str, id: i64, params: Value) -> Value {
    json!({"jsonrpc": "2.0", "id": id, "method": method, "params": modern_params(params)})
}

fn modern_request(
    client: &Client,
    mock: &Mock,
    name: &str,
    method: &str,
    params: Value,
    id: i64,
    tool_name: Option<&str>,
) -> RequestBuilder {
    let mut request = client
        .post(format!("{}/s/{name}/mcp", mock.url))
        .header("mcp-protocol-version", MODERN)
        .header("mcp-method", method)
        .json(&modern_body(method, id, params));
    if let Some(tool_name) = tool_name {
        request = request.header("mcp-name", tool_name);
    }
    request
}

async fn body(response: Response) -> Value {
    response.json().await.unwrap()
}

async fn unit_response(response: Response, status: StatusCode) -> Value {
    assert_eq!(response.status(), status);
    body(response).await
}

fn record(app: &Arc<mock_mcp::App>, filter: RecordFilter) -> Vec<RequestRecord> {
    app.record_snapshot(&filter)
}

fn records_for(app: &Arc<mock_mcp::App>, server: &str) -> Vec<RequestRecord> {
    record(
        app,
        RecordFilter {
            server: Some(server.into()),
            ..Default::default()
        },
    )
}

async fn wait_for_records<F>(
    app: &Arc<mock_mcp::App>,
    filter: RecordFilter,
    timeout: Duration,
    mut predicate: F,
) -> RequestRecord
where
    F: FnMut(&RequestRecord) -> bool,
{
    let deadline = tokio::time::Instant::now() + timeout;
    loop {
        if let Some(found) = app
            .record_snapshot(&filter)
            .into_iter()
            .find(|record| predicate(record))
        {
            return found;
        }
        if tokio::time::Instant::now() > deadline {
            panic!("timed out; records={:?}", app.record_snapshot(&filter));
        }
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
}

fn sse_messages(bytes: &[u8]) -> Vec<Value> {
    let mut messages = Vec::new();
    for line in bytes.split(|byte| *byte == b'\n') {
        if let Some(data) = line.strip_prefix(b"data: ") {
            messages.push(serde_json::from_slice(data).unwrap());
        }
    }
    messages
}

fn header_value<'a>(record: &'a RequestRecord, name: &str) -> Option<&'a str> {
    record
        .headers
        .get(name)
        .and_then(|values| values.first())
        .map(String::as_str)
}

#[tokio::test]
async fn modern_flow_and_rejections() {
    let app = mock_mcp::App::new(options(&["--workers", "2"]));
    app.create_instance(server(
        "modern",
        Era::Modern,
        vec![tool("echo"), tool("écho")],
    ))
    .unwrap();
    let mock = start(app.clone()).await;
    let client = Client::new();

    let discover = unit_response(
        modern_request(
            &client,
            &mock,
            "modern",
            "server/discover",
            json!({}),
            1,
            None,
        )
        .send()
        .await
        .unwrap(),
        StatusCode::OK,
    )
    .await;
    let discover = &discover["result"];
    assert_eq!(discover["resultType"], "complete");
    assert_eq!(discover["supportedVersions"], json!([MODERN]));
    assert_eq!(discover["capabilities"], json!({"tools": {}}));
    assert_eq!(discover["ttlMs"], 60000);
    assert_eq!(discover["cacheScope"], "public");
    assert!(
        discover["_meta"]["io.modelcontextprotocol/serverInfo"]["name"]
            .as_str()
            .unwrap()
            .starts_with("mock-mcp/")
    );

    let listed = unit_response(
        modern_request(&client, &mock, "modern", "tools/list", json!({}), 2, None)
            .send()
            .await
            .unwrap(),
        StatusCode::OK,
    )
    .await;
    assert_eq!(listed["result"]["tools"][0]["name"], "echo");
    assert_eq!(listed["result"]["ttlMs"], 60000);
    assert_eq!(listed["result"]["cacheScope"], "public");

    let called = unit_response(
        modern_request(
            &client,
            &mock,
            "modern",
            "tools/call",
            json!({"name": "echo", "arguments": {"message": "hi"}}),
            3,
            Some("echo"),
        )
        .send()
        .await
        .unwrap(),
        StatusCode::OK,
    )
    .await;
    assert_eq!(called["result"]["resultType"], "complete");
    assert_eq!(
        called["result"]["content"][0]["text"],
        "{\"message\":\"hi\"}"
    );
    assert_eq!(
        called["result"]["structuredContent"],
        json!({"message": "hi"})
    );

    let unicode = unit_response(
        modern_request(
            &client,
            &mock,
            "modern",
            "tools/call",
            json!({"name": "écho", "arguments": {}}),
            4,
            Some(&format!(
                "=?base64?{}?=",
                base64::engine::general_purpose::STANDARD.encode("écho")
            )),
        )
        .send()
        .await
        .unwrap(),
        StatusCode::OK,
    )
    .await;
    assert_eq!(unicode["result"]["resultType"], "complete");

    let unknown_tool = unit_response(
        modern_request(
            &client,
            &mock,
            "modern",
            "tools/call",
            json!({"name": "missing", "arguments": {}}),
            5,
            Some("missing"),
        )
        .send()
        .await
        .unwrap(),
        StatusCode::OK,
    )
    .await;
    assert_eq!(unknown_tool["error"]["code"], -32602);
    assert!(unknown_tool["error"]["message"]
        .as_str()
        .unwrap()
        .contains("Unknown tool"));

    assert_eq!(
        client
            .get(format!("{}/s/modern/mcp", mock.url))
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::METHOD_NOT_ALLOWED
    );
    assert_eq!(
        client
            .delete(format!("{}/s/modern/mcp", mock.url))
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::METHOD_NOT_ALLOWED
    );

    // A modern server rejects the legacy handshake by naming its own version.
    let initialize = unit_response(
        client
            .post(format!("{}/s/modern/mcp", mock.url))
            .json(&json!({"jsonrpc": "2.0", "id": 6, "method": "initialize",
                "params": {"protocolVersion": LEGACY, "capabilities": {}}}))
            .send()
            .await
            .unwrap(),
        StatusCode::BAD_REQUEST,
    )
    .await;
    assert_eq!(initialize["error"]["code"], -32022);
    assert_eq!(initialize["error"]["data"]["supported"], json!([MODERN]));

    let missing_version = client
        .post(format!("{}/s/modern/mcp", mock.url))
        .header("mcp-method", "tools/list")
        .json(&modern_body("tools/list", 7, json!({})))
        .send()
        .await
        .unwrap();
    assert_eq!(missing_version.status(), StatusCode::BAD_REQUEST);
    assert_eq!(body(missing_version).await["error"]["code"], -32020);

    let version_mismatch = client
        .post(format!("{}/s/modern/mcp", mock.url))
        .header("mcp-protocol-version", LEGACY)
        .header("mcp-method", "tools/list")
        .json(&modern_body("tools/list", 8, json!({})))
        .send()
        .await
        .unwrap();
    assert_eq!(body(version_mismatch).await["error"]["code"], -32020);

    let missing_method = client
        .post(format!("{}/s/modern/mcp", mock.url))
        .header("mcp-protocol-version", MODERN)
        .json(&modern_body("tools/list", 9, json!({})))
        .send()
        .await
        .unwrap();
    assert_eq!(body(missing_method).await["error"]["code"], -32020);

    let method_mismatch = client
        .post(format!("{}/s/modern/mcp", mock.url))
        .header("mcp-protocol-version", MODERN)
        .header("mcp-method", "tools/call")
        .json(&modern_body("tools/list", 10, json!({})))
        .send()
        .await
        .unwrap();
    assert_eq!(body(method_mismatch).await["error"]["code"], -32020);

    let missing_name = modern_request(
        &client,
        &mock,
        "modern",
        "tools/call",
        json!({"name": "echo", "arguments": {}}),
        11,
        None,
    )
    .send()
    .await
    .unwrap();
    assert_eq!(body(missing_name).await["error"]["code"], -32020);

    let name_mismatch = modern_request(
        &client,
        &mock,
        "modern",
        "tools/call",
        json!({"name": "echo", "arguments": {}}),
        12,
        Some("écho"),
    )
    .send()
    .await
    .unwrap();
    assert_eq!(body(name_mismatch).await["error"]["code"], -32020);

    let no_meta = client
        .post(format!("{}/s/modern/mcp", mock.url))
        .header("mcp-protocol-version", MODERN)
        .header("mcp-method", "tools/list")
        .json(&json!({"jsonrpc": "2.0", "id": 13, "method": "tools/list",
            "params": {}}))
        .send()
        .await
        .unwrap();
    assert_eq!(body(no_meta).await["error"]["code"], -32602);

    let mut capabilities = modern_body("tools/list", 14, json!({}));
    capabilities["params"]["_meta"]
        .as_object_mut()
        .unwrap()
        .remove("io.modelcontextprotocol/clientCapabilities");
    let missing_capabilities = client
        .post(format!("{}/s/modern/mcp", mock.url))
        .header("mcp-protocol-version", MODERN)
        .header("mcp-method", "tools/list")
        .json(&capabilities)
        .send()
        .await
        .unwrap();
    assert_eq!(body(missing_capabilities).await["error"]["code"], -32602);

    let mut unsupported_body = modern_body("tools/list", 15, json!({}));
    unsupported_body["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"] =
        json!("1900-01-01");
    let unsupported = client
        .post(format!("{}/s/modern/mcp", mock.url))
        .header("mcp-protocol-version", "1900-01-01")
        .header("mcp-method", "tools/list")
        .json(&unsupported_body)
        .send()
        .await
        .unwrap();
    let unsupported = unit_response(unsupported, StatusCode::BAD_REQUEST).await;
    assert_eq!(unsupported["error"]["code"], -32022);
    assert_eq!(unsupported["error"]["data"]["supported"], json!([MODERN]));

    let unknown_method = unit_response(
        modern_request(&client, &mock, "modern", "no/such", json!({}), 16, None)
            .send()
            .await
            .unwrap(),
        StatusCode::NOT_FOUND,
    )
    .await;
    assert_eq!(unknown_method["error"]["code"], -32601);
}

#[tokio::test]
async fn modern_param_headers() {
    let definition = json!({
        "name": "with_headers",
        "inputSchema": {
            "type": "object",
            "properties": {
                "region": {"type": "string", "x-mcp-header": "Region"},
                "count": {"type": "integer", "x-mcp-header": "Count"},
                "flag": {"type": "boolean", "x-mcp-header": "Flag"},
                "nested": {
                    "type": "object",
                    "properties": {
                        "token": {"type": "string", "x-mcp-header": "Token"},
                    },
                },
            },
        },
    });
    let tool_config = ToolConfig {
        name: "with_headers".into(),
        definition: Some(definition.clone()),
        input_schema: definition["inputSchema"].clone(),
        ..Default::default()
    };
    let app = mock_mcp::App::new(options(&[]));
    app.create_instance(server("params", Era::Modern, vec![tool_config]))
        .unwrap();
    let mock = start(app).await;
    let client = Client::new();

    let call = |arguments: Value| {
        modern_request(
            &client,
            &mock,
            "params",
            "tools/call",
            json!({"name": "with_headers", "arguments": arguments}),
            1,
            Some("with_headers"),
        )
    };

    let ok = call(json!({
        "region": "us-west1",
        "count": 42,
        "flag": true,
        "nested": {"token": "Hello, 世界"},
    }))
    .header("mcp-param-region", "us-west1")
    .header("mcp-param-count", "42.0")
    .header("mcp-param-flag", "true")
    .header(
        "mcp-param-token",
        format!(
            "=?base64?{}?=",
            base64::engine::general_purpose::STANDARD.encode("Hello, 世界")
        ),
    )
    .send()
    .await
    .unwrap();
    assert_eq!(ok.status(), StatusCode::OK);
    assert_eq!(body(ok).await["result"]["resultType"], "complete");

    let missing = call(json!({"region": "us-west1"})).send().await.unwrap();
    assert_eq!(body(missing).await["error"]["code"], -32020);

    let mismatch = call(json!({"region": "us-west1"}))
        .header("mcp-param-region", "us-east1")
        .send()
        .await
        .unwrap();
    assert_eq!(body(mismatch).await["error"]["code"], -32020);

    let unexpected = call(json!({"region": "us-west1"}))
        .header("mcp-param-region", "us-west1")
        .header("mcp-param-flag", "true")
        .send()
        .await
        .unwrap();
    assert_eq!(body(unexpected).await["error"]["code"], -32020);

    let null_with_header = call(json!({"region": "us-west1", "nested": null}))
        .header("mcp-param-region", "us-west1")
        .header("mcp-param-token", "anything")
        .send()
        .await
        .unwrap();
    assert_eq!(body(null_with_header).await["error"]["code"], -32020);

    let padded = call(json!({"region": " padded "}))
        .header(
            "mcp-param-region",
            format!(
                "=?base64?{}?=",
                base64::engine::general_purpose::STANDARD.encode(" padded ")
            ),
        )
        .send()
        .await
        .unwrap();
    assert_eq!(padded.status(), StatusCode::OK);

    let newline = call(json!({"region": "line1\nline2"}))
        .header(
            "mcp-param-region",
            format!(
                "=?base64?{}?=",
                base64::engine::general_purpose::STANDARD.encode("line1\nline2")
            ),
        )
        .send()
        .await
        .unwrap();
    assert_eq!(newline.status(), StatusCode::OK);

    let unknown_ignored = call(json!({"region": "us-west1"}))
        .header("mcp-param-region", "us-west1")
        .header("mcp-param-other", "ignored")
        .send()
        .await
        .unwrap();
    assert_eq!(unknown_ignored.status(), StatusCode::OK);

    let duplicate = call(json!({"region": "us-west1"}))
        .header("mcp-param-region", "us-west1")
        .header("mcp-param-region", "us-west1")
        .send()
        .await
        .unwrap();
    assert_eq!(body(duplicate).await["error"]["code"], -32020);
}

#[tokio::test]
async fn legacy_sessions_and_streams() {
    let app = mock_mcp::App::new(options(&[]));
    app.create_instance(server("legacy", Era::Legacy, vec![tool("echo")]))
        .unwrap();
    let mut streamed_config = server("streamed", Era::Legacy, vec![]);
    streamed_config.get_stream = true;
    app.create_instance(streamed_config).unwrap();
    app.create_instance(InstanceConfig {
        name: "required".into(),
        era: Era::Legacy,
        session: SessionMode::Required,
        tools: vec![tool("echo")],
        ..Default::default()
    })
    .unwrap();
    let mock = start(app).await;
    let client = Client::new();

    let initialize = client
        .post(format!("{}/s/legacy/mcp", mock.url))
        .json(&json!({"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": LEGACY, "capabilities": {},
                "clientInfo": {"name": "tests", "version": "1"}}}))
        .send()
        .await
        .unwrap();
    assert_eq!(initialize.status(), StatusCode::OK);
    let session = initialize
        .headers()
        .get("mcp-session-id")
        .unwrap()
        .to_str()
        .unwrap()
        .to_string();
    let initialized = body(initialize).await;
    assert_eq!(initialized["result"]["protocolVersion"], LEGACY);
    assert!(initialized["result"]["capabilities"]["tools"].is_object());

    let notification = client
        .post(format!("{}/s/legacy/mcp", mock.url))
        .header("mcp-protocol-version", LEGACY)
        .header("mcp-session-id", &session)
        .json(&json!({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        .send()
        .await
        .unwrap();
    assert_eq!(notification.status(), StatusCode::ACCEPTED);
    assert!(notification.bytes().await.unwrap().is_empty());

    let listed = client
        .post(format!("{}/s/legacy/mcp", mock.url))
        .header("mcp-protocol-version", LEGACY)
        .header("mcp-session-id", &session)
        .json(&json!({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}))
        .send()
        .await
        .unwrap();
    let listed = body(listed).await;
    assert!(listed["result"]["resultType"].is_null());
    assert!(listed["result"]["ttlMs"].is_null());
    assert_eq!(listed["result"]["tools"][0]["name"], "echo");

    let called = client
        .post(format!("{}/s/legacy/mcp", mock.url))
        .header("mcp-protocol-version", LEGACY)
        .header("mcp-session-id", &session)
        .json(&json!({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "echo", "arguments": {"x": 1}}}))
        .send()
        .await
        .unwrap();
    let called = body(called).await;
    assert_eq!(called["result"]["structuredContent"], json!({"x": 1}));

    let unknown = client
        .post(format!("{}/s/legacy/mcp", mock.url))
        .header("mcp-session-id", "unknown")
        .header("mcp-protocol-version", LEGACY)
        .json(&json!({"jsonrpc": "2.0", "id": 4, "method": "tools/list"}))
        .send()
        .await
        .unwrap();
    assert_eq!(unknown.status(), StatusCode::NOT_FOUND);

    let required_session = client
        .post(format!("{}/s/required/mcp", mock.url))
        .json(&json!({"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": LEGACY, "capabilities": {}}}))
        .send()
        .await
        .unwrap();
    assert_eq!(required_session.status(), StatusCode::OK);
    let missing = client
        .post(format!("{}/s/required/mcp", mock.url))
        .header("mcp-protocol-version", LEGACY)
        .json(&json!({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}))
        .send()
        .await
        .unwrap();
    assert_eq!(missing.status(), StatusCode::BAD_REQUEST);

    assert_eq!(
        client
            .get(format!("{}/s/legacy/mcp", mock.url))
            .header("mcp-session-id", &session)
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::METHOD_NOT_ALLOWED
    );

    let stream_initialize = client
        .post(format!("{}/s/streamed/mcp", mock.url))
        .json(&json!({"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": LEGACY, "capabilities": {}}}))
        .send()
        .await
        .unwrap();
    let stream_session = stream_initialize
        .headers()
        .get("mcp-session-id")
        .unwrap()
        .to_str()
        .unwrap()
        .to_string();
    let _ = stream_initialize.bytes().await.unwrap();
    let mut stream = client
        .get(format!("{}/s/streamed/mcp", mock.url))
        .header("mcp-session-id", stream_session)
        .send()
        .await
        .unwrap();
    assert_eq!(stream.status(), StatusCode::OK);
    assert!(stream
        .headers()
        .get("content-type")
        .unwrap()
        .to_str()
        .unwrap()
        .starts_with("text/event-stream"));
    let first = stream.chunk().await.unwrap().unwrap();
    assert_eq!(&first[..], b": mock-mcp stream open\n\n");
}

#[tokio::test]
async fn progress_and_mrtr() {
    let app = mock_mcp::App::new(options(&[]));
    app.create_instance(InstanceConfig {
        name: "progress".into(),
        era: Era::Modern,
        response: Shape::Sse,
        tools: vec![ToolConfig {
            name: "progress".into(),
            behaviour: Behaviour::Progress,
            progress_events: 3,
            progress_interval_ms: 1,
            ..Default::default()
        }],
        ..Default::default()
    })
    .unwrap();
    app.create_instance(server(
        "mrtr",
        Era::Modern,
        vec![ToolConfig {
            name: "needs_input".into(),
            behaviour: Behaviour::Mrtr,
            ..Default::default()
        }],
    ))
    .unwrap();
    let mock = start(app).await;
    let client = Client::new();

    let mut progress_body = modern_body(
        "tools/call",
        1,
        json!({"name": "progress", "arguments": {}}),
    );
    progress_body["params"]["_meta"]["progressToken"] = json!("tok");
    let response = client
        .post(format!("{}/s/progress/mcp", mock.url))
        .header("mcp-protocol-version", MODERN)
        .header("mcp-method", "tools/call")
        .header("mcp-name", "progress")
        .json(&progress_body)
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let messages = sse_messages(&response.bytes().await.unwrap());
    assert_eq!(messages.len(), 4);
    for (index, message) in messages[..3].iter().enumerate() {
        assert_eq!(message["method"], "notifications/progress");
        assert_eq!(message["params"]["progress"], json!(index + 1));
        assert_eq!(message["params"]["progressToken"], "tok");
    }
    assert_eq!(messages[3]["result"]["resultType"], "complete");

    let no_capability = client
        .post(format!("{}/s/mrtr/mcp", mock.url))
        .header("mcp-protocol-version", MODERN)
        .header("mcp-method", "tools/call")
        .header("mcp-name", "needs_input")
        .json(&modern_body(
            "tools/call",
            2,
            json!({"name": "needs_input", "arguments": {}}),
        ))
        .send()
        .await
        .unwrap();
    let no_capability = unit_response(no_capability, StatusCode::BAD_REQUEST).await;
    assert_eq!(no_capability["error"]["code"], -32021);
    assert_eq!(
        no_capability["error"]["data"]["requiredCapabilities"],
        json!(["elicitation"])
    );

    let mut capable = modern_body(
        "tools/call",
        3,
        json!({"name": "needs_input", "arguments": {}}),
    );
    capable["params"]["_meta"]["io.modelcontextprotocol/clientCapabilities"] =
        json!({"elicitation": {}});
    let first = client
        .post(format!("{}/s/mrtr/mcp", mock.url))
        .header("mcp-protocol-version", MODERN)
        .header("mcp-method", "tools/call")
        .header("mcp-name", "needs_input")
        .json(&capable)
        .send()
        .await
        .unwrap();
    let first = body(first).await;
    assert_eq!(first["result"]["resultType"], "input_required");
    assert_eq!(
        first["result"]["inputRequests"]["confirm"]["method"],
        "elicitation/create"
    );
    let state = first["result"]["requestState"]
        .as_str()
        .unwrap()
        .to_string();

    let mut retry = modern_body(
        "tools/call",
        4,
        json!({
            "name": "needs_input",
            "arguments": {},
            "inputResponses": {"confirm": {"action": "accept", "content": {"answer": "yes"}}},
            "requestState": state,
        }),
    );
    retry["params"]["_meta"]["io.modelcontextprotocol/clientCapabilities"] =
        json!({"elicitation": {}});
    let completed = client
        .post(format!("{}/s/mrtr/mcp", mock.url))
        .header("mcp-protocol-version", MODERN)
        .header("mcp-method", "tools/call")
        .header("mcp-name", "needs_input")
        .json(&retry)
        .send()
        .await
        .unwrap();
    let completed = body(completed).await;
    assert_eq!(completed["result"]["resultType"], "complete");
    assert!(completed["result"]["content"][0]["text"]
        .as_str()
        .unwrap()
        .contains("MRTR completed"));

    let mut wrong_state = modern_body(
        "tools/call",
        5,
        json!({
            "name": "needs_input",
            "arguments": {},
            "inputResponses": {},
            "requestState": "wrong",
        }),
    );
    wrong_state["params"]["_meta"]["io.modelcontextprotocol/clientCapabilities"] =
        json!({"elicitation": {}});
    let wrong = client
        .post(format!("{}/s/mrtr/mcp", mock.url))
        .header("mcp-protocol-version", MODERN)
        .header("mcp-method", "tools/call")
        .header("mcp-name", "needs_input")
        .json(&wrong_state)
        .send()
        .await
        .unwrap();
    let wrong = body(wrong).await;
    assert_eq!(wrong["error"]["code"], -32602);
}

#[tokio::test]
async fn modern_disconnect_cancels() {
    let app = mock_mcp::App::new(options(&[]));
    app.create_instance(InstanceConfig {
        name: "cancel".into(),
        era: Era::Modern,
        response: Shape::Sse,
        tools: vec![ToolConfig {
            name: "slow".into(),
            behaviour: Behaviour::Sleep,
            sleep_ms: 10_000,
            ..Default::default()
        }],
        ..Default::default()
    })
    .unwrap();
    let mock = start(app.clone()).await;
    let client = Client::new();

    let response = modern_request(
        &client,
        &mock,
        "cancel",
        "tools/call",
        json!({"name": "slow", "arguments": {}}),
        7,
        Some("slow"),
    )
    .send()
    .await
    .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    drop(response);

    let found = wait_for_records(
        &app,
        RecordFilter {
            server: Some("cancel".into()),
            tool: Some("slow".into()),
            ..Default::default()
        },
        Duration::from_secs(5),
        |record| record.cancelled,
    )
    .await;
    assert_eq!(found.cancelled_by.as_deref(), Some("disconnect"));
    assert!(found.disconnected);
    assert!(!found.completed_after_disconnect);
}

#[tokio::test]
async fn legacy_cancellation_and_disconnect() {
    let app = mock_mcp::App::new(options(&[]));
    app.create_instance(InstanceConfig {
        name: "legacy".into(),
        era: Era::Legacy,
        response: Shape::Sse,
        tools: vec![
            ToolConfig {
                name: "slow".into(),
                behaviour: Behaviour::Sleep,
                sleep_ms: 400,
                ..Default::default()
            },
            ToolConfig {
                name: "slower".into(),
                behaviour: Behaviour::Sleep,
                sleep_ms: 5000,
                ..Default::default()
            },
        ],
        ..Default::default()
    })
    .unwrap();
    let mock = start(app.clone()).await;
    let client = Client::new();

    let initialize = client
        .post(format!("{}/s/legacy/mcp", mock.url))
        .json(&json!({"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": LEGACY, "capabilities": {}}}))
        .send()
        .await
        .unwrap();
    let session = initialize
        .headers()
        .get("mcp-session-id")
        .unwrap()
        .to_str()
        .unwrap()
        .to_string();

    let response = client
        .post(format!("{}/s/legacy/mcp", mock.url))
        .header("mcp-protocol-version", LEGACY)
        .header("mcp-session-id", &session)
        .json(&json!({"jsonrpc": "2.0", "id": 11, "method": "tools/call",
            "params": {"name": "slow", "arguments": {}}}))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    drop(response);

    let disconnected = wait_for_records(
        &app,
        RecordFilter {
            server: Some("legacy".into()),
            tool: Some("slow".into()),
            ..Default::default()
        },
        Duration::from_secs(5),
        |record| record.disconnected && record.completed_at_ms.is_some(),
    )
    .await;
    assert!(!disconnected.cancelled);
    assert_eq!(disconnected.outcome.as_deref(), Some("result"));
    assert!(disconnected.completed_after_disconnect);

    let call_client = client.clone();
    let call_url = format!("{}/s/legacy/mcp", mock.url);
    let call_session = session.clone();
    let call = tokio::spawn(async move {
        call_client
            .post(call_url)
            .header("mcp-protocol-version", LEGACY)
            .header("mcp-session-id", call_session)
            .json(&json!({"jsonrpc": "2.0", "id": 12, "method": "tools/call",
                "params": {"name": "slower", "arguments": {}}}))
            .send()
            .await
    });
    wait_for_records(
        &app,
        RecordFilter {
            server: Some("legacy".into()),
            tool: Some("slower".into()),
            ..Default::default()
        },
        Duration::from_secs(5),
        |record| record.completed_at_ms.is_none(),
    )
    .await;
    let cancelled = client
        .post(format!("{}/s/legacy/mcp", mock.url))
        .header("mcp-protocol-version", LEGACY)
        .header("mcp-session-id", &session)
        .json(
            &json!({"jsonrpc": "2.0", "method": "notifications/cancelled",
            "params": {"requestId": 12, "reason": "test"}}),
        )
        .send()
        .await
        .unwrap();
    assert_eq!(cancelled.status(), StatusCode::ACCEPTED);
    let response = call.await.unwrap().unwrap();
    // The instance answers calls over SSE, so headers are 200 even when the
    // cancellation ends the stream without a result.
    assert_eq!(response.status(), StatusCode::OK);
    let record = wait_for_records(
        &app,
        RecordFilter {
            server: Some("legacy".into()),
            tool: Some("slower".into()),
            ..Default::default()
        },
        Duration::from_secs(5),
        |record| record.cancelled,
    )
    .await;
    assert_eq!(record.cancelled_by.as_deref(), Some("notification"));
}

#[tokio::test]
async fn http_faults() {
    let app = mock_mcp::App::new(options(&[]));
    app.create_instance(InstanceConfig {
        name: "auth".into(),
        era: Era::Modern,
        auth: Auth::Bearer {
            token: "upstream-token".into(),
        },
        tools: vec![tool("echo")],
        ..Default::default()
    })
    .unwrap();
    app.create_instance(InstanceConfig {
        name: "faults".into(),
        era: Era::Modern,
        response: Shape::Sse,
        tools: vec![tool("echo")],
        ..Default::default()
    })
    .unwrap();
    let mock = start(app.clone()).await;
    let client = Client::new();

    let request =
        |name: &str| modern_request(&client, &mock, name, "tools/list", json!({}), 1, None);
    let unauthenticated = request("auth").send().await.unwrap();
    assert_eq!(unauthenticated.status(), StatusCode::UNAUTHORIZED);
    assert!(unauthenticated.headers().contains_key("www-authenticate"));
    let wrong = request("auth")
        .header("authorization", "Bearer wrong")
        .send()
        .await
        .unwrap();
    assert_eq!(wrong.status(), StatusCode::UNAUTHORIZED);
    let ok = request("auth")
        .header("authorization", "Bearer upstream-token")
        .send()
        .await
        .unwrap();
    assert_eq!(ok.status(), StatusCode::OK);
    let recorded = records_for(&app, "auth");
    assert!(recorded
        .iter()
        .any(|record| header_value(record, "authorization") == Some("Bearer upstream-token")));

    let origin = "origin";
    app.create_instance(InstanceConfig {
        name: origin.into(),
        era: Era::Modern,
        allowed_origins: vec!["http://allowed.example".into()],
        tools: vec![tool("echo")],
        ..Default::default()
    })
    .unwrap();
    assert_eq!(
        modern_request(&client, &mock, origin, "tools/list", json!({}), 1, None)
            .header("origin", "http://allowed.example")
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::OK
    );
    assert_eq!(
        modern_request(&client, &mock, origin, "tools/list", json!({}), 2, None)
            .header("origin", "http://bad.example")
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::FORBIDDEN
    );

    let set_fault = |fault: Option<Fault>| {
        app.set_fault("faults", fault).unwrap();
    };
    set_fault(Some(Fault {
        kind: FaultKind::ServerError,
        status: Some(500),
        ..Fault::new(FaultKind::ServerError)
    }));
    assert_eq!(
        request("faults").send().await.unwrap().status(),
        StatusCode::INTERNAL_SERVER_ERROR
    );
    set_fault(Some(Fault {
        kind: FaultKind::Unauthorized,
        www_authenticate: Some("Bearer realm=\"fault\"".into()),
        ..Fault::new(FaultKind::ServerError)
    }));
    let unauthorized = request("faults").send().await.unwrap();
    assert_eq!(unauthorized.status(), StatusCode::UNAUTHORIZED);
    assert_eq!(
        unauthorized.headers()["www-authenticate"],
        "Bearer realm=\"fault\""
    );
    set_fault(Some(Fault {
        kind: FaultKind::WrongContentType,
        content_type: Some("text/plain".into()),
        ..Fault::new(FaultKind::ServerError)
    }));
    let wrong_type = request("faults").send().await.unwrap();
    assert!(wrong_type.headers()["content-type"]
        .to_str()
        .unwrap()
        .starts_with("text/plain"));
    let _ = wrong_type.json::<Value>().await.unwrap();
    set_fault(Some(Fault {
        kind: FaultKind::Compressed,
        ..Fault::new(FaultKind::ServerError)
    }));
    let compressed = request("faults").send().await.unwrap();
    assert_eq!(compressed.headers()["content-encoding"], "gzip");
    let bytes = compressed.bytes().await.unwrap();
    let mut decoder = GzDecoder::new(&bytes[..]);
    let mut decoded = String::new();
    decoder.read_to_string(&mut decoded).unwrap();
    let value: Value = serde_json::from_str(&decoded).unwrap();
    assert_eq!(value["result"]["resultType"], "complete");
    set_fault(None);
}

#[tokio::test]
async fn stream_faults() {
    let app = mock_mcp::App::new(options(&[]));
    app.create_instance(InstanceConfig {
        name: "stream".into(),
        era: Era::Modern,
        response: Shape::Sse,
        tools: vec![tool("echo")],
        ..Default::default()
    })
    .unwrap();
    let mock = start(app.clone()).await;
    let client = Client::new();
    let request =
        || modern_request(&client, &mock, "stream", "tools/list", json!({}), 1, None).send();

    app.set_fault(
        "stream",
        Some(Fault {
            kind: FaultKind::OversizedSseEvent,
            bytes: Some(1024),
            ..Fault::new(FaultKind::ServerError)
        }),
    )
    .unwrap();
    let oversized = request().await.unwrap();
    assert_eq!(oversized.status(), StatusCode::OK);
    let bytes = oversized.bytes().await.unwrap();
    assert!(bytes.len() > 1024);
    assert!(bytes.windows(8).any(|window| window == b"xxxxxxxx"));
    let record = records_for(&app, "stream")
        .into_iter()
        .max_by_key(|record| record.seq)
        .unwrap();
    assert_eq!(record.fault.as_deref(), Some("oversized_sse_event"));

    app.set_fault(
        "stream",
        Some(Fault {
            kind: FaultKind::InvalidSse,
            ..Fault::new(FaultKind::ServerError)
        }),
    )
    .unwrap();
    let invalid = request().await.unwrap().bytes().await.unwrap();
    assert!(invalid.starts_with(b"data: {\"jsonrpc\":\"2.0\",\"id\":"));
    assert!(!invalid.windows(8).any(|window| window == b"\"result\""));

    app.set_fault(
        "stream",
        Some(Fault {
            kind: FaultKind::SlowReads,
            events: Some(3),
            interval_ms: Some(1),
            ..Fault::new(FaultKind::ServerError)
        }),
    )
    .unwrap();
    let slow = request().await.unwrap().bytes().await.unwrap();
    assert_eq!(
        slow.windows(6)
            .filter(|window| *window == b": slow")
            .count(),
        3
    );
    assert!(slow.windows(8).any(|window| window == b"\"result\""));

    app.set_fault(
        "stream",
        Some(Fault {
            kind: FaultKind::ResetMidStream,
            after_events: Some(1),
            ..Fault::new(FaultKind::ServerError)
        }),
    )
    .unwrap();
    if let Ok(reset) = request().await {
        assert_eq!(reset.status(), StatusCode::OK);
        assert!(reset.bytes().await.is_err());
    }

    app.set_fault(
        "stream",
        Some(Fault {
            kind: FaultKind::StalledStream,
            ..Fault::new(FaultKind::ServerError)
        }),
    )
    .unwrap();
    let stalled = request().await.unwrap();
    assert_eq!(stalled.status(), StatusCode::OK);
    assert!(
        tokio::time::timeout(Duration::from_millis(200), stalled.bytes())
            .await
            .is_err()
    );
    let record = wait_for_records(
        &app,
        RecordFilter {
            server: Some("stream".into()),
            ..Default::default()
        },
        Duration::from_secs(5),
        |record| record.disconnected,
    )
    .await;
    assert!(record.fault.as_deref() == Some("stalled_stream") || record.outcome.is_some());
}

#[tokio::test]
async fn session_404_once() {
    let app = mock_mcp::App::new(options(&[]));
    app.create_instance(InstanceConfig {
        name: "expiry".into(),
        era: Era::Legacy,
        tools: vec![tool("echo")],
        fault: Some(Fault {
            kind: FaultKind::Session404Once,
            ..Fault::new(FaultKind::ServerError)
        }),
        ..Default::default()
    })
    .unwrap();
    let mock = start(app).await;
    let client = Client::new();

    let initialize = |id: i64| {
        client
            .post(format!("{}/s/expiry/mcp", mock.url))
            .json(&json!({"jsonrpc": "2.0", "id": id, "method": "initialize",
                "params": {"protocolVersion": LEGACY, "capabilities": {}}}))
            .send()
    };
    let first = initialize(1).await.unwrap();
    let session = first
        .headers()
        .get("mcp-session-id")
        .unwrap()
        .to_str()
        .unwrap()
        .to_string();
    let _ = first.bytes().await.unwrap();
    let expired = client
        .post(format!("{}/s/expiry/mcp", mock.url))
        .header("mcp-session-id", &session)
        .header("mcp-protocol-version", LEGACY)
        .json(&json!({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}))
        .send()
        .await
        .unwrap();
    assert_eq!(expired.status(), StatusCode::NOT_FOUND);

    let second = initialize(3).await.unwrap();
    let second_session = second
        .headers()
        .get("mcp-session-id")
        .unwrap()
        .to_str()
        .unwrap()
        .to_string();
    assert_ne!(second_session, session);
    let _ = second.bytes().await.unwrap();
    let recovered = client
        .post(format!("{}/s/expiry/mcp", mock.url))
        .header("mcp-session-id", &second_session)
        .header("mcp-protocol-version", LEGACY)
        .json(&json!({"jsonrpc": "2.0", "id": 4, "method": "tools/list"}))
        .send()
        .await
        .unwrap();
    assert_eq!(recovered.status(), StatusCode::OK);
    let old = client
        .post(format!("{}/s/expiry/mcp", mock.url))
        .header("mcp-session-id", &session)
        .header("mcp-protocol-version", LEGACY)
        .json(&json!({"jsonrpc": "2.0", "id": 5, "method": "tools/list"}))
        .send()
        .await
        .unwrap();
    assert_eq!(old.status(), StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn recording_and_admin_api() {
    let app = mock_mcp::App::new(options(&[]));
    let mock = start(app.clone()).await;
    let client = Client::new();

    let created = client
        .post(format!("{}/admin/servers", mock.url))
        .json(&json!({"name": "admin", "era": "modern",
            "tools": [{"name": "echo"}]}))
        .send()
        .await
        .unwrap();
    assert_eq!(created.status(), StatusCode::CREATED);
    assert_eq!(body(created).await["name"], "admin");

    let duplicate = client
        .post(format!("{}/admin/servers", mock.url))
        .json(&json!({"name": "admin"}))
        .send()
        .await
        .unwrap();
    assert_eq!(duplicate.status(), StatusCode::CONFLICT);

    let invalid = client
        .post(format!("{}/admin/servers", mock.url))
        .json(&json!({"name": "bad name"}))
        .send()
        .await
        .unwrap();
    assert_eq!(invalid.status(), StatusCode::BAD_REQUEST);

    let listed = client
        .get(format!("{}/admin/servers", mock.url))
        .send()
        .await
        .unwrap();
    assert_eq!(body(listed).await["servers"][0]["name"], "admin");

    let updated = client
        .put(format!("{}/admin/servers/admin/tools", mock.url))
        .json(&json!({"tools": [{"name": "echo", "definition": {
            "name": "echo",
            "description": "changed",
            "inputSchema": {"type": "object", "properties": {}}
        }}]}))
        .send()
        .await
        .unwrap();
    assert_eq!(updated.status(), StatusCode::OK);

    let listed_tools = modern_request(&client, &mock, "admin", "tools/list", json!({}), 1, None)
        .send()
        .await
        .unwrap();
    let listed_tools = body(listed_tools).await;
    assert_eq!(listed_tools["result"]["tools"][0]["description"], "changed");

    let upserted = client
        .post(format!("{}/admin/servers/admin/tools", mock.url))
        .json(&json!({"name": "fixed", "behaviour": "fixed",
            "result": {"content": [{"type": "text", "text": "ok"}]}}))
        .send()
        .await
        .unwrap();
    assert_eq!(upserted.status(), StatusCode::OK);
    let removed = client
        .delete(format!("{}/admin/servers/admin/tools/fixed", mock.url))
        .send()
        .await
        .unwrap();
    assert_eq!(removed.status(), StatusCode::OK);

    let faulted = client
        .post(format!("{}/admin/servers/admin/fault", mock.url))
        .json(&json!({"fault": {"kind": "server_error", "status": 503}}))
        .send()
        .await
        .unwrap();
    assert_eq!(faulted.status(), StatusCode::OK);
    assert_eq!(
        modern_request(&client, &mock, "admin", "tools/list", json!({}), 2, None)
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::SERVICE_UNAVAILABLE
    );
    let cleared = client
        .delete(format!("{}/admin/servers/admin/fault", mock.url))
        .send()
        .await
        .unwrap();
    assert_eq!(cleared.status(), StatusCode::OK);

    let call = modern_request(
        &client,
        &mock,
        "admin",
        "tools/call",
        json!({"name": "echo", "arguments": {"payload": "0123456789"}}),
        3,
        Some("echo"),
    )
    .header("authorization", "Bearer visible")
    .header("x-test", "one")
    .send()
    .await
    .unwrap();
    assert_eq!(call.status(), StatusCode::OK);

    let records = app.record_snapshot(&RecordFilter {
        server: Some("admin".into()),
        tool: Some("echo".into()),
        ..Default::default()
    });
    assert_eq!(records.len(), 1);
    let record = &records[0];
    assert_eq!(
        header_value(record, "authorization"),
        Some("Bearer visible")
    );
    assert_eq!(header_value(record, "x-test"), Some("one"));
    assert_eq!(record.method.as_deref(), Some("tools/call"));
    assert_eq!(record.result_type.as_deref(), Some("complete"));
    assert!(record.body.contains("0123456789"));

    assert_eq!(app.clear_records(Some("admin")), 3);
    assert!(app
        .record_snapshot(&RecordFilter {
            server: Some("admin".into()),
            ..Default::default()
        })
        .is_empty());

    app.set_recording(false);
    assert!(!app.recording_enabled());
    let _ = modern_request(&client, &mock, "admin", "tools/list", json!({}), 4, None)
        .send()
        .await
        .unwrap();
    assert!(app
        .record_snapshot(&RecordFilter {
            server: Some("admin".into()),
            ..Default::default()
        })
        .is_empty());
    app.set_recording(true);

    let deleted = client
        .delete(format!("{}/admin/servers/admin", mock.url))
        .send()
        .await
        .unwrap();
    assert_eq!(deleted.status(), StatusCode::OK);
    assert_eq!(app.instance_configs().len(), 0);

    let unknown = client
        .get(format!("{}/admin/servers/nope", mock.url))
        .send()
        .await
        .unwrap();
    assert_eq!(unknown.status(), StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn recording_ring_is_bounded_and_body_truncated() {
    let app = mock_mcp::App::new(options(&[
        "--record-capacity",
        "2",
        "--record-body-limit",
        "8",
    ]));
    let mock = start(app.clone()).await;
    let client = Client::new();
    client
        .post(format!("{}/admin/servers", mock.url))
        .json(&json!({"name": "ring", "tools": [{"name": "echo"}]}))
        .send()
        .await
        .unwrap();
    for id in 0..3 {
        modern_request(
            &client,
            &mock,
            "ring",
            "tools/call",
            json!({"name": "echo", "arguments": {"payload": "0123456789"}}),
            id,
            Some("echo"),
        )
        .send()
        .await
        .unwrap();
    }
    let records = app.record_snapshot(&RecordFilter {
        server: Some("ring".into()),
        ..Default::default()
    });
    assert_eq!(records.len(), 2);
    assert_eq!(records.last().unwrap().seq, 2);
    assert!(records.iter().all(|record| record.body_truncated));
    assert!(records.iter().all(|record| record.body.len() <= 8));

    let no_record = mock_mcp::App::new(options(&["--no-record"]));
    assert!(!no_record.recording_enabled());
    no_record
        .create_instance(server("quiet", Era::Modern, vec![tool("echo")]))
        .unwrap();
    let quiet = start(no_record.clone()).await;
    modern_request(
        &Client::new(),
        &quiet,
        "quiet",
        "tools/list",
        json!({}),
        1,
        None,
    )
    .send()
    .await
    .unwrap();
    assert!(no_record
        .record_snapshot(&RecordFilter::default())
        .is_empty());
}

#[tokio::test]
async fn legacy_mrtr_is_a_tool_error() {
    let app = mock_mcp::App::new(options(&[]));
    app.create_instance(server(
        "legacy",
        Era::Legacy,
        vec![ToolConfig {
            name: "needs_input".into(),
            behaviour: Behaviour::Mrtr,
            ..Default::default()
        }],
    ))
    .unwrap();
    let mock = start(app).await;
    let client = Client::new();
    let initialize = client
        .post(format!("{}/s/legacy/mcp", mock.url))
        .json(&json!({"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": LEGACY, "capabilities": {}}}))
        .send()
        .await
        .unwrap();
    let session = initialize
        .headers()
        .get("mcp-session-id")
        .unwrap()
        .to_str()
        .unwrap()
        .to_string();
    let _ = initialize.bytes().await.unwrap();
    let called = client
        .post(format!("{}/s/legacy/mcp", mock.url))
        .header("mcp-session-id", &session)
        .header("mcp-protocol-version", LEGACY)
        .json(&json!({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "needs_input", "arguments": {}}}))
        .send()
        .await
        .unwrap();
    let called = body(called).await;
    assert_eq!(called["result"]["isError"], true);
    assert!(called["result"]["content"][0]["text"]
        .as_str()
        .unwrap()
        .contains("2026-07-28"));
}
