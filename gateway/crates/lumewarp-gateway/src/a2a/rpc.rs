//! The A2A JSON-RPC proxy: one request from the client to an upstream agent,
//! with the pipeline of plan L5.
//!
//! ```text
//! key auth (server.rs) → agent grant → parse + A2A-Version 1.0
//!   → push-notification refusal (D12) → unwrap task/context ids (D17)
//!   → request hook → re-check the hook's output
//!   → strip client credentials + outbound auth (upstream_auth)
//!   → upstream
//!   → response hook → card rewrite (GetExtendedAgentCard) → wrap ids
//! ```
//!
//! Every id-bearing field is wrapped with the caller's key, whether it is the
//! JSON-RPC response of a blocking call or one SSE event: the stream parser is
//! incremental (multi-line `data:` frames, one event at a time, at most
//! [`MAX_STREAM_EVENT`] bytes) and never collects the stream. A hook error or a
//! refused hook output in a stream is the last event, then the stream closes.
//! Client disconnect drops the upstream body, which cancels the upstream call.
//!
//! Only Push-notification methods and a `SendMessage` carrying
//! `configuration.taskPushNotificationConfig` are refused locally (`-32003`);
//! both are checked again after the request hook. `ListTasks` is only forwarded
//! when the agent's outbound auth is `passthrough` (D18); other agents answer
//! `UnsupportedOperationError`, because their upstream cannot prove ownership.

use std::{
    sync::Arc,
    time::{Duration, Instant},
};

use bytes::Bytes;
use http_body_util::{BodyExt, Limited, StreamBody};
use hyper::{
    body::{Frame, Incoming},
    header::{self, HeaderMap, HeaderName, HeaderValue},
    Method, Request, Response, StatusCode,
};
use lumewarp_plugin_host::a2a_middleware::{self as hook, Middleware};
use lumewarp_proto::usage::v1::{RequestOutcome, TrafficKind};
use serde_json::{json, Value};
use tokio_stream::wrappers::ReceiverStream;

use super::{card, ids, record_usage, Call};
use crate::usage::{TrafficEvent, TrafficRecorder};
use crate::{
    a2a_ids::IdKey,
    auth::Caller,
    metrics::{status_index, Metrics},
    proxy::{full_body, BoxError, ProxyBody},
    router::KeyContext,
    runtime::Runtime,
    targets::{self, A2aTarget, Auth},
    upstream_auth,
    usage::RequestCtx,
};

/// Largest JSON-RPC request body (plan L10).
pub const MAX_REQUEST_BODY: usize = 4 * 1024 * 1024;
/// Largest non-streaming response body (plan L10).
pub const MAX_RESPONSE_BODY: usize = 16 * 1024 * 1024;
/// Largest single SSE event (plan L10).
pub const MAX_STREAM_EVENT: usize = 1024 * 1024;
/// Error message used by both the blocking and the streaming path.
const BAD_AGENT_RESPONSE: &str = "The agent returned an invalid response";

/// The A2A v1.0 JSON-RPC methods (`ref/specs.md` §2.2). Anything else is
/// refused with `-32601` and never forwarded: id unwrapping, the D12 push
/// refusal and the D18 `ListTasks` gate key off these exact names, so an
/// unknown method would bypass all three (and the agent's response would be
/// wrapped as the caller's).
pub const METHODS: [&str; 11] = [
    "SendMessage",
    "SendStreamingMessage",
    "GetTask",
    "ListTasks",
    "CancelTask",
    "SubscribeToTask",
    "CreateTaskPushNotificationConfig",
    "GetTaskPushNotificationConfig",
    "ListTaskPushNotificationConfigs",
    "DeleteTaskPushNotificationConfig",
    "GetExtendedAgentCard",
];

/// Whether `method` is one of the v1.0 methods.
pub fn known_method(method: &str) -> bool {
    METHODS.contains(&method)
}

/// Whether a method's result is an SSE stream. Only these two may answer with
/// `text/event-stream`; a card or task result that arrives that way would skip
/// the non-stream response path (including the L6 card rewrite).
pub fn method_streams(method: &str) -> bool {
    matches!(method, "SendStreamingMessage" | "SubscribeToTask")
}

/// The metrics method label of one v1.0 method (`metrics::A2A_METHODS`).
pub fn method_label(method: &str) -> &'static str {
    match method {
        "SendMessage" => "message_send",
        "SendStreamingMessage" => "message_stream",
        "GetTask" => "tasks_get",
        "CancelTask" => "tasks_cancel",
        "SubscribeToTask" => "tasks_resubscribe",
        _ => "other",
    }
}

/// One finished JSON-RPC request, before metrics and the usage event are
/// written.
struct Reply {
    response: Response<ProxyBody>,
    /// `metrics::A2A_METHODS` label.
    method: &'static str,
    /// The v1.0 method for the usage event ("" when the body was unparseable).
    method_name: String,
    stream: bool,
    outcome: RequestOutcome,
}

impl Reply {
    #[allow(clippy::too_many_arguments)]
    fn json_rpc_error(
        status: StatusCode,
        id: Option<&Value>,
        code: i32,
        message: &str,
        reason: &'static str,
        method: &'static str,
        method_name: String,
        outcome: RequestOutcome,
    ) -> Self {
        let body = error_value(id, code, message, reason, None);
        Self {
            response: Response::builder()
                .status(status)
                .header(header::CONTENT_TYPE, "application/json")
                .body(full_body(Bytes::from(
                    serde_json::to_vec(&body).expect("JSON values serialize"),
                )))
                .expect("static response parts are valid"),
            method,
            method_name,
            stream: false,
            outcome,
        }
    }

    /// A protocol-level error that A2A renders in a 200 JSON-RPC envelope.
    fn protocol_error(
        id: Option<&Value>,
        code: i32,
        message: &str,
        reason: &'static str,
        method: &'static str,
        method_name: String,
        outcome: RequestOutcome,
    ) -> Self {
        Self::json_rpc_error(
            StatusCode::OK,
            id,
            code,
            message,
            reason,
            method,
            method_name,
            outcome,
        )
    }
}

fn error_value(
    id: Option<&Value>,
    code: i32,
    message: &str,
    reason: &'static str,
    metadata: Option<Value>,
) -> Value {
    let mut info = json!({
        "@type": "type.googleapis.com/google.rpc.ErrorInfo",
        "reason": reason,
        "domain": "a2a-protocol.org",
    });
    if let Some(metadata) = metadata {
        info["metadata"] = metadata;
    }
    json!({
        "jsonrpc": "2.0",
        "id": id.cloned().unwrap_or(Value::Null),
        "error": {"code": code, "message": message, "data": [info]},
    })
}

/// Serves one `/a2a/{agent}` request.
pub async fn serve(call: Call<'_>, agent: &str) -> Response<ProxyBody> {
    let Call {
        route: _,
        method,
        http,
        caller,
        routing_table,
        targets,
        runtime,
        request,
        body_read_timeout,
    } = call;
    let Caller::Key(key) = caller else {
        unreachable!("A2A requests are authenticated")
    };
    let reply = run(
        key,
        runtime,
        request,
        routing_table.version,
        method,
        http,
        body_read_timeout,
        targets,
        agent,
    )
    .await;
    let status = reply.response.status().as_u16();
    // The agent label must be a snapshot name, never client input: an unknown
    // or ungranted name creates no metric series and no usage target.
    if targets
        .agent(agent)
        .is_some_and(|target| key.grants.may_call_agent(&target.name))
    {
        runtime.metrics.a2a_request_handles(agent, reply.method)[status_index(status)].inc();
        // A stream reports itself when it ends (`TrafficRecorder` in the pump):
        // its outcome is not known at handoff.
        if !reply.stream {
            record_usage(
                runtime,
                routing_table,
                request,
                &Caller::Key(Arc::clone(key)),
                agent,
                &reply.method_name,
                status,
                false,
                reply.outcome,
            );
        }
    }
    reply.response
}

