//! Integration tests for the example A2A agent, speaking raw HTTP.

mod support;

use mock_upstream::{
    a2a::A2aOptions,
    auth::{AuthMode, AuthOptions},
    util::now_ms,
};
use serde_json::{json, Value};
use std::time::Duration;
use support::{client, post_rpc, read_sse, rpc_body, signed_headers, start_a2a, start_a2a_on};

fn options() -> A2aOptions {
    A2aOptions {
        stream_step_ms: 5,
        ..A2aOptions::default()
    }
}

fn message(text: &str) -> Value {
    json!({
        "messageId": format!("msg-{text}"),
        "role": "ROLE_USER",
        "parts": [{"text": text, "mediaType": "text/plain"}],
    })
}

async fn send(base: &str, configuration: Option<Value>) -> Value {
    let mut params = json!({"message": message("hello")});
    if let Some(configuration) = configuration {
        params["configuration"] = configuration;
    }
    let response = post_rpc(
        &client(),
        base,
        "/a2a",
        &rpc_body(json!(1), "SendMessage", params),
    )
    .await;
    assert_eq!(response.status(), 200);
    response.json::<Value>().await.unwrap()
}

#[tokio::test]
async fn agent_card_is_well_known_and_cacheable() {
    let server = start_a2a(options()).await;
    let client = client();
    let response = client
        .get(format!("{}/.well-known/agent-card.json", server.url))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 200);
    let etag = response.headers()["etag"].to_str().unwrap().to_owned();
    assert!(response.headers()["cache-control"]
        .to_str()
        .unwrap()
        .contains("max-age"));
    let card: Value = response.json().await.unwrap();
    assert_eq!(card["name"], "Lumewarp Mock A2A Agent");
    assert!(card["description"].as_str().unwrap().len() > 20);
    assert_eq!(card["version"], env!("CARGO_PKG_VERSION"));
    assert_eq!(card["supportedInterfaces"][0]["protocolBinding"], "JSONRPC");
    assert_eq!(card["supportedInterfaces"][0]["protocolVersion"], "1.0");
    assert_eq!(
        card["supportedInterfaces"][0]["url"],
        format!("{}/a2a", server.url)
    );
    assert_eq!(card["capabilities"]["streaming"], true);
    assert_eq!(card["capabilities"]["pushNotifications"], false);
    assert!(!card["defaultInputModes"].as_array().unwrap().is_empty());
    assert!(!card["defaultOutputModes"].as_array().unwrap().is_empty());
    assert_eq!(card["skills"][0]["id"], "echo");
    // A dummy JWS so a test can prove the gateway drops upstream signatures.
    assert_eq!(
        card["signatures"][0]["signature"],
        "bm90LWEtcmVhbC1zaWduYXR1cmU"
    );
    assert!(card.get("url").is_none());
    assert!(card.get("protocolVersion").is_none());

    let response = client
        .get(format!("{}/.well-known/agent-card.json", server.url))
        .header("if-none-match", etag)
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 304);
}

#[tokio::test]
async fn send_message_returns_a_completed_task_with_artifacts() {
    let server = start_a2a(options()).await;
    let response = send(&server.url, None).await;
    let task = &response["result"]["task"];
    assert!(task["id"].as_str().unwrap().starts_with("task-"));
    assert!(task["contextId"].as_str().unwrap().starts_with("ctx-"));
    assert_eq!(task["status"]["state"], "TASK_STATE_COMPLETED");
    let timestamp = task["status"]["timestamp"].as_str().unwrap();
    assert!(timestamp.ends_with('Z'));
    assert_eq!(timestamp.len(), "2026-11-08T07:00:00.000Z".len());
    let artifacts = task["artifacts"].as_array().unwrap();
    assert!(artifacts.iter().any(|artifact| {
        artifact["parts"][0]["mediaType"] == "text/plain"
            && artifact["parts"][0]["text"]
                .as_str()
                .unwrap()
                .contains("Echo: hello")
    }));
    assert!(artifacts.iter().any(|artifact| artifact["parts"]
        .as_array()
        .unwrap()
        .iter()
        .any(|part| part["mediaType"] == "application/x-mock-secret")));
    let history = task["history"].as_array().unwrap();
    assert!(history.len() >= 2);
    assert_eq!(history[0]["role"], "ROLE_USER");
    assert_eq!(history[history.len() - 1]["role"], "ROLE_AGENT");
    assert!(task.get("kind").is_none());

    let task_id = task["id"].as_str().unwrap();
    let response = post_rpc(
        &client(),
        &server.url,
        "/a2a",
        &rpc_body(
            json!("get"),
            "GetTask",
            json!({"id": task_id, "historyLength": 1}),
        ),
    )
    .await;
    let body: Value = response.json().await.unwrap();
    assert_eq!(body["result"]["status"]["state"], "TASK_STATE_COMPLETED");
    assert_eq!(body["result"]["history"].as_array().unwrap().len(), 1);
    assert!(!body["result"]["artifacts"].as_array().unwrap().is_empty());
}

