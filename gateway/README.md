# Lumewarp gateway

A Rust HTTP/1.1 gateway for OpenAI-compatible HTTP/HTTPS upstreams, including
vLLM and OpenAI. It compiles only from a protobuf config `Snapshot`
(`proto/lumewarp/config/v1/snapshot.proto`). No control plane, database, or
Python is in the request path.

Two modes are available:

- **standalone** (`--config`): read YAML at startup and convert it to a snapshot.
- **control-plane** (`--control-plane`): boot from a cached snapshot, follow the
  versioned gRPC config stream and report usage back to the control plane.

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
models:
  - name: mock-local
    upstream: local
    upstream_model: mock
routers:
  - name: demo
    members:
      - {model: mock-local}
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
router will be used.

## Control-plane mode

Standalone mode (`--config`) reads the whole configuration from YAML. In
control-plane mode the gateway follows the control plane's config stream and
reports usage to the same endpoint:

```sh
# Issue the token for this gateway id (`bootstrap --gateway-id gw-1`, or create it
# for gw-1 in the Admin UI); the control plane binds the token to that id and stores
# only its SHA-256 hash.
export LUMEWARP_GATEWAY_TOKEN='lwg-...'

lumewarp \
  --control-plane http://127.0.0.1:50051 \
  --gateway-token-env LUMEWARP_GATEWAY_TOKEN \
  --state-dir /tmp/lumewarp-state \
  --scope default \
  --gateway-id gw-1 \
  --local-config /etc/lumewarp/local.yaml
```

The token is read from the named environment variable, sent as
`authorization: Bearer <token>` on the config stream and usage reports, and never
written to disk. `--gateway-id` defaults to `gateway` for local runs, but the
control plane binds each token to one gateway id: `Hello.gateway_id` must equal
the id the token was issued for. The Helm chart requires a non-empty
`gateway.gatewayId` (default `gw-1`) and runs one replica; multi-replica gateways
are deferred, so deploy one release per gateway identity instead. The
chart runs the pod as UID/GID 65532 with `fsGroup: 65532` so the mounted state
directory is writable. `--local-config` is a YAML file with only the `server:`
block described below (listener, admin listener, timeouts); upstreams, routers,
teams and keys (including `server.api_keys`) are ignored in this mode.

### Flags

| Flag | Environment variable | Default | Meaning |
| --- | --- | --- | --- |
| `--config`, `-c` | `LUMEWARP_CONFIG` | required for standalone | Standalone YAML. Conflicts with every control-plane flag. |
| `--control-plane URL` | `LUMEWARP_CONTROL_PLANE` | unset | Control-plane gRPC URL (`http://` or `https://`). Enables control-plane mode and requires the three flags below. |
| `--gateway-token-env NAME` | none | required in control-plane mode | Name of the environment variable that holds the gateway token. |
| `--state-dir DIR` | none | required in control-plane mode | Directory for the last good snapshot. It is created `0700` on the first successful persist. |
| `--scope SCOPE` | none | required in control-plane mode | Config scope this gateway serves. Must match the control plane and the cached snapshot. |
| `--gateway-id ID` | none | `gateway` | Identity sent in the stream `Hello`; must match the id the token was issued for and is shown in the control-plane gateway list. The Helm chart sets `gateway.gatewayId`. |
| `--local-config FILE` | none | unset | YAML with only `server:` listener/timeout settings. Without it the defaults from the YAML reference apply. |

A minimal local config only needs the listeners:

```yaml
server:
  listen: 0.0.0.0:4000
  admin_listen: 0.0.0.0:9464
```

The admin listener stays unauthenticated; keep it private (see the auth section).

### Boot and readiness

- On start the gateway reads `<state-dir>/snapshot.pb`. The file starts with a
  format marker, has a trailing SHA-256, is size-capped and must carry the
  configured scope; a corrupt, truncated, wrong-scope or older-format file is
  never served and never offered as a delta base. The next full snapshot
  replaces it.
- With a valid snapshot it serves `/v1/*` immediately and connects to the
  control plane in the background. Without one it stays not ready: `/v1/*`
  returns 503 `not_ready` **before authentication**, the admin `/readyz` returns
  503, and the process never falls back to an open router.
- Control-plane mode always requires a key: with zero keys in the snapshot every
  `/v1/*` request is 401. A revoked, suspended or expired key is 401; a key over
  budget is 429 `insufficient_quota` (the control plane flips its status).
