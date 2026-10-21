use hyper::{
    header::{HeaderName, HeaderValue},
    HeaderMap,
};
use opentelemetry::{
    propagation::{Extractor, Injector, TextMapPropagator},
    trace::TracerProvider,
};
use opentelemetry_sdk::{propagation::TraceContextPropagator, trace::SdkTracerProvider, Resource};
use tracing::Span;
use tracing_opentelemetry::OpenTelemetrySpanExt;
use tracing_subscriber::{
    filter::{LevelFilter, Targets},
    layer::SubscriberExt,
    util::SubscriberInitExt,
    EnvFilter, Layer,
};

pub fn init() -> anyhow::Result<Option<SdkTracerProvider>> {
    let endpoint = std::env::var("OTEL_EXPORTER_OTLP_ENDPOINT")
        .ok()
        .filter(|v| !v.is_empty());
    let provider = endpoint
        .map(|_| -> anyhow::Result<_> {
            // The exporter applies OTLP environment endpoint semantics, including
            // appending /v1/traces to the generic OTEL_EXPORTER_OTLP_ENDPOINT.
            let exporter = opentelemetry_otlp::SpanExporter::builder()
                .with_http()
                .build()?;
            Ok(SdkTracerProvider::builder()
                .with_batch_exporter(exporter)
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
    Ok(provider)
}

fn otel_targets() -> Targets {
    Targets::new().with_target("lumewarp", LevelFilter::INFO)
}

pub fn request_span(enabled: bool, headers: &HeaderMap, route: &'static str) -> Span {
    let span = if enabled {
        tracing::info_span!(
            "request",
            route,
            model = "",
            deployment = "",
            status = tracing::field::Empty,
            stream = false
        )
    } else {
        tracing::debug_span!(
            "request",
            route,
            model = "",
            deployment = "",
            status = tracing::field::Empty,
            stream = false
        )
    };
    if enabled {
        let context = TraceContextPropagator::new().extract(&Headers(headers));
        let _ = span.set_parent(context);
    }
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
}
