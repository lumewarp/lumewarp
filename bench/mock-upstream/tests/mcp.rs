//! Integration tests for the example MCP server, speaking raw HTTP.
//!
//! Revision 2025-11-25: initialize/sessions, GET notification stream, and the
//! `ask` tool's `elicitation/create` round trip on the POST SSE stream.

mod support;

use mock_upstream::{
    auth::{AuthMode, AuthOptions},
    mcp::McpOptions,
    util::now_ms,
};
use serde_json::{json, Value};
use std::time::Duration;
use support::{client, read_sse, signed_headers, start_mcp, start_mcp_on, Running, SseReader};

const VERSION: &str = "2025-11-25";

fn init_request(version: Option<&str>) -> Value {
    let mut params = json!({
        "capabilities": {"elicitation": {"form": {}}},
        "clientInfo": {"name": "mcp-integration-test", "version": "1.0.0"},
    });
    if let Some(version) = version {
        params["protocolVersion"] = json!(version);
    }
    json!({"jsonrpc": "2.0", "id": "init", "method": "initialize", "params": params})
}

fn request(id: Value, method: &str, params: Value) -> Value {
    json!({"jsonrpc": "2.0", "id": id, "method": method, "params": params})
}

async fn initialize(server: &Running, version: &str) -> (String, Value) {
    let response = client()
        .post(&server.url)
        .json(&init_request(Some(version)))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 200);
    let session = response
        .headers()
        .get("mcp-session-id")
        .expect("initialize must issue a session")
        .to_str()
        .unwrap()
        .to_owned();
    let body = response.json::<Value>().await.unwrap();
    (session, body)
}

async fn initialized(server: &Running) -> String {
    initialize(server, VERSION).await.0
}

async fn post(server: &Running, session: &str, body: &Value) -> reqwest::Response {
    client()
        .post(&server.url)
        .header("mcp-session-id", session)
        .header("mcp-protocol-version", VERSION)
        .json(body)
        .send()
        .await
        .unwrap()
}

