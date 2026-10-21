use std::{
    borrow::Cow,
    error::Error,
    pin::Pin,
    task::{Context, Poll},
    time::{Duration, Instant},
};

use bytes::Bytes;
use http_body_util::{combinators::UnsyncBoxBody, BodyExt, Full, Limited};
use hyper::{
    body::{Body, Frame, Incoming, SizeHint},
    header::{self, HeaderName},
    HeaderMap, Request, Response, StatusCode, Uri,
};
use hyper_rustls::{HttpsConnector, HttpsConnectorBuilder};
use hyper_util::{
    client::legacy::{connect::HttpConnector, Client},
    rt::TokioExecutor,
};
use serde::Deserialize;
use serde_json::value::RawValue;

use crate::{
    auth::{self, Caller},
    error,
    metrics::{Observation, UpstreamError},
    ratelimit::Permit,
    router::{Deployment, InFlight, Router},
    telemetry,
};

pub type BoxError = Box<dyn Error + Send + Sync>;
pub type ProxyBody = UnsyncBoxBody<Bytes, BoxError>;
pub type UpstreamClient = Client<HttpsConnector<HttpConnector>, Full<Bytes>>;
const MAX_BODY: usize = 32 * 1024 * 1024;

pub fn client() -> UpstreamClient {
    let mut http = HttpConnector::new();
    http.set_nodelay(true);
    http.enforce_http(false);
    http.set_keepalive(Some(Duration::from_secs(60)));
    let connector = HttpsConnectorBuilder::new()
        .with_webpki_roots()
        .https_or_http()
        .enable_http1()
        .wrap_connector(http);
    Client::builder(TokioExecutor::new()).build(connector)
}

pub fn full_body(bytes: Bytes) -> ProxyBody {
    Full::new(bytes)
        .map_err(|never| match never {})
        .boxed_unsync()
}

#[derive(Deserialize)]
pub struct RequestFields<'a> {
    #[serde(borrow)]
    model: &'a RawValue,
    #[serde(default, borrow)]
    stream: Option<&'a RawValue>,
}

impl<'a> RequestFields<'a> {
    pub fn parse(body: &'a [u8]) -> Result<Self, &'static str> {
        // Derived struct deserializers also accept positional JSON arrays. Only
        // objects are valid OpenAI requests, and splicing must preserve that shape.
        if body.iter().copied().find(|b| !b.is_ascii_whitespace()) != Some(b'{') {
            return Err("Expected a JSON request object");
        }
        serde_json::from_slice(body).map_err(|err| {
            // Classify only known static field errors; never expose serde's message,
            // which may include arbitrary client input.
            let message = err.to_string();
            if message.starts_with("missing field `model`")
                || message.starts_with("duplicate field `model`")
            {
                "Expected exactly one string model field"
            } else if message.starts_with("duplicate field `stream`") {
                "Expected at most one boolean or null stream field"
            } else {
                "Invalid JSON request object"
            }
        })
    }

    pub fn model(&self) -> Result<Cow<'a, str>, serde_json::Error> {
        match serde_json::from_str::<&str>(self.model.get()) {
            Ok(model) => Ok(Cow::Borrowed(model)),
            Err(_) => serde_json::from_str::<String>(self.model.get()).map(Cow::Owned),
        }
    }

    pub fn stream(&self) -> Result<bool, serde_json::Error> {
        self.stream
            .map(|raw| serde_json::from_str::<Option<bool>>(raw.get()))
            .transpose()
            .map(|value| value.flatten().unwrap_or(false))
    }

    pub fn rewrite(&self, body: &[u8], upstream_model_json: &[u8]) -> Bytes {
        // RawValue borrows the exact original token, including string escapes.
        let start = self.model.get().as_ptr() as usize - body.as_ptr() as usize;
        let end = start + self.model.get().len();
        let mut output = Vec::with_capacity(body.len() - (end - start) + upstream_model_json.len());
        output.extend_from_slice(&body[..start]);
        output.extend_from_slice(upstream_model_json);
        output.extend_from_slice(&body[end..]);
        Bytes::from(output)
    }
}

