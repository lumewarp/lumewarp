use bytes::Bytes;
use hyper::{header, Response, StatusCode};

use crate::proxy::{full_body, ProxyBody};

/// Which endpoints answer with a code; the OpenAPI document groups by it.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Scope {
    /// Any request on the primary listener.
    Any,
    /// `/v1/*`.
    Llm,
    Mcp,
    A2a,
    /// The admin listener (`/readyz`, `/metrics`).
    Admin,
}

pub struct Code {
    /// 0: the status varies with the failure.
    pub status: u16,
    pub code: &'static str,
    pub scope: Scope,
    pub meaning: &'static str,
}

/// Every `error.code` the gateway answers with. `json` refuses any other code in debug
/// builds, and the OpenAPI document (`openapi.rs`) is rendered from this table, so a code
/// cannot ship without a description. JSON-RPC errors of MCP and A2A are not HTTP errors
/// and are not listed here.
pub const CODES: &[Code] = &[
    code(400, "invalid_request", Scope::Llm, "The body is not a JSON object (or valid multipart form) with exactly one string `model`, `stream` is not a boolean or null, or the body could not be read."),
    code(400, "model_type_mismatch", Scope::Llm, "The called name is of another model type than the endpoint serves."),
    code(401, "invalid_api_key", Scope::Any, "The key is missing, sent more than once, unknown, revoked, suspended or expired."),
    code(403, "model_not_allowed", Scope::Llm, "The key is not granted the called name."),
    code(404, "model_not_found", Scope::Llm, "No model, alias or router has the called name."),
    code(404, "not_found", Scope::Any, "Unknown path, or a method the path does not serve."),
    code(404, "agent_card_not_found", Scope::A2a, "The agent is unknown, not granted to the key, or has no card yet."),
    code(405, "method_not_allowed", Scope::A2a, "The A2A path does not serve this method; `Allow` names the one it does."),
    code(408, "request_timeout", Scope::Llm, "The request body did not arrive within `server.body_read_timeout_ms`."),
    code(413, "request_too_large", Scope::Llm, "The request body exceeds 32 MiB."),
    code(429, "insufficient_quota", Scope::Llm, "The key's budget is exhausted."),
    code(429, "rate_limit_exceeded", Scope::Any, "A rate limit of the key, its owner, team or org is exhausted; `Retry-After` gives the seconds to wait."),
    code(500, "metrics_error", Scope::Admin, "The metrics could not be encoded."),
    code(502, "upstream_error", Scope::Llm, "The upstream could not be reached or answered with a broken response, after any retries and fallbacks."),
    code(502, "invalid_agent_card", Scope::A2a, "The agent's card is not a JSON object."),
    code(503, "not_ready", Scope::Any, "The gateway has no config snapshot yet."),
    code(503, "upstream_unavailable", Scope::Llm, "The model's circuit breaker is open and its recovery trial is already in progress."),
    code(503, "mcp_not_configured", Scope::Mcp, "`LUMEWARP_GATEWAY_ID_KEY` is not set on this gateway."),
    code(503, "a2a_not_configured", Scope::A2a, "`LUMEWARP_GATEWAY_ID_KEY` or `LUMEWARP_GATEWAY_PUBLIC_URL` is not set on this gateway."),
    code(504, "upstream_timeout", Scope::Llm, "The upstream's response headers did not arrive within its `timeout_ms`, or the router's `total_timeout_ms` ran out."),
    code(0, "mcp_error", Scope::Mcp, "An MCP transport failure outside JSON-RPC: a bad or missing header, an unknown or ungranted server, an expired session, a body over 4 MiB, or an upstream failure. The status says which."),
];

const fn code(status: u16, code: &'static str, scope: Scope, meaning: &'static str) -> Code {
    Code {
        status,
        code,
        scope,
        meaning,
    }
}

pub fn json(status: StatusCode, code: &'static str, message: &str) -> Response<ProxyBody> {
    debug_assert!(
        CODES
            .iter()
            .any(|known| known.code == code
                && (known.status == 0 || known.status == status.as_u16())),
        "{status} {code} is missing from error::CODES"
    );
    let kind = match (status, code) {
        (StatusCode::UNAUTHORIZED, _) => "authentication_error",
        (StatusCode::FORBIDDEN, _) => "permission_error",
        (_, "insufficient_quota") => "insufficient_quota",
        (StatusCode::TOO_MANY_REQUESTS, _) => "rate_limit_error",
        (StatusCode::BAD_GATEWAY | StatusCode::GATEWAY_TIMEOUT, _) => "upstream_error",
        _ => "invalid_request_error",
    };
    let body = serde_json::json!({"error": {
        "message": message, "type": kind, "param": null, "code": code,
    }});
    Response::builder()
        .status(status)
        .header(header::CONTENT_TYPE, "application/json")
        .body(full_body(Bytes::from(body.to_string())))
        .expect("static response parts are valid")
}

pub fn rate_limited(retry_after_secs: u64) -> Response<ProxyBody> {
    let mut response = json(
        StatusCode::TOO_MANY_REQUESTS,
        "rate_limit_exceeded",
        "Rate limit exceeded",
    );
    response
        .headers_mut()
        .insert(header::RETRY_AFTER, retry_after_secs.into());
    response
}