#[allow(clippy::too_many_arguments)]
async fn run(
    key: &Arc<KeyContext>,
    runtime: &Runtime,
    usage: &RequestCtx,
    config_version: u64,
    method: Method,
    http: Request<Incoming>,
    body_read_timeout: Duration,
    targets: &targets::Targets,
    agent: &str,
) -> Reply {
    let Some(target) = targets.agent(agent) else {
        return Reply::json_rpc_error(
            StatusCode::NOT_FOUND,
            None,
            -32600,
            "Unknown agent",
            "UNKNOWN_AGENT",
            "other",
            String::new(),
            RequestOutcome::Rejected,
        );
    };
    // An agent the key may not call must look exactly like one that does not
    // exist (A2A authorization errors must not reveal resources).
    if !key.grants.may_call_agent(&target.name) {
        return Reply::json_rpc_error(
            StatusCode::NOT_FOUND,
            None,
            -32600,
            "Unknown agent",
            "UNKNOWN_AGENT",
            "other",
            String::new(),
            RequestOutcome::Rejected,
        );
    }
    let target = Arc::clone(target);
    if method != Method::POST {
        let mut reply = Reply::json_rpc_error(
            StatusCode::METHOD_NOT_ALLOWED,
            None,
            -32600,
            "Method not allowed",
            "METHOD_NOT_ALLOWED",
            "other",
            String::new(),
            RequestOutcome::Rejected,
        );
        reply
            .response
            .headers_mut()
            .insert(header::ALLOW, HeaderValue::from_static("POST"));
        return reply;
    }
    let (parts, incoming) = http.into_parts();
    let body = match read_body(incoming, body_read_timeout).await {
        Ok(body) => body,
        Err(reply) => return reply,
    };
    let Some(mut request) = parse_request(&body) else {
        return Reply::protocol_error(
            None,
            -32700,
            "Invalid JSON payload",
            "JSON_PARSE_ERROR",
            "other",
            String::new(),
            RequestOutcome::Rejected,
        );
    };
    let id = request.get("id").cloned();
    let Some(rpc_method) = request
        .get("method")
        .and_then(Value::as_str)
        .map(str::to_owned)
    else {
        return malformed(&id, "missing method");
    };
    let label = method_label(&rpc_method);
    // Plan L5: an A2A-Version other than 1.0, or none at all, is -32009. The
    // spec reads an absent header as 0.3; this gateway does not implement 0.3.
    if !version_supported(&parts.headers, parts.uri.query()) {
        return Reply::protocol_error(
            id.as_ref(),
            -32009,
            "A2A version is not supported",
            "VERSION_NOT_SUPPORTED",
            label,
            rpc_method,
            RequestOutcome::Rejected,
        );
    }
    // B2: only the v1.0 methods are forwarded. Unknown names would skip id
    // unwrapping, the push refusal and the ListTasks gate, so they are refused
    // here — before the upstream span is created with the client's string.
    if !known_method(&rpc_method) {
        return Reply::protocol_error(
            id.as_ref(),
            -32601,
            "Method not found",
            "METHOD_NOT_FOUND",
            "other",
            rpc_method,
            RequestOutcome::Rejected,
        );
    }
    if let Some(params) = request.get("params") {
        if !params.is_object() {
            return Reply::protocol_error(
                id.as_ref(),
                -32602,
                "Invalid parameters",
                "INVALID_PARAMS",
                label,
                rpc_method,
                RequestOutcome::Rejected,
            );
        }
    }
    // N2: ProtoJSON parsers accept the original snake_case field names as
    // aliases of the lowerCamelCase JSON names. The gateway only knows the
    // canonical names, so an alias would bypass id unwrapping, the D12 refusal
    // and the response wrapping; refuse it as invalid params.
    if ids::request_snake_alias(&request).is_some() {
        return Reply::protocol_error(
            id.as_ref(),
            -32602,
            "Invalid parameters: ProtoJSON snake_case aliases are not supported",
            "INVALID_PARAMS",
            label,
            rpc_method,
            RequestOutcome::Rejected,
        );
    }
    // D12: push notifications are not supported, in any entry point.
    if ids::push_config_present(&rpc_method, request.get("params")) {
        return Reply::protocol_error(
            id.as_ref(),
            -32003,
            "Push notifications are not supported",
            "PUSH_NOTIFICATION_NOT_SUPPORTED",
            label,
            rpc_method,
            RequestOutcome::Rejected,
        );
    }
    // D18: ListTasks is only meaningful when the upstream isolates callers with
    // the user's own token.
    if rpc_method == "ListTasks" && !matches!(target.auth, Auth::Passthrough) {
        return Reply::protocol_error(
            id.as_ref(),
            -32004,
            "ListTasks is not supported for this agent",
            "UNSUPPORTED_OPERATION",
            label,
            rpc_method,
            RequestOutcome::Rejected,
        );
    }
    let ctx = ids::Context {
        key: runtime.id_key.as_ref().expect("checked by handle"),
        agent: &target.name,
        owner: &key.key_id,
    };
    if ids::unwrap_request(&rpc_method, &mut request, &ctx).is_err() {
        // Foreign owner and "not minted by us" look the same: never reveal
        // whether an id exists (L5, D17).
        return Reply::protocol_error(
            id.as_ref(),
            ids::TASK_NOT_FOUND,
            "Task not found",
            "TASK_NOT_FOUND",
            label,
            rpc_method,
            RequestOutcome::Rejected,
        );
    }
    let middleware = match configured_middleware(&target).await {
        Ok(middleware) => middleware,
        Err(reply) => return reply,
    };
    let processor = Processor {
        method: &rpc_method,
        agent: &target.name,
        key: ctx.key,
        owner: &key.key_id,
        middleware: middleware.as_ref(),
        public_url: runtime.public_url.as_deref().expect("checked by handle"),
        timeout: middleware_timeout(target.middleware.as_ref()),
    };
    let request = match processor.on_request(request).await {
        Ok(request) => request,
        Err(failure) => return failure.into_reply(id.as_ref(), label, rpc_method),
    };
    let body = serde_json::to_vec(&request).expect("JSON values serialize");
    let mut headers = upstream_headers(&parts.headers);
    if let Err(failure) =
        upstream_auth::authorize_a2a(&target, &target.url, &Method::POST, &body, &mut headers).await
    {
        tracing::warn!(
            agent = %target.name,
            failure = failure.label(),
            "A2A outbound authentication failed"
        );
        return Reply::json_rpc_error(
            auth_failure_status(failure),
            id.as_ref(),
            -32603,
            "Upstream authentication failed",
            "UPSTREAM_AUTH_FAILED",
            label,
            rpc_method,
            RequestOutcome::UpstreamError,
        );
    }
    let upstream_request = match Request::builder()
        .method(Method::POST)
        .uri(target.url.clone())
        .body(crate::request_body::RequestBody::from(Bytes::from(body)))
    {
        Ok(mut upstream_request) => {
            *upstream_request.headers_mut() = headers;
            upstream_request
        }
        Err(_) => {
            return Reply::json_rpc_error(
                StatusCode::BAD_GATEWAY,
                id.as_ref(),
                -32603,
                "Could not build the upstream request",
                "UPSTREAM_REQUEST_FAILED",
                label,
                rpc_method,
                RequestOutcome::UpstreamError,
            )
        }
    };
    let started = Instant::now();
    let upstream = tracing::info_span!("a2a_upstream", agent = %target.name, method = %rpc_method);
    let response = match tokio::time::timeout(
        target.timeout,
        tracing::Instrument::instrument(runtime.client.request(upstream_request), upstream),
    )
    .await
    {
        Err(_) => {
            return Reply::json_rpc_error(
                StatusCode::GATEWAY_TIMEOUT,
                id.as_ref(),
                -32603,
                "The agent did not answer in time",
                "UPSTREAM_TIMEOUT",
                label,
                rpc_method,
                RequestOutcome::UpstreamError,
            )
        }
        Ok(Err(_)) => {
            return Reply::json_rpc_error(
                StatusCode::BAD_GATEWAY,
                id.as_ref(),
                -32603,
                "The agent could not be reached",
                "UPSTREAM_UNAVAILABLE",
                label,
                rpc_method,
                RequestOutcome::UpstreamError,
            )
        }
        Ok(Ok(response)) => response,
    };
    let content_type = response
        .headers()
        .get(header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .map(|value| value.split(';').next().unwrap_or("").trim().to_owned());
    if response.status() == StatusCode::OK && content_type.as_deref() == Some("text/event-stream") {
        if !method_streams(&rpc_method) {
            // A non-streaming method that answers SSE would skip the response
            // hook and, for GetExtendedAgentCard, the card rewrite (m5).
            return Reply::protocol_error(
                id.as_ref(),
                -32006,
                BAD_AGENT_RESPONSE,
                "INVALID_AGENT_RESPONSE",
                label,
                rpc_method_name(&request),
                RequestOutcome::UpstreamError,
            );
        }
        return start_stream(
            runtime,
            &target,
            response,
            processor,
            id,
            label,
            rpc_method_name(&request),
            started,
            usage,
            key,
            config_version,
        );
    }
    let status = response.status();
    let content_type = response.headers().get(header::CONTENT_TYPE).cloned();
    let body = match tokio::time::timeout(
        target.timeout,
        Limited::new(response.into_body(), MAX_RESPONSE_BODY).collect(),
    )
    .await
    {
        Err(_) => {
            return Reply::json_rpc_error(
                StatusCode::GATEWAY_TIMEOUT,
                id.as_ref(),
                -32603,
                "The agent did not finish its response in time",
                "UPSTREAM_TIMEOUT",
                label,
                rpc_method,
                RequestOutcome::UpstreamError,
            )
        }
        Ok(Err(error)) if error.is::<http_body_util::LengthLimitError>() => {
            return Reply::json_rpc_error(
                StatusCode::BAD_GATEWAY,
                id.as_ref(),
                -32603,
                "The agent response is too large",
                "UPSTREAM_RESPONSE_TOO_LARGE",
                label,
                rpc_method,
                RequestOutcome::UpstreamError,
            )
        }
        Ok(Err(_)) => {
            return Reply::json_rpc_error(
                StatusCode::BAD_GATEWAY,
                id.as_ref(),
                -32603,
                "Could not read the agent response",
                "UPSTREAM_READ_FAILED",
                label,
                rpc_method,
                RequestOutcome::UpstreamError,
            )
        }
        Ok(Ok(body)) => body.to_bytes(),
    };
    runtime
        .metrics
        .a2a_upstream_duration(&target.name)
        .observe(started.elapsed().as_secs_f64());
    if status != StatusCode::OK {
        // A transport-level upstream error is forwarded as it is; it carries no
        // client ids the gateway owns.
        let mut response = Response::new(full_body(body));
        *response.status_mut() = status;
        if let Some(content_type) = content_type {
            response
                .headers_mut()
                .insert(header::CONTENT_TYPE, content_type);
        }
        return Reply {
            response,
            method: label,
            method_name: rpc_method,
            stream: false,
            outcome: RequestOutcome::UpstreamError,
        };
    }
    let Some(mut value) = serde_json::from_slice::<Value>(&body).ok() else {
        return Reply::protocol_error(
            id.as_ref(),
            -32006,
            BAD_AGENT_RESPONSE,
            "INVALID_AGENT_RESPONSE",
            label,
            rpc_method,
            RequestOutcome::UpstreamError,
        );
    };
    let method_name = rpc_method_name(&request);
    if !value.is_object() {
        return Reply::protocol_error(
            id.as_ref(),
            -32006,
            BAD_AGENT_RESPONSE,
            "INVALID_AGENT_RESPONSE",
            label,
            method_name,
            RequestOutcome::UpstreamError,
        );
    }
    let outcome = if value.get("error").is_some() {
        RequestOutcome::UpstreamError
    } else {
        RequestOutcome::Completed
    };
    if let Err(failure) = processor.on_response(&mut value).await {
        return failure.into_reply(id.as_ref(), label, method_name);
    }
    // The extended card is an AgentCard in the result and gets the same rewrite
    // as the public card; a response hook must not be able to undo it.
    if rpc_method == "GetExtendedAgentCard" {
        if let Some(result) = value.get_mut("result") {
            if result.is_object() {
                card::rewrite(result, &target.name, processor.public_url);
            }
        }
    }
    if ids::wrap_response(&rpc_method, &mut value, &processor.context()).is_err() {
        return Reply::protocol_error(
            id.as_ref(),
            -32006,
            BAD_AGENT_RESPONSE,
            "INVALID_AGENT_RESPONSE",
            label,
            method_name,
            RequestOutcome::UpstreamError,
        );
    }
    Reply {
        response: Response::builder()
            .status(StatusCode::OK)
            .header(header::CONTENT_TYPE, "application/json")
            .body(full_body(Bytes::from(
                serde_json::to_vec(&value).expect("JSON values serialize"),
            )))
            .expect("static response parts are valid"),
        method: label,
        method_name,
        stream: false,
        outcome,
    }
}

fn malformed(id: &Option<Value>, _detail: &str) -> Reply {
    Reply::protocol_error(
        id.as_ref(),
        -32600,
        "Request payload validation error",
        "INVALID_REQUEST",
        "other",
        String::new(),
        RequestOutcome::Rejected,
    )
}

/// Parses a JSON-RPC request object and checks the envelope. The method is
/// read separately so an early error can carry no client text.
fn parse_request(body: &[u8]) -> Option<Value> {
    let value: Value = serde_json::from_slice(body).ok()?;
    let object = value.as_object()?;
    if object.get("jsonrpc").and_then(Value::as_str) != Some("2.0") {
        return None;
    }
    // A notification (no id) is not a request this proxy can answer.
    object.get("id")?;
    value.is_object().then_some(value)
}

/// `A2A-Version` must be exactly `1.0`, in the header or the permitted query
/// parameter. Absent or empty means 0.3, which this gateway does not speak.
fn version_supported(headers: &HeaderMap, query: Option<&str>) -> bool {
    let header = headers
        .get("a2a-version")
        .and_then(|value| value.to_str().ok());
    let query = query.and_then(|query| {
        query.split('&').find_map(|pair| {
            let (key, value) = pair.split_once('=')?;
            (key == "A2A-Version").then_some(value)
        })
    });
    match header.or(query) {
        Some(version) => version.trim() == "1.0",
        None => false,
    }
}

fn rpc_method_name(request: &Value) -> String {
    request
        .get("method")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_owned()
}

#[allow(clippy::result_large_err)]
async fn read_body(incoming: Incoming, timeout: Duration) -> Result<Bytes, Reply> {
    let too_large = || {
        Reply::json_rpc_error(
            StatusCode::PAYLOAD_TOO_LARGE,
            None,
            -32600,
            "Request body exceeds 4 MiB",
            "REQUEST_TOO_LARGE",
            "other",
            String::new(),
            RequestOutcome::Rejected,
        )
    };
    match tokio::time::timeout(timeout, Limited::new(incoming, MAX_REQUEST_BODY).collect()).await {
        Err(_) => Err(Reply::json_rpc_error(
            StatusCode::REQUEST_TIMEOUT,
            None,
            -32600,
            "Request body read timed out",
            "REQUEST_TIMEOUT",
            "other",
            String::new(),
            RequestOutcome::Rejected,
        )),
        Ok(Err(error)) if error.is::<http_body_util::LengthLimitError>() => Err(too_large()),
        Ok(Err(_)) => Err(Reply::json_rpc_error(
            StatusCode::BAD_REQUEST,
            None,
            -32600,
            "Could not read the request body",
            "INVALID_REQUEST",
            "other",
            String::new(),
            RequestOutcome::Rejected,
        )),
        Ok(Ok(body)) => Ok(body.to_bytes()),
    }
}

/// The headers sent upstream: the client's headers minus framing, client
/// credentials and the headers the gateway owns. `upstream_auth` strips the
/// credential set itself; this copy keeps `Authorization` for `passthrough`.
fn upstream_headers(client: &HeaderMap) -> HeaderMap {
    // Connection-nominated header names are hop-by-hop for this request.
    let connection_named: Vec<HeaderName> = client
        .get(header::CONNECTION)
        .and_then(|value| value.to_str().ok())
        .map(|value| {
            value
                .split(',')
                .filter_map(|name| HeaderName::from_bytes(name.trim().as_bytes()).ok())
                .collect()
        })
        .unwrap_or_default();
    let mut headers = HeaderMap::new();
    for (name, value) in client {
        if matches!(
            name.as_str(),
            "host"
                | "content-length"
                | "transfer-encoding"
                | "connection"
                | "te"
                | "trailer"
                | "upgrade"
                | "keep-alive"
                | "proxy-connection"
                | "cookie"
                | "proxy-authorization"
                | "x-lumewarp-key"
                | "a2a-version"
                | "accept-encoding"
        ) || connection_named.contains(name)
        {
            continue;
        }
        headers.append(name.clone(), value.clone());
    }
    headers.insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("application/json"),
    );
    headers.insert("a2a-version", HeaderValue::from_static("1.0"));
    headers
}