#[tokio::test]
async fn send_streaming_message_streams_task_status_and_artifacts() {
    let server = start_a2a(options()).await;
    let response = post_rpc(
        &client(),
        &server.url,
        "/a2a",
        &rpc_body(
            json!(7),
            "SendStreamingMessage",
            json!({"message": message("stream this")}),
        ),
    )
    .await;
    assert_eq!(response.status(), 200);
    let events = read_sse(response).await;
    assert!(events.len() >= 4, "expected several events, got {events:?}");
    assert!(events.iter().all(|event| event["id"] == 7));
    assert_eq!(
        events[0]["result"]["task"]["status"]["state"],
        "TASK_STATE_WORKING"
    );
    let task_id = events[0]["result"]["task"]["id"]
        .as_str()
        .unwrap()
        .to_owned();

    let statuses: Vec<&Value> = events
        .iter()
        .filter_map(|event| event["result"].get("statusUpdate"))
        .collect();
    assert!(statuses.iter().any(|update| {
        update["taskId"] == task_id && update["status"]["state"] == "TASK_STATE_WORKING"
    }));
    let final_status = statuses
        .iter()
        .find(|update| update["status"]["state"] == "TASK_STATE_COMPLETED")
        .expect("a completed status update");
    assert_eq!(
        final_status["contextId"],
        events[0]["result"]["task"]["contextId"]
    );
    assert!(events
        .iter()
        .position(|event| event["result"]
            .get("statusUpdate")
            .is_some_and(|update| { update["status"]["state"] == "TASK_STATE_COMPLETED" }))
        .is_some_and(|index| index + 1 == events.len()));

    let artifacts: Vec<&Value> = events
        .iter()
        .filter_map(|event| event["result"].get("artifactUpdate"))
        .collect();
    assert_eq!(
        artifacts.len(),
        2,
        "text and secret artifacts: {artifacts:?}"
    );
    assert!(artifacts
        .iter()
        .any(|update| update["artifact"]["parts"][0]["text"]
            .as_str()
            .is_some_and(|text| text.contains("Echo: stream this"))));
    assert!(artifacts
        .iter()
        .any(|update| update["artifact"]["parts"][0]["mediaType"] == "application/x-mock-secret"));
    assert!(artifacts.iter().all(|update| update["lastChunk"] == true));
    assert!(events.iter().all(|event| event.get("kind").is_none()));
}

#[tokio::test]
async fn list_tasks_paginates_and_hides_artifacts_by_default() {
    let server = start_a2a(options()).await;
    for index in 0..3 {
        let response = send(&server.url, None).await;
        assert_eq!(
            response["result"]["task"]["status"]["state"],
            "TASK_STATE_COMPLETED"
        );
        let _ = index;
    }
    let response = post_rpc(
        &client(),
        &server.url,
        "/a2a",
        &rpc_body(json!(1), "ListTasks", json!({"pageSize": 2})),
    )
    .await;
    let body: Value = response.json().await.unwrap();
    let result = &body["result"];
    assert_eq!(result["tasks"].as_array().unwrap().len(), 2);
    assert_eq!(result["nextPageToken"], "2");
    assert_eq!(result["pageSize"], 2);
    assert_eq!(result["totalSize"], 3);
    for task in result["tasks"].as_array().unwrap() {
        assert!(task.get("artifacts").is_none(), "artifacts must be omitted");
        assert!(task.get("history").is_none());
    }

    let response = post_rpc(
        &client(),
        &server.url,
        "/a2a",
        &rpc_body(
            json!(2),
            "ListTasks",
            json!({"pageSize": 2, "pageToken": "2"}),
        ),
    )
    .await;
    let body: Value = response.json().await.unwrap();
    assert_eq!(body["result"]["tasks"].as_array().unwrap().len(), 1);
    assert_eq!(body["result"]["nextPageToken"], "");

    let response = post_rpc(
        &client(),
        &server.url,
        "/a2a",
        &rpc_body(
            json!(3),
            "ListTasks",
            json!({"includeArtifacts": true, "historyLength": 1}),
        ),
    )
    .await;
    let body: Value = response.json().await.unwrap();
    let tasks = body["result"]["tasks"].as_array().unwrap();
    assert!(tasks
        .iter()
        .all(|task| !task["artifacts"].as_array().unwrap().is_empty()));
    assert!(tasks
        .iter()
        .all(|task| task["history"].as_array().unwrap().len() == 1));
}

