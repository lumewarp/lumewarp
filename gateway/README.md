# Lumewarp gateway

A Rust HTTP/1.1 gateway for OpenAI-compatible HTTP/HTTPS upstreams, including
vLLM and OpenAI. It compiles only from a protobuf config `Snapshot`
(`proto/lumewarp/config/v1/snapshot.proto`); standalone mode reads YAML at
startup and converts it to the same snapshot. No control plane, database, or
Python is in the request path. (The control-plane config stream is P1 work in
progress; this README covers standalone mode.)

## Run locally

From the repository root, build and start the [mock upstream](../bench/README.md)
in one terminal (9100 avoids the commonly occupied 9000):

```sh
cargo build --release --manifest-path bench/mock-upstream/Cargo.toml
bench/mock-upstream/target/release/mock-upstream --listen 127.0.0.1:9100
```

Save this as `/tmp/lumewarp.yaml`:

```yaml
server:
  listen: 127.0.0.1:4000
  admin_listen: 127.0.0.1:9464
upstreams:
  - name: local
    kind: openai_compatible
    base_url: http://127.0.0.1:9100/v1
deployments:
  - name: mock-local
    upstream: local
    upstream_model: mock
routes:
  - model: demo
    deployments: [mock-local]
```

In a second terminal:

```sh
cd gateway
cargo run --release -- --config /tmp/lumewarp.yaml
```

`--config` (also `-c`) can instead be supplied through `LUMEWARP_CONFIG`.
In another terminal:

```sh
curl -sS http://127.0.0.1:4000/v1/models
curl -sS -H 'Content-Type: application/json' \
  http://127.0.0.1:4000/v1/chat/completions \
  --data-binary '{"model":"demo","messages":[{"role":"user","content":"Hello"}]}'
curl -sS -N -H 'Content-Type: application/json' \
  http://127.0.0.1:4000/v1/chat/completions \
  --data-binary '{"model":"demo","messages":[{"role":"user","content":"Hello"}],"stream":true}'
curl -sS http://127.0.0.1:9464/metrics
```

For a real upstream, change `base_url`, `upstream_model`, and optionally
`api_key_env`. [config.example.yaml](config.example.yaml) includes OpenAI and
vLLM examples; its `OPENAI_API_KEY` variable must be set even if only the vLLM
route will be used.

## YAML reference

The complete schema is in
[`lumewarp-config`](crates/lumewarp-config/src/lib.rs). Unknown YAML fields are
rejected at every object level. The YAML is converted to a version-0 snapshot
and validated exactly like a control-plane snapshot: names/ids are unique within
each upstream, deployment, route, team and key list; references must resolve;
key hashes must be unique. Environment-variable substitution inside arbitrary
YAML strings is not implemented. P0 files load unchanged.

| Top-level field | Default | Meaning |
| --- | --- | --- |
| `server` | Defaults below | Local listener, authentication shorthand, and shutdown settings. |
| `upstreams` | `[]` | List of upstream objects. |
| `deployments` | `[]` | List of deployments binding an upstream to a model. |
| `routes` | `[]` | List of public model aliases and their deployment choices. |
| `teams` | `[]` | Teams: the bounded `team` metric label and shared rate-limit scopes. |
| `keys` | `[]` | Virtual keys with status, expiry, model allowlist and rate limits. |

### Server

| Field | Default | Meaning |
| --- | --- | --- |
| `server.listen` | `0.0.0.0:4000` | Primary HTTP listener, an IP socket address (`IP:port`, or `[IPv6]:port`). |
| `server.admin_listen` | `127.0.0.1:9464` | Separate HTTP admin listener. Must differ from `listen`. |
| `server.api_keys` | `[]` | Shorthand for team-less keys that may call every model: client bearer-key SHA-256 hashes, exactly 64 lowercase hexadecimal characters each. Duplicates collapse into one key. |
| `server.shutdown_grace_ms` | `25000` | Maximum connection drain time after shutdown begins; must be greater than zero. |
| `server.header_read_timeout_ms` | `30000` | HTTP request-header read timeout on both listeners; must be greater than zero. |
| `server.body_read_timeout_ms` | `30000` | Total time allowed to collect a proxied request body, not an idle timeout; must be greater than zero. |

