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

/// Who is to blame for an error: the `error_source` label of
/// `lumewarp_requests_total`. `None` is a success.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Source {
    None,
    Client,
    Policy,
    Upstream,
    Gateway,
}

impl Source {
    pub const fn label(self) -> &'static str {
        match self {
            Source::None => "none",
            Source::Client => "client",
            Source::Policy => "policy",
            Source::Upstream => "upstream",
            Source::Gateway => "gateway",
        }
    }
}

/// Attached to every response [`json`] builds, so the request metrics and the
/// usage event can name the gateway's own error without parsing the body.
#[derive(Clone, Copy, Debug)]
pub struct ErrorInfo {
    pub code: &'static str,
    pub source: Source,
}

pub struct Code {
    pub source: Source,
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
    code(502, "upstream_auth_failed", Scope::Llm, "The upstream's WASM auth plugin failed, timed out, trapped or returned a header the gateway must not send, after any retries and fallbacks."),
    upstream(502, "invalid_agent_card", Scope::A2a, "The agent's card is not a JSON object."),
    code(503, "not_ready", Scope::Any, "The gateway has no config snapshot yet."),
    code(503, "upstream_unavailable", Scope::Llm, "The model's circuit breaker is open and its recovery trial is already in progress."),
    code(503, "mcp_not_configured", Scope::Mcp, "`LUMEWARP_GATEWAY_ID_KEY` is not set on this gateway."),
    code(503, "a2a_not_configured", Scope::A2a, "`LUMEWARP_GATEWAY_ID_KEY` or `LUMEWARP_GATEWAY_PUBLIC_URL` is not set on this gateway."),
    code(504, "upstream_timeout", Scope::Llm, "The upstream's response headers did not arrive within its `timeout_ms`, or the router's `total_timeout_ms` ran out."),
    code(0, "mcp_error", Scope::Mcp, "An MCP transport failure outside JSON-RPC: a bad or missing header, an unknown or ungranted server, an expired session, a body over 4 MiB, or an upstream failure. The status says which."),
];

const fn starts_with(text: &str, prefix: &str) -> bool {
    let (text, prefix) = (text.as_bytes(), prefix.as_bytes());
    if text.len() < prefix.len() {
        return false;
    }
    let mut i = 0;
    while i < prefix.len() {
        if text[i] != prefix[i] {
            return false;
        }
        i += 1;
    }
    true
}

/// The blame for a code: upstream failures by name, then 403/429 are policy,
/// other 4xx are the client, everything else is the gateway.
const fn source_of(status: u16, code: &str) -> Source {
    if starts_with(code, "upstream_") {
        Source::Upstream
    } else if status == 403 || status == 429 {
        Source::Policy
    } else if status >= 400 && status < 500 {
        Source::Client
    } else {
        Source::Gateway
    }
}

/// A code that is always the upstream's fault, whatever its name says.
const fn upstream(status: u16, code: &'static str, scope: Scope, meaning: &'static str) -> Code {
    Code {
        source: Source::Upstream,
        status,
        code,
        scope,
        meaning,
    }
}

const fn code(status: u16, code: &'static str, scope: Scope, meaning: &'static str) -> Code {
    Code {
        source: source_of(status, code),
        status,
        code,
        scope,
        meaning,
    }
}

pub fn json(status: StatusCode, code: &'static str, message: &str) -> Response<ProxyBody> {
    json_from(None, status, code, message)
}

/// Like [`json`], but names who is to blame when the code's table entry cannot say (`mcp_error`
/// has one code for every status). The client-visible body is the same.
pub fn json_from(
    source: Option<Source>,
    status: StatusCode,
    code: &'static str,
    message: &str,
) -> Response<ProxyBody> {
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
        (StatusCode::REQUEST_TIMEOUT, _) => "api_error",
        (status, _) if status.is_server_error() => "api_error",
        _ => "invalid_request_error",
    };
    let body = serde_json::json!({"error": {
        "message": message, "type": kind, "param": null, "code": code,
    }});
    let mut response = Response::builder()
        .status(status)
        .header(header::CONTENT_TYPE, "application/json")
        .body(full_body(Bytes::from(body.to_string())))
        .expect("static response parts are valid");
    response.extensions_mut().insert(ErrorInfo {
        code,
        source: source.unwrap_or_else(|| {
            CODES
                .iter()
                .find(|known| known.code == code)
                .map_or(Source::Gateway, |known| known.source)
        }),
    });
    response
}

