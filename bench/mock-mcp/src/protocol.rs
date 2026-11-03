//! MCP protocol handling for both eras, plus the SSE/tool-call machinery.

use crate::instance::{InFlight, Instance};
use crate::model::{
    Auth, Behaviour, Era, Fault, FaultKind, RequestRecord, SessionMode, Shape, LEGACY_VERSION,
    MODERN_VERSION,
};
use crate::records;
use crate::state::{HeaderKind, InstanceState, ParamHeader, Tool};
use crate::{App, Body};
use base64::Engine as _;
use bytes::Bytes;
use flate2::write::GzEncoder;
use flate2::Compression;
use futures_util::Stream;
use http_body_util::{BodyExt, Full, StreamBody};
use hyper::body::Frame;
use hyper::header::{HeaderMap, HeaderValue};
use hyper::http::request::Parts;
use hyper::{Method, Response};
use serde_json::{json, Map, Value};
use std::borrow::Cow;
use std::io::{ErrorKind, Write};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio::sync::mpsc;

const SSE_CONTENT_TYPE: &str = "text/event-stream";

#[derive(Debug)]
struct RpcError {
    status: u16,
    code: i64,
    message: String,
    data: Option<Value>,
}

impl RpcError {
    fn new(code: i64, message: impl Into<String>) -> Self {
        Self {
            status: 400,
            code,
            message: message.into(),
            data: None,
        }
    }
}

struct Rpc<'a> {
    id: Option<&'a Value>,
    method: &'a str,
    params: Option<&'a Value>,
}

fn parse_rpc(value: &Value) -> Result<Rpc<'_>, RpcError> {
    let Some(object) = value.as_object() else {
        return Err(RpcError::new(-32600, "Invalid Request"));
    };
    if object.get("jsonrpc").and_then(Value::as_str) != Some("2.0") {
        return Err(RpcError::new(
            -32600,
            "Invalid Request: jsonrpc must be 2.0",
        ));
    }
    let Some(method) = object.get("method").and_then(Value::as_str) else {
        return Err(RpcError::new(
            -32600,
            "Invalid Request: method must be a string",
        ));
    };
    let id = match object.get("id") {
        None => None,
        Some(Value::String(_)) => object.get("id"),
        Some(Value::Number(number)) if number.is_i64() || number.is_u64() => object.get("id"),
        Some(Value::Null) => {
            return Err(RpcError::new(
                -32600,
                "Invalid Request: id must not be null",
            ))
        }
        Some(_) => {
            return Err(RpcError::new(
                -32600,
                "Invalid Request: id must be a string or integer",
            ))
        }
    };
    let params = match object.get("params") {
        None => None,
        Some(value @ Value::Object(_)) => Some(value),
        Some(_) => {
            return Err(RpcError::new(
                -32602,
                "Invalid params: params must be an object",
            ))
        }
    };
    Ok(Rpc { id, method, params })
}

fn request_id_key(id: &Value) -> String {
    match id {
        Value::String(value) => format!("s:{value}"),
        Value::Number(number) => format!("n:{number}"),
        _ => "invalid".into(),
    }
}

/// Metadata filled into a record before the protocol layer runs.
pub fn quick_metadata(
    value: &Value,
) -> (
    Option<String>,
    Option<Value>,
    Option<String>,
    Option<String>,
) {
    let method = value
        .get("method")
        .and_then(Value::as_str)
        .map(str::to_string);
    let id = value.get("id").cloned();
    let tool = value
        .pointer("/params/name")
        .and_then(Value::as_str)
        .map(str::to_string);
    let version = value
        .pointer("/params/_meta/io.modelcontextprotocol/protocolVersion")
        .and_then(Value::as_str)
        .or_else(|| {
            value
                .pointer("/params/protocolVersion")
                .and_then(Value::as_str)
        })
        .map(str::to_string);
    (method, id, tool, version)
}

pub async fn handle(
    _app: &App,
    instance: Arc<Instance>,
    parts: Parts,
    body: Bytes,
    parsed: Option<Value>,
    record: Option<Arc<Mutex<RequestRecord>>>,
) -> Response<Body> {
    let _ = body;
    let state = instance.snapshot();
    let era = state.era();

    if !origin_allowed(&parts.headers, &state) {
        records::annotate(&record, |record| {
            record.notes = Some("origin rejected".into());
        });
        return rpc_error_http(403, None, -32600, "Origin not allowed", None, &record, None);
    }
    if !authorized(&parts.headers, &state) {
        return unauthorized_response(None, &record);
    }

    if era == Era::Legacy && parts.method == Method::GET {
        return legacy_get(&instance, &state, &parts, &record);
    }
    if parts.method != Method::POST {
        return method_not_allowed(&record);
    }

    let Some(body_value) = parsed else {
        return rpc_error_http(400, None, -32700, "Parse error", None, &record, None);
    };
    let rpc = match parse_rpc(&body_value) {
        Ok(rpc) => rpc,
        Err(error) => {
            return rpc_error_http(
                error.status,
                body_value.get("id"),
                error.code,
                &error.message,
                error.data,
                &record,
                None,
            )
        }
    };
    records::annotate(&record, |record| {
        record.method = Some(rpc.method.to_string());
        record.request_id = rpc.id.cloned();
        record.era = Some(era);
    });

    match era {
        Era::Modern => modern(&instance, &state, &parts, rpc, &record).await,
        Era::Legacy => legacy(&instance, &state, &parts, rpc, &record).await,
    }
}

fn origin_allowed(headers: &HeaderMap, state: &InstanceState) -> bool {
    let mut origins = headers.get_all(hyper::header::ORIGIN).iter();
    let Some(origin) = origins.next() else {
        return true;
    };
    if origins.next().is_some() {
        return false;
    }
    let Ok(origin) = origin.to_str() else {
        return false;
    };
    state
        .config
        .allowed_origins
        .iter()
        .any(|allowed| allowed == origin)
}

fn authorized(headers: &HeaderMap, state: &InstanceState) -> bool {
    match &state.config.auth {
        Auth::None => true,
        Auth::Bearer { token } => {
            let expected = format!("Bearer {token}");
            let mut values = headers.get_all(hyper::header::AUTHORIZATION).iter();
            let Some(value) = values.next() else {
                return false;
            };
            values.next().is_none() && value.as_bytes() == expected.as_bytes()
        }
    }
}

fn unauthorized_response(
    fault: Option<&Fault>,
    record: &Option<Arc<Mutex<RequestRecord>>>,
) -> Response<Body> {
    let challenge = fault
        .map(Fault::www_authenticate)
        .unwrap_or("Bearer realm=\"mock-mcp\"");
    let body = json!({
        "error": "invalid_token",
        "error_description": "the bearer token is missing or does not match this instance",
    });
    records::finish(
        record,
        "unauthorized",
        401,
        "application/json",
        None,
        Some(FaultKind::Unauthorized.as_str()),
    );
    let mut response = json_response(401, &body);
    response.headers_mut().insert(
        hyper::header::WWW_AUTHENTICATE,
        HeaderValue::from_str(challenge).expect("validated challenge"),
    );
    response
}