**Auth mode.** If neither `server.api_keys` nor `keys` configures any key, the
gateway is open: every `/v1/*` request is anonymous and `Authorization` is
ignored. As soon as one key exists, every `/v1/*` request (including
`/v1/models`) needs a valid key. Control-plane mode will always require keys,
so an empty key set there rejects everything.

Send one `Authorization: Bearer <key>` header. The gateway hashes the key with
SHA-256 and looks it up (one hash, one map lookup). Bearer scheme matching is
case-insensitive; leading whitespace before the token is trimmed. Duplicate
Authorization headers are rejected. Expired, revoked and suspended keys are
rejected like unknown keys (401) before a `budget_exceeded` status (429) is
considered. The model allowlist is checked right after the request body's
`model` is parsed and before any route lookup, so a key that may not use a model
gets 403 whether or not the alias exists. `/v1/models` lists only the aliases
the key may call.

Generate a key and its config hash without adding a newline to the hash input:

```sh
export CLIENT_KEY="$(openssl rand -hex 32)"
printf '%s' "$CLIENT_KEY" | sha256sum | awk '{print $1}'
```

Put the printed hash in `server.api_keys: ["<hash>"]` or a `keys[].key_sha256`;
retain the raw key for clients, not in YAML:

```sh
curl -sS -H "Authorization: Bearer $CLIENT_KEY" http://127.0.0.1:4000/v1/models
```

`/healthz` stays open. The admin listener has **no authentication**, including
when API keys are configured. Keep it private. For a remote Prometheus scraper,
explicitly bind `admin_listen` to a reachable address and restrict access through
your network policy. Both listeners are plain HTTP; terminate client TLS outside
the gateway.

### Upstreams

| Field | Default | Meaning |
| --- | --- | --- |
| `upstreams[].name` | Required | Unique upstream name. |
| `upstreams[].kind` | Required | `openai`, `vllm`, or `openai_compatible`. All use the same OpenAI-compatible passthrough; there is no provider translation. |
| `upstreams[].base_url` | Required | Absolute HTTP/HTTPS base URI, with an authority and without userinfo or a query. Include `/v1` when that is the upstream API prefix. |
| `upstreams[].api_key_env` | Absent | Name of the environment variable containing the raw upstream bearer token, **not** the token or a client-key hash. |
| `upstreams[].timeout_ms` | `600000` | Unsigned 32-bit millisecond timeout from issuing the upstream request through receipt of response headers, including connection establishment. Does not limit response-body or stream lifetime. (A snapshot value of 0 also means the default.) |
| `upstreams[].stream_usage_mode` | `include_usage` | `include_usage` (the gateway may force `stream_options.include_usage` upstream for metering) or `passthrough` (upstream does not support it). Not acted on yet. |

All upstreams are compiled at startup. A configured `api_key_env` must exist,
contain a valid Unicode value, and produce a valid Authorization header;
otherwise startup fails, even for an unused upstream. Tokens are read once, not
per request. If this field is absent, client Authorization is still removed and
no upstream Authorization is sent.

Endpoint suffixes `/chat/completions`, `/completions`, and `/embeddings` are
appended to the base path after trimming its trailing slash. For example,
`https://example.com/prefix/v1/` becomes
`https://example.com/prefix/v1/chat/completions`. The client's query string is
preserved. Connections are pooled with keep-alive and TCP_NODELAY; HTTPS uses
rustls with WebPKI roots.

### Deployments

| Field | Default | Meaning |
| --- | --- | --- |
| `deployments[].name` | Required | Unique deployment name; also used in metrics. |
| `deployments[].upstream` | Required | Name of an existing upstream. |
| `deployments[].upstream_model` | Required | Model string substituted for the client's public alias. |
| `deployments[].weight` | `1` | Unsigned 32-bit relative weight for weighted routing. Zero is allowed, but a weighted route must have positive total weight. Ignored by least-in-flight routing. |
| `deployments[].price` | Absent | `{input_usd_per_mtok, output_usd_per_mtok}`: USD per one million tokens as decimals, e.g. `"0.15"` (quoted or plain). Parsed from the YAML text, never through floating point, and converted exactly to integer nano-USD; at most 9 fractional digits, no exponent or sign. Absent means the price is unknown (usage is metered, cost reported as unknown); explicit zeros mean free. |

### Routes