/// The `error_source` and `code` labels (and usage `error_code`) of a finished
/// request: the gateway's own error when `info` is set, otherwise a non-2xx the
/// upstream passed through (`upstream_<status>`), a client cancellation (499),
/// or a success. A bounded set: codes come from [`CODES`] or from a status.
pub fn classify(status: u16, info: Option<ErrorInfo>) -> (Source, &'static str) {
    if let Some(info) = info {
        return (info.source, info.code);
    }
    match status {
        499 => (Source::Client, "client_cancelled"),
        400..=599 => (Source::Upstream, upstream_code(status)),
        _ => (Source::None, ""),
    }
}

/// `upstream_<status>` as a static string, for statuses 400..=599.
pub fn upstream_code(status: u16) -> &'static str {
    static CODES: std::sync::LazyLock<Vec<&'static str>> = std::sync::LazyLock::new(|| {
        (400..600)
            .map(|status| &*Box::leak(format!("upstream_{status}").into_boxed_str()))
            .collect()
    });
    CODES
        .get(usize::from(status).wrapping_sub(400))
        .copied()
        .unwrap_or("upstream_other")
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

#[cfg(test)]
mod tests {
    use super::*;

    fn kind(status: u16, code: &'static str) -> String {
        let response = json(StatusCode::from_u16(status).unwrap(), code, "x");
        let body = http_body_util::BodyExt::collect(response.into_body());
        let bytes = tokio::runtime::Builder::new_current_thread()
            .build()
            .unwrap()
            .block_on(body)
            .unwrap()
            .to_bytes();
        let value: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        value["error"]["type"].as_str().unwrap().to_owned()
    }

    #[test]
    fn error_type_per_status() {
        assert_eq!(kind(401, "invalid_api_key"), "authentication_error");
        assert_eq!(kind(403, "model_not_allowed"), "permission_error");
        assert_eq!(kind(429, "insufficient_quota"), "insufficient_quota");
        assert_eq!(kind(429, "rate_limit_exceeded"), "rate_limit_error");
        assert_eq!(kind(502, "upstream_error"), "upstream_error");
        assert_eq!(kind(504, "upstream_timeout"), "upstream_error");
        assert_eq!(kind(408, "request_timeout"), "api_error");
        assert_eq!(kind(500, "metrics_error"), "api_error");
        assert_eq!(kind(503, "not_ready"), "api_error");
        assert_eq!(kind(503, "upstream_unavailable"), "api_error");
        assert_eq!(kind(400, "invalid_request"), "invalid_request_error");
        assert_eq!(kind(404, "model_not_found"), "invalid_request_error");
        assert_eq!(kind(413, "request_too_large"), "invalid_request_error");
    }

    #[test]
    fn every_code_has_a_source_and_the_table_matches_the_contract() {
        for known in CODES {
            assert_ne!(known.source, Source::None, "{}", known.code);
        }
        let source = |code: &str| CODES.iter().find(|k| k.code == code).unwrap().source;
        assert_eq!(source("invalid_api_key"), Source::Client);
        assert_eq!(source("request_timeout"), Source::Client);
        assert_eq!(source("model_not_allowed"), Source::Policy);
        assert_eq!(source("rate_limit_exceeded"), Source::Policy);
        assert_eq!(source("insufficient_quota"), Source::Policy);
        assert_eq!(source("upstream_unavailable"), Source::Upstream);
        assert_eq!(source("upstream_auth_failed"), Source::Upstream);
        assert_eq!(source("not_ready"), Source::Gateway);
        assert_eq!(source("metrics_error"), Source::Gateway);
    }

    #[test]
    fn an_explicit_source_overrides_the_table_and_keeps_the_code() {
        let response = json_from(
            Some(Source::Upstream),
            StatusCode::BAD_GATEWAY,
            "mcp_error",
            "x",
        );
        let info = response.extensions().get::<ErrorInfo>().unwrap();
        assert_eq!((info.code, info.source), ("mcp_error", Source::Upstream));
        let table = json(StatusCode::BAD_GATEWAY, "mcp_error", "x");
        assert_eq!(
            table.extensions().get::<ErrorInfo>().unwrap().source,
            Source::Gateway
        );
        assert_eq!(
            CODES
                .iter()
                .find(|k| k.code == "invalid_agent_card")
                .unwrap()
                .source,
            Source::Upstream
        );
    }

    #[test]
    fn classify_passthrough_and_success() {
        assert_eq!(classify(200, None), (Source::None, ""));
        assert_eq!(classify(429, None), (Source::Upstream, "upstream_429"));
        assert_eq!(classify(503, None), (Source::Upstream, "upstream_503"));
        assert_eq!(classify(499, None), (Source::Client, "client_cancelled"));
        let info = ErrorInfo {
            code: "rate_limit_exceeded",
            source: Source::Policy,
        };
        assert_eq!(
            classify(429, Some(info)),
            (Source::Policy, "rate_limit_exceeded")
        );
    }
}
