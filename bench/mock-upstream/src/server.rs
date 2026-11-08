//! HTTP plumbing shared by the example MCP server and A2A agent.

use crate::auth::AuthFailure;
use bytes::Bytes;
use futures_util::Stream;
use http_body_util::{combinators::UnsyncBoxBody, BodyExt, Full, StreamBody};
use hyper::{
    body::{Frame, Incoming},
    header::{HeaderMap, HeaderValue, ALLOW, CONTENT_TYPE, WWW_AUTHENTICATE},
    service::service_fn,
    Method, Request, Response, StatusCode,
};
use hyper_util::rt::TokioIo;
use serde_json::{json, Map, Value};
use std::{
    collections::VecDeque,
    convert::Infallible,
    net::SocketAddr,
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::net::TcpListener;

pub type Body = UnsyncBoxBody<Bytes, Infallible>;

/// Serve HTTP/1 keep-alive connections. `handler` receives the peer address so
/// the control endpoints can refuse anything that is not loopback.
pub async fn serve<F, Fut>(listener: TcpListener, handler: F) -> std::io::Result<()>
where
    F: Fn(Request<Incoming>, SocketAddr) -> Fut + Send + Sync + 'static,
    Fut: std::future::Future<Output = Response<Body>> + Send + 'static,
{
    let handler = Arc::new(handler);
    loop {
        let (socket, peer) = match listener.accept().await {
            Ok(connection) => connection,
            Err(error) => {
                eprintln!("mock server accept error: {error}");
                tokio::time::sleep(Duration::from_millis(1)).await;
                continue;
            }
        };
        let _ = socket.set_nodelay(true);
        let handler = handler.clone();
        tokio::spawn(async move {
            let service = service_fn(move |request| {
                let handler = handler.clone();
                async move { Ok::<_, Infallible>(handler(request, peer).await) }
            });
            let _ = hyper::server::conn::http1::Builder::new()
                .keep_alive(true)
                .serve_connection(TokioIo::new(socket), service)
                .await;
        });
    }
}

pub fn json(status: StatusCode, value: Value) -> Response<Body> {
    let body = serde_json::to_vec(&value).expect("JSON values are serializable");
    Response::builder()
        .status(status)
        .header(CONTENT_TYPE, "application/json")
        .body(Full::new(Bytes::from(body)).boxed_unsync())
        .expect("static headers")
}

pub fn raw(status: StatusCode, content_type: &'static str, body: Bytes) -> Response<Body> {
    Response::builder()
        .status(status)
        .header(CONTENT_TYPE, content_type)
        .body(Full::new(body).boxed_unsync())
        .expect("static headers")
}

pub fn empty(status: StatusCode) -> Response<Body> {
    Response::builder()
        .status(status)
        .body(Full::new(Bytes::new()).boxed_unsync())
        .expect("static headers")
}

pub fn method_not_allowed(allow: &'static str) -> Response<Body> {
    let mut response = json(
        StatusCode::METHOD_NOT_ALLOWED,
        json!({"error": "method_not_allowed"}),
    );
    response
        .headers_mut()
        .insert(ALLOW, HeaderValue::from_static(allow));
    response
}

pub fn not_found() -> Response<Body> {
    json(StatusCode::NOT_FOUND, json!({"error": "not_found"}))
}

/// The control endpoints are only reachable from the same machine.
pub fn control_forbidden() -> Response<Body> {
    json(
        StatusCode::FORBIDDEN,
        json!({"error": "control endpoints are loopback-only"}),
    )
}

pub fn unauthorized(failure: &AuthFailure) -> Response<Body> {
    let mut response = json(
        StatusCode::UNAUTHORIZED,
        json!({"error": "unauthorized", "reason": failure.reason, "detail": failure.detail}),
    );
    response.headers_mut().insert(
        WWW_AUTHENTICATE,
        HeaderValue::from_str(failure.challenge).expect("static challenge"),
    );
    response
}

pub fn rpc_error(
    status: StatusCode,
    id: Option<Value>,
    code: i64,
    message: &str,
) -> Response<Body> {
    json(
        status,
        json!({
            "jsonrpc": "2.0",
            "id": id.unwrap_or(Value::Null),
            "error": {"code": code, "message": message},
        }),
    )
}

pub fn rpc_result(id: Value, result: Value) -> Response<Body> {
    json(
        StatusCode::OK,
        json!({"jsonrpc": "2.0", "id": id, "result": result}),
    )
}

/// One SSE frame whose `data:` payload is a JSON-RPC message.
pub fn sse_frame(value: &Value) -> Bytes {
    Bytes::from(format!(
        "data: {}\n\n",
        serde_json::to_string(value).expect("JSON values are serializable")
    ))
}

pub fn sse<S>(stream: S) -> Response<Body>
where
    S: Stream<Item = Result<Frame<Bytes>, Infallible>> + Send + 'static,
{
    Response::builder()
        .status(StatusCode::OK)
        .header(CONTENT_TYPE, "text/event-stream")
        .header("cache-control", "no-cache")
        .header("x-accel-buffering", "no")
        .body(StreamBody::new(stream).boxed_unsync())
        .expect("static headers")
}

pub fn header_str<'a>(headers: &'a HeaderMap, name: &str) -> Option<&'a str> {
    headers.get(name).and_then(|value| value.to_str().ok())
}

