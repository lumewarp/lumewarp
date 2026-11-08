//! Example A2A agent: v1.0 JSON-RPC binding plus SSE.
//!
//! The agent card lives at `/.well-known/agent-card.json` and points at this
//! server's own JSON-RPC URL. Every JSON-RPC method uses the v1.0 PascalCase
//! name; streaming responses are `StreamResponse` objects on `data:` frames.
//! Tasks are kept in memory. Push notifications are not supported, so the four
//! push-configuration methods answer `PushNotificationNotSupportedError`.

use crate::{
    auth::{Auth, AuthOptions},
    server::{
        control_forbidden, empty, header_str, is_loopback, json, method_not_allowed, not_found,
        query_param, raw, rpc_error, rpc_result, serve, unauthorized, Body, RequestLog,
    },
    util::{iso8601_from_ms, now_ms, parse_iso8601_ms, sha256_hex},
};
use bytes::Bytes;
use clap::Args;
use http_body_util::BodyExt;
use hyper::{
    body::Incoming,
    header::{HeaderMap, HeaderValue, CACHE_CONTROL, ETAG, IF_NONE_MATCH},
    Method, Request, Response, StatusCode,
};
use serde_json::{json, Map, Value};
use std::{
    collections::HashMap,
    net::{IpAddr, SocketAddr},
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc, Mutex, MutexGuard,
    },
    time::Duration,
};
use tokio::{net::TcpListener, sync::watch};

const A2A_VERSION: &str = "1.0";
const CARD_PATH: &str = "/.well-known/agent-card.json";
const ERROR_INFO_TYPE: &str = "type.googleapis.com/google.rpc.ErrorInfo";
const ERROR_DOMAIN: &str = "a2a-protocol.org";
const TASK_STATES: [&str; 8] = [
    "TASK_STATE_SUBMITTED",
    "TASK_STATE_WORKING",
    "TASK_STATE_COMPLETED",
    "TASK_STATE_FAILED",
    "TASK_STATE_CANCELED",
    "TASK_STATE_REJECTED",
    "TASK_STATE_INPUT_REQUIRED",
    "TASK_STATE_AUTH_REQUIRED",
];

#[derive(Clone, Debug, Args)]
pub struct A2aOptions {
    /// Address of the JSON-RPC endpoint.
    #[arg(long, default_value = "127.0.0.1:9120")]
    pub listen: SocketAddr,
    /// Path of the JSON-RPC endpoint.
    #[arg(long, default_value = "/a2a")]
    pub path: String,
    /// Base URL advertised in the agent card; defaults to the listen address.
    #[arg(long, value_name = "URL")]
    pub public_url: Option<String>,
    #[command(flatten)]
    pub auth: AuthOptions,
    /// Media type of the extra artifact part the agent returns.
    #[arg(long, default_value = "application/x-mock-secret")]
    pub artifact_media_type: String,
    /// Delay between streamed task updates.
    #[arg(long, default_value_t = 50)]
    pub stream_step_ms: u64,
    /// Omit the dummy `signatures` entry from the agent card.
    #[arg(long)]
    pub no_signature: bool,
    /// Advertise and serve no streaming methods.
    #[arg(long)]
    pub no_streaming: bool,
    /// Do not return the configurable-media-type artifact.
    #[arg(long)]
    pub no_secret_artifact: bool,
}

impl Default for A2aOptions {
    fn default() -> Self {
        Self {
            listen: "127.0.0.1:9120".parse().expect("static address"),
            path: "/a2a".to_owned(),
            public_url: None,
            auth: AuthOptions::default(),
            artifact_media_type: "application/x-mock-secret".to_owned(),
            stream_step_ms: 50,
            no_signature: false,
            no_streaming: false,
            no_secret_artifact: false,
        }
    }
}

pub struct A2aServer {
    opts: A2aOptions,
    auth: Auth,
    card: Bytes,
    card_etag: String,
    store: TaskStore,
    log: RequestLog,
}