- The stream sends `Hello` with the cached version, scope and digest, then
  applies at most one update at a time: apply to a private copy, validate,
  compile, persist (`snapshot.pb.tmp` → fsync → rename → fsync directory), swap
  with ArcSwap, then ACK. A persist failure NACKs `PERSIST_FAILED` and keeps
  serving the previous version. ACK therefore means "active and durable": a
  restart boots into that version even while the control plane is down.
- Reconnects use exponential backoff from 0.5 s to 30 s with full jitter.
  `lumewarp_config_stream_connected`, `lumewarp_config_version` and
  `lumewarp_config_apply_failures_total{code}` expose the stream state.

### Usage reporting

Control-plane mode also enables usage reporting to the same URL with the same
token. Each finalized proxied request (including a rejection for a known key)
produces one event with a UUIDv7 `request_id`, returned to the client in the
`x-lumewarp-request-id` response header: tokens, status, config version, cost
computed from `models[].price`, the name the client called (`requested`), the
model that served it (`model`), how it got there (`alias`, `router`, `fallback`),
what the final attempt used (`upstream`, `upstream_model`) and `attempts`; see
[the table](../docs/config-protocol.md). Events
enter a bounded in-memory queue (100,000) and are reported every 1 s or every
500 events. Failed batches retry from the queue with backoff; when the queue is
full new events are dropped and `lumewarp_usage_events_dropped_total`
increments. There is no disk spool: a gateway crash loses queued events, and a
long control-plane outage eventually drops them. On shutdown the reporter
flushes for up to 5 s.

For streams the gateway injects `stream_options.include_usage: true` when the
upstream's `stream_usage_mode` is `include_usage` (the default) and the client
did not ask for usage; the extra usage-only chunk is stripped before it reaches
the client. `passthrough` disables the injection for upstreams that do not
support it. Non-stream usage is parsed from a bounded copy of the response body.

## YAML reference

The complete schema is in
[`lumewarp-config`](crates/lumewarp-config/src/lib.rs). Unknown YAML fields are
rejected at every object level. The YAML is converted to a version-0 snapshot
and validated exactly like a control-plane snapshot: names/ids are unique within
each upstream, model, router, team and key list; references must resolve;
key hashes must be unique. Environment-variable substitution inside arbitrary
YAML strings is not implemented. Older files load unchanged.

| Top-level field | Default | Meaning |
| --- | --- | --- |
| `server` | Defaults below | Local listener, authentication shorthand, and shutdown settings. |
| `upstreams` | `[]` | List of upstream objects. |
| `models` | `[]` | List of models binding an upstream to a model. |
| `routers` | `[]` | List of public model aliases and their model choices. |
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
| `server.retry_budget_percent` | `20` | Process-wide retry allowance, `0..=20`; `0` disables retries. Each client request on a router **with a `retry` policy** funds this percentage of one retry, so retries stay at about this share of the traffic that may retry; routers without a retry policy neither fund nor touch the budget (no shared atomic on their request path). The token bucket holds about three retries (the cap is approximate under concurrent deposits) and restores that minimum after ten idle seconds. |

**Auth mode.** If neither `server.api_keys` nor `keys` configures any key, the
gateway is open: every `/v1/*` request is anonymous and `Authorization` is
ignored. As soon as one key exists, every `/v1/*` request (including
`/v1/models`) needs a valid key. Control-plane mode always requires keys, so
an empty key set there rejects every `/v1/*` request.

Send one `Authorization: Bearer <key>` header. The gateway hashes the key with
SHA-256 and looks it up (one hash, one map lookup). Bearer scheme matching is
case-insensitive; leading whitespace before the token is trimmed. Duplicate
Authorization headers are rejected. Expired, revoked and suspended keys are
rejected like unknown keys (401) before a `budget_exceeded` status (429) is
considered. The model allowlist is checked right after the request body's
`model` is parsed and before any router lookup, so a key that may not use a model
gets 403 whether or not the alias exists. `/v1/models` lists only the aliases
the key may call.

