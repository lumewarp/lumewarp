//! Example MCP server: revision 2025-11-25 over Streamable HTTP.
//!
//! The lifecycle is the pre-stateless one: `initialize` negotiates a protocol
//! version and an `Mcp-Session-Id`, every later request must carry the session
//! (and `MCP-Protocol-Version`), `DELETE` ends it, and `GET` opens an SSE
//! stream for server-initiated notifications. There is no `server/discover`
//! and no `Mcp-Method`/`Mcp-Name` headers in this revision.
//!
//! The `ask` tool is the interesting one for a gateway: it sends an
//! `elicitation/create` request on the POST's SSE stream and waits for the
//! client to POST the JSON-RPC response back, so request-id routing has to
//! work end to end. See `.omh/runs/mcp-a2a/ref/specs-mcp-2025-11-25.md`.

use crate::{
    auth::{Auth, AuthOptions},
    server::{
        control_body, control_forbidden, empty, header_str, is_loopback, json, method_not_allowed,
        not_found, query_param, rpc_error, rpc_result, serve, sse, sse_frame, unauthorized, Body,
        RequestLog,
    },
    util::{now_ms, sha256_hex},
};
use bytes::Bytes;
use clap::Args;
use http_body_util::BodyExt;
use hyper::{
    body::{Frame, Incoming},
    header::{HeaderMap, HeaderValue},
    Method, Request, Response, StatusCode,
};
use serde_json::{json, Map, Value};
use std::{
    collections::HashMap,
    convert::Infallible,
    net::SocketAddr,
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc, Mutex, RwLock,
    },
    time::Duration,
};
use tokio::{net::TcpListener, sync::mpsc, sync::oneshot};

const LATEST_VERSION: &str = "2025-11-25";
const SUPPORTED_VERSIONS: [&str; 2] = ["2025-11-25", "2025-06-18"];
const ADMIN_TOOL_META: &str = "mock/adminOnly";

#[derive(Clone, Debug, Args)]
pub struct McpOptions {
    /// Address of the MCP endpoint.
    #[arg(long, default_value = "127.0.0.1:9110")]
    pub listen: SocketAddr,
    /// Path of the Streamable HTTP endpoint.
    #[arg(long, default_value = "/mcp")]
    pub path: String,
    #[command(flatten)]
    pub auth: AuthOptions,
    /// Page `tools/list` every N tools; 0 returns everything in one page.
    #[arg(long, default_value_t = 0)]
    pub tools_page_size: usize,
    /// Page `prompts/list` every N prompts; 0 returns everything in one page.
    #[arg(long, default_value_t = 0)]
    pub prompts_page_size: usize,
    /// Request header that selects an authorization role.
    #[arg(long, default_value = "x-mock-role")]
    pub role_header: String,
    /// Value of `--role-header` that adds `admin_only` to the tool list.
    #[arg(long, default_value = "admin")]
    pub role_admin_value: String,
    /// How long the `ask` tool waits for the client's elicitation response.
    #[arg(long, default_value_t = 30_000)]
    pub elicitation_timeout_ms: u64,
    /// Extra allowed `Origin` values (loopback origins are always allowed).
    #[arg(long = "allow-origin", value_name = "ORIGIN")]
    pub allow_origins: Vec<String>,
    /// Skip the `Origin` check entirely.
    #[arg(long)]
    pub allow_any_origin: bool,
}

impl Default for McpOptions {
    fn default() -> Self {
        Self {
            listen: "127.0.0.1:9110".parse().expect("static address"),
            path: "/mcp".to_owned(),
            auth: AuthOptions::default(),
            tools_page_size: 0,
            prompts_page_size: 0,
            role_header: "x-mock-role".to_owned(),
            role_admin_value: "admin".to_owned(),
            elicitation_timeout_ms: 30_000,
            allow_origins: Vec::new(),
            allow_any_origin: false,
        }
    }
}

pub struct McpServer {
    opts: McpOptions,
    auth: Auth,
    tools: RwLock<Vec<Value>>,
    prompts: Vec<Value>,
    log: RequestLog,
    sessions: Mutex<HashMap<String, Session>>,
    pending: Mutex<HashMap<PendingKey, oneshot::Sender<Value>>>,
}

struct Session {
    protocol_version: String,
    /// Whether the client declared form-mode elicitation at initialize.
    form_elicitation: bool,
    /// At most one active GET stream per session: the spec forbids sending one
    /// message on more than one stream.
    notify: Option<mpsc::UnboundedSender<Value>>,
}

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
struct PendingKey {
    session: String,
    id: String,
}

#[derive(Clone, Debug)]
struct SessionInfo {
    id: String,
    version: String,
    form_elicitation: bool,
}

