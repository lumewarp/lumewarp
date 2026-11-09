//! Real gateway process against the benchmark mock and event-controlled SSE.
mod common;

use bytes::Bytes;
use clap::Parser;
use common::{bounded, bytes, client, Behavior, Mock, Prometheus, TestClient};
use http_body_util::{BodyExt, Full};
use hyper::{body::Incoming, Request, Response};
use mock_upstream::{App, Options};
use std::{
    io::{BufRead, BufReader},
    net::SocketAddr,
    process::{Child, Command, Stdio},
    time::Duration,
};
use tokio::{net::TcpListener, sync::mpsc, task::JoinHandle};

struct Backend {
    addr: SocketAddr,
    task: JoinHandle<std::io::Result<()>>,
}
impl Backend {
    async fn start(extra: &[&str]) -> Self {
        let mut args = vec!["mock", "--stream-chunks", "1", "--chunk-interval-ms", "0"];
        if !extra.contains(&"--latency-ms") {
            args.extend_from_slice(&["--latency-ms", "0"]);
        }
        args.extend_from_slice(extra);
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let app = App::new(Options::parse_from(args));
        Self {
            addr,
            task: tokio::spawn(mock_upstream::serve(listener, app)),
        }
    }
    async fn load(&self, query: &str) {
        let response = bounded(
            client().request(
                Request::builder()
                    .method("POST")
                    .uri(format!("http://{}/load?{query}", self.addr))
                    .body(Full::new(Bytes::new()))
                    .unwrap(),
            ),
        )
        .await
        .unwrap();
        assert_eq!(response.status(), 200);
        bytes(response).await;
    }
    async fn fault(&self, status: u16) {
        let response = bounded(
            client().request(
                Request::builder()
                    .method("POST")
                    .uri(format!("http://{}/fault?status={status}", self.addr))
                    .body(Full::new(Bytes::new()))
                    .unwrap(),
            ),
        )
        .await
        .unwrap();
        assert_eq!(response.status(), 200);
        bytes(response).await;
    }
}
impl Drop for Backend {
    fn drop(&mut self) {
        self.task.abort();
    }
}