async fn post_raw(
    server: &Running,
    session: Option<&str>,
    body: &[u8],
    extra: &[(&'static str, String)],
) -> reqwest::Response {
    let mut request = client()
        .post(&server.url)
        .header("content-type", "application/json");
    if let Some(session) = session {
        request = request
            .header("mcp-session-id", session)
            .header("mcp-protocol-version", VERSION);
    }
    for (name, value) in extra {
        request = request.header(*name, value);
    }
    request.body(body.to_vec()).send().await.unwrap()
}

#[tokio::test]
async fn initialize_negotiates_version_and_issues_a_session() {
    let server = start_mcp(McpOptions::default()).await;

    let (session, body) = initialize(&server, "2025-11-25").await;
    assert!(!session.is_empty());
    assert!(session.bytes().all(|byte| (0x21..=0x7e).contains(&byte)));
    assert_eq!(body["result"]["protocolVersion"], "2025-11-25");
    assert_eq!(body["result"]["capabilities"]["tools"]["listChanged"], true);
    assert!(body["result"]["capabilities"]["prompts"].is_object());
    assert_eq!(body["result"]["serverInfo"]["name"], "Lumewarp Mock MCP");
    assert!(!body["result"]["serverInfo"]["version"]
        .as_str()
        .unwrap()
        .is_empty());
    assert!(body["result"].get("resultType").is_none());

    // 2025-06-18 is accepted and echoed back.
    let (older_session, older) = initialize(&server, "2025-06-18").await;
    assert_ne!(older_session, session);
    assert_eq!(older["result"]["protocolVersion"], "2025-06-18");

    // An unsupported request gets the version the server supports instead.
    let (_, fallback) = initialize(&server, "1999-01-01").await;
    assert_eq!(fallback["result"]["protocolVersion"], "2025-11-25");
    let response = client()
        .post(&server.url)
        .json(&init_request(None))
        .send()
        .await
        .unwrap();
    let body = response.json::<Value>().await.unwrap();
    assert_eq!(body["result"]["protocolVersion"], "2025-11-25");

    // notifications/initialized is accepted with 202 and no body.
    let response = post_raw(
        &server,
        Some(&session),
        br#"{"jsonrpc":"2.0","method":"notifications/initialized"}"#,
        &[],
    )
    .await;
    assert_eq!(response.status(), 202);
    assert!(response.bytes().await.unwrap().is_empty());
}

#[tokio::test]
async fn session_header_is_required_after_initialize() {
    let server = start_mcp(McpOptions::default()).await;
    let session = initialized(&server).await;
    let list = request(json!(1), "tools/list", json!({}));

    let response = client().post(&server.url).json(&list).send().await.unwrap();
    assert_eq!(response.status(), 400);
    let body = response.json::<Value>().await.unwrap();
    assert_eq!(body["error"]["code"], -32600);

    let response = client()
        .post(&server.url)
        .header("mcp-session-id", "not-a-session")
        .json(&list)
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 404);

    let response = client()
        .post(&server.url)
        .header("mcp-session-id", &session)
        .header("mcp-protocol-version", "2024-01-01")
        .json(&list)
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 400);

    let response = post(&server, &session, &list).await;
    assert_eq!(response.status(), 200);
    let body = response.json::<Value>().await.unwrap();
    let names: Vec<&str> = body["result"]["tools"]
        .as_array()
        .unwrap()
        .iter()
        .map(|tool| tool["name"].as_str().unwrap())
        .collect();
    assert_eq!(names, ["echo", "add", "slow", "ask", "whoami"]);

    // DELETE ends the session; afterwards the id is unknown (404).
    let response = client()
        .delete(&server.url)
        .header("mcp-session-id", &session)
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 204);
    let response = post(&server, &session, &list).await;
    assert_eq!(response.status(), 404);
    let response = client()
        .delete(&server.url)
        .header("mcp-session-id", &session)
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 404);
    let response = client().delete(&server.url).send().await.unwrap();
    assert_eq!(response.status(), 400);
}

#[tokio::test]
async fn tools_list_paginates_with_an_opaque_cursor() {
    let server = start_mcp(McpOptions {
        tools_page_size: 2,
        ..McpOptions::default()
    })
    .await;
    let session = initialized(&server).await;
    let mut names = Vec::new();
    let mut cursor: Option<String> = None;
    let mut pages = 0;
    loop {
        let mut params = json!({});
        if let Some(cursor) = &cursor {
            params["cursor"] = json!(cursor);
        }
        let body = post(&server, &session, &request(json!(1), "tools/list", params))
            .await
            .json::<Value>()
            .await
            .unwrap();
        pages += 1;
        assert!(pages <= 5);
        for tool in body["result"]["tools"].as_array().unwrap() {
            names.push(tool["name"].as_str().unwrap().to_owned());
        }
        match body["result"].get("nextCursor").and_then(Value::as_str) {
            Some(next) => cursor = Some(next.to_owned()),
            None => break,
        }
    }
    assert_eq!(names, ["echo", "add", "slow", "ask", "whoami"]);
    assert_eq!(pages, 3);

    let body = post(
        &server,
        &session,
        &request(json!(2), "tools/list", json!({"cursor": "bogus"})),
    )
    .await
    .json::<Value>()
    .await
    .unwrap();
    assert_eq!(body["error"]["code"], -32602);

    let body = post(
        &server,
        &session,
        &request(json!(3), "prompts/list", json!({})),
    )
    .await
    .json::<Value>()
    .await
    .unwrap();
    assert_eq!(body["result"]["prompts"][0]["name"], "greet");
    let body = post(
        &server,
        &session,
        &request(
            json!(4),
            "prompts/get",
            json!({"name": "greet", "arguments": {"name": "Andy"}}),
        ),
    )
    .await
    .json::<Value>()
    .await
    .unwrap();
    assert!(body["result"]["messages"][0]["content"]["text"]
        .as_str()
        .unwrap()
        .contains("Andy"));
    let body = post(
        &server,
        &session,
        &request(
            json!(5),
            "prompts/get",
            json!({"name": "greet", "arguments": {}}),
        ),
    )
    .await
    .json::<Value>()
    .await
    .unwrap();
    assert_eq!(body["error"]["code"], -32602);
}