pub async fn forward(
    router: &Router,
    client: &UpstreamClient,
    req: Request<Incoming>,
    observation: &mut Observation,
    endpoint: usize,
    caller: Caller,
    body_read_timeout: Duration,
) -> Response<ProxyBody> {
    let (mut parts, incoming) = req.into_parts();
    if parts
        .headers
        .get(header::CONTENT_LENGTH)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.parse::<u64>().ok())
        .is_some_and(|length| length > MAX_BODY as u64)
    {
        return error::json(
            StatusCode::PAYLOAD_TOO_LARGE,
            "request_too_large",
            "Request body exceeds 32 MiB",
        );
    }
    observation.reading_body();
    let collected = tokio::time::timeout(
        body_read_timeout,
        Limited::new(incoming, MAX_BODY).collect(),
    )
    .await;
    observation.body_read_complete();
    let body = match collected {
        Ok(Ok(body)) => body.to_bytes(),
        Err(_) => {
            return error::json(
                StatusCode::REQUEST_TIMEOUT,
                "request_timeout",
                "Request body read timed out",
            )
        }
        Ok(Err(err)) => {
            return if err.is::<http_body_util::LengthLimitError>() {
                error::json(
                    StatusCode::PAYLOAD_TOO_LARGE,
                    "request_too_large",
                    "Request body exceeds 32 MiB",
                )
            } else {
                error::json(
                    StatusCode::BAD_REQUEST,
                    "invalid_request",
                    "Could not read request body",
                )
            };
        }
    };
    let fields = match RequestFields::parse(&body) {
        Ok(fields) => fields,
        Err(message) => return error::json(StatusCode::BAD_REQUEST, "invalid_request", message),
    };
    let stream = match fields.stream() {
        Ok(stream) => stream,
        Err(_) => {
            return error::json(
                StatusCode::BAD_REQUEST,
                "invalid_request",
                "Expected a boolean or null stream field",
            )
        }
    };
    let model = match fields.model() {
        Ok(model) => model,
        Err(_) => {
            return error::json(
                StatusCode::BAD_REQUEST,
                "invalid_request",
                "Expected a string model",
            )
        }
    };
    // Before route lookup, so denied callers cannot probe which aliases exist.
    if let Err(err) = auth::check_model(&caller, &model) {
        observation.routed(router.rejected_model(&caller, &model, endpoint), stream);
        return err.into();
    }
    let Some(route) = router.routes.get(model.as_ref()) else {
        observation.routed(router.unknown(&caller, endpoint), stream);
        return error::json(StatusCode::NOT_FOUND, "model_not_found", "Model not found");
    };
    let permit = match router.runtime.limiters.admit(&caller) {
        Ok(permit) => permit,
        Err(limited) => {
            observation.routed(router.rejected_model(&caller, &model, endpoint), stream);
            return error::rate_limited(limited.retry_after_secs);
        }
    };
    let guard = router.acquire(route, endpoint, &caller);
    observation.routed(guard.metrics.clone(), stream);
    let deployment = &guard.deployment;
    tracing::debug!(deployment = %deployment.name, stream, "proxying request");
    let rewritten = fields.rewrite(&body, &deployment.upstream_model_json);
    let uri = match upstream_uri(deployment, &parts.uri) {
        Ok(uri) => uri,
        Err(_) => {
            return error::json(
                StatusCode::BAD_REQUEST,
                "invalid_request",
                "Invalid request URI",
            )
        }
    };
    strip_request_headers(&mut parts.headers);
    parts.headers.remove(header::AUTHORIZATION);
    if let Some(value) = &deployment.authorization {
        parts.headers.insert(header::AUTHORIZATION, value.clone());
    }
    parts.headers.insert(header::HOST, deployment.host.clone());
    parts.uri = uri;
    parts.extensions.clear();
    if observation.otel {
        telemetry::inject(&mut parts.headers);
    }
    // The downstream protocol version is independent of the upstream connection.
    parts.version = hyper::Version::HTTP_11;
    let request = Request::from_parts(parts, Full::new(rewritten));
    observation.upstream_started = Some(Instant::now());
    let response = match tokio::time::timeout(deployment.timeout, client.request(request)).await {
        Ok(Ok(response)) => response,
        Ok(Err(err)) => {
            observation.handles.upstream_error(if err.is_connect() {
                UpstreamError::Connect
            } else {
                UpstreamError::Protocol
            });
            return error::json(
                StatusCode::BAD_GATEWAY,
                "upstream_error",
                "Upstream connection failed",
            );
        }
        Err(_) => {
            observation.handles.upstream_error(UpstreamError::Timeout);
            return error::json(
                StatusCode::GATEWAY_TIMEOUT,
                "upstream_timeout",
                "Upstream response timed out",
            );
        }
    };
    observation.headers_received = Some(Instant::now());
    let (mut parts, body) = response.into_parts();
    strip_response_headers(&mut parts.headers);
    Response::from_parts(
        parts,
        StreamingBody {
            body,
            guard: Some(guard),
            permit: Some(permit),
        }
        .boxed_unsync(),
    )
}

