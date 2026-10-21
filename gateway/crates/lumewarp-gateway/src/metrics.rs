//! Duration covers handler entry through response-body completion or drop.
//! `lumewarp_request_body_read_seconds{route}` measures only the client body
//! collection interval, including failed, timed-out and cancelled reads.
//! Gateway overhead starts after collection and excludes all upstream waits;
//! see `overhead_seconds` for the exact stream and non-stream approximation.

use std::{
    pin::Pin,
    sync::{atomic::Ordering, Arc},
    task::{Context, Poll},
    time::{Duration, Instant},
};

use bytes::Bytes;
use http_body_util::BodyExt;
use hyper::{
    body::{Body, Frame, SizeHint},
    StatusCode,
};
use prometheus_client::{
    encoding::{text::encode, EncodeLabelSet},
    metrics::{counter::Counter, family::Family, gauge::Gauge, histogram::Histogram},
    registry::Registry,
};
use tracing::Span;

use crate::{
    proxy::{BoxError, ProxyBody},
    router::Deployment,
};

pub const ROUTES: [&str; 7] = [
    "chat_completions",
    "completions",
    "embeddings",
    "models",
    "healthz",
    "metrics",
    "other",
];
const STATUSES: [&str; 14] = [
    "200", "400", "401", "404", "408", "413", "429", "500", "502", "503", "504", "other", "204",
    "499",
];
const ERROR_KINDS: [&str; 4] = ["connect", "timeout", "stream", "protocol"];

pub enum UpstreamError {
    Connect,
    Timeout,
    Stream,
    Protocol,
}

pub fn route_index(path: &str) -> usize {
    match path {
        "/v1/chat/completions" => 0,
        "/v1/completions" => 1,
        "/v1/embeddings" => 2,
        "/v1/models" => 3,
        "/healthz" => 4,
        "/metrics" => 5,
        _ => 6,
    }
}

#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct Labels {
    model: String,
    route: &'static str,
    deployment: String,
}
#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct RequestLabels {
    model: String,
    route: &'static str,
    deployment: String,
    status: &'static str,
}
#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct EndpointLabels {
    route: &'static str,
}
#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct DeploymentLabels {
    deployment: String,
}
#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct TtftLabels {
    model: String,
    deployment: String,
}
#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct ErrorLabels {
    deployment: String,
    kind: &'static str,
}
#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct ConfigLabels {
    source: &'static str,
    sha256: String,
}

type Histograms<L> = Family<L, Histogram, fn() -> Histogram>;
fn duration_histogram() -> Histogram {
    Histogram::new([
        0.000025, 0.00005, 0.0001, 0.00025, 0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1,
        0.25, 0.5, 1., 2.5, 5., 10., 30., 60.,
    ])
}
fn overhead_histogram() -> Histogram {
    Histogram::new([
        0.000025, 0.00005, 0.0001, 0.00025, 0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05,
    ])
}

pub struct RequestMetrics {
    pub model: String,
    pub deployment: String,
    counters: [Counter; 14],
    duration: Histogram,
    overhead: Histogram,
    body_read: Histogram,
    ttft: Option<Histogram>,
    errors: Option<[Counter; 4]>,
}

impl RequestMetrics {
    pub fn upstream_error(&self, kind: UpstreamError) {
        if let Some(errors) = &self.errors {
            errors[kind as usize].inc();
        }
    }
}

pub struct Metrics {
    registry: Registry,
    requests: Family<RequestLabels, Counter>,
    durations: Histograms<Labels>,
    overhead: Histograms<EndpointLabels>,
    body_read: Histograms<EndpointLabels>,
    ttft: Histograms<TtftLabels>,
    errors: Family<ErrorLabels, Counter>,
    config_info: Family<ConfigLabels, Gauge>,
    in_flight: Vec<(Arc<Deployment>, Gauge)>,
    gauges: Family<DeploymentLabels, Gauge>,
}