#[tokio::test]
async fn cancel_task_and_task_errors() {
    let server = start_a2a(A2aOptions {
        stream_step_ms: 300,
        ..A2aOptions::default()
    })
    .await;
    let response = send(&server.url, Some(json!({"returnImmediately": true}))).await;
    let task = &response["result"]["task"];
    assert_eq!(task["status"]["state"], "TASK_STATE_WORKING");
    let task_id = task["id"].as_str().unwrap().to_owned();

    let response = post_rpc(
        &client(),
        &server.url,
        "/a2a",
        &rpc_body(json!(1), "CancelTask", json!({"id": task_id})),
    )
    .await;
    let body: Value = response.json().await.unwrap();
    assert_eq!(body["result"]["status"]["state"], "TASK_STATE_CANCELED");

    let response = post_rpc(
        &client(),
        &server.url,
        "/a2a",
        &rpc_body(json!(2), "CancelTask", json!({"id": task_id})),
    )
    .await;
    let body: Value = response.json().await.unwrap();
    assert_eq!(body["error"]["code"], -32002);
    assert_eq!(
        body["error"]["data"][0]["@type"],
        "type.googleapis.com/google.rpc.ErrorInfo"
    );

    let response = post_rpc(
        &client(),
        &server.url,
        "/a2a",
        &rpc_body(json!(3), "GetTask", json!({"id": "missing"})),
    )
    .await;
    let body: Value = response.json().await.unwrap();
    assert_eq!(body["error"]["code"], -32001);
    assert_eq!(body["error"]["data"][0]["metadata"]["taskId"], "missing");
}

#[tokio::test]
async fn push_notification_methods_are_refused() {
    let server = start_a2a(options()).await;
    let client = client();
    for method in [
        "CreateTaskPushNotificationConfig",
        "GetTaskPushNotificationConfig",
        "ListTaskPushNotificationConfigs",
        "DeleteTaskPushNotificationConfig",
    ] {
        let response = post_rpc(
            &client,
            &server.url,
            "/a2a",
            &rpc_body(
                json!(1),
                method,
                json!({"taskId": "t", "url": "https://example.com/hook"}),
            ),
        )
        .await;
        assert_eq!(response.status(), 200);
        let body: Value = response.json().await.unwrap();
        assert_eq!(body["error"]["code"], -32003, "method {method}");
    }
    let response = send(
        &server.url,
        Some(json!({"taskPushNotificationConfig": {"url": "https://example.com/hook"}})),
    )
    .await;
    assert_eq!(response["error"]["code"], -32003);

    let response = post_rpc(
        &client,
        &server.url,
        "/a2a",
        &rpc_body(json!(2), "GetExtendedAgentCard", json!({})),
    )
    .await;
    let body: Value = response.json().await.unwrap();
    assert_eq!(body["error"]["code"], -32004);

    let response = post_rpc(
        &client,
        &server.url,
        "/a2a",
        &rpc_body(json!(3), "message/send", json!({})),
    )
    .await;
    let body: Value = response.json().await.unwrap();
    assert_eq!(body["error"]["code"], -32601);
}