impl A2aServer {
    pub fn new(opts: A2aOptions) -> Result<Self, String> {
        if !opts.path.starts_with('/') {
            return Err("--path must start with '/'".to_owned());
        }
        if opts.artifact_media_type.trim().is_empty() {
            return Err("--artifact-media-type must not be empty".to_owned());
        }
        if opts
            .public_url
            .as_deref()
            .is_some_and(|url| !url.starts_with("http"))
        {
            return Err("--public-url must be an absolute http(s) URL".to_owned());
        }
        let auth = opts.auth.build()?;
        let card =
            Bytes::from(serde_json::to_vec(&build_card(&opts)).expect("card is serializable"));
        let card_etag = format!("\"{}\"", sha256_hex(&card));
        Ok(Self {
            opts,
            auth,
            card,
            card_etag,
            store: TaskStore::default(),
            log: RequestLog::new(64),
        })
    }

    fn next_id(&self, prefix: &str) -> String {
        static COUNTER: AtomicU64 = AtomicU64::new(0);
        format!(
            "{prefix}-{}-{}",
            now_ms(),
            COUNTER.fetch_add(1, Ordering::Relaxed)
        )
    }

    async fn handle(
        self: &Arc<Self>,
        request: Request<Incoming>,
        peer: SocketAddr,
    ) -> Response<Body> {
        let path = request.uri().path().to_owned();
        if path.starts_with("/_mock/") {
            if !is_loopback(peer) {
                return control_forbidden();
            }
            return self.control(request).await;
        }
        if request.method() == Method::GET && path == CARD_PATH {
            return self.card_response(request.headers());
        }
        if request.method() == Method::GET && path == "/health" {
            return json(StatusCode::OK, json!({"status": "ok"}));
        }
        if path != self.opts.path && path != "/" {
            return not_found();
        }
        if request.method() != Method::POST {
            return method_not_allowed("POST");
        }
        let query = request.uri().query().unwrap_or("").to_owned();
        let (parts, body) = request.into_parts();
        let Ok(body) = body.collect().await else {
            return rpc_error(StatusCode::OK, None, -32700, "Invalid JSON payload");
        };
        let body = body.to_bytes();
        self.log.record(&parts.method, &path, &parts.headers, peer);
        if let Err(failure) = self
            .auth
            .check(parts.method.as_str(), &path, &parts.headers, &body)
        {
            return unauthorized(&failure);
        }
        let version = header_str(&parts.headers, "a2a-version")
            .map(str::to_owned)
            .or_else(|| query_param(&query, "A2A-Version"));
        let Ok(value) = serde_json::from_slice::<Value>(&body) else {
            return rpc_error(StatusCode::OK, None, -32700, "Invalid JSON payload");
        };
        self.dispatch(value, version.as_deref()).await
    }

    fn card_response(&self, headers: &HeaderMap) -> Response<Body> {
        let matches = header_str(headers, IF_NONE_MATCH.as_str())
            .is_some_and(|value| value.split(',').any(|etag| etag.trim() == self.card_etag));
        if matches {
            let mut response = empty(StatusCode::NOT_MODIFIED);
            response.headers_mut().insert(
                ETAG,
                HeaderValue::from_str(&self.card_etag).expect("etag is a valid header value"),
            );
            return response;
        }
        let mut response = raw(StatusCode::OK, "application/json", self.card.clone());
        response.headers_mut().insert(
            ETAG,
            HeaderValue::from_str(&self.card_etag).expect("etag is a valid header value"),
        );
        response.headers_mut().insert(
            CACHE_CONTROL,
            HeaderValue::from_static("public, max-age=60"),
        );
        response
    }