impl McpServer {
    pub fn new(opts: McpOptions) -> Result<Self, String> {
        if !opts.path.starts_with('/') {
            return Err("--path must start with '/'".to_owned());
        }
        let auth = opts.auth.build()?;
        Ok(Self {
            opts,
            auth,
            tools: RwLock::new(default_tools()),
            prompts: default_prompts(),
            log: RequestLog::new(64),
            sessions: Mutex::new(HashMap::new()),
            pending: Mutex::new(HashMap::new()),
        })
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
        if path != self.opts.path {
            return not_found();
        }
        if request.method() == Method::DELETE {
            return self.handle_delete(&request);
        }
        if request.method() == Method::GET {
            return self.handle_get(&request).await;
        }
        if request.method() == Method::POST {
            return self.handle_post(request, peer).await;
        }
        method_not_allowed("GET, POST, DELETE")
    }

    fn handle_delete(&self, request: &Request<Incoming>) -> Response<Body> {
        if let Err(failure) = self
            .auth
            .check("DELETE", &self.opts.path, request.headers(), &[])
        {
            return unauthorized(&failure);
        }
        let Some(id) = header_str(request.headers(), "mcp-session-id") else {
            return rpc_error(
                StatusCode::BAD_REQUEST,
                None,
                -32600,
                "Missing Mcp-Session-Id header",
            );
        };
        let removed = self
            .sessions
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .remove(id);
        match removed {
            Some(_) => empty(StatusCode::NO_CONTENT),
            None => unknown_session(),
        }
    }

    async fn handle_get(self: &Arc<Self>, request: &Request<Incoming>) -> Response<Body> {
        let path = request.uri().path().to_owned();
        if let Err(failure) = self.auth.check("GET", &path, request.headers(), &[]) {
            return unauthorized(&failure);
        }
        let session = match self.require_session(request.headers()) {
            Ok(session) => session,
            Err(response) => return *response,
        };
        if header_str(request.headers(), "accept").is_some_and(|accept| {
            !accept
                .split(',')
                .any(|value| value.trim().starts_with("text/event-stream"))
        }) {
            return json(
                StatusCode::NOT_ACCEPTABLE,
                json!({"error": "accept must include text/event-stream"}),
            );
        }
        let Some(receiver) = self.install_get_stream(&session.id) else {
            return unknown_session();
        };
        let event_id = self.next_event_id();
        let stream = async_stream::stream! {
            yield Ok::<_, Infallible>(Frame::data(priming_frame(event_id)));
            let mut receiver = receiver;
            while let Some(message) = receiver.recv().await {
                yield Ok(Frame::data(sse_frame(&message)));
            }
        };
        sse(stream)
    }

    async fn handle_post(
        self: &Arc<Self>,
        request: Request<Incoming>,
        peer: SocketAddr,
    ) -> Response<Body> {
        let path = request.uri().path().to_owned();
        let (parts, body) = request.into_parts();
        let Ok(body) = body.collect().await else {
            return rpc_error(StatusCode::BAD_REQUEST, None, -32700, "Cannot read body");
        };
        let body = body.to_bytes();
        self.log.record(&parts.method, &path, &parts.headers, peer);
        if let Some(origin) = header_str(&parts.headers, "origin") {
            if !self.origin_allowed(origin) {
                return json(
                    StatusCode::FORBIDDEN,
                    json!({"error": "invalid_origin", "origin": origin}),
                );
            }
        }
        if let Err(failure) = self
            .auth
            .check(parts.method.as_str(), &path, &parts.headers, &body)
        {
            return unauthorized(&failure);
        }
        let Ok(value) = serde_json::from_slice::<Value>(&body) else {
            return rpc_error(StatusCode::BAD_REQUEST, None, -32700, "Parse error");
        };
        self.dispatch(parts.headers, value).await
    }

