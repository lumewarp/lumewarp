# Metrics

## Gateway

Scrape the **admin** port, not the primary listener. Histograms expose `_bucket`,
`_sum`, and `_count`. Zero-valued series for successful statuses are pre-registered for
`team=""`; series for a team, and every series for an error status, appear on their first
request, so `rate()` over an error series needs an absent-safe form such as
`or vector(0)`. The registry lives for
the whole process, so counters are never reset by a config change.

| Series | Type | Labels |
| --- | --- | --- |
| `lumewarp_requests_total` | Counter | `requested`, `endpoint`, `model`, `team`, `status`, `error_source`, `code` |
| `lumewarp_gateway_info` | Gauge | `zone`, `gateway_id`, `version`; always `1`. |
| `lumewarp_rate_limited_total` | Counter | `scope` (`key`, `team`, `member`, `org`, `service_account`, `other`); the gateway's own 429 for a rate limit. |
| `lumewarp_request_attempts` | Histogram | `requested`; upstream attempts per request, buckets 1, 2, 3, 4, 5, 8. |
| `lumewarp_request_duration_seconds` | Histogram | `requested`, `endpoint`, `model` |
| `lumewarp_gateway_overhead_seconds` | Histogram | `endpoint` |
| `lumewarp_request_body_read_seconds` | Histogram | `endpoint` |
| `lumewarp_ttft_seconds` | Histogram | `requested`, `model` |
| `lumewarp_upstream_errors_total` | Counter | `model`, `kind` |
| `lumewarp_upstream_attempts_total` | Counter | `requested`, `model`, `outcome`: `success`, `retryable_status`, `connect_error`, `timeout`, `circuit_open`, `non_retryable` |
| `lumewarp_upstream_auth_total` | Counter | `upstream`, `result`: `ok`, `cached`, `plugin`, `timeout`, `trap`, `header`, `configure`; LLM upstream auth decisions. `ok` is a WASM call that answered, `cached` a cache hit that did not enter the sandbox. |
| `lumewarp_fallbacks_total` | Counter | `router`, `model` |
| `lumewarp_retries_budget_exhausted_total` | Counter | `router` |
| `lumewarp_upstream_circuit_state` | Gauge | `model`; 0 closed, 1 half-open, 2 open |
| `lumewarp_upstream_healthy` | Gauge | `upstream`; 1 healthy, 0 unhealthy |
| `lumewarp_model_healthy` | Gauge | `model`; chat check 1 healthy, 0 unhealthy (1 without `chat_check`) |
| `lumewarp_in_flight_requests` | Gauge | `model` |
| `lumewarp_split_requests_total` | Counter | `router`, `split`; requests assigned to a traffic split. |
| `lumewarp_session_affinity_total` | Counter | `router`, `result` (`hash`, `override`, `moved`, `none`); how a session-affinity router picked a member. |
| `lumewarp_session_affinity_overrides` | Gauge | None; live session-affinity overrides in this process (expired entries are pruned when scraped). |
| `lumewarp_session_affinity_sync_errors_total` | Counter | `reason` (`connect`, `timeout`, `script`); override-exchange failures with Redis, only with a shared-limits Redis. |
| `lumewarp_vllm_queue_depth` | Gauge | `model`; last scraped `vllm:num_requests_waiting`. |
| `lumewarp_vllm_kv_cache_usage` | Gauge | `model`; last scraped KV-cache usage, 0..1. |
| `lumewarp_vllm_scrape_failures_total` | Counter | `upstream`; failed or incomplete vLLM metrics scrapes. |
| `lumewarp_upstream_latency_ewma_seconds` | Gauge | `model`; `lowest_latency` EWMA (models with samples). |
| `lumewarp_config_version` | Gauge | None; active snapshot version, standalone YAML is `0`. |
| `lumewarp_config_info` | Gauge | `source="yaml"`, `sha256` (first 12 lowercase hex characters of the YAML file hash); value `1`. |
| `lumewarp_config_apply_failures_total` | Counter | `code` (`base_mismatch`, `invalid_config`, `unsupported_feature`, `persist_failed`, `unspecified`) |
| `lumewarp_config_stream_connected` | Gauge | None; `1` while connected to the control plane. |
| `lumewarp_usage_events_dropped_total` / `_reported_total` / `_failed_total` | Counter | None |
| `lumewarp_usage_queue_depth` | Gauge | None; events waiting to be reported. |
| `lumewarp_usage_capture_total` | Counter | `state` (`unspecified`, `reported`, `missing`, `partial`, `invalid`); metering gaps. |
| `lumewarp_tokens_total` | Counter | `requested`, `team`, `direction` (`input`/`output`) |
| `lumewarp_spend_total` | Counter | `team`; USD. |
| `lumewarp_shared_limits_sync_duration_seconds` | Histogram | `result` (`success`, `error`); only with a shared-limits Redis. |
| `lumewarp_shared_limits_sync_errors_total` | Counter | `reason` (`connect`, `timeout`, `script`). |
| `lumewarp_shared_limits_fallback` | Gauge | None; `1` while the gateway counts limits per gateway because Redis is unreachable. |
| `lumewarp_shared_limits_last_sync_age_seconds` | Gauge | None; seconds since the last successful sync. |
| `lumewarp_shared_limits_scopes` | Gauge | None; scopes in the last sync. |

