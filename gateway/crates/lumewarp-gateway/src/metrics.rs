//! Duration covers handler entry through response-body completion or drop.
//! `lumewarp_request_body_read_seconds{endpoint}` measures only the client body
//! collection interval, including failed, timed-out and cancelled reads.
//! Gateway overhead starts after collection and excludes all upstream waits;
//! see `overhead_seconds` for the exact stream and non-stream approximation.

use std::{
    collections::HashMap,
    pin::Pin,
    sync::{atomic::AtomicU64, Arc, Mutex, OnceLock},
    task::{Context, Poll},
    time::{Duration, Instant},
};

use bytes::Bytes;
use http_body_util::BodyExt;
use hyper::{
    body::{Body, Frame, SizeHint},
    StatusCode,
};
use lumewarp_proto::{config::v1::NackCode, usage::v1::UsageState};
use prometheus_client::{
    encoding::{text::encode, EncodeLabelSet},
    metrics::{counter::Counter, family::Family, gauge::Gauge, histogram::Histogram},
    registry::Registry,
};
use tracing::Span;

use crate::{
    proxy::{BoxError, ProxyBody},
    router::Model,
    runtime::InFlightRegistry,
};

pub const API_ENDPOINTS: [&str; 7] = [
    "chat_completions",
    "completions",
    "embeddings",
    "models",
    "healthz",
    "metrics",
    "other",
];
const STATUSES: [&str; 15] = [
    "200", "400", "401", "404", "408", "413", "429", "500", "502", "503", "504", "other", "204",
    "499", "403",
];
const NACK_CODES: [NackCode; 5] = [
    NackCode::Unspecified,
    NackCode::BaseMismatch,
    NackCode::InvalidConfig,
    NackCode::UnsupportedFeature,
    NackCode::PersistFailed,
];

fn nack_label(code: NackCode) -> &'static str {
    match code {
        NackCode::Unspecified => "unspecified",
        NackCode::BaseMismatch => "base_mismatch",
        NackCode::InvalidConfig => "invalid_config",
        NackCode::UnsupportedFeature => "unsupported_feature",
        NackCode::PersistFailed => "persist_failed",
    }
}

/// `lumewarp_usage_capture_total{state}` labels, indexed by `UsageState`.
const USAGE_STATES: [&str; 5] = ["unspecified", "reported", "missing", "partial", "invalid"];
const ERROR_KINDS: [&str; 4] = ["connect", "timeout", "stream", "protocol"];
pub const ATTEMPT_OUTCOMES: [&str; 6] = [
    "success",
    "retryable_status",
    "connect_error",
    "timeout",
    "circuit_open",
    "non_retryable",
];

/// `lumewarp_mcp_requests_total{method}`: what the gateway forwards, plus
/// `other` for a method it does not name (a notification is a request too).
#[allow(dead_code)] // wired by the MCP/A2A proxy (W2)
pub const MCP_METHODS: [&str; 8] = [
    "initialize",
    "tools_list",
    "tools_call",
    "prompts_list",
    "prompts_get",
    "ping",
    "notification",
    "other",
];

/// `lumewarp_a2a_requests_total{method}`: the A2A v1.0 JSON-RPC methods the
/// gateway forwards (push notification methods are refused, plan D12), plus
/// `agent_card` for the card endpoint and `other`.
#[allow(dead_code)] // wired by the MCP/A2A proxy (W2)
pub const A2A_METHODS: [&str; 7] = [
    "message_send",
    "message_stream",
    "tasks_get",
    "tasks_cancel",
    "tasks_resubscribe",
    "agent_card",
    "other",
];

/// The `tool` label of a tool call whose definition the gateway did not
/// approve. An upstream must not be able to create label series, so this is the
/// only alternative to an approved name (`tool_label`).
#[allow(dead_code)] // wired by the MCP/A2A proxy (W2)
pub const UNAPPROVED_TOOL: &str = "_unapproved";

/// Longest tool name used as a `tool` label (MCP says names SHOULD be 1..=128
/// characters; `targets` refuses to approve anything longer than this).
#[allow(dead_code)] // wired by the MCP/A2A proxy (W2)
pub const MAX_TOOL_LABEL: usize = 256;

/// `lumewarp_plugin_calls_total{kind}`; `other` for a kind this gateway does
/// not know keeps the label set bounded.
#[allow(dead_code)] // wired by the MCP/A2A proxy (W2)
pub const PLUGIN_KINDS: [&str; 4] = [
    "router-strategy",
    "upstream-auth",
    "a2a-middleware",
    "other",
];

/// `lumewarp_plugin_calls_total{result}`.
#[allow(dead_code)] // wired by the MCP/A2A proxy (W2)
pub const PLUGIN_RESULTS: [&str; 4] = ["ok", "error", "timeout", "trap"];

/// `lumewarp_mcp_catalog_reports_dropped_total{reason}`.
#[allow(dead_code)] // wired by the MCP/A2A proxy (W2)
pub const CATALOG_DROP_REASONS: [&str; 3] = ["oversized", "pending_limit", "invalid"];

/// The `status` label for an HTTP status: a [`STATUSES`] entry, or `other`.
#[allow(dead_code)] // wired by the MCP/A2A proxy (W2)
pub fn status_label(status: u16) -> &'static str {
    match status {
        200 => "200",
        204 => "204",
        400 => "400",
        401 => "401",
        403 => "403",
        404 => "404",
        408 => "408",
        413 => "413",
        429 => "429",
        499 => "499",
        500 => "500",
        502 => "502",
        503 => "503",
        504 => "504",
        _ => "other",
    }
}

/// Position of a status in [`STATUSES`]: the index into the arrays of
/// per-status handles below.
#[allow(dead_code)] // wired by the MCP/A2A proxy (W2)
pub fn status_index(status: u16) -> usize {
    STATUSES
        .iter()
        .position(|known| *known == status_label(status))
        .unwrap_or(11)
}

/// The `tool` label of `lumewarp_mcp_tool_calls_total`: the upstream name only
/// when its definition was approved, [`UNAPPROVED_TOOL`] otherwise (and for a
/// name too long to be approved). Build the label only through this function.
#[allow(dead_code)] // wired by the MCP/A2A proxy (W2)
pub fn tool_label(approved: bool, tool: &str) -> &str {
    if approved && !tool.is_empty() && tool.len() <= MAX_TOOL_LABEL {
        tool
    } else {
        UNAPPROVED_TOOL
    }
}