| Field | Default | Meaning |
| --- | --- | --- |
| `routes[].model` | Required | Unique public alias accepted in requests and listed by `/v1/models`. The literal alias `unknown` is reserved and rejected. |
| `routes[].deployments` | Required | Nonempty list of existing deployment names. |
| `routes[].strategy` | `weighted` | `weighted` or `least_in_flight`. The schema also recognizes `lowest_latency`, but validation rejects it as not supported yet. |

### Teams

| Field | Default | Meaning |
| --- | --- | --- |
| `teams[].id` | Required | Unique, stable team id. It is the `team` metric label value, so keep the number of teams bounded. |
| `teams[].name` | `id` | Display name. |
| `teams[].org_id` / `teams[].org_name` | `""` | Owning organization; copied to the team's keys. |
| `teams[].rate_limits` | Absent | Limits shared by **all** keys of the team (scope `team:<id>`); see rate limits below. |

### Keys

| Field | Default | Meaning |
| --- | --- | --- |
| `keys[].key_id` | Required | Unique, public, non-secret id (appears in usage events, never in metrics). |
| `keys[].key_sha256` | Required | SHA-256 of the full secret key, 64 lowercase hex characters; unique across all keys including `server.api_keys`. |
| `keys[].team_id` | `""` | Existing team id, or empty for no team (`team=""` in metrics). |
| `keys[].project_id` | `""` | Free-form project id for usage attribution. |
| `keys[].status` | `active` | `active`, `revoked`, `suspended` (401) or `budget_exceeded` (429 `insufficient_quota`). |
| `keys[].expires_at_unix_ms` | `0` | Expiry in Unix milliseconds; `0` never expires. Expired keys get 401. |
| `keys[].allow_all_models` | `false` | Allow every route alias, present and future. |
| `keys[].allowed_models` | `[]` | Public aliases the key may call when `allow_all_models` is false. **Fail-closed:** a key with neither may call nothing. |
| `keys[].rate_limits` | Absent | Limits for this key alone (scope `key:<key_id>`). |

`rate_limits` objects have `requests_per_minute`, `tokens_per_minute` and
`max_parallel_requests` (each `0`/absent = unlimited). They are compiled into
per-key `RateLimitScope`s (team scope first, then key scope); limiter state is
keyed by scope id, so it survives reloads, and it is local to one gateway
instance (N instances allow up to N times the limit). Enforcement is not active
yet: the limiter currently admits every request.

### Routing

`weighted` samples precomputed cumulative weights. `least_in_flight` selects the
smallest atomic in-flight count, scanning from a random offset to spread ties.
This is an approximate concurrent decision, not a global scheduling lock.
Reservations last until the upstream response body ends/errors or is dropped,
or the request fails/is cancelled before headers. Counters are keyed by
deployment name for the life of the process: aliases referencing the same
deployment share one, and a recompiled snapshot keeps counting requests still
running on the previous one.

## Endpoints and passthrough

| Listener | Method and path | Behavior |
| --- | --- | --- |
| Primary | `POST /v1/chat/completions` | Proxy to the selected deployment; stream responses frame by frame. |
| Primary | `POST /v1/completions` | Same routing and passthrough. |
| Primary | `POST /v1/embeddings` | Same routing and passthrough. |
| Primary | `GET /v1/models` | Local OpenAI-shaped model list from configured route aliases, not upstream discovery. |
| Primary and admin | `GET /healthz` | Local liveness response `ok`; does not probe upstreams. |
| Admin only | `GET /metrics` | Prometheus-scrapable OpenMetrics text. |

Other paths or methods return 404, not 405 (unauthenticated `/v1/*` requests can
be rejected with 401 first).

The gateway collects the **request** body, capped at **32 MiB**. It requires a
top-level object with exactly one string `model` field. `stream` may be boolean,
null, or absent; null/absent means false. Duplicate `model` or `stream` fields
are rejected. Known-field errors do not echo client bodies.

Only the raw JSON value of the top-level `model` field is replaced. Every other
accepted request-body byte is preserved, including whitespace, field order,
unknown fields, nested objects, and string escapes. Examples include `tools`,
`extra_body`, `guided_json`, and `stream_options`. Unknown-field semantics are
left to the upstream; no `stream_options.include_usage` is injected.