#[tokio::test]
async fn tool_calls_reports_headers_and_follows_the_role_header() {
    let server = start_mcp(McpOptions::default()).await;
    let session = initialized(&server).await;

    let body = post(
        &server,
        &session,
        &request(
            json!(1),
            "tools/call",
            json!({"name": "add", "arguments": {"a": 1, "b": 2}}),
        ),
    )
    .await
    .json::<Value>()
    .await
    .unwrap();
    assert_eq!(body["result"]["content"][0]["text"], "3");
    assert_eq!(body["result"]["structuredContent"]["sum"], 3.0);
    assert_eq!(body["result"]["isError"], false);

    // The 2026 headers do not exist in this revision: a stray Mcp-Name must not
    // fail the call.
    let body = post_raw(
        &server,
        Some(&session),
        &serde_json::to_vec(&request(
            json!(2),
            "tools/call",
            json!({"name": "echo", "arguments": {"message": "still works"}}),
        ))
        .unwrap(),
        &[("mcp-name", "some-other-tool".to_owned())],
    )
    .await
    .json::<Value>()
    .await
    .unwrap();
    assert_eq!(body["result"]["content"][0]["text"], "still works");

    let body = post_raw(
        &server,
        Some(&session),
        &serde_json::to_vec(&request(
            json!(3),
            "tools/call",
            json!({"name": "whoami", "arguments": {}}),
        ))
        .unwrap(),
        &[
            ("authorization", "Bearer user-token".to_owned()),
            ("x-lumewarp-key", "lw_key_secret".to_owned()),
        ],
    )
    .await
    .json::<Value>()
    .await
    .unwrap();
    let whoami = &body["result"]["structuredContent"];
    assert_eq!(whoami["authorization"], "Bearer user-token");
    assert_eq!(whoami["saw_lumewarp_key"], true);
    assert_eq!(whoami["mcp_session_id"], session);
    assert_eq!(whoami["protocol_version"], VERSION);
    assert_eq!(whoami["headers"]["x-lumewarp-key"], "lw_key_secret");

    let body = post(
        &server,
        &session,
        &request(
            json!(4),
            "tools/call",
            json!({"name": "admin_only", "arguments": {}}),
        ),
    )
    .await
    .json::<Value>()
    .await
    .unwrap();
    assert_eq!(body["error"]["code"], -32602);
    let body = post_raw(
        &server,
        Some(&session),
        &serde_json::to_vec(&request(
            json!(5),
            "tools/call",
            json!({"name": "admin_only", "arguments": {}}),
        ))
        .unwrap(),
        &[("x-mock-role", "admin".to_owned())],
    )
    .await
    .json::<Value>()
    .await
    .unwrap();
    assert!(body["result"]["content"][0]["text"]
        .as_str()
        .unwrap()
        .contains("admin_only"));
    let body = post_raw(
        &server,
        Some(&session),
        &serde_json::to_vec(&request(json!(6), "tools/list", json!({}))).unwrap(),
        &[("x-mock-role", "admin".to_owned())],
    )
    .await
    .json::<Value>()
    .await
    .unwrap();
    assert!(body["result"]["tools"]
        .as_array()
        .unwrap()
        .iter()
        .any(|tool| tool["name"] == "admin_only"));

    let body = post(
        &server,
        &session,
        &request(
            json!(7),
            "tools/call",
            json!({"name": "nope", "arguments": {}}),
        ),
    )
    .await
    .json::<Value>()
    .await
    .unwrap();
    assert_eq!(body["error"]["code"], -32602);

    let response = post(
        &server,
        &session,
        &request(json!(8), "resources/list", json!({})),
    )
    .await;
    assert_eq!(response.status(), 200);
    let body = response.json::<Value>().await.unwrap();
    assert_eq!(body["error"]["code"], -32601);

    // Input validation is a tool execution error in 2025-11-25, not -32602.
    let body = post(
        &server,
        &session,
        &request(
            json!(9),
            "tools/call",
            json!({"name": "echo", "arguments": {}}),
        ),
    )
    .await
    .json::<Value>()
    .await
    .unwrap();
    assert_eq!(body["result"]["isError"], true);
    assert_eq!(body["error"], Value::Null);

    // No tasks capability is advertised, so a task-augmented call is refused.
    let body = post(
        &server,
        &session,
        &request(
            json!(10),
            "tools/call",
            json!({"name": "echo", "arguments": {"message": "x"}, "task": {"ttl": 1000}}),
        ),
    )
    .await
    .json::<Value>()
    .await
    .unwrap();
    assert_eq!(body["error"]["code"], -32601);
}

