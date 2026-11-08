//! Shared helpers for the MCP and A2A integration tests.
#![allow(dead_code)]

use futures_util::StreamExt;
use mock_upstream::{
    a2a::{serve_a2a, A2aOptions, A2aServer},
    mcp::{serve_mcp, McpOptions, McpServer},
    util::sha256_hex,
};
use serde_json::{json, Value};
use std::{
    net::SocketAddr,
    sync::Arc,
    time::{Duration, Instant},
};
use tokio::net::TcpListener;

pub struct Running {
    pub url: String,
    pub addr: SocketAddr,
    task: tokio::task::JoinHandle<std::io::Result<()>>,
}

impl Drop for Running {
    fn drop(&mut self) {
        self.task.abort();
    }
}

/// Bind an ephemeral loopback port, then build the server for that exact
/// address so the A2A card advertises a usable URL.
pub async fn start_a2a(options: A2aOptions) -> Running {
    start_a2a_on("127.0.0.1:0", options).await
}

pub async fn start_a2a_on(bind: &str, mut options: A2aOptions) -> Running {
    let listener = TcpListener::bind(bind).await.unwrap();
    let addr = listener.local_addr().unwrap();
    options.listen = addr;
    let server = Arc::new(A2aServer::new(options).unwrap());
    Running {
        url: format!("http://{addr}"),
        addr,
        task: tokio::spawn(serve_a2a(listener, server)),
    }
}

pub async fn start_mcp(options: McpOptions) -> Running {
    start_mcp_on("127.0.0.1:0", options).await
}

pub async fn start_mcp_on(bind: &str, mut options: McpOptions) -> Running {
    let listener = TcpListener::bind(bind).await.unwrap();
    let addr = listener.local_addr().unwrap();
    options.listen = addr;
    let path = options.path.clone();
    let server = Arc::new(McpServer::new(options).unwrap());
    Running {
        url: format!("http://{addr}{path}"),
        addr,
        task: tokio::spawn(serve_mcp(listener, server)),
    }
}

pub fn client() -> reqwest::Client {
    reqwest::Client::builder()
        .no_proxy()
        .timeout(Duration::from_secs(10))
        .build()
        .unwrap()
}

/// A2A JSON-RPC call with the required `A2A-Version` header.
pub fn rpc_body(id: Value, method: &str, params: Value) -> Value {
    json!({"jsonrpc": "2.0", "id": id, "method": method, "params": params})
}

pub async fn post_rpc(
    client: &reqwest::Client,
    base: &str,
    path: &str,
    body: &Value,
) -> reqwest::Response {
    client
        .post(format!("{base}{path}"))
        .header("a2a-version", "1.0")
        .json(body)
        .send()
        .await
        .unwrap()
}

/// Read an SSE response to completion, returning each frame's JSON `data:`
/// payload. Empty priming events are skipped.
pub async fn read_sse(response: reqwest::Response) -> Vec<Value> {
    let content_type = response
        .headers()
        .get("content-type")
        .and_then(|value| value.to_str().ok())
        .unwrap_or_default()
        .to_owned();
    assert!(
        content_type.starts_with("text/event-stream"),
        "expected SSE, got {content_type}"
    );
    let mut stream = response.bytes_stream();
    let mut buffer = String::new();
    let mut events = Vec::new();
    let deadline = Instant::now() + Duration::from_secs(10);
    while Instant::now() < deadline {
        let Some(chunk) = stream.next().await else {
            break;
        };
        let chunk = chunk.unwrap();
        buffer.push_str(&String::from_utf8_lossy(&chunk));
        while let Some(index) = buffer.find("\n\n") {
            let frame: String = buffer.drain(..index + 2).collect();
            for line in frame.lines() {
                let line = line.trim_end_matches('\r');
                if line.starts_with(':') {
                    continue;
                }
                let Some(data) = line.strip_prefix("data:") else {
                    continue;
                };
                let data = data.trim();
                if data.is_empty() {
                    continue;
                }
                events.push(
                    serde_json::from_str(data)
                        .unwrap_or_else(|error| panic!("invalid SSE JSON {data:?}: {error}")),
                );
            }
        }
    }
    events
}

pub fn signed_headers(
    secret: &str,
    timestamp: u64,
    method: &str,
    path: &str,
    body: &[u8],
) -> Vec<(&'static str, String)> {
    use hmac::{Hmac, Mac};
    let canonical = format!("{timestamp}.{method}.{path}.{}", sha256_hex(body));
    let mut mac = Hmac::<sha2::Sha256>::new_from_slice(secret.as_bytes()).unwrap();
    mac.update(canonical.as_bytes());
    let signature = mock_upstream::util::hex_encode(&mac.finalize().into_bytes());
    vec![(
        "x-lumewarp-signature",
        format!("t={timestamp},v1={signature}"),
    )]
}

/// A non-loopback IPv4 address of this host, if it has one. UDP `connect` only
/// performs a route lookup; no packet leaves the host.
pub fn non_loopback_ip() -> Option<std::net::IpAddr> {
    let socket = std::net::UdpSocket::bind("0.0.0.0:0").ok()?;
    socket.connect("192.0.2.1:9").ok()?;
    let ip = socket.local_addr().ok()?.ip();
    (!ip.is_loopback() && !ip.is_unspecified()).then_some(ip)
}

/// Incremental SSE reader for tests that must react mid-stream (elicitation,
/// GET notifications). `next_event` skips priming frames with empty `data`.
pub struct SseReader {
    stream: futures_util::stream::BoxStream<'static, reqwest::Result<bytes::Bytes>>,
    buffer: String,
}

impl SseReader {
    pub fn new(response: reqwest::Response) -> Self {
        Self {
            stream: response.bytes_stream().boxed(),
            buffer: String::new(),
        }
    }

    pub async fn next_event(&mut self) -> Option<Value> {
        loop {
            while let Some(index) = self.buffer.find("\n\n") {
                let frame: String = self.buffer.drain(..index + 2).collect();
                for line in frame.lines() {
                    let line = line.trim_end_matches('\r');
                    if line.starts_with(':') {
                        continue;
                    }
                    let Some(data) = line.strip_prefix("data:") else {
                        continue;
                    };
                    let data = data.trim();
                    if data.is_empty() {
                        continue;
                    }
                    return Some(
                        serde_json::from_str(data)
                            .unwrap_or_else(|error| panic!("invalid SSE JSON {data:?}: {error}")),
                    );
                }
            }
            match self.stream.next().await? {
                Ok(chunk) => self.buffer.push_str(&String::from_utf8_lossy(&chunk)),
                Err(error) => panic!("SSE stream error: {error}"),
            }
        }
    }
}
