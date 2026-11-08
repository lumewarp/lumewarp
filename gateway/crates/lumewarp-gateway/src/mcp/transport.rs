//! Bounded HTTP and SSE transport. Only discovery/initialize responses are collected.
use super::session::Leg;
use crate::{
    proxy::{self, ProxyBody},
    runtime::Runtime,
    targets::McpTarget,
    upstream_auth,
};
use bytes::{Bytes, BytesMut};
use http_body_util::{BodyExt, Full, StreamBody};
use hyper::{
    body::{Frame, Incoming},
    HeaderMap, Method, Request, Response, StatusCode,
};
use serde_json::Value;
use tokio_stream::wrappers::ReceiverStream;
use tracing::Instrument;
pub const REQUEST_LIMIT: usize = 4 * 1024 * 1024;
pub const RESPONSE_LIMIT: usize = 16 * 1024 * 1024;
pub const EVENT_LIMIT: usize = 1024 * 1024;
pub async fn collect(mut body: Incoming, limit: usize) -> Result<Bytes, ()> {
    let mut out = BytesMut::new();
    while let Some(frame) = body.frame().await {
        if let Ok(data) = frame.map_err(|_| ())?.into_data() {
            if data.len() > limit.saturating_sub(out.len()) {
                return Err(());
            }
            out.extend_from_slice(&data);
        }
    }
    Ok(out.freeze())
}
/// Failures preserve timeout provenance without exposing plugin error text.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SendError {
    AuthTimeout,
    UpstreamTimeout,
    Other,
}
impl SendError {
    pub fn status(self) -> StatusCode {
        match self {
            Self::AuthTimeout | Self::UpstreamTimeout => StatusCode::GATEWAY_TIMEOUT,
            Self::Other => StatusCode::BAD_GATEWAY,
        }
    }
}
pub async fn send(
    runtime: &Runtime,
    target: &McpTarget,
    headers: &HeaderMap,
    method: Method,
    leg: Option<&Leg>,
    body: Bytes,
) -> Result<Response<Incoming>, SendError> {
    let mut headers = headers.clone();
    // Never forward framing from the inbound connection.
    if let Some(connection) = headers
        .get("connection")
        .and_then(|h| h.to_str().ok())
        .map(str::to_owned)
    {
        for name in connection.split(',') {
            headers.remove(name.trim());
        }
    }
    for h in [
        "host",
        "content-length",
        "connection",
        "transfer-encoding",
        "upgrade",
        "keep-alive",
        "te",
        "trailer",
        "proxy-connection",
        "accept-encoding",
        "origin",
        "mcp-session-id",
        "mcp-protocol-version",
    ] {
        headers.remove(h);
    }
    if let Some(leg) = leg {
        headers.insert(
            "mcp-protocol-version",
            leg.version.parse().map_err(|_| SendError::Other)?,
        );
        if let Some(session) = &leg.session {
            headers.insert(
                "mcp-session-id",
                session.parse().map_err(|_| SendError::Other)?,
            );
        }
    }
    upstream_auth::authorize_mcp(target, &target.url, &method, &body, &mut headers)
        .await
        .map_err(|failure| match failure {
            upstream_auth::Failure::Timeout => SendError::AuthTimeout,
            _ => SendError::Other,
        })?;
    let mut req = Request::builder()
        .method(method)
        .uri(target.url.clone())
        .body(Full::new(body))
        .map_err(|_| SendError::Other)?;
    *req.headers_mut() = headers;
    let start = std::time::Instant::now();
    let result = tokio::time::timeout(
        target.timeout,
        runtime
            .client
            .request(req)
            .instrument(tracing::info_span!("mcp_upstream", server = %target.name)),
    )
    .await;
    runtime
        .metrics
        .mcp_upstream_duration(&target.name)
        .observe(start.elapsed().as_secs_f64());
    result
        .map_err(|_| SendError::UpstreamTimeout)?
        .map_err(|_| SendError::Other)
}
pub async fn json(response: Response<Incoming>) -> Result<Value, ()> {
    if !response.status().is_success() {
        return Err(());
    }
    let sse = is_sse(response.headers());
    let bytes = collect(response.into_body(), RESPONSE_LIMIT).await?;
    if !sse {
        return serde_json::from_slice(&bytes).map_err(|_| ());
    }
    let text = std::str::from_utf8(&bytes).map_err(|_| ())?;
    for event in text.replace("\r\n", "\n").split("\n\n") {
        if event.len() > EVENT_LIMIT {
            return Err(());
        }
        let data = event
            .lines()
            .filter_map(|l| {
                l.strip_prefix("data:")
                    .map(|s| s.strip_prefix(' ').unwrap_or(s))
            })
            .collect::<Vec<_>>()
            .join("\n");
        if data.is_empty() {
            continue;
        }
        let value: Value = serde_json::from_str(&data).map_err(|_| ())?;
        if value.get("result").is_some() || value.get("error").is_some() {
            return Ok(value);
        }
    }
    Err(())
}
pub fn is_sse(headers: &HeaderMap) -> bool {
    headers
        .get("content-type")
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v.starts_with("text/event-stream"))
}
fn rewrite_event(event: &[u8], server: Option<&str>) -> Result<Bytes, ()> {
    if event.len() > EVENT_LIMIT {
        return Err(());
    }
    let Some(server) = server else {
        return Ok(Bytes::copy_from_slice(event));
    };
    let text = std::str::from_utf8(event)
        .map_err(|_| ())?
        .replace("\r\n", "\n");
    let data = text
        .lines()
        .filter_map(|l| l.strip_prefix("data:").map(str::trim_start))
        .collect::<Vec<_>>()
        .join("\n");
    if data.is_empty() {
        // Aggregate GET is deliberately unsupported: do not expose a cursor
        // which the client cannot use to resume a POST stream.
        return Ok(Bytes::from_static(b"data:\n\n"));
    }
    let mut value: Value = serde_json::from_str(&data).map_err(|_| ())?;
    if value.get("method").is_some() && value.get("id").is_some() {
        value["id"] = Value::String(format!("lw:{server}:{}", value["id"]));
        if let Some(token) = value.pointer_mut("/params/_meta/progressToken") {
            *token = Value::String(format!("lw:{server}:{token}"));
        }
    }
    if value["method"] == "notifications/cancelled" {
        if let Some(id) = value.pointer_mut("/params/requestId") {
            *id = Value::String(format!("lw:{server}:{id}"));
        }
    }
    // No aggregate GET/resumption: do not advertise upstream event cursors.
    let mut out = String::new();
    for line in text.lines() {
        if !line.starts_with("data:")
            && !line.starts_with("id:")
            && !line.starts_with("retry:")
            && !line.is_empty()
        {
            out.push_str(line);
            out.push('\n');
        }
    }
    out.push_str("data: ");
    out.push_str(&value.to_string());
    out.push_str("\n\n");
    Ok(Bytes::from(out))
}
/// Streams an upstream response while rewriting aggregate namespaces.
///
/// `usage` is the [`crate::usage::TrafficRecorder`] of a streaming RPC: the
/// stream's outcome is only known here, so the pump finishes it — `Completed`
/// when the upstream ended the stream, `UpstreamError` when it broke, and the
/// recorder's own `Drop` when the client disappears (`tx.send` fails or the
/// receiver is dropped).
pub fn relay(
    response: Response<Incoming>,
    server: Option<String>,
    usage: Option<crate::usage::TrafficRecorder>,
) -> Response<ProxyBody> {
    use lumewarp_proto::usage::v1::RequestOutcome;
    let sse = is_sse(response.headers());
    let (mut parts, mut body) = response.into_parts();
    response_headers(&mut parts.headers);
    let (tx, rx) = tokio::sync::mpsc::channel(2);
    tokio::spawn(async move {
        let mut usage = usage;
        let mut outcome = RequestOutcome::Completed;
        let mut held = BytesMut::new();
        let mut total = 0usize;
        loop {
            let frame = tokio::select! {
                // The client is gone: `usage` drops unfinished, which records
                // the cancellation.
                _ = tx.closed() => return,
                frame = body.frame() => match frame { Some(frame) => frame, None => break },
            };
            let data = match frame {
                Ok(frame) => match frame.into_data() {
                    Ok(d) => d,
                    Err(_) => continue,
                },
                Err(_) => {
                    // A broken upstream body is a mid-stream failure.
                    outcome = RequestOutcome::UpstreamError;
                    break;
                }
            };
            if !sse {
                total = total.saturating_add(data.len());
                if total > RESPONSE_LIMIT {
                    let _ = tx
                        .send(Err(std::io::Error::other("MCP response limit")))
                        .await;
                    finish(&mut usage, RequestOutcome::UpstreamError);
                    return;
                }
                if tx.send(Ok(Frame::data(data))).await.is_err() {
                    return;
                }
                continue;
            }
            for byte in data {
                held.extend_from_slice(&[byte]);
                if held.len() > EVENT_LIMIT {
                    let _ = tx.send(Err(std::io::Error::other("MCP event limit"))).await;
                    finish(&mut usage, RequestOutcome::UpstreamError);
                    return;
                }
                if held.ends_with(b"\n\n") || held.ends_with(b"\r\n\r\n") {
                    match rewrite_event(&held.split().freeze(), server.as_deref()) {
                        Ok(out) => {
                            if tx.send(Ok(Frame::data(out))).await.is_err() {
                                return;
                            }
                        }
                        Err(()) => {
                            let _ = tx
                                .send(Err(std::io::Error::other("Invalid MCP event")))
                                .await;
                            finish(&mut usage, RequestOutcome::UpstreamError);
                            return;
                        }
                    }
                }
            }
        }
        finish(&mut usage, outcome);
    });
    Response::from_parts(
        parts,
        StreamBody::new(ReceiverStream::new(rx))
            .map_err(|e: std::io::Error| -> proxy::BoxError { Box::new(e) })
            .boxed_unsync(),
    )
}
/// Records the stream's outcome once; a later `Drop` of the recorder is a no-op.
fn finish(
    usage: &mut Option<crate::usage::TrafficRecorder>,
    outcome: lumewarp_proto::usage::v1::RequestOutcome,
) {
    if let Some(recorder) = usage.as_mut() {
        recorder.finish(outcome);
    }
}

