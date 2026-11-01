use hyper::{
    header::{HeaderName, HeaderValue},
    HeaderMap,
};
use opentelemetry::{
    propagation::{Extractor, Injector, TextMapPropagator},
    trace::TracerProvider,
};
use opentelemetry_sdk::{
    propagation::TraceContextPropagator,
    trace::{Sampler, SdkTracerProvider},
    Resource,
};
use tracing::Span;
use tracing_opentelemetry::OpenTelemetrySpanExt;
use tracing_subscriber::{
    filter::{LevelFilter, Targets},
    layer::SubscriberExt,
    util::SubscriberInitExt,
    EnvFilter, Layer,
};

/// The gateway decides sampling itself and never asks the parent. The SDK's
/// default (`ParentBased(AlwaysOn)`) obeys `traceparent` sampled=0, and OTel is
/// where a single request is looked up, so a client could erase its own record.
/// The parent's trace id is still used. `OTEL_TRACES_SAMPLER` keeps its standard
/// names, with `parentbased_*` meaning the same as the plain sampler.
fn sampler(name: Option<&str>, arg: Option<&str>) -> (Sampler, Option<String>) {
    let name = name.unwrap_or("always_on");
    match name.strip_prefix("parentbased_").unwrap_or(name) {
        "always_on" => (Sampler::AlwaysOn, None),
        "always_off" => (Sampler::AlwaysOff, None),
        "traceidratio" => match arg.map(str::parse::<f64>) {
            None => (Sampler::AlwaysOn, None),
            Some(Ok(ratio)) if (0.0..=1.0).contains(&ratio) => {
                (Sampler::TraceIdRatioBased(ratio), None)
            }
            Some(_) => (
                Sampler::AlwaysOn,
                Some("OTEL_TRACES_SAMPLER_ARG is not a ratio in 0..=1; sampling everything".into()),
            ),
        },
        other => (
            Sampler::AlwaysOn,
            Some(format!(
                "unsupported OTEL_TRACES_SAMPLER {other:?}; sampling everything"
            )),
        ),
    }
}

pub fn init() -> anyhow::Result<Option<SdkTracerProvider>> {
    let endpoint = std::env::var("OTEL_EXPORTER_OTLP_ENDPOINT")
        .ok()
        .filter(|v| !v.is_empty());
    let (sampler, sampler_warning) = sampler(
        std::env::var("OTEL_TRACES_SAMPLER").ok().as_deref(),
        std::env::var("OTEL_TRACES_SAMPLER_ARG").ok().as_deref(),
    );
    let provider = endpoint
        .map(|_| -> anyhow::Result<_> {
            // The exporter applies OTLP environment endpoint semantics, including
            // appending /v1/traces to the generic OTEL_EXPORTER_OTLP_ENDPOINT.
            let exporter = opentelemetry_otlp::SpanExporter::builder()
                .with_http()
                .build()?;
            Ok(SdkTracerProvider::builder()
                .with_batch_exporter(exporter)
                .with_sampler(sampler)
                .with_resource(
                    Resource::builder()
                        .with_service_name("lumewarp-gateway")
                        .build(),
                )
                .build())
        })
        .transpose()?;
    let otel = provider.as_ref().map(|provider| {
        tracing_opentelemetry::layer()
            .with_location(false)
            .with_tracer(provider.tracer("lumewarp-gateway"))
            .with_filter(otel_targets())
    });
    tracing_subscriber::registry()
        .with(
            tracing_subscriber::fmt::layer()
                .with_filter(EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into())),
        )
        .with(otel)
        .try_init()?;
    if let (Some(warning), true) = (sampler_warning, provider.is_some()) {
        tracing::warn!("{warning}");
    }
    Ok(provider)
}

fn otel_targets() -> Targets {
    Targets::new().with_target("lumewarp", LevelFilter::INFO)
}

pub fn request_span(enabled: bool, headers: &HeaderMap, endpoint: &'static str) -> Span {
    if !enabled {
        return Span::none();
    }
    let empty = tracing::field::Empty;
    let span = tracing::info_span!(
        "request",
        endpoint,
        request_id = empty,
        team_id = empty,
        key_id = empty,
        requested = empty,
        alias = empty,
        router = empty,
        model = empty,
        upstream = empty,
        upstream_model = empty,
        attempts = empty,
        fallback = empty,
        config_version = empty,
        status = empty,
        stream = false
    );
    let context = TraceContextPropagator::new().extract(&Headers(headers));
    let _ = span.set_parent(context);
    span
}

pub fn inject(headers: &mut HeaderMap) {
    headers.remove("traceparent");
    headers.remove("tracestate");
    TraceContextPropagator::new()
        .inject_context(&Span::current().context(), &mut HeadersMut(headers));
}