    async fn dispatch(self: &Arc<Self>, headers: HeaderMap, value: Value) -> Response<Body> {
        let Some(object) = value.as_object() else {
            return rpc_error(
                StatusCode::BAD_REQUEST,
                None,
                -32600,
                "Invalid Request: expected a JSON object",
            );
        };
        if object.get("jsonrpc").and_then(Value::as_str) != Some("2.0") {
            return rpc_error(
                StatusCode::BAD_REQUEST,
                object.get("id").cloned(),
                -32600,
                "Invalid Request: jsonrpc must be \"2.0\"",
            );
        }
        if object.get("id").is_some_and(Value::is_null) {
            return rpc_error(
                StatusCode::BAD_REQUEST,
                Some(Value::Null),
                -32600,
                "Invalid Request: id must not be null",
            );
        }
        let method = object.get("method").and_then(Value::as_str);
        let id = object.get("id").cloned();
        if method == Some("initialize") {
            if id.is_none() {
                return rpc_error(
                    StatusCode::BAD_REQUEST,
                    None,
                    -32600,
                    "initialize must be a request with an id",
                );
            }
            return self.initialize(id.expect("checked above"), object);
        }
        // Every other message needs a live session, whether it is a request, a
        // notification or the client's JSON-RPC response to `elicitation/create`.
        let session = match self.require_session(&headers) {
            Ok(session) => session,
            Err(response) => return *response,
        };
        let Some(method) = method else {
            if object.contains_key("id") {
                return self.handle_client_response(session, value);
            }
            return rpc_error(
                StatusCode::BAD_REQUEST,
                None,
                -32600,
                "Invalid Request: missing method",
            );
        };
        let params = object.get("params").cloned().unwrap_or_else(|| json!({}));
        let Some(params) = params.as_object() else {
            return rpc_error(
                StatusCode::BAD_REQUEST,
                id,
                -32602,
                "Invalid params: params must be an object",
            );
        };
        let Some(id) = id else {
            // Notification, e.g. notifications/initialized or notifications/cancelled.
            return empty(StatusCode::ACCEPTED);
        };
        match method {
            "ping" => rpc_result(id, json!({})),
            "tools/list" => match self.tools_list(params, &headers) {
                Ok(result) => rpc_result(id, result),
                Err(message) => rpc_error(StatusCode::OK, Some(id), -32602, &message),
            },
            "tools/call" => self.tools_call(id, params, &headers, &session).await,
            "prompts/list" => match self.prompts_list(params) {
                Ok(result) => rpc_result(id, result),
                Err(message) => rpc_error(StatusCode::OK, Some(id), -32602, &message),
            },
            "prompts/get" => match self.prompts_get(params) {
                Ok(result) => rpc_result(id, result),
                Err(message) => rpc_error(StatusCode::OK, Some(id), -32602, &message),
            },
            _ => rpc_error(
                StatusCode::OK,
                Some(id),
                -32601,
                &format!("Method not found: {method}"),
            ),
        }
    }

    fn initialize(&self, id: Value, object: &Map<String, Value>) -> Response<Body> {
        let params = object.get("params").and_then(Value::as_object);
        let requested = params
            .and_then(|params| params.get("protocolVersion"))
            .and_then(Value::as_str)
            .unwrap_or("2025-03-26");
        let negotiated = if SUPPORTED_VERSIONS.contains(&requested) {
            requested
        } else {
            LATEST_VERSION
        };
        let form_elicitation = params.is_some_and(|params| {
            params
                .get("capabilities")
                .and_then(|capabilities| capabilities.get("elicitation"))
                .is_some_and(form_elicitation_capability)
        });
        let session_id = self.create_session(negotiated, form_elicitation);
        let response = rpc_result(
            id,
            json!({
                "protocolVersion": negotiated,
                "capabilities": {
                    "tools": {"listChanged": true},
                    "prompts": {},
                },
                "serverInfo": {
                    "name": "Lumewarp Mock MCP",
                    "version": env!("CARGO_PKG_VERSION"),
                },
                "instructions": "Lumewarp mock MCP server: tools echo, add, slow, ask, \
                                 whoami and (with the role header) admin_only; prompt greet.",
            }),
        );
        let mut response = response;
        response.headers_mut().insert(
            "mcp-session-id",
            HeaderValue::from_str(&session_id).expect("session id is a valid header value"),
        );
        response
    }

    fn handle_client_response(&self, session: SessionInfo, value: Value) -> Response<Body> {
        let id = match value.get("id") {
            Some(Value::String(id)) => id.clone(),
            Some(Value::Number(id)) => id.to_string(),
            _ => {
                return rpc_error(
                    StatusCode::BAD_REQUEST,
                    None,
                    -32600,
                    "Invalid response: id is required",
                );
            }
        };
        let key = PendingKey {
            session: session.id,
            id,
        };
        let Some(sender) = self
            .pending
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .remove(&key)
        else {
            return rpc_error(
                StatusCode::BAD_REQUEST,
                None,
                -32600,
                "No pending server request with that id in this session",
            );
        };
        let _ = sender.send(value);
        empty(StatusCode::ACCEPTED)
    }