fn server_error_response(
    fault: &Fault,
    record: &Option<Arc<Mutex<RequestRecord>>>,
) -> Response<Body> {
    let status = fault.status();
    let body = json!({"error": "injected_fault", "status": status});
    records::finish(
        record,
        "fault",
        status,
        "application/json",
        None,
        Some(fault.kind.as_str()),
    );
    json_response(status, &body)
}

fn method_not_allowed(record: &Option<Arc<Mutex<RequestRecord>>>) -> Response<Body> {
    records::finish(record, "method_not_allowed", 405, "text/plain", None, None);
    let mut response = empty_response(405);
    response
        .headers_mut()
        .insert(hyper::header::ALLOW, HeaderValue::from_static("POST"));
    response
}

fn header_single<'a>(
    headers: &'a HeaderMap,
    name: &str,
) -> Result<Option<&'a HeaderValue>, String> {
    let mut values = headers.get_all(name).iter();
    let Some(value) = values.next() else {
        return Ok(None);
    };
    if values.next().is_some() {
        return Err(format!("duplicate {name} header"));
    }
    Ok(Some(value))
}

fn decode_sentinel(bytes: &[u8]) -> Result<Cow<'_, [u8]>, ()> {
    let Some(rest) = bytes.strip_prefix(b"=?base64?") else {
        return Ok(Cow::Borrowed(bytes));
    };
    let Some(encoded) = rest.strip_suffix(b"?=") else {
        return Err(());
    };
    if encoded.is_empty() {
        return Ok(Cow::Owned(Vec::new()));
    }
    match base64::engine::general_purpose::STANDARD.decode(encoded) {
        Ok(decoded) => Ok(Cow::Owned(decoded)),
        Err(_) => base64::engine::general_purpose::STANDARD_NO_PAD
            .decode(encoded)
            .map(Cow::Owned)
            .map_err(|_| ()),
    }
}

fn header_safe(value: &str) -> bool {
    if value.is_empty() {
        return true;
    }
    let bytes = value.as_bytes();
    if matches!(bytes[0], b' ' | b'\t') || matches!(bytes[bytes.len() - 1], b' ' | b'\t') {
        return false;
    }
    bytes
        .iter()
        .all(|byte| matches!(*byte, b'\t' | 0x20..=0x7E))
}

fn value_at<'a>(root: &'a Value, path: &[String]) -> Option<&'a Value> {
    let mut current = root;
    for key in path {
        current = current.get(key)?;
    }
    Some(current)
}

fn decoded_header(header: &[u8]) -> Option<Cow<'_, [u8]>> {
    if header.starts_with(b"=?base64?") {
        decode_sentinel(header).ok()
    } else {
        Some(Cow::Borrowed(header))
    }
}

fn matches_param(kind: HeaderKind, body_value: &Value, header: &[u8]) -> bool {
    match kind {
        HeaderKind::String => {
            let Some(expected) = body_value.as_str() else {
                return false;
            };
            if header.starts_with(b"=?base64?") {
                decoded_header(header)
                    .map(|decoded| decoded.as_ref() == expected.as_bytes())
                    .unwrap_or(false)
            } else {
                header_safe(expected) && header == expected.as_bytes()
            }
        }
        HeaderKind::Integer => {
            let Some(expected) = body_value.as_f64() else {
                return false;
            };
            if !expected.is_finite() {
                return false;
            }
            let Some(decoded) = decoded_header(header) else {
                return false;
            };
            let Ok(text) = std::str::from_utf8(decoded.as_ref()) else {
                return false;
            };
            let Ok(actual) = text.parse::<f64>() else {
                return false;
            };
            actual.is_finite() && actual == expected
        }
        HeaderKind::Boolean => {
            let Some(expected) = body_value.as_bool() else {
                return false;
            };
            let Some(decoded) = decoded_header(header) else {
                return false;
            };
            decoded.as_ref()
                == (if expected {
                    b"true".as_slice()
                } else {
                    b"false".as_slice()
                })
        }
    }
}

fn validate_param_headers(
    headers: &HeaderMap,
    tool: &Tool,
    arguments: &Value,
) -> Result<(), String> {
    for ParamHeader { path, name, kind } in &tool.param_headers {
        let header_name = format!("mcp-param-{}", name.to_ascii_lowercase());
        let values: Vec<&HeaderValue> = headers.get_all(header_name.as_str()).iter().collect();
        let value = value_at(arguments, path).filter(|value| !value.is_null());
        match value {
            None => {
                if !values.is_empty() {
                    return Err(format!(
                        "Mcp-Param-{name} is present but the parameter is absent or null"
                    ));
                }
            }
            Some(value) => {
                if values.len() > 1 {
                    return Err(format!("duplicate Mcp-Param-{name} header"));
                }
                let Some(header) = values.first() else {
                    return Err(format!("missing Mcp-Param-{name} header"));
                };
                if !matches_param(*kind, value, header.as_bytes()) {
                    return Err(format!("Mcp-Param-{name} header does not match the body"));
                }
            }
        }
    }
    Ok(())
}

type HeaderError = (u16, i64, String, Option<Value>);

