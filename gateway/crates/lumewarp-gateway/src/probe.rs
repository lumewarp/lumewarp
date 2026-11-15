//! Type-specific synthetic probes; audio is generated once, not stored as an asset.
//!
//! Also runs a one-off `ProbeRequest` from the control plane (gateway-side Test
//! now): the control plane cannot run WASM auth plugins, so a connected
//! gateway probes its current snapshot and answers with `ProbeResult`. Never on
//! the request path, and the upstream response body is never included.
use std::{
    sync::OnceLock,
    time::{Duration, Instant},
};

use bytes::Bytes;
use hyper::{
    header::{HeaderValue, AUTHORIZATION, CONTENT_TYPE},
    HeaderMap, Method, Request, Uri,
};
use lumewarp_proto::config::v1::{Model, ModelType, ProbeRequest, ProbeResult, Snapshot};

use crate::{
    request_body::RequestBody,
    runtime::Runtime,
    targets::{self, open_secret},
    upstream_auth::{self, LlmAuth},
};

pub const AUDIO_CONTENT_TYPE: &str = "multipart/form-data; boundary=lumewarp-probe";
pub fn endpoint(kind: i32) -> usize {
    crate::endpoint::ENDPOINTS
        .iter()
        .position(|e| e.model_type as i32 == kind)
        .expect("validated type")
}
pub fn wav() -> &'static [u8] {
    static WAV: OnceLock<Vec<u8>> = OnceLock::new();
    WAV.get_or_init(|| {
        let mut bytes = Vec::with_capacity(32044);
        bytes.extend_from_slice(b"RIFF");
        bytes.extend_from_slice(&32036u32.to_le_bytes());
        bytes.extend_from_slice(b"WAVEfmt ");
        bytes.extend_from_slice(&16u32.to_le_bytes());
        bytes.extend_from_slice(&1u16.to_le_bytes());
        bytes.extend_from_slice(&1u16.to_le_bytes());
        bytes.extend_from_slice(&16000u32.to_le_bytes());
        bytes.extend_from_slice(&32000u32.to_le_bytes());
        bytes.extend_from_slice(&2u16.to_le_bytes());
        bytes.extend_from_slice(&16u16.to_le_bytes());
        bytes.extend_from_slice(b"data");
        bytes.extend_from_slice(&32000u32.to_le_bytes());
        bytes.resize(32044, 0);
        bytes
    })
}
/// Runs one control-plane probe against the gateway's current snapshot.
///
/// `upstream` is an `Upstream.name`; `model` is a `Model.name` on it. An empty
/// model probes the upstream itself with `GET <base_url>/models`, like its
/// health check. Unknown names are `ok=false` with `not_found` /
/// `not_found_model`; every failure is a fixed class, never upstream text.
/// Upper bound of one probe requested over the config stream, plugin call
/// included; the control plane gives up after at most the same 15 s.
pub const PROBE_DEADLINE: std::time::Duration = std::time::Duration::from_secs(15);

/// The answer for a probe that did not finish within [`PROBE_DEADLINE`].
pub fn timed_out(request: &ProbeRequest) -> ProbeResult {
    ProbeResult {
        request_id: request.request_id.clone(),
        ok: false,
        status: 0,
        latency_ms: u32::try_from(PROBE_DEADLINE.as_millis()).unwrap_or(u32::MAX),
        error_class: "timeout".to_owned(),
    }
}