/// The plugin call budget: `PluginRef.timeout_ms`, with 0 meaning the 60 s
/// default (`targets::compile_plugin` already applied it).
fn middleware_timeout(plugin: Option<&targets::Plugin>) -> Duration {
    plugin.map_or(hook::DEFAULT_TIMEOUT, |plugin| plugin.timeout)
}

/// Outbound-auth failures map like the MCP path: a plugin timeout is a gateway
/// timeout, every other failure is a bad gateway.
fn auth_failure_status(failure: upstream_auth::Failure) -> StatusCode {
    match failure {
        upstream_auth::Failure::Timeout => StatusCode::GATEWAY_TIMEOUT,
        upstream_auth::Failure::Configure
        | upstream_auth::Failure::Plugin
        | upstream_auth::Failure::Trap
        | upstream_auth::Failure::Header => StatusCode::BAD_GATEWAY,
    }
}

/// Finds (or registers) the agent's middleware. Only metadata is built here;
/// the guest `configure` is awaited by the first hook.
#[allow(clippy::result_large_err)]
async fn configured_middleware(target: &A2aTarget) -> Result<Option<Arc<Middleware>>, Reply> {
    let Some(plugin) = &target.middleware else {
        return Ok(None);
    };
    let spec = hook::Spec {
        id: target.id.to_string(),
        name: target.name.to_string(),
        url: target.url.to_string(),
        plugin: plugin.name.to_string(),
        params_json: plugin.params_json.to_string(),
        digest: plugin.digest,
    };
    if let Some(configured) = hook::get(&spec.id) {
        if configured.matches(&spec) {
            return Ok(Some(configured));
        }
    }
    match hook::global().get_or_configure(&spec) {
        Ok(middleware) => Ok(Some(middleware)),
        Err(_) => {
            tracing::error!(agent = %target.name, "A2A middleware could not be registered");
            Err(Reply::json_rpc_error(
                StatusCode::BAD_GATEWAY,
                None,
                -32603,
                "Middleware is not available",
                "MIDDLEWARE_UNAVAILABLE",
                "other",
                String::new(),
                RequestOutcome::UpstreamError,
            ))
        }
    }
}