/// Record the last N requests so tests can assert what an upstream received.
#[derive(Debug)]
pub struct RequestLog {
    limit: usize,
    entries: Mutex<VecDeque<Value>>,
}

impl RequestLog {
    pub fn new(limit: usize) -> Self {
        Self {
            limit: limit.max(1),
            entries: Mutex::new(VecDeque::new()),
        }
    }

    pub fn record(&self, method: &Method, path: &str, headers: &HeaderMap, peer: SocketAddr) {
        let mut values = Map::new();
        for (name, value) in headers {
            values.insert(
                name.as_str().to_owned(),
                Value::String(String::from_utf8_lossy(value.as_bytes()).into_owned()),
            );
        }
        let entry = json!({
            "at_ms": crate::util::now_ms(),
            "peer": peer.to_string(),
            "method": method.as_str(),
            "path": path,
            "headers": values,
        });
        let mut entries = self
            .entries
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        entries.push_back(entry);
        while entries.len() > self.limit {
            entries.pop_front();
        }
    }

    /// Most recent last, capped at the configured limit.
    pub fn last(&self, count: usize) -> Vec<Value> {
        let entries = self
            .entries
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let start = entries.len().saturating_sub(count);
        entries.iter().skip(start).cloned().collect()
    }

    pub fn clear(&self) {
        self.entries
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .clear();
    }
}

/// Read `name` from a query string without decoding percent escapes (the mock
/// only needs simple values such as `n=10`).
pub fn query_param(query: &str, name: &str) -> Option<String> {
    query.split('&').find_map(|pair| {
        let (key, value) = pair.split_once('=').unwrap_or((pair, ""));
        (key == name).then(|| value.to_owned())
    })
}

pub fn is_loopback(peer: SocketAddr) -> bool {
    peer.ip().is_loopback()
}

/// Parse a JSON body for a control endpoint; `None` means it was not JSON.
pub fn control_body(bytes: &Bytes) -> Option<Value> {
    serde_json::from_slice(bytes).ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn loopback_only() {
        assert!(is_loopback("127.0.0.1:1234".parse().unwrap()));
        assert!(is_loopback("[::1]:1234".parse().unwrap()));
        assert!(!is_loopback("192.0.2.1:1234".parse().unwrap()));
        assert!(!is_loopback("0.0.0.0:1234".parse().unwrap()));
    }

    #[test]
    fn query_values() {
        assert_eq!(query_param("n=5&x=1", "n").as_deref(), Some("5"));
        assert_eq!(query_param("x=1", "n"), None);
        assert_eq!(query_param("flag", "flag").as_deref(), Some(""));
    }
}