The `lumewarp_shared_limits_*` series exist only when
`--shared-limits-redis-url` is set and carry no scope or key labels. The same
URL, namespace and interval carry the session-affinity override exchange;
`lumewarp_session_affinity_sync_errors_total` exists only then too. The
config-stream
series move in control-plane mode; `lumewarp_config_info` is
set only in standalone mode. Usage event counters and queue depth move in
control-plane mode. Token and spend series move in both modes whenever the
upstream reports usage; control-plane mode forces stream usage where the
upstream supports it.

Label semantics:

- `requested` is the **name the client called**, of any kind, never
  `upstream_model`. Unknown requested names use the fixed label `unknown`; other pre-routing rejections and local
  endpoints use `requested=""` / `model=""`. Arbitrary client names never become
  labels; they are kept only in the usage event.
- `endpoint` is `chat_completions`, `completions`, `responses`, `embeddings`,
  `audio_transcriptions`, `audio_translations`, `decisions`, `systemone`, or `models`; local
  health/admin/other paths use bounded `healthz`, `metrics`, or `other` labels.
- `model` is the configured model name, also in `lumewarp_fallbacks_total`, where
  `router` is the router whose fallback was used, whatever name the client called.
- `team` is the caller key's team id; `""` for anonymous callers, team-less
  keys, and requests rejected before authentication succeeds (401, and 429
  `insufficient_quota`).
- `status` uses `200`, `204`, `400`, `401`, `403`, `404`, `408`, `413`, `429`, `499`,
  `500`, `502`, `503`, `504`, or fallback `other`. A handler dropped **before a
  response exists** records 499; this is a metric label, not a sent HTTP response.
  After headers, a dropped/broken body retains the response's HTTP status.
- `error_source` says who an unsuccessful request is to blame on: `none` for a success,
  `client` (a malformed or unauthenticated request, a 4xx other than 403 and 429, or a client
  that went away), `policy` (403 and 429: a name, a limit or a budget refused it),
  `upstream` (an `upstream_*` gateway code, or a non-2xx status the upstream answered and
  the gateway passed through) or `gateway` (everything else, 5xx included).