/// A bounded `kind` label value for the three plugin kinds.
#[allow(dead_code)] // wired by the MCP/A2A proxy (W2)
pub fn plugin_kind_label(kind: &str) -> &'static str {
    match kind {
        "router-strategy" => "router-strategy",
        "upstream-auth" => "upstream-auth",
        "a2a-middleware" => "a2a-middleware",
        _ => "other",
    }
}

/// Index of `result` in [`PLUGIN_RESULTS`]; unknown results are errors.
#[allow(dead_code)] // wired by the MCP/A2A proxy (W2)
pub fn plugin_result_index(result: &str) -> Option<usize> {
    PLUGIN_RESULTS.iter().position(|known| *known == result)
}

#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct AttemptLabels {
    requested: String,
    model: String,
    outcome: &'static str,
}
#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct FallbackLabels {
    router: String,
    model: String,
}
#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct RouterLabels {
    router: String,
}
#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct SplitLabels {
    router: String,
    split: String,
}
#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct UpstreamLabels {
    upstream: String,
}
#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct McpRequestLabels {
    server: String,
    method: &'static str,
    status: &'static str,
}
#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct McpToolLabels {
    server: String,
    tool: String,
    team: String,
    status: &'static str,
}
#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct A2aRequestLabels {
    agent: String,
    method: &'static str,
    status: &'static str,
}
#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct ServerLabels {
    server: String,
}
#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct AgentLabels {
    agent: String,
}
#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct CatalogDropLabels {
    reason: &'static str,
}
#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct PluginLabels {
    kind: &'static str,
    plugin: String,
}
#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct PluginResultLabels {
    kind: &'static str,
    plugin: String,
    result: &'static str,
}

/// `lumewarp_plugin_calls_total` and `lumewarp_plugin_duration_seconds` for one
/// (kind, plugin): fetch once when the plugin is loaded, record per call.
#[derive(Clone)]
#[allow(dead_code)] // wired by the MCP/A2A proxy (W2)
pub struct PluginHandles {
    /// One counter per [`PLUGIN_RESULTS`] entry.
    pub calls: [Counter; PLUGIN_RESULTS.len()],
    pub duration: Histogram,
}

#[allow(dead_code)] // wired by the MCP/A2A proxy (W2)
impl PluginHandles {
    /// Records one call: `result` is one of [`PLUGIN_RESULTS`]; anything else is
    /// counted as an error.
    pub fn record(&self, result: &str, elapsed: Duration) {
        let index = plugin_result_index(result).unwrap_or(1);
        self.calls[index].inc();
        self.duration.observe(elapsed.as_secs_f64());
    }
}

pub enum UpstreamError {
    Connect,
    Timeout,
    Stream,
    Protocol,
}

pub fn endpoint_index(path: &str) -> usize {
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
    requested: String,
    endpoint: &'static str,
    model: String,
}
#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct RequestLabels {
    requested: String,
    endpoint: &'static str,
    model: String,
    team: String,
    status: &'static str,
}
#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct EndpointLabels {
    endpoint: &'static str,
}
#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct ModelLabels {
    model: String,
}
#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct PlaygroundLabels {
    result: &'static str,
}
#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct RejectionLabels {
    model: String,
    reason: &'static str,
}
#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct TtftLabels {
    requested: String,
    model: String,
}
#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct ErrorLabels {
    model: String,
    kind: &'static str,
}
#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct ConfigLabels {
    source: &'static str,
    sha256: String,
}
#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct NackLabels {
    code: &'static str,
}
#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct TokenLabels {
    requested: String,
    team: String,
    direction: &'static str,
}
#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct SpendLabels {
    team: String,
}
#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct UsageStateLabels {
    state: &'static str,
}
type SpendCounter = Counter<f64, AtomicU64>;

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
    pub requested: String,
    pub model: String,
    team: String,
    /// (input, output) `lumewarp_tokens_total` series, created on first use
    /// so routers nobody meters do not export zero series.
    tokens: OnceLock<[Counter; 2]>,
    token_family: Family<TokenLabels, Counter>,
    counters: [Counter; 15],
    duration: Histogram,
    overhead: Histogram,
    body_read: Histogram,
    ttft: Option<Histogram>,
    errors: Option<[Counter; 4]>,
    pub attempts: [Counter; 6],
}

impl RequestMetrics {
    pub fn upstream_error(&self, kind: UpstreamError) {
        if let Some(errors) = &self.errors {
            errors[kind as usize].inc();
        }
    }

    /// Adds upstream-reported tokens for this handle's requested name and team.
    /// Allocation-free after the first call per label set.
    pub fn record_tokens(&self, input: u64, output: u64) {
        let [input_counter, output_counter] = self.tokens.get_or_init(|| {
            ["input", "output"].map(|direction| {
                self.token_family
                    .get_or_create(&TokenLabels {
                        requested: self.requested.clone(),
                        team: self.team.clone(),
                        direction,
                    })
                    .clone()
            })
        });
        input_counter.inc_by(input);
        output_counter.inc_by(output);
    }
}

/// Process-lifetime id of a label value (team id, alias or model name).
/// Equal strings get equal ids in every snapshot; `""` is always 0.
pub type LabelId = u32;

/// Sparse handle-cache key: only label sets that were actually used exist.
#[derive(Clone, Copy, Debug, Hash, PartialEq, Eq)]
pub struct HandleKey {
    pub team: LabelId,
    pub requested: LabelId,
    pub model: LabelId,
    /// Index into `API_ENDPOINTS`.
    pub endpoint: u8,
}