fn validate_modern_headers(
    headers: &HeaderMap,
    params: Option<&Value>,
    method: &str,
    state: &InstanceState,
) -> Result<Option<Arc<Tool>>, HeaderError> {
    let Some(params) = params else {
        return Err((
            400,
            -32602,
            "Invalid params: params is required".into(),
            None,
        ));
    };
    let Some(meta) = params.get("_meta").and_then(Value::as_object) else {
        return Err((
            400,
            -32602,
            "Invalid params: params._meta is required on a modern request".into(),
            None,
        ));
    };
    let Some(version) = meta
        .get("io.modelcontextprotocol/protocolVersion")
        .and_then(Value::as_str)
    else {
        return Err((
            400,
            -32602,
            "Invalid params: missing io.modelcontextprotocol/protocolVersion in _meta".into(),
            None,
        ));
    };
    let Some(client_capabilities) = meta.get("io.modelcontextprotocol/clientCapabilities") else {
        return Err((
            400,
            -32602,
            "Invalid params: missing io.modelcontextprotocol/clientCapabilities in _meta".into(),
            None,
        ));
    };
    if !client_capabilities.is_object() {
        return Err((
            400,
            -32602,
            "Invalid params: io.modelcontextprotocol/clientCapabilities must be an object".into(),
            None,
        ));
    }
    if version != MODERN_VERSION {
        return Err((
            400,
            -32022,
            "Unsupported protocol version".into(),
            Some(json!({"supported": [MODERN_VERSION], "requested": version})),
        ));
    }

    let version_header = header_single(headers, "mcp-protocol-version")
        .map_err(|message| (400, -32020, format!("Header mismatch: {message}"), None))?;
    let Some(version_header) = version_header else {
        return Err((
            400,
            -32020,
            "Header mismatch: missing MCP-Protocol-Version".into(),
            None,
        ));
    };
    let Ok(version_header) = version_header.to_str() else {
        return Err((
            400,
            -32020,
            "Header mismatch: invalid MCP-Protocol-Version".into(),
            None,
        ));
    };
    if version_header != version {
        return Err((
            400,
            -32020,
            format!(
                "Header mismatch: MCP-Protocol-Version '{version_header}' does not match body value '{version}'"
            ),
            None,
        ));
    }

    let method_header = header_single(headers, "mcp-method")
        .map_err(|message| (400, -32020, format!("Header mismatch: {message}"), None))?;
    let Some(method_header) = method_header else {
        return Err((
            400,
            -32020,
            "Header mismatch: missing Mcp-Method".into(),
            None,
        ));
    };
    let Ok(method_header) = method_header.to_str() else {
        return Err((
            400,
            -32020,
            "Header mismatch: invalid Mcp-Method".into(),
            None,
        ));
    };
    if method_header != method {
        return Err((
            400,
            -32020,
            format!(
                "Header mismatch: Mcp-Method '{method_header}' does not match body value '{method}'"
            ),
            None,
        ));
    }

    if method != "tools/call" {
        return Ok(None);
    }

    let Some(name) = params.get("name").and_then(Value::as_str) else {
        return Err((
            400,
            -32602,
            "Invalid params: tools/call params.name must be a string".into(),
            None,
        ));
    };
    let name_header = header_single(headers, "mcp-name")
        .map_err(|message| (400, -32020, format!("Header mismatch: {message}"), None))?;
    let Some(name_header) = name_header else {
        return Err((
            400,
            -32020,
            "Header mismatch: missing Mcp-Name".into(),
            None,
        ));
    };
    let Some(decoded) = decoded_header(name_header.as_bytes()) else {
        return Err((
            400,
            -32020,
            "Header mismatch: Mcp-Name is not valid base64".into(),
            None,
        ));
    };
    if decoded.as_ref() != name.as_bytes() {
        return Err((
            400,
            -32020,
            format!(
                "Header mismatch: Mcp-Name '{}' does not match body value '{name}'",
                String::from_utf8_lossy(name_header.as_bytes())
            ),
            None,
        ));
    }

    let Some(tool) = state.tool(name) else {
        return Err((200, -32602, format!("Unknown tool: {name}"), None));
    };
    let arguments = params
        .get("arguments")
        .cloned()
        .unwrap_or_else(|| json!({}));
    if !arguments.is_object() {
        return Err((
            200,
            -32602,
            "Invalid params: tools/call arguments must be an object".into(),
            None,
        ));
    }
    if let Err(message) = validate_param_headers(headers, &tool, &arguments) {
        return Err((400, -32020, format!("Header mismatch: {message}"), None));
    }
    Ok(Some(tool))
}

async fn modern(
    instance: &Arc<Instance>,
    state: &Arc<InstanceState>,
    parts: &Parts,
    rpc: Rpc<'_>,
    record: &Option<Arc<Mutex<RequestRecord>>>,
) -> Response<Body> {
    if rpc.id.is_none() {
        if rpc.method == "notifications/cancelled" {
            if let Some(request_id) = rpc.params.and_then(|params| params.get("requestId")) {
                let key = format!("|{}", request_id_key(request_id));
                instance.cancel_in_flight(&key);
            }
        }
        records::finish(record, "notification", 202, "", None, None);
        return empty_response(202);
    }
    let id = rpc.id.expect("checked");

    if rpc.method == "initialize" && rpc.params.and_then(|params| params.get("_meta")).is_none() {
        records::finish(record, "error", 400, "application/json", None, None);
        return rpc_error_http(
            400,
            Some(id),
            -32022,
            "Unsupported protocol version",
            Some(json!({"supported": [MODERN_VERSION], "requested": "legacy-initialize"})),
            record,
            None,
        );
    }
    let tool = match validate_modern_headers(&parts.headers, rpc.params, rpc.method, state) {
        Ok(tool) => tool,
        Err((status, code, message, data)) => {
            records::finish(record, "error", status, "application/json", None, None);
            return rpc_error_http(status, Some(id), code, &message, data, record, None);
        }
    };

    let shape = state.shape();
    let instance_fault = state.config.fault.clone();
    match rpc.method {
        "server/discover" => {
            records::finish(
                record,
                "result",
                200,
                "application/json",
                Some("complete"),
                None,
            );
            respond_simple(
                Some(id),
                Ok(state.discover.clone()),
                shape,
                instance_fault.as_ref(),
                record,
                "complete",
            )
        }
        "tools/list" => {
            records::finish(
                record,
                "result",
                200,
                "application/json",
                Some("complete"),
                None,
            );
            respond_simple(
                Some(id),
                Ok(state.list_for(Era::Modern).clone()),
                shape,
                instance_fault.as_ref(),
                record,
                "complete",
            )
        }
        "tools/call" => {
            let tool = tool.expect("tools/call has a tool");
            records::annotate(record, |record| {
                record.tool = Some(tool.config.name.clone());
            });
            let params = rpc.params.expect("validated params");
            let arguments = params
                .get("arguments")
                .cloned()
                .unwrap_or_else(|| json!({}));
            let meta = params.get("_meta").cloned().unwrap_or(Value::Null);
            let progress_token = meta
                .get("progressToken")
                .filter(|value| value.is_string() || value.is_number())
                .cloned();
            respond_tool(
                instance,
                state,
                Era::Modern,
                id,
                tool,
                params,
                arguments,
                meta,
                progress_token,
                shape,
                None,
                record,
            )
            .await
        }
        _ => {
            records::finish(record, "error", 404, "application/json", None, None);
            rpc_error_http(
                404,
                Some(id),
                -32601,
                &format!("Method not found: {}", rpc.method),
                None,
                record,
                None,
            )
        }
    }
}