impl Metrics {
    pub fn new() -> Self {
        let mut registry = Registry::default();
        let requests = Family::default();
        let durations = Family::new_with_constructor(duration_histogram as fn() -> Histogram);
        let overhead = Family::new_with_constructor(overhead_histogram as fn() -> Histogram);
        let body_read = Family::new_with_constructor(duration_histogram as fn() -> Histogram);
        let ttft = Family::new_with_constructor(duration_histogram as fn() -> Histogram);
        let errors = Family::default();
        let gauges = Family::default();
        let config_info = Family::default();
        registry.register(
            "lumewarp_requests",
            "Completed requests, including dropped response bodies",
            requests.clone(),
        );
        registry.register(
            "lumewarp_request_duration_seconds",
            "End-to-end request duration",
            durations.clone(),
        );
        registry.register(
            "lumewarp_gateway_overhead_seconds",
            "Gateway processing time excluding client body upload and upstream waits",
            overhead.clone(),
        );
        registry.register(
            "lumewarp_request_body_read_seconds",
            "Time spent reading client request bodies",
            body_read.clone(),
        );
        registry.register(
            "lumewarp_ttft_seconds",
            "Stream time to first forwarded data frame",
            ttft.clone(),
        );
        registry.register(
            "lumewarp_upstream_errors",
            "Upstream connect, timeout, body stream and protocol errors",
            errors.clone(),
        );
        registry.register(
            "lumewarp_in_flight_requests",
            "Requests holding a deployment reservation",
            gauges.clone(),
        );
        registry.register(
            "lumewarp_config_version",
            "Standalone YAML config version",
            Gauge::<i64>::default(),
        );
        registry.register(
            "lumewarp_config_info",
            "Config source and first twelve hex SHA-256 characters",
            config_info.clone(),
        );
        Self {
            registry,
            requests,
            durations,
            overhead,
            body_read,
            ttft,
            errors,
            config_info,
            in_flight: Vec::new(),
            gauges,
        }
    }

    pub fn set_yaml_hash(&self, hash: &str) {
        self.config_info
            .get_or_create(&ConfigLabels {
                source: "yaml",
                sha256: hash.into(),
            })
            .set(1);
    }

    pub fn register_deployment(&mut self, deployment: Arc<Deployment>) {
        let gauge = self
            .gauges
            .get_or_create(&DeploymentLabels {
                deployment: deployment.name.clone(),
            })
            .clone();
        for kind in ERROR_KINDS {
            drop(self.errors.get_or_create(&ErrorLabels {
                deployment: deployment.name.clone(),
                kind,
            }));
        }
        self.in_flight.push((deployment, gauge));
    }

    // Only called while compiling the snapshot. Request handling clones direct handles.
    pub fn handles(&self, model: &str, deployment: &str, route: usize) -> Arc<RequestMetrics> {
        let counters = std::array::from_fn(|i| {
            self.requests
                .get_or_create(&RequestLabels {
                    model: model.into(),
                    route: ROUTES[route],
                    deployment: deployment.into(),
                    status: STATUSES[i],
                })
                .clone()
        });
        let duration = self
            .durations
            .get_or_create(&Labels {
                model: model.into(),
                route: ROUTES[route],
                deployment: deployment.into(),
            })
            .clone();
        let overhead = self
            .overhead
            .get_or_create(&EndpointLabels {
                route: ROUTES[route],
            })
            .clone();
        let body_read = self
            .body_read
            .get_or_create(&EndpointLabels {
                route: ROUTES[route],
            })
            .clone();
        let ttft = (!deployment.is_empty()).then(|| {
            self.ttft
                .get_or_create(&TtftLabels {
                    model: model.into(),
                    deployment: deployment.into(),
                })
                .clone()
        });
        let errors = (!deployment.is_empty()).then(|| {
            ERROR_KINDS.map(|kind| {
                self.errors
                    .get_or_create(&ErrorLabels {
                        deployment: deployment.into(),
                        kind,
                    })
                    .clone()
            })
        });
        Arc::new(RequestMetrics {
            model: model.into(),
            deployment: deployment.into(),
            counters,
            duration,
            overhead,
            body_read,
            ttft,
            errors,
        })
    }

    pub fn encode(&self) -> Result<String, std::fmt::Error> {
        for (deployment, gauge) in &self.in_flight {
            gauge.set(deployment.in_flight.load(Ordering::Relaxed) as i64);
        }
        let mut output = String::new();
        encode(&mut output, &self.registry)?;
        Ok(output)
    }
}

pub struct Observation {
    pub handles: Arc<RequestMetrics>,
    pub started: Instant,
    pub body_read: Option<Instant>,
    pub upstream_started: Option<Instant>,
    pub headers_received: Option<Instant>,
    pub stream: bool,
    pub otel: bool,
    pub span: Span,
    status: StatusCode,
    finished: bool,
    first_frame_at: Option<Instant>,
    first_frame_forwarding: Duration,
    body_read_started: Option<Instant>,
    handler_finished: Option<Instant>,
}

/// Gateway overhead excludes client uploads and all upstream waiting, including prefill.
/// For both stream and non-stream responses it is (body_read -> upstream sent) +
/// (upstream headers -> handler return). Streams additionally include the first
/// frame's local forwarding work (upstream frame ready -> frame forwarded).
/// Neither upstream body generation nor client socket flush/ACK time is measured.
/// Without routing, it is body_read -> handler return (request start if no body read).
/// Cancelled uploads contribute only to body-read duration, not gateway overhead.
fn overhead_seconds(
    body_read: Instant,
    sent: Option<Instant>,
    headers: Option<Instant>,
    handler_end: Instant,
    forwarding: Duration,
) -> f64 {
    let before = sent
        .unwrap_or(handler_end)
        .saturating_duration_since(body_read);
    let after = headers
        .map(|headers| handler_end.saturating_duration_since(headers))
        .unwrap_or_default();
    (before + after + forwarding).as_secs_f64()
}