#[tokio::test]
async fn subscribe_to_task_replays_updates_until_terminal() {
    let server = start_a2a(A2aOptions {
        stream_step_ms: 60,
        ..A2aOptions::default()
    })
    .await;
    let response = send(&server.url, Some(json!({"returnImmediately": true}))).await;
    let task_id = response["result"]["task"]["id"]
        .as_str()
        .unwrap()
        .to_owned();
    let response = post_rpc(
        &client(),
        &server.url,
        "/a2a",
        &rpc_body(json!(9), "SubscribeToTask", json!({"id": task_id})),
    )
    .await;
    let events = read_sse(response).await;
    assert!(events.len() >= 3);
    assert_eq!(events[0]["result"]["task"]["id"], task_id);
    assert_eq!(
        events[0]["result"]["task"]["status"]["state"],
        "TASK_STATE_WORKING"
    );
    let states: Vec<&str> = events
        .iter()
        .filter_map(|event| event["result"]["statusUpdate"]["status"]["state"].as_str())
        .collect();
    assert!(states.contains(&"TASK_STATE_WORKING"));
    assert_eq!(states.last(), Some(&"TASK_STATE_COMPLETED"));

    // A terminal task cannot be subscribed to.
    let request = async {
        let response = post_rpc(
            &client(),
            &server.url,
            "/a2a",
            &rpc_body(json!(10), "SubscribeToTask", json!({"id": task_id})),
        )
        .await;
        response.json::<Value>().await.unwrap()
    };
    let body = tokio::time::timeout(Duration::from_secs(5), request)
        .await
        .unwrap();
    assert_eq!(body["error"]["code"], -32004);
}

#[tokio::test]
async fn version_and_streaming_capabilities_are_enforced() {
    let server = start_a2a(A2aOptions {
        no_streaming: true,
        ..A2aOptions::default()
    })
    .await;
    let response = post_rpc(
        &client(),
        &server.url,
        "/a2a",
        &rpc_body(
            json!(1),
            "SendStreamingMessage",
            json!({"message": message("x")}),
        ),
    )
    .await;
    let body: Value = response.json().await.unwrap();
    assert_eq!(body["error"]["code"], -32004);

    let card: Value = client()
        .get(format!("{}/.well-known/agent-card.json", server.url))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(card["capabilities"]["streaming"], false);

    let response = client()
        .post(format!("{}/a2a", server.url))
        .header("a2a-version", "0.3")
        .json(&rpc_body(json!(2), "ListTasks", json!({})))
        .send()
        .await
        .unwrap();
    let body: Value = response.json().await.unwrap();
    assert_eq!(body["error"]["code"], -32009);
    assert_eq!(body["error"]["data"][0]["metadata"]["requested"], "0.3");
}

#[tokio::test]
async fn content_type_validation() {
    let server = start_a2a(A2aOptions {
        stream_step_ms: 1,
        ..A2aOptions::default()
    })
    .await;
    let response = send(
        &server.url,
        Some(json!({"acceptedOutputModes": ["image/png"]})),
    )
    .await;
    assert_eq!(response["error"]["code"], -32005);
}

#[tokio::test]
async fn bearer_authentication_leaves_the_card_public() {
    let server = start_a2a(A2aOptions {
        auth: AuthOptions {
            auth: AuthMode::Bearer,
            bearer: Some("upstream-token".to_owned()),
            ..AuthOptions::default()
        },
        ..options()
    })
    .await;
    let client = client();
    assert_eq!(
        client
            .get(format!("{}/.well-known/agent-card.json", server.url))
            .send()
            .await
            .unwrap()
            .status(),
        200
    );
    let body = rpc_body(json!(1), "ListTasks", json!({}));
    let response = client
        .post(format!("{}/a2a", server.url))
        .header("a2a-version", "1.0")
        .json(&body)
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 401);
    assert!(response.headers().contains_key("www-authenticate"));
    let response = client
        .post(format!("{}/a2a", server.url))
        .header("a2a-version", "1.0")
        .header("authorization", "Bearer upstream-token")
        .json(&body)
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 200);
    let card: Value = client
        .get(format!("{}/.well-known/agent-card.json", server.url))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert!(card["securitySchemes"]["bearerAuth"].is_object());
}

