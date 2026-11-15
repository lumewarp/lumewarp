//! LLM upstreams with a WASM `upstream-auth` plugin: the gateway process loads
//! the built components, so these tests exercise the real host adapter and the
//! request path. Skipped when `plugins/build.sh` has not run.
mod common;

use common::{bounded, bytes, plugin_test_dir, Behavior, Gateway, Mock};
use serde_json::Value;

fn auth_dir() -> Option<String> {
    let dir = plugin_test_dir("auth")?;
    Some(dir.to_str().expect("utf-8 path").to_owned())
}

fn skip() {
    eprintln!("SKIP: build components with bash gateway/plugins/build.sh --tests");
}

async fn json_error_code(response: hyper::Response<hyper::body::Incoming>) -> String {
    let body = bytes(response).await;
    let body: Value = serde_json::from_slice(&body).expect("JSON error");
    body["error"]["code"].as_str().expect("code").to_owned()
}

fn config(upstreams: &str, models: &str, routers: &str) -> String {
    format!("upstreams:\n{upstreams}\nmodels:\n{models}\nrouters:\n{routers}\n")
}

#[tokio::test]
async fn plugin_headers_reach_the_upstream_and_a_cache_hit_skips_wasm() {
    let Some(auth) = auth_dir() else {
        skip();
        return;
    };
    let mut mock = Mock::start(Behavior::Json).await;
    let mock_addr = mock.addr;
    let yaml = config(
        &format!(
            "  - {{name: u, kind: openai_compatible, base_url: 'http://{mock_addr}/prefix/v1', timeout_ms: 2000, auth: {{plugin: auth_fixture, params: {{mode: fixed, headers: ['x-auth: llm'], cache_ms: 60000}}}}}}\n"
        ),
        "  - {type: llm, name: m, upstream: u, upstream_model: actual}\n",
        "  - {name: public, members: [{model: m}]}\n",
    );
    let gateway =
        Gateway::start_from_yaml(yaml, &[("LUMEWARP_AUTH_PLUGIN_DIR", auth.as_str())]).await;
    let headers = [
        ("authorization", "Bearer client-secret"),
        ("x-lumewarp-key", "lw-client"),
        ("cookie", "session=1"),
        ("proxy-authorization", "Basic cHJveHk="),
    ];
    let body = br#"{"model":"public","messages":[]}"#;
    let response = gateway
        .request("POST", "/v1/chat/completions", body.as_slice(), &headers)
        .await;
    assert_eq!(response.status(), 200);
    bytes(response).await;
    let first = mock.next().await;
    assert_eq!(first.headers["x-auth"], "llm");
    assert_eq!(
        first.headers["x-body-len"], "0",
        "needs_body is false, so the plugin gets no LLM body"
    );
    assert!(
        !first.headers.contains_key("authorization"),
        "the client's Authorization must never reach the upstream"
    );
    assert!(!first.headers.contains_key("x-lumewarp-key"));
    assert!(!first.headers.contains_key("cookie"));
    assert!(!first.headers.contains_key("proxy-authorization"));
    assert_eq!(first.headers["x-calls"], "1");

    // The plugin promised 60 s of cache: the second call reuses its headers and
    // x-calls stays 1 (a second WASM call would make it 2).
    let response = gateway
        .request("POST", "/v1/chat/completions", body.as_slice(), &headers)
        .await;
    assert_eq!(response.status(), 200);
    bytes(response).await;
    let second = mock.next().await;
    assert_eq!(second.headers["x-calls"], "1");
    let metrics = gateway.metrics().await;
    assert_eq!(
        metrics.sum(
            "lumewarp_upstream_auth_total",
            &[("upstream", "u"), ("result", "cached")]
        ),
        1.0
    );
    assert_eq!(
        metrics.sum(
            "lumewarp_plugin_calls_total",
            &[
                ("kind", "upstream-auth"),
                ("plugin", "auth_fixture"),
                ("result", "ok")
            ]
        ),
        1.0
    );
}

#[tokio::test]
async fn a_plugin_failure_is_a_502_without_reaching_the_upstream() {
    let Some(auth) = auth_dir() else {
        skip();
        return;
    };
    let mut mock = Mock::start(Behavior::Json).await;
    let mock_addr = mock.addr;
    let yaml = config(
        &format!(
            "  - {{name: u, kind: openai_compatible, base_url: 'http://{mock_addr}/prefix/v1', timeout_ms: 2000, auth: {{plugin: auth_fixture, params: {{mode: error}}}}}}\n"
        ),
        "  - {type: llm, name: m, upstream: u, upstream_model: actual}\n",
        "  - {name: public, members: [{model: m}]}\n",
    );
    let gateway =
        Gateway::start_from_yaml(yaml, &[("LUMEWARP_AUTH_PLUGIN_DIR", auth.as_str())]).await;
    let response = gateway
        .request(
            "POST",
            "/v1/chat/completions",
            br#"{"model":"public"}"#.as_slice(),
            &[],
        )
        .await;
    assert_eq!(response.status(), 502);
    assert_eq!(json_error_code(response).await, "upstream_auth_failed");
    assert!(
        mock.records.try_recv().is_err(),
        "a failed auth call must not send a request upstream"
    );
    let metrics = gateway.metrics().await;
    assert_eq!(
        metrics.sum(
            "lumewarp_upstream_auth_total",
            &[("upstream", "u"), ("result", "plugin")]
        ),
        1.0
    );
    // Recorded as an upstream error, not dropped: the request log shows it.
    assert_eq!(
        metrics.sum("lumewarp_requests_total", &[("status", "502")]),
        1.0
    );
}

