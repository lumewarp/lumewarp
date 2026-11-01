mod common;

use std::{
    io::{Read, Write},
    time::{Duration, Instant},
};

use bytes::Bytes;
use common::*;
use http_body_util::{BodyExt, Full};
use hyper::{body::Incoming, Request};
use hyper_util::rt::TokioIo;

const ENDPOINT: &[(&str, &str)] = &[("endpoint", "chat_completions")];
const PUBLIC: &[(&str, &str)] = &[
    ("requested", "public"),
    ("model", "d0"),
    ("endpoint", "chat_completions"),
];
const UNKNOWN: &[(&str, &str)] = &[
    ("requested", "unknown"),
    ("model", ""),
    ("endpoint", "chat_completions"),
];
const STREAM: &[u8] = br#"{"model":"public","stream":true}"#;
const NORMAL: &[u8] = br#"{"model":"public"}"#;

async fn wait_metrics(gateway: &Gateway, condition: impl Fn(&Prometheus) -> bool) -> Prometheus {
    bounded(async {
        loop {
            let metrics = gateway.metrics().await;
            if condition(&metrics) {
                return metrics;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
}

async fn first_event(body: &mut Incoming) {
    let received = bounded(async {
        let mut received = Vec::new();
        while received.len() < FIRST.len() {
            let frame = body.frame().await.expect("first event before EOF").unwrap();
            if let Ok(data) = frame.into_data() {
                received.extend_from_slice(&data);
            }
        }
        received
    })
    .await;
    assert_eq!(received, FIRST);
}

#[test]
fn prometheus_parser_handles_labels_and_histograms() {
    let text = "# TYPE example counter\nexample{key=\"a,b c\\\"d\",empty=\"\"} 2\nhist_bucket{le=\"+Inf\"} 3\nhist_count 3\n# EOF\n";
    let parsed = Prometheus::parse(text);
    assert_eq!(
        parsed.value("example", &[("key", "a,b c\"d"), ("empty", "")]),
        2.0
    );
    assert_eq!(parsed.value("hist_bucket", &[("le", "+Inf")]), 3.0);
    assert_eq!(parsed.value("hist_count", &[]), 3.0);
    assert_eq!(parsed.sum("missing", &[]), 0.0);
}

#[tokio::test]
async fn request_counts_labels_in_flight_and_histograms_match() {
    let mut mock = Mock::start(Behavior::ControlledStream).await;
    let gateway = Gateway::start(&[mock.addr], false, 5000).await;
    let before = gateway.metrics().await;
    let response = gateway
        .request("POST", "/v1/chat/completions", NORMAL, &[])
        .await;
    assert_eq!(response.status(), 200);
    bytes(response).await;
    mock.next().await;
    let response = gateway
        .request("POST", "/v1/chat/completions", STREAM, &[])
        .await;
    let mut body = response.into_body();
    first_event(&mut body).await;
    let control = mock.next().await.stream.unwrap();
    assert_eq!(
        gateway
            .metrics()
            .await
            .value("lumewarp_in_flight_requests", &[("model", "d0")]),
        1.0
    );
    control.frames.send(Bytes::from_static(LAST)).await.unwrap();
    drop(control.frames);
    assert_eq!(bounded(body.collect()).await.unwrap().to_bytes(), LAST);
    let response = gateway
        .request(
            "POST",
            "/v1/chat/completions",
            br#"{"model":"untrusted-user-input"}"#.as_slice(),
            &[],
        )
        .await;
    assert_eq!(response.status(), 404);
    bytes(response).await;
    let after = wait_metrics(&gateway, |m| {
        m.sum("lumewarp_requests_total", ENDPOINT) - before.sum("lumewarp_requests_total", ENDPOINT)
            == 3.0
    })
    .await;
    let mut public_200 = PUBLIC.to_vec();
    public_200.push(("status", "200"));
    let mut unknown_404 = UNKNOWN.to_vec();
    unknown_404.push(("status", "404"));
    assert_eq!(
        after.value("lumewarp_requests_total", &public_200)
            - before.value("lumewarp_requests_total", &public_200),
        2.0
    );
    assert_eq!(
        after.value("lumewarp_requests_total", &unknown_404)
            - before.value("lumewarp_requests_total", &unknown_404),
        1.0
    );
    assert_eq!(
        after.value("lumewarp_request_duration_seconds_count", PUBLIC)
            - before.value("lumewarp_request_duration_seconds_count", PUBLIC),
        2.0
    );
    assert_eq!(
        after.value("lumewarp_request_duration_seconds_count", UNKNOWN)
            - before.value("lumewarp_request_duration_seconds_count", UNKNOWN),
        1.0
    );
    assert_eq!(
        after.sum("lumewarp_request_duration_seconds_count", ENDPOINT)
            - before.sum("lumewarp_request_duration_seconds_count", ENDPOINT),
        3.0
    );
    assert_eq!(
        after.value("lumewarp_in_flight_requests", &[("model", "d0")]),
        0.0
    );
    assert_eq!(
        after.sum("lumewarp_ttft_seconds_count", &[("requested", "public")])
            - before.sum("lumewarp_ttft_seconds_count", &[("requested", "public")]),
        1.0
    );
    assert!(!after.samples.iter().any(|sample| sample
        .labels
        .values()
        .any(|value| value == "untrusted-user-input")));
}

#[tokio::test]
async fn headers_first_prefill_is_ttft_not_gateway_overhead() {
    let mut mock = Mock::start(Behavior::HeadersFirstStream).await;
    let gateway = Gateway::start(&[mock.addr], false, 5000).await;
    let before = gateway.metrics().await;
    let response = gateway
        .request("POST", "/v1/chat/completions", STREAM, &[])
        .await;
    assert_eq!(response.status(), 200);
    assert_eq!(response.headers()["content-type"], "text/event-stream");
    let control = mock.next().await.stream.unwrap();
    // No frame is queued. Receiving headers above must not depend on prefill.
    tokio::time::sleep(Duration::from_millis(300)).await;
    control
        .frames
        .send(Bytes::from_static(FIRST))
        .await
        .unwrap();
    control.frames.send(Bytes::from_static(LAST)).await.unwrap();
    drop(control.frames);
    let expected = [FIRST, LAST].concat();
    assert_eq!(bytes(response).await, expected);
    let after = wait_metrics(&gateway, |m| {
        m.sum("lumewarp_requests_total", ENDPOINT) > before.sum("lumewarp_requests_total", ENDPOINT)
    })
    .await;
    let overhead = after.value("lumewarp_gateway_overhead_seconds_sum", ENDPOINT)
        - before.value("lumewarp_gateway_overhead_seconds_sum", ENDPOINT);
    let ttft = after.value(
        "lumewarp_ttft_seconds_sum",
        &[("requested", "public"), ("model", "d0")],
    ) - before.value(
        "lumewarp_ttft_seconds_sum",
        &[("requested", "public"), ("model", "d0")],
    );
    assert!(
        (0.0..0.050).contains(&overhead),
        "prefill leaked into overhead: {overhead}s"
    );
    assert!(ttft >= 0.300, "missing prefill in TTFT: {ttft}s");
}

#[tokio::test]
async fn client_cancel_before_headers_records_499_not_500() {
    let mut mock = Mock::start(Behavior::DelayedHeaders).await;
    let gateway = Gateway::start(&[mock.addr], false, 5000).await;
    let before = gateway.metrics().await;
    let socket = bounded(tokio::net::TcpStream::connect(gateway.addr))
        .await
        .unwrap();
    let (mut sender, connection) =
        hyper::client::conn::http1::handshake::<_, Full<Bytes>>(TokioIo::new(socket))
            .await
            .unwrap();
    let driver = tokio::spawn(connection);
    let request = Request::builder()
        .method("POST")
        .uri("/v1/chat/completions")
        .header("host", gateway.addr.to_string())
        .body(Full::new(Bytes::from_static(STREAM)))
        .unwrap();
    let pending = tokio::spawn(async move { sender.send_request(request).await });
    mock.next().await; // Confirms the request was routed before starting the cancel delay.
    tokio::time::sleep(Duration::from_millis(200)).await;
    driver.abort();
    pending.abort();
    let _ = driver.await;
    let _ = pending.await;
    let mut cancelled = PUBLIC.to_vec();
    cancelled.push(("status", "499"));
    let after = wait_metrics(&gateway, |m| {
        m.value("lumewarp_requests_total", &cancelled)
            - before.value("lumewarp_requests_total", &cancelled)
            == 1.0
    })
    .await;
    assert_eq!(
        after.sum(
            "lumewarp_requests_total",
            &[("endpoint", "chat_completions"), ("status", "500")]
        ) - before.sum(
            "lumewarp_requests_total",
            &[("endpoint", "chat_completions"), ("status", "500")]
        ),
        0.0
    );
    assert_eq!(
        after.sum("lumewarp_requests_total", ENDPOINT)
            - before.sum("lumewarp_requests_total", ENDPOINT),
        1.0
    );
    assert_eq!(
        after.value("lumewarp_in_flight_requests", &[("model", "d0")]),
        0.0
    );
}

#[tokio::test]
async fn mid_stream_failure_counts_once_and_keeps_http_200() {
    let mut mock = Mock::start(Behavior::BrokenStream).await;
    let gateway = Gateway::start(&[mock.addr], false, 5000).await;
    let before = gateway.metrics().await;
    let response = gateway
        .request("POST", "/v1/chat/completions", STREAM, &[])
        .await;
    assert_eq!(response.status(), 200);
    let mut body = response.into_body();
    first_event(&mut body).await;
    let control = mock.next().await.stream.unwrap();
    // Closing this producer injects an error, not a clean chunked EOF.
    drop(control.frames);
    assert!(bounded(body.collect()).await.is_err());
    let labels = &[("model", "d0"), ("kind", "stream")];
    let after = wait_metrics(&gateway, |m| {
        m.value("lumewarp_upstream_errors_total", labels)
            - before.value("lumewarp_upstream_errors_total", labels)
            == 1.0
            && m.sum("lumewarp_requests_total", ENDPOINT)
                > before.sum("lumewarp_requests_total", ENDPOINT)
    })
    .await;
    assert_eq!(
        after.sum("lumewarp_upstream_errors_total", &[("model", "d0")])
            - before.sum("lumewarp_upstream_errors_total", &[("model", "d0")]),
        1.0
    );
    assert_eq!(
        after.sum(
            "lumewarp_requests_total",
            &[("endpoint", "chat_completions"), ("status", "200")]
        ) - before.sum(
            "lumewarp_requests_total",
            &[("endpoint", "chat_completions"), ("status", "200")]
        ),
        1.0
    );
    assert_eq!(
        after.value("lumewarp_in_flight_requests", &[("model", "d0")]),
        0.0
    );
}

#[tokio::test]
async fn connect_and_protocol_errors_have_distinct_kinds() {
    let closed = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = closed.local_addr().unwrap();
    let connect = Gateway::start(&[addr], false, 5000).await;
    drop(closed);
    let mock = Mock::start(Behavior::ImmediateClose).await;
    let protocol = Gateway::start(&[mock.addr], false, 5000).await;
    for (gateway, kind) in [(&connect, "connect"), (&protocol, "protocol")] {
        let before = gateway.metrics().await;
        let response = gateway
            .request("POST", "/v1/chat/completions", NORMAL, &[])
            .await;
        assert_eq!(response.status(), 502);
        bytes(response).await;
        let labels = &[("model", "d0"), ("kind", kind)];
        let after = wait_metrics(gateway, |m| {
            m.value("lumewarp_upstream_errors_total", labels)
                > before.value("lumewarp_upstream_errors_total", labels)
        })
        .await;
        assert_eq!(
            after.value("lumewarp_upstream_errors_total", labels)
                - before.value("lumewarp_upstream_errors_total", labels),
            1.0
        );
        assert_eq!(
            after.sum("lumewarp_upstream_errors_total", &[("model", "d0")])
                - before.sum("lumewarp_upstream_errors_total", &[("model", "d0")]),
            1.0
        );
    }
}

#[tokio::test]
async fn body_read_timeout_returns_408_for_stalled_upload() {
    let mut mock = Mock::start(Behavior::Json).await;
    let gateway =
        Gateway::start_with_server(&[mock.addr], false, 5000, "  body_read_timeout_ms: 300\n")
            .await;
    let addr = gateway.addr;
    let (status, elapsed) = bounded(tokio::task::spawn_blocking(move || {
        let mut socket = std::net::TcpStream::connect(addr).unwrap();
        socket.set_read_timeout(Some(Duration::from_secs(1))).unwrap();
        socket.set_write_timeout(Some(Duration::from_secs(1))).unwrap();
        let start = Instant::now();
        write!(socket, "POST /v1/chat/completions HTTP/1.1\r\nHost: {addr}\r\nContent-Type: application/json\r\nContent-Length: 100\r\nConnection: close\r\n\r\n{{\"model\":").unwrap();
        let mut status = String::new();
        std::io::BufRead::read_line(&mut std::io::BufReader::new(socket), &mut status).unwrap();
        (status, start.elapsed())
    })).await.unwrap();
    assert!(status.starts_with("HTTP/1.1 408"), "{status}");
    assert!(
        elapsed >= Duration::from_millis(250) && elapsed < Duration::from_secs(1),
        "body timeout took {elapsed:?}"
    );
    assert!(mock.records.try_recv().is_err());
}

#[cfg(unix)]
#[tokio::test]
async fn shutdown_grace_aborts_held_stream_and_exits_zero() {
    let mut mock = Mock::start(Behavior::ControlledStream).await;
    let mut gateway =
        Gateway::start_with_server(&[mock.addr], false, 5000, "  shutdown_grace_ms: 500\n").await;
    let response = gateway
        .request("POST", "/v1/chat/completions", STREAM, &[])
        .await;
    let mut body = response.into_body();
    first_event(&mut body).await;
    let control = mock.next().await.stream.unwrap();
    let start = Instant::now();
    assert!(std::process::Command::new("kill")
        .arg("-TERM")
        .arg(gateway.child.id().to_string())
        .status()
        .unwrap()
        .success());
    let status = tokio::time::timeout(Duration::from_secs(2), async {
        loop {
            if let Some(status) = gateway.child.try_wait().unwrap() {
                break status;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .expect("shutdown exceeded 2 seconds");
    assert!(status.success(), "shutdown exited {status}");
    assert!(
        start.elapsed() >= Duration::from_millis(450),
        "did not hold stream through grace period"
    );
    assert!(
        bounded(body.collect()).await.is_err(),
        "stream must be truncated at drain deadline"
    );
    // Producer is intentionally still open until shutdown has completed.
    drop(control.frames);
}

#[tokio::test]
async fn header_read_timeout_closes_partial_request() {
    let mut mock = Mock::start(Behavior::Json).await;
    let gateway =
        Gateway::start_with_server(&[mock.addr], false, 5000, "  header_read_timeout_ms: 300\n")
            .await;
    let addr = gateway.addr;
    let elapsed = bounded(tokio::task::spawn_blocking(move || {
        let mut socket = std::net::TcpStream::connect(addr).unwrap();
        socket
            .set_read_timeout(Some(Duration::from_millis(1500)))
            .unwrap();
        socket
            .set_write_timeout(Some(Duration::from_secs(1)))
            .unwrap();
        let start = Instant::now();
        socket.write_all(b"POST /v1/chat").unwrap();
        let mut received = Vec::new();
        if let Err(error) = socket.read_to_end(&mut received) {
            assert_eq!(
                error.kind(),
                std::io::ErrorKind::ConnectionReset,
                "not a server close: {error}"
            );
        }
        start.elapsed()
    }))
    .await
    .unwrap();
    assert!(
        elapsed >= Duration::from_millis(250) && elapsed < Duration::from_millis(1500),
        "header timeout took {elapsed:?}"
    );
    assert!(mock.records.try_recv().is_err());
}
