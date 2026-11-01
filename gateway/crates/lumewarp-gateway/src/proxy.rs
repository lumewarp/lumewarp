use std::{
    borrow::Cow,
    error::Error,
    ops::Range,
    pin::Pin,
    sync::Arc,
    task::{Context, Poll},
    time::{Duration, Instant, SystemTime},
};

use bytes::{Bytes, BytesMut};
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
use lumewarp_proto::{
    config::v1::{CircuitBreaker, StreamUsageMode},
    usage::v1::{RequestOutcome, UsageState},
};
use serde::Deserialize;
use serde_json::value::RawValue;
use tracing::Instrument;

use crate::{
    auth::{self, Caller},
    error,
    health::Breaker,
    metrics::{Observation, RequestMetrics, UpstreamError, ATTEMPT_OUTCOMES},
    ratelimit::clock,
    ratelimit::Permit,
    router::{InFlight, Model, RoutingTable, Skipped},
    telemetry,
    usage::{Captured, PendingCapture, RequestCtx, SseFilter, Tee, UsageCapture},
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
    #[serde(default, borrow, deserialize_with = "raw_value")]
    stream_options: Option<&'a RawValue>,
}

/// Keeps an explicit JSON `null` as `Some(raw null)`: plain `Option`
/// deserialization would turn it into `None` and a splice would then add a
/// duplicate key.
fn raw_value<'de: 'a, 'a, D: serde::Deserializer<'de>>(
    deserializer: D,
) -> Result<Option<&'a RawValue>, D::Error> {
    <&RawValue>::deserialize(deserializer).map(Some)
}