/// A failed pipeline step. `status` is the HTTP status the client gets; a
/// protocol error after an upstream answer stays 200.
struct Failure {
    status: StatusCode,
    code: i32,
    message: String,
    reason: &'static str,
}

impl Failure {
    fn refused(message: &'static str) -> Self {
        Self {
            status: StatusCode::BAD_GATEWAY,
            code: -32603,
            message: message.to_owned(),
            reason: "MIDDLEWARE_REFUSED",
        }
    }

    /// A hook returned a ProtoJSON snake_case alias: a client-visible invalid
    /// parameters error (N2), not a host failure.
    fn invalid_params() -> Self {
        Self {
            status: StatusCode::OK,
            code: -32602,
            message: "Invalid parameters: ProtoJSON snake_case aliases are not supported"
                .to_owned(),
            reason: "INVALID_PARAMS",
        }
    }

    fn from_hook(error: hook::Error) -> Self {
        match error {
            // A deliberate guest `rpc-error` is a client-facing JSON-RPC error
            // (HTTP 200), at any stage.
            hook::Error::Rpc { code, message } => Self {
                status: StatusCode::OK,
                code,
                message,
                reason: "MIDDLEWARE_ERROR",
            },
            hook::Error::Timeout => Self {
                status: StatusCode::GATEWAY_TIMEOUT,
                code: -32603,
                message: "Middleware timed out".to_owned(),
                reason: "MIDDLEWARE_TIMEOUT",
            },
            hook::Error::TooLarge => Self {
                status: StatusCode::BAD_GATEWAY,
                code: -32603,
                message: "Middleware output is too large".to_owned(),
                reason: "MIDDLEWARE_TOO_LARGE",
            },
            _ => Self {
                status: StatusCode::BAD_GATEWAY,
                code: -32603,
                message: "Middleware failed".to_owned(),
                reason: "MIDDLEWARE_FAILED",
            },
        }
    }

    fn into_reply(self, id: Option<&Value>, method: &'static str, method_name: String) -> Reply {
        Reply::json_rpc_error(
            self.status,
            id,
            self.code,
            &self.message,
            self.reason,
            method,
            method_name,
            RequestOutcome::UpstreamError,
        )
    }
}

/// The request/response hooks plus id wrapping for one RPC.
struct Processor<'a> {
    method: &'a str,
    agent: &'a str,
    key: &'a IdKey,
    owner: &'a str,
    middleware: Option<&'a Arc<Middleware>>,
    public_url: &'a str,
    /// `PluginRef.timeout_ms`; 0 means the 60 s default.
    timeout: Duration,
}