async fn legacy(
    instance: &Arc<Instance>,
    state: &Arc<InstanceState>,
    parts: &Parts,
    rpc: Rpc<'_>,
    record: &Option<Arc<Mutex<RequestRecord>>>,
) -> Response<Body> {
    if rpc.id.is_none() {
        if let Err(response) = check_legacy_session(instance, state, &parts.headers, record) {
            return response;
        }
        if rpc.method == "notifications/cancelled" {
            if let Some(request_id) = rpc.params.and_then(|params| params.get("requestId")) {
                let session = session_header(&parts.headers).unwrap_or_default();
                let key = format!("{session}|{}", request_id_key(request_id));
                instance.cancel_in_flight(&key);
            }
        } else if rpc.method == "notifications/initialized" {
            if let Some(session) = session_header(&parts.headers) {
                instance.mark_initialized(&session);
            }
        }
        records::finish(record, "notification", 202, "", None, None);
        return empty_response(202);
    }
    let id = rpc.id.expect("checked");

    if rpc.method == "initialize" {
        match header_single(&parts.headers, "mcp-protocol-version") {
            Ok(Some(header)) => {
                if header.as_bytes() != LEGACY_VERSION.as_bytes() {
                    records::finish(record, "error", 400, "application/json", None, None);
                    return rpc_error_http(
                        400,
                        Some(id),
                        -32600,
                        "Unsupported protocol version",
                        Some(json!({"supported": [LEGACY_VERSION]})),
                        record,
                        None,
                    );
                }
            }
            Ok(None) => {}
            Err(message) => {
                records::finish(record, "error", 400, "application/json", None, None);
                return rpc_error_http(400, Some(id), -32600, &message, None, record, None);
            }
        }
        let Some(requested) = rpc
            .params
            .and_then(|params| params.get("protocolVersion"))
            .and_then(Value::as_str)
        else {
            records::finish(record, "error", 400, "application/json", None, None);
            return rpc_error_http(
                400,
                Some(id),
                -32602,
                "Invalid params: protocolVersion is required",
                None,
                record,
                None,
            );
        };
        let session = instance.new_session();
        let mut result = Map::new();
        result.insert("protocolVersion".into(), json!(LEGACY_VERSION));
        result.insert("capabilities".into(), json!({"tools": {}}));
        result.insert("serverInfo".into(), state.server_info.clone());
        if let Some(instructions) = &state.config.instructions {
            result.insert("instructions".into(), json!(instructions));
        }
        let _ = requested;
        let result =
            Bytes::from(serde_json::to_vec(&Value::Object(result)).expect("serialize initialize"));
        records::annotate(record, |record| {
            record.session_id = session.clone();
        });
        let mut response = respond_simple(
            Some(id),
            Ok(result),
            state.shape(),
            state.config.fault.as_ref(),
            record,
            "initialize",
        );
        if let Some(session) = session {
            if let Ok(value) = HeaderValue::from_str(&session) {
                response.headers_mut().insert("mcp-session-id", value);
            }
            instance.mark_initialized(&session);
        }
        return response;
    }

    match header_single(&parts.headers, "mcp-protocol-version") {
        Ok(Some(header)) => {
            if header.as_bytes() != LEGACY_VERSION.as_bytes() {
                records::finish(record, "error", 400, "application/json", None, None);
                return rpc_error_http(
                    400,
                    Some(id),
                    -32600,
                    "Unsupported protocol version",
                    Some(json!({"supported": [LEGACY_VERSION]})),
                    record,
                    None,
                );
            }
        }
        Ok(None) => {}
        Err(message) => {
            records::finish(record, "error", 400, "application/json", None, None);
            return rpc_error_http(400, Some(id), -32600, &message, None, record, None);
        }
    }

    let session = match check_legacy_session(instance, state, &parts.headers, record) {
        Ok(session) => session,
        Err(response) => return response,
    };
    records::annotate(record, |record| {
        record.session_id = session.clone();
    });

    if state
        .config
        .fault
        .as_ref()
        .is_some_and(|fault| fault.kind == FaultKind::Session404Once)
    {
        if let Some(session) = &session {
            if instance.take_session_expiry() && instance.expire_session(session) {
                return session_not_found(record);
            }
        }
    }

    let shape = state.shape();
    let args = rpc
        .params
        .and_then(|params| params.get("arguments"))
        .cloned()
        .unwrap_or_else(|| json!({}));
    match rpc.method {
        "ping" => {
            records::finish(
                record,
                "result",
                200,
                "application/json",
                Some("complete"),
                None,
            );
            respond_simple(
                Some(id),
                Ok(Bytes::from_static(b"{}")),
                shape,
                state.config.fault.as_ref(),
                record,
                "complete",
            )
        }
        "tools/list" => {
            records::finish(
                record,
                "result",
                200,
                "application/json",
                Some("complete"),
                None,
            );
            respond_simple(
                Some(id),
                Ok(state.list_for(Era::Legacy).clone()),
                shape,
                state.config.fault.as_ref(),
                record,
                "complete",
            )
        }
        "tools/call" => {
            let Some(name) = rpc
                .params
                .and_then(|params| params.get("name"))
                .and_then(Value::as_str)
            else {
                records::finish(record, "error", 400, "application/json", None, None);
                return rpc_error_http(
                    400,
                    Some(id),
                    -32602,
                    "Invalid params: tools/call params.name must be a string",
                    None,
                    record,
                    None,
                );
            };
            let Some(tool) = state.tool(name) else {
                records::finish(record, "error", 200, "application/json", None, None);
                return rpc_error_http(
                    200,
                    Some(id),
                    -32602,
                    &format!("Unknown tool: {name}"),
                    None,
                    record,
                    None,
                );
            };
            records::annotate(record, |record| {
                record.tool = Some(tool.config.name.clone());
            });
            let params = rpc.params.expect("validated params");
            let meta = params.get("_meta").cloned().unwrap_or(Value::Null);
            let progress_token = meta
                .get("progressToken")
                .filter(|value| value.is_string() || value.is_number())
                .cloned();
            respond_tool(
                instance,
                state,
                Era::Legacy,
                id,
                tool,
                params,
                args,
                meta,
                progress_token,
                shape,
                session,
                record,
            )
            .await
        }
        _ => {
            records::finish(record, "error", 200, "application/json", None, None);
            rpc_error_http(
                200,
                Some(id),
                -32601,
                &format!("Method not found: {}", rpc.method),
                None,
                record,
                None,
            )
        }
    }
}

fn session_header(headers: &HeaderMap) -> Option<String> {
    header_single(headers, "mcp-session-id")
        .ok()
        .flatten()
        .and_then(|value| value.to_str().ok())
        .map(str::to_string)
}

#[allow(clippy::result_large_err)]
fn check_legacy_session(
    instance: &Instance,
    state: &InstanceState,
    headers: &HeaderMap,
    record: &Option<Arc<Mutex<RequestRecord>>>,
) -> Result<Option<String>, Response<Body>> {
    let mode = state.config.session;
    let header = match header_single(headers, "mcp-session-id") {
        Ok(header) => header
            .and_then(|value| value.to_str().ok())
            .map(str::to_string),
        Err(_) => {
            records::finish(record, "error", 400, "application/json", None, None);
            return Err(rpc_error_http(
                400,
                None,
                -32600,
                "Duplicate Mcp-Session-Id header",
                None,
                record,
                None,
            ));
        }
    };
    if mode == SessionMode::Off {
        return Ok(None);
    }
    match header {
        Some(session) => {
            if instance.session_exists(&session) {
                Ok(Some(session))
            } else {
                Err(session_not_found(record))
            }
        }
        None if mode == SessionMode::Required => {
            records::finish(record, "error", 400, "application/json", None, None);
            Err(rpc_error_http(
                400,
                None,
                -32600,
                "Mcp-Session-Id is required",
                None,
                record,
                None,
            ))
        }
        None => Ok(None),
    }
}

fn session_not_found(record: &Option<Arc<Mutex<RequestRecord>>>) -> Response<Body> {
    records::finish(
        record,
        "session_unknown",
        404,
        "application/json",
        None,
        None,
    );
    rpc_error_http(404, None, -32600, "Session not found", None, record, None)
}