#[tokio::test]
async fn slow_streams_progress_notifications_then_the_result() {
    let server = start_mcp(McpOptions::default()).await;
    let session = initialized(&server).await;
    let response = client()
        .post(&server.url)
        .header("mcp-session-id", &session)
        .header("mcp-protocol-version", VERSION)
        .header("accept", "application/json, text/event-stream")
        .json(&request(
            json!("slow-1"),
            "tools/call",
            json!({
                "name": "slow",
                "arguments": {"delay_ms": 5, "steps": 2},
                "_meta": {"progressToken": "progress-1"},
            }),
        ))
        .send()
        .await
        .unwrap();
    let events = read_sse(response).await;
    let progress: Vec<&Value> = events
        .iter()
        .filter(|event| event["method"] == "notifications/progress")
        .collect();
    assert_eq!(progress.len(), 2);
    assert_eq!(progress[0]["params"]["progress"], 1);
    assert_eq!(progress[1]["params"]["progress"], 2);
    assert_eq!(progress[0]["params"]["progressToken"], "progress-1");
    let final_response = events.last().expect("a final JSON-RPC response frame");
    assert_eq!(final_response["id"], "slow-1");
    assert!(final_response["result"]["content"][0]["text"]
        .as_str()
        .unwrap()
        .contains("slept 10 ms"));

    // Without an SSE Accept the same call is a plain JSON response.
    let body = post(
        &server,
        &session,
        &request(
            json!(2),
            "tools/call",
            json!({"name": "slow", "arguments": {"delay_ms": 0, "steps": 1}}),
        ),
    )
    .await
    .json::<Value>()
    .await
    .unwrap();
    assert!(body["result"]["content"][0]["text"]
        .as_str()
        .unwrap()
        .contains("slept 0 ms"));
}

