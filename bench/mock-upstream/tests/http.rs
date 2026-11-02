use clap::Parser;
use futures_util::StreamExt;
use mock_upstream::{serve, App, Options};
use serde_json::{json, Value};
use std::time::{Duration, Instant};

struct Server {
    url: String,
    task: tokio::task::JoinHandle<std::io::Result<()>>,
}
impl Drop for Server {
    fn drop(&mut self) {
        self.task.abort();
    }
}
async fn server(extra: &[&str]) -> Server {
    let mut args = vec!["mock", "--stream-chunks", "2"];
    if !extra.contains(&"--latency-ms") {
        args.extend_from_slice(&["--latency-ms", "0"]);
    }
    args.extend_from_slice(extra);
    let app = App::new(Options::parse_from(args));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    Server {
        url: format!("http://{}", listener.local_addr().unwrap()),
        task: tokio::spawn(serve(listener, app)),
    }
}

#[tokio::test]
async fn non_stream_chat_shape() {
    let server = server(&[]).await;
    let response = reqwest::Client::new()
        .post(format!("{}/v1/chat/completions", server.url))
        .json(&json!({"model":"other-model","messages":[]}))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 200);
    let body: Value = response.json().await.unwrap();
    assert_eq!(body["object"], "chat.completion");
    assert_eq!(body["model"], "other-model");
    assert_eq!(body["choices"][0]["message"]["role"], "assistant");
    assert_eq!(body["choices"][0]["finish_reason"], "stop");
    assert!(body["usage"]["total_tokens"].as_u64().unwrap() > 0);
}

#[tokio::test]
async fn request_faults_periodic_failures_and_admin_recovery() {
    let server = server(&[]).await;
    let client = reqwest::Client::new();
    let endpoint = format!("{}/v1/chat/completions", server.url);
    let response = client
        .post(&endpoint)
        .header("x-mock-fail", "429")
        .header("x-mock-retry-after", "12")
        .json(&json!({"model":"mock"}))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 429);
    assert_eq!(response.headers()["retry-after"], "12");
    assert!(client
        .post(&endpoint)
        .header("x-mock-fail", "reset")
        .json(&json!({}))
        .send()
        .await
        .is_err());
    assert!(client
        .post(&endpoint)
        .header("x-mock-fail", "timeout")
        .timeout(Duration::from_millis(30))
        .json(&json!({}))
        .send()
        .await
        .unwrap_err()
        .is_timeout());
    for (status, expected) in [(503, 503), (0, 200)] {
        assert_eq!(
            client
                .post(format!("{}/fault?status={status}", server.url))
                .send()
                .await
                .unwrap()
                .status(),
            200
        );
        assert_eq!(
            client
                .get(format!("{}/health", server.url))
                .send()
                .await
                .unwrap()
                .status(),
            expected
        );
    }
    let server = self::server(&["--fail-every", "2", "--fail-status", "502"]).await;
    for expected in [200, 502, 200, 502] {
        assert_eq!(
            client
                .post(format!("{}/v1/chat/completions", server.url))
                .json(&json!({}))
                .send()
                .await
                .unwrap()
                .status(),
            expected
        );
    }
}

async fn events(server: &Server, path: &str, body: Value) -> Vec<String> {
    let response = reqwest::Client::new()
        .post(format!("{}{path}", server.url))
        .json(&body)
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 200);
    assert_eq!(response.headers()["content-type"], "text/event-stream");
    response
        .text()
        .await
        .unwrap()
        .split("\n\n")
        .filter_map(|line| line.strip_prefix("data: ").map(str::to_owned))
        .collect()
}

#[tokio::test]
async fn sse_event_order_done_and_optional_usage() {
    let server = server(&[]).await;
    for path in ["/v1/chat/completions", "/v1/completions"] {
        for include_usage in [false, true] {
            let events = events(&server, path, json!({"model":"mock","stream":true,"stream_options":{"include_usage":include_usage}})).await;
            assert_eq!(events.len(), if include_usage { 5 } else { 4 });
            assert_eq!(events.last().unwrap(), "[DONE]");
            for data in &events[..2] {
                let chunk: Value = serde_json::from_str(data).unwrap();
                assert_eq!(chunk["choices"][0]["finish_reason"], Value::Null);
                let content = if path.contains("chat") {
                    &chunk["choices"][0]["delta"]["content"]
                } else {
                    &chunk["choices"][0]["text"]
                };
                assert_eq!(content, "hello ");
            }
            let finish: Value = serde_json::from_str(&events[2]).unwrap();
            assert_eq!(finish["choices"][0]["finish_reason"], "stop");
            if include_usage {
                let usage: Value = serde_json::from_str(&events[3]).unwrap();
                assert_eq!(usage["choices"], json!([]));
                assert!(usage["usage"]["total_tokens"].is_number());
            }
        }
    }
}