impl<'a> Processor<'a> {
    fn context(&self) -> ids::Context<'a> {
        ids::Context {
            key: self.key,
            agent: self.agent,
            owner: self.owner,
        }
    }

    /// The request hook. Its output must keep the method, the JSON-RPC id and
    /// every id-bearing field exactly, and must not add push configuration
    /// (plan L5).
    async fn on_request(&self, request: Value) -> Result<Value, Failure> {
        let Some(middleware) = self.middleware else {
            return Ok(request);
        };
        let mut before = ids::request_fields(self.method, &request);
        before.sort();
        let body = serde_json::to_vec(&request).expect("JSON values serialize");
        let output = match middleware
            .on_request(self.method, &body, self.timeout)
            .await
        {
            Ok(output) => output,
            Err(error) => return Err(Failure::from_hook(error)),
        };
        if output.len() > MAX_REQUEST_BODY {
            return Err(Failure::refused(
                "Middleware returned a request that is too large",
            ));
        }
        let Ok(after) = serde_json::from_slice::<Value>(&output) else {
            return Err(Failure::refused("Middleware returned invalid JSON"));
        };
        if !after.is_object()
            || after.get("jsonrpc").and_then(Value::as_str) != Some("2.0")
            || after.get("method").and_then(Value::as_str) != Some(self.method)
        {
            return Err(Failure::refused("Middleware changed the request method"));
        }
        if ids::request_snake_alias(&after).is_some() {
            return Err(Failure::invalid_params());
        }
        let mut after_fields = ids::request_fields(self.method, &after);
        after_fields.sort();
        if after_fields != before {
            return Err(Failure::refused("Middleware changed a request id"));
        }
        if ids::push_config_present(self.method, after.get("params")) {
            return Err(Failure::refused(
                "Middleware added push notification configuration",
            ));
        }
        Ok(after)
    }

    /// The response hook. Its output may drop id-bearing fields but must not
    /// add or change one, and the JSON-RPC id must stay the client's.
    async fn on_response(&self, value: &mut Value) -> Result<(), Failure> {
        let Some(middleware) = self.middleware else {
            return Ok(());
        };
        let before = ids::response_fields(value);
        let body = serde_json::to_vec(value).expect("JSON values serialize");
        let output = match middleware
            .on_response(self.method, &body, self.timeout)
            .await
        {
            Ok(output) => output,
            Err(error) => return Err(Failure::from_hook(error)),
        };
        if output.len() > MAX_RESPONSE_BODY {
            return Err(Failure {
                status: StatusCode::OK,
                code: -32603,
                message: "Middleware returned a response that is too large".into(),
                reason: "MIDDLEWARE_TOO_LARGE",
            });
        }
        let Ok(after) = serde_json::from_slice::<Value>(&output) else {
            return Err(Failure::refused("Middleware returned invalid JSON"));
        };
        if !after.is_object() {
            return Err(Failure::refused(
                "Middleware returned a non-object response",
            ));
        }
        if ids::response_snake_alias(&after).is_some() {
            return Err(Failure::invalid_params());
        }
        check_response_unchanged(&before, &after)?;
        *value = after;
        Ok(())
    }
}

fn envelope_id(fields: &[(String, String)]) -> Option<&str> {
    fields
        .iter()
        .find(|(path, _)| path == "$id")
        .map(|(_, value)| value.as_str())
}

/// The response hook may remove id fields but never add, change or replace the
/// JSON-RPC id (an added raw id would otherwise be wrapped as the caller's).
fn check_response_unchanged(before: &[(String, String)], after: &Value) -> Result<(), Failure> {
    let fields = ids::response_fields(after);
    if fields.iter().any(|field| !before.contains(field)) {
        return Err(Failure::refused(
            "Middleware changed or added a response id",
        ));
    }
    if envelope_id(before) != envelope_id(&fields) {
        return Err(Failure::refused("Middleware changed the JSON-RPC id"));
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// Streaming
// ---------------------------------------------------------------------------

/// Owned form of one stream: the request task ends before the last event, so
/// everything the per-event processor needs is captured.
struct StreamState {
    key: IdKey,
    agent: Arc<str>,
    owner: Arc<str>,
    method: String,
    middleware: Option<Arc<Middleware>>,
    timeout: Duration,
    id: Value,
    metrics: Arc<Metrics>,
}

#[allow(clippy::too_many_arguments)]
fn start_stream(
    runtime: &Runtime,
    target: &A2aTarget,
    response: Response<Incoming>,
    processor: Processor<'_>,
    id: Option<Value>,
    label: &'static str,
    method_name: String,
    started: Instant,
    usage: &RequestCtx,
    caller_key: &Arc<KeyContext>,
    config_version: u64,
) -> Reply {
    let Some(id_key) = runtime.id_key.as_ref() else {
        unreachable!("checked by handle")
    };
    // The stream outlives this request's borrow of the runtime; the id key is
    // copied (32 bytes of key material) so every event can still be wrapped.
    let key = match IdKey::from_secret(id_key.secret().to_vec()) {
        Ok(key) => key,
        Err(_) => {
            return Reply::json_rpc_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                id.as_ref(),
                -32603,
                "The gateway id key is not configured",
                "A2A_NOT_CONFIGURED",
                label,
                method_name,
                RequestOutcome::UpstreamError,
            )
        }
    };
    // The stream's usage is only final when it ends, so the pump owns a
    // recorder from here: it records the real outcome and duration, and a
    // client disconnect on `Drop` (`CLIENT_CANCELLED`, 499).
    let caller = Caller::Key(Arc::clone(caller_key));
    let recorder = TrafficRecorder::new(
        runtime,
        &TrafficEvent {
            kind: TrafficKind::A2a,
            request: usage,
            caller: &caller,
            target: &target.name,
            method: &method_name,
            tool: "",
            status: StatusCode::OK.as_u16(),
            outcome: RequestOutcome::Completed,
            stream: true,
            config_version,
        },
    );
    let state = StreamState {
        key,
        agent: target.name.clone(),
        owner: Arc::from(processor.owner),
        method: processor.method.to_owned(),
        middleware: processor.middleware.cloned(),
        timeout: processor.timeout,
        id: id.unwrap_or(Value::Null),
        metrics: Arc::clone(&runtime.metrics),
    };
    let (tx, rx) = tokio::sync::mpsc::channel::<Result<Frame<Bytes>, std::io::Error>>(4);
    let (_parts, incoming) = response.into_parts();
    tokio::spawn(pump(incoming, tx, state, started, recorder));
    let body = StreamBody::new(ReceiverStream::new(rx))
        .map_err(|error: std::io::Error| -> BoxError { Box::new(error) })
        .boxed_unsync();
    Reply {
        response: Response::builder()
            .status(StatusCode::OK)
            .header(header::CONTENT_TYPE, "text/event-stream")
            .header("cache-control", "no-cache")
            .header("x-accel-buffering", "no")
            .body(body)
            .expect("static response parts are valid"),
        method: label,
        method_name,
        stream: true,
        outcome: RequestOutcome::Completed,
    }
}

async fn pump<B>(
    mut incoming: B,
    tx: tokio::sync::mpsc::Sender<Result<Frame<Bytes>, std::io::Error>>,
    state: StreamState,
    started: Instant,
    mut usage: TrafficRecorder,
) where
    B: hyper::body::Body<Data = Bytes> + Unpin,
{
    let mut parser = SseParser::default();
    let mut finished = false;
    // `None` while the stream is healthy; a mid-stream failure decides the
    // recorded outcome. The upstream ending its stream normally is `Completed`.
    let mut failure: Option<RequestOutcome> = None;
    'outer: loop {
        // A client that goes away is noticed even while the upstream is silent
        // or a hook is running (M5): `tx.closed()` resolves when the response
        // body (and its receiver) is dropped. Without it the upstream
        // connection, the task and the usage recorder would stay alive until
        // the agent wrote again.
        let frame = tokio::select! {
            frame = incoming.frame() => frame,
            _ = tx.closed() => return,
        };
        // `frame()` returns None at EOF.
        let Some(frame) = frame else { break };
        let Ok(frame) = frame else {
            failure = Some(RequestOutcome::UpstreamError);
            break;
        };
        let Some(data) = frame.data_ref() else {
            continue;
        };
        for event in parser.push(data) {
            let Ok(event) = event else {
                let body = error_value(
                    Some(&state.id),
                    -32603,
                    "A stream event is too large",
                    "STREAM_EVENT_TOO_LARGE",
                    None,
                );
                let frame = format!(
                    "data: {}\n\n",
                    serde_json::to_string(&body).expect("JSON values serialize")
                );
                let _ = tx.send(Ok(Frame::data(Bytes::from(frame)))).await;
                finished = true;
                failure = Some(RequestOutcome::UpstreamError);
                break 'outer;
            };
            // A hook must not outlive the client either; dropping the future
            // aborts the guest at its next epoch yield.
            let result = tokio::select! {
                result = handle_event(&state, event) => result,
                _ = tx.closed() => return,
            };
            match result {
                EventResult::Emit(bytes) => {
                    if tx.send(Ok(Frame::data(bytes))).await.is_err() {
                        return;
                    }
                }
                EventResult::Drop => {}
                EventResult::Fatal(code, message) => {
                    let body =
                        error_value(Some(&state.id), code, &message, "MIDDLEWARE_FAILED", None);
                    let frame = format!(
                        "data: {}\n\n",
                        serde_json::to_string(&body).expect("JSON values serialize")
                    );
                    let _ = tx.send(Ok(Frame::data(Bytes::from(frame)))).await;
                    finished = true;
                    failure = Some(RequestOutcome::UpstreamError);
                    break 'outer;
                }
            }
        }
    }
    if !finished {
        for event in parser.finish() {
            let Ok(event) = event else {
                let body = error_value(
                    Some(&state.id),
                    -32603,
                    "A stream event is too large",
                    "STREAM_EVENT_TOO_LARGE",
                    None,
                );
                let frame = format!(
                    "data: {}\n\n",
                    serde_json::to_string(&body).expect("JSON values serialize")
                );
                let _ = tx.send(Ok(Frame::data(Bytes::from(frame)))).await;
                failure = Some(RequestOutcome::UpstreamError);
                break;
            };
            // A hook must not outlive the client either; dropping the future
            // aborts the guest at its next epoch yield.
            let result = tokio::select! {
                result = handle_event(&state, event) => result,
                _ = tx.closed() => return,
            };
            match result {
                EventResult::Emit(bytes) => {
                    if tx.send(Ok(Frame::data(bytes))).await.is_err() {
                        return;
                    }
                }
                EventResult::Drop => {}
                EventResult::Fatal(code, message) => {
                    let body =
                        error_value(Some(&state.id), code, &message, "MIDDLEWARE_FAILED", None);
                    let frame = format!(
                        "data: {}\n\n",
                        serde_json::to_string(&body).expect("JSON values serialize")
                    );
                    let _ = tx.send(Ok(Frame::data(Bytes::from(frame)))).await;
                    failure = Some(RequestOutcome::UpstreamError);
                    break;
                }
            }
        }
    }
    state
        .metrics
        .a2a_upstream_duration(&state.agent)
        .observe(started.elapsed().as_secs_f64());
    usage.finish(failure.unwrap_or(RequestOutcome::Completed));
}