#[tokio::test]
async fn ask_elicits_on_the_post_stream_and_uses_the_response_post() {
    let server = start_mcp(McpOptions::default()).await;
    let session = initialized(&server).await;
    let body = request(
        json!("call-1"),
        "tools/call",
        json!({"name": "ask", "arguments": {"question": "What is the answer?"}}),
    );
    let response = client()
        .post(&server.url)
        .header("mcp-session-id", &session)
        .header("mcp-protocol-version", VERSION)
        .header("accept", "application/json, text/event-stream")
        .json(&body)
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 200);
    let mut reader = SseReader::new(response);
    let elicit = tokio::time::timeout(Duration::from_secs(5), reader.next_event())
        .await
        .expect("an elicitation request frame")
        .expect("stream open");
    assert_eq!(elicit["method"], "elicitation/create");
    assert!(elicit.get("id").is_some());
    assert_eq!(elicit["params"]["message"], "What is the answer?");
    assert_eq!(elicit["params"]["requestedSchema"]["required"][0], "answer");
    let elicit_id = elicit["id"].clone();

    let response_body = json!({
        "jsonrpc": "2.0",
        "id": elicit_id,
        "result": {"action": "accept", "content": {"answer": "42"}},
    });
    let response = post_raw(
        &server,
        Some(&session),
        &serde_json::to_vec(&response_body).unwrap(),
        &[],
    )
    .await;
    assert_eq!(response.status(), 202);
    assert!(response.bytes().await.unwrap().is_empty());

    let final_response = tokio::time::timeout(Duration::from_secs(5), reader.next_event())
        .await
        .expect("the tool result frame")
        .expect("stream open");
    assert_eq!(final_response["id"], "call-1");
    assert!(final_response["result"]["content"][0]["text"]
        .as_str()
        .unwrap()
        .contains("42"));
    assert_eq!(
        final_response["result"]["structuredContent"]["action"],
        "accept"
    );
    let closed = tokio::time::timeout(Duration::from_secs(2), reader.next_event()).await;
    assert!(closed.map(|event| event.is_none()).unwrap_or(true));

    // A response for a session that does not own the pending request is a 400.
    let other_session = initialized(&server).await;
    let response = client()
        .post(&server.url)
        .header("mcp-session-id", &session)
        .header("mcp-protocol-version", VERSION)
        .header("accept", "text/event-stream")
        .json(&request(
            json!("call-2"),
            "tools/call",
            json!({"name": "ask", "arguments": {"question": "again"}}),
        ))
        .send()
        .await
        .unwrap();
    let mut reader = SseReader::new(response);
    let elicit = reader.next_event().await.expect("elicitation");
    let response = post_raw(
        &server,
        Some(&other_session),
        &serde_json::to_vec(&json!({
            "jsonrpc": "2.0",
            "id": elicit["id"],
            "result": {"action": "accept", "content": {"answer": "wrong session"}},
        }))
        .unwrap(),
        &[],
    )
    .await;
    assert_eq!(response.status(), 400);
    // The correct session resolves it.
    let response = post_raw(
        &server,
        Some(&session),
        &serde_json::to_vec(&json!({
            "jsonrpc": "2.0",
            "id": elicit["id"],
            "result": {"action": "accept", "content": {"answer": "right session"}},
        }))
        .unwrap(),
        &[],
    )
    .await;
    assert_eq!(response.status(), 202);
    let final_response = reader.next_event().await.expect("tool result");
    assert!(final_response["result"]["content"][0]["text"]
        .as_str()
        .unwrap()
        .contains("right session"));

    // Unknown ids are rejected, not silently accepted.
    let response = post_raw(
        &server,
        Some(&session),
        &serde_json::to_vec(&json!({
            "jsonrpc": "2.0",
            "id": "elicit-does-not-exist",
            "result": {"action": "accept"},
        }))
        .unwrap(),
        &[],
    )
    .await;
    assert_eq!(response.status(), 400);

    // Without an SSE stream the tool reports isError instead of hanging.
    let response = post(
        &server,
        &session,
        &request(
            json!("call-3"),
            "tools/call",
            json!({"name": "ask", "arguments": {"question": "no stream"}}),
        ),
    )
    .await
    .json::<Value>()
    .await
    .unwrap();
    assert_eq!(response["result"]["isError"], true);
}

