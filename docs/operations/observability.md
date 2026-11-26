# Metrics, alerts and tracing

The gateway and the control plane each expose Prometheus metrics on their own listener;
every metric is listed in the [metrics reference](../reference/metrics.md). The control
plane's [request log](../admin/requests.md) keeps one metadata row per request; a request's
spans, including every upstream attempt, are looked up by its id in traces.

## Looking up a request

The gateway writes no access log of its own. The control plane's request log keeps one row
per gateway request from its usage event: the called name, alias, router, fallback, final
model and upstream, status, attempts, tokens, cost and duration. It never stores prompts,
responses or headers. Each response carries `x-lumewarp-request-id`, the same id as its
usage event, request-log row and trace; the `request` span carries it as `request_id`, with
the called name, alias, router, final model and one `upstream_attempt` child per try. Use
the request log for the metadata and OTel for the per-attempt detail. To turn OTel on:

```yaml
gateway:
  otel:
    endpoint: http://otel-collector.observability:4318
    samplingRatio: 1
```

What the lookup depends on:

- With `gateway.otel.endpoint` empty there is no trace record at all; the request log still has the metadata row.
- With `samplingRatio` below 1, or a full exporter queue, some requests cannot be found.
  Use 1 if you need every request in OTel. A slow collector loses spans without any log line
  or metric; the queue is tuned with `OTEL_BSP_MAX_QUEUE_SIZE`,
  `OTEL_BSP_SCHEDULE_DELAY` and `OTEL_BSP_MAX_EXPORT_BATCH_SIZE` in `gateway.extraEnv`
  (measurements in the [Gateway](./gateway.md)).
- Retention is whatever the OTel backend keeps.
- The gateway ignores the sampled flag of a client's `traceparent`, so a client cannot erase
  its own record; it still joins the client's trace.

The request log and usage events do not depend on any of this, but they have no per-attempt detail.

## Tracing and OTLP

`RUST_LOG` controls console tracing (default `info`). Without an exporter,
request spans are DEBUG and the normal info filter disables them. Logs and
request span attributes do not include prompts, responses, or bearer keys.

Set a **nonempty generic endpoint** to enable the HTTP/protobuf trace exporter:

```sh
export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318
cargo run --release -- --config /tmp/lumewarp.yaml  # from gateway/
```

The exporter uses a batch processor and service name `lumewarp-gateway`.
No OTLP metrics exporter is installed. Relevant environment variables from the
compiled exporter/SDK:

| Variable | Effect |
| --- | --- |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Gateway's enable switch; generic collector base URL, with `/v1/traces` appended for traces. Unset/empty disables OTel. |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | Full trace endpoint override, used as-is, including its path. Does **not** enable OTel by itself. |
| `OTEL_EXPORTER_OTLP_HEADERS` / `OTEL_EXPORTER_OTLP_TRACES_HEADERS` | Collector request headers as comma-separated `key=value` entries; trace-specific variable takes precedence. |
| `OTEL_EXPORTER_OTLP_TIMEOUT` / `OTEL_EXPORTER_OTLP_TRACES_TIMEOUT` | Export HTTP timeout in milliseconds (SDK default 10000); trace-specific setting takes precedence. Not an upstream LLM timeout. |
| `OTEL_BSP_SCHEDULE_DELAY` | Batch schedule delay in milliseconds (SDK default 5000). |
| `OTEL_BSP_MAX_QUEUE_SIZE` | Batch queue capacity (SDK default 2048 spans). |
| `OTEL_BSP_MAX_EXPORT_BATCH_SIZE` | Maximum batch size (SDK default 512 spans). |

Use an HTTP/protobuf collector endpoint, not a gRPC-only endpoint.

The request log holds metadata only; OTel is where a single request's detailed
record is looked up, starting from the `x-lumewarp-request-id` response header. The
`request` span has `request_id`, `endpoint`, `team_id`, `key_id`, `requested`, `alias`,
`router`, `model`, `upstream`, `upstream_model`, `attempts`, `fallback`,
`config_version`, `status` and `stream`. Each `upstream_attempt` child has `model`,
`upstream`, `upstream_model`, `attempt`, `fallback`, `outcome` and `upstream_status`.
When an LLM upstream uses a WASM auth plugin, an `upstream_auth` span records
`upstream`, `plugin`, `method`, `result` and `cached`. Prompts, responses and keys are
never span attributes; `key_id` is the public id.

Every `/v1/*` response has the header, including a 503 `not_ready` before any
snapshot is loaded (`config_version` 0 and no `key_id`; there is no usage event,
since the key is unknown). A request that made no upstream attempt has `attempts`
0 and `fallback` false. A client that goes away while an attempt waits for upstream
headers leaves that attempt's `upstream`, `upstream_model`, `attempts` and
`fallback`, matching the usage event. A 403 span has the `alias` and `router` of a
known name, which the response does not reveal. Authentication rejections have no
`requested`: the body is not read for them.

This only works under these conditions:

- Without `OTEL_EXPORTER_OTLP_ENDPOINT` there is no trace record at all.
- Sampling below 100%, or a full export queue, loses records, and the gateway
  neither logs nor counts that loss. A request produces at least 2 spans, so at
  1000 RPS the default queue of 2048 spans holds about one second. Measured at
  1000 RPS for 30 s (60000 spans): with a collector answering at once the defaults
  lost nothing; with a collector taking 1 s per export the defaults delivered 17408
  (71% lost), while `OTEL_BSP_MAX_QUEUE_SIZE=65536`, `OTEL_BSP_SCHEDULE_DELAY=200`
  and `OTEL_BSP_MAX_EXPORT_BATCH_SIZE=2048` delivered all 60000. To keep a detailed
  record for every request, keep the 100% default and size the queue for your rate and
  for how long the collector may stall.
- How long records are kept is decided by the OTel backend.

Usage events do not depend on any of this; they just have no per-attempt detail.

Sampling is decided by the gateway, not by the client. The SDK's default sampler
is `ParentBased(AlwaysOn)`, which obeys `traceparent` sampled=0 and would let a
client erase its own record (`default_sdk_sampler_lets_a_client_suppress_its_record`
pins this). The gateway therefore installs a parent-ignoring sampler: a span is
exported in the client's trace, as a child of its parent, whatever that flag says,
and the flag sent upstream is the gateway's decision.
`OTEL_TRACES_SAMPLER` keeps its standard names (`always_on`, `always_off`,
`traceidratio` with `OTEL_TRACES_SAMPLER_ARG`), with `parentbased_*` meaning the
same as the plain name. Anything else logs a warning and samples everything.

OTel-enabled spans are INFO; the OTel layer accepts only `lumewarp` targets at INFO or above and
disables code-location attributes. It does not enable/export dependency DEBUG /
TRACE events. Explicit console verbosity is still controlled by `RUST_LOG`.

Incoming W3C `traceparent` / `tracestate` establish the parent; the gateway
injects its span context upstream. With OTel disabled, there is no extraction /
injection, just header passthrough. Exporter creation and export work are absent
when the enable switch is unset.

## Shared-limits sync

A gateway started with `--shared-limits-redis-url` adds five series, with no scope or
key labels: `lumewarp_shared_limits_fallback` (`1` while it counts per gateway),
`lumewarp_shared_limits_sync_duration_seconds{result="success"|"error"}`,
`lumewarp_shared_limits_sync_errors_total{reason="connect"|"timeout"|"script"}`,
`lumewarp_shared_limits_last_sync_age_seconds` and `lumewarp_shared_limits_scopes`. They
do not exist without a URL. A failed sync and a sync slower than the interval also open
a `shared_limits_sync` span with the result, reason, namespace, scope count and elapsed
milliseconds, so a trace shows whether Redis or the gateway is slow.
`LumewarpSharedLimitsFallback` warns after two minutes in fallback: check Redis
reachability, credentials and the namespace, then the sync error reason. While the
alert fires the limits are per gateway, so N gateways allow up to N times the configured
rate.

## Alert rules and Grafana dashboards

Enable the bundled observability resources independently:

```sh
helm template lumewarp deploy/helm/lumewarp -n lumewarp \
  --set monitoring.serviceMonitor.enabled=true \
  --set monitoring.prometheusRule.enabled=true \
  --set monitoring.prometheusRule.labels.release=kube-prometheus-stack \
  --set monitoring.grafanaDashboards.enabled=true
```

`monitoring.prometheusRule.enabled` defaults to `false` and requires Prometheus Operator
CRDs. `monitoring.prometheusRule.labels` must match your Prometheus `ruleSelector`.
`monitoring.grafanaDashboards.enabled` defaults to `false` and creates a ConfigMap containing
five dashboards. The sidecar label defaults to `grafana_dashboard: "1"`; override
`monitoring.grafanaDashboards.sidecarLabel` / `sidecarLabelValue` to match your Grafana
sidecar, which must watch the release namespace. Select a Prometheus datasource
using the dashboard's `datasource` variable.

### Dashboards

Each dashboard leads with at-a-glance panels and groups the remaining trends into
collapsible rows. Every panel has a description, units and thresholds; the `namespace`
filter applies to all of them.