    async fn dispatch(self: &Arc<Self>, value: Value, version: Option<&str>) -> Response<Body> {
        let Some(object) = value.as_object() else {
            return rpc_error(
                StatusCode::OK,
                None,
                -32600,
                "Request payload validation error",
            );
        };
        let id = object.get("id").cloned();
        if object.get("jsonrpc").and_then(Value::as_str) != Some("2.0") {
            return rpc_error(
                StatusCode::OK,
                id,
                -32600,
                "Request payload validation error: jsonrpc must be \"2.0\"",
            );
        }
        let Some(method) = object.get("method").and_then(Value::as_str) else {
            return rpc_error(
                StatusCode::OK,
                id,
                -32600,
                "Request payload validation error: missing method",
            );
        };
        let params = object.get("params").cloned().unwrap_or_else(|| json!({}));
        if !params.is_object() {
            return a2a_error(id, -32602, "Invalid parameters", "INVALID_PARAMS", None);
        }
        if let Some(version) = version {
            if version != A2A_VERSION {
                return a2a_error(
                    id,
                    -32009,
                    "A2A version is not supported",
                    "VERSION_NOT_SUPPORTED",
                    Some(json!({"requested": version, "supported": [A2A_VERSION]})),
                );
            }
        }
        match method {
            "SendMessage" => self.send_message(id, &params, false).await,
            "SendStreamingMessage" => self.send_message(id, &params, true).await,
            "GetTask" => self.get_task(id, &params),
            "ListTasks" => self.list_tasks(id, &params),
            "CancelTask" => self.cancel_task(id, &params),
            "SubscribeToTask" => self.subscribe_to_task(id, &params),
            "CreateTaskPushNotificationConfig"
            | "GetTaskPushNotificationConfig"
            | "ListTaskPushNotificationConfigs"
            | "DeleteTaskPushNotificationConfig" => a2a_error(
                id,
                -32003,
                "Push notifications are not supported",
                "PUSH_NOTIFICATION_NOT_SUPPORTED",
                None,
            ),
            "GetExtendedAgentCard" => a2a_error(
                id,
                -32004,
                "The extended agent card is not available",
                "UNSUPPORTED_OPERATION",
                None,
            ),
            _ => rpc_error(
                StatusCode::OK,
                id,
                -32601,
                &format!("Method not found: {method}"),
            ),
        }
    }

    async fn send_message(
        self: &Arc<Self>,
        id: Option<Value>,
        params: &Value,
        streaming: bool,
    ) -> Response<Body> {
        if streaming && self.opts.no_streaming {
            return a2a_error(
                id,
                -32004,
                "Streaming is not supported",
                "UNSUPPORTED_OPERATION",
                None,
            );
        }
        let Some(message) = params.get("message").filter(|value| value.is_object()) else {
            return a2a_error(
                id,
                -32602,
                "Invalid parameters: message is required",
                "INVALID_PARAMS",
                None,
            );
        };
        let Some(text) = validate_message(message) else {
            return a2a_error(
                id,
                -32602,
                "Invalid parameters: message needs messageId, role ROLE_USER and parts",
                "INVALID_PARAMS",
                None,
            );
        };
        let mut return_immediately = false;
        if let Some(configuration) = params.get("configuration").and_then(Value::as_object) {
            if configuration.contains_key("taskPushNotificationConfig") {
                return a2a_error(
                    id,
                    -32003,
                    "Push notifications are not supported",
                    "PUSH_NOTIFICATION_NOT_SUPPORTED",
                    None,
                );
            }
            if let Some(modes) = configuration
                .get("acceptedOutputModes")
                .and_then(Value::as_array)
            {
                let acceptable = modes.is_empty()
                    || modes.iter().any(|mode| {
                        mode.as_str().is_some_and(|mode| {
                            mode == "*/*"
                                || mode == "text/plain"
                                || mode == "application/json"
                                || mode == self.opts.artifact_media_type
                        })
                    });
                if !acceptable {
                    return a2a_error(
                        id,
                        -32005,
                        "Incompatible content types",
                        "CONTENT_TYPE_NOT_SUPPORTED",
                        None,
                    );
                }
            }
            return_immediately = configuration
                .get("returnImmediately")
                .and_then(Value::as_bool)
                .unwrap_or(false);
        }
        let task_id = self.next_id("task");
        let context_id = message
            .get("contextId")
            .and_then(Value::as_str)
            .map(str::to_owned)
            .unwrap_or_else(|| self.next_id("ctx"));
        let task = self.store.create(task_id, context_id, message.clone());
        spawn_producer(self.clone(), task.clone(), text);
        if streaming {
            return self.subscribe_stream(id, task);
        }
        if return_immediately {
            return rpc_result(
                id.unwrap_or(Value::Null),
                json!({"task": render_task(&lock(&task), HistoryMode::None, true)}),
            );
        }
        let timeout = Duration::from_millis(self.opts.stream_step_ms.saturating_mul(10).max(5_000));
        let finished = wait_terminal(&task, timeout).await;
        // The task completes in a background producer; a slow step should not
        // make a blocking SendMessage fail, but it must not hang forever.
        if !finished {
            return a2a_error(
                id,
                -32603,
                "Internal error: the task did not reach a terminal state",
                "TASK_TIMEOUT",
                None,
            );
        }
        rpc_result(
            id.unwrap_or(Value::Null),
            json!({"task": render_task(&lock(&task), HistoryMode::All, true)}),
        )
    }