/// Process-lifetime registry owned by `Runtime`; snapshot swaps never reset it.
pub struct Metrics {
    registry: Registry,
    label_ids: Mutex<HashMap<Box<str>, LabelId>>,
    /// Lock-free reads; an entry is created on the first request with its
    /// (team, requested, model, endpoint) and kept across snapshot swaps.
    handle_cache: papaya::HashMap<HandleKey, Arc<RequestMetrics>>,
    requests: Family<RequestLabels, Counter>,
    durations: Histograms<Labels>,
    overhead: Histograms<EndpointLabels>,
    body_read: Histograms<EndpointLabels>,
    ttft: Histograms<TtftLabels>,
    errors: Family<ErrorLabels, Counter>,
    config_info: Family<ConfigLabels, Gauge>,
    gauges: Family<ModelLabels, Gauge>,
    config_version: Gauge,
    // Written only through the methods the config stream calls;
    // registered here so their series exist from boot.
    config_apply_failures: Family<NackLabels, Counter>,
    config_stream_connected: Gauge,
    usage_dropped: Counter,
    usage_reported: Counter,
    usage_failed: Counter,
    usage_queue_depth: Gauge,
    usage_capture: [Counter; USAGE_STATES.len()],
    tokens: Family<TokenLabels, Counter>,
    spend: Family<SpendLabels, SpendCounter>,
    /// team -> spend series, so recording spend is lock- and allocation-free
    /// after the first request of a team.
    spend_cache: papaya::HashMap<Box<str>, SpendCounter>,
    attempts: Family<AttemptLabels, Counter>,
    fallbacks: Family<FallbackLabels, Counter>,
    budget_exhausted: Family<RouterLabels, Counter>,
    circuit: Family<ModelLabels, Gauge>,
    healthy: Family<UpstreamLabels, Gauge>,
    model_healthy: Family<ModelLabels, Gauge>,
    splits: Family<SplitLabels, Counter>,
    vllm_queue: Family<ModelLabels, Gauge>,
    vllm_kv: Family<ModelLabels, Gauge<f64, AtomicU64>>,
    vllm_scrape_failures: Family<UpstreamLabels, Counter>,
    latency_ewma: Family<ModelLabels, Gauge<f64, AtomicU64>>,
    rewrite_compressed: Family<ModelLabels, Counter>,
    rejected: Family<RejectionLabels, Counter>,
    pub playground: [Counter; 2],
    mcp_requests: Family<McpRequestLabels, Counter>,
    mcp_tool_calls: Family<McpToolLabels, Counter>,
    mcp_durations: Family<ServerLabels, Histogram>,
    a2a_requests: Family<A2aRequestLabels, Counter>,
    a2a_durations: Family<AgentLabels, Histogram>,
    catalog_drops: Family<CatalogDropLabels, Counter>,
    plugin_calls: Family<PluginResultLabels, Counter>,
    plugin_durations: Family<PluginLabels, Histogram>,
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
        let config_version = Gauge::default();
        let config_apply_failures = Family::default();
        let config_stream_connected = Gauge::default();
        let usage_dropped = Counter::default();
        let usage_reported = Counter::default();
        let usage_failed = Counter::default();
        let usage_queue_depth = Gauge::default();
        let usage_capture_family = Family::<UsageStateLabels, Counter>::default();
        let usage_capture = USAGE_STATES.map(|state| {
            usage_capture_family
                .get_or_create(&UsageStateLabels { state })
                .clone()
        });
        let tokens = Family::default();
        let spend = Family::default();
        let attempts = Family::default();
        let fallbacks = Family::default();
        let budget_exhausted = Family::default();
        let circuit = Family::default();
        let healthy = Family::default();
        let model_healthy = Family::default();
        let splits = Family::default();
        let vllm_queue = Family::default();
        let vllm_kv = Family::default();
        let vllm_scrape_failures = Family::default();
        let latency_ewma = Family::default();
        let rewrite_compressed = Family::default();
        let playground_family = Family::<PlaygroundLabels, Counter>::default();
        let playground = ["ok", "rejected"].map(|result| {
            playground_family
                .get_or_create(&PlaygroundLabels { result })
                .clone()
        });
        registry.register(
            "lumewarp_playground_requests",
            "Playground token authentication attempts by result",
            playground_family,
        );
        let rejected = Family::default();
        registry.register(
            "lumewarp_requests_rejected",
            "Requests answered 400 by a model's body rules before reaching the upstream (reason: max_output_tokens, reasoning_effort, invalid_body)",
            rejected.clone(),
        );
        registry.register(
            "lumewarp_response_model_rewrite_compressed",
            "Successful responses that kept the upstream's model name because the upstream compressed them",
            rewrite_compressed.clone(),
        );
        registry.register(
            "lumewarp_split_requests",
            "Requests assigned to a traffic split",
            splits.clone(),
        );
        registry.register(
            "lumewarp_vllm_queue_depth",
            "Last scraped vllm:num_requests_waiting",
            vllm_queue.clone(),
        );
        registry.register(
            "lumewarp_vllm_kv_cache_usage",
            "Last scraped vLLM KV-cache usage (0..1)",
            vllm_kv.clone(),
        );
        registry.register(
            "lumewarp_vllm_scrape_failures",
            "Failed vLLM metrics scrapes",
            vllm_scrape_failures.clone(),
        );
        registry.register(
            "lumewarp_upstream_latency_ewma_seconds",
            "Time-to-response-headers EWMA used by lowest_latency routing",
            latency_ewma.clone(),
        );
        registry.register(
            "lumewarp_upstream_attempts",
            "Upstream attempts by outcome",
            attempts.clone(),
        );
        registry.register(
            "lumewarp_fallbacks",
            "Requests moving to a fallback model",
            fallbacks.clone(),
        );
        registry.register(
            "lumewarp_retries_budget_exhausted",
            "Retries refused by the process budget",
            budget_exhausted.clone(),
        );
        registry.register(
            "lumewarp_upstream_circuit_state",
            "Model breaker: closed 0, half-open 1, open 2",
            circuit.clone(),
        );
        registry.register(
            "lumewarp_upstream_healthy",
            "Active upstream health: healthy 1, unhealthy 0",
            healthy.clone(),
        );
        registry.register(
            "lumewarp_model_healthy",
            "Model chat check: healthy 1, unhealthy 0 (1 while a model has no chat_check)",
            model_healthy.clone(),
        );
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
            "Requests holding a model reservation",
            gauges.clone(),
        );
        registry.register(
            "lumewarp_config_version",
            "Active config snapshot version (standalone YAML is 0)",
            config_version.clone(),
        );
        registry.register(
            "lumewarp_config_info",
            "Config source and first twelve hex SHA-256 characters",
            config_info.clone(),
        );
        registry.register(
            "lumewarp_config_apply_failures",
            "Config updates rejected or not persisted, by NACK code",
            config_apply_failures.clone(),
        );
        for code in NACK_CODES {
            drop(config_apply_failures.get_or_create(&NackLabels {
                code: nack_label(code),
            }));
        }
        registry.register(
            "lumewarp_config_stream_connected",
            "1 while the control-plane config stream is connected",
            config_stream_connected.clone(),
        );
        registry.register(
            "lumewarp_usage_events_dropped",
            "Usage events dropped because the report queue was full",
            usage_dropped.clone(),
        );
        registry.register(
            "lumewarp_usage_events_reported",
            "Usage events accepted by the control plane",
            usage_reported.clone(),
        );
        registry.register(
            "lumewarp_usage_events_failed",
            "Usage events in report attempts that failed (retried from the queue)",
            usage_failed.clone(),
        );
        registry.register(
            "lumewarp_usage_queue_depth",
            "Usage events waiting in the report queue",
            usage_queue_depth.clone(),
        );
        registry.register(
            "lumewarp_usage_capture",
            "Finalized requests by usage state (missing/partial/invalid = metering gaps)",
            usage_capture_family,
        );
        let mcp_requests = Family::default();
        registry.register(
            "lumewarp_mcp_requests",
            "MCP requests by server, method and HTTP status",
            mcp_requests.clone(),
        );
        let mcp_tool_calls = Family::default();
        registry.register(
            "lumewarp_mcp_tool_calls",
            "MCP tool calls by server, tool, team and status (tool is an approved name or _unapproved)",
            mcp_tool_calls.clone(),
        );
        let mcp_durations = Family::new_with_constructor(duration_histogram as fn() -> Histogram);
        registry.register(
            "lumewarp_mcp_upstream_duration_seconds",
            "Time to the upstream response headers of an MCP request",
            mcp_durations.clone(),
        );
        let a2a_requests = Family::default();
        registry.register(
            "lumewarp_a2a_requests",
            "A2A requests by agent, method and status",
            a2a_requests.clone(),
        );
        let a2a_durations = Family::new_with_constructor(duration_histogram as fn() -> Histogram);
        registry.register(
            "lumewarp_a2a_upstream_duration_seconds",
            "Time to the upstream response headers of an A2A request",
            a2a_durations.clone(),
        );
        let catalog_drops = Family::default();
        for reason in CATALOG_DROP_REASONS {
            drop(catalog_drops.get_or_create(&CatalogDropLabels { reason }));
        }
        registry.register(
            "lumewarp_mcp_catalog_reports_dropped",
            "MCP catalog observations dropped before a report (reason: oversized, pending_limit, invalid)",
            catalog_drops.clone(),
        );
        let plugin_calls = Family::default();
        registry.register(
            "lumewarp_plugin_calls",
            "Plugin calls by kind, plugin and result (ok, error, timeout, trap)",
            plugin_calls.clone(),
        );
        let plugin_durations =
            Family::new_with_constructor(duration_histogram as fn() -> Histogram);
        registry.register(
            "lumewarp_plugin_duration_seconds",
            "Plugin call duration by kind and plugin",
            plugin_durations.clone(),
        );
        registry.register(
            "lumewarp_tokens",
            "Upstream-reported tokens by requested name, team and direction",
            tokens.clone(),
        );
        registry.register(
            "lumewarp_spend",
            "Spend in USD computed from model prices",
            spend.clone(),
        );
        Self {
            registry,
            label_ids: Mutex::new(HashMap::from([("".into(), 0)])),
            handle_cache: papaya::HashMap::new(),
            requests,
            durations,
            overhead,
            body_read,
            ttft,
            errors,
            config_info,
            gauges,
            config_version,
            config_apply_failures,
            config_stream_connected,
            usage_dropped,
            usage_reported,
            usage_failed,
            usage_queue_depth,
            usage_capture,
            tokens,
            spend,
            spend_cache: papaya::HashMap::new(),
            attempts,
            fallbacks,
            budget_exhausted,
            circuit,
            healthy,
            model_healthy,
            splits,
            vllm_queue,
            vllm_kv,
            vllm_scrape_failures,
            latency_ewma,
            rewrite_compressed,
            rejected,
            playground,
            mcp_requests,
            mcp_tool_calls,
            mcp_durations,
            a2a_requests,
            a2a_durations,
            catalog_drops,
            plugin_calls,
            plugin_durations,
        }
    }

    pub fn request_rejected(&self, model: &str, reason: &'static str) {
        self.rejected
            .get_or_create(&RejectionLabels {
                model: model.into(),
                reason,
            })
            .inc();
    }

    /// Only for the rare upstream that compresses despite no `Accept-Encoding`.
    pub fn response_rewrite_compressed(&self, model: &str) {
        self.rewrite_compressed
            .get_or_create(&ModelLabels {
                model: model.into(),
            })
            .inc();
    }

    pub fn split_handle(&self, router: &str, split: &str) -> Counter {
        self.splits
            .get_or_create(&SplitLabels {
                router: router.into(),
                split: split.into(),
            })
            .clone()
    }

    pub fn vllm_gauges(&self, model: &str) -> (Gauge, Gauge<f64, AtomicU64>) {
        let labels = ModelLabels {
            model: model.into(),
        };
        (
            self.vllm_queue.get_or_create(&labels).clone(),
            self.vllm_kv.get_or_create(&labels).clone(),
        )
    }

    pub fn scrape_failures(&self, upstream: &str) -> Counter {
        self.vllm_scrape_failures
            .get_or_create(&UpstreamLabels {
                upstream: upstream.into(),
            })
            .clone()
    }

    /// Off the request path: refreshed when `/metrics` is scraped.
    pub fn export_latency(&self, latencies: Vec<(String, u64)>) {
        for (model, us) in latencies {
            self.latency_ewma
                .get_or_create(&ModelLabels { model })
                .set(us as f64 / 1e6);
        }
    }

    pub fn config_applied(&self, version: u64) {
        self.config_version
            .set(i64::try_from(version).unwrap_or(i64::MAX));
    }

    pub fn attempt_handles(&self, requested: &str, model: &str) -> [Counter; 6] {
        ATTEMPT_OUTCOMES.map(|outcome| {
            self.attempts
                .get_or_create(&AttemptLabels {
                    requested: requested.into(),
                    model: model.into(),
                    outcome,
                })
                .clone()
        })
    }

    pub fn fallback_handle(&self, router: &str, model: &str) -> Counter {
        self.fallbacks
            .get_or_create(&FallbackLabels {
                router: router.into(),
                model: model.into(),
            })
            .clone()
    }

    pub fn budget_handle(&self, router: &str) -> Counter {
        self.budget_exhausted
            .get_or_create(&RouterLabels {
                router: router.into(),
            })
            .clone()
    }

    pub fn circuit_gauge(&self, model: &str) -> Gauge {
        self.circuit
            .get_or_create(&ModelLabels {
                model: model.into(),
            })
            .clone()
    }

    pub fn health_gauge(&self, upstream: &str) -> Gauge {
        self.healthy
            .get_or_create(&UpstreamLabels {
                upstream: upstream.into(),
            })
            .clone()
    }

    pub fn model_health_gauge(&self, model: &str) -> Gauge {
        self.model_healthy
            .get_or_create(&ModelLabels {
                model: model.into(),
            })
            .clone()
    }

    pub fn config_apply_failed(&self, code: NackCode) {
        self.config_apply_failures
            .get_or_create(&NackLabels {
                code: nack_label(code),
            })
            .inc();
    }

    pub fn config_stream_connected(&self, connected: bool) {
        self.config_stream_connected.set(i64::from(connected));
    }

    pub fn usage_dropped(&self) {
        self.usage_dropped.inc();
    }

    pub fn usage_dropped_n(&self, n: u64) {
        self.usage_dropped.inc_by(n);
    }

    /// `sent` events were accepted; `failed` events were in a failed attempt.
    pub fn usage_batch(&self, sent: u64, failed: u64) {
        self.usage_reported.inc_by(sent);
        self.usage_failed.inc_by(failed);
    }

    pub fn usage_queue_depth(&self, depth: usize) {
        self.usage_queue_depth
            .set(i64::try_from(depth).unwrap_or(i64::MAX));
    }

    /// One finalized request (or reported rejection) in `state`.
    pub fn usage_captured(&self, state: UsageState) {
        if let Some(counter) = self.usage_capture.get(state as usize) {
            counter.inc();
        }
    }

    /// `team` is an id (`""` anonymous). Allocates only the first time a team
    /// is seen.
    pub fn spend(&self, team: &str, nano_usd: u64) {
        let teams = self.spend_cache.pin();
        let counter = match teams.get(team) {
            Some(counter) => counter,
            None => teams.get_or_insert_with(team.into(), || {
                self.spend
                    .get_or_create(&SpendLabels { team: team.into() })
                    .clone()
            }),
        };
        counter.inc_by(nano_usd as f64 / 1e9);
    }

    pub fn set_yaml_hash(&self, hash: &str) {
        self.config_info
            .get_or_create(&ConfigLabels {
                source: "yaml",
                sha256: hash.into(),
            })
            .set(1);
    }

    /// Pre-registers zero-valued per-model series.
    pub fn register_model(&self, model: &str) {
        drop(self.gauges.get_or_create(&ModelLabels {
            model: model.into(),
        }));
        for kind in ERROR_KINDS {
            drop(self.errors.get_or_create(&ErrorLabels {
                model: model.into(),
                kind,
            }));
        }
    }

    /// Interns a label value. Compile time only (takes a lock).
    pub fn label_id(&self, value: &str) -> LabelId {
        let mut ids = self.label_ids.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(&id) = ids.get(value) {
            return id;
        }
        let id = LabelId::try_from(ids.len()).expect("fewer than 2^32 label values");
        ids.insert(value.into(), id);
        id
    }

    /// Request-path lookup: lock-free and allocation-free once `key` was used
    /// in this process; the first use creates the series. `team`, `requested` and
    /// `model` must be the strings `key` was interned from.
    pub fn cached_handles(
        &self,
        key: HandleKey,
        team: &str,
        requested: &str,
        model: &str,
    ) -> Arc<RequestMetrics> {
        let cache = self.handle_cache.pin();
        if let Some(handles) = cache.get(&key) {
            return handles.clone();
        }
        cache
            .get_or_insert_with(key, || {
                self.handles(requested, model, key.endpoint.into(), team)
            })
            .clone()
    }

    // Creates (or finds) the registry series for one label set. Cold path:
    // allocates label strings and locks the families.
    pub fn handles(
        &self,
        requested: &str,
        model: &str,
        endpoint: usize,
        team: &str,
    ) -> Arc<RequestMetrics> {
        let counters = std::array::from_fn(|i| {
            self.requests
                .get_or_create(&RequestLabels {
                    requested: requested.into(),
                    endpoint: API_ENDPOINTS[endpoint],
                    model: model.into(),
                    team: team.into(),
                    status: STATUSES[i],
                })
                .clone()
        });
        let duration = self
            .durations
            .get_or_create(&Labels {
                requested: requested.into(),
                endpoint: API_ENDPOINTS[endpoint],
                model: model.into(),
            })
            .clone();
        let overhead = self
            .overhead
            .get_or_create(&EndpointLabels {
                endpoint: API_ENDPOINTS[endpoint],
            })
            .clone();
        let body_read = self
            .body_read
            .get_or_create(&EndpointLabels {
                endpoint: API_ENDPOINTS[endpoint],
            })
            .clone();
        let ttft = (!model.is_empty()).then(|| {
            self.ttft
                .get_or_create(&TtftLabels {
                    requested: requested.into(),
                    model: model.into(),
                })
                .clone()
        });
        let errors = (!model.is_empty()).then(|| {
            ERROR_KINDS.map(|kind| {
                self.errors
                    .get_or_create(&ErrorLabels {
                        model: model.into(),
                        kind,
                    })
                    .clone()
            })
        });
        Arc::new(RequestMetrics {
            requested: requested.into(),
            model: model.into(),
            team: team.into(),
            tokens: OnceLock::new(),
            token_family: self.tokens.clone(),
            counters,
            duration,
            overhead,
            body_read,
            ttft,
            errors,
            attempts: self.attempt_handles(requested, model),
        })
    }

    /// One `lumewarp_mcp_requests_total` counter per [`STATUSES`] entry for
    /// this (server, method). Fetch once while compiling the snapshot, then a
    /// request only indexes the array with [`status_index`].
    #[allow(dead_code)] // wired by the MCP proxy (W2)
    pub fn mcp_request_handles(
        &self,
        server: &str,
        method: &'static str,
    ) -> [Counter; STATUSES.len()] {
        std::array::from_fn(|i| {
            self.mcp_requests
                .get_or_create(&McpRequestLabels {
                    server: server.into(),
                    method,
                    status: STATUSES[i],
                })
                .clone()
        })
    }

    /// One `lumewarp_mcp_tool_calls_total` counter per [`STATUSES`] entry for
    /// (server, tool, team). `approved` is the review decision for the tool:
    /// only an approved name becomes a label, everything else is
    /// [`UNAPPROVED_TOOL`].
    #[allow(dead_code)] // wired by the MCP proxy (W2)
    pub fn mcp_tool_call_handles(
        &self,
        server: &str,
        tool: &str,
        team: &str,
        approved: bool,
    ) -> [Counter; STATUSES.len()] {
        let tool = tool_label(approved, tool);
        std::array::from_fn(|i| {
            self.mcp_tool_calls
                .get_or_create(&McpToolLabels {
                    server: server.into(),
                    tool: tool.into(),
                    team: team.into(),
                    status: STATUSES[i],
                })
                .clone()
        })
    }

    /// `lumewarp_mcp_upstream_duration_seconds{server}`.
    #[allow(dead_code)] // wired by the MCP proxy (W2)
    pub fn mcp_upstream_duration(&self, server: &str) -> Histogram {
        self.mcp_durations
            .get_or_create(&ServerLabels {
                server: server.into(),
            })
            .clone()
    }

    /// One `lumewarp_a2a_requests_total` counter per [`STATUSES`] entry for
    /// this (agent, method).
    #[allow(dead_code)] // wired by the A2A proxy (W2)
    pub fn a2a_request_handles(
        &self,
        agent: &str,
        method: &'static str,
    ) -> [Counter; STATUSES.len()] {
        std::array::from_fn(|i| {
            self.a2a_requests
                .get_or_create(&A2aRequestLabels {
                    agent: agent.into(),
                    method,
                    status: STATUSES[i],
                })
                .clone()
        })
    }

    /// `lumewarp_a2a_upstream_duration_seconds{agent}`.
    #[allow(dead_code)] // wired by the A2A proxy (W2)
    pub fn a2a_upstream_duration(&self, agent: &str) -> Histogram {
        self.a2a_durations
            .get_or_create(&AgentLabels {
                agent: agent.into(),
            })
            .clone()
    }

    /// `lumewarp_mcp_catalog_reports_dropped_total{reason}`.
    #[allow(dead_code)] // wired by the catalog registry (W2)
    pub fn catalog_report_dropped(&self, reason: &'static str) {
        self.catalog_drops
            .get_or_create(&CatalogDropLabels { reason })
            .inc();
    }

    /// `lumewarp_plugin_calls_total` and `lumewarp_plugin_duration_seconds`
    /// for one (kind, plugin). The plugin host fetches these once per loaded
    /// plugin and calls [`PluginHandles::record`] per call.
    #[allow(dead_code)] // wired by the plugin host adapter (T1/W2)
    pub fn plugin_handles(&self, kind: &str, plugin: &str) -> PluginHandles {
        let kind = plugin_kind_label(kind);
        PluginHandles {
            calls: PLUGIN_RESULTS.map(|result| {
                self.plugin_calls
                    .get_or_create(&PluginResultLabels {
                        kind,
                        plugin: plugin.into(),
                        result,
                    })
                    .clone()
            }),
            duration: self
                .plugin_durations
                .get_or_create(&PluginLabels {
                    kind,
                    plugin: plugin.into(),
                })
                .clone(),
        }
    }

    pub fn encode(&self, in_flight: &InFlightRegistry) -> Result<String, std::fmt::Error> {
        for (model, count) in in_flight.counts() {
            self.gauges
                .get_or_create(&ModelLabels {
                    model: model.to_string(),
                })
                .set(count as i64);
        }
        let mut output = String::new();
        encode(&mut output, &self.registry)?;
        Ok(output)
    }
}