enum EventResult {
    Emit(Bytes),
    Drop,
    Fatal(i32, String),
}

async fn handle_event(state: &StreamState, event: SseEvent) -> EventResult {
    let (data, raw) = match event {
        SseEvent::Data { data, raw } => (data, raw),
        SseEvent::Raw(raw) => return EventResult::Emit(raw),
    };
    let Ok(mut value) = serde_json::from_slice::<Value>(&data) else {
        // A comment or an upstream extension frame with non-JSON data is
        // forwarded untouched; there are no ids the gateway owns in it.
        return EventResult::Emit(raw);
    };
    if !value.is_object() {
        return EventResult::Emit(raw);
    }
    let before = ids::response_fields(&value);
    if let Some(middleware) = &state.middleware {
        let output = match middleware
            .on_stream_event(&state.method, &data, state.timeout)
            .await
        {
            Ok(output) => output,
            Err(hook::Error::Rpc { code, message }) => return EventResult::Fatal(code, message),
            Err(_) => {
                return EventResult::Fatal(-32603, "Middleware failed on a stream event".into())
            }
        };
        let Some(output) = output else {
            // Only an artifact update may be dropped (plan L5): every task
            // stream starts with a Task and ends with a terminal status.
            if ids::is_artifact_update(&value) {
                return EventResult::Drop;
            }
            return EventResult::Fatal(
                -32603,
                "Middleware dropped an event that is not an artifact update".into(),
            );
        };
        if output.len() > MAX_STREAM_EVENT {
            return EventResult::Fatal(-32603, "Middleware returned an oversized event".into());
        }
        match serde_json::from_slice::<Value>(&output) {
            Ok(after) if after.is_object() => {
                if ids::response_snake_alias(&after).is_some() {
                    return EventResult::Fatal(
                        -32602,
                        "ProtoJSON snake_case aliases are not supported".into(),
                    );
                }
                if let Err(failure) = check_response_unchanged(&before, &after) {
                    return EventResult::Fatal(failure.code, failure.message);
                }
                value = after;
            }
            _ => return EventResult::Fatal(-32603, "Middleware returned an invalid event".into()),
        }
    }
    let context = ids::Context {
        key: &state.key,
        agent: &state.agent,
        owner: &state.owner,
    };
    if ids::wrap_response(&state.method, &mut value, &context).is_err() {
        return EventResult::Fatal(-32006, BAD_AGENT_RESPONSE.into());
    }
    EventResult::Emit(Bytes::from(format!(
        "data: {}\n\n",
        serde_json::to_string(&value).expect("JSON values serialize")
    )))
}

// ---------------------------------------------------------------------------
// Incremental SSE parser
// ---------------------------------------------------------------------------

enum SseEvent {
    /// A complete event with at least one `data:` line; `data` is the lines
    /// joined with `\n`, `raw` the original event frame.
    Data { data: Vec<u8>, raw: Bytes },
    /// An event with no `data:` line (comment, keep-alive): forwarded as is.
    Raw(Bytes),
}

/// One event's parsed form or a size failure.
type ParsedEvent = Result<SseEvent, ()>;

#[derive(Default)]
struct SseParser {
    /// Bytes after the last processed line: the current partial line, if any.
    pending: Vec<u8>,
    /// Bytes of `pending` already searched for a terminator, so a byte is
    /// scanned at most once across chunks.
    scanned: usize,
    /// Complete lines of the event under construction.
    lines: Vec<Vec<u8>>,
    /// Bytes held for the event under construction.
    size: usize,
    /// The previous chunk ended with a bare CR; a leading LF of the next chunk
    /// belongs to that CRLF (m10).
    skip_lf: bool,
}