#[tokio::test]
async fn a_plugin_failure_falls_back_to_another_member() {
    let Some(auth) = auth_dir() else {
        skip();
        return;
    };
    let mut broken = Mock::start(Behavior::Json).await;
    let mut healthy = Mock::start(Behavior::Json).await;
    let (broken_addr, healthy_addr) = (broken.addr, healthy.addr);
    let yaml = config(
        &format!(
            "  - {{name: a, kind: openai_compatible, base_url: 'http://{broken_addr}/prefix/v1', timeout_ms: 2000, auth: {{plugin: auth_fixture, params: {{mode: error}}}}}}\n  - {{name: b, kind: openai_compatible, base_url: 'http://{healthy_addr}/prefix/v1', timeout_ms: 2000}}\n"
        ),
        "  - {type: llm, name: a, upstream: a, upstream_model: actual-a}\n  - {type: llm, name: b, upstream: b, upstream_model: actual-b}\n",
        "  - {name: public, members: [{model: a}], retry: {max_attempts: 2}, fallbacks: [b]}\n",
    );
    let gateway =
        Gateway::start_from_yaml(yaml, &[("LUMEWARP_AUTH_PLUGIN_DIR", auth.as_str())]).await;
    let response = gateway
        .request(
            "POST",
            "/v1/chat/completions",
            br#"{"model":"public"}"#.as_slice(),
            &[],
        )
        .await;
    assert_eq!(
        response.status(),
        200,
        "a broken plugin on one member must not fail the request"
    );
    bytes(response).await;
    assert!(
        broken.records.try_recv().is_err(),
        "the member whose plugin failed is never contacted"
    );
    let served = healthy.next().await;
    assert_eq!(served.path, "/prefix/v1/chat/completions");
}

#[tokio::test]
async fn a_fallback_authorizes_its_own_upstream() {
    let Some(auth) = auth_dir() else {
        skip();
        return;
    };
    let mut slow = Mock::start(Behavior::Hang).await;
    let mut healthy = Mock::start(Behavior::Json).await;
    let (slow_addr, healthy_addr) = (slow.addr, healthy.addr);
    let yaml = config(
        &format!(
            "  - {{name: a, kind: openai_compatible, base_url: 'http://{slow_addr}/prefix/v1', timeout_ms: 100, auth: {{plugin: auth_fixture, params: {{mode: fixed, headers: ['x-auth: a'], cache_ms: 60000}}}}}}\n  - {{name: b, kind: openai_compatible, base_url: 'http://{healthy_addr}/prefix/v1', timeout_ms: 2000, auth: {{plugin: auth_fixture, params: {{mode: fixed, headers: ['x-auth: b'], cache_ms: 60000}}}}}}\n"
        ),
        "  - {type: llm, name: a, upstream: a, upstream_model: actual-a}\n  - {type: llm, name: b, upstream: b, upstream_model: actual-b}\n",
        "  - {name: public, members: [{model: a}], retry: {max_attempts: 2}, fallbacks: [b]}\n",
    );
    let gateway =
        Gateway::start_from_yaml(yaml, &[("LUMEWARP_AUTH_PLUGIN_DIR", auth.as_str())]).await;
    let response = gateway
        .request(
            "POST",
            "/v1/chat/completions",
            br#"{"model":"public"}"#.as_slice(),
            &[],
        )
        .await;
    assert_eq!(response.status(), 200);
    bytes(response).await;
    let first = slow.next().await;
    assert_eq!(first.headers["x-auth"], "a");
    let fallback = healthy.next().await;
    assert_eq!(
        fallback.headers["x-auth"], "b",
        "the fallback upstream's own plugin must authorize it"
    );
}

#[tokio::test]
async fn health_checks_and_vllm_scrapes_send_plugin_headers() {
    let Some(auth) = auth_dir() else {
        skip();
        return;
    };
    let mut mock = Mock::start(Behavior::Json).await;
    let mock_addr = mock.addr;
    let yaml = config(
        &format!(
            "  - {{name: u, kind: vllm, base_url: 'http://{mock_addr}/prefix/v1', timeout_ms: 2000, metrics_url: 'http://{mock_addr}/metrics', health_check: {{interval_ms: 1000, timeout_ms: 500}}, auth: {{plugin: auth_fixture, params: {{mode: fixed, headers: ['x-probe: yes'], cache_ms: 60000}}}}}}\n"
        ),
        "  - {type: llm, name: m, upstream: u, upstream_model: actual}\n",
        "  - {name: public, members: [{model: m}], strategy: vllm_load}\n",
    );
    let _gateway =
        Gateway::start_from_yaml(yaml, &[("LUMEWARP_AUTH_PLUGIN_DIR", auth.as_str())]).await;
    let mut paths = Vec::new();
    for _ in 0..2 {
        let request = bounded(mock.next()).await;
        assert_eq!(request.headers["x-probe"], "yes");
        paths.push(request.path);
    }
    paths.sort();
    assert_eq!(paths, ["/metrics", "/prefix/v1/models"]);
}