    fn get_task(&self, id: Option<Value>, params: &Value) -> Response<Body> {
        let Some(task_id) = params.get("id").and_then(Value::as_str) else {
            return a2a_error(
                id,
                -32602,
                "Invalid parameters: id is required",
                "INVALID_PARAMS",
                None,
            );
        };
        let Some(task) = self.store.get(task_id) else {
            return task_not_found(id, task_id);
        };
        let history = match params.get("historyLength") {
            None => HistoryMode::All,
            Some(value) => match value.as_u64() {
                Some(length) => HistoryMode::Last(length as usize),
                None => {
                    return a2a_error(
                        id,
                        -32602,
                        "Invalid parameters: historyLength must be a non-negative integer",
                        "INVALID_PARAMS",
                        None,
                    );
                }
            },
        };
        let rendered = render_task(&lock(&task), history, true);
        rpc_result(id.unwrap_or(Value::Null), rendered)
    }

    fn list_tasks(&self, id: Option<Value>, params: &Value) -> Response<Body> {
        let invalid = |detail: &str| {
            a2a_error(
                id.clone(),
                -32602,
                &format!("Invalid parameters: {detail}"),
                "INVALID_PARAMS",
                None,
            )
        };
        let context_id = params.get("contextId").and_then(Value::as_str);
        let status = match params.get("status") {
            None => None,
            Some(Value::String(status)) if TASK_STATES.contains(&status.as_str()) => {
                Some(status.as_str())
            }
            Some(_) => return invalid("status is not a TaskState"),
        };
        let page_size = match params.get("pageSize") {
            None => 50,
            Some(value) => match value.as_u64() {
                Some(size) if (1..=100).contains(&size) => size as usize,
                _ => return invalid("pageSize must be between 1 and 100"),
            },
        };
        let page_token = match params.get("pageToken") {
            None => 0,
            Some(Value::String(token)) if token.is_empty() => 0,
            Some(Value::String(token)) => match token.parse::<usize>() {
                Ok(start) => start,
                Err(_) => return invalid("pageToken is not valid"),
            },
            Some(_) => return invalid("pageToken must be a string"),
        };
        let status_after = match params.get("statusTimestampAfter") {
            None => None,
            Some(Value::String(timestamp)) => match parse_iso8601_ms(timestamp) {
                Some(millis) => Some(millis),
                None => return invalid("statusTimestampAfter is not an ISO 8601 timestamp"),
            },
            Some(_) => return invalid("statusTimestampAfter must be a string"),
        };
        let include_artifacts = match params.get("includeArtifacts") {
            None => false,
            Some(Value::Bool(value)) => *value,
            Some(_) => return invalid("includeArtifacts must be a boolean"),
        };
        let history = match params.get("historyLength") {
            None => HistoryMode::None,
            Some(value) => match value.as_u64() {
                Some(length) => HistoryMode::Last(length as usize),
                None => return invalid("historyLength must be a non-negative integer"),
            },
        };
        let mut tasks: Vec<Arc<Mutex<TaskRecord>>> = self
            .store
            .all()
            .into_iter()
            .filter(|task| {
                let record = lock(task);
                context_id.is_none_or(|context| record.context_id == context)
                    && status.is_none_or(|status| record.state == status)
                    && status_after.is_none_or(|after| record.status_ts_ms > after)
            })
            .collect();
        tasks.sort_by(|left, right| {
            let left = lock(left);
            let right = lock(right);
            right
                .status_ts_ms
                .cmp(&left.status_ts_ms)
                .then_with(|| right.id.cmp(&left.id))
        });
        let total = tasks.len();
        let start = page_token.min(total);
        let end = start.saturating_add(page_size).min(total);
        let rendered: Vec<Value> = tasks[start..end]
            .iter()
            .map(|task| render_task(&lock(task), history, include_artifacts))
            .collect();
        let next_page_token = if end < total {
            end.to_string()
        } else {
            String::new()
        };
        rpc_result(
            id.unwrap_or(Value::Null),
            json!({
                "tasks": rendered,
                "nextPageToken": next_page_token,
                "pageSize": page_size,
                "totalSize": total,
            }),
        )
    }