struct Headers<'a>(&'a HeaderMap);
impl Extractor for Headers<'_> {
    fn get(&self, key: &str) -> Option<&str> {
        self.0.get(key).and_then(|v| v.to_str().ok())
    }
    fn keys(&self) -> Vec<&str> {
        self.0.keys().map(|key| key.as_str()).collect()
    }
}
struct HeadersMut<'a>(&'a mut HeaderMap);
impl Injector for HeadersMut<'_> {
    fn set(&mut self, key: &str, value: String) {
        if let (Ok(name), Ok(value)) = (
            HeaderName::from_bytes(key.as_bytes()),
            HeaderValue::from_str(&value),
        ) {
            self.0.insert(name, value);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn otel_does_not_enable_dependency_debug_or_trace() {
        let provider = SdkTracerProvider::builder().build();
        let subscriber = tracing_subscriber::registry()
            .with(
                tracing_subscriber::fmt::layer()
                    .with_writer(std::io::sink)
                    .with_filter(EnvFilter::new("info")),
            )
            .with(
                tracing_opentelemetry::layer()
                    .with_location(false)
                    .with_tracer(provider.tracer("test"))
                    .with_filter(otel_targets()),
            );
        tracing::subscriber::with_default(subscriber, || {
            assert_eq!(LevelFilter::current(), LevelFilter::INFO);
            assert!(!tracing::enabled!(target: "hyper_util::client", tracing::Level::TRACE));
            assert!(!tracing::enabled!(target: "hyper::proto", tracing::Level::DEBUG));
            assert!(!tracing::enabled!(target: "lumewarp_gateway::proxy", tracing::Level::DEBUG));
            assert!(tracing::enabled!(target: "lumewarp_gateway::server", tracing::Level::INFO));
        });
    }

    const UNSAMPLED: &str = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-00";

    fn started_sampled(provider: &SdkTracerProvider) -> (bool, opentelemetry::trace::TraceId) {
        use opentelemetry::trace::{Span as _, TraceContextExt, Tracer};
        let headers = HeaderMap::from_iter([(
            HeaderName::from_static("traceparent"),
            HeaderValue::from_static(UNSAMPLED),
        )]);
        let parent = TraceContextPropagator::new().extract(&Headers(&headers));
        assert!(parent.span().span_context().is_remote());
        assert!(!parent.span().span_context().is_sampled());
        let span = provider
            .tracer("test")
            .start_with_context("request", &parent);
        let context = span.span_context().clone();
        (context.is_sampled(), context.trace_id())
    }

    /// The reason the gateway sets its own sampler. If an SDK upgrade changes
    /// this default, the docs in docs/config-protocol.md change with it.
    #[test]
    fn default_sdk_sampler_lets_a_client_suppress_its_record() {
        let default = SdkTracerProvider::builder().build();
        assert!(!started_sampled(&default).0);
    }

    #[test]
    fn gateway_sampler_ignores_the_parent_decision_and_keeps_its_trace_id() {
        for (name, arg) in [
            (None, None),
            (Some("always_on"), None),
            (Some("parentbased_always_on"), None),
            (Some("traceidratio"), Some("1.0")),
            (Some("parentbased_traceidratio"), Some("1.0")),
        ] {
            let (sampler, warning) = sampler(name, arg);
            assert!(warning.is_none());
            let provider = SdkTracerProvider::builder().with_sampler(sampler).build();
            let (sampled, trace_id) = started_sampled(&provider);
            assert!(sampled, "{name:?}");
            assert_eq!(trace_id.to_string(), "0af7651916cd43dd8448eb211c80319c");
        }
    }

    #[test]
    fn sampler_names_and_bad_input_keep_the_record() {
        for (name, arg, warns) in [
            (Some("always_off"), None, false),
            (Some("parentbased_always_off"), None, false),
            (Some("traceidratio"), Some("0"), false),
            (Some("traceidratio"), Some("not-a-ratio"), true),
            (Some("traceidratio"), Some("1.5"), true),
            (Some("jaeger_remote"), None, true),
        ] {
            let (chosen, warning) = sampler(name, arg);
            assert_eq!(warning.is_some(), warns, "{name:?} {arg:?}");
            let provider = SdkTracerProvider::builder().with_sampler(chosen).build();
            assert_eq!(started_sampled(&provider).0, warns, "{name:?} {arg:?}");
        }
    }

    #[test]
    fn a_disabled_request_span_is_none() {
        assert!(request_span(false, &HeaderMap::new(), "chat_completions").is_none());
    }
}