#[tokio::test]
async fn stream_is_delivered_incrementally() {
    let server = server(&["--chunk-interval-ms", "100"]).await;
    let response = reqwest::Client::new()
        .post(format!("{}/v1/chat/completions", server.url))
        .json(&json!({"stream":true}))
        .send()
        .await
        .unwrap();
    let mut stream = response.bytes_stream();
    let first = stream.next().await.unwrap().unwrap();
    assert!(String::from_utf8_lossy(&first).contains("hello "));
    assert!(!String::from_utf8_lossy(&first).contains("[DONE]"));
    let start = Instant::now();
    while stream.next().await.is_some() {}
    assert!(start.elapsed() >= Duration::from_millis(50));
}

#[tokio::test]
async fn headers_first_sends_headers_before_prefill() {
    let server = server(&["--headers-first", "--latency-ms", "200"]).await;
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(2))
        .build()
        .unwrap();
    for path in ["/v1/chat/completions", "/v1/completions"] {
        let start = Instant::now();
        let mut response = client
            .post(format!("{}{path}", server.url))
            .json(&json!({"model":"mock","stream":true}))
            .send()
            .await
            .unwrap();
        let headers_at = start.elapsed();
        assert_eq!(response.status(), 200);
        assert_eq!(response.headers()["content-type"], "text/event-stream");
        assert!(
            headers_at < Duration::from_millis(50),
            "headers took {headers_at:?}"
        );
        let first = response.chunk().await.unwrap().unwrap();
        let first_at = start.elapsed();
        assert!(
            first_at >= Duration::from_millis(200),
            "first chunk took {first_at:?}"
        );
        assert!(String::from_utf8_lossy(&first).contains("hello "));
        let remaining = response.text().await.unwrap();
        assert!(remaining.contains("data: [DONE]"));
    }
}

#[tokio::test]
async fn headers_first_preserves_non_stream_and_default_stream_latency() {
    for (extra, stream) in [
        (vec!["--headers-first", "--latency-ms", "200"], false),
        (vec!["--latency-ms", "200"], true),
    ] {
        let server = server(&extra).await;
        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs(2))
            .build()
            .unwrap();
        let start = Instant::now();
        let response = client
            .post(format!("{}/v1/chat/completions", server.url))
            .json(&json!({"model":"mock","stream":stream}))
            .send()
            .await
            .unwrap();
        assert_eq!(response.status(), 200);
        assert!(start.elapsed() >= Duration::from_millis(200));
        response.bytes().await.unwrap();
    }
}

#[tokio::test]
async fn echo_preserves_unknown_fields_and_raw_json_body() {
    let server = server(&["--echo"]).await;
    let raw = r#"{"model":"mock", "unknown":{"nested":[1,2,3]}, "number":1.2300e+02,"extra_body":{"guided_json":{"type":"object"}}}"#;
    let response = reqwest::Client::new()
        .post(format!("{}/v1/chat/completions", server.url))
        .header("content-type", "application/json")
        .header("x-custom", "preserved")
        .body(raw)
        .send()
        .await
        .unwrap();
    let text = response.text().await.unwrap();
    assert!(text.contains(raw));
    let echo: Value = serde_json::from_str(&text).unwrap();
    assert_eq!(echo["body"], serde_json::from_str::<Value>(raw).unwrap());
    assert_eq!(echo["method"], "POST");
    assert_eq!(echo["path"], "/v1/chat/completions");
    assert_eq!(echo["headers"]["x-custom"], "preserved");
    let body = json!({"model":"mock","stream":true,"unknown":{"a":true}});
    let events = events(&server, "/v1/chat/completions", body.clone()).await;
    let echo: Value = serde_json::from_str(&events[0]).unwrap();
    assert_eq!(echo["body"], body);
    assert_eq!(events.last().unwrap(), "[DONE]");
}

#[tokio::test]
async fn invalid_utf8_in_ignored_field_returns_400_without_stopping_server() {
    for extra in [&["--echo"][..], &[][..]] {
        let server = server(extra).await;
        let client = reqwest::Client::new();
        let response = client
            .post(format!("{}/v1/chat/completions", server.url))
            .header("content-type", "application/json")
            .body(b"{\"model\":\"mock\",\"x\":\"\xff\"}".to_vec())
            .send()
            .await
            .unwrap();
        assert_eq!(response.status(), 400);
        assert!(response.json::<Value>().await.unwrap()["error"]["message"].is_string());
        let valid = client
            .post(format!("{}/v1/chat/completions", server.url))
            .json(&json!({"model":"mock"}))
            .send()
            .await
            .unwrap();
        assert_eq!(valid.status(), 200);
    }
}

#[tokio::test]
async fn sse_bench_reports_successful_and_attempted_rps() {
    let server = server(&[]).await;
    for (path, expected_errors) in [("/v1/chat/completions", 0), ("/missing/completions", 3)] {
        let url = format!("{}{path}", server.url);
        let output = tokio::task::spawn_blocking(move || {
            std::process::Command::new(env!("CARGO_BIN_EXE_sse-bench"))
                .args(["--url", &url, "--concurrency", "2", "--requests", "3"])
                .output()
                .unwrap()
        })
        .await
        .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        let report: Value = serde_json::from_slice(&output.stdout).unwrap();
        assert_eq!(report["requests"], 3);
        assert_eq!(report["errors"], expected_errors);
        let rps = report["rps"].as_f64().unwrap();
        let ok_rps = report["ok_rps"].as_f64().unwrap();
        assert!(rps > 0.0);
        if expected_errors == 0 {
            assert_eq!(ok_rps, rps);
        } else {
            assert_eq!(ok_rps, 0.0);
        }
        assert!(report["ttft_ms"]["p99"].is_number());
        assert!(report["total_ms"]["p99"].is_number());
    }
}