- `code` is the stable code of the error: one of the gateway's own
  [error codes](./errors.md#gateway); `upstream_<status>` (for example `upstream_503`) for
  a passed-through upstream status; `client_cancelled` when the client dropped before any
  response existed (the 499 `status`, which is not an error code the gateway ever sends);
  empty on success. The set is fixed, so the label cannot grow from client input. The same
  value is the usage event's `error_code`, shown in the [request log](../admin/requests.md).
- `lumewarp_gateway_info` names the gateway by its zone and its control-plane-assigned id;
  a standalone gateway has both empty. A reconnect assigns a new id and removes the old
  series. Join it onto other gateway series on `namespace` and `instance` with
  `group_left(zone)`, as the shipped [alert rules](../operations/alerts-and-incidents.md#rules-that-ship-with-lumewarp) do.
- `lumewarp_rate_limited_total` counts the gateway's own 429 (an LLM request over its
  rpm, tpm or concurrency limit, or an MCP `tools/call` over the key's rpm), by the scope
  whose limit was hit. A 429 that an upstream answered is `upstream_429` in
  `lumewarp_requests_total`, never here.
- `lumewarp_request_attempts` is observed once per request that reached an upstream
  attempt (a success, a passed-through error or all attempts exhausted); a request refused
  or dropped before routing completes is not observed.
- `kind` is `connect`, `protocol` (other upstream request failures), `timeout`
  (response headers), or `stream` (upstream body failure, including non-stream
  response bodies). A truncated 200 response increments `kind="stream"` while
  its request counter still records 200.
- `upstream` in `lumewarp_upstream_auth_total` is the configured upstream name;
  `result` is the auth decision class. The matching `upstream_auth` span carries
  `upstream`, `plugin`, `method`, `result` and `cached`.
- No API-key labels exist. In-flight gauges read the process-lifetime model
  counters at scrape time.

### Timing definitions

These follow the doc comments in [`metrics.rs`](https://github.com/lumewarp/lumewarp/blob/main/gateway/crates/lumewarp-gateway/src/metrics.rs):

- **Request duration:** handler entry through response-body completion or drop,
  including upload, upstream waiting, and stream lifetime. Counters and duration
  are finalized once at completion/drop, not at receipt of response headers.
- **Body read:** only the client body collection interval, including failed,
  timed-out, and cancelled reads. No observation is made if body collection was
  never started (for example an early auth failure or Content-Length rejection).
- **Gateway overhead:** excludes client uploads and all upstream waiting,
  including prefill. For both stream and non-stream responses it is
  `(body_read → upstream sent) + (upstream headers → handler return)`.
  Streams additionally include the first frame's local forwarding work
  `(upstream frame ready → frame forwarded)`. Neither upstream body generation
  nor client socket flush/ACK time is measured. Without routing, it is
  `body_read → handler return` (request start if no body read). Cancelled uploads
  contribute only to body-read duration, not gateway overhead.
- **TTFT:** for successful upstream stream responses, handler entry through the
  first nonempty data frame ready for forwarding to hyper. It includes upload
  and prefill, observes only the first frame, and does not parse SSE tokens or
  measure client socket flush/ACK time.

Overhead histogram finite buckets run from 25 µs to 50 ms. These internal
processing estimates are not the benchmark's difference between gateway and
direct end-to-end latency distributions; see [bench/README.md](https://github.com/lumewarp/lumewarp/blob/main/bench/README.md).
Requests rejected before hyper constructs a handler request (for example an
incomplete HTTP header) do not have endpoint request metrics.

## Control plane

`serve` exposes Prometheus metrics on `LUMEWARP_METRICS_BIND` (default
`127.0.0.1:9465`, text format on any path). Each app owns its own `CollectorRegistry`
(`metrics.py`); there are no process/platform collectors and no `*_created` series. Labels
are bounded enums only: no key, user, team or gateway ids. Counters move only after the
transaction that makes the event real has committed. The alert rules
(`../deploy/prometheus/rules.yaml`) and the control-plane/usage Grafana dashboards consume
these names.

| Metric | Type | Labels | Updated |
| --- | --- | --- | --- |
| `lumewarp_cp_compile_duration_seconds` | histogram | | Compiler loop (`compiler/publish.py`), per committed compile: snapshot compile + validation, including compiles whose content is unchanged and failed ones. |
| `lumewarp_cp_compile_failures_total` | counter | | A compile that raised `CompileError` (outbox rows marked failed; previous version stays current). |
| `lumewarp_cp_config_version` | gauge | `zone` | Latest published version in each zone: set on publish, and refreshed from `config_versions` every 5 s (covers API rollbacks and restarts). |
| `lumewarp_cp_outbox_pending` | gauge | `zone` | Pending `config_outbox` rows in each zone; one `COUNT` every 5 s by a background task, never per scrape. |
| `lumewarp_cp_gateway_sessions` | gauge | `zone` | Connected config-stream sessions per zone in this process (`grpc/` session registry). |
| `lumewarp_cp_gateway_acks_total` | counter | `zone`, `result` = `ack` \| `nack` | Each valid ACK/NACK of a pending update in a zone. Protocol errors are not counted. |
| `lumewarp_cp_gateway_plugins_ignored_total` | counter | `kind` = `router-strategy` \| `upstream-auth` \| `a2a-middleware` \| `other` | Entries of a `Hello` ignored: past the per-gateway limit of 128 per kind, an invalid name, or (for `other`) a descriptor whose kind is unusable or a non-plugin capability over its own limit of 64. The stream stays up. |
| `lumewarp_cp_mcp_catalog_definitions_total` | counter | `result` = `approved` \| `pending` \| `known` | MCP definitions stored from a gateway `CatalogReport` or Discover now (`known`: the exact hash was already there). |
| `lumewarp_cp_mcp_catalog_discarded_total` | counter | `reason` = `unknown_target` \| `stale_id` \| `stale_version` \| `hash_mismatch` \| `invalid` \| `pending_limit` \| `definition_limit` \| `report_limit` | Definition reports dropped by validation or bounds. |
| `lumewarp_cp_mcp_definition_reviews_total` | counter | `action` = `approve` \| `reject` | Administrator decisions on pending definitions. |
| `lumewarp_cp_mcp_discover_requests_total` | counter | `result` = `ok` \| `failed` | Discover-now calls against an MCP server. |
| `lumewarp_cp_a2a_agent_cards_total` | counter | `result` = `stored` \| `discarded` | A2A agent cards recorded from gateway reports. |
| `lumewarp_cp_usage_events_ingested_total` | counter | | Events accepted by a committed `UsageService.Report` (an identical replay is accepted again but not re-costed). |
| `lumewarp_cp_usage_events_skipped_total` | counter | `reason` = `invalid_event` \| `request_id_conflict` \| `key_mismatch` | Events skipped by per-event validation of a committed report: malformed fields; a request id reused with different data in the batch or vs. a stored event; unknown key or key not in the event's team. Batch-level rejections (`INVALID_ARGUMENT`) count nothing. |
| `lumewarp_cp_authz_checks_total` | counter | `backend`, `decision` = `allow` \| `deny` \| `error` | Every `Authorizer.check` and every item of `Authorizer.batch_check` (native batch failures count one `error` per item). `backend` is `LUMEWARP_AUTHZ_BACKEND`. |
| `lumewarp_cp_keys_suspended_total` | counter | `reason` = `owner_removed` \| `owner_deactivated` \| `admin` \| `owner` | Keys moved from active to suspended: key reconciler and member removal (`owner_removed`), user deactivation (`owner_deactivated`), `PATCH /keys/{id}` (`admin`/`owner`). |
| `lumewarp_cp_key_reconcile_last_success_timestamp_seconds` | gauge | | Unix time of the key reconciler's last fully successful step: no exception, a decision for every owner checked and, with `changes()`, a readable change stream. 0 until the first success; not exported when `LUMEWARP_KEY_RECONCILE_ENABLED=false`. |
| `lumewarp_cp_key_writes_total` | counter | `operation` = `create` \| `rotate`, `result` = `success` \| `rate_limited` | Self-service key writes: a successful committed create/rotate, and an attempt rejected by the in-process per-principal, per-operation limiter. |