#[allow(dead_code)] // wired by the catalog registry (W2)
impl crate::catalog::Drops for Metrics {
    fn catalog_dropped(&self, reason: &'static str) {
        self.catalog_report_dropped(reason);
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
    // What `finish` writes to the span. The SDK appends every record instead of
    // replacing the value, and exports duplicate keys, so a field that changes
    // during a request is held here and recorded once. Only set with OTel on.
    identified: bool,
    routed: bool,
    attempt: Option<Attempt>,
}

/// The upstream attempt the request span reports.
struct Attempt {
    model: Arc<Model>,
    attempts: usize,
    fallback: bool,
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
            identified: false,
            routed: false,
            attempt: None,
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
        self.routed = true;
    }

    /// Identifiers known once the request entered `/v1/*`.
    pub fn identified(&mut self, request_id: &uuid::Uuid, config_version: u64) {
        if self.otel {
            self.identified = true;
            self.span
                .record("request_id", tracing::field::display(request_id));
            self.span.record("config_version", config_version);
        }
    }

    pub fn caller(&self, key_id: &str, team_id: &str) {
        if self.otel {
            self.span.record("key_id", key_id);
            self.span.record("team_id", team_id);
        }
    }

    /// The name the client called, bounded because it is client input.
    pub fn requested(&self, name: &str) {
        if self.otel {
            self.span
                .record("requested", &*crate::usage::bounded_requested(name));
        }
    }