pub async fn run_probe(
    snapshot: &Snapshot,
    runtime: &Runtime,
    request: &ProbeRequest,
) -> ProbeResult {
    let result = |ok: bool, status: u32, latency_ms: u64, error_class: &str| ProbeResult {
        request_id: request.request_id.clone(),
        ok,
        status,
        latency_ms: u32::try_from(latency_ms).unwrap_or(u32::MAX),
        error_class: error_class.to_owned(),
    };
    let Some(upstream) = snapshot
        .upstreams
        .iter()
        .find(|u| u.name == request.upstream)
    else {
        return result(false, 0, 0, "not_found");
    };
    let Some(base) = crate::router::plain_http_uri(&upstream.base_url) else {
        return result(false, 0, 0, "config");
    };

    let (uri, method, body, content_type) = if request.model.is_empty() {
        let uri = url_endpoint(&base, "/models");
        (uri, Method::GET, Bytes::new(), None)
    } else {
        let Some(model) = snapshot.models.iter().find(|m| m.name == request.model) else {
            return result(false, 0, 0, "not_found_model");
        };
        if model.upstream != upstream.name {
            return result(false, 0, 0, "not_found_model");
        }
        let spec = &crate::endpoint::ENDPOINTS[endpoint(model.r#type)];
        let content_type = if model.r#type == ModelType::Transcription as i32 {
            Some(AUDIO_CONTENT_TYPE)
        } else {
            Some("application/json")
        };
        (
            url_endpoint(&base, spec.upstream_suffix),
            Method::POST,
            body(model),
            content_type,
        )
    };

    let mut headers = HeaderMap::new();
    if let Some(content_type) = content_type {
        headers.insert(CONTENT_TYPE, HeaderValue::from_static(content_type));
    }
    let authorization = match upstream_authorization(upstream) {
        Ok(value) => value,
        Err(()) => return result(false, 0, 0, "config"),
    };
    let auth = match targets::compile_llm_auth(
        upstream.auth.as_ref(),
        &format!("upstream {:?}", upstream.name),
    ) {
        Ok(Some(plugin)) => Some(LlmAuth::new(
            &upstream.name,
            base.clone(),
            plugin,
            &runtime.metrics,
        )),
        Ok(None) => None,
        Err(_) => return result(false, 0, 0, "config"),
    };
    if let Some(auth) = &auth {
        if upstream_auth::authorize_llm(auth, &uri, &method, &body, &mut headers)
            .await
            .is_err()
        {
            return result(false, 0, 0, "auth");
        }
    } else if let Some(value) = authorization {
        headers.insert(AUTHORIZATION, value);
    }

    let mut outbound = Request::new(RequestBody::from(body));
    *outbound.uri_mut() = uri;
    *outbound.method_mut() = method;
    *outbound.headers_mut() = headers;
    let timeout_ms = if upstream.timeout_ms == 0 {
        lumewarp_config::default_timeout_ms()
    } else {
        upstream.timeout_ms
    };
    let started = Instant::now();
    let response = match tokio::time::timeout(
        Duration::from_millis(timeout_ms.into()),
        runtime.client.request(outbound),
    )
    .await
    {
        Ok(Ok(response)) => response,
        Ok(Err(error)) => {
            let class = if error.is_connect() {
                "connect"
            } else {
                "protocol"
            };
            return result(false, 0, started.elapsed().as_millis() as u64, class);
        }
        Err(_) => return result(false, 0, started.elapsed().as_millis() as u64, "timeout"),
    };
    let status = response.status();
    let ok = status.is_success();
    // The body is dropped unread: the control plane never receives it.
    drop(response.into_body());
    result(
        ok,
        u32::from(status.as_u16()),
        started.elapsed().as_millis() as u64,
        if ok { "" } else { "upstream_status" },
    )
}

fn url_endpoint(base: &Uri, suffix: &str) -> Uri {
    let mut parts = base.clone().into_parts();
    parts.path_and_query = Some(
        format!("{}{suffix}", base.path().trim_end_matches('/'))
            .parse()
            .expect("validated base URL"),
    );
    Uri::from_parts(parts).expect("validated base URL")
}

/// The static bearer an upstream without a WASM plugin sends, opened at probe
/// time only. Errors are a configuration problem; the secret never leaves here.
fn upstream_authorization(
    upstream: &lumewarp_proto::config::v1::Upstream,
) -> Result<Option<HeaderValue>, ()> {
    let token = if !upstream.api_key_ciphertext.is_empty() {
        open_secret(
            crate::sealed::env_secret().as_deref(),
            &upstream.api_key_ciphertext,
        )
        .map_err(|_| ())?
    } else if !upstream.api_key_ref.is_empty() {
        std::env::var(&upstream.api_key_ref).map_err(|_| ())?
    } else {
        return Ok(None);
    };
    let mut value = HeaderValue::from_str(&format!("Bearer {token}")).map_err(|_| ())?;
    value.set_sensitive(true);
    Ok(Some(value))
}

pub fn body(model: &Model) -> Bytes {
    if model.r#type == ModelType::Transcription as i32 {
        let mut bytes = format!("--lumewarp-probe\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n{}\r\n--lumewarp-probe\r\nContent-Disposition: form-data; name=\"response_format\"\r\n\r\njson\r\n--lumewarp-probe\r\nContent-Disposition: form-data; name=\"file\"; filename=\"probe.wav\"\r\nContent-Type: audio/wav\r\n\r\n", model.upstream_model).into_bytes();
        bytes.extend_from_slice(wav());
        bytes.extend_from_slice(b"\r\n--lumewarp-probe--\r\n");
        return Bytes::from(bytes);
    }
    let value = match ModelType::try_from(model.r#type).expect("validated type") {
        ModelType::Embedding => serde_json::json!({"model":model.upstream_model,"input":"ping"}),
        ModelType::Decision => {
            serde_json::json!({"model":model.upstream_model,"input":"ping","questions":[{"type":"predicate","name":"ok","instructions":"Is the input a word?"}]})
        }
        _ => {
            serde_json::json!({"model":model.upstream_model,"messages":[{"role":"user","content":"1 + 1 = ?"}],"max_tokens":if model.max_output_tokens == 0 {16} else {model.max_output_tokens.min(16)},"stream":false})
        }
    };
    Bytes::from(serde_json::to_vec(&value).expect("JSON"))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn generated_wav_header() {
        assert_eq!(wav().len(), 32044);
        assert_eq!(&wav()[..44], b"RIFF\x24\x7d\0\0WAVEfmt \x10\0\0\0\x01\0\x01\0\x80\x3e\0\0\0\x7d\0\0\x02\0\x10\0data\0\x7d\0\0");
        assert!(wav()[44..].iter().all(|&b| b == 0));
    }
}
