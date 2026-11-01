//! A real gateway process exporting OTLP to an in-process sink. OTel is where a
//! single request is looked up, so the fields and what stays out of them are
//! part of the contract. The OTLP protobuf is walked by hand: only the handful
//! of fields asserted here are needed and no dependency is added for them.
mod common;

use bytes::Bytes;
use clap::Parser;
use common::{bounded, bytes, client, TestClient};
use http_body_util::{BodyExt, Full};
use hyper::{service::service_fn, Request, Response};
use hyper_util::rt::TokioIo;
use mock_upstream::{App, Options};
use sha2::{Digest, Sha256};
use std::{
    collections::HashMap,
    io::{BufRead, BufReader},
    net::SocketAddr,
    process::{Child, Command, Stdio},
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::{net::TcpListener, sync::Notify};

const SECRET: &str = "sk-otel-secret-key-do-not-leak";
const PROMPT: &str = "the-prompt-must-not-be-exported";

#[derive(Debug, Default, Clone)]
struct Span {
    name: String,
    trace_id: String,
    parent: String,
    attributes: HashMap<String, String>,
}

impl Span {
    fn get(&self, key: &str) -> Option<&str> {
        self.attributes.get(key).map(String::as_str)
    }
}

/// (field number, wire type, value or length-delimited bytes)
fn fields(mut data: &[u8]) -> Vec<(u64, u8, u64, &[u8])> {
    fn varint(data: &mut &[u8]) -> u64 {
        let (mut value, mut shift) = (0u64, 0);
        loop {
            let byte = data[0];
            *data = &data[1..];
            value |= u64::from(byte & 0x7f) << shift;
            if byte & 0x80 == 0 {
                return value;
            }
            shift += 7;
        }
    }
    let mut out = Vec::new();
    while !data.is_empty() {
        let tag = varint(&mut data);
        let (number, wire) = (tag >> 3, (tag & 7) as u8);
        match wire {
            0 => out.push((number, wire, varint(&mut data), &[][..])),
            1 => {
                out.push((number, wire, 0, &data[..8]));
                data = &data[8..];
            }
            2 => {
                let len = varint(&mut data) as usize;
                out.push((number, wire, 0, &data[..len]));
                data = &data[len..];
            }
            5 => {
                out.push((number, wire, 0, &data[..4]));
                data = &data[4..];
            }
            other => panic!("wire type {other}"),
        }
    }
    out
}

fn text(data: &[u8]) -> String {
    String::from_utf8_lossy(data).into_owned()
}

fn hex_id(data: &[u8]) -> String {
    data.iter().map(|b| format!("{b:02x}")).collect()
}

/// ExportTraceServiceRequest.resource_spans(1).scope_spans(2).spans(2).
fn decode(request: &[u8]) -> Vec<Span> {
    let mut spans = Vec::new();
    for (_, _, _, resource) in fields(request).into_iter().filter(|f| f.0 == 1) {
        for (_, _, _, scope) in fields(resource).into_iter().filter(|f| f.0 == 2) {
            for (_, _, _, raw) in fields(scope).into_iter().filter(|f| f.0 == 2) {
                let mut span = Span::default();
                for (number, _, _, value) in fields(raw) {
                    match number {
                        1 => span.trace_id = hex_id(value),
                        4 => span.parent = hex_id(value),
                        5 => span.name = text(value),
                        9 => {
                            let (mut key, mut attr) = (String::new(), String::new());
                            for (n, _, _, v) in fields(value) {
                                match n {
                                    1 => key = text(v),
                                    2 => {
                                        for (kind, _, number, s) in fields(v) {
                                            attr = match kind {
                                                1 => text(s),
                                                2 => (number != 0).to_string(),
                                                3 => number.to_string(),
                                                _ => attr,
                                            };
                                        }
                                    }
                                    _ => {}
                                }
                            }
                            span.attributes.insert(key, attr);
                        }
                        _ => {}
                    }
                }
                spans.push(span);
            }
        }
    }
    spans
}

#[derive(Default)]
struct Sink {
    spans: Mutex<Vec<Span>>,
    raw: Mutex<Vec<u8>>,
    arrived: Notify,
}

impl Sink {
    async fn start() -> (Arc<Self>, SocketAddr) {
        let sink = Arc::new(Self::default());
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let shared = sink.clone();
        tokio::spawn(async move {
            while let Ok((stream, _)) = listener.accept().await {
                let sink = shared.clone();
                tokio::spawn(hyper::server::conn::http1::Builder::new().serve_connection(
                    TokioIo::new(stream),
                    service_fn(move |request: Request<hyper::body::Incoming>| {
                        let sink = sink.clone();
                        async move {
                            assert_eq!(request.uri().path(), "/v1/traces");
                            let body = request.into_body().collect().await?.to_bytes();
                            sink.raw.lock().unwrap().extend_from_slice(&body);
                            sink.spans.lock().unwrap().extend(decode(&body));
                            sink.arrived.notify_waiters();
                            Ok::<_, hyper::Error>(Response::new(Full::new(Bytes::new())))
                        }
                    }),
                ));
            }
        });
        (sink, addr)
    }

    /// The spans of the request whose id the response header gave.
    async fn wait_request(&self, request_id: &str) -> Vec<Span> {
        bounded(async {
            loop {
                let arrived = self.arrived.notified();
                let found = {
                    let spans = self.spans.lock().unwrap();
                    spans
                        .iter()
                        .find(|s| s.name == "request" && s.get("request_id") == Some(request_id))
                        .map(|root| {
                            spans
                                .iter()
                                .filter(|s| s.trace_id == root.trace_id)
                                .cloned()
                                .collect::<Vec<_>>()
                        })
                };
                if let Some(found) = found {
                    return found;
                }
                arrived.await;
            }
        })
        .await
    }
}

struct Gateway {
    addr: SocketAddr,
    child: Child,
    http: TestClient,
    _dir: tempfile::TempDir,
}

impl Gateway {
    async fn start(sink: SocketAddr, good: SocketAddr, bad: SocketAddr) -> Self {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("config.yaml");
        let hash = hex::encode(Sha256::digest(SECRET));
        std::fs::write(
            &path,
            format!(
                r#"server:
  listen: '127.0.0.1:0'
  admin_listen: '127.0.0.2:0'
upstreams:
  - {{name: good-up, kind: vllm, base_url: 'http://{good}/v1', timeout_ms: 2000}}
  - {{name: bad-up, kind: vllm, base_url: 'http://{bad}/v1', timeout_ms: 2000}}
models:
  - {{name: good, upstream: good-up, upstream_model: actual-good}}
  - {{name: bad, upstream: bad-up, upstream_model: actual-bad}}
routers:
  - {{name: pool, members: [{{model: bad}}], retry: {{max_attempts: 2}}, fallbacks: [good]}}
aliases:
  - {{name: nick, target: pool}}
teams:
  - {{id: team-a}}
keys:
  - {{key_id: key-a, key_sha256: '{hash}', team_id: team-a, allowed_models: [nick, good]}}
"#
            ),
        )
        .unwrap();
        let mut child = Command::new(env!("CARGO_BIN_EXE_lumewarp"))
            .arg("--config")
            .arg(path)
            .env("RUST_LOG", "info")
            .env("NO_COLOR", "1")
            .env("OTEL_EXPORTER_OTLP_ENDPOINT", format!("http://{sink}"))
            .env("OTEL_BSP_SCHEDULE_DELAY", "50")
            .env_remove("OTEL_TRACES_SAMPLER")
            .env_remove("OTEL_TRACES_SAMPLER_ARG")
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .unwrap();
        let stdout = child.stdout.take().unwrap();
        let (tx, rx) = tokio::sync::oneshot::channel();
        std::thread::spawn(move || {
            let mut tx = Some(tx);
            for line in BufReader::new(stdout).lines().map_while(Result::ok) {
                if line.contains("lumewarp gateway listening") {
                    let addr = line
                        .split_whitespace()
                        .find_map(|part| part.strip_prefix("listen="))
                        .unwrap()
                        .parse::<SocketAddr>()
                        .unwrap();
                    if let Some(tx) = tx.take() {
                        let _ = tx.send(addr);
                    }
                }
            }
        });
        let addr = tokio::time::timeout(Duration::from_secs(10), rx)
            .await
            .expect("gateway listening")
            .expect("gateway exited");
        Self {
            addr,
            child,
            http: client(),
            _dir: dir,
        }
    }

    /// Returns the response status and its request id.
    async fn call(&self, model: &str, traceparent: Option<&str>) -> (u16, String) {
        let mut request = Request::builder()
            .method("POST")
            .uri(format!("http://{}/v1/chat/completions", self.addr))
            .header("authorization", format!("Bearer {SECRET}"));
        if let Some(traceparent) = traceparent {
            request = request.header("traceparent", traceparent);
        }
        let body =
            format!(r#"{{"model":"{model}","messages":[{{"role":"user","content":"{PROMPT}"}}]}}"#);
        let response = bounded(
            self.http
                .request(request.body(Full::new(Bytes::from(body))).unwrap()),
        )
        .await
        .unwrap();
        let status = response.status().as_u16();
        let id = response.headers()["x-lumewarp-request-id"]
            .to_str()
            .unwrap()
            .to_owned();
        bytes(response).await;
        (status, id)
    }
}

impl Drop for Gateway {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

async fn mock(extra: &[&str]) -> SocketAddr {
    let mut args = vec!["mock", "--latency-ms", "0"];
    args.extend_from_slice(extra);
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(mock_upstream::serve(
        listener,
        App::new(Options::parse_from(args)),
    ));
    addr
}

async fn setup() -> (Arc<Sink>, Gateway) {
    let (sink, sink_addr) = Sink::start().await;
    let good = mock(&[]).await;
    let bad = mock(&["--fail-every", "1"]).await;
    let gateway = Gateway::start(sink_addr, good, bad).await;
    (sink, gateway)
}

#[tokio::test]
async fn request_span_has_the_whole_story_and_no_secrets() {
    let (sink, gateway) = setup().await;
    let (status, id) = gateway.call("nick", None).await;
    assert_eq!(status, 200);
    let spans = sink.wait_request(&id).await;
    let root = spans.iter().find(|s| s.name == "request").unwrap();
    for (field, want) in [
        ("request_id", id.as_str()),
        ("team_id", "team-a"),
        ("key_id", "key-a"),
        ("requested", "nick"),
        ("alias", "nick"),
        ("router", "pool"),
        ("model", "good"),
        ("upstream", "good-up"),
        ("upstream_model", "actual-good"),
        ("attempts", "2"),
        ("fallback", "true"),
        ("status", "200"),
    ] {
        assert_eq!(root.get(field), Some(want), "{field}: {root:?}");
    }
    assert!(root.get("config_version").is_some(), "{root:?}");

    // upstream_attempt spans are children of it, one per try. Retry budget 2:
    // the bad model, then the fallback.
    let mut attempts: Vec<_> = spans
        .iter()
        .filter(|s| s.name == "upstream_attempt")
        .collect();
    assert_eq!(attempts.len(), 2, "{spans:?}");
    attempts.sort_by_key(|s| s.get("attempt").map(str::to_owned));
    let first = attempts
        .iter()
        .find(|s| s.get("upstream") == Some("bad-up"))
        .unwrap();
    assert_eq!(first.get("upstream_model"), Some("actual-bad"));
    assert_eq!(first.get("upstream_status"), Some("503"));
    assert_eq!(first.get("fallback"), Some("false"));
    let second = attempts
        .iter()
        .find(|s| s.get("upstream") == Some("good-up"))
        .unwrap();
    assert_eq!(second.get("upstream_model"), Some("actual-good"));
    assert_eq!(second.get("upstream_status"), Some("200"));
    assert_eq!(second.get("fallback"), Some("true"));

    let raw = sink.raw.lock().unwrap();
    for secret in [SECRET, PROMPT, hex::encode(Sha256::digest(SECRET)).as_str()] {
        assert!(
            !raw.windows(secret.len()).any(|w| w == secret.as_bytes()),
            "{secret} was exported"
        );
    }
}

#[tokio::test]
async fn direct_call_has_empty_provenance() {
    let (sink, gateway) = setup().await;
    let (status, id) = gateway.call("good", None).await;
    assert_eq!(status, 200);
    let spans = sink.wait_request(&id).await;
    let root = spans.iter().find(|s| s.name == "request").unwrap();
    assert_eq!(root.get("requested"), Some("good"));
    assert_eq!(root.get("alias"), None);
    assert_eq!(root.get("router"), None);
    assert_eq!(root.get("model"), Some("good"));
    assert_eq!(root.get("attempts"), Some("1"));
    assert_eq!(root.get("fallback"), Some("false"));
}

#[tokio::test]
async fn rejection_is_recorded_with_the_requested_name() {
    let (sink, gateway) = setup().await;
    let (status, id) = gateway.call("bad", None).await;
    assert_eq!(status, 403);
    let spans = sink.wait_request(&id).await;
    let root = spans.iter().find(|s| s.name == "request").unwrap();
    assert_eq!(root.get("requested"), Some("bad"));
    assert_eq!(root.get("key_id"), Some("key-a"));
    assert_eq!(root.get("status"), Some("403"));
    assert_eq!(root.get("model"), Some(""));
}

/// A client asking not to be sampled still gets a record, in its own trace.
#[tokio::test]
async fn a_client_cannot_suppress_its_own_record() {
    let (sink, gateway) = setup().await;
    let trace_id = "0af7651916cd43dd8448eb211c80319c";
    let parent = "b7ad6b7169203331";
    let (status, id) = gateway
        .call("good", Some(&format!("00-{trace_id}-{parent}-00")))
        .await;
    assert_eq!(status, 200);
    let spans = sink.wait_request(&id).await;
    let root = spans.iter().find(|s| s.name == "request").unwrap();
    assert_eq!(root.trace_id, trace_id);
    assert_eq!(root.parent, parent);
}