fn legacy_get(
    instance: &Instance,
    state: &InstanceState,
    parts: &Parts,
    record: &Option<Arc<Mutex<RequestRecord>>>,
) -> Response<Body> {
    if !state.config.get_stream {
        return method_not_allowed(record);
    }
    if let Err(response) = check_legacy_session(instance, state, &parts.headers, record) {
        return response;
    }
    records::annotate(record, |record| {
        record.session_id = session_header(&parts.headers);
        record.status = Some(200);
        record.content_type = Some(SSE_CONTENT_TYPE.into());
        record.outcome = Some("stream".into());
    });
    let done = Arc::new(AtomicBool::new(false));
    let record = record.as_ref().map(Arc::clone);
    let stream = async_stream::stream! {
        let _guard = StreamGuard {
            record: record.clone(),
            done: done.clone(),
        };
        yield Ok::<_, crate::BoxError>(Frame::data(Bytes::from_static(
            b": mock-mcp stream open\n\n",
        )));
        let mut interval = tokio::time::interval(Duration::from_secs(10));
        interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        loop {
            interval.tick().await;
            yield Ok(Frame::data(Bytes::from_static(b": keep-alive\n\n")));
        }
    };
    sse_response(stream)
}

struct StreamGuard {
    record: Option<Arc<Mutex<RequestRecord>>>,
    done: Arc<AtomicBool>,
}

impl Drop for StreamGuard {
    fn drop(&mut self) {
        if !self.done.load(Ordering::SeqCst) {
            records::mark_disconnected(&self.record);
            records::finish(
                &self.record,
                "disconnected",
                200,
                SSE_CONTENT_TYPE,
                None,
                None,
            );
        }
    }
}

fn rpc_error_value(id: Option<&Value>, code: i64, message: &str, data: Option<Value>) -> Value {
    let mut object = Map::new();
    object.insert("jsonrpc".into(), json!("2.0"));
    if let Some(id) = id {
        object.insert("id".into(), id.clone());
    }
    let mut error = Map::new();
    error.insert("code".into(), json!(code));
    error.insert("message".into(), json!(message));
    if let Some(data) = data {
        error.insert("data".into(), data);
    }
    object.insert("error".into(), Value::Object(error));
    Value::Object(object)
}

fn rpc_error_value_from(id: Option<&Value>, error: &Value) -> Value {
    let mut object = Map::new();
    object.insert("jsonrpc".into(), json!("2.0"));
    if let Some(id) = id {
        object.insert("id".into(), id.clone());
    }
    object.insert("error".into(), error.clone());
    Value::Object(object)
}

fn bytes_of(value: &Value) -> Bytes {
    Bytes::from(serde_json::to_vec(value).expect("serialize JSON value"))
}

fn rpc_result_bytes(id: &Value, result: &[u8]) -> Bytes {
    let mut out = Vec::with_capacity(result.len() + 64);
    out.extend_from_slice(b"{\"jsonrpc\":\"2.0\",\"id\":");
    serde_json::to_writer(&mut out, id).expect("serialize id");
    out.extend_from_slice(b",\"result\":");
    out.extend_from_slice(result);
    out.push(b'}');
    Bytes::from(out)
}

fn rpc_result_value_bytes(id: &Value, result: &Value) -> Bytes {
    let raw = serde_json::to_vec(result).expect("serialize result");
    rpc_result_bytes(id, &raw)
}

fn rpc_error_http(
    status: u16,
    id: Option<&Value>,
    code: i64,
    message: &str,
    data: Option<Value>,
    record: &Option<Arc<Mutex<RequestRecord>>>,
    fault: Option<&str>,
) -> Response<Body> {
    records::finish(
        record,
        if status >= 400 {
            "error"
        } else {
            "protocol_error"
        },
        status,
        "application/json",
        None,
        fault,
    );
    let value = rpc_error_value(id, code, message, data);
    json_response(status, &value)
}

fn respond_simple(
    id: Option<&Value>,
    result: Result<Bytes, Value>,
    shape: Shape,
    fault: Option<&Fault>,
    record: &Option<Arc<Mutex<RequestRecord>>>,
    result_type: &str,
) -> Response<Body> {
    if let Some(fault) = fault {
        match fault.kind {
            FaultKind::Unauthorized => return unauthorized_response(Some(fault), record),
            FaultKind::ServerError => return server_error_response(fault, record),
            _ => {}
        }
    }
    let envelope = match &result {
        Ok(result) => {
            let id = id.expect("a result has an id");
            rpc_result_bytes(id, result)
        }
        Err(error) => bytes_of(&rpc_error_value_from(id, error)),
    };
    let mut shape = shape;
    if let Some(fault) = fault {
        if fault.kind.is_stream() {
            shape = Shape::Sse;
        } else if matches!(
            fault.kind,
            FaultKind::Compressed | FaultKind::WrongContentType
        ) {
            shape = Shape::Json;
        }
    }
    match shape {
        Shape::Json => {
            let (content_type, body, extra) = match fault {
                Some(fault) if fault.kind == FaultKind::Compressed => (
                    "application/json",
                    gzip(&envelope),
                    Some(("content-encoding", "gzip")),
                ),
                Some(fault) if fault.kind == FaultKind::WrongContentType => {
                    (fault.content_type(), envelope, None)
                }
                _ => ("application/json", envelope, None),
            };
            let response = bytes_response(
                200,
                content_type,
                body,
                extra.map(|(k, v)| (k, v.to_string())),
            );
            records::finish(
                record,
                if result.is_ok() { "result" } else { "error" },
                200,
                content_type,
                result.is_ok().then_some(result_type),
                fault.map(|fault| fault.kind.as_str()),
            );
            response
        }
        Shape::Sse => {
            let done = Arc::new(AtomicBool::new(false));
            let record_for_stream = record.as_ref().map(Arc::clone);
            let fault_for_stream = fault.cloned();
            let stream = async_stream::stream! {
                let _guard = StreamGuard {
                    record: record_for_stream.clone(),
                    done: done.clone(),
                };
                if let Some(fault) = fault_for_stream.as_ref() {
                    match fault.kind {
                        FaultKind::OversizedSseEvent => {
                            let huge = "x".repeat(fault.oversized_bytes());
                            let value = json!({
                                "jsonrpc": "2.0",
                                "method": "notifications/progress",
                                "params": {
                                    "progressToken": "oversized",
                                    "progress": 1,
                                    "total": 1,
                                    "message": huge,
                                },
                            });
                            yield Ok::<_, crate::BoxError>(Frame::data(sse_json(&value)));
                            done.store(true, Ordering::SeqCst);
                            return;
                        }
                        FaultKind::InvalidSse => {
                            yield Ok(Frame::data(Bytes::from_static(
                                b"data: {\"jsonrpc\":\"2.0\",\"id\":",
                            )));
                            done.store(true, Ordering::SeqCst);
                            return;
                        }
                        FaultKind::StalledStream => {
                            yield Ok(Frame::data(Bytes::from_static(b": stalled\n\n")));
                            tokio::time::sleep(Duration::from_secs(86_400)).await;
                            return;
                        }
                        FaultKind::SlowReads => {
                            for _ in 0..fault.slow_events() {
                                yield Ok(Frame::data(Bytes::from_static(b": slow\n\n")));
                                tokio::time::sleep(Duration::from_millis(fault.slow_interval_ms())).await;
                            }
                        }
                        FaultKind::ResetMidStream => {
                            for _ in 0..fault.after_events() {
                                yield Ok(Frame::data(Bytes::from_static(b": before-reset\n\n")));
                            }
                            yield Err(crate::BoxError::from(std::io::Error::new(
                                ErrorKind::ConnectionReset,
                                "injected connection reset mid-stream",
                            )));
                            done.store(true, Ordering::SeqCst);
                            return;
                        }
                        _ => {}
                    }
                }
                let mut event = Vec::with_capacity(envelope.len() + 8);
                event.extend_from_slice(b"data: ");
                event.extend_from_slice(&envelope);
                event.extend_from_slice(b"\n\n");
                done.store(true, Ordering::SeqCst);
                yield Ok(Frame::data(Bytes::from(event)));
            };
            records::finish(
                record,
                if fault.is_some_and(|fault| fault.kind.is_stream()) {
                    "fault"
                } else if result.is_ok() {
                    "result"
                } else {
                    "error"
                },
                200,
                SSE_CONTENT_TYPE,
                result.is_ok().then_some(result_type),
                fault.map(|fault| fault.kind.as_str()),
            );
            sse_response(stream)
        }
    }
}