fn upstream_uri(deployment: &Deployment, request: &Uri) -> Result<Uri, hyper::http::Error> {
    let index = match request.path() {
        "/v1/chat/completions" => 0,
        "/v1/completions" => 1,
        "/v1/embeddings" => 2,
        _ => unreachable!("dispatch only forwards supported endpoints"),
    };
    let endpoint = &deployment.endpoints[index];
    let Some(query) = request.query() else {
        return Ok(endpoint.clone());
    };
    let mut parts = endpoint.clone().into_parts();
    parts.path_and_query = Some(format!("{}?{query}", endpoint.path()).parse()?);
    Ok(Uri::from_parts(parts)?)
}

fn strip_headers(headers: &mut HeaderMap) {
    // Connection may nominate additional hop-by-hop fields, not just standard ones.
    if headers.contains_key(header::CONNECTION) {
        let nominated: Vec<HeaderName> = headers
            .get_all(header::CONNECTION)
            .iter()
            .filter_map(|v| v.to_str().ok())
            .flat_map(|v| v.split(','))
            .filter_map(|name| HeaderName::from_bytes(name.trim().as_bytes()).ok())
            .collect();
        for name in nominated {
            headers.remove(name);
        }
    }
    for name in [
        "connection",
        "keep-alive",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "content-length",
    ] {
        headers.remove(name);
    }
    // The common path has neither Connection nominations nor proxy-* fields and
    // allocates nothing. Uncommon custom proxy headers are removed one at a time.
    loop {
        let name = headers
            .keys()
            .find(|name| name.as_str().starts_with("proxy-"))
            .cloned();
        let Some(name) = name else { break };
        headers.remove(name);
    }
}

fn strip_request_headers(headers: &mut HeaderMap) {
    strip_headers(headers);
    for name in ["expect", "openai-organization", "openai-project"] {
        headers.remove(name);
    }
}

fn strip_response_headers(headers: &mut HeaderMap) {
    strip_headers(headers);
    for name in ["set-cookie", "openai-organization", "openai-project"] {
        headers.remove(name);
    }
}

struct StreamingBody {
    body: Incoming,
    guard: Option<InFlight>,
    permit: Option<Permit>,
}

impl StreamingBody {
    fn release(&mut self) -> Option<InFlight> {
        self.permit.take();
        self.guard.take()
    }
}

impl Body for StreamingBody {
    type Data = Bytes;
    type Error = BoxError;

    fn poll_frame(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
    ) -> Poll<Option<Result<Frame<Bytes>, BoxError>>> {
        match Pin::new(&mut self.body).poll_frame(cx) {
            Poll::Ready(None) => {
                self.release();
                Poll::Ready(None)
            }
            Poll::Ready(Some(Err(err))) => {
                if let Some(guard) = self.release() {
                    guard.metrics.upstream_error(UpstreamError::Stream);
                }
                Poll::Ready(Some(Err(Box::new(err))))
            }
            Poll::Ready(Some(Ok(frame))) => {
                if self.body.is_end_stream() {
                    self.release();
                }
                Poll::Ready(Some(Ok(frame)))
            }
            Poll::Pending => Poll::Pending,
        }
    }