Responses are not collected or translated: status and remaining end-to-end
headers are forwarded, with data frames passed through as they arrive. SSE works
without waiting for the final event. HTTP framing/chunk boundaries are managed
by hyper; they are not a byte-for-byte wire-format guarantee. Response JSON
`model` values remain the upstream model, not the public alias.

### Headers

On **both** request and response headers, remove:

- `connection` and any header names nominated by it;
- `keep-alive`, `te`, `trailer`, `transfer-encoding`, `upgrade`, and all `proxy-*`;
- `content-length` (hyper supplies appropriate framing/length).

Additionally on requests:

- Remove `expect`, `openai-organization`, and `openai-project`.
- Replace `Host` with the upstream authority, including its port when present.
- Remove client `Authorization`; replace it with the configured upstream bearer
  token, or leave it absent when no upstream key is configured.

Additionally on responses, remove `set-cookie`, `openai-organization`, and
`openai-project`. Other end-to-end headers pass through. With OTel disabled,
`traceparent` / `tracestate` pass through unchanged unless nominated as
hop-by-hop; with OTel enabled they are replaced by the gateway span context.

## Errors

Gateway-generated handler errors have `Content-Type: application/json` and this
shape:

```json
{"error":{"message":"Model not found","type":"invalid_request_error","param":null,"code":"model_not_found"}}
```

| HTTP status | `error.code` | Cause |
| --- | --- | --- |
| 400 | `invalid_request` | Invalid request object/model/stream, request-body read failure, or invalid upstream request URI. |
| 401 | `invalid_api_key` | Missing, unknown, duplicate, revoked, suspended or expired client key when auth is enabled. |
| 403 | `model_not_allowed` | The key's allowlist does not include the requested model (checked before route lookup). |
| 404 | `model_not_found` | No configured route for the requested model. |
| 404 | `not_found` | Unsupported path or method. |
| 408 | `request_timeout` | Proxied request body exceeded `body_read_timeout_ms`. |
| 413 | `request_too_large` | Request body exceeds 32 MiB; oversized Content-Length can be rejected before reading it. |
| 429 | `insufficient_quota` | The key's status is `budget_exceeded`. |
| 429 | `rate_limit_exceeded` | A rate-limit scope is exhausted; includes `Retry-After`. (Not emitted until limits are enforced.) |
| 500 | `metrics_error` | Admin metrics encoding failed. |
| 502 | `upstream_error` | Upstream connection or response-header protocol failure. |
| 504 | `upstream_timeout` | Upstream response headers did not arrive within `timeout_ms`. |

`error.type` is `authentication_error` for 401, `permission_error` for 403,
`insufficient_quota` / `rate_limit_error` for the two 429 codes,
`upstream_error` for 502/504, and `invalid_request_error` for the other handler
errors above. Upstream HTTP
error responses are passed through, not rewritten into this table's codes.
Malformed HTTP or header-read timeouts are handled by hyper and need not have
this JSON shape. A body failure after headers have been sent terminates the
response; it cannot be replaced with a new JSON error/status.

## Metrics

Scrape the **admin** port, not the primary listener. Histograms expose `_bucket`,
`_sum`, and `_count`. Zero-valued series are pre-registered for `team=""`;
series for a team appear on that team's first request. The registry lives for
the whole process, so counters are never reset by a config change.

| Series | Type | Labels |
| --- | --- | --- |
| `lumewarp_requests_total` | Counter | `model`, `route`, `deployment`, `team`, `status` |
| `lumewarp_request_duration_seconds` | Histogram | `model`, `route`, `deployment` |
| `lumewarp_gateway_overhead_seconds` | Histogram | `route` |
| `lumewarp_request_body_read_seconds` | Histogram | `route` |
| `lumewarp_ttft_seconds` | Histogram | `model`, `deployment` |
| `lumewarp_upstream_errors_total` | Counter | `deployment`, `kind` |
| `lumewarp_in_flight_requests` | Gauge | `deployment` |
| `lumewarp_config_version` | Gauge | None; active snapshot version, standalone YAML is `0`. |
| `lumewarp_config_info` | Gauge | `source="yaml"`, `sha256` (first 12 lowercase hex characters of the YAML file hash); value `1`. |
| `lumewarp_config_apply_failures_total` | Counter | `code` (`base_mismatch`, `invalid_config`, `unsupported_feature`, `persist_failed`, `unspecified`) |
| `lumewarp_config_stream_connected` | Gauge | None; `1` while connected to the control plane. |
| `lumewarp_usage_events_dropped_total` / `_reported_total` / `_failed_total` | Counter | None |
| `lumewarp_tokens_total` | Counter | `model`, `team`, `direction` (`input`/`output`) |
| `lumewarp_spend_total` | Counter | `team`, `project`; USD. |