| Dashboard | Leads with | Also contains |
| --- | --- | --- |
| `Lumewarp / Overview` | Stat tiles for requests/s, 5xx ratio, gateway-overhead p99, TTFT p99, tokens/s and spend in range, with sparklines and thresholds; a 5xx gauge; a request-duration heatmap; an endpoint donut | Top called names by requests and tokens, top teams by spend, then the per-name traffic, latency, token and upstream-error trends |
| `Lumewarp / Routing` | A per-model table from instant queries: probe health, breaker state, in-flight, vLLM queue, KV-cache usage, p99 and 5xx | Fallback and retry-budget bargauges, an attempt-outcome donut, split share, session-affinity hit/moved stats and the live-override gauge, then attempts, breaker, health, queue, in-flight and rewrite trends |
| `Lumewarp / Usage` | Token, spend and request totals with the percent change against the preceding equal-length range, plus a 5xx ratio | Prompt/completion and per-team token donuts, top teams and called names, pipeline-health stats (failures, drops, queue depth, metering gaps), then token, spend and delivery trends |
| `Lumewarp / MCP & A2A` | MCP and A2A request rates and 5xx ratios, plugin trap/timeout share and dropped catalog reports | Error-ratio gauges, top tools and agents, upstream-latency heatmaps, then method, tool, plugin and catalog-review trends |
| `Lumewarp / Control plane` | Connected sessions, worst-zone outbox backlog, compile failures and NACKs in the last hour, skipped usage events and compile rate | Per-zone and per-gateway state tables, a compile-duration heatmap, then ACK/NACK, version, ingestion, authorization and key-lifecycle trends |

Filters follow the labels the metrics actually carry: `requested` and `team` on Overview
and Usage, `requested`, `router` and `model` on Routing, and `server`/`agent` on
MCP & A2A. Gateway metrics carry no zone label, so zone selection is not offered there.
The affinity panels need the gateway's `lumewarp_session_affinity_total` and
`lumewarp_session_affinity_overrides` series. The `Lumewarp / MCP & A2A` dashboard
reads gateway MCP/A2A, plugin and catalog metrics plus the control plane's
`lumewarp_cp_mcp_*` counters, so its review panels need `controlPlane.metrics.enabled`
and its traffic panels a gateway ServiceMonitor.

The chart bundles `files/prometheus/rules.yaml` and `files/dashboards/*.json`.
Their sources of truth are `deploy/prometheus/rules.yaml` and
`deploy/grafana/dashboards/*.json`. Helm cannot read files outside a packaged chart,
so update the bundled copies whenever sources change; the CI observability job
checks byte equality and validates all dashboard expressions with promtool.

Seven of the alerts (circuit open, model and upstream unhealthy, 5xx ratio, TTFT, vLLM
queue and KV cache) also carry a `lumewarp_status` label and a `lumewarp_summary`
annotation, and a `zone` label taken from `lumewarp_gateway_info`. With Alertmanager
pointed at the control plane they become the users' status banner and the admin incident
history; see [Alerts and incidents](./alerts-and-incidents.md). Upgrade the gateways before
the rules, because a gateway without `lumewarp_gateway_info` has no zone and so no alert.

Operational defaults and response steps are documented beside each alert in
`rules.yaml`. The defaults include 5% 5xx at >=1 request/s, overhead p99 >10 ms
for 10 minutes, TTFT p99 >5 s for 10 minutes, and queue >80,000 of the fixed
100,000-event capacity. The MCP and A2A alerts cover plugin trap/timeout share,
upstream 502/504 share per server and agent, MCP definitions stored as pending
(the review backlog) and discarded catalog reports. The shared-limits fallback alert
covers gateways whose Redis sync has been down for two minutes. Tune TTFT, vLLM queue
depth and backlog limits against production baselines. Overhead's alert threshold is
not the benchmark target.

Preserve the `namespace`, `job` and `instance` scrape labels. Version comparison
assumes one control-plane installation per namespace (no cross-installation joins).
The gateway-down alert selects `up{job=~".*lumewarp.*gateway.*"}`, matching chart
gateway Service names; custom scrape jobs must follow that naming or adjust the
selector. Missing targets are not detected by `up == 0`; configure scrape target
discovery separately. Standalone YAML gateways are excluded from config-stream
and version-mismatch alerts via `lumewarp_config_info{source="yaml"}`.

`monitoring.serviceMonitor.enabled` scrapes both the gateway admin port and the control
plane's dedicated `metrics` port (`lumewarp_cp_*`, see [Metrics](../reference/metrics.md)).
Panels whose exporters are not deployed stay empty; missing metrics do not trigger
these threshold alerts. The control plane exports
`lumewarp_cp_key_reconcile_last_success_timestamp_seconds` as 0 until the key
reconciler's first success, so a never-successful reconciler still alerts. With
`LUMEWARP_KEY_RECONCILE_ENABLED=false` the series is not exported at all.

Dashboard filters apply only where metric labels exist: tokens support team and
model, while spend supports team only. Spend panels explicitly cover all models;
model-level cost cannot be derived from the current metrics contract, so no dashboard
offers spend by requested name. Latency histograms have no team label, and global
pipeline and model gauges are not filtered by team. Gauge observations of the same vLLM
upstream are aggregated with max/min, not summed across gateways. There is no budget
metric, so budget burn stays in the Admin UI and API rather than on a dashboard.

Validate rules locally (pinned CI image):

```sh
docker run --rm -v "$PWD/deploy/prometheus:/w" -w /w \
  --entrypoint promtool prom/prometheus:v3.5.0 check rules rules.yaml
docker run --rm -v "$PWD/deploy/prometheus:/w" -w /w \
  --entrypoint promtool prom/prometheus:v3.5.0 test rules rules.test.yaml
```