impl SseParser {
    fn push(&mut self, chunk: &[u8]) -> Vec<ParsedEvent> {
        let mut out = Vec::new();
        if chunk.is_empty() {
            return out;
        }
        let mut chunk = chunk;
        if self.skip_lf {
            if chunk.first() == Some(&b'\n') {
                chunk = &chunk[1..];
            }
            self.skip_lf = false;
        }
        self.pending.extend_from_slice(chunk);
        let mut start = 0usize;
        let mut scan = self.scanned.min(self.pending.len());
        // One linear walk per chunk: a frame of many short lines is O(n), not
        // O(n^2) from draining the front per line (m11).
        while let Some(offset) = self.pending[scan..]
            .iter()
            .position(|byte| *byte == b'\r' || *byte == b'\n')
        {
            let terminator = scan + offset;
            let line = self.pending[start..terminator].to_vec();
            let mut next = terminator + 1;
            if self.pending[terminator] == b'\r' {
                if next == self.pending.len() {
                    // A CR at the end of the buffer terminates the line; a LF
                    // starting the next chunk is the second half of a CRLF.
                    self.skip_lf = true;
                } else if self.pending[next] == b'\n' {
                    next += 1;
                }
            }
            if line.is_empty() {
                if let Some(event) = self.take_event() {
                    out.push(event);
                }
            } else {
                self.size = self.size.saturating_add(line.len() + 1);
                if self.size > MAX_STREAM_EVENT {
                    self.pending.clear();
                    self.scanned = 0;
                    self.lines.clear();
                    self.size = 0;
                    self.skip_lf = false;
                    out.push(Err(()));
                    return out;
                }
                self.lines.push(line);
            }
            start = next;
            scan = next;
        }
        if start > 0 {
            self.pending.drain(..start);
        }
        // Everything left is the partial line just searched to its end.
        self.scanned = self.pending.len();
        if self.pending.len() > MAX_STREAM_EVENT {
            self.pending.clear();
            self.scanned = 0;
            self.lines.clear();
            self.size = 0;
            self.skip_lf = false;
            out.push(Err(()));
        }
        out
    }

    /// Handles the line (and any unterminated event) left at EOF.
    fn finish(&mut self) -> Vec<ParsedEvent> {
        let mut out = Vec::new();
        if !self.pending.is_empty() {
            let line = std::mem::take(&mut self.pending);
            self.scanned = 0;
            self.size = self.size.saturating_add(line.len());
            if self.size > MAX_STREAM_EVENT {
                self.lines.clear();
                self.size = 0;
                return vec![Err(())];
            }
            self.lines.push(line);
        }
        if let Some(event) = self.take_event() {
            out.push(event);
        }
        out
    }