/// Where the client's `stream_options.include_usage` stands, with the raw
/// tokens a splice replaces.
#[derive(Debug)]
pub enum IncludeUsage<'a> {
    /// No `stream_options`, or `"stream_options": null` (the raw `null`).
    NoOptions(Option<&'a RawValue>),
    /// A `stream_options` object without `include_usage`.
    NoField(&'a RawValue),
    /// The client asked for usage.
    True,
    /// `include_usage` is `false` or `null` (the raw value).
    Off(&'a RawValue),
}

impl IncludeUsage<'_> {
    pub fn requested(&self) -> bool {
        matches!(self, Self::True)
    }
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
            } else if message.starts_with("duplicate field `stream_options`") {
                "Expected at most one stream_options field"
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

    /// Locates `stream_options.include_usage` without re-serializing; other
    /// `stream_options` fields are ignored and preserved.
    pub fn include_usage(&self) -> Result<IncludeUsage<'a>, &'static str> {
        #[derive(Deserialize)]
        struct StreamOptions<'a> {
            #[serde(default, borrow, deserialize_with = "raw_value")]
            include_usage: Option<&'a RawValue>,
        }
        let Some(raw) = self.stream_options else {
            return Ok(IncludeUsage::NoOptions(None));
        };
        let text: &'a str = raw.get();
        if text == "null" {
            return Ok(IncludeUsage::NoOptions(Some(raw)));
        }
        if !text.starts_with('{') {
            return Err("Expected stream_options to be an object or null");
        }
        let options: StreamOptions<'a> = serde_json::from_str(text)
            .map_err(|_| "Expected at most one boolean or null stream_options.include_usage")?;
        let Some(value) = options.include_usage else {
            return Ok(IncludeUsage::NoField(raw));
        };
        match serde_json::from_str::<Option<bool>>(value.get()) {
            Ok(Some(true)) => Ok(IncludeUsage::True),
            Ok(_) => Ok(IncludeUsage::Off(value)),
            Err(_) => Err("Expected a boolean or null stream_options.include_usage"),
        }
    }

    /// Replaces the model token and, with `force_usage`, makes
    /// `stream_options.include_usage` true; every other byte is preserved.
    pub fn rewrite(
        &self,
        body: &[u8],
        upstream_model_json: &[u8],
        force_usage: Option<&IncludeUsage<'_>>,
    ) -> Bytes {
        // RawValue borrows the exact original token, including string escapes.
        let span = |raw: &RawValue| {
            let start = raw.get().as_ptr() as usize - body.as_ptr() as usize;
            start..start + raw.get().len()
        };
        let mut edits: [Option<(Range<usize>, &[u8])>; 2] =
            [Some((span(self.model), upstream_model_json)), None];
        edits[1] = match force_usage {
            None | Some(IncludeUsage::True) => None,
            Some(IncludeUsage::NoOptions(None)) => {
                let open = body
                    .iter()
                    .position(|&b| b == b'{')
                    .expect("validated object");
                Some((
                    open + 1..open + 1,
                    br#""stream_options":{"include_usage":true},"#,
                ))
            }
            Some(IncludeUsage::NoOptions(Some(null))) => {
                Some((span(null), br#"{"include_usage":true}"#))
            }
            Some(IncludeUsage::NoField(options)) => {
                let at = span(options).start + 1;
                let empty = options.get()[1..].trim_start().starts_with('}');
                let insert: &[u8] = if empty {
                    br#""include_usage":true"#
                } else {
                    br#""include_usage":true,"#
                };
                Some((at..at, insert))
            }
            Some(IncludeUsage::Off(value)) => Some((span(value), b"true")),
        };
        if let [Some(a), Some(b)] = &edits {
            if b.0.start < a.0.start {
                edits.swap(0, 1);
            }
        }
        let added: usize = edits.iter().flatten().map(|(_, new)| new.len()).sum();
        let mut output = Vec::with_capacity(body.len() + added);
        let mut at = 0;
        for (range, new) in edits.iter().flatten() {
            output.extend_from_slice(&body[at..range.start]);
            output.extend_from_slice(new);
            at = range.end;
        }
        output.extend_from_slice(&body[at..]);
        Bytes::from(output)
    }
}

/// Reports a request rejected after its model was parsed (403 / 429). Only
/// keyed callers are reported: anonymous rejections would be unattributable.
fn report_rejection(
    routing_table: &RoutingTable,
    caller: &Caller,
    request: RequestCtx,
    handles: Arc<RequestMetrics>,
    endpoint: usize,
    stream: bool,
    status: StatusCode,
) {
    if matches!(caller, Caller::Key(_)) && routing_table.runtime.usage.enabled() {
        UsageCapture {
            runtime: routing_table.runtime.clone(),
            caller: caller.clone(),
            request,
            handles,
            model: None,
            config_version: routing_table.version,
            endpoint,
            stream,
            status: status.as_u16(),
        }
        .finalize(RequestOutcome::Rejected, UsageState::Missing, None);
    }
}

#[allow(clippy::too_many_arguments)]
pub async fn forward(
    routing_table: &RoutingTable,
    client: &UpstreamClient,
    req: Request<Incoming>,
    observation: &mut Observation,
    endpoint: usize,
    caller: Caller,
    request: RequestCtx,
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
    let include_usage = match fields.include_usage() {
        Ok(include_usage) => include_usage,
        Err(message) => return error::json(StatusCode::BAD_REQUEST, "invalid_request", message),
    };
    // Before router lookup, so denied callers cannot probe which aliases exist.
    if let Err(err) = auth::check_model(&caller, &model) {
        let handles = routing_table.rejected_model(&caller, &model, endpoint);
        observation.routed(handles.clone(), stream);
        report_rejection(
            routing_table,
            &caller,
            request,
            handles,
            endpoint,
            stream,
            err.status(),
        );
        return err.into();
    }
    let Some(router) = routing_table.routers.get(model.as_ref()) else {
        observation.routed(routing_table.unknown(&caller, endpoint), stream);
        return error::json(StatusCode::NOT_FOUND, "model_not_found", "Model not found");
    };
    let permit = match routing_table.runtime.limiters.admit(&caller) {
        Ok(permit) => permit,
        Err(limited) => {
            let handles = routing_table.rejected_model(&caller, &model, endpoint);
            observation.routed(handles.clone(), stream);
            report_rejection(
                routing_table,
                &caller,
                request,
                handles,
                endpoint,
                stream,
                StatusCode::TOO_MANY_REQUESTS,
            );
            return error::rate_limited(limited.retry_after_secs);
        }
    };
    // Only requests that may retry fund the global retry budget; routers
    // without a retry policy never touch its shared state.
    if router.retry.is_some() {
        routing_table.runtime.retry_budget.request();
    }
    let mut skipped = Skipped::default();
    let Some((mut guard, initial_fallback, split)) =
        routing_table.acquire_initial(router, endpoint, &caller, &parts.headers, &mut skipped)
    else {
        let handles = routing_table.rejected_model(&caller, &model, endpoint);
        observation.routed(handles.clone(), stream);
        report_rejection(
            routing_table,
            &caller,
            request,
            handles,
            endpoint,
            stream,
            StatusCode::SERVICE_UNAVAILABLE,
        );
        return error::json(
            StatusCode::SERVICE_UNAVAILABLE,
            "upstream_unavailable",
            "Upstream recovery trial already in progress",
        );
    };
    if let Some(counter) = initial_fallback {
        counter.inc();
    }
    observation.routed(guard.metrics.clone(), stream);
    strip_request_headers(&mut parts.headers);
    parts.headers.remove(header::AUTHORIZATION);
    parts.extensions.clear();
    parts.version = hyper::Version::HTTP_11;
    let max_attempts = router.retry.as_ref().map_or(1, |p| {
        if p.max_attempts == 0 {
            2
        } else {
            p.max_attempts
        }
    });
    // Preserve headers only for retry-enabled routers. The ordinary single-send
    // path moves the original map and adds no replay allocation.
    let replay = (max_attempts > 1).then(|| parts.clone());
    // Resolved before a usage capture exists: an unusable request URI is a
    // client error, never a cancelled (499) usage event.
    let mut first_uri = match upstream_uri(&guard.model, &parts.uri) {
        Ok(uri) => Some(uri),
        Err(_) => {
            return error::json(
                StatusCode::BAD_REQUEST,
                "invalid_request",
                "Invalid request URI",
            )
        }
    };
    let original_uri = parts.uri.clone();
    let mut next_parts = Some(parts);
    let deadline = router
        .retry
        .as_ref()
        .filter(|p| p.total_timeout_ms > 0)
        .map(|p| tokio::time::Instant::now() + Duration::from_millis(p.total_timeout_ms.into()));
    let mut pending = PendingCapture::new(UsageCapture {
        runtime: routing_table.runtime.clone(),
        caller: caller.clone(),
        request,
        handles: guard.metrics.clone(),
        model: Some(guard.model.clone()),
        config_version: routing_table.version,
        endpoint,
        stream,
        status: 499,
    });
    let mut tried = [0; 5];
    // The latest upstream response kept while a later attempt runs, with the
    // reservation (model, metrics handles, in-flight slot) it belongs to.
    let mut last_response: Option<(Response<Incoming>, bool, bool, InFlight)> = None;
    let mut used_fallback = initial_fallback.is_some();
    let mut current_fallback = initial_fallback;
    let mut attempt = 0usize;
    let (response, fallback_response, inject) = loop {
        attempt += 1;
        tried[attempt - 1] = guard.model.label;
        let model = &guard.model;
        let inject = stream
            && model.stream_usage_mode == StreamUsageMode::IncludeUsage
            && !include_usage.requested();
        let rewritten = fields.rewrite(
            &body,
            &model.upstream_model_json,
            inject.then_some(&include_usage),
        );
        let mut parts = next_parts
            .take()
            .unwrap_or_else(|| replay.as_ref().expect("retry template").clone());
        let uri = match first_uri.take() {
            Some(uri) => Ok(uri),
            None => upstream_uri(model, &original_uri),
        };
        parts.uri = match uri {
            Ok(uri) => uri,
            Err(_) => {
                pending.discard();
                return error::json(
                    StatusCode::BAD_REQUEST,
                    "invalid_request",
                    "Invalid request URI",
                );
            }
        };
        if let Some(value) = &model.authorization {
            parts.headers.insert(header::AUTHORIZATION, value.clone());
        }
        parts.headers.insert(header::HOST, model.host.clone());
        let span = if observation.otel {
            tracing::info_span!(parent: &observation.span, "upstream_attempt",
                model = %model.name, attempt, outcome = tracing::field::Empty)
        } else {
            tracing::Span::none()
        };
        if observation.otel {
            let _entered = span.enter();
            telemetry::inject(&mut parts.headers);
        }
        let upstream_request = Request::from_parts(parts, Full::new(rewritten));
        // `capped`: the router's total_timeout, not the model's own
        // timeout, bounds this attempt.
        let (timeout, capped) = match deadline {
            Some(end) => {
                let left = end.saturating_duration_since(tokio::time::Instant::now());
                if left < model.timeout {
                    (left, true)
                } else {
                    (model.timeout, false)
                }
            }
            None => (model.timeout, false),
        };
        observation
            .upstream_started
            .get_or_insert_with(Instant::now);
        // Only models behind a LOWEST_LATENCY router pay for a clock read.
        let attempt_started = model.track_latency.then(Instant::now);
        let result = tokio::time::timeout(timeout, client.request(upstream_request))
            .instrument(span.clone())
            .await;
        let (response, outcome, retryable, error_status) = match result {
            Ok(Ok(response)) => {
                let status = response.status();
                let retryable = retry_status(router.retry.as_ref(), status.as_u16());
                if matches!(status.as_u16(), 429 | 503) {
                    if let Some(delay) = response
                        .headers()
                        .get(header::RETRY_AFTER)
                        .and_then(|v| v.to_str().ok())
                        .and_then(|v| retry_after(v, SystemTime::now()))
                    {
                        guard.model.breaker.retry_after.fetch_max(
                            clock::mono_ms()
                                .saturating_add(cooldown_ms(delay, guard.model.circuit)),
                            std::sync::atomic::Ordering::Relaxed,
                        );
                    }
                }
                let outcome = if status.is_success() {
                    0
                } else if retryable {
                    1
                } else {
                    5
                };
                (
                    Some(response),
                    outcome,
                    retryable && !stream,
                    StatusCode::BAD_GATEWAY,
                )
            }
            Ok(Err(err)) => {
                guard.metrics.upstream_error(if err.is_connect() {
                    UpstreamError::Connect
                } else {
                    UpstreamError::Protocol
                });
                (None, 2, true, StatusCode::BAD_GATEWAY)
            }
            Err(_) => {
                guard.metrics.upstream_error(UpstreamError::Timeout);
                (None, 3, true, StatusCode::GATEWAY_TIMEOUT)
            }
        };
        guard.metrics.attempts[outcome].inc();
        span.record("outcome", ATTEMPT_OUTCOMES[outcome]);
        // An attempt cut short by the router's total_timeout says nothing
        // about the model: no breaker verdict, no latency sample (it
        // still counts as outcome="timeout" in the attempts metric).
        let cut_short = outcome == 3 && capped;
        let failed = matches!(outcome, 2 | 3)
            || (outcome == 1
                && response
                    .as_ref()
                    .is_some_and(|r| r.status().is_server_error() || r.status().as_u16() == 429));
        if let Some(started) = attempt_started.filter(|_| !cut_short) {
            // Successes feed the EWMA; breaker-worthy failures add a penalty;
            // client errors (4xx other than 429) say nothing about latency.
            if outcome == 0 {
                guard.model.latency.success(started.elapsed());
            } else if failed {
                guard.model.latency.failure(started.elapsed());
            }
        }
        // Left unfinished, a cut-short half-open trial is abandoned on drop.
        if !cut_short {
            guard.complete(failed);
        }
        if retryable
            && attempt < max_attempts as usize
            && deadline.is_none_or(|end| tokio::time::Instant::now() < end)
        {
            let headers = &replay.as_ref().expect("retries keep a template").headers;
            if let Some((next, fallback)) = routing_table.retry(
                router,
                endpoint,
                &caller,
                headers,
                &tried[..attempt],
                split,
                &mut skipped,
            ) {
                if routing_table.runtime.retry_budget.take() {
                    if let Some(counter) = fallback {
                        if !current_fallback.is_some_and(|previous| std::ptr::eq(previous, counter))
                        {
                            counter.inc();
                        }
                    }
                    current_fallback = fallback;
                    let previous = std::mem::replace(&mut guard, next);
                    match response {
                        // Keep the response with its own reservation: it may
                        // still be the final answer. Replacing an older kept
                        // response releases that one's slot.
                        Some(response) => {
                            last_response = Some((response, used_fallback, inject, previous))
                        }
                        // Releases the abandoned attempt's in-flight slot.
                        None => drop(previous),
                    }
                    used_fallback = fallback.is_some();
                    observation.routed(guard.metrics.clone(), stream);
                    let mut capture = pending.take();
                    capture.handles = guard.metrics.clone();
                    capture.model = Some(guard.model.clone());
                    pending = PendingCapture::new(capture);
                    continue;
                }
                router.budget_exhausted.inc();
            }
        }
        if let Some(response) = response {
            break (response, used_fallback, inject);
        }
        match last_response.take() {
            Some((response, fallback, inject, kept)) => {
                // The earlier attempt's response is the answer: account it to
                // that attempt's model and release this failed one.
                guard = kept;
                observation.routed(guard.metrics.clone(), stream);
                let mut capture = pending.take();
                capture.handles = guard.metrics.clone();
                capture.model = Some(guard.model.clone());
                pending = PendingCapture::new(capture);
                break (response, fallback, inject);
            }
            None => {
                let mut capture = pending.take();
                capture.status = error_status.as_u16();
                capture.finalize(RequestOutcome::UpstreamError, UsageState::Missing, None);
                return if error_status == StatusCode::GATEWAY_TIMEOUT {
                    error::json(
                        error_status,
                        "upstream_timeout",
                        "Upstream response timed out",
                    )
                } else {
                    error::json(error_status, "upstream_error", "Upstream connection failed")
                };
            }
        }
    };
    observation.headers_received = Some(Instant::now());
    let (mut parts, body) = response.into_parts();
    strip_response_headers(&mut parts.headers);
    let meter = Meter::select(&parts, stream, inject, include_usage.requested());
    let mut capture = pending.take();
    capture.status = parts.status.as_u16();
    let alias = (fallback_response && parts.status.is_success()).then(|| {
        AliasBody::new(
            serde_json::to_vec(model.as_ref()).expect("model serializes"),
            stream
                && !parts
                    .headers
                    .get(header::CONTENT_TYPE)
                    .is_some_and(|v| v.as_bytes().starts_with(b"application/json")),
        )
    });
    let body = MeteredBody {
        body,
        guard: Some(guard),
        permit: Some(permit),
        meter,
        capture: Some(capture),
        end: None,
        held: None,
    }
    .boxed_unsync();
    Response::from_parts(
        parts,
        match alias {
            Some(mut alias) => {
                alias.body = body;
                alias.boxed_unsync()
            }
            None => body,
        },
    )
}

fn retry_status(policy: Option<&lumewarp_proto::config::v1::RetryPolicy>, status: u16) -> bool {
    match policy {
        Some(p) if !p.retry_on_status.is_empty() => p.retry_on_status.contains(&u32::from(status)),
        _ => matches!(status, 429 | 502 | 503 | 504),
    }
}

/// Longest `Retry-After` cooldown honoured.
const MAX_COOLDOWN_MS: u64 = 60_000;

/// A model's `Retry-After` cooldown in ms: at most 60 s, and never
/// longer than its breaker's open interval when one is configured.
fn cooldown_ms(delay: Duration, circuit: Option<CircuitBreaker>) -> u64 {
    let ms = u64::try_from(delay.as_millis())
        .unwrap_or(u64::MAX)
        .min(MAX_COOLDOWN_MS);
    circuit.map_or(ms, |config| ms.min(Breaker::open_ms(&config)))
}

fn retry_after(value: &str, now: SystemTime) -> Option<Duration> {
    let value = value.trim();
    if !value.is_empty() && value.bytes().all(|b| b.is_ascii_digit()) {
        return value.parse().ok().map(Duration::from_secs);
    }
    httpdate::parse_http_date(value)
        .ok()
        .map(|at| at.duration_since(now).unwrap_or_default())
}

#[cfg(test)]
mod resilience_tests {
    use super::*;
    use lumewarp_proto::config::v1::RetryPolicy;

    #[test]
    fn retry_status_defaults_and_explicit_override() {
        for status in [429, 502, 503, 504] {
            assert!(retry_status(None, status));
        }
        for status in [200, 400, 500] {
            assert!(!retry_status(None, status));
        }
        let policy = RetryPolicy {
            retry_on_status: vec![500],
            ..Default::default()
        };
        assert!(retry_status(Some(&policy), 500));
        assert!(!retry_status(Some(&policy), 503));
    }

    #[test]
    fn retry_after_seconds_dates_and_invalid_values() {
        let now = SystemTime::UNIX_EPOCH + Duration::from_secs(1_000_000);
        assert_eq!(retry_after(" 30 ", now), Some(Duration::from_secs(30)));
        assert_eq!(
            retry_after(&httpdate::fmt_http_date(now + Duration::from_secs(10)), now),
            Some(Duration::from_secs(10))
        );
        assert_eq!(
            retry_after(&httpdate::fmt_http_date(now - Duration::from_secs(1)), now),
            Some(Duration::ZERO)
        );
        for value in ["", "-1", "1.5", "tomorrow", "18446744073709551616"] {
            assert_eq!(retry_after(value, now), None);
        }
    }

    #[test]
    fn fallback_alias_rewrites_json_and_split_multiline_sse() {
        let mut alias = AliasBody::new(br#""public""#.to_vec(), false);
        alias.pending = br#"{ "model": "backup", "extra":{"model":"untouched"} }"#.to_vec();
        assert_eq!(
            alias.flush(true),
            br#"{ "model": "public", "extra":{"model":"untouched"} }"#.as_slice()
        );
        let mut alias = AliasBody::new(br#""public""#.to_vec(), true);
        alias.pending.extend_from_slice(b"data: {\"model\":\r\n");
        assert!(alias.flush(false).is_empty());
        alias
            .pending
            .extend_from_slice(b"data: \"backup\"}\r\n\r\n");
        let event = alias.flush(false);
        assert_eq!(
            event,
            b"data: {\"model\":\ndata: \"public\"}\n\n".as_slice()
        );
        // Valid SSE: one event, every line a `data:` field, and the joined
        // field values are the rewritten JSON.
        let text = std::str::from_utf8(&event).unwrap();
        let body = text.strip_suffix("\n\n").expect("event terminator");
        let data: Vec<&str> = body
            .split('\n')
            .map(|line| line.strip_prefix("data: ").expect("data field"))
            .collect();
        let value: serde_json::Value = serde_json::from_str(&data.join("\n")).unwrap();
        assert_eq!(value["model"], "public");
        // Comments and other fields are kept; a single-line payload is unchanged.
        alias
            .pending
            .extend_from_slice(b": keep\nevent: chunk\ndata: {\"model\":\"backup\"}\n\n");
        assert_eq!(
            alias.flush(false),
            b": keep\nevent: chunk\ndata: {\"model\":\"public\"}\n\n".as_slice()
        );
    }

    #[test]
    fn retry_after_cooldown_is_clamped() {
        let s = Duration::from_secs;
        assert_eq!(cooldown_ms(s(10), None), 10_000);
        assert_eq!(cooldown_ms(s(3600), None), MAX_COOLDOWN_MS);
        assert_eq!(cooldown_ms(Duration::MAX, None), MAX_COOLDOWN_MS);
        let breaker = |open_ms| {
            Some(CircuitBreaker {
                failure_threshold: 1,
                open_ms,
            })
        };
        assert_eq!(cooldown_ms(s(120), breaker(5_000)), 5_000);
        assert_eq!(cooldown_ms(s(2), breaker(5_000)), 2_000);
        // open_ms 0 is the 30 s default.
        assert_eq!(cooldown_ms(s(120), breaker(0)), 30_000);
    }
}

/// Only fallback responses need alias restoration. Bound buffering to one SSE
/// event or one JSON response; ordinary responses retain the original body.
struct AliasBody {
    body: ProxyBody,
    model: Vec<u8>,
    pending: Vec<u8>,
    stream: bool,
    ended: bool,
    held: Option<Frame<Bytes>>,
}

impl AliasBody {
    fn new(model: Vec<u8>, stream: bool) -> Self {
        Self {
            body: full_body(Bytes::new()),
            model,
            pending: Vec::new(),
            stream,
            ended: false,
            held: None,
        }
    }

    fn rewrite(&self, bytes: &[u8]) -> Bytes {
        match RequestFields::parse(bytes) {
            Ok(fields) => fields.rewrite(bytes, &self.model, None),
            Err(_) => Bytes::copy_from_slice(bytes),
        }
    }

    fn flush(&mut self, eof: bool) -> Bytes {
        if !self.stream {
            if !eof {
                return Bytes::new();
            }
            let pending = std::mem::take(&mut self.pending);
            return self.rewrite(&pending);
        }
        let mut output = Vec::new();
        // SSE field values are UTF-8 text; keep comments and non-data fields.
        // Reassemble multiline data values before parsing their JSON.
        while let Some(end) = sse_event_end(&self.pending)
            .or_else(|| (eof && !self.pending.is_empty()).then_some(self.pending.len()))
        {
            let event: Vec<_> = self.pending.drain(..end).collect();
            let mut data = Vec::new();
            let mut metadata = Vec::new();
            for line in event.split(|b| *b == b'\n') {
                let line = line.strip_suffix(b"\r").unwrap_or(line);
                if let Some(value) = line.strip_prefix(b"data:") {
                    if !data.is_empty() {
                        data.push(b'\n');
                    }
                    data.extend_from_slice(value.strip_prefix(b" ").unwrap_or(value));
                } else if !line.is_empty() {
                    metadata.extend_from_slice(line);
                    metadata.push(b'\n');
                }
            }
            output.extend_from_slice(&metadata);
            if !data.is_empty() {
                // Every line of the (possibly multiline) payload is its own
                // `data:` field, so the client reassembles the same value.
                for line in self.rewrite(&data).split(|b| *b == b'\n') {
                    output.extend_from_slice(b"data: ");
                    output.extend_from_slice(line);
                    output.push(b'\n');
                }
            }
            output.push(b'\n');
        }
        Bytes::from(output)
    }
}

fn sse_event_end(bytes: &[u8]) -> Option<usize> {
    let mut line_start = 0;
    let mut at = 0;
    while at < bytes.len() {
        if matches!(bytes[at], b'\r' | b'\n') {
            let empty = at == line_start;
            if bytes[at] == b'\r' {
                if at + 1 == bytes.len() {
                    return None;
                }
                if bytes[at + 1] == b'\n' {
                    at += 1;
                }
            }
            at += 1;
            if empty {
                return Some(at);
            }
            line_start = at;
        } else {
            at += 1;
        }
    }
    None
}

impl Body for AliasBody {
    type Data = Bytes;
    type Error = BoxError;
    fn poll_frame(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
    ) -> Poll<Option<Result<Frame<Bytes>, BoxError>>> {
        if let Some(frame) = self.held.take() {
            return Poll::Ready(Some(Ok(frame)));
        }
        if self.ended {
            return Poll::Ready(None);
        }
        loop {
            match Pin::new(&mut self.body).poll_frame(cx) {
                Poll::Pending => return Poll::Pending,
                Poll::Ready(Some(Err(err))) => {
                    self.ended = true;
                    return Poll::Ready(Some(Err(err)));
                }
                Poll::Ready(frame) => {
                    let eof = frame.is_none();
                    if let Some(Ok(frame)) = frame {
                        match frame.into_data() {
                            Ok(data) => self.pending.extend_from_slice(&data),
                            Err(trailers) => {
                                self.held = Some(trailers);
                                let tail = self.flush(true);
                                if !tail.is_empty() {
                                    return Poll::Ready(Some(Ok(Frame::data(tail))));
                                }
                                return Poll::Ready(self.held.take().map(Ok));
                            }
                        }
                    }
                    if self.pending.len() > MAX_BODY {
                        self.ended = true;
                        return Poll::Ready(Some(Err(std::io::Error::other(
                            "fallback response exceeds alias rewrite limit",
                        )
                        .into())));
                    }
                    self.ended = eof;
                    let data = self.flush(eof);
                    if !data.is_empty() {
                        return Poll::Ready(Some(Ok(Frame::data(data))));
                    }
                    if eof {
                        return Poll::Ready(None);
                    }
                }
            }
        }
    }
    fn is_end_stream(&self) -> bool {
        self.ended && self.held.is_none()
    }
}

fn upstream_uri(model: &Model, request: &Uri) -> Result<Uri, hyper::http::Error> {
    let index = match request.path() {
        "/v1/chat/completions" => 0,
        "/v1/completions" => 1,
        "/v1/embeddings" => 2,
        _ => unreachable!("dispatch only forwards supported endpoints"),
    };
    let endpoint = &model.endpoints[index];
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

/// How a response body is metered.
enum Meter {
    /// Nothing to read: error status, or a passthrough stream the client did
    /// not ask usage for. Reported MISSING.
    Off,
    Sse(SseFilter),
    Json(Tee),
    /// Content-encoded body; not decompressed. Reported INVALID.
    Encoded,
}

impl Meter {
    fn select(
        parts: &hyper::http::response::Parts,
        stream: bool,
        inject: bool,
        requested: bool,
    ) -> Self {
        if !parts.status.is_success() {
            return Self::Off;
        }
        // Before choosing a parser: compressed bytes are never parsed or
        // filtered, whatever the content type.
        if parts
            .headers
            .get(header::CONTENT_ENCODING)
            .is_some_and(|value| !value.as_bytes().eq_ignore_ascii_case(b"identity"))
        {
            return Self::Encoded;
        }
        let json = parts
            .headers
            .get(header::CONTENT_TYPE)
            .is_some_and(|value| value.as_bytes().starts_with(b"application/json"));
        if stream && !json {
            return if inject || requested {
                Self::Sse(SseFilter::new(inject))
            } else {
                Self::Off
            };
        }
        Self::Json(Tee::default())
    }

    fn holds_bytes(&self) -> bool {
        matches!(self, Self::Sse(filter) if filter.holds_bytes())
    }

    fn rewrites(&self) -> bool {
        matches!(self, Self::Sse(filter) if filter.strips())
    }
}

/// Upstream response body: meters usage while forwarding, holds the
/// model reservation and rate-limit permit until EOF / error / drop,
/// and finalizes the usage capture exactly once when dropped.
struct MeteredBody {
    body: Incoming,
    guard: Option<InFlight>,
    permit: Option<Permit>,
    meter: Meter,
    capture: Option<UsageCapture>,
    /// Set when the upstream body ended (EOF or error).
    end: Option<RequestOutcome>,
    /// A trailers frame waiting behind flushed SSE bytes.
    held: Option<Frame<Bytes>>,
}

impl MeteredBody {
    fn release(&mut self) -> Option<InFlight> {
        self.permit.take();
        self.guard.take()
    }

    fn meter(&mut self, data: Bytes) -> Bytes {
        match &mut self.meter {
            Meter::Sse(filter) => filter.push(data),
            Meter::Json(tee) => {
                tee.push(&data);
                data
            }
            Meter::Off | Meter::Encoded => data,
        }
    }

    /// Upstream EOF: returns bytes still held by the SSE filter.
    fn eof(&mut self) -> Bytes {
        self.end = Some(RequestOutcome::Completed);
        self.release();
        match &mut self.meter {
            Meter::Sse(filter) => filter.push_eof(),
            _ => Bytes::new(),
        }
    }

    fn finalize(&mut self) {
        let Some(capture) = self.capture.take() else {
            return;
        };
        let end = self.end.or_else(|| {
            // Never polled to EOF, but nothing was left (e.g. an empty body).
            (self.body.is_end_stream() && !self.meter.holds_bytes())
                .then_some(RequestOutcome::Completed)
        });
        let completed = end == Some(RequestOutcome::Completed);
        let outcome = match end {
            Some(RequestOutcome::Completed) if capture.status >= 500 => {
                RequestOutcome::UpstreamError
            }
            Some(outcome) => outcome,
            None => RequestOutcome::ClientCancelled,
        };
        let (captured, partial) = match std::mem::replace(&mut self.meter, Meter::Off) {
            Meter::Sse(filter) => (filter.captured(), !completed),
            Meter::Json(tee) if completed => (tee.finish(), false),
            Meter::Json(_) => (Captured::Missing, true),
            Meter::Encoded => (Captured::Invalid, false),
            Meter::Off => (Captured::Missing, false),
        };
        let (state, tokens) = match captured {
            Captured::Reported(tokens) => (UsageState::Reported, Some(tokens)),
            Captured::Invalid => (UsageState::Invalid, None),
            Captured::Missing if partial => (UsageState::Partial, None),
            Captured::Missing => (UsageState::Missing, None),
        };
        capture.finalize(outcome, state, tokens);
    }
}

impl Drop for MeteredBody {
    fn drop(&mut self) {
        self.finalize();
    }
}

impl Body for MeteredBody {
    type Data = Bytes;
    type Error = BoxError;

    fn poll_frame(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
    ) -> Poll<Option<Result<Frame<Bytes>, BoxError>>> {
        let this = &mut *self;
        if let Some(frame) = this.held.take() {
            return Poll::Ready(Some(Ok(frame)));
        }
        if this.end.is_some() {
            return Poll::Ready(None);
        }
        loop {
            match Pin::new(&mut this.body).poll_frame(cx) {
                Poll::Pending => return Poll::Pending,
                Poll::Ready(None) => {
                    let tail = this.eof();
                    return if tail.is_empty() {
                        Poll::Ready(None)
                    } else {
                        Poll::Ready(Some(Ok(Frame::data(tail))))
                    };
                }
                Poll::Ready(Some(Err(err))) => {
                    this.end = Some(RequestOutcome::UpstreamError);
                    if let Some(guard) = this.release() {
                        guard.metrics.upstream_error(UpstreamError::Stream);
                    }
                    return Poll::Ready(Some(Err(Box::new(err))));
                }
                Poll::Ready(Some(Ok(frame))) => match frame.into_data() {
                    Ok(data) => {
                        let mut data = this.meter(data);
                        if this.body.is_end_stream() {
                            let tail = this.eof();
                            if !tail.is_empty() {
                                data = if data.is_empty() {
                                    tail
                                } else {
                                    let mut joined =
                                        BytesMut::with_capacity(data.len() + tail.len());
                                    joined.extend_from_slice(&data);
                                    joined.extend_from_slice(&tail);
                                    joined.freeze()
                                };
                            }
                            if data.is_empty() {
                                return Poll::Ready(None);
                            }
                        } else if data.is_empty() {
                            // Held back or dropped: nothing to forward yet.
                            continue;
                        }
                        return Poll::Ready(Some(Ok(Frame::data(data))));
                    }
                    Err(frame) => {
                        if this.body.is_end_stream() {
                            let tail = this.eof();
                            if !tail.is_empty() {
                                this.held = Some(frame);
                                return Poll::Ready(Some(Ok(Frame::data(tail))));
                            }
                        }
                        return Poll::Ready(Some(Ok(frame)));
                    }
                },
            }
        }
    }

    fn is_end_stream(&self) -> bool {
        self.held.is_none()
            && (self.end.is_some() || (self.body.is_end_stream() && !self.meter.holds_bytes()))
    }

    fn size_hint(&self) -> SizeHint {
        if self.meter.rewrites() {
            // Stripping changes the length; only the upper bound would hold.
            SizeHint::default()
        } else if self.end.is_some() {
            SizeHint::with_exact(0)
        } else {
            self.body.size_hint()
        }
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
        assert_eq!(
            fields.rewrite(body, br#""actual""#, None),
            expected.as_bytes()
        );
    }

    fn forced(body: &[u8]) -> Result<String, &'static str> {
        let fields = RequestFields::parse(body)?;
        let include_usage = fields.include_usage()?;
        let rewritten = fields.rewrite(body, br#""up""#, Some(&include_usage));
        Ok(String::from_utf8(rewritten.to_vec()).unwrap())
    }

    #[test]
    fn forces_include_usage_by_splicing() {
        for (body, expected) in [
            (
                r#" { "model":"public", "stream":true }"#,
                r#" {"stream_options":{"include_usage":true}, "model":"up", "stream":true }"#,
            ),
            (
                r#"{"stream_options":null,"model":"public"}"#,
                r#"{"stream_options":{"include_usage":true},"model":"up"}"#,
            ),
            (
                r#"{"model":"public","stream_options":{ }}"#,
                r#"{"model":"up","stream_options":{"include_usage":true }}"#,
            ),
            (
                r#"{"model":"public","stream_options":{"continuous_usage_stats":true,"x":{"include_usage":false}}}"#,
                r#"{"model":"up","stream_options":{"include_usage":true,"continuous_usage_stats":true,"x":{"include_usage":false}}}"#,
            ),
            (
                r#"{"stream_options":{"include_usage" : false},"model":"public","extra_body":{"guided_json":{}}}"#,
                r#"{"stream_options":{"include_usage" : true},"model":"up","extra_body":{"guided_json":{}}}"#,
            ),
            (
                r#"{"model":"public","stream_options":{"include_usage":null}}"#,
                r#"{"model":"up","stream_options":{"include_usage":true}}"#,
            ),
            (
                r#"{"model":"public","stream_options":{"include_usage":true}}"#,
                r#"{"model":"up","stream_options":{"include_usage":true}}"#,
            ),
        ] {
            assert_eq!(forced(body.as_bytes()).unwrap(), expected, "{body}");
            let rewritten = forced(body.as_bytes()).unwrap();
            let value: serde_json::Value = serde_json::from_str(&rewritten).unwrap();
            assert_eq!(value["stream_options"]["include_usage"], true);
        }
        let fields =
            RequestFields::parse(br#"{"model":"m","stream_options":{"include_usage":true}}"#)
                .unwrap();
        assert!(fields.include_usage().unwrap().requested());
        let fields = RequestFields::parse(br#"{"model":"m"}"#).unwrap();
        assert!(!fields.include_usage().unwrap().requested());
    }

    #[test]
    fn rejects_invalid_stream_options() {
        for body in [
            r#"{"model":"m","stream_options":true}"#,
            r#"{"model":"m","stream_options":[]}"#,
            r#"{"model":"m","stream_options":{"include_usage":"yes"}}"#,
            r#"{"model":"m","stream_options":{"include_usage":1}}"#,
            r#"{"model":"m","stream_options":{"include_usage":true,"include_usage":false}}"#,
        ] {
            assert!(forced(body.as_bytes()).is_err(), "{body}");
        }
        assert_eq!(
            RequestFields::parse(br#"{"model":"m","stream_options":{},"stream_options":{}}"#).err(),
            Some("Expected at most one stream_options field")
        );
    }

    #[test]
    fn rewrite_unicode_and_escaped_names() {
        let body = br#"{"model":"\u6a21\u578b\"\\","other":"\u1234"}"#;
        let fields = RequestFields::parse(body).unwrap();
        assert_eq!(fields.model().unwrap(), "模型\"\\");
        let rewritten = fields.rewrite(body, &serde_json::to_vec("上游\"\\").unwrap(), None);
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
models:
  - {name: d, upstream: u, upstream_model: actual}
routers:
  - {name: public, models: [d]}
"#,
        )
        .unwrap();
        let routing_table = RoutingTable::compile(
            &snapshot,
            lumewarp_config::AuthMode::Open,
            &crate::runtime::Runtime::new(),
        )
        .unwrap();
        let guard = routing_table.acquire(&routing_table.routers["public"], 0, &Caller::Anonymous);
        let model = &guard.model;
        assert_eq!(model.host, "localhost:8123");
        assert_eq!(model.upstream_model_json, br#""actual""#.as_slice());
        for suffix in ["/chat/completions", "/completions", "/embeddings"] {
            let request: Uri = format!("/v1{suffix}").parse().unwrap();
            assert_eq!(
                upstream_uri(model, &request).unwrap().to_string(),
                format!("http://localhost:8123/prefix/v1{suffix}")
            );
            let request: Uri = format!("/v1{suffix}?x=a%2Fb&x=two").parse().unwrap();
            assert_eq!(
                upstream_uri(model, &request).unwrap().to_string(),
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