#[tokio::test]
async fn ask_requires_the_client_elicitation_capability() {
    let server = start_mcp(McpOptions::default()).await;
    let response = client()
        .post(&server.url)
        .json(&json!({
            "jsonrpc": "2.0",
            "id": "init",
            "method": "initialize",
            "params": {
                "protocolVersion": VERSION,
                "capabilities": {},
                "clientInfo": {"name": "no-elicitation", "version": "1.0.0"},
            },
        }))
        .send()
        .await
        .unwrap();
    let session = response.headers()["mcp-session-id"]
        .to_str()
        .unwrap()
        .to_owned();
    let response = client()
        .post(&server.url)
        .header("mcp-session-id", session)
        .header("mcp-protocol-version", VERSION)
        .header("accept", "text/event-stream")
        .json(&request(
            json!(1),
            "tools/call",
            json!({"name": "ask", "arguments": {"question": "hello?"}}),
        ))
        .send()
        .await
        .unwrap();
    let body: Value = response.json().await.unwrap();
    assert_eq!(body["result"]["isError"], true);
    assert!(body["result"]["content"][0]["text"]
        .as_str()
        .unwrap()
        .contains("elicitation capability"));
}

#[tokio::test]
async fn ask_times_out_when_the_client_never_answers() {
    let server = start_mcp(McpOptions {
        elicitation_timeout_ms: 100,
        ..McpOptions::default()
    })
    .await;
    let session = initialized(&server).await;
    let response = client()
        .post(&server.url)
        .header("mcp-session-id", session)
        .header("mcp-protocol-version", VERSION)
        .header("accept", "text/event-stream")
        .json(&request(
            json!(1),
            "tools/call",
            json!({"name": "ask", "arguments": {"question": "hello?"}}),
        ))
        .send()
        .await
        .unwrap();
    let events = read_sse(response).await;
    let final_response = events.last().unwrap();
    assert_eq!(final_response["result"]["isError"], true);
    assert!(final_response["result"]["content"][0]["text"]
        .as_str()
        .unwrap()
        .contains("no elicitation response"));
}

#[tokio::test]
async fn get_stream_delivers_tools_list_changed_notifications() {
    let server = start_mcp(McpOptions::default()).await;
    let session = initialized(&server).await;
    let response = client()
        .get(&server.url)
        .header("mcp-session-id", &session)
        .header("mcp-protocol-version", VERSION)
        .header("accept", "text/event-stream")
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 200);
    let mut reader = SseReader::new(response);

    let response = client()
        .post(format!(
            "{}/_mock/tool/description",
            server.url.trim_end_matches("/mcp")
        ))
        .json(&json!({"name": "echo", "description": "Changed by the control endpoint."}))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 200);
    let notification = tokio::time::timeout(Duration::from_secs(5), reader.next_event())
        .await
        .expect("a notification on the GET stream")
        .expect("stream open");
    assert_eq!(notification["method"], "notifications/tools/list_changed");

    let body = post(
        &server,
        &session,
        &request(json!(1), "tools/list", json!({})),
    )
    .await
    .json::<Value>()
    .await
    .unwrap();
    let echo = body["result"]["tools"]
        .as_array()
        .unwrap()
        .iter()
        .find(|tool| tool["name"] == "echo")
        .unwrap();
    assert_eq!(echo["description"], "Changed by the control endpoint.");

    // Adding and removing tools also notifies.
    let base = server.url.trim_end_matches("/mcp");
    let response = client()
        .post(format!("{base}/_mock/tool"))
        .json(&json!({
            "name": "added_by_control",
            "description": "Added at runtime.",
            "inputSchema": {"type": "object", "properties": {}},
        }))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 200);
    let notification = tokio::time::timeout(Duration::from_secs(5), reader.next_event())
        .await
        .expect("notification")
        .expect("stream open");
    assert_eq!(notification["method"], "notifications/tools/list_changed");
    let body = post(
        &server,
        &session,
        &request(json!(2), "tools/list", json!({})),
    )
    .await
    .json::<Value>()
    .await
    .unwrap();
    assert!(body["result"]["tools"]
        .as_array()
        .unwrap()
        .iter()
        .any(|tool| tool["name"] == "added_by_control"));

    let response = client()
        .post(format!("{base}/_mock/tool/remove"))
        .json(&json!({"name": "added_by_control"}))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 200);
    let notification = tokio::time::timeout(Duration::from_secs(5), reader.next_event())
        .await
        .expect("notification")
        .expect("stream open");
    assert_eq!(notification["method"], "notifications/tools/list_changed");
    let body = post(
        &server,
        &session,
        &request(json!(3), "tools/list", json!({})),
    )
    .await
    .json::<Value>()
    .await
    .unwrap();
    assert!(!body["result"]["tools"]
        .as_array()
        .unwrap()
        .iter()
        .any(|tool| tool["name"] == "added_by_control"));

    // GET without a session is rejected before the stream starts.
    let response = client()
        .get(&server.url)
        .header("accept", "text/event-stream")
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 400);
}