fn gzip(bytes: &Bytes) -> Bytes {
    let mut encoder = GzEncoder::new(Vec::new(), Compression::fast());
    encoder.write_all(bytes).expect("gzip write");
    Bytes::from(encoder.finish().expect("gzip finish"))
}

fn sse_json(value: &Value) -> Bytes {
    let raw = serde_json::to_vec(value).expect("serialize SSE event");
    let mut out = Vec::with_capacity(raw.len() + 8);
    out.extend_from_slice(b"data: ");
    out.extend_from_slice(&raw);
    out.extend_from_slice(b"\n\n");
    Bytes::from(out)
}

fn sse_data(raw: &[u8]) -> Bytes {
    let mut out = Vec::with_capacity(raw.len() + 8);
    out.extend_from_slice(b"data: ");
    out.extend_from_slice(raw);
    out.extend_from_slice(b"\n\n");
    Bytes::from(out)
}

fn json_response(status: u16, value: &Value) -> Response<Body> {
    bytes_response(
        status,
        "application/json",
        bytes_of(value),
        None::<(&str, String)>,
    )
}

fn empty_response(status: u16) -> Response<Body> {
    Response::builder()
        .status(status)
        .body(
            Full::new(Bytes::new())
                .map_err(|never| match never {})
                .boxed_unsync(),
        )
        .expect("static response")
}

fn bytes_response(
    status: u16,
    content_type: &str,
    bytes: Bytes,
    extra: Option<(&str, String)>,
) -> Response<Body> {
    let mut builder = Response::builder()
        .status(status)
        .header("content-type", content_type);
    if let Some((name, value)) = extra {
        builder = builder.header(name, value);
    }
    builder
        .body(
            Full::new(bytes)
                .map_err(|never| match never {})
                .boxed_unsync(),
        )
        .expect("valid response")
}

fn sse_response(
    stream: impl Stream<Item = Result<Frame<Bytes>, crate::BoxError>> + Send + 'static,
) -> Response<Body> {
    Response::builder()
        .status(200)
        .header("content-type", SSE_CONTENT_TYPE)
        .header("cache-control", "no-cache")
        .header("x-accel-buffering", "no")
        .body(StreamBody::new(stream).boxed_unsync())
        .expect("valid SSE response")
}

enum CallEvent {
    Progress(Value),
    Final(Value),
    Error(Value),
    Comment(&'static str),
    Oversized(usize),
    Invalid,
    Reset,
    Cancelled,
}

struct ProducerSpec {
    sse: bool,
    result: Option<Value>,
    error: Option<Value>,
    result_type: Option<String>,
    outcome: String,
    fault: Option<Fault>,
    progress_events: u32,
    progress_interval_ms: u64,
    sleep_ms: u64,
    progress_token: Option<Value>,
}

async fn sleep_or_cancel(ms: u64, token: &tokio_util::sync::CancellationToken) -> bool {
    if ms == 0 {
        return token.is_cancelled();
    }
    tokio::select! {
        _ = tokio::time::sleep(Duration::from_millis(ms)) => false,
        _ = token.cancelled() => true,
    }
}

enum SendOutcome {
    Sent,
    Cancelled,
    Closed,
}

async fn send_or_cancel(
    tx: &mpsc::Sender<CallEvent>,
    token: &tokio_util::sync::CancellationToken,
    event: CallEvent,
) -> SendOutcome {
    tokio::select! {
        result = tx.send(event) => if result.is_ok() {
            SendOutcome::Sent
        } else {
            SendOutcome::Closed
        },
        _ = token.cancelled() => SendOutcome::Cancelled,
    }
}

fn content_type(sse: bool) -> &'static str {
    if sse {
        SSE_CONTENT_TYPE
    } else {
        "application/json"
    }
}

fn spawn_producer(
    instance: &Arc<Instance>,
    key: String,
    record: Option<Arc<Mutex<RequestRecord>>>,
    spec: ProducerSpec,
) -> (mpsc::Receiver<CallEvent>, Arc<InFlight>) {
    let (tx, rx) = mpsc::channel(16);
    let inflight = InFlight::new(key.clone(), record, instance.clone());
    instance.register_in_flight(key, &inflight);
    let producer = inflight.clone();
    tokio::spawn(async move {
        run_producer(tx, producer, spec).await;
    });
    (rx, inflight)
}