    fn require_session(&self, headers: &HeaderMap) -> Result<SessionInfo, Box<Response<Body>>> {
        let Some(id) = header_str(headers, "mcp-session-id") else {
            return Err(Box::new(rpc_error(
                StatusCode::BAD_REQUEST,
                None,
                -32600,
                "Missing Mcp-Session-Id header; send initialize first",
            )));
        };
        let (version, form_elicitation) = {
            let sessions = self
                .sessions
                .lock()
                .unwrap_or_else(|error| error.into_inner());
            match sessions.get(id) {
                Some(session) => (session.protocol_version.clone(), session.form_elicitation),
                None => return Err(Box::new(unknown_session())),
            }
        };
        if let Some(header_version) = header_str(headers, "mcp-protocol-version") {
            if header_version != version {
                return Err(Box::new(rpc_error(
                    StatusCode::BAD_REQUEST,
                    None,
                    -32600,
                    &format!(
                        "Unsupported MCP-Protocol-Version '{header_version}': this session negotiated '{version}'"
                    ),
                )));
            }
        }
        Ok(SessionInfo {
            id: id.to_owned(),
            version,
            form_elicitation,
        })
    }

    fn create_session(&self, protocol_version: &str, form_elicitation: bool) -> String {
        static COUNTER: AtomicU64 = AtomicU64::new(0);
        let seed = format!(
            "{}:{}:{}",
            now_ms(),
            COUNTER.fetch_add(1, Ordering::Relaxed),
            std::process::id()
        );
        let id = sha256_hex(seed.as_bytes())[..32].to_owned();
        self.sessions
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .insert(
                id.clone(),
                Session {
                    protocol_version: protocol_version.to_owned(),
                    form_elicitation,
                    notify: None,
                },
            );
        id
    }

    fn install_get_stream(&self, id: &str) -> Option<mpsc::UnboundedReceiver<Value>> {
        let (sender, receiver) = mpsc::unbounded_channel();
        let mut sessions = self
            .sessions
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let session = sessions.get_mut(id)?;
        session.notify = Some(sender);
        Some(receiver)
    }