The config-stream, usage and token/spend series are registered now but only
move once the control-plane stream and usage reporting land.

Label semantics:

- `model` is the **public alias**, never `upstream_model`. Unknown requested
  models use the fixed label `unknown`; other pre-routing rejections and local
  endpoints use `model=""` / `deployment=""`. Arbitrary client model names never
  become labels.
- `route` is `chat_completions`, `completions`, `embeddings`, or `models`; local
  health/admin/other paths use bounded `healthz`, `metrics`, or `other` labels.
- `deployment` is the configured deployment name.
- `team` is the caller key's team id; `""` for anonymous callers, team-less
  keys, and requests rejected before authentication succeeds (401, and 429
  `insufficient_quota`).
- `status` uses `200`, `204`, `400`, `401`, `403`, `404`, `408`, `413`, `429`, `499`,
  `500`, `502`, `503`, `504`, or fallback `other`. A handler dropped **before a
  response exists** records 499; this is a metric label, not a sent HTTP response.
  After headers, a dropped/broken body retains the response's HTTP status.
- `kind` is `connect`, `protocol` (other upstream request failures), `timeout`
  (response headers), or `stream` (upstream body failure, including non-stream
  response bodies). A truncated 200 response increments `kind="stream"` while
  its request counter still records 200.
- No API-key labels exist. In-flight gauges read the process-lifetime deployment
  counters at scrape time.

### Timing definitions

These follow the doc comments in [`metrics.rs`](crates/lumewarp-gateway/src/metrics.rs):

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
direct end-to-end latency distributions; see [bench/README.md](../bench/README.md).
Requests rejected before hyper constructs a handler request (for example an
incomplete HTTP header) do not have endpoint request metrics.

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

Use an HTTP/protobuf collector endpoint, not a gRPC-only endpoint. OTel-enabled
request spans are INFO with `route`, `model`, `deployment`, `status`, and `stream`
attributes; the OTel layer accepts only `lumewarp` targets at INFO or above and
disables code-location attributes. It does not enable/export dependency DEBUG /
TRACE events. Explicit console verbosity is still controlled by `RUST_LOG`.

Incoming W3C `traceparent` / `tracestate` establish the parent; the gateway
injects its span context upstream. With OTel disabled, there is no extraction /
injection, just header passthrough. Exporter creation and export work are absent
when the enable switch is unset.

## Shutdown and current limitations

On SIGTERM (Unix) or Ctrl-C, both listeners stop accepting, active HTTP
connections begin graceful shutdown, and in-flight responses can drain for up
to `shutdown_grace_ms`. At the deadline remaining connection tasks are aborted.
If OTel is enabled, exporter shutdown/flush follows; the SDK's shutdown timeout
is 5 seconds. Exporter shutdown failures only warn and do not change an otherwise
successful exit code.

For Kubernetes, set `terminationGracePeriodSeconds` **above**
`shutdown_grace_ms / 1000 + 5`, with extra margin for scheduling and any lifecycle
hooks. The default drain alone is 25 seconds; a 30-second pod grace period leaves
no margin when an exporter stalls. Also override the default loopback admin bind
if Prometheus scrapes from outside the pod.

The gateway does not yet implement:

- application-level retries, fallback chains, circuit breaking, active upstream
  health checks, or latency-aware routing;
- hot reload, gRPC config delivery, or persisted snapshots; restart to change
  configuration or environment-resolved credentials;
- token, TPOT, spend, circuit, vLLM-load, or MCP metrics; upstream usage fields
  pass through without gateway accounting;
- an idle timeout on upstream response bodies after headers (including
  non-stream bodies); `timeout_ms` stops at response headers;
- rate-limit enforcement, usage reporting, the control-plane config stream and
  on-disk snapshot boot (P1 work in progress), JWT, or client TLS termination.

See the [architecture](../docs/architecture.md) for planned capabilities, not a
statement that those features ship today.