    fn take_event(&mut self) -> Option<ParsedEvent> {
        if self.lines.is_empty() {
            return None;
        }
        let mut data = Vec::new();
        let mut has_data = false;
        for line in &self.lines {
            if let Some(rest) = line.strip_prefix(b"data:") {
                let rest = rest.strip_prefix(b" ").unwrap_or(rest);
                if has_data {
                    data.push(b'\n');
                }
                data.extend_from_slice(rest);
                has_data = true;
            }
        }
        let mut raw = Vec::new();
        for line in &self.lines {
            raw.extend_from_slice(line);
            raw.push(b'\n');
        }
        raw.push(b'\n');
        self.lines.clear();
        self.size = 0;
        Some(if has_data {
            Ok(SseEvent::Data {
                data,
                raw: Bytes::from(raw),
            })
        } else {
            Ok(SseEvent::Raw(Bytes::from(raw)))
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn collect(parser: &mut SseParser, chunk: &[u8]) -> Vec<ParsedEvent> {
        parser.push(chunk)
    }

    #[test]
    fn multi_line_data_frames_are_joined_and_raw_is_preserved() {
        let mut parser = SseParser::default();
        let events = collect(&mut parser, b"data: {\"a\":\ndata: 1}\n\ndata: two\n\n");
        assert_eq!(events.len(), 2);
        let Ok(SseEvent::Data { data, raw }) = &events[0] else {
            panic!("first event is data");
        };
        assert_eq!(data, b"{\"a\":\n1}");
        assert_eq!(&raw[..], b"data: {\"a\":\ndata: 1}\n\n");
        let Ok(SseEvent::Data { data, .. }) = &events[1] else {
            panic!("second event is data");
        };
        assert_eq!(data, b"two");
    }

    #[test]
    fn comments_and_keepalives_have_no_data() {
        let mut parser = SseParser::default();
        let events = collect(&mut parser, b": keepalive\n\nevent: ping\n\n");
        assert_eq!(events.len(), 2);
        for event in &events {
            assert!(matches!(event, Ok(SseEvent::Raw(_))));
        }
    }

    #[test]
    fn events_split_across_chunks_and_crlf_are_parsed() {
        let mut parser = SseParser::default();
        assert!(collect(&mut parser, b"da").is_empty());
        assert!(collect(&mut parser, b"ta: {\"x\"").is_empty());
        let events = collect(&mut parser, b":1}\r\n\r\n");
        assert_eq!(events.len(), 1);
        let Ok(SseEvent::Data { data, .. }) = &events[0] else {
            panic!("data event");
        };
        assert_eq!(data, b"{\"x\":1}");
    }

    #[test]
    fn an_unterminated_event_is_still_emitted_at_eof() {
        let mut parser = SseParser::default();
        assert!(collect(&mut parser, b"data: tail").is_empty());
        let events = parser.finish();
        assert_eq!(events.len(), 1);
        let Ok(SseEvent::Data { data, .. }) = &events[0] else {
            panic!("data event");
        };
        assert_eq!(data, b"tail");
    }

    #[test]
    fn an_oversized_event_is_refused() {
        let mut parser = SseParser::default();
        let mut chunk = b"data: ".to_vec();
        chunk.extend(std::iter::repeat_n(b'x', MAX_STREAM_EVENT + 1));
        chunk.push(b'\n');
        let events = collect(&mut parser, &chunk);
        assert!(events.iter().any(|event| event.is_err()));
    }

    #[test]
    fn outbound_auth_failures_map_like_mcp() {
        use upstream_auth::Failure;
        assert_eq!(
            auth_failure_status(Failure::Timeout),
            StatusCode::GATEWAY_TIMEOUT
        );
        for failure in [
            Failure::Configure,
            Failure::Plugin,
            Failure::Trap,
            Failure::Header,
        ] {
            assert_eq!(auth_failure_status(failure), StatusCode::BAD_GATEWAY);
        }
    }

    #[test]
    fn only_exact_version_1_0_is_supported() {
        let mut headers = HeaderMap::new();
        assert!(!version_supported(&headers, None));
        headers.insert("a2a-version", HeaderValue::from_static("1.0"));
        assert!(version_supported(&headers, None));
        headers.insert("a2a-version", HeaderValue::from_static("0.3"));
        assert!(!version_supported(&headers, None));
        headers.insert("a2a-version", HeaderValue::from_static(""));
        assert!(!version_supported(&headers, None));
        assert!(!version_supported(
            &HeaderMap::new(),
            Some("A2A-Version=0.3")
        ));
        assert!(version_supported(
            &HeaderMap::new(),
            Some("x=1&A2A-Version=1.0")
        ));
    }

    #[test]
    fn client_credentials_and_framing_headers_are_not_forwarded() {
        let mut headers = HeaderMap::new();
        headers.insert("authorization", HeaderValue::from_static("Bearer x"));
        headers.insert("x-lumewarp-key", HeaderValue::from_static("secret"));
        headers.insert("cookie", HeaderValue::from_static("a=b"));
        headers.insert("a2a-version", HeaderValue::from_static("0.3"));
        headers.insert("a2a-extensions", HeaderValue::from_static("urn:x"));
        headers.insert("accept-encoding", HeaderValue::from_static("gzip, br"));
        headers.insert("x-custom", HeaderValue::from_static("kept"));
        let forwarded = upstream_headers(&headers);
        assert!(forwarded.get("authorization").is_some());
        assert!(forwarded.get("x-lumewarp-key").is_none());
        assert!(forwarded.get("cookie").is_none());
        // The gateway never decompresses, so it must not invite compression (M9).
        assert!(forwarded.get("accept-encoding").is_none());
        assert_eq!(forwarded.get("a2a-version").unwrap(), "1.0");
        assert_eq!(forwarded.get("a2a-extensions").unwrap(), "urn:x");
        assert_eq!(forwarded.get("x-custom").unwrap(), "kept");
        assert_eq!(forwarded.get("content-type").unwrap(), "application/json");
    }

    #[test]
    fn connection_nominated_headers_are_dropped() {
        let mut headers = HeaderMap::new();
        headers.insert("connection", HeaderValue::from_static("x-hop"));
        headers.insert("x-hop", HeaderValue::from_static("1"));
        let forwarded = upstream_headers(&headers);
        assert!(forwarded.get("x-hop").is_none());
    }

    #[test]
    fn only_the_v1_0_methods_are_accepted() {
        assert_eq!(METHODS.len(), 11);
        for method in METHODS {
            assert!(known_method(method), "{method}");
        }
        for method in [
            "tasks/get",
            "tasks/cancel",
            "tasks/resubscribe",
            "message/send",
            "message/stream",
            "sendmessage",
            "",
            "SendMessage ",
            "GetTaskExtra",
        ] {
            assert!(!known_method(method), "{method:?}");
        }
    }

    #[test]
    fn only_streaming_methods_may_answer_sse() {
        assert!(method_streams("SendStreamingMessage"));
        assert!(method_streams("SubscribeToTask"));
        for method in METHODS
            .iter()
            .filter(|method| !matches!(**method, "SendStreamingMessage" | "SubscribeToTask"))
        {
            assert!(!method_streams(method), "{method}");
        }
    }

    #[test]
    fn an_event_with_a_bare_cr_terminator_is_parsed() {
        let mut parser = SseParser::default();
        let events = collect(&mut parser, b"data: one\r\rdata: two\r\r");
        assert_eq!(events.len(), 2);
        let Ok(SseEvent::Data { data, .. }) = &events[0] else {
            panic!("data event");
        };
        assert_eq!(data, b"one");
        let Ok(SseEvent::Data { data, .. }) = &events[1] else {
            panic!("data event");
        };
        assert_eq!(data, b"two");
    }

    #[test]
    fn a_crlf_split_across_chunks_is_one_line_end() {
        let mut parser = SseParser::default();
        assert!(collect(&mut parser, b"data: x\r").is_empty());
        let events = collect(&mut parser, b"\n\r\n");
        assert_eq!(events.len(), 1);
        let Ok(SseEvent::Data { data, .. }) = &events[0] else {
            panic!("data event");
        };
        assert_eq!(data, b"x");
    }

    #[test]
    fn a_frame_with_many_short_lines_is_one_event() {
        let mut parser = SseParser::default();
        let mut frame = Vec::new();
        for index in 0..10_000 {
            frame.extend_from_slice(format!("data: {index}\n").as_bytes());
        }
        frame.push(b'\n');
        let events = collect(&mut parser, &frame);
        assert_eq!(events.len(), 1);
        let Ok(SseEvent::Data { data, .. }) = &events[0] else {
            panic!("data event");
        };
        assert_eq!(data.split(|byte| *byte == b'\n').count(), 10_000);
    }

    #[tokio::test]
    async fn a_client_disconnect_cancels_a_silent_stream() {
        use lumewarp_proto::config::v1::KeyStatus;
        struct SilentBody;
        impl hyper::body::Body for SilentBody {
            type Data = Bytes;
            type Error = std::io::Error;
            fn poll_frame(
                self: std::pin::Pin<&mut Self>,
                _: &mut std::task::Context<'_>,
            ) -> std::task::Poll<Option<Result<hyper::body::Frame<Bytes>, std::io::Error>>>
            {
                std::task::Poll::Pending
            }
        }

        let (tx, rx) = tokio::sync::mpsc::channel(4);
        drop(rx);
        let runtime = crate::runtime::Runtime::new();
        let key = Arc::new(KeyContext {
            key_id: "key".into(),
            team_id: "team".into(),
            status: KeyStatus::Active,
            expires_at_ms: 0,
            allow_all_models: true,
            allowed_models: Default::default(),
            scopes: Vec::new(),
            team_label: 0,
            grants: crate::grants::Grants::none(),
        });
        let caller = Caller::Key(key);
        let request = RequestCtx::new(Instant::now());
        let recorder = TrafficRecorder::new(
            &runtime,
            &TrafficEvent {
                kind: TrafficKind::A2a,
                request: &request,
                caller: &caller,
                target: "agent",
                method: "SubscribeToTask",
                tool: "",
                status: 200,
                outcome: RequestOutcome::Completed,
                stream: true,
                config_version: 0,
            },
        );
        let state = StreamState {
            key: IdKey::from_secret(vec![7u8; 32]).unwrap(),
            agent: "agent".into(),
            owner: "owner".into(),
            method: "SubscribeToTask".into(),
            middleware: None,
            timeout: Duration::from_secs(60),
            id: json!(1),
            metrics: Arc::new(Metrics::new()),
        };
        let pump = tokio::spawn(pump(SilentBody, tx, state, Instant::now(), recorder));
        tokio::time::timeout(Duration::from_millis(500), pump)
            .await
            .expect("the pump did not notice the dropped client")
            .unwrap();
    }

    #[test]
    fn a_response_hook_may_drop_ids_but_not_add_them() {
        let before = vec![
            ("$id".to_owned(), "1".to_owned()),
            ("result.artifactUpdate.taskId".to_owned(), "t1".to_owned()),
            (
                "result.artifactUpdate.contextId".to_owned(),
                "c1".to_owned(),
            ),
        ];
        let dropped = json!({"jsonrpc": "2.0", "id": 1, "result": {"artifactUpdate": {
            "taskId": "t1", "artifact": {}
        }}});
        assert!(check_response_unchanged(&before, &dropped).is_ok());
        let added = json!({"jsonrpc": "2.0", "id": 1, "result": {"artifactUpdate": {
            "taskId": "t1", "contextId": "c1", "artifact": {}
        }, "extra": {"taskId": "other"}}});
        assert!(check_response_unchanged(&before, &added).is_err());
        let changed = json!({"jsonrpc": "2.0", "id": 1, "result": {"artifactUpdate": {
            "taskId": "t2", "contextId": "c1", "artifact": {}
        }}});
        assert!(check_response_unchanged(&before, &changed).is_err());
        let wrong_id = json!({"jsonrpc": "2.0", "id": 2, "result": {"artifactUpdate": {
            "taskId": "t1", "contextId": "c1", "artifact": {}
        }}});
        assert!(check_response_unchanged(&before, &wrong_id).is_err());
        // `wrap_error_data` rewrites typed error metadata, so adding a raw id there
        // must be refused too (m4).
        let error_added = json!({"jsonrpc": "2.0", "id": 1, "error": {
            "code": -32001, "message": "x", "data": [{"metadata": {"taskId": "raw"}}]
        }});
        let event_without_error = json!({"jsonrpc": "2.0", "id": 1, "result": {}});
        assert!(check_response_unchanged(
            &ids::response_fields(&event_without_error),
            &error_added
        )
        .is_err());
        assert!(ids::response_fields(&error_added)
            .iter()
            .any(|(path, value)| path == "error.data[0].metadata.taskId" && value == "raw"));
    }
}