Control-plane snapshots may also carry `previous_key_hash` /
`previous_key_expires_at_unix_ms` during a rotation grace (capability
`rotation_grace`). It maps to the same key context and expires on its own, so a
gateway booting from its persisted snapshot stops accepting it at that deadline
even if the control plane is unreachable. Standalone YAML has no such field.

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
| `upstreams[].stream_usage_mode` | `include_usage` | `include_usage` (the gateway may force `stream_options.include_usage` upstream for metering and strips the extra chunk if the client did not ask) or `passthrough` (upstream does not support it; no injection). |
| `upstreams[].health_check` | Absent | Active GET probe: `{path, interval_ms, timeout_ms, unhealthy_threshold, healthy_threshold}`. `path` defaults to `/models`, appended to `base_url`. Interval must be at least 1000 ms; timeout must be positive and less than interval. Thresholds of zero mean 3 failures / 1 success. Uses the existing upstream client and credentials. |
| `upstreams[].circuit_breaker` | Absent | Per-model `{failure_threshold, open_ms}`; zero values mean 5 consecutive retryable failures and 30000 ms open. One half-open trial is allowed after the open interval. Only that trial's result closes or re-opens a half-open breaker; late results of requests admitted while closed never move an open/half-open breaker or extend its deadline. Removing `circuit_breaker` from an upstream resets its models' breakers to closed (gauge 0) when the snapshot is activated. |
| `upstreams[].metrics_url` | Empty | Prometheus endpoint scraped every second by `vllm_load` routers. Empty means `base_url` with a trailing `/v1` removed plus `/metrics` (`http://vllm:8000/v1` -> `http://vllm:8000/metrics`). Must be absolute http(s) without userinfo. The upstream bearer token is sent only when the metrics URL has the same host and port as `base_url`. Setting it requires capability `vllm_load`. |

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

### Models

| Field | Default | Meaning |
| --- | --- | --- |
| `models[].name` | Required | Unique name across models, aliases and routers; callable directly, listed by `/v1/models`, and used in metrics. A direct call is one attempt, with no retry and no fallback. `unknown` is reserved. |
| `models[].upstream` | Required | Name of an existing upstream. |
| `models[].upstream_model` | Required | Model string substituted for the name the client called. |
| `models[].price` | Absent | `{input_usd_per_mtok, output_usd_per_mtok}`: USD per one million tokens as decimals, e.g. `"0.15"` (quoted or plain). Parsed from the YAML text, never through floating point, and converted exactly to integer nano-USD; at most 9 fractional digits, no exponent or sign. Absent means the price is unknown (usage is metered, cost reported as unknown); explicit zeros mean free. |

### Aliases

| Field | Default | Meaning |
| --- | --- | --- |
| `aliases[].name` | Required | Unique name across models, aliases and routers; callable and listed by `/v1/models`. `unknown` is reserved. |
| `aliases[].target` | Required | An existing model or router, never another alias. The alias shares its pool: same retries, fallbacks and breakers. |

### Routers