struct Gateway {
    addr: SocketAddr,
    admin: SocketAddr,
    client: TestClient,
    child: Child,
    events: mpsc::UnboundedReceiver<String>,
    history: Vec<String>,
    _dir: tempfile::TempDir,
}
impl Gateway {
    async fn start(config: String) -> Self {
        let addr: SocketAddr = "127.0.0.1:0".parse().unwrap();
        let admin_addr: SocketAddr = "127.0.0.2:0".parse().unwrap();
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("config.yaml");
        std::fs::write(
            &path,
            format!("server:\n  listen: '{addr}'\n  admin_listen: '{admin_addr}'\n{config}"),
        )
        .unwrap();
        let mut child = Command::new(env!("CARGO_BIN_EXE_lumewarp"))
            .arg("--config")
            .arg(path)
            .env("RUST_LOG", "info")
            .env("NO_COLOR", "1")
            .env_remove("OTEL_EXPORTER_OTLP_ENDPOINT")
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .unwrap();
        let stdout = child.stdout.take().unwrap();
        let (tx, events) = mpsc::unbounded_channel();
        std::thread::spawn(move || {
            for line in BufReader::new(stdout).lines() {
                if let Ok(line) = line {
                    let _ = tx.send(line);
                } else {
                    break;
                }
            }
        });
        let mut gateway = Self {
            addr,
            admin: admin_addr,
            client: client(),
            child,
            events,
            history: Vec::new(),
            _dir: dir,
        };
        gateway.log("lumewarp gateway listening").await;
        let line = gateway
            .history
            .iter()
            .find(|line| line.contains("lumewarp gateway listening"))
            .unwrap();
        gateway.addr = line
            .split_whitespace()
            .find_map(|part| part.strip_prefix("listen="))
            .unwrap()
            .parse()
            .unwrap();
        gateway.admin = line
            .split_whitespace()
            .find_map(|part| part.strip_prefix("admin_listen="))
            .unwrap()
            .parse()
            .unwrap();
        gateway
    }
    async fn log(&mut self, marker: &str) {
        if self.history.iter().any(|line| line.contains(marker)) {
            return;
        }
        bounded(async {
            loop {
                let line = self
                    .events
                    .recv()
                    .await
                    .expect("gateway exited before expected event");
                let found = line.contains(marker);
                self.history.push(line);
                if found {
                    break;
                }
            }
        })
        .await;
    }
    async fn post(&self, stream: bool, headers: &[(&str, &str)]) -> Response<Incoming> {
        self.post_model("public", stream, headers).await
    }
    async fn post_model(
        &self,
        model: &str,
        stream: bool,
        headers: &[(&str, &str)],
    ) -> Response<Incoming> {
        let mut request = Request::builder()
            .method("POST")
            .uri(format!("http://{}/v1/chat/completions", self.addr));
        for &(name, value) in headers {
            request = request.header(name, value);
        }
        bounded(self.client.request(request.body(Full::new(Bytes::from(
            format!(r#"{{"model":"{model}","stream":{stream},"stream_options":{{"include_usage":true}}}}"#),
        ))).unwrap())).await.unwrap()
    }
    async fn metrics(&self) -> Prometheus {
        let response = bounded(
            self.client.request(
                Request::builder()
                    .uri(format!("http://{}/metrics", self.admin))
                    .body(Full::new(Bytes::new()))
                    .unwrap(),
            ),
        )
        .await
        .unwrap();
        Prometheus::parse(std::str::from_utf8(&bytes(response).await).unwrap())
    }
}
impl Drop for Gateway {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn config(a: SocketAddr, b: SocketAddr, upstream_options: &str, router: &str) -> String {
    format!(
        r#"upstreams:
  - {{name: a, kind: vllm, base_url: 'http://{a}/v1', timeout_ms: 100, {upstream_options}}}
  - {{name: b, kind: vllm, base_url: 'http://{b}/v1', timeout_ms: 100}}
models:
  - {{type: llm, name: a, upstream: a, upstream_model: actual-a}}
  - {{type: llm, name: b, upstream: b, upstream_model: actual-b}}
routers:
{router}
"#
    )
}
const FALLBACK: &str =
    "  - {name: public, members: [{model: a}], retry: {max_attempts: 2}, fallbacks: [b]}";
fn attempts(metrics: &Prometheus, outcome: &str) -> f64 {
    metrics.sum(
        "lumewarp_upstream_attempts_total",
        &[("requested", "public"), ("outcome", outcome)],
    )
}

#[tokio::test]
async fn retry_chooses_another_model_and_meters_once() {
    let backend = Backend::start(&["--fail-every", "2"]).await;
    // The next inference fails, whichever model weighted routing chooses.
    let warm = client()
        .request(
            Request::builder()
                .method("POST")
                .uri(format!("http://{}/v1/chat/completions", backend.addr))
                .body(Full::new(Bytes::from_static(br#"{"model":"warm"}"#)))
                .unwrap(),
        )
        .await
        .unwrap();
    bytes(warm).await;
    let gateway = Gateway::start(config(
        backend.addr,
        backend.addr,
        "",
        "  - {name: public, members: [{model: a}, {model: b}], retry: {max_attempts: 2}}",
    ))
    .await;
    let response = gateway.post(false, &[]).await;
    assert_eq!(response.status(), 200);
    let body: serde_json::Value = serde_json::from_slice(&bytes(response).await).unwrap();
    assert_eq!(body["model"], "public");
    let metrics = gateway.metrics().await;
    assert_eq!(attempts(&metrics, "retryable_status"), 1.);
    assert_eq!(attempts(&metrics, "success"), 1.);
    let final_model = if metrics.sum("lumewarp_requests_total", &[("model", "a")]) == 1. {
        "a"
    } else {
        "b"
    };
    let first = if final_model == "a" { "b" } else { "a" };
    assert_eq!(
        metrics.sum(
            "lumewarp_upstream_attempts_total",
            &[("model", first), ("outcome", "retryable_status")]
        ),
        1.
    );
    assert_eq!(
        metrics.sum(
            "lumewarp_requests_total",
            &[("model", final_model), ("status", "200")]
        ),
        1.
    );
    assert_eq!(metrics.sum("lumewarp_usage_capture_total", &[]), 1.);
    assert_eq!(metrics.sum("lumewarp_in_flight_requests", &[]), 0.);
}

#[tokio::test]
async fn fallback_restores_the_called_name_and_counts_the_real_model() {
    let bad = Backend::start(&["--fail-every", "1"]).await;
    let good = Backend::start(&[]).await;
    let gateway = Gateway::start(config(bad.addr, good.addr, "", FALLBACK)).await;
    let response = gateway.post(false, &[]).await;
    assert_eq!(response.status(), 200);
    let body: serde_json::Value = serde_json::from_slice(&bytes(response).await).unwrap();
    assert_eq!(body["model"], "public");
    let metrics = gateway.metrics().await;
    assert_eq!(
        metrics.sum(
            "lumewarp_fallbacks_total",
            &[("router", "public"), ("model", "b")]
        ),
        1.
    );
    assert_eq!(
        metrics.sum("lumewarp_requests_total", &[("model", "b")]),
        1.
    );
}

#[tokio::test]
async fn streams_retry_transport_errors_before_headers_but_never_status_headers() {
    let reset = Backend::start(&["--fail-every", "1", "--fail-mode", "reset"]).await;
    let good = Backend::start(&[]).await;
    let gateway = Gateway::start(config(reset.addr, good.addr, "", FALLBACK)).await;
    let response = gateway.post(true, &[]).await;
    assert_eq!(response.status(), 200);
    let body = bytes(response).await;
    let text = std::str::from_utf8(&body).unwrap();
    assert!(text.contains("\"model\":\"public\""));
    assert!(text.contains("[DONE]"));
    assert_eq!(attempts(&gateway.metrics().await, "connect_error"), 1.);

    let bad = Backend::start(&["--fail-every", "1"]).await;
    let gateway = Gateway::start(config(bad.addr, good.addr, "", FALLBACK)).await;
    let response = gateway.post(true, &[]).await;
    assert_eq!(response.status(), 503);
    bytes(response).await;
    assert_eq!(attempts(&gateway.metrics().await, "success"), 0.);
}

#[tokio::test]
async fn stream_is_not_retried_after_headers_or_first_byte() {
    let good = Backend::start(&[]).await;
    for behavior in [Behavior::HeadersFirstStream, Behavior::BrokenStream] {
        let mut upstream = Mock::start(behavior).await;
        let gateway = Gateway::start(config(upstream.addr, good.addr, "", FALLBACK)).await;
        let response = gateway.post(true, &[]).await;
        assert_eq!(response.status(), 200);
        let control = upstream.next().await.stream.unwrap();
        let mut body = response.into_body();
        if matches!(behavior, Behavior::BrokenStream) {
            let first = bounded(body.frame())
                .await
                .unwrap()
                .unwrap()
                .into_data()
                .unwrap();
            assert!(!first.is_empty());
            drop(control.frames);
            assert!(bounded(body.collect()).await.is_err());
        } else {
            // Headers are already visible although the first event has not
            // been supplied. Closing this stream must not trigger a retry.
            drop(control.frames);
            bounded(body.collect()).await.unwrap();
        }
        assert_eq!(attempts(&gateway.metrics().await, "success"), 1.);
        assert_eq!(
            gateway.metrics().await.sum("lumewarp_fallbacks_total", &[]),
            0.
        );
    }
}

#[tokio::test]
async fn retry_after_cooldown_and_total_timeout_stop_retries() {
    let bad = Backend::start(&["--fail-every", "1"]).await;
    let routers =
        "  - {name: public, members: [{model: a}], retry: {max_attempts: 3, total_timeout_ms: 50}}";
    let gateway = Gateway::start(config(bad.addr, bad.addr, "", routers)).await;
    let response = gateway.post(false, &[("x-mock-retry-after", "60")]).await;
    assert_eq!(response.status(), 503);
    bytes(response).await;
    assert_eq!(attempts(&gateway.metrics().await, "retryable_status"), 1.);

    let hung = Mock::start(Behavior::Hang).await;
    let good = Backend::start(&[]).await;
    let routers = "  - {name: public, members: [{model: a}], retry: {max_attempts: 2, total_timeout_ms: 20}, fallbacks: [b]}";
    let gateway = Gateway::start(config(hung.addr, good.addr, "", routers)).await;
    let response = gateway.post(false, &[]).await;
    assert_eq!(response.status(), 504);
    bytes(response).await;
    assert_eq!(attempts(&gateway.metrics().await, "timeout"), 1.);
    assert_eq!(attempts(&gateway.metrics().await, "success"), 0.);
}

#[tokio::test]
async fn exhausted_global_budget_limits_attempts() {
    let bad = Backend::start(&["--fail-every", "1"]).await;
    let routers = "  - {name: public, members: [{model: a}], retry: {max_attempts: 5}}";
    let gateway = Gateway::start(config(bad.addr, bad.addr, "", routers)).await;
    let response = gateway.post(false, &[]).await;
    assert_eq!(response.status(), 503);
    bytes(response).await;
    let metrics = gateway.metrics().await;
    assert_eq!(attempts(&metrics, "retryable_status"), 4.);
    assert_eq!(
        metrics.sum(
            "lumewarp_retries_budget_exhausted_total",
            &[("router", "public")]
        ),
        1.
    );
}

#[tokio::test]
async fn returns_last_upstream_response_when_later_connection_fails() {
    let bad = Backend::start(&["--fail-every", "1"]).await;
    let reset = Backend::start(&["--fail-every", "1", "--fail-mode", "reset"]).await;
    let gateway = Gateway::start(config(bad.addr, reset.addr, "", FALLBACK)).await;
    let response = gateway.post(false, &[]).await;
    assert_eq!(response.status(), 503);
    assert!(std::str::from_utf8(&bytes(response).await)
        .unwrap()
        .contains("Injected upstream failure"));
    let metrics = gateway.metrics().await;
    assert_eq!(attempts(&metrics, "retryable_status"), 1.);
    assert_eq!(attempts(&metrics, "connect_error"), 1.);
    // The 503 came from a: the request sample belongs to a, not to the
    // later attempt on b that failed to connect.
    assert_eq!(
        metrics.sum(
            "lumewarp_requests_total",
            &[("model", "a"), ("status", "503")]
        ),
        1.
    );
    assert_eq!(
        metrics.sum("lumewarp_requests_total", &[("model", "b")]),
        0.
    );
    assert_eq!(metrics.sum("lumewarp_usage_capture_total", &[]), 1.);
    assert_eq!(metrics.sum("lumewarp_in_flight_requests", &[]), 0.);
}

#[tokio::test]
async fn kept_response_is_accounted_to_its_model_after_a_later_timeout() {
    // a answers 503 at once; the retry on b hangs until total_timeout.
    let bad = Backend::start(&["--fail-every", "1"]).await;
    let hung = Mock::start(Behavior::Hang).await;
    let routers = "  - {name: public, members: [{model: a}], retry: {max_attempts: 2, total_timeout_ms: 80}, fallbacks: [b]}";
    let gateway = Gateway::start(config(bad.addr, hung.addr, "", routers)).await;
    let response = gateway.post(false, &[]).await;
    assert_eq!(response.status(), 503);
    bytes(response).await;
    let metrics = gateway.metrics().await;
    assert_eq!(attempts(&metrics, "retryable_status"), 1.);
    assert_eq!(attempts(&metrics, "timeout"), 1.);
    assert_eq!(
        metrics.sum(
            "lumewarp_requests_total",
            &[("model", "a"), ("status", "503")]
        ),
        1.
    );
    assert_eq!(
        metrics.sum("lumewarp_requests_total", &[("model", "b")]),
        0.
    );
    assert_eq!(metrics.sum("lumewarp_usage_capture_total", &[]), 1.);
    // Both reservations were released: the kept one at the body's end, the
    // timed-out one when the kept response was restored.
    assert_eq!(metrics.sum("lumewarp_in_flight_requests", &[]), 0.);
}

#[tokio::test]
async fn routers_without_retry_do_not_fund_the_budget() {
    let bad = Backend::start(&["--fail-every", "1"]).await;
    let routers =
        "  - {name: public, members: [{model: a}], retry: {max_attempts: 5}}\n  - {name: plain, members: [{model: a}]}";
    let gateway = Gateway::start(config(bad.addr, bad.addr, "", routers)).await;
    // Spend the initial three-retry burst.
    bytes(gateway.post(false, &[]).await).await;
    assert_eq!(attempts(&gateway.metrics().await, "retryable_status"), 4.);
    // Ten requests on a router without a retry policy: 20% each would buy
    // two retries if they funded the budget.
    for _ in 0..10 {
        let response = gateway.post_model("plain", false, &[]).await;
        assert_eq!(response.status(), 503);
        bytes(response).await;
    }
    bytes(gateway.post(false, &[]).await).await;
    let metrics = gateway.metrics().await;
    assert_eq!(
        attempts(&metrics, "retryable_status"),
        5.,
        "no retry funded"
    );
    assert_eq!(
        metrics.sum(
            "lumewarp_retries_budget_exhausted_total",
            &[("router", "public")]
        ),
        2.
    );
}

#[tokio::test]
async fn total_timeout_cut_is_not_a_model_failure() {
    let hung = Mock::start(Behavior::Hang).await;
    let breaker = "circuit_breaker: {failure_threshold: 1, open_ms: 30000}";
    // The router's 20 ms total_timeout cuts the attempt before the
    // model's own 100 ms timeout: no breaker failure, no latency penalty.
    let routers = "  - {name: public, members: [{model: a}], strategy: lowest_latency, retry: {max_attempts: 2, total_timeout_ms: 20}}";
    let gateway = Gateway::start(config(hung.addr, hung.addr, breaker, routers)).await;
    let response = gateway.post(false, &[]).await;
    assert_eq!(response.status(), 504);
    bytes(response).await;
    let metrics = gateway.metrics().await;
    assert_eq!(attempts(&metrics, "timeout"), 1.);
    assert_eq!(
        metrics.value("lumewarp_upstream_circuit_state", &[("model", "a")]),
        0.
    );
    assert_eq!(
        metrics.sum("lumewarp_upstream_latency_ewma_seconds", &[("model", "a")]),
        0.
    );
    // The model's own timeout is a failure and opens the breaker.
    let routers = "  - {name: public, members: [{model: a}], strategy: lowest_latency, retry: {max_attempts: 2, total_timeout_ms: 1000}}";
    let gateway = Gateway::start(config(hung.addr, hung.addr, breaker, routers)).await;
    let response = gateway.post(false, &[]).await;
    assert_eq!(response.status(), 504);
    bytes(response).await;
    let metrics = gateway.metrics().await;
    assert_eq!(
        metrics.value("lumewarp_upstream_circuit_state", &[("model", "a")]),
        2.
    );
    assert!(
        metrics.sum("lumewarp_upstream_latency_ewma_seconds", &[("model", "a")]) >= 1.,
        "timeout penalty"
    );
}

#[tokio::test]
async fn breaker_skips_open_primary_and_half_open_recovers() {
    let bad = Backend::start(&[]).await;
    let good = Backend::start(&[]).await;
    bad.fault(503).await;
    let gateway = Gateway::start(config(
        bad.addr,
        good.addr,
        "circuit_breaker: {failure_threshold: 1, open_ms: 1000}",
        FALLBACK,
    ))
    .await;
    bytes(gateway.post(false, &[]).await).await;
    bytes(gateway.post(false, &[]).await).await;
    let metrics = gateway.metrics().await;
    assert_eq!(
        metrics.value("lumewarp_upstream_circuit_state", &[("model", "a")]),
        2.
    );
    assert_eq!(attempts(&metrics, "retryable_status"), 1.);
    bad.fault(0).await;
    // The open interval itself is the behavior under test. Await its deadline,
    // rather than polling for readiness or relying on unrelated work taking time.
    tokio::time::sleep_until(tokio::time::Instant::now() + Duration::from_millis(1000)).await;
    let response = gateway.post(false, &[]).await;
    let body: serde_json::Value = serde_json::from_slice(&bytes(response).await).unwrap();
    assert_eq!(body["model"], "public");
    assert_eq!(
        gateway.metrics().await.sum(
            "lumewarp_requests_total",
            &[("model", "a"), ("status", "200")]
        ),
        1.,
        "a served it"
    );
    assert_eq!(
        gateway
            .metrics()
            .await
            .value("lumewarp_upstream_circuit_state", &[("model", "a")]),
        0.
    );
}

#[tokio::test]
async fn active_health_probes_skip_down_upstreams_and_recover() {
    let a = Backend::start(&[]).await;
    let b = Backend::start(&[]).await;
    a.fault(503).await;
    let mut gateway = Gateway::start(config(a.addr, b.addr, "health_check: {interval_ms: 1000, timeout_ms: 100, unhealthy_threshold: 1, healthy_threshold: 1}", FALLBACK)).await;
    gateway.log("healthy=false").await;
    bytes(gateway.post(false, &[]).await).await;
    let metrics = gateway.metrics().await;
    assert_eq!(
        metrics.value("lumewarp_upstream_healthy", &[("upstream", "a")]),
        0.
    );
    assert_eq!(attempts(&metrics, "retryable_status"), 0.);
    assert_eq!(
        metrics.sum("lumewarp_requests_total", &[("model", "b")]),
        1.
    );
    a.fault(0).await;
    gateway.log("healthy=true").await;
    let response = gateway.post(false, &[]).await;
    let body: serde_json::Value = serde_json::from_slice(&bytes(response).await).unwrap();
    assert_eq!(body["model"], "public");
    assert_eq!(
        gateway.metrics().await.sum(
            "lumewarp_requests_total",
            &[("model", "a"), ("status", "200")]
        ),
        1.,
        "a served it"
    );
    assert_eq!(
        gateway
            .metrics()
            .await
            .value("lumewarp_upstream_healthy", &[("upstream", "a")]),
        1.
    );
}

/// Polls the admin endpoint until `check` holds for the summed series.
async fn wait_metric(
    gateway: &Gateway,
    name: &str,
    labels: &[(&str, &str)],
    check: fn(f64) -> bool,
) {
    bounded(async {
        loop {
            if check(gateway.metrics().await.sum(name, labels)) {
                break;
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
    })
    .await;
}

/// Sends `n` sequential requests and returns how many `model` served.
async fn served_by(gateway: &Gateway, n: usize, headers: &[(&str, &str)], model: &str) -> usize {
    let served = |metrics: Prometheus| metrics.sum("lumewarp_requests_total", &[("model", model)]);
    let before = served(gateway.metrics().await);
    for _ in 0..n {
        let response = gateway.post(false, headers).await;
        assert_eq!(response.status(), 200);
        let body: serde_json::Value = serde_json::from_slice(&bytes(response).await).unwrap();
        assert_eq!(body["model"], "public");
    }
    (served(gateway.metrics().await) - before) as usize
}

#[tokio::test]
async fn vllm_load_avoids_congested_model_and_falls_back_when_stale() {
    let congested = Backend::start(&["--waiting", "50", "--kv-cache-usage", "0.9"]).await;
    let idle = Backend::start(&[]).await;
    let gateway = Gateway::start(config(
        congested.addr,
        idle.addr,
        "",
        "  - {name: public, members: [{model: a}, {model: b}], strategy: vllm_load}",
    ))
    .await;
    let queue = "lumewarp_vllm_queue_depth";
    wait_metric(&gateway, queue, &[("model", "a")], |v| v == 50.).await;
    wait_metric(
        &gateway,
        "lumewarp_vllm_kv_cache_usage",
        &[("model", "b")],
        |v| v == 0.,
    )
    .await;
    let to_congested = served_by(&gateway, 100, &[], "a").await;
    assert!(
        to_congested <= 10,
        "congested model served {to_congested}/100"
    );
    // The scrape now fails: a's stale queue no longer counts; with equal
    // in-flight load both models share the traffic again.
    congested.load("metrics_status=503").await;
    wait_metric(
        &gateway,
        "lumewarp_vllm_scrape_failures_total",
        &[("upstream", "a")],
        |v| v >= 1.,
    )
    .await;
    let to_congested = served_by(&gateway, 100, &[], "a").await;
    assert!(
        to_congested >= 20,
        "stale metrics fall back to in-flight: {to_congested}/100"
    );
    // Recovered metrics steer traffic away again after the next scrape.
    congested.load("metrics_status=0").await;
    tokio::time::sleep(Duration::from_millis(1200)).await;
    let to_congested = served_by(&gateway, 50, &[], "a").await;
    assert!(to_congested <= 5, "recovered: {to_congested}/50");
    assert_eq!(
        gateway
            .metrics()
            .await
            .sum("lumewarp_vllm_scrape_failures_total", &[("upstream", "b")]),
        0.
    );
}

#[tokio::test]
async fn lowest_latency_prefers_the_faster_upstream() {
    let slow = Backend::start(&["--latency-ms", "30"]).await;
    let fast = Backend::start(&[]).await;
    let gateway = Gateway::start(config(
        slow.addr,
        fast.addr,
        "",
        "  - {name: public, members: [{model: a}, {model: b}], strategy: lowest_latency}",
    ))
    .await;
    let to_slow = served_by(&gateway, 80, &[], "a").await;
    assert!(to_slow <= 12, "slow model served {to_slow}/80");
    let metrics = gateway.metrics().await;
    let ewma = |d: &str| metrics.sum("lumewarp_upstream_latency_ewma_seconds", &[("model", d)]);
    assert!(
        ewma("a") > 0.02 && ewma("b") < ewma("a"),
        "a={} b={}",
        ewma("a"),
        ewma("b")
    );
}

#[tokio::test]
async fn sticky_traffic_split_is_stable_end_to_end() {
    let backend = Backend::start(&[]).await;
    let gateway = Gateway::start(config(
        backend.addr,
        backend.addr,
        "",
        "  - name: public\n    traffic_split:\n      - {name: stable, members: [{model: a}], weight: 1}\n      - {name: canary, members: [{model: b}], weight: 1}\n    sticky: header\n    sticky_header: x-session",
    ))
    .await;
    let mut splits = [0usize; 2];
    for session in 0..12 {
        let session = format!("session-{session}");
        let to_a = served_by(&gateway, 5, &[("x-session", &session)], "a").await;
        assert!(to_a == 0 || to_a == 5, "{session} moved between splits");
        splits[usize::from(to_a == 0)] += 1;
    }
    assert!(splits[0] > 0 && splits[1] > 0, "{splits:?}");
    let metrics = gateway.metrics().await;
    assert_eq!(
        metrics.sum(
            "lumewarp_split_requests_total",
            &[("router", "public"), ("split", "canary")]
        ),
        (splits[1] * 5) as f64
    );
    assert_eq!(metrics.sum("lumewarp_split_requests_total", &[]), 60.);
}