#[tokio::test]
async fn embeddings_models_health_errors_and_completions() {
    let server = server(&[]).await;
    let client = reqwest::Client::new();
    for (input, count) in [(json!("hi"), 1), (json!(["hi", "there"]), 2)] {
        let body: Value = client
            .post(format!("{}/v1/embeddings", server.url))
            .json(&json!({"model":"mock","input":input}))
            .send()
            .await
            .unwrap()
            .json()
            .await
            .unwrap();
        assert_eq!(body["data"].as_array().unwrap().len(), count);
        assert_eq!(body["data"][0]["embedding"].as_array().unwrap().len(), 16);
    }
    let models: Value = client
        .get(format!("{}/v1/models", server.url))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(models["data"][0]["id"], "mock");
    assert_eq!(
        client
            .get(format!("{}/health", server.url))
            .send()
            .await
            .unwrap()
            .status(),
        200
    );
    let missing = client
        .get(format!("{}/missing", server.url))
        .send()
        .await
        .unwrap();
    assert_eq!(missing.status(), 404);
    assert!(missing.json::<Value>().await.unwrap()["error"]["message"].is_string());
    let invalid = client
        .post(format!("{}/v1/chat/completions", server.url))
        .body("not-json")
        .send()
        .await
        .unwrap();
    assert_eq!(invalid.status(), 400);
    let completion: Value = client
        .post(format!("{}/v1/completions", server.url))
        .json(&json!({"model":"mock","prompt":"hi"}))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(completion["object"], "text_completion");
    assert!(completion["choices"][0]["text"].is_string());
}

#[tokio::test]
async fn vllm_metrics_follow_load_control() {
    let server = server(&["--served-model-name", "llama \"x\"", "--waiting", "2"]).await;
    let client = reqwest::Client::new();
    let metrics = || async {
        let response = client
            .get(format!("{}/metrics", server.url))
            .send()
            .await
            .unwrap();
        (response.status().as_u16(), response.text().await.unwrap())
    };
    let (status, text) = metrics().await;
    assert_eq!(status, 200);
    assert!(text
        .contains("vllm:num_requests_waiting{engine=\"0\",model_name=\"llama \\\"x\\\"\"} 2.0\n"));
    assert!(text
        .contains("vllm:kv_cache_usage_perc{engine=\"0\",model_name=\"llama \\\"x\\\"\"} 0.0\n"));
    let load = |query: &'static str| {
        let client = client.clone();
        let url = format!("{}/load?{query}", server.url);
        async move { client.post(url).send().await.unwrap() }
    };
    let response = load("waiting=17&kv=0.75").await;
    assert_eq!(response.status(), 200);
    let body: Value = response.json().await.unwrap();
    assert_eq!(body["waiting"], 17.0);
    assert_eq!(body["scrapes"], 1);
    let (_, text) = metrics().await;
    assert!(
        text.contains("} 17.0\n") && text.contains("} 0.75\n"),
        "{text}"
    );
    for bad in ["kv=2", "waiting=-1", "metrics_status=200", "bogus=1"] {
        assert_eq!(load(bad).await.status(), 400, "{bad}");
    }
    assert_eq!(load("metrics_status=503").await.status(), 200);
    assert_eq!(metrics().await.0, 503);
    // Inference is unaffected by the metrics fault.
    let response = client
        .post(format!("{}/v1/chat/completions", server.url))
        .json(&json!({"model":"mock"}))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 200);
    load("metrics_status=0").await;
    assert_eq!(metrics().await.0, 200);
}

#[tokio::test]
async fn large_embedding_body_has_model_after_data() {
    const SIZE: usize = 200 * 1024;
    let server = server(&["--large-embedding-bytes", "204800"]).await;
    let client = reqwest::Client::new();
    let post = |model: &'static str| {
        client
            .post(format!("{}/v1/embeddings", server.url))
            .json(&json!({"model": model, "input": "x"}))
            .send()
    };
    let text = post("mock").await.unwrap().text().await.unwrap();
    assert!(text.len() >= SIZE, "{}", text.len());
    assert!(text.find("\"data\"").unwrap() < text.find("\"model\"").unwrap());
    let body: Value = serde_json::from_str(&text).unwrap();
    assert_eq!(body["model"], "mock");
    assert_eq!(body["usage"]["prompt_tokens"], 8);
    assert_eq!(body["data"][0]["embedding"].as_array().unwrap().len(), 1536);
    // Any other model keeps the small response.
    let small: Value = post("other").await.unwrap().json().await.unwrap();
    assert_eq!(small["model"], "other");
    assert_eq!(small["data"][0]["embedding"].as_array().unwrap().len(), 16);
}