#[tokio::test]
async fn hmac_authentication_good_bad_and_stale() {
    let secret = "a2a-shared-secret";
    let server = start_a2a(A2aOptions {
        auth: AuthOptions {
            auth: AuthMode::Hmac,
            hmac_secret: Some(secret.to_owned()),
            hmac_skew_ms: 5_000,
            ..AuthOptions::default()
        },
        ..options()
    })
    .await;
    let client = client();
    let body = rpc_body(json!(1), "ListTasks", json!({}));
    let raw = serde_json::to_vec(&body).unwrap();
    let path = "/a2a";
    let post = |headers: Vec<(&'static str, String)>| {
        let client = client.clone();
        let url = format!("{}{path}", server.url);
        let raw = raw.clone();
        async move {
            let mut request = client.post(url).header("a2a-version", "1.0");
            for (name, value) in headers {
                request = request.header(name, value);
            }
            request
                .header("content-type", "application/json")
                .body(raw)
                .send()
                .await
                .unwrap()
        }
    };
    let now = now_ms();
    assert_eq!(
        post(signed_headers(secret, now, "POST", path, &raw))
            .await
            .status(),
        200
    );
    assert_eq!(
        post(signed_headers(secret, now, "POST", "/other", &raw))
            .await
            .status(),
        401
    );
    assert_eq!(
        post(signed_headers("wrong-secret", now, "POST", path, &raw))
            .await
            .status(),
        401
    );
    assert_eq!(
        post(signed_headers(secret, now - 60_000, "POST", path, &raw))
            .await
            .status(),
        401
    );
    assert_eq!(post(Vec::new()).await.status(), 401);
}

#[tokio::test]
async fn control_endpoints_log_headers_and_reset() {
    let server = start_a2a(options()).await;
    let client = client();
    let _ = client
        .post(format!("{}/a2a", server.url))
        .header("a2a-version", "1.0")
        .header("authorization", "Bearer observed")
        .json(&rpc_body(json!(1), "ListTasks", json!({})))
        .send()
        .await
        .unwrap();
    let body: Value = client
        .get(format!("{}/_mock/headers?n=5", server.url))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    let entries = body["headers"].as_array().unwrap();
    assert!(!entries.is_empty());
    let last = entries.last().unwrap();
    assert_eq!(last["path"], "/a2a");
    assert_eq!(last["headers"]["authorization"], "Bearer observed");
    assert_eq!(last["headers"]["a2a-version"], "1.0");

    let tasks: Value = client
        .get(format!("{}/_mock/tasks", server.url))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert!(tasks["tasks"].is_array());
    assert_eq!(
        client
            .post(format!("{}/_mock/reset", server.url))
            .send()
            .await
            .unwrap()
            .status(),
        200
    );
    let body: Value = client
        .get(format!("{}/_mock/headers", server.url))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert!(body["headers"].as_array().unwrap().is_empty());
}

#[tokio::test]
async fn control_endpoints_refuse_non_loopback_peers() {
    let Some(ip) = support::non_loopback_ip() else {
        eprintln!("skipping: no non-loopback IPv4 address on this host");
        return;
    };
    let server = start_a2a_on("0.0.0.0:0", options()).await;
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

/// The HTTP client must see a full SSE stream close after the terminal status.
#[tokio::test]
async fn streaming_response_ends_after_the_terminal_status() {
    let server = start_a2a(options()).await;
    let response = post_rpc(
        &client(),
        &server.url,
        "/a2a",
        &rpc_body(
            json!(4),
            "SendStreamingMessage",
            json!({"message": message("end")}),
        ),
    )
    .await;
    let events = read_sse(response).await;
    let final_state = events
        .iter()
        .rev()
        .find_map(|event| event["result"]["statusUpdate"]["status"]["state"].as_str());
    assert_eq!(final_state, Some("TASK_STATE_COMPLETED"));
}

/// Guards against a regression where the agent card advertises a URL the
/// server does not serve.
#[tokio::test]
async fn advertised_jsonrpc_interface_is_served() {
    let server = start_a2a(options()).await;
    let card: Value = client()
        .get(format!("{}/.well-known/agent-card.json", server.url))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    let url = card["supportedInterfaces"][0]["url"].as_str().unwrap();
    let response = client()
        .post(url)
        .header("a2a-version", "1.0")
        .json(&rpc_body(json!(1), "ListTasks", json!({})))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 200);
    let body: Value = response.json().await.unwrap();
    assert_eq!(body["result"]["totalSize"], 0);
}