    fn cancel_task(&self, id: Option<Value>, params: &Value) -> Response<Body> {
        let Some(task_id) = params.get("id").and_then(Value::as_str) else {
            return a2a_error(
                id,
                -32602,
                "Invalid parameters: id is required",
                "INVALID_PARAMS",
                None,
            );
        };
        let Some(task) = self.store.get(task_id) else {
            return task_not_found(id, task_id);
        };
        {
            let mut record = lock(&task);
            if record.terminal {
                drop(record);
                return a2a_error(
                    id,
                    -32002,
                    "Task cannot be canceled",
                    "TASK_NOT_CANCELABLE",
                    Some(json!({"taskId": task_id, "state": lock(&task).state})),
                );
            }
            record.state = "TASK_STATE_CANCELED";
            record.terminal = true;
            record.status_ts_ms = now_ms();
            let message = agent_message("Canceled by request.");
            record.status_message = Some(message.clone());
            record.history.push(message);
            let event = json!({
                "statusUpdate": {
                    "taskId": record.id,
                    "contextId": record.context_id,
                    "status": record.status_json(),
                },
            });
            record.events.push(event);
            bump(&mut record);
        }
        let rendered = render_task(&lock(&task), HistoryMode::All, true);
        rpc_result(id.unwrap_or(Value::Null), rendered)
    }

    fn subscribe_to_task(self: &Arc<Self>, id: Option<Value>, params: &Value) -> Response<Body> {
        if self.opts.no_streaming {
            return a2a_error(
                id,
                -32004,
                "Streaming is not supported",
                "UNSUPPORTED_OPERATION",
                None,
            );
        }
        let Some(task_id) = params.get("id").and_then(Value::as_str) else {
            return a2a_error(
                id,
                -32602,
                "Invalid parameters: id is required",
                "INVALID_PARAMS",
                None,
            );
        };
        let Some(task) = self.store.get(task_id) else {
            return task_not_found(id, task_id);
        };
        if lock(&task).terminal {
            return a2a_error(
                id,
                -32004,
                "The task is already terminal",
                "UNSUPPORTED_OPERATION",
                None,
            );
        }
        self.subscribe_stream(id, task)
    }

    /// SSE: the current `Task` first, then every recorded update until the task
    /// reaches a terminal state. Closing the stream is cancellation of the
    /// subscription, not of the task.
    fn subscribe_stream(
        self: &Arc<Self>,
        id: Option<Value>,
        task: Arc<Mutex<TaskRecord>>,
    ) -> Response<Body> {
        let rpc_id = id.unwrap_or(Value::Null);
        let initial = render_task(&lock(&task), HistoryMode::None, true);
        let mut version = lock(&task).version.subscribe();
        let stream = async_stream::stream! {
            yield Ok::<_, std::convert::Infallible>(hyper::body::Frame::data(
                crate::server::sse_frame(&json!({"jsonrpc": "2.0", "id": rpc_id, "result": {"task": initial}})),
            ));
            let mut index = 0usize;
            loop {
                let (events, terminal) = {
                    let record = lock(&task);
                    if index > record.events.len() {
                        index = record.events.len();
                    }
                    (record.events[index..].to_vec(), record.terminal)
                };
                for event in events {
                    index += 1;
                    yield Ok(hyper::body::Frame::data(crate::server::sse_frame(&json!({
                        "jsonrpc": "2.0",
                        "id": rpc_id.clone(),
                        "result": event,
                    }))));
                }
                if terminal {
                    break;
                }
                if version.changed().await.is_err() {
                    break;
                }
            }
        };
        crate::server::sse(stream)
    }

    async fn control(self: &Arc<Self>, request: Request<Incoming>) -> Response<Body> {
        let path = request.uri().path().to_owned();
        let query = request.uri().query().unwrap_or("").to_owned();
        let method = request.method().clone();
        let Ok(body) = request.into_body().collect().await else {
            return json(
                StatusCode::BAD_REQUEST,
                json!({"error": "cannot read body"}),
            );
        };
        let _ = body.to_bytes();
        match (method.as_str(), path.as_str()) {
            ("GET", "/_mock/headers") => {
                let count = query_param(&query, "n")
                    .and_then(|value| value.parse::<usize>().ok())
                    .unwrap_or(20)
                    .min(64);
                json(StatusCode::OK, json!({"headers": self.log.last(count)}))
            }
            ("GET", "/_mock/tasks") => {
                let tasks: Vec<Value> = self
                    .store
                    .all()
                    .iter()
                    .map(|task| render_task(&lock(task), HistoryMode::None, true))
                    .collect();
                json(StatusCode::OK, json!({"tasks": tasks}))
            }
            ("POST", "/_mock/reset") => {
                self.store.clear();
                self.log.clear();
                json(StatusCode::OK, json!({"ok": true}))
            }
            _ => not_found(),
        }
    }
}