#[tokio::test]
async fn control_endpoints_log_headers_and_do_not_log_themselves() {
    let server = start_mcp(McpOptions::default()).await;
    let session = initialized(&server).await;
    let body = request(json!(1), "tools/list", json!({}));
    let response = post_raw(
        &server,
        Some(&session),
        &serde_json::to_vec(&body).unwrap(),
        &[
            ("authorization", "Bearer upstream-token".to_owned()),
            ("x-lumewarp-key", "lw_key_123".to_owned()),
        ],
    )
    .await;
    assert_eq!(response.status(), 200);
    let base = server.url.trim_end_matches("/mcp");
    let entries: Value = client()
        .get(format!("{base}/_mock/headers?n=10"))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    let entries = entries["headers"].as_array().unwrap();
    let last = entries.last().unwrap();
    assert_eq!(last["path"], "/mcp");
    assert_eq!(last["headers"]["authorization"], "Bearer upstream-token");
    assert_eq!(last["headers"]["x-lumewarp-key"], "lw_key_123");
    assert_eq!(last["headers"]["mcp-session-id"], session);
    assert_eq!(last["headers"]["mcp-protocol-version"], VERSION);

    let tools: Value = client()
        .get(format!("{base}/_mock/tools"))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert!(tools["tools"]
        .as_array()
        .unwrap()
        .iter()
        .any(|tool| tool["name"] == "ask"));
}

#[tokio::test]
async fn control_endpoints_refuse_non_loopback_peers() {
    let Some(ip) = support::non_loopback_ip() else {
        eprintln!("skipping: no non-loopback IPv4 address on this host");
        return;
    };
    let server = start_mcp_on("0.0.0.0:0", McpOptions::default()).await;
    let Ok(response) = client()
        .get(format!("http://{ip}:{}/_mock/headers", server.addr.port()))
        .send()
        .await
    else {
        eprintln!("skipping: cannot reach the listener through {ip}");
        return;
    };
    assert_eq!(response.status(), 403);
    let port = server.addr.port();
    assert_eq!(
        client()
            .get(format!("http://127.0.0.1:{port}/_mock/headers"))
            .send()
            .await
            .unwrap()
            .status(),
        200
    );
}

