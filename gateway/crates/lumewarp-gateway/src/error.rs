use bytes::Bytes;
use hyper::{header, Response, StatusCode};

use crate::proxy::{full_body, ProxyBody};

pub fn json(status: StatusCode, code: &'static str, message: &'static str) -> Response<ProxyBody> {
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