/// Serve the A2A JSON-RPC endpoint and agent card.
pub async fn serve_a2a(listener: TcpListener, server: Arc<A2aServer>) -> std::io::Result<()> {
    serve(listener, move |request, peer| {
        let server = server.clone();
        async move { server.handle(request, peer).await }
    })
    .await
}

fn display_addr(addr: SocketAddr) -> String {
    match addr.ip() {
        IpAddr::V4(ip) if ip.is_unspecified() => format!("127.0.0.1:{}", addr.port()),
        IpAddr::V6(ip) if ip.is_unspecified() => format!("[::1]:{}", addr.port()),
        _ => addr.to_string(),
    }
}

fn build_card(opts: &A2aOptions) -> Value {
    let base = opts
        .public_url
        .clone()
        .unwrap_or_else(|| format!("http://{}", display_addr(opts.listen)));
    let base = base.trim_end_matches('/');
    let mobile_media = opts.artifact_media_type.clone();
    let mut card = json!({
        "name": "Lumewarp Mock A2A Agent",
        "description": "Echo agent for Lumewarp development and tests. Streams status updates plus a text artifact and an artifact with a configurable media type.",
        "supportedInterfaces": [{
            "url": format!("{base}{}", opts.path),
            "protocolBinding": "JSONRPC",
            "protocolVersion": A2A_VERSION,
        }],
        "provider": {
            "organization": "Lumewarp",
            "url": "https://lumewarp.example",
        },
        "version": env!("CARGO_PKG_VERSION"),
        "documentationUrl": "https://github.com/lumewarp/lumewarp",
        "capabilities": {
            "streaming": !opts.no_streaming,
            "pushNotifications": false,
        },
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["text/plain", "application/json", mobile_media],
        "skills": [{
            "id": "echo",
            "name": "Echo",
            "description": "Echo the text it receives and return a text artifact plus a secret artifact.",
            "tags": ["echo", "mock"],
            "examples": ["Say hello in five words."],
            "inputModes": ["text/plain"],
            "outputModes": ["text/plain", opts.artifact_media_type],
        }],
    });
    match opts.auth.auth {
        crate::auth::AuthMode::None => {}
        crate::auth::AuthMode::Bearer | crate::auth::AuthMode::AnyBearer => {
            card["securitySchemes"] = json!({
                "bearerAuth": {
                    "httpAuthSecurityScheme": {
                        "description": "Bearer token issued by the agent operator.",
                        "scheme": "Bearer",
                    },
                },
            });
            card["securityRequirements"] = json!([
                {"schemes": {"bearerAuth": {"list": []}}},
            ]);
        }
        crate::auth::AuthMode::Hmac => {
            card["securitySchemes"] = json!({
                "hmacSignature": {
                    "apiKeySecurityScheme": {
                        "description": "HMAC-SHA256 request signature; see HMAC.md.",
                        "location": "header",
                        "name": "X-Lumewarp-Signature",
                    },
                },
            });
            card["securityRequirements"] = json!([
                {"schemes": {"hmacSignature": {"list": []}}},
            ]);
        }
    }
    if !opts.no_signature {
        // JWS-shaped, deliberately not a valid signature: the gateway must drop
        // upstream signatures when it rewrites the card.
        card["signatures"] = json!([{
            "protected": "eyJhbGciOiJFUzI1NiIsImtpZCI6Im1vY2sta2V5IiwidHlwIjoiSk9TRSJ9",
            "signature": "bm90LWEtcmVhbC1zaWduYXR1cmU",
        }]);
    }
    card
}

fn validate_message(message: &Value) -> Option<String> {
    let object = message.as_object()?;
    let message_id = object.get("messageId").and_then(Value::as_str)?;
    if message_id.is_empty() {
        return None;
    }
    if object.get("role").and_then(Value::as_str) != Some("ROLE_USER") {
        return None;
    }
    let parts = object.get("parts")?.as_array()?;
    if parts.is_empty() {
        return None;
    }
    let mut texts = Vec::new();
    for part in parts {
        let part = part.as_object()?;
        if let Some(text) = part.get("text").and_then(Value::as_str) {
            texts.push(text.to_owned());
        }
    }
    Some(texts.join("\n"))
}