    fn notify_tools_changed(&self) {
        let message = json!({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"});
        let senders: Vec<mpsc::UnboundedSender<Value>> = self
            .sessions
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .values()
            .filter_map(|session| session.notify.clone())
            .collect();
        for sender in senders {
            let _ = sender.send(message.clone());
        }
    }

    fn next_event_id(&self) -> u64 {
        static COUNTER: AtomicU64 = AtomicU64::new(0);
        COUNTER.fetch_add(1, Ordering::Relaxed) + 1
    }

    fn tools_list(
        &self,
        params: &Map<String, Value>,
        headers: &HeaderMap,
    ) -> Result<Value, String> {
        let role = header_str(headers, &self.opts.role_header);
        let tools: Vec<Value> = self
            .tools
            .read()
            .unwrap_or_else(|error| error.into_inner())
            .iter()
            .filter(|tool| {
                !is_admin_tool(tool) || role == Some(self.opts.role_admin_value.as_str())
            })
            .cloned()
            .collect();
        let start = cursor_offset(params.get("cursor"))?;
        if start > tools.len() {
            return Err("Invalid cursor".to_owned());
        }
        let page_size = if self.opts.tools_page_size == 0 {
            tools.len()
        } else {
            self.opts.tools_page_size
        };
        let end = start.saturating_add(page_size).min(tools.len());
        let mut result = json!({"tools": tools[start..end].to_vec()});
        if end < tools.len() {
            result["nextCursor"] = json!(encode_cursor(end));
        }
        Ok(result)
    }

    fn prompts_list(&self, params: &Map<String, Value>) -> Result<Value, String> {
        let start = cursor_offset(params.get("cursor"))?;
        if start > self.prompts.len() {
            return Err("Invalid cursor".to_owned());
        }
        let page_size = if self.opts.prompts_page_size == 0 {
            self.prompts.len()
        } else {
            self.opts.prompts_page_size
        };
        let end = start.saturating_add(page_size).min(self.prompts.len());
        let mut result = json!({"prompts": self.prompts[start..end].to_vec()});
        if end < self.prompts.len() {
            result["nextCursor"] = json!(encode_cursor(end));
        }
        Ok(result)
    }

    async fn tools_call(
        self: &Arc<Self>,
        id: Value,
        params: &Map<String, Value>,
        headers: &HeaderMap,
        session: &SessionInfo,
    ) -> Response<Body> {
        if params.contains_key("task") {
            return rpc_error(
                StatusCode::OK,
                Some(id),
                -32601,
                "Task-augmented execution is not supported by this server",
            );
        }
        let Some(name) = params.get("name").and_then(Value::as_str) else {
            return rpc_error(
                StatusCode::OK,
                Some(id),
                -32602,
                "Invalid params: name is required",
            );
        };
        let arguments = params
            .get("arguments")
            .cloned()
            .unwrap_or_else(|| json!({}));
        let Some(arguments) = arguments.as_object() else {
            return rpc_error(
                StatusCode::OK,
                Some(id),
                -32602,
                "Invalid params: arguments must be an object",
            );
        };
        let role = header_str(headers, &self.opts.role_header);
        let known = self
            .tools
            .read()
            .unwrap_or_else(|error| error.into_inner())
            .iter()
            .any(|tool| tool.get("name").and_then(Value::as_str) == Some(name));
        let permitted =
            known && (name != "admin_only" || role == Some(self.opts.role_admin_value.as_str()));
        if !permitted {
            return rpc_error(
                StatusCode::OK,
                Some(id),
                -32602,
                &format!("Unknown tool: {name}"),
            );
        }
        match name {
            "echo" => {
                let Some(message) = arguments.get("message").and_then(Value::as_str) else {
                    return rpc_result(id, tool_error("message is required"));
                };
                rpc_result(id, tool_result(message, Some(json!({"message": message}))))
            }
            "add" => {
                let (Some(a), Some(b)) = (
                    arguments.get("a").and_then(Value::as_f64),
                    arguments.get("b").and_then(Value::as_f64),
                ) else {
                    return rpc_result(id, tool_error("a and b are required numbers"));
                };
                let sum = a + b;
                rpc_result(
                    id,
                    tool_result(
                        &format_number(sum),
                        Some(json!({"sum": sum, "a": a, "b": b})),
                    ),
                )
            }
            "slow" => {
                let delay_ms = arguments
                    .get("delay_ms")
                    .and_then(Value::as_u64)
                    .unwrap_or(100)
                    .min(60_000);
                let steps = arguments
                    .get("steps")
                    .and_then(Value::as_u64)
                    .unwrap_or(4)
                    .clamp(1, 100);
                let progress_token = params
                    .get("_meta")
                    .and_then(|meta| meta.get("progressToken"))
                    .cloned();
                let total_ms = delay_ms * steps;
                let result = tool_result(
                    &format!("slept {total_ms} ms in {steps} steps"),
                    Some(json!({
                        "delay_ms": delay_ms,
                        "steps": steps,
                        "total_ms": total_ms,
                    })),
                );
                if !accepts_sse(headers) {
                    tokio::time::sleep(Duration::from_millis(total_ms)).await;
                    return rpc_result(id, result);
                }
                let priming = priming_frame(self.next_event_id());
                let stream = sse_progress(id, priming, result, delay_ms, steps, progress_token);
                sse(stream)
            }
            "ask" => {
                let Some(question) = arguments.get("question").and_then(Value::as_str) else {
                    return rpc_result(id, tool_error("question is required"));
                };
                if !session.form_elicitation {
                    return rpc_result(
                        id,
                        tool_error(
                            "the client did not declare the elicitation capability at initialize",
                        ),
                    );
                }
                if !accepts_sse(headers) {
                    return rpc_result(
                        id,
                        tool_error(
                            "ask needs an SSE response stream: send \
                             Accept: application/json, text/event-stream",
                        ),
                    );
                }
                self.ask(id, question, session)
            }
            "whoami" => {
                let mut all = Map::new();
                for (header, value) in headers {
                    all.insert(
                        header.as_str().to_owned(),
                        Value::String(String::from_utf8_lossy(value.as_bytes()).into_owned()),
                    );
                }
                let summary = json!({
                    "authorization": header_str(headers, "authorization"),
                    "saw_lumewarp_key": headers.contains_key("x-lumewarp-key"),
                    "mcp_session_id": session.id,
                    "protocol_version": session.version,
                    "headers": all,
                });
                let text = serde_json::to_string_pretty(&summary).expect("JSON is serializable");
                rpc_result(id, tool_result(&text, Some(summary)))
            }
            "admin_only" => rpc_result(
                id,
                tool_result(
                    "admin_only: the request carried the admin role header",
                    Some(json!({"admin": true})),
                ),
            ),
            _ => rpc_error(
                StatusCode::OK,
                Some(id),
                -32602,
                &format!("Unknown tool: {name}"),
            ),
        }
    }

    /// `ask`: one `elicitation/create` request on the POST stream, then wait
    /// for the matching response POST. The pending entry is keyed by session
    /// and JSON-RPC id so a response cannot resolve another session's request.
    fn ask(self: &Arc<Self>, id: Value, question: &str, session: &SessionInfo) -> Response<Body> {
        static COUNTER: AtomicU64 = AtomicU64::new(0);
        let elicit_id = format!(
            "elicit-{}-{}",
            now_ms(),
            COUNTER.fetch_add(1, Ordering::Relaxed)
        );
        let (sender, receiver) = oneshot::channel();
        self.pending
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .insert(
                PendingKey {
                    session: session.id.clone(),
                    id: elicit_id.clone(),
                },
                sender,
            );
        let request = json!({
            "jsonrpc": "2.0",
            "id": elicit_id,
            "method": "elicitation/create",
            "params": {
                "message": question,
                "requestedSchema": {
                    "type": "object",
                    "properties": {
                        "answer": {
                            "type": "string",
                            "title": "Answer",
                            "description": "The text to send back.",
                        },
                    },
                    "required": ["answer"],
                },
            },
        });
        let key = PendingKey {
            session: session.id.clone(),
            id: elicit_id,
        };
        let server = self.clone();
        let timeout = Duration::from_millis(self.opts.elicitation_timeout_ms);
        let priming = priming_frame(self.next_event_id());
        let stream = async_stream::stream! {
            yield Ok::<_, Infallible>(Frame::data(priming));
            yield Ok(Frame::data(sse_frame(&request)));
            let outcome = tokio::time::timeout(timeout, receiver).await;
            server
                .pending
                .lock()
                .unwrap_or_else(|error| error.into_inner())
                .remove(&key);
            let result = match outcome {
                Ok(Ok(response)) => ask_result(&response),
                Ok(Err(_)) => tool_error("the elicitation response channel closed"),
                Err(_) => tool_error(&format!(
                    "no elicitation response within {} ms",
                    timeout.as_millis()
                )),
            };
            yield Ok(Frame::data(sse_frame(&json!({
                "jsonrpc": "2.0",
                "id": id,
                "result": result,
            }))));
        };
        sse(stream)
    }

    fn prompts_get(&self, params: &Map<String, Value>) -> Result<Value, String> {
        let Some(name) = params.get("name").and_then(Value::as_str) else {
            return Err("Invalid params: name is required".to_owned());
        };
        if name != "greet" {
            return Err(format!("Unknown prompt: {name}"));
        }
        let arguments = params
            .get("arguments")
            .cloned()
            .unwrap_or_else(|| json!({}));
        let Some(person) = arguments.get("name").and_then(Value::as_str) else {
            return Err("Invalid params: name is required".to_owned());
        };
        Ok(json!({
            "description": "A friendly greeting.",
            "messages": [{
                "role": "user",
                "content": {
                    "type": "text",
                    "text": format!("Hello, {person}! Welcome to the Lumewarp mock MCP server."),
                },
            }],
        }))
    }

    fn origin_allowed(&self, origin: &str) -> bool {
        if self.opts.allow_any_origin {
            return true;
        }
        if self
            .opts
            .allow_origins
            .iter()
            .any(|allowed| allowed.eq_ignore_ascii_case(origin))
        {
            return true;
        }
        let authority = origin
            .split_once("://")
            .map_or(origin, |(_, authority)| authority);
        let authority = authority.split('/').next().unwrap_or(authority);
        let host = if let Some(rest) = authority.strip_prefix('[') {
            rest.split(']').next().unwrap_or(rest)
        } else {
            authority.split(':').next().unwrap_or(authority)
        };
        let host = host.to_ascii_lowercase();
        host == "localhost" || host == "127.0.0.1" || host == "::1" || host.ends_with(".localhost")
    }

    async fn control(self: &Arc<Self>, request: Request<Incoming>) -> Response<Body> {
        let path = request.uri().path().to_owned();
        let query = request.uri().query().unwrap_or("").to_owned();
        let method = request.method().clone();
        let body = match request.into_body().collect().await {
            Ok(body) => body.to_bytes(),
            Err(_) => {
                return json(
                    StatusCode::BAD_REQUEST,
                    json!({"error": "cannot read body"}),
                );
            }
        };
        match (method.as_str(), path.as_str()) {
            ("GET", "/_mock/headers") => {
                let count = query_param(&query, "n")
                    .and_then(|value| value.parse::<usize>().ok())
                    .unwrap_or(20)
                    .min(64);
                json(StatusCode::OK, json!({"headers": self.log.last(count)}))
            }
            ("GET", "/_mock/tools") => json(
                StatusCode::OK,
                json!({"tools": *self.tools.read().unwrap_or_else(|error| error.into_inner())}),
            ),
            ("POST", "/_mock/tool") => {
                let Some(tool) = control_body(&body) else {
                    return json(
                        StatusCode::BAD_REQUEST,
                        json!({"error": "body must be a JSON tool definition"}),
                    );
                };
                let Some(name) = tool.get("name").and_then(Value::as_str).map(str::to_owned) else {
                    return json(
                        StatusCode::BAD_REQUEST,
                        json!({"error": "tool name is required"}),
                    );
                };
                if name.is_empty() || name.len() > 128 {
                    return json(
                        StatusCode::BAD_REQUEST,
                        json!({"error": "tool name must be 1..=128 characters"}),
                    );
                }
                {
                    let mut tools = self
                        .tools
                        .write()
                        .unwrap_or_else(|error| error.into_inner());
                    if let Some(existing) = tools.iter_mut().find(|existing| {
                        existing.get("name").and_then(Value::as_str) == Some(&name)
                    }) {
                        *existing = tool;
                    } else {
                        tools.push(tool);
                    }
                }
                self.notify_tools_changed();
                json(StatusCode::OK, json!({"ok": true, "name": name}))
            }
            ("POST", "/_mock/tool/description") => {
                let Some(change) = control_body(&body) else {
                    return json(
                        StatusCode::BAD_REQUEST,
                        json!({"error": "body must be {\"name\", \"description\"}"}),
                    );
                };
                let (Some(name), Some(description)) = (
                    change.get("name").and_then(Value::as_str),
                    change.get("description").and_then(Value::as_str),
                ) else {
                    return json(
                        StatusCode::BAD_REQUEST,
                        json!({"error": "name and description are required"}),
                    );
                };
                {
                    let mut tools = self
                        .tools
                        .write()
                        .unwrap_or_else(|error| error.into_inner());
                    let Some(tool) = tools
                        .iter_mut()
                        .find(|tool| tool.get("name").and_then(Value::as_str) == Some(name))
                    else {
                        return json(
                            StatusCode::NOT_FOUND,
                            json!({"error": "unknown tool", "name": name}),
                        );
                    };
                    tool["description"] = json!(description);
                }
                self.notify_tools_changed();
                json(StatusCode::OK, json!({"ok": true, "name": name}))
            }
            ("POST", "/_mock/tool/remove") => {
                let Some(change) = control_body(&body) else {
                    return json(
                        StatusCode::BAD_REQUEST,
                        json!({"error": "body must be {\"name\"}"}),
                    );
                };
                let Some(name) = change.get("name").and_then(Value::as_str) else {
                    return json(
                        StatusCode::BAD_REQUEST,
                        json!({"error": "name is required"}),
                    );
                };
                let removed = {
                    let mut tools = self
                        .tools
                        .write()
                        .unwrap_or_else(|error| error.into_inner());
                    let before = tools.len();
                    tools.retain(|tool| tool.get("name").and_then(Value::as_str) != Some(name));
                    before != tools.len()
                };
                if removed {
                    self.notify_tools_changed();
                }
                json(StatusCode::OK, json!({"ok": true, "removed": removed}))
            }
            ("POST", "/_mock/reset") => {
                *self
                    .tools
                    .write()
                    .unwrap_or_else(|error| error.into_inner()) = default_tools();
                self.log.clear();
                self.notify_tools_changed();
                json(StatusCode::OK, json!({"ok": true}))
            }
            _ => not_found(),
        }
    }
}

/// Serve the MCP endpoint. Dropping the enclosing task stops accepting.
pub async fn serve_mcp(listener: TcpListener, server: Arc<McpServer>) -> std::io::Result<()> {
    serve(listener, move |request, peer| {
        let server = server.clone();
        async move { server.handle(request, peer).await }
    })
    .await
}

fn unknown_session() -> Response<Body> {
    rpc_error(
        StatusCode::NOT_FOUND,
        None,
        -32600,
        "Unknown Mcp-Session-Id; send a new initialize",
    )
}

/// Prepend the empty-`data` priming event the transport recommends.
fn priming_frame(event_id: u64) -> Bytes {
    Bytes::from(format!("id: {event_id}\ndata:\n\n"))
}

fn tool_result(text: &str, structured: Option<Value>) -> Value {
    let mut result = json!({
        "content": [{"type": "text", "text": text}],
        "isError": false,
    });
    if let Some(structured) = structured {
        result["structuredContent"] = structured;
    }
    result
}

fn tool_error(text: &str) -> Value {
    json!({
        "content": [{"type": "text", "text": text}],
        "isError": true,
    })
}

fn ask_result(response: &Value) -> Value {
    if let Some(error) = response.get("error") {
        return tool_error(&format!(
            "the client answered with an error: {}",
            error
                .get("message")
                .and_then(Value::as_str)
                .unwrap_or("unknown error")
        ));
    }
    let result = response.get("result").cloned().unwrap_or_else(|| json!({}));
    let action = result
        .get("action")
        .and_then(Value::as_str)
        .unwrap_or("unknown");
    let content = result.get("content").cloned();
    let text = match action {
        "accept" => match content
            .as_ref()
            .and_then(|content| content.get("answer"))
            .and_then(Value::as_str)
        {
            Some(answer) => format!("accepted: {answer}"),
            None => format!("accepted: {}", content.clone().unwrap_or(Value::Null)),
        },
        other => format!("the client answered '{other}'"),
    };
    tool_result(&text, Some(json!({"action": action, "content": content})))
}

fn format_number(value: f64) -> String {
    if value.fract() == 0.0 && value.abs() < 1e15 {
        format!("{}", value as i64)
    } else {
        format!("{value}")
    }
}

/// `elicitation: {}` means form only (2025-06-18 compatibility); `form` must be
/// present when the object has only the newer `url` mode.
fn form_elicitation_capability(capability: &Value) -> bool {
    capability.as_object().is_some_and(|capability| {
        capability.contains_key("form")
            || (!capability.contains_key("form") && !capability.contains_key("url"))
    })
}

fn accepts_sse(headers: &HeaderMap) -> bool {
    header_str(headers, "accept").is_some_and(|accept| {
        accept.split(',').any(|value| {
            value
                .split(';')
                .next()
                .map(str::trim)
                .is_some_and(|mime| mime.eq_ignore_ascii_case("text/event-stream"))
        })
    })
}

fn sse_progress(
    id: Value,
    priming: Bytes,
    result: Value,
    delay_ms: u64,
    steps: u64,
    progress_token: Option<Value>,
) -> impl futures_util::Stream<Item = Result<Frame<Bytes>, Infallible>> + Send + 'static {
    async_stream::stream! {
        yield Ok(Frame::data(priming));
        for step in 1..=steps {
            tokio::time::sleep(Duration::from_millis(delay_ms)).await;
            if let Some(token) = progress_token.clone() {
                yield Ok(Frame::data(sse_frame(&json!({
                    "jsonrpc": "2.0",
                    "method": "notifications/progress",
                    "params": {
                        "progressToken": token,
                        "progress": step,
                        "total": steps,
                        "message": format!("step {step} of {steps}"),
                    },
                }))));
            }
        }
        yield Ok(Frame::data(sse_frame(&json!({
            "jsonrpc": "2.0",
            "id": id,
            "result": result,
        }))));
    }
}

fn is_admin_tool(tool: &Value) -> bool {
    tool.get("_meta")
        .and_then(|meta| meta.get(ADMIN_TOOL_META))
        .and_then(Value::as_bool)
        .unwrap_or(false)
}

fn encode_cursor(offset: usize) -> String {
    use base64::{engine::general_purpose::STANDARD as BASE64, Engine as _};
    BASE64.encode(format!("c:{offset}"))
}

fn cursor_offset(cursor: Option<&Value>) -> Result<usize, String> {
    use base64::{engine::general_purpose::STANDARD as BASE64, Engine as _};
    match cursor {
        None => Ok(0),
        Some(Value::String(value)) if value.is_empty() => Ok(0),
        Some(Value::String(value)) => {
            let bytes = BASE64
                .decode(value)
                .map_err(|_| "Invalid cursor".to_owned())?;
            let text = String::from_utf8(bytes).map_err(|_| "Invalid cursor".to_owned())?;
            text.strip_prefix("c:")
                .ok_or_else(|| "Invalid cursor".to_owned())?
                .parse()
                .map_err(|_| "Invalid cursor".to_owned())
        }
        Some(_) => Err("Invalid cursor".to_owned()),
    }
}

fn default_tools() -> Vec<Value> {
    vec![
        json!({
            "name": "echo",
            "title": "Echo",
            "description": "Return the message it was given.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "Text to echo back."},
                },
                "required": ["message"],
                "additionalProperties": false,
            },
        }),
        json!({
            "name": "add",
            "title": "Add two numbers",
            "description": "Add two numbers and return the sum.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "a": {"type": "number", "description": "First addend."},
                    "b": {"type": "number", "description": "Second addend."},
                },
                "required": ["a", "b"],
                "additionalProperties": false,
            },
        }),
        json!({
            "name": "slow",
            "title": "Slow tool",
            "description": "Wait for a while and stream progress when the client accepts SSE.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "delay_ms": {"type": "integer", "minimum": 0, "maximum": 60000, "default": 100},
                    "steps": {"type": "integer", "minimum": 1, "maximum": 100, "default": 4},
                },
                "additionalProperties": false,
            },
        }),
        json!({
            "name": "ask",
            "title": "Ask the client",
            "description": "Send an elicitation/create request on this POST's SSE stream and return the client's answer.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "Question shown to the user."},
                },
                "required": ["question"],
                "additionalProperties": false,
            },
        }),
        json!({
            "name": "whoami",
            "title": "Who am I",
            "description": "Report the Authorization, X-Lumewarp-Key, session and every other header this request carried.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": false},
        }),
        json!({
            "name": "admin_only",
            "title": "Admin only",
            "description": "Listed only when the role header names an administrator.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": false},
            "_meta": {"mock/adminOnly": true},
        }),
    ]
}

fn default_prompts() -> Vec<Value> {
    vec![json!({
        "name": "greet",
        "title": "Greet someone",
        "description": "A friendly greeting prompt.",
        "arguments": [
            {"name": "name", "description": "Who to greet.", "required": true},
        ],
    })]
}