    pub fn provenance(&self, alias: Option<&str>, router: Option<&str>) {
        if self.otel {
            if let Some(alias) = alias {
                self.span.record("alias", alias);
            }
            if let Some(router) = router {
                self.span.record("router", router);
            }
        }
    }

    /// The attempt about to wait on an upstream, so a request cancelled during
    /// that wait still reports it; called again with what finally served.
    pub fn served(&mut self, model: &Arc<Model>, attempts: usize, fallback: bool) {
        if self.otel {
            self.attempt = Some(Attempt {
                model: model.clone(),
                attempts,
                fallback,
            });
        }
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
                // or atomic buckets if 5k/10k RPS p99 shows contention.
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
        // NOTE: Histogram::observe uses shared write locks; profile if p99 shows contention.
        self.handles
            .duration
            .observe(now.saturating_duration_since(self.started).as_secs_f64());
        self.handles.overhead.observe(self.overhead(now));
        if let Some(read) = self.body_read_duration(now) {
            self.handles.body_read.observe(read.as_secs_f64());
        }
        if self.otel {
            self.span.record("status", self.status.as_u16());
            self.span.record("stream", self.stream);
            if self.routed {
                self.span.record("model", &self.handles.model);
            }
            match &self.attempt {
                Some(attempt) => {
                    self.span.record("upstream", &*attempt.model.upstream);
                    self.span
                        .record("upstream_model", &*attempt.model.upstream_model);
                    self.span.record("attempts", attempt.attempts);
                    self.span.record("fallback", attempt.fallback);
                }
                None if self.identified => {
                    self.span.record("attempts", 0);
                    self.span.record("fallback", false);
                }
                None => {}
            }
        }
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
pub(crate) mod tests {
    use super::*;
    use std::time::Duration;

    /// Records one finished request with `status` on `handles`.
    pub fn finish_with(handles: &Arc<RequestMetrics>, status: u16) {
        let mut observation = Observation::new(handles.clone(), Span::none(), false);
        observation.status = StatusCode::from_u16(status).unwrap();
        observation.finish();
    }
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
            Observation::new(metrics.handles("public", "d", 0, ""), Span::none(), false);
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
            Observation::new(metrics.handles("", "", 0, ""), Span::none(), false);
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
        let text = metrics.encode(&InFlightRegistry::default()).unwrap();
        assert!(text.contains("lumewarp_ttft_seconds_sum{requested=\"public\",model=\"d\"} 1.3"));
        assert!(text.contains("lumewarp_ttft_seconds_count{requested=\"public\",model=\"d\"} 1"));
    }

    #[test]
    fn encoder_and_bounded_labels() {
        let (_, snapshot) = lumewarp_config::Config::parse(
            r#"
upstreams:
  - {name: u, kind: vllm, base_url: 'http://localhost:8123/v1'}
models:
  - {name: d, upstream: u, upstream_model: actual}
routers:
  - {name: public, members: [{model: d}]}
"#,
        )
        .unwrap();
        let runtime = crate::runtime::Runtime::new();
        let router = crate::router::RoutingTable::compile(
            &snapshot,
            lumewarp_config::AuthMode::Open,
            &runtime,
        )
        .unwrap();
        let anonymous = crate::auth::Caller::Anonymous;
        let metrics = &runtime.metrics;
        metrics.set_yaml_hash("123456789abc");
        assert!(!router
            .names
            .contains_key("client-supplied-secret-requested"));
        let mut observation = Observation::new(router.unknown(&anonymous, 0), Span::none(), false);
        observation.status = StatusCode::NOT_FOUND;
        observation.finish();
        observation.finish();
        let text = runtime.encode_metrics().unwrap();
        assert!(text.contains("lumewarp_requests_total{requested=\"unknown\",endpoint=\"chat_completions\",model=\"\",team=\"\",status=\"404\"} 1"));
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
            "lumewarp_config_apply_failures_total",
            "lumewarp_config_stream_connected",
            "lumewarp_usage_events_dropped_total",
            "lumewarp_usage_events_reported_total",
            "lumewarp_usage_events_failed_total",
        ] {
            assert!(text.contains(name), "missing {name}");
        }
        assert!(text.contains("requested=\"unknown\""));
        assert!(!text.contains("client-supplied-secret-requested"));
        assert!(!text.contains("requested=\"unknown\",endpoint=\"healthz\""));
        assert!(!text.contains("requested=\"unknown\",endpoint=\"metrics\""));
        assert!(!text.contains("requested=\"unknown\",endpoint=\"other\""));
        drop(Observation::new(
            router.unknown(&anonymous, 0),
            Span::none(),
            false,
        ));
        assert!(runtime.encode_metrics().unwrap().contains(
            "requested=\"unknown\",endpoint=\"chat_completions\",model=\"\",team=\"\",status=\"499\"} 1"
        ));
        let guard = router.acquire(&router.names["public"], 0, &anonymous);
        assert!(runtime
            .encode_metrics()
            .unwrap()
            .contains("lumewarp_in_flight_requests{model=\"d\"} 1"));
        drop(guard);
        assert!(runtime
            .encode_metrics()
            .unwrap()
            .contains("lumewarp_in_flight_requests{model=\"d\"} 0"));
        let mut fallback = Observation::new(router.unknown(&anonymous, 0), Span::none(), false);
        fallback.status = StatusCode::IM_A_TEAPOT;
        drop(fallback);
        assert!(runtime.encode_metrics().unwrap().contains(
            "requested=\"unknown\",endpoint=\"chat_completions\",model=\"\",team=\"\",status=\"other\"} 1"
        ));
        finish_with(&router.unknown(&anonymous, 0), 403);
        assert!(runtime.encode_metrics().unwrap().contains(
            "requested=\"unknown\",endpoint=\"chat_completions\",model=\"\",team=\"\",status=\"403\"} 1"
        ));
    }