| Field | Default | Meaning |
| --- | --- | --- |
| `routers[].name` | Required | Unique name across models, aliases and routers; callable and listed by `/v1/models`. `unknown` is reserved. |
| `routers[].members` | Required without `traffic_split` | Nonempty list of `{model, weight, params}` on distinct existing models. `weight` defaults to `1`, is an unsigned 32-bit relative weight and belongs to this router, so a model can weigh differently in two routers. Zero is allowed, but a weighted router must have positive total weight; other built-in strategies ignore it. `params` is an object read only by a strategy that declares it. Ignored (may be empty) when `traffic_split` is set. |
| `routers[].strategy` | `weighted` | `weighted`, `least_in_flight`, `lowest_latency`, `vllm_load` or a compiled-in strategy; see "Load-aware routing". With `traffic_split` it picks the model inside the chosen split. An unknown name is refused. |
| `routers[].params` | `{}` | Object for a compiled-in strategy, at most 16 KiB. Built-in strategies take none. |
| `routers[].retry` | Absent (one attempt) | `{max_attempts, retry_on_status, total_timeout_ms}`. An empty object enables two total attempts. `max_attempts` includes the first and is at most 5; `retry_on_status` defaults to `[429, 502, 503, 504]` and accepts only 400..599. `total_timeout_ms: 0` leaves only each upstream's header timeout. |
| `routers[].fallbacks` | `[]` | Existing model names, in order; never routers or aliases. Uses the router's attempt/deadline budget. Configure a retry budget of at least two attempts for failure-triggered fallback. |
| `routers[].traffic_split` | `[]` | Canary split: `[{name, members, weight}]`. A request picks a split with probability `weight / sum(weights)`, then a model inside it with `strategy`. At most 16 splits; names unique, 1..=32 of `[A-Za-z0-9_.-]` (they are the `split` metric label); each split has a nonempty list of members (`{model, weight, params}`) on distinct existing models; weights must sum to more than 0 (a single split may have weight 0: it only receives retries). |
| `routers[].sticky` | `none` | `none`, `header` (hash of the `sticky_header` value; requests without the header are not sticky), `team` (the key's team id) or `key` (the key id). Anonymous or team-less callers are not sticky for `team`/`key`. Requires `traffic_split`. |
| `routers[].sticky_header` | Empty | Request header hashed for `sticky: header`; required there, rejected otherwise. |

### Retry and health behavior

Only connect failures, timeouts before headers, and configured statuses can cause
another attempt. Streams **never** retry after upstream headers, even before the
first event; no response-body error is retried. The buffered request body is
reused; only model/usage splicing is repeated. Retry-enabled requests retain one
copy of the request headers for replay; routers without retries move their headers
directly and have no replay allocation.

An attempt prefers an untried, healthy model with a closed (or available
half-open) circuit, using the router's strategy. It then tries fallbacks in order
with their own strategies. Already-tried models are reused only after the
untried candidates are exhausted. `Retry-After` on 429/503 accepts integer seconds
or an HTTP date and cools down that model across requests, for at most 60 s
and never longer than its breaker's `open_ms` when one is configured. Retries skip
cooling models rather than sleeping, including when the cooldown exceeds
the remaining total timeout. A new client request may still make its first
attempt there if necessary. If later transport attempts fail, the last received
upstream response is returned, and the request sample, usage capture and
in-flight reservation belong to the attempt that produced it (the later failed
attempt's reservation is released).

An attempt cut short by the router's `total_timeout_ms` (the deadline, not the
upstream's own `timeout_ms`, ended it) is not a model failure: it records no
breaker failure (an interrupted half-open trial is abandoned, not failed) and no
latency penalty. It still counts as `outcome="timeout"` in
`lumewarp_upstream_attempts_total` and `kind="timeout"` in
`lumewarp_upstream_errors_total`, and the client gets 504.

Breaker state is keyed by model name and survives snapshot swaps. Healthy
fallbacks can be selected immediately when every primary is unhealthy/open.
If **every** candidate is unhealthy/open, routing deliberately fails open and
tries a model anyway; it does not turn a total backend outage into a
permanent local rejection. An already-running half-open trial is never duplicated:
fail-open selection skips that model and uses another candidate (503 if
there is none). `outcome="circuit_open"` counts each open/half-open candidate at
most once per client request, labelled with the client's model (also for
fallback models). Active probes are diffed on accepted snapshot swaps by
upstream, URL, settings and credentials: unchanged probes keep running with
their consecutive failure/success counts and schedule (key-only deltas do not
reset or re-probe them); only removed or changed probes are stopped and only new
or changed ones started. Probes never increment request/attempt/usage metrics.

### Load-aware routing and traffic splits

- `lowest_latency`: each model keeps an EWMA of time to response headers
  (weight 1/5 per sample) of successful attempts. Connect errors, timeouts and
  5xx/429 responses add a penalty sample of `max(elapsed, 2 x current, 1 s)`;
  other 4xx are ignored. A pick is power-of-two-choices on
  `EWMA x (in_flight + 1)`, so concurrent load spreads roughly inversely to
  latency instead of piling onto the fastest model. A model without
  samples costs 0 (it is explored first), and 5% of picks are uniformly random
  so a model that was slow once is re-measured. The EWMA is per model
  across endpoints (a non-stream completion's headers arrive after generation,
  a stream's after the first token), keyed by name and kept across swaps.
- `vllm_load`: one background task per upstream used by a `vllm_load` router
  GETs its metrics URL every second (900 ms timeout, 8 MiB cap) and reads
  `vllm:num_requests_waiting` and `vllm:kv_cache_usage_perc` (or the older
  `vllm:gpu_cache_usage_perc`). Series whose `model_name` label equals the
  model's `upstream_model` are used when present, else all series; queue
  depth is summed and KV usage maximized across engines. A pick is
  power-of-two-choices on `(waiting, KV usage in 5% buckets, in_flight)`. If
  either candidate's last scrape failed or is older than 3 s, the two are
  compared on in-flight requests only. A failed scrape counts in
  `lumewarp_vllm_scrape_failures_total` and invalidates the values at once.
  Scraping never happens on the request path; on accepted snapshot swaps
  scrapers are diffed like health probes (unchanged ones keep running).
- `traffic_split`: the split is chosen once per request (counted in
  `lumewarp_split_requests_total`). With `sticky`, the input is hashed with a
  fixed-seed 64-bit hash (FNV-1a + SplitMix64 finalizer) and mapped onto the
  cumulative weights, so the same session/team/key keeps its split across
  requests, gateway restarts and replicas while the weights stay unchanged
  (changing weights moves only part of the users). Without sticky input the
  split is drawn at random. Retries first try the other models of the
  chosen split, then the other splits in declaration order, then fallbacks.
  If every model of the chosen split is unhealthy/open, the request moves
  to another split before any fallback.

Hot-path cost: picks read relaxed atomics only (no locks, no allocation; the
regression tests assert zero allocations for warm `lowest_latency`, `vllm_load`
and sticky-split picks and retries). `lowest_latency` adds one clock read and one
CAS per attempt, only for models behind such a router. The EWMA gauge is
computed when `/metrics` is scraped, not per request.

Each client request records one request sample and one usage capture, attributed
to its final model. Abandoned attempts release their in-flight reservations.
Response `model`: a successful response carries the name the client called, whichever model
served it, retries and fallbacks included. The top-level JSON `model` (or, for SSE, each event's
`data` JSON `model`) is replaced by a streaming scanner that buffers nothing, so a response of
any size passes through without a cap, and `Content-Length` gives way to chunked encoding.
When the called name equals the serving model's `upstream_model`, the scanner is skipped; an
upstream that answers with a more specific name (`gpt-4o` answered as `gpt-4o-2024-08-06`) is
then seen as is. Error responses are never rewritten. A response the upstream compresses despite
the removed `Accept-Encoding` is forwarded as is and counted in
`lumewarp_response_model_rewrite_compressed_total`. The full decision table is in the
[config protocol](../docs/config-protocol.md).

### Teams

| Field | Default | Meaning |
| --- | --- | --- |
| `teams[].id` | Required | Unique, stable team id. It is the `team` metric label value, so keep the number of teams bounded. |
| `teams[].name` | `id` | Display name. |
| `teams[].rate_limits` | Absent | Limits shared by **all** keys of the team (scope `team:<id>`); see rate limits below. |

### Keys

| Field | Default | Meaning |
| --- | --- | --- |
| `keys[].key_id` | Required | Unique, public, non-secret id (appears in usage events, never in metrics). |
| `keys[].key_sha256` | Required | SHA-256 of the full secret key, 64 lowercase hex characters; unique across all keys including `server.api_keys`. |
| `keys[].team_id` | `""` | Existing team id, or empty for no team (`team=""` in metrics). |
| `keys[].status` | `active` | `active`, `revoked`, `suspended` (401) or `budget_exceeded` (429 `insufficient_quota`). |
| `keys[].expires_at_unix_ms` | `0` | Expiry in Unix milliseconds; `0` never expires. Expired keys get 401. |
| `keys[].allow_all_models` | `false` | Allow every name, present and future. |
| `keys[].allowed_models` | `[]` | Names of any kind the key may call when `allow_all_models` is false. A grant covers exactly that name: a router's members and an alias's target need their own. **Fail-closed:** a key with neither may call nothing. |
| `keys[].rate_limits` | Absent | Limits for this key alone (scope `key:<key_id>`). |

`rate_limits` objects have `requests_per_minute`, `tokens_per_minute` and
`max_parallel_requests` (each `0`/absent = unlimited). They are compiled into
per-key `RateLimitScope`s (team scope first, then key scope); limiter state is
keyed by scope id, so it survives reloads, and it is local to one gateway
instance (N instances allow up to N times the limit). A request acquires all of
its scopes or none: `requests_per_minute` and `max_parallel_requests` are checked
before forwarding (`max_parallel` is held until the response body ends, errors or
is dropped); `tokens_per_minute` is charged after usage is known and blocks later
requests in the same window. A rejection is 429 `rate_limit_exceeded` with
`Retry-After`.

### Routing

`weighted` samples precomputed cumulative weights. `least_in_flight` selects the
smallest atomic in-flight count, scanning from a random offset to spread ties.
This is an approximate concurrent decision, not a global scheduling lock.
Reservations last until the upstream response body ends/errors or is dropped,
or the request fails/is cancelled before headers. Counters are keyed by
model name for the life of the process: every router holding the same
model shares one, and a recompiled snapshot keeps counting requests still
running on the previous one.

## Endpoints and passthrough

| Listener | Method and path | Behavior |
| --- | --- | --- |
| Primary | `POST /v1/chat/completions` | Proxy to the selected model; stream responses frame by frame. |
| Primary | `POST /v1/completions` | Same routing and passthrough. |
| Primary | `POST /v1/embeddings` | Same routing and passthrough. |
| Primary | `GET /v1/models` | Local OpenAI-shaped list of every name the caller may call (models, aliases, routers), sorted; not upstream discovery. |
| Primary and admin | `GET /healthz` | Local liveness response `ok`; does not probe upstreams. |
| Admin only | `GET /readyz` | `ready` (200) once a router is loaded; 503 JSON `not_ready` in control-plane mode before the first snapshot is applied. |
| Admin only | `GET /metrics` | Prometheus-scrapable OpenMetrics text. |

Other paths or methods return 404, not 405 (unauthenticated `/v1/*` requests can
be rejected with 401 first).

The gateway collects the **request** body, capped at **32 MiB**. It requires a
top-level object with exactly one string `model` field. `stream` may be boolean,
null, or absent; null/absent means false. Duplicate `model` or `stream` fields
are rejected. Known-field errors do not echo client bodies.

The gateway rewrites only what it must: the raw JSON value of the top-level
`model` field, and `stream_options.include_usage` for an eligible stream when
the client did not ask for usage (see control-plane mode). Every other accepted
request-body byte is preserved, including whitespace, field order, unknown
fields, nested objects, and string escapes. Examples include `tools`,
`extra_body`, `guided_json`, and the rest of `stream_options`. Unknown-field
semantics are left to the upstream.

Responses are not translated: status and remaining end-to-end headers are
forwarded, with data frames passed through as they arrive. The only removed SSE
event is the usage-only chunk the gateway itself requested, and only when the
client did not ask for it. SSE works without waiting for the final event. HTTP
framing/chunk boundaries are managed by hyper; they are not a byte-for-byte
wire-format guarantee. Response JSON `model` values remain the upstream model,
not the name the client called, except for successful fallback responses (see "Load-aware routing and traffic splits" above).

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
| 403 | `model_not_allowed` | The key's allowlist does not include the requested model (checked before router lookup). |
| 404 | `model_not_found` | No model, alias or router has the requested name. |
| 404 | `not_found` | Unsupported path or method. |
| 408 | `request_timeout` | Proxied request body exceeded `body_read_timeout_ms`. |
| 413 | `request_too_large` | Request body exceeds 32 MiB; oversized Content-Length can be rejected before reading it. |
| 429 | `insufficient_quota` | The key's status is `budget_exceeded`. |
| 429 | `rate_limit_exceeded` | A rate-limit scope is exhausted; includes `Retry-After`. |
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
| `lumewarp_requests_total` | Counter | `requested`, `endpoint`, `model`, `team`, `status` |
| `lumewarp_request_duration_seconds` | Histogram | `requested`, `endpoint`, `model` |
| `lumewarp_gateway_overhead_seconds` | Histogram | `endpoint` |
| `lumewarp_request_body_read_seconds` | Histogram | `endpoint` |
| `lumewarp_ttft_seconds` | Histogram | `requested`, `model` |
| `lumewarp_upstream_errors_total` | Counter | `model`, `kind` |
| `lumewarp_upstream_attempts_total` | Counter | `requested`, `model`, `outcome`: `success`, `retryable_status`, `connect_error`, `timeout`, `circuit_open`, `non_retryable` |
| `lumewarp_fallbacks_total` | Counter | `router`, `model` |
| `lumewarp_retries_budget_exhausted_total` | Counter | `router` |
| `lumewarp_upstream_circuit_state` | Gauge | `model`; 0 closed, 1 half-open, 2 open |
| `lumewarp_upstream_healthy` | Gauge | `upstream`; 1 healthy, 0 unhealthy |
| `lumewarp_in_flight_requests` | Gauge | `model` |
| `lumewarp_split_requests_total` | Counter | `router`, `split`; requests assigned to a traffic split. |
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

The config-stream series move in control-plane mode; `lumewarp_config_info` is
set only in standalone mode. Usage event counters and queue depth move in
control-plane mode. Token and spend series move in both modes whenever the
upstream reports usage; control-plane mode forces stream usage where the
upstream supports it.

Label semantics:

- `requested` is the **name the client called**, of any kind, never
  `upstream_model`. Unknown requested names use the fixed label `unknown`; other pre-routing rejections and local
  endpoints use `requested=""` / `model=""`. Arbitrary client names never become
  labels; they are kept only in the usage event.
- `endpoint` is `chat_completions`, `completions`, `embeddings`, or `models`; local
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
- `kind` is `connect`, `protocol` (other upstream request failures), `timeout`
  (response headers), or `stream` (upstream body failure, including non-stream
  response bodies). A truncated 200 response increments `kind="stream"` while
  its request counter still records 200.
- No API-key labels exist. In-flight gauges read the process-lifetime model
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

Use an HTTP/protobuf collector endpoint, not a gRPC-only endpoint.

The gateway writes no access log: OTel is where a single request is looked up,
starting from the `x-lumewarp-request-id` response header. The `request` span has
`request_id`, `endpoint`, `team_id`, `key_id`, `requested`, `alias`, `router`,
`model`, `upstream`, `upstream_model`, `attempts`, `fallback`, `config_version`,
`status` and `stream`. Each `upstream_attempt` child has `model`, `upstream`,
`upstream_model`, `attempt`, `fallback`, `outcome` and `upstream_status`. Prompts,
responses and keys are never span attributes; `key_id` is the public id.

Every `/v1/*` response has the header, including a 503 `not_ready` before any
snapshot is loaded (`config_version` 0 and no `key_id`; there is no usage event,
since the key is unknown). A request that made no upstream attempt has `attempts`
0 and `fallback` false. A client that goes away while an attempt waits for upstream
headers leaves that attempt's `upstream`, `upstream_model`, `attempts` and
`fallback`, matching the usage event. A 403 span has the `alias` and `router` of a
known name, which the response does not reveal. Authentication rejections have no
`requested`: the body is not read for them.

This only works under these conditions:

- Without `OTEL_EXPORTER_OTLP_ENDPOINT` there is no record at all.
- Sampling below 100%, or a full export queue, loses records, and the gateway
  neither logs nor counts that loss. A request produces at least 2 spans, so at
  1000 RPS the default queue of 2048 spans holds about one second. Measured at
  1000 RPS for 30 s (60000 spans): with a collector answering at once the defaults
  lost nothing; with a collector taking 1 s per export the defaults delivered 17408
  (71% lost), while `OTEL_BSP_MAX_QUEUE_SIZE=65536`, `OTEL_BSP_SCHEDULE_DELAY=200`
  and `OTEL_BSP_MAX_EXPORT_BATCH_SIZE=2048` delivered all 60000. To use OTel as an
  access log keep the 100% default and size the queue for your rate and for how long
  the collector may stall.
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

## Shutdown and current limitations

On SIGTERM (Unix) or Ctrl-C, both listeners stop accepting, active HTTP
connections begin graceful shutdown, and in-flight responses can drain for up
to `shutdown_grace_ms`. At the deadline remaining connection tasks are aborted.
In control-plane mode the config stream task is then aborted and the usage
reporter flushes what is queued for up to 5 seconds. If OTel is enabled,
exporter shutdown/flush follows; the SDK's shutdown timeout is 5 seconds.
Exporter shutdown failures only warn and do not change an otherwise successful
exit code.

For Kubernetes, set `terminationGracePeriodSeconds` **above**
`shutdown_grace_ms / 1000 + 5`, with extra margin for scheduling and any lifecycle
hooks. The default drain alone is 25 seconds; a 30-second pod grace period leaves
no margin when an exporter stalls. Also override the default loopback admin bind
if Prometheus scrapes from outside the pod.

Listener and timeout settings are read at startup and do not change without a
restart; in control-plane mode only the config snapshot is hot-swapped. The
gateway does not yet implement:

- canary or percentage config rollout; publishing and rollback are whole-scope
  versions driven by the control plane;
- Redis-backed or global rate limits (limits are local per gateway instance);
  budgets are eventual and in-flight streams are not cut; usage events are lost
  when the bounded queue is full or the gateway crashes;
- TPOT or MCP metrics; JWT/mTLS client authentication and MCP/A2A proxying;
- an idle timeout on upstream response bodies after headers (including
  non-stream bodies); `timeout_ms` stops at response headers;
- client TLS termination (terminate it in front of the gateway).

See the [architecture](../docs/architecture.md) for planned capabilities, not a
statement that those features ship today.