    fn is_end_stream(&self) -> bool {
        self.body.is_end_stream()
    }
    fn size_hint(&self) -> SizeHint {
        self.body.size_hint()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rewrite_preserves_order_and_nested_fields() {
        let body = br#"{ "tools":[{"model":"nested"}], "model" : "public", "extra_body": { "guided_json": {"x":1} }, "stream_options":{"include_usage":true}, "stream":true }"#;
        let fields = RequestFields::parse(body).unwrap();
        assert!(fields.stream().unwrap());
        assert_eq!(fields.model().unwrap(), "public");
        let expected = String::from_utf8(body.to_vec())
            .unwrap()
            .replace("\"public\"", "\"actual\"");
        assert_eq!(fields.rewrite(body, br#""actual""#), expected.as_bytes());
    }

    #[test]
    fn rewrite_unicode_and_escaped_names() {
        let body = br#"{"model":"\u6a21\u578b\"\\","other":"\u1234"}"#;
        let fields = RequestFields::parse(body).unwrap();
        assert_eq!(fields.model().unwrap(), "模型\"\\");
        let rewritten = fields.rewrite(body, &serde_json::to_vec("上游\"\\").unwrap());
        assert_eq!(
            std::str::from_utf8(&rewritten).unwrap(),
            "{\"model\":\"上游\\\"\\\\\",\"other\":\"\\u1234\"}"
        );
    }

    #[test]
    fn rejects_missing_invalid_and_duplicate_model() {
        for body in [
            b"{}".as_slice(),
            b"[]",
            br#"["public",false]"#,
            b"{bad}",
            br#"{"model":"a","model":"b"}"#,
        ] {
            assert!(RequestFields::parse(body).is_err());
        }
        for body in [br#"{"model":null}"#.as_slice(), br#"{"model":123}"#] {
            assert!(RequestFields::parse(body).unwrap().model().is_err());
        }
    }

    #[test]
    fn precomputed_endpoints_preserve_prefix_and_query() {
        let (_, snapshot) = lumewarp_config::Config::parse(
            r#"
upstreams:
  - {name: u, kind: vllm, base_url: 'http://localhost:8123/prefix/v1/'}
deployments:
  - {name: d, upstream: u, upstream_model: actual}
routes:
  - {model: public, deployments: [d]}
"#,
        )
        .unwrap();
        let router = Router::compile(
            &snapshot,
            lumewarp_config::AuthMode::Open,
            &crate::runtime::Runtime::new(),
        )
        .unwrap();
        let guard = router.acquire(&router.routes["public"], 0, &Caller::Anonymous);
        let deployment = &guard.deployment;
        assert_eq!(deployment.host, "localhost:8123");
        assert_eq!(deployment.upstream_model_json, br#""actual""#.as_slice());
        for suffix in ["/chat/completions", "/completions", "/embeddings"] {
            let request: Uri = format!("/v1{suffix}").parse().unwrap();
            assert_eq!(
                upstream_uri(deployment, &request).unwrap().to_string(),
                format!("http://localhost:8123/prefix/v1{suffix}")
            );
            let request: Uri = format!("/v1{suffix}?x=a%2Fb&x=two").parse().unwrap();
            assert_eq!(
                upstream_uri(deployment, &request).unwrap().to_string(),
                format!("http://localhost:8123/prefix/v1{suffix}?x=a%2Fb&x=two")
            );
        }
    }

    #[test]
    fn accepts_null_stream_and_classifies_bad_fields() {
        let fields = RequestFields::parse(br#"{"model":"public","stream":null}"#).unwrap();
        assert!(!fields.stream().unwrap());
        assert!(matches!(fields.model().unwrap(), Cow::Borrowed("public")));
        assert!(
            RequestFields::parse(br#"{"model":"public","stream":"true"}"#)
                .unwrap()
                .stream()
                .is_err()
        );
        assert!(RequestFields::parse(br#"{"model":"public","stream":{}}"#)
            .unwrap()
            .stream()
            .is_err());
        assert!(RequestFields::parse(br#"{"stream":true}"#)
            .err()
            .unwrap()
            .contains("model"));
        assert!(
            RequestFields::parse(br#"{"model":"public","stream":true,"stream":false}"#)
                .err()
                .unwrap()
                .contains("stream")
        );
    }

    #[test]
    fn strips_direction_specific_headers() {
        let mut headers = HeaderMap::new();
        for name in [
            "expect",
            "openai-organization",
            "openai-project",
            "set-cookie",
            "traceparent",
            "tracestate",
        ] {
            headers.insert(HeaderName::from_static(name), "opaque".parse().unwrap());
        }
        let mut request = headers.clone();
        strip_request_headers(&mut request);
        for name in ["expect", "openai-organization", "openai-project"] {
            assert!(!request.contains_key(name));
        }
        assert_eq!(request["traceparent"], "opaque");
        assert_eq!(request["tracestate"], "opaque");
        strip_response_headers(&mut headers);
        for name in ["set-cookie", "openai-organization", "openai-project"] {
            assert!(!headers.contains_key(name));
        }
        assert_eq!(headers["traceparent"], "opaque");
    }

    #[test]
    fn strips_hop_headers_but_keeps_trace() {
        let mut headers = HeaderMap::new();
        headers.insert("connection", "x-private, keep-alive".parse().unwrap());
        headers.insert("x-private", "secret".parse().unwrap());
        headers.insert("proxy-authorization", "secret".parse().unwrap());
        headers.insert("content-length", "42".parse().unwrap());
        headers.insert("traceparent", "untouched".parse().unwrap());
        strip_headers(&mut headers);
        assert_eq!(headers.len(), 1);
        assert_eq!(headers["traceparent"], "untouched");
    }
}