/// The task store holds tasks in memory; each record has a watch channel so a
/// subscription wakes up when the producer appends an event.
struct TaskStore {
    tasks: Mutex<HashMap<String, Arc<Mutex<TaskRecord>>>>,
}

impl Default for TaskStore {
    fn default() -> Self {
        Self {
            tasks: Mutex::new(HashMap::new()),
        }
    }
}

impl TaskStore {
    fn create(
        &self,
        id: String,
        context_id: String,
        user_message: Value,
    ) -> Arc<Mutex<TaskRecord>> {
        let task = Arc::new(Mutex::new(TaskRecord::new(
            id.clone(),
            context_id,
            user_message,
        )));
        self.tasks
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .insert(id, task.clone());
        task
    }

    fn get(&self, id: &str) -> Option<Arc<Mutex<TaskRecord>>> {
        self.tasks
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .get(id)
            .cloned()
    }

    fn all(&self) -> Vec<Arc<Mutex<TaskRecord>>> {
        self.tasks
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .values()
            .cloned()
            .collect()
    }

    fn clear(&self) {
        self.tasks
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .clear();
    }
}

struct TaskRecord {
    id: String,
    context_id: String,
    state: &'static str,
    status_ts_ms: u64,
    status_message: Option<Value>,
    history: Vec<Value>,
    artifacts: Vec<Value>,
    events: Vec<Value>,
    terminal: bool,
    version: watch::Sender<u64>,
}

impl TaskRecord {
    fn new(id: String, context_id: String, user_message: Value) -> Self {
        Self {
            id,
            context_id,
            state: "TASK_STATE_WORKING",
            status_ts_ms: now_ms(),
            status_message: None,
            history: vec![user_message],
            artifacts: Vec::new(),
            events: Vec::new(),
            terminal: false,
            version: watch::channel(0).0,
        }
    }

    fn status_json(&self) -> Value {
        let mut status = Map::new();
        status.insert("state".to_owned(), json!(self.state));
        status.insert(
            "timestamp".to_owned(),
            json!(iso8601_from_ms(self.status_ts_ms)),
        );
        if let Some(message) = &self.status_message {
            status.insert("message".to_owned(), message.clone());
        }
        Value::Object(status)
    }
}

#[derive(Clone, Copy)]
enum HistoryMode {
    None,
    Last(usize),
    All,
}

fn lock(task: &Arc<Mutex<TaskRecord>>) -> MutexGuard<'_, TaskRecord> {
    task.lock().unwrap_or_else(|error| error.into_inner())
}

fn bump(record: &mut TaskRecord) {
    let next = *record.version.borrow() + 1;
    record.version.send_replace(next);
}

fn render_task(record: &TaskRecord, history: HistoryMode, include_artifacts: bool) -> Value {
    let mut task = Map::new();
    task.insert("id".to_owned(), json!(record.id));
    task.insert("contextId".to_owned(), json!(record.context_id));
    task.insert("status".to_owned(), record.status_json());
    if include_artifacts && !record.artifacts.is_empty() {
        task.insert(
            "artifacts".to_owned(),
            Value::Array(record.artifacts.clone()),
        );
    }
    let messages: &[Value] = match history {
        HistoryMode::None => &[],
        HistoryMode::Last(0) => &[],
        HistoryMode::Last(count) => &record.history[record.history.len().saturating_sub(count)..],
        HistoryMode::All => &record.history,
    };
    if !messages.is_empty() {
        task.insert("history".to_owned(), Value::Array(messages.to_vec()));
    }
    Value::Object(task)
}

async fn wait_terminal(task: &Arc<Mutex<TaskRecord>>, timeout: Duration) -> bool {
    let mut version = lock(task).version.subscribe();
    let wait = async {
        loop {
            if lock(task).terminal {
                return;
            }
            if version.changed().await.is_err() {
                return;
            }
        }
    };
    tokio::time::timeout(timeout, wait).await.is_ok()
}