impl Observation {
    pub fn new(handles: Arc<RequestMetrics>, span: Span, otel: bool) -> Self {
        Self {
            handles,
            started: Instant::now(),
            body_read: None,
            upstream_started: None,
            headers_received: None,
            stream: false,
            otel,
            span,
            status: StatusCode::from_u16(499).expect("valid cancellation status"),
            finished: false,
            first_frame_at: None,
            first_frame_forwarding: Duration::ZERO,
            body_read_started: None,
            handler_finished: None,
        }
    }

    pub fn reading_body(&mut self) {
        self.body_read_started = Some(Instant::now());
    }

    pub fn body_read_complete(&mut self) {
        self.body_read = Some(Instant::now());
    }

    /// Client upload/read time only; includes failed, timed-out or cancelled reads.
    fn body_read_duration(&self, now: Instant) -> Option<Duration> {
        self.body_read_started.map(|start| {
            self.body_read
                .unwrap_or(now)
                .saturating_duration_since(start)
        })
    }

    pub fn routed(&mut self, handles: Arc<RequestMetrics>, stream: bool) {
        self.handles = handles;
        self.stream = stream;
        self.span.record("model", &self.handles.model);
        self.span.record("deployment", &self.handles.deployment);
        self.span.record("stream", stream);
    }

    fn first_frame(&mut self, ready_at: Instant) {
        if self.stream && self.first_frame_at.is_none() {
            self.first_frame_at = Some(ready_at);
            if let Some(ttft) = self
                .handles
                .ttft
                .as_ref()
                .filter(|_| self.headers_received.is_some() && self.status.is_success())
            {
                // NOTE: Histogram::observe takes a shared write lock. Revisit sharding
                // or atomic buckets in W3 if 5k/10k RPS p99 shows contention.
                ttft.observe(
                    ready_at
                        .saturating_duration_since(self.started)
                        .as_secs_f64(),
                );
            }
            self.first_frame_forwarding = Instant::now().saturating_duration_since(ready_at);
        }
    }

    fn overhead(&self, now: Instant) -> f64 {
        let read = self.body_read.unwrap_or_else(|| {
            if self.body_read_started.is_some() {
                now
            } else {
                self.started
            }
        });
        overhead_seconds(
            read,
            self.upstream_started,
            self.headers_received,
            self.handler_finished.unwrap_or(now),
            self.first_frame_forwarding,
        )
    }

    fn finish(&mut self) {
        if self.finished {
            return;
        }
        self.finished = true;
        let now = Instant::now();
        let status = self.status.as_str();
        let index = STATUSES
            .iter()
            .position(|&known| known == status)
            .unwrap_or(11);
        self.handles.counters[index].inc();
        // NOTE: Histogram::observe uses shared write locks; profile contention in W3.
        self.handles
            .duration
            .observe(now.saturating_duration_since(self.started).as_secs_f64());
        self.handles.overhead.observe(self.overhead(now));
        if let Some(read) = self.body_read_duration(now) {
            self.handles.body_read.observe(read.as_secs_f64());
        }
        self.span.record("status", self.status.as_u16());
    }

    pub fn wrap(mut self, response: hyper::Response<ProxyBody>) -> hyper::Response<ProxyBody> {
        self.status = response.status();
        self.handler_finished = Some(Instant::now());
        let (parts, body) = response.into_parts();
        hyper::Response::from_parts(
            parts,
            ObservedBody {
                body,
                observation: self,
            }
            .boxed_unsync(),
        )
    }
}

impl Drop for Observation {
    fn drop(&mut self) {
        self.finish();
    }
}