pub fn response_headers(headers: &mut HeaderMap) {
    if let Some(connection) = headers
        .get("connection")
        .and_then(|h| h.to_str().ok())
        .map(str::to_owned)
    {
        for name in connection.split(',') {
            headers.remove(name.trim());
        }
    }
    for name in [
        "content-length",
        "transfer-encoding",
        "connection",
        "mcp-session-id",
        "set-cookie",
        "upgrade",
        "keep-alive",
        "te",
        "trailer",
        "proxy-connection",
    ] {
        headers.remove(name);
    }
}
pub fn reply(status: StatusCode, value: Value) -> Response<ProxyBody> {
    let mut response = Response::builder()
        .status(status)
        .header("content-type", "application/json")
        .body(proxy::full_body(Bytes::from(value.to_string())))
        .unwrap();
    if value.get("error").is_some()
        || value.pointer("/result/isError").and_then(Value::as_bool) == Some(true)
    {
        response
            .extensions_mut()
            .insert(lumewarp_proto::usage::v1::RequestOutcome::UpstreamError);
    }
    response
}
pub fn empty(status: StatusCode) -> Response<ProxyBody> {
    Response::builder()
        .status(status)
        .body(proxy::full_body(Bytes::new()))
        .unwrap()
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn reverse_ids_and_priming() {
        let bytes = rewrite_event(b"event: message\ndata: {\"jsonrpc\":\"2.0\",\"method\":\"elicitation/create\",\"id\":17}\n\n", Some("s1")).unwrap();
        assert!(std::str::from_utf8(&bytes).unwrap().contains("lw:s1:17"));
        assert_eq!(
            rewrite_event(b"id: 1\ndata:\n\n", Some("s1")).unwrap(),
            Bytes::from_static(b"data:\n\n")
        );
        assert!(rewrite_event(&vec![b'x'; EVENT_LIMIT + 1], None).is_err());
    }
}