fn spawn_producer(server: Arc<A2aServer>, task: Arc<Mutex<TaskRecord>>, text: String) {
    let step = Duration::from_millis(server.opts.stream_step_ms);
    let media = server.opts.artifact_media_type.clone();
    let with_secret = !server.opts.no_secret_artifact;
    tokio::spawn(async move {
        // A canceled task must stop producing updates.
        if !step_done(&task, step).await {
            return;
        }
        push_status(
            &task,
            "TASK_STATE_WORKING",
            Some(agent_message("Working on it…")),
            false,
        );
        if !step_done(&task, step).await {
            return;
        }
        push_artifact(&task, text_artifact(&task, &text));
        if with_secret {
            if !step_done(&task, step).await {
                return;
            }
            push_artifact(&task, secret_artifact(&task, &media));
        }
        if !step_done(&task, step).await {
            return;
        }
        push_status(
            &task,
            "TASK_STATE_COMPLETED",
            Some(agent_message(&format!("Done. You said: {text}"))),
            true,
        );
    });
}

async fn step_done(task: &Arc<Mutex<TaskRecord>>, step: Duration) -> bool {
    tokio::time::sleep(step).await;
    !lock(task).terminal
}

fn push_status(
    task: &Arc<Mutex<TaskRecord>>,
    state: &'static str,
    message: Option<Value>,
    terminal: bool,
) {
    let mut record = lock(task);
    record.state = state;
    record.status_ts_ms = now_ms();
    record.terminal = terminal;
    record.status_message = message;
    if terminal {
        if let Some(message) = record.status_message.clone() {
            record.history.push(message);
        }
    }
    let event = json!({
        "statusUpdate": {
            "taskId": record.id,
            "contextId": record.context_id,
            "status": record.status_json(),
        },
    });
    record.events.push(event);
    bump(&mut record);
}

fn push_artifact(task: &Arc<Mutex<TaskRecord>>, artifact: Value) {
    let mut record = lock(task);
    record.artifacts.push(artifact.clone());
    let event = json!({
        "artifactUpdate": {
            "taskId": record.id,
            "contextId": record.context_id,
            "artifact": artifact,
            "append": false,
            "lastChunk": true,
        },
    });
    record.events.push(event);
    bump(&mut record);
}

fn text_artifact(task: &Arc<Mutex<TaskRecord>>, text: &str) -> Value {
    let artifact_id = {
        let record = lock(task);
        format!("{}-text", record.id)
    };
    json!({
        "artifactId": artifact_id,
        "name": "reply",
        "description": "The agent's text answer.",
        "parts": [{"text": format!("Echo: {text}"), "mediaType": "text/plain"}],
    })
}

fn secret_artifact(task: &Arc<Mutex<TaskRecord>>, media_type: &str) -> Value {
    let artifact_id = {
        let record = lock(task);
        format!("{}-secret", record.id)
    };
    json!({
        "artifactId": artifact_id,
        "name": "mock secret",
        "description": "An artifact with a configurable media type for middleware tests.",
        "parts": [{
            "data": {"secret": "mock-secret-value", "note": "strip me"},
            "mediaType": media_type,
        }],
    })
}

fn agent_message(text: &str) -> Value {
    static COUNTER: AtomicU64 = AtomicU64::new(0);
    json!({
        "messageId": format!("agent-{}-{}", now_ms(), COUNTER.fetch_add(1, Ordering::Relaxed)),
        "role": "ROLE_AGENT",
        "parts": [{"text": text, "mediaType": "text/plain"}],
    })
}

fn a2a_error(
    id: Option<Value>,
    code: i64,
    message: &str,
    reason: &str,
    metadata: Option<Value>,
) -> Response<Body> {
    let mut error = json!({"code": code, "message": message});
    let mut info = json!({
        "@type": ERROR_INFO_TYPE,
        "reason": reason,
        "domain": ERROR_DOMAIN,
    });
    if let Some(metadata) = metadata {
        info["metadata"] = metadata;
    }
    error["data"] = json!([info]);
    json(
        StatusCode::OK,
        json!({
            "jsonrpc": "2.0",
            "id": id.unwrap_or(Value::Null),
            "error": error,
        }),
    )
}

fn task_not_found(id: Option<Value>, task_id: &str) -> Response<Body> {
    a2a_error(
        id,
        -32001,
        "Task not found",
        "TASK_NOT_FOUND",
        Some(json!({"taskId": task_id})),
    )
}