struct ObservedBody {
    body: ProxyBody,
    observation: Observation,
}
impl Body for ObservedBody {
    type Data = Bytes;
    type Error = BoxError;
    fn poll_frame(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
    ) -> Poll<Option<Result<Frame<Bytes>, BoxError>>> {
        let frame = Pin::new(&mut self.body).poll_frame(cx);
        if let Poll::Ready(Some(Ok(frame))) = &frame {
            if frame.data_ref().is_some_and(|data| !data.is_empty()) {
                // poll_frame has returned Ready: upstream waiting is over. Only
                // local work from this point to forwarding belongs to overhead.
                self.observation.first_frame(Instant::now());
            }
        }
        if matches!(frame, Poll::Ready(None) | Poll::Ready(Some(Err(_))))
            || self.body.is_end_stream()
        {
            self.observation.finish();
        }
        frame
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
    use std::time::Duration;
    #[test]
    fn overhead_excludes_prefill_and_upload_but_ttft_includes_them() {
        let start = Instant::now();
        let read = start + Duration::from_secs(1); // Slow upload is not gateway overhead.
        let sent = read + Duration::from_millis(1);
        let headers = read + Duration::from_millis(2);
        let handler = headers + Duration::from_micros(100);
        let first_ready = read + Duration::from_millis(300);
        let metrics = Metrics::new();
        let mut observation =
            Observation::new(metrics.handles("public", "d", 0), Span::none(), false);
        observation.started = start;
        observation.body_read_started = Some(start);
        observation.body_read = Some(read);
        observation.upstream_started = Some(sent);
        observation.headers_received = Some(headers);
        observation.handler_finished = Some(handler);
        observation.stream = true;
        observation.status = StatusCode::OK;
        observation.first_frame(first_ready);
        observation.first_frame(first_ready + Duration::from_millis(10));
        // Deterministic 20 us local forwarding work; 298 ms prefill is excluded.
        observation.first_frame_forwarding = Duration::from_micros(20);
        assert!((observation.overhead(start + Duration::from_secs(3)) - 0.00112).abs() < 1e-9);
        assert_eq!(
            observation.body_read_duration(handler),
            Some(Duration::from_secs(1))
        );
        let mut cancelled_upload =
            Observation::new(metrics.handles("", "", 0), Span::none(), false);
        cancelled_upload.started = start;
        cancelled_upload.body_read_started = Some(start);
        assert_eq!(cancelled_upload.overhead(read), 0.0);
        assert_eq!(
            cancelled_upload.body_read_duration(read),
            Some(Duration::from_secs(1))
        );
        observation.stream = false;
        observation.first_frame_forwarding = Duration::ZERO;
        assert!((observation.overhead(start + Duration::from_secs(10)) - 0.0011).abs() < 1e-9);
        let text = metrics.encode().unwrap();
        assert!(text.contains("lumewarp_ttft_seconds_sum{model=\"public\",deployment=\"d\"} 1.3"));
        assert!(text.contains("lumewarp_ttft_seconds_count{model=\"public\",deployment=\"d\"} 1"));
    }

    #[test]
    fn encoder_and_bounded_labels() {
        let config = lumewarp_config::Config::from_yaml(
            r#"
upstreams:
  - {name: u, kind: vllm, base_url: 'http://localhost:8123/v1'}
deployments:
  - {name: d, upstream: u, upstream_model: actual}
routes:
  - {model: public, deployments: [d]}
"#,
        )
        .unwrap();
        let router = crate::router::Router::compile(&config).unwrap();
        let metrics = &router.metrics;
        metrics.set_yaml_hash("123456789abc");
        assert!(!router.routes.contains_key("client-supplied-secret-model"));
        let mut observation = Observation::new(router.unknown[0].clone(), Span::none(), false);
        observation.status = StatusCode::NOT_FOUND;
        observation.finish();
        observation.finish();
        let text = metrics.encode().unwrap();
        assert!(text.contains("lumewarp_requests_total{model=\"unknown\",route=\"chat_completions\",deployment=\"\",status=\"404\"} 1"));
        for name in [
            "lumewarp_requests_total",
            "lumewarp_request_duration_seconds",
            "lumewarp_gateway_overhead_seconds",
            "lumewarp_request_body_read_seconds",
            "lumewarp_ttft_seconds",
            "lumewarp_upstream_errors_total",
            "lumewarp_in_flight_requests",
            "lumewarp_config_version",
            "lumewarp_config_info",
        ] {
            assert!(text.contains(name), "missing {name}");
        }
        assert!(text.contains("model=\"unknown\""));
        assert!(!text.contains("client-supplied-secret-model"));
        assert!(!text.contains("model=\"unknown\",route=\"healthz\""));
        assert!(!text.contains("model=\"unknown\",route=\"metrics\""));
        assert!(!text.contains("model=\"unknown\",route=\"other\""));
        drop(Observation::new(
            router.unknown[0].clone(),
            Span::none(),
            false,
        ));
        assert!(metrics.encode().unwrap().contains(
            "model=\"unknown\",route=\"chat_completions\",deployment=\"\",status=\"499\"} 1"
        ));
        let guard = router.routes["public"].acquire_for(0);
        assert!(metrics
            .encode()
            .unwrap()
            .contains("lumewarp_in_flight_requests{deployment=\"d\"} 1"));
        drop(guard);
        assert!(metrics
            .encode()
            .unwrap()
            .contains("lumewarp_in_flight_requests{deployment=\"d\"} 0"));
        let mut fallback = Observation::new(router.unknown[0].clone(), Span::none(), false);
        fallback.status = StatusCode::IM_A_TEAPOT;
        drop(fallback);
        assert!(metrics.encode().unwrap().contains(
            "model=\"unknown\",route=\"chat_completions\",deployment=\"\",status=\"other\"} 1"
        ));
    }
}