async fn run_producer(tx: mpsc::Sender<CallEvent>, inflight: Arc<InFlight>, spec: ProducerSpec) {
    let token = inflight.token();
    let content_type = content_type(spec.sse);
    let mut gone = false;

    if let Some(fault) = &spec.fault {
        match fault.kind {
            FaultKind::OversizedSseEvent => {
                let _ = tx.send(CallEvent::Oversized(fault.oversized_bytes())).await;
                inflight.finish(
                    "fault",
                    Some(200),
                    Some(SSE_CONTENT_TYPE),
                    None,
                    Some(fault.kind.as_str()),
                );
                return;
            }
            FaultKind::InvalidSse => {
                let _ = tx.send(CallEvent::Invalid).await;
                inflight.finish(
                    "fault",
                    Some(200),
                    Some(SSE_CONTENT_TYPE),
                    None,
                    Some(fault.kind.as_str()),
                );
                return;
            }
            FaultKind::StalledStream => {
                if tx.send(CallEvent::Comment("stalled")).await.is_err() {
                    inflight.finish("disconnected", None, Some(SSE_CONTENT_TYPE), None, None);
                    return;
                }
                tokio::select! {
                    _ = token.cancelled() => inflight.finish_cancelled(),
                    _ = tx.closed() => inflight.finish(
                        "disconnected",
                        None,
                        Some(SSE_CONTENT_TYPE),
                        None,
                        Some(fault.kind.as_str()),
                    ),
                    _ = tokio::time::sleep(Duration::from_secs(86_400)) => inflight.finish(
                        "fault",
                        Some(200),
                        Some(SSE_CONTENT_TYPE),
                        None,
                        Some(fault.kind.as_str()),
                    ),
                }
                return;
            }
            FaultKind::SlowReads => {
                for _ in 0..fault.slow_events() {
                    match send_or_cancel(&tx, &token, CallEvent::Comment("slow")).await {
                        SendOutcome::Sent => {}
                        SendOutcome::Cancelled => {
                            inflight.finish_cancelled();
                            return;
                        }
                        SendOutcome::Closed => {
                            gone = true;
                            break;
                        }
                    }
                    if sleep_or_cancel(fault.slow_interval_ms(), &token).await {
                        inflight.finish_cancelled();
                        return;
                    }
                }
            }
            FaultKind::ResetMidStream => {
                for _ in 0..fault.after_events() {
                    match send_or_cancel(&tx, &token, CallEvent::Comment("before-reset")).await {
                        SendOutcome::Sent => {}
                        SendOutcome::Cancelled => {
                            inflight.finish_cancelled();
                            return;
                        }
                        SendOutcome::Closed => {
                            gone = true;
                            break;
                        }
                    }
                }
                if !gone {
                    let _ = tx.send(CallEvent::Reset).await;
                }
                inflight.finish(
                    "fault",
                    Some(200),
                    Some(SSE_CONTENT_TYPE),
                    None,
                    Some(fault.kind.as_str()),
                );
                return;
            }
            _ => {}
        }
    }

    if spec.result_type.as_deref() != Some("input_required")
        && spec.error.is_none()
        && spec.progress_events > 0
    {
        if let Some(token_value) = &spec.progress_token {
            for index in 1..=spec.progress_events {
                let notification = json!({
                    "jsonrpc": "2.0",
                    "method": "notifications/progress",
                    "params": {
                        "progressToken": token_value,
                        "progress": index,
                        "total": spec.progress_events,
                        "message": format!("step {index}/{}", spec.progress_events),
                    },
                });
                match send_or_cancel(&tx, &token, CallEvent::Progress(notification)).await {
                    SendOutcome::Sent => {}
                    SendOutcome::Cancelled => {
                        inflight.finish_cancelled();
                        let _ = tx.send(CallEvent::Cancelled).await;
                        return;
                    }
                    SendOutcome::Closed => {
                        gone = true;
                        break;
                    }
                }
                if sleep_or_cancel(spec.progress_interval_ms, &token).await {
                    inflight.finish_cancelled();
                    let _ = tx.send(CallEvent::Cancelled).await;
                    return;
                }
            }
        }
    }

    if spec.sleep_ms > 0 && sleep_or_cancel(spec.sleep_ms, &token).await {
        inflight.finish_cancelled();
        let _ = tx.send(CallEvent::Cancelled).await;
        return;
    }

    let event = match spec.error {
        Some(error) => CallEvent::Error(error),
        None => CallEvent::Final(spec.result.unwrap_or(Value::Null)),
    };
    if !gone {
        let _ = tx.send(event).await;
    }
    inflight.finish(
        &spec.outcome,
        Some(200),
        Some(content_type),
        spec.result_type.as_deref(),
        spec.fault.as_ref().map(|fault| fault.kind.as_str()),
    );
}

struct ToolStreamGuard {
    inflight: Arc<InFlight>,
    era: Era,
    record: Option<Arc<Mutex<RequestRecord>>>,
}

impl Drop for ToolStreamGuard {
    fn drop(&mut self) {
        if self.inflight.is_finished() {
            return;
        }
        records::mark_disconnected(&self.record);
        if self.era == Era::Modern {
            self.inflight.cancel("disconnect");
        }
    }
}

fn tool_sse(
    id: &Value,
    rx: mpsc::Receiver<CallEvent>,
    inflight: Arc<InFlight>,
    era: Era,
    record: &Option<Arc<Mutex<RequestRecord>>>,
) -> Response<Body> {
    let id = id.clone();
    let record = record.clone();
    let stream = async_stream::stream! {
        let mut rx = rx;
        let _guard = ToolStreamGuard {
            inflight,
            era,
            record,
        };
        while let Some(event) = rx.recv().await {
            match event {
                CallEvent::Progress(value) => {
                    yield Ok::<_, crate::BoxError>(Frame::data(sse_json(&value)));
                }
                CallEvent::Final(value) => {
                    yield Ok(Frame::data(sse_data(&rpc_result_value_bytes(&id, &value))));
                    return;
                }
                CallEvent::Error(error) => {
                    yield Ok(Frame::data(sse_data(&bytes_of(&rpc_error_value_from(
                        Some(&id),
                        &error,
                    )))));
                    return;
                }
                CallEvent::Comment(text) => {
                    yield Ok(Frame::data(Bytes::from(format!(": {text}\n\n"))));
                }
                CallEvent::Oversized(bytes) => {
                    let huge = "x".repeat(bytes);
                    let value = json!({
                        "jsonrpc": "2.0",
                        "method": "notifications/progress",
                        "params": {
                            "progressToken": "oversized",
                            "progress": 1,
                            "total": 1,
                            "message": huge,
                        },
                    });
                    yield Ok(Frame::data(sse_json(&value)));
                    return;
                }
                CallEvent::Invalid => {
                    yield Ok(Frame::data(Bytes::from_static(
                        b"data: {\"jsonrpc\":\"2.0\",\"id\":",
                    )));
                    return;
                }
                CallEvent::Reset => {
                    yield Err(crate::BoxError::from(std::io::Error::new(
                        ErrorKind::ConnectionReset,
                        "injected connection reset mid-stream",
                    )));
                    return;
                }
                CallEvent::Cancelled => return,
            }
        }
    };
    sse_response(stream)
}

struct JsonGuard {
    inflight: Arc<InFlight>,
    record: Option<Arc<Mutex<RequestRecord>>>,
}

impl Drop for JsonGuard {
    fn drop(&mut self) {
        if !self.inflight.is_finished() {
            records::mark_disconnected(&self.record);
        }
    }
}

async fn await_tool_json(
    id: &Value,
    mut rx: mpsc::Receiver<CallEvent>,
    inflight: Arc<InFlight>,
    fault: Option<&Fault>,
    record: &Option<Arc<Mutex<RequestRecord>>>,
) -> Response<Body> {
    let _guard = JsonGuard {
        inflight,
        record: record.clone(),
    };
    loop {
        match rx.recv().await {
            Some(CallEvent::Final(value)) => {
                let envelope = rpc_result_value_bytes(id, &value);
                let (content_type, body, extra) = match fault {
                    Some(fault) if fault.kind == FaultKind::Compressed => (
                        "application/json",
                        gzip(&envelope),
                        Some(("content-encoding", "gzip")),
                    ),
                    Some(fault) if fault.kind == FaultKind::WrongContentType => {
                        (fault.content_type(), envelope, None)
                    }
                    _ => ("application/json", envelope, None),
                };
                records::annotate(record, |record| {
                    record.content_type = Some(content_type.into());
                });
                return bytes_response(
                    200,
                    content_type,
                    body,
                    extra.map(|(name, value)| (name, value.to_string())),
                );
            }
            Some(CallEvent::Error(error)) => {
                let envelope = bytes_of(&rpc_error_value_from(Some(id), &error));
                return bytes_response(200, "application/json", envelope, None::<(&str, String)>);
            }
            Some(CallEvent::Cancelled) => return empty_response(202),
            Some(CallEvent::Reset) => {
                records::annotate(record, |record| {
                    record.outcome = Some("fault".into());
                    record.fault = Some("reset_mid_stream".into());
                });
                return empty_response(200);
            }
            Some(_) => {}
            None => return empty_response(202),
        }
    }
}