    #[test]
    fn usage_metrics_are_allocation_free_after_first_use() {
        let metrics = Metrics::new();
        let handles = metrics.handles("public", "d", 0, "t1");
        let record = || {
            handles.record_tokens(3, 4);
            metrics.spend("t1", 1);
            metrics.usage_captured(UsageState::Reported);
        };
        record();
        let ((allocations, _), ()) = crate::test_alloc::count(record);
        assert_eq!(allocations, 0);
        let text = metrics.encode(&InFlightRegistry::default()).unwrap();
        assert!(text.contains(
            "lumewarp_tokens_total{requested=\"public\",team=\"t1\",direction=\"output\"} 8"
        ));
    }

    #[test]
    fn later_task_methods() {
        let metrics = Metrics::new();
        let inflight = InFlightRegistry::default();
        metrics.config_applied(42);
        metrics.config_apply_failed(NackCode::PersistFailed);
        metrics.config_apply_failed(NackCode::PersistFailed);
        metrics.config_stream_connected(true);
        metrics.usage_dropped();
        metrics.usage_batch(500, 3);
        metrics.usage_dropped_n(2);
        metrics.usage_queue_depth(7);
        metrics.usage_captured(UsageState::Reported);
        metrics.usage_captured(UsageState::Partial);
        let handles = metrics.handles("public", "d", 0, "t1");
        handles.record_tokens(10, 4);
        metrics.spend("t1", 1_000_000_000);
        metrics.spend("t1", 500_000_000);
        metrics.spend("t2", 1);
        let text = metrics.encode(&inflight).unwrap();
        for line in [
            "lumewarp_config_version 42",
            "lumewarp_config_apply_failures_total{code=\"persist_failed\"} 2",
            "lumewarp_config_apply_failures_total{code=\"invalid_config\"} 0",
            "lumewarp_config_stream_connected 1",
            "lumewarp_usage_events_dropped_total 3",
            "lumewarp_usage_queue_depth 7",
            "lumewarp_usage_capture_total{state=\"reported\"} 1",
            "lumewarp_usage_capture_total{state=\"partial\"} 1",
            "lumewarp_usage_capture_total{state=\"invalid\"} 0",
            "lumewarp_usage_events_reported_total 500",
            "lumewarp_usage_events_failed_total 3",
            "lumewarp_tokens_total{requested=\"public\",team=\"t1\",direction=\"input\"} 10",
            "lumewarp_tokens_total{requested=\"public\",team=\"t1\",direction=\"output\"} 4",
            "lumewarp_spend_total{team=\"t1\"} 1.5",
        ] {
            assert!(text.contains(line), "missing {line}:\n{text}");
        }
    }

