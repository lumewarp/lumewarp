use bytes::Bytes;
use hyper::{header, Response, StatusCode};

use crate::proxy::{full_body, ProxyBody};

pub fn json(status: StatusCode, code: &'static str, message: &'static str) -> Response<ProxyBody> {
    let kind = match status {
        StatusCode::UNAUTHORIZED => "authentication_error",
        StatusCode::BAD_GATEWAY | StatusCode::GATEWAY_TIMEOUT => "upstream_error",
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