#[tokio::test]
async fn bearer_any_bearer_and_hmac_authentication() {
    // Fixed bearer: initialize itself is protected.
    let server = start_mcp(McpOptions {
        auth: AuthOptions {
            auth: AuthMode::Bearer,
            bearer: Some("fixed-token".to_owned()),
            ..AuthOptions::default()
        },
        ..McpOptions::default()
    })
    .await;
    let response = client()
        .post(&server.url)
        .json(&init_request(Some(VERSION)))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 401);
    assert!(response.headers().contains_key("www-authenticate"));
    let response = client()
        .post(&server.url)
        .header("authorization", "Bearer fixed-token")
        .json(&init_request(Some(VERSION)))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 200);

    // Any bearer simulates a passthrough user token.
    let server = start_mcp(McpOptions {
        auth: AuthOptions {
            auth: AuthMode::AnyBearer,
            ..AuthOptions::default()
        },
        ..McpOptions::default()
    })
    .await;
    let response = client()
        .post(&server.url)
        .json(&init_request(Some(VERSION)))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 401);
    let response = client()
        .post(&server.url)
        .header("authorization", "Bearer user-supplied")
        .json(&init_request(Some(VERSION)))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 200);

    // HMAC: good, wrong secret, stale, and a body/path mismatch.
    let secret = "mcp-shared-secret";
    let server = start_mcp(McpOptions {
        auth: AuthOptions {
            auth: AuthMode::Hmac,
            hmac_secret: Some(secret.to_owned()),
            hmac_skew_ms: 5_000,
            ..AuthOptions::default()
        },
        ..McpOptions::default()
    })
    .await;
    let body = serde_json::to_vec(&init_request(Some(VERSION))).unwrap();
    let path = "/mcp";
    let signed = |timestamp: u64, secret: &str, path: &str| {
        signed_headers(secret, timestamp, "POST", path, &body)
    };
    let now = now_ms();
    assert_eq!(
        post_raw(&server, None, &body, &signed(now, secret, path))
            .await
            .status(),
        200
    );
    assert_eq!(
        post_raw(&server, None, &body, &signed(now, "wrong", path))
            .await
            .status(),
        401
    );
    assert_eq!(
        post_raw(&server, None, &body, &signed(now - 60_000, secret, path))
            .await
            .status(),
        401
    );
    assert_eq!(
        post_raw(&server, None, &body, &signed(now, secret, "/other"))
            .await
            .status(),
        401
    );
    assert_eq!(post_raw(&server, None, &body, &[]).await.status(), 401);
}

#[tokio::test]
async fn origin_is_validated() {
    let server = start_mcp(McpOptions {
        allow_origins: vec!["https://allowed.example".to_owned()],
        ..McpOptions::default()
    })
    .await;
    let body = serde_json::to_vec(&init_request(Some(VERSION))).unwrap();
    assert_eq!(
        post_raw(
            &server,
            None,
            &body,
            &[("origin", "https://evil.example".to_owned())],
        )
        .await
        .status(),
        403
    );
    assert_eq!(
        post_raw(
            &server,
            None,
            &body,
            &[("origin", "http://localhost:5173".to_owned())],
        )
        .await
        .status(),
        200
    );
    assert_eq!(
        post_raw(
            &server,
            None,
            &body,
            &[("origin", "https://allowed.example".to_owned())],
        )
        .await
        .status(),
        200
    );
}

#[tokio::test]
async fn parse_errors_and_notifications_use_the_transport_statuses() {
    let server = start_mcp(McpOptions::default()).await;
    let response = post_raw(&server, None, b"{not json", &[]).await;
    assert_eq!(response.status(), 400);
    let body = response.json::<Value>().await.unwrap();
    assert_eq!(body["error"]["code"], -32700);

    let session = initialized(&server).await;
    // A notification with a session is 202 with no body.
    let response = post_raw(
        &server,
        Some(&session),
        br#"{"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":1}}"#,
        &[],
    )
    .await;
    assert_eq!(response.status(), 202);
    assert!(response.bytes().await.unwrap().is_empty());

    // A notification without a session is rejected.
    let response = post_raw(
        &server,
        None,
        br#"{"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":1}}"#,
        &[],
    )
    .await;
    assert_eq!(response.status(), 400);

    // ping is part of this revision.
    let body = post(&server, &session, &request(json!(1), "ping", json!({})))
        .await
        .json::<Value>()
        .await
        .unwrap();
    assert_eq!(body["result"], json!({}));

    // A null id is not a notification in JSON-RPC.
    let response = post_raw(
        &server,
        Some(&session),
        br#"{"jsonrpc":"2.0","id":null,"method":"tools/list","params":{}}"#,
        &[],
    )
    .await;
    assert_eq!(response.status(), 400);
}