    #[test]
    fn status_labels_are_the_statuses_table() {
        for status in [
            200, 204, 400, 401, 403, 404, 408, 413, 429, 499, 500, 502, 503, 504,
        ] {
            let label = status_label(status);
            assert!(STATUSES.contains(&label), "{status} -> {label}");
            assert_eq!(STATUSES[status_index(status)], label);
        }
        for status in [0, 201, 301, 418, 599, 1000] {
            assert_eq!(status_label(status), "other");
            assert_eq!(STATUSES[status_index(status)], "other");
        }
        // The pre-routing rejection and cancellation statuses are their own
        // series, not `other`.
        assert_eq!(status_label(499), "499");
        assert_eq!(status_label(403), "403");
    }

    #[test]
    fn only_an_approved_tool_name_becomes_a_label() {
        assert_eq!(tool_label(true, "echo"), "echo");
        assert_eq!(tool_label(false, "echo"), UNAPPROVED_TOOL);
        assert_eq!(tool_label(true, ""), UNAPPROVED_TOOL);
        assert_eq!(
            tool_label(true, &"t".repeat(MAX_TOOL_LABEL + 1)),
            UNAPPROVED_TOOL
        );
        let longest = "t".repeat(MAX_TOOL_LABEL);
        assert_eq!(tool_label(true, &longest), longest);
        // A tool name with metrics-breaking characters stays a label value
        // (the encoder escapes it), it is never parsed as extra labels.
        assert_eq!(tool_label(true, "a{b=\"c\""), "a{b=\"c\"");
    }