#[allow(clippy::too_many_arguments)]
async fn respond_tool(
    instance: &Arc<Instance>,
    state: &Arc<InstanceState>,
    era: Era,
    id: &Value,
    tool: Arc<Tool>,
    params: &Value,
    arguments: Value,
    meta: Value,
    progress_token: Option<Value>,
    shape: Shape,
    session: Option<String>,
    record: &Option<Arc<Mutex<RequestRecord>>>,
) -> Response<Body> {
    let fault = tool
        .config
        .fault
        .clone()
        .or_else(|| state.config.fault.clone());
    if let Some(fault) = &fault {
        match fault.kind {
            FaultKind::Unauthorized => return unauthorized_response(Some(fault), record),
            FaultKind::ServerError => return server_error_response(fault, record),
            FaultKind::Session404Once if era == Era::Legacy => {
                if let Some(session) = &session {
                    instance.expire_session(session);
                    return session_not_found(record);
                }
            }
            _ => {}
        }
    }

    let mut result_type = "complete".to_string();
    let mut outcome = "result".to_string();
    let mut result: Option<Value> = None;
    let mut error: Option<Value> = None;

    match tool.config.behaviour {
        Behaviour::Mrtr => {
            if era == Era::Legacy {
                result = Some(json!({
                    "content": [{
                        "type": "text",
                        "text": "MRTR requires protocol 2026-07-28; this legacy server cannot ask the client for input",
                    }],
                    "isError": true,
                }));
                outcome = "tool_error".into();
            } else if params.get("inputResponses").is_some() {
                let expected = format!("mock-mrtr:{}", tool.config.name);
                if params.get("requestState").and_then(Value::as_str) != Some(expected.as_str()) {
                    error = Some(json!({
                        "code": -32602,
                        "message": "Invalid params: requestState does not match this tool",
                    }));
                    outcome = "error".into();
                } else {
                    let responses = params.get("inputResponses").cloned().unwrap_or(Value::Null);
                    result = Some(json!({
                        "content": [{
                            "type": "text",
                            "text": format!(
                                "MRTR completed: {}",
                                serde_json::to_string(&responses).unwrap_or_default()
                            ),
                        }],
                        "structuredContent": {
                            "inputResponses": responses,
                            "requestState": expected,
                        },
                    }));
                }
            } else {
                let has_elicitation = meta
                    .get("io.modelcontextprotocol/clientCapabilities")
                    .and_then(|capabilities| capabilities.get("elicitation"))
                    .is_some();
                if !has_elicitation {
                    records::finish(record, "error", 400, "application/json", None, None);
                    return rpc_error_http(
                        400,
                        Some(id),
                        -32021,
                        "Missing required client capability: elicitation",
                        Some(json!({"requiredCapabilities": ["elicitation"]})),
                        record,
                        None,
                    );
                }
                result_type = "input_required".into();
                outcome = "input_required".into();
                result = Some(json!({
                    "resultType": "input_required",
                    "inputRequests": {
                        "confirm": {
                            "method": "elicitation/create",
                            "params": {
                                "mode": "form",
                                "message": format!("Confirm {}", tool.config.name),
                                "requestedSchema": {
                                    "type": "object",
                                    "properties": {
                                        "answer": {"type": "string"},
                                    },
                                    "required": ["answer"],
                                },
                            },
                        },
                    },
                    "requestState": format!("mock-mrtr:{}", tool.config.name),
                }));
            }
        }
        Behaviour::ProtocolError => {
            let spec = tool.config.error.clone().unwrap_or_default();
            error = Some(json!({
                "code": spec.code.unwrap_or(-32603),
                "message": spec.message.unwrap_or_else(|| "Injected protocol error".into()),
            }));
            outcome = "error".into();
        }
        Behaviour::ToolError => {
            let message = tool
                .config
                .error
                .as_ref()
                .and_then(|error| error.message.clone())
                .unwrap_or_else(|| "Injected tool error".into());
            result = Some(json!({
                "content": [{"type": "text", "text": message}],
                "isError": true,
            }));
            outcome = "tool_error".into();
        }
        Behaviour::Echo => {
            let text = serde_json::to_string(&arguments).unwrap_or_else(|_| "{}".into());
            let structured = if arguments.is_object() {
                arguments.clone()
            } else {
                json!({"arguments": arguments})
            };
            result = Some(json!({
                "content": [{"type": "text", "text": text}],
                "structuredContent": structured,
            }));
        }
        Behaviour::Fixed => {
            result =
                Some(tool.config.result.clone().unwrap_or_else(
                    || json!({"content": [{"type": "text", "text": "fixed result"}]}),
                ));
        }
        Behaviour::Sleep => {
            result = Some(json!({
                "content": [{
                    "type": "text",
                    "text": format!("slept {} ms", tool.config.sleep_ms),
                }],
            }));
        }
        Behaviour::Progress => {
            result = Some(json!({
                "content": [{
                    "type": "text",
                    "text": format!("emitted {} progress events", tool.config.progress_events),
                }],
            }));
        }
    }

    if let Some(value) = &mut result {
        if let Some(object) = value.as_object_mut() {
            if era == Era::Modern {
                object.insert("resultType".into(), json!(result_type));
                if let Some(meta) = object
                    .entry("_meta")
                    .or_insert_with(|| json!({}))
                    .as_object_mut()
                {
                    meta.insert(
                        "io.modelcontextprotocol/serverInfo".into(),
                        state.server_info.clone(),
                    );
                }
            } else {
                object.remove("resultType");
                object.remove("ttlMs");
                object.remove("cacheScope");
            }
        }
    }

    let mut sse = tool.config.response.unwrap_or(shape) == Shape::Sse;
    if fault.as_ref().is_some_and(|fault| fault.kind.is_stream()) {
        sse = true;
    }
    if fault.as_ref().is_some_and(|fault| {
        matches!(
            fault.kind,
            FaultKind::Compressed | FaultKind::WrongContentType
        )
    }) {
        sse = false;
    }
    if result_type == "complete"
        && error.is_none()
        && tool.config.progress_events > 0
        && progress_token.is_some()
    {
        sse = true;
    }

    let spec = ProducerSpec {
        sse,
        result,
        error,
        result_type: (outcome != "error").then_some(result_type),
        outcome,
        fault: fault.clone(),
        progress_events: tool.config.progress_events,
        progress_interval_ms: tool.config.progress_interval_ms,
        sleep_ms: tool.config.sleep_ms,
        progress_token,
    };
    let key = format!(
        "{}|{}",
        session.as_deref().unwrap_or(""),
        request_id_key(id)
    );
    let (rx, inflight) = spawn_producer(instance, key, record.clone(), spec);
    if sse {
        tool_sse(id, rx, inflight, era, record)
    } else {
        await_tool_json(id, rx, inflight, fault.as_ref(), record).await
    }
}

pub(crate) fn admin_json(status: u16, value: &Value) -> Response<Body> {
    json_response(status, value)
}

pub(crate) fn admin_empty(status: u16) -> Response<Body> {
    empty_response(status)
}