    #[test]
    fn plugin_labels_are_bounded() {
        assert_eq!(plugin_kind_label("router-strategy"), "router-strategy");
        assert_eq!(plugin_kind_label("upstream-auth"), "upstream-auth");
        assert_eq!(plugin_kind_label("a2a-middleware"), "a2a-middleware");
        assert_eq!(plugin_kind_label("something-new"), "other");
        assert_eq!(plugin_kind_label(""), "other");
        for (i, result) in PLUGIN_RESULTS.iter().enumerate() {
            assert_eq!(plugin_result_index(result), Some(i));
        }
        assert_eq!(plugin_result_index("panicked"), None);
        assert_eq!(PLUGIN_KINDS.len(), 4);
    }

    #[test]
    fn mcp_and_a2a_families_are_exported_and_bounded() {
        let metrics = Metrics::new();
        let mcp = metrics.mcp_request_handles("s1", MCP_METHODS[2]);
        mcp[status_index(200)].inc();
        mcp[status_index(200)].inc();
        mcp[status_index(502)].inc();
        let tools = metrics.mcp_tool_call_handles("s1", "echo", "t1", true);
        tools[status_index(200)].inc();
        let hidden = metrics.mcp_tool_call_handles("s1", "shadow-tool", "t1", false);
        hidden[status_index(200)].inc();
        metrics
            .mcp_upstream_duration("s1")
            .observe(Duration::from_millis(25).as_secs_f64());
        let a2a = metrics.a2a_request_handles("a1", A2A_METHODS[1]);
        a2a[status_index(200)].inc();
        metrics
            .a2a_upstream_duration("a1")
            .observe(Duration::from_millis(5).as_secs_f64());
        metrics.catalog_report_dropped("oversized");
        metrics.catalog_report_dropped("oversized");
        metrics.catalog_report_dropped("pending_limit");
        let plugins = metrics.plugin_handles("a2a-middleware", "strip_artifacts");
        plugins.record("ok", Duration::from_micros(50));
        plugins.record("timeout", Duration::from_millis(50));
        plugins.record("panicked", Duration::from_millis(1));
        let text = metrics.encode(&InFlightRegistry::default()).unwrap();
        for line in [
            "lumewarp_mcp_requests_total{server=\"s1\",method=\"tools_call\",status=\"200\"} 2",
            "lumewarp_mcp_requests_total{server=\"s1\",method=\"tools_call\",status=\"502\"} 1",
            "lumewarp_mcp_tool_calls_total{server=\"s1\",tool=\"echo\",team=\"t1\",status=\"200\"} 1",
            "lumewarp_mcp_tool_calls_total{server=\"s1\",tool=\"_unapproved\",team=\"t1\",status=\"200\"} 1",
            "lumewarp_mcp_upstream_duration_seconds_count{server=\"s1\"} 1",
            "lumewarp_a2a_requests_total{agent=\"a1\",method=\"message_stream\",status=\"200\"} 1",
            "lumewarp_a2a_upstream_duration_seconds_count{agent=\"a1\"} 1",
            "lumewarp_mcp_catalog_reports_dropped_total{reason=\"oversized\"} 2",
            "lumewarp_mcp_catalog_reports_dropped_total{reason=\"pending_limit\"} 1",
            "lumewarp_mcp_catalog_reports_dropped_total{reason=\"invalid\"} 0",
            "lumewarp_plugin_calls_total{kind=\"a2a-middleware\",plugin=\"strip_artifacts\",result=\"ok\"} 1",
            "lumewarp_plugin_calls_total{kind=\"a2a-middleware\",plugin=\"strip_artifacts\",result=\"timeout\"} 1",
            // An unknown result is counted as an error.
            "lumewarp_plugin_calls_total{kind=\"a2a-middleware\",plugin=\"strip_artifacts\",result=\"error\"} 1",
            "lumewarp_plugin_duration_seconds_count{kind=\"a2a-middleware\",plugin=\"strip_artifacts\"} 3",
        ] {
            assert!(text.contains(line), "missing {line}:\n{text}");
        }
        assert!(
            !text.contains("shadow-tool"),
            "unapproved names never label"
        );
    }

    #[test]
    fn catalog_drops_are_counted_through_the_trait() {
        let metrics = Metrics::new();
        // How `Runtime` builds its catalog registry: `Arc<Metrics>` coerces to
        // the `Arc<dyn catalog::Drops>` the registry takes.
        let _ = crate::catalog::Registry::with_drops(std::sync::Arc::new(Metrics::new()));
        crate::catalog::Drops::catalog_dropped(&metrics, "invalid");
        let text = metrics.encode(&InFlightRegistry::default()).unwrap();
        assert!(text.contains("lumewarp_mcp_catalog_reports_dropped_total{reason=\"invalid\"} 1"));
    }
}
