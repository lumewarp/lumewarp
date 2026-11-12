# Gateway YAML

The file a standalone gateway reads with `--config`. In control-plane mode the same schema
arrives as a snapshot and only the `server:` block is read from `--local-config`.

The complete schema is in
[`lumewarp-config`](https://github.com/lumewarp/lumewarp/blob/main/gateway/crates/lumewarp-config/src/lib.rs). Unknown YAML fields are
rejected at every object level. The YAML is converted to a version-0 snapshot
and validated exactly like a control-plane snapshot: names/ids are unique within
each upstream, model, router, team and key list; references must resolve;
key hashes must be unique. Environment-variable substitution inside arbitrary
YAML strings is not implemented. Every model must explicitly declare its `type`.

| Top-level field | Default | Meaning |
| --- | --- | --- |
| `server` | Defaults below | Local listener, authentication shorthand, and shutdown settings. |
| `upstreams` | `[]` | List of upstream objects. |
| `models` | `[]` | List of models binding an upstream to a model. |
| `routers` | `[]` | List of public model aliases and their model choices. |
| `teams` | `[]` | Teams: the bounded `team` metric label and shared rate-limit scopes. |
| `keys` | `[]` | Virtual keys with status, expiry, model allowlist and rate limits. |

## Server

| Field | Default | Meaning |
| --- | --- | --- |
| `server.listen` | `0.0.0.0:4000` | Primary HTTP listener, an IP socket address (`IP:port`, or `[IPv6]:port`). |
| `server.admin_listen` | `127.0.0.1:9464` | Separate HTTP admin listener. Must differ from `listen`. |
| `server.api_keys` | `[]` | Shorthand for team-less keys that may call every model: client bearer-key SHA-256 hashes, exactly 64 lowercase hexadecimal characters each. Duplicates collapse into one key. |
| `server.shutdown_grace_ms` | `25000` | Maximum connection drain time after shutdown begins; must be greater than zero. |
| `server.header_read_timeout_ms` | `30000` | HTTP request-header read timeout on both listeners; must be greater than zero. |
| `server.body_read_timeout_ms` | `30000` | Total time allowed to collect the whole proxied upload, including multipart transcription uploads, not an idle timeout; must be greater than zero. Slow uploads may require increasing this value. |
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

## Upstreams

| Field | Default | Meaning |
| --- | --- | --- |
| `upstreams[].name` | Required | Unique upstream name. |
| `upstreams[].kind` | Required | `openai`, `vllm`, or `openai_compatible`. All use the same OpenAI-compatible passthrough; there is no provider translation. |
| `upstreams[].base_url` | Required | Absolute HTTP/HTTPS base URI, with an authority and without userinfo or a query. Include `/v1` when that is the upstream API prefix. |
| `upstreams[].api_key_env` | Absent | Name of the environment variable containing the raw upstream bearer token, **not** the token or a client-key hash. |
| `upstreams[].timeout_ms` | `600000` | Unsigned 32-bit millisecond timeout from issuing the upstream request through receipt of response headers, including connection establishment. Does not limit response-body or stream lifetime. (A snapshot value of 0 also means the default.) |
| `upstreams[].stream_usage_mode` | `include_usage` | `include_usage` (the gateway may force `stream_options.include_usage` upstream for metering and strips the extra chunk if the client did not ask) or `passthrough` (upstream does not support it; no injection). |
| `upstreams[].health_check` | Absent | Active GET probe: `{url, interval_ms, timeout_ms, unhealthy_threshold, healthy_threshold}`. `url` is an absolute http(s) URL without userinfo; empty means `base_url` without a trailing `/v1` plus `/health`, for every kind. The upstream bearer token is sent only when the URL has the same scheme, host and port as `base_url`. Interval must be at least 1000 ms; timeout must be positive and less than interval. Thresholds of zero mean 3 failures / 1 success. Uses the existing upstream client. In control-plane mode, state changes are reported to the control plane (`HealthReport`) for display. |
| `upstreams[].circuit_breaker` | Absent | Per-model `{failure_threshold, open_ms}`; zero values mean 5 consecutive retryable failures and 30000 ms open. One half-open trial is allowed after the open interval. Only that trial's result closes or re-opens a half-open breaker; late results of requests admitted while closed never move an open/half-open breaker or extend its deadline. Removing `circuit_breaker` from an upstream resets its models' breakers to closed (gauge 0) when the snapshot is activated. |
| `upstreams[].metrics_url` | Empty | Prometheus endpoint scraped every second by `vllm_load` routers. Empty means `base_url` with a trailing `/v1` removed plus `/metrics` (`http://vllm:8000/v1` -> `http://vllm:8000/metrics`). Must be absolute http(s) without userinfo. The upstream bearer token is sent only when the metrics URL has the same scheme, host and port as `base_url`. Setting it requires capability `vllm_load`. |

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

## Models

| Field | Default | Meaning |
| --- | --- | --- |
| `models[].type` | Required | `llm`, `embedding`, `transcription` or `decision`; aliases and routers inherit it, and every router member and fallback must share one type. |
| `models[].name` | Required | Unique name across models, aliases and routers; callable directly, listed by `/v1/models`, and used in metrics. A direct call is one attempt, with no retry and no fallback. `unknown` is reserved. |
| `models[].upstream` | Required | Name of an existing upstream. |
| `models[].upstream_model` | Required | Model string substituted for the name the client called. |
| `models[].price` | Absent | `{input_usd_per_mtok, output_usd_per_mtok, audio_usd_per_second}`: token rates are USD per one million tokens; audio is USD per second (transcription only), embedding output price must be zero. Prices are decimals, e.g. `"0.15"` (quoted or plain). Parsed from the YAML text, never through floating point, and converted exactly to integer nano-USD; at most 9 fractional digits, no exponent or sign. Absent means the price is unknown (usage is metered, cost reported as unknown); only an explicitly all-zero Price is free. For a non-all-zero Price, a reported unit whose corresponding rates are all zero has unknown cost (both input/output rates zero for tokens, or audio rate zero for duration). Transcription token rates of 0/0 with a nonzero audio rate do not make token usage free. |
| `models[].max_output_tokens` | Absent | LLM-only: bounds chat `max_tokens` / `max_completion_tokens`, completions `max_tokens`, and responses `max_output_tokens`; larger values get a 400 (`max_output_tokens`) before reaching the upstream. Requires capability `model_limits`. |
| `models[].default_body` | Empty | JSON object merged under every request body: the client's fields win, objects on both sides merge recursively. Must not set `model`, `stream`, `stream_options` or `reasoning_effort` (use `reasoning_effort.default_level`); at most 32 levels of nesting. Requires capability `default_body`. |
| `models[].reasoning_effort` | Absent | `{mode, fragments, default_level}` for the OpenAI `reasoning_effort` field (levels `none`, `minimal`, `low`, `medium`, `high`, `xhigh`). `passthrough` forwards it unchecked, `strip` removes it, `mapped` replaces it with `fragments[level]` (e.g. `{chat_template_kwargs: {enable_thinking: false}}` for vLLM) and answers 400 for an unmapped level. `default_level` applies when the client sends none. Precedence: `default_body` < fragment < client. Requires capability `reasoning_effort`. |
| `models[].chat_check` | Absent | Periodic model probe: `{interval_ms, timeout_ms, unhealthy_threshold, healthy_threshold}`. POSTs a chat completion for LLM, `input: ping` to `/embeddings`, one predicate question to `/decisions`, or a generated one-second silent WAV multipart upload to `/audio/transcriptions`, with the upstream's token and the model's JSON body rules; 2xx is healthy. While unhealthy only this model is skipped, failing open when no candidate is healthy. Same timing rules and threshold defaults as `health_check`. Reported in `HealthReport.models`. Requires capability `model_chat_check`. |

A model with none of the last three settings is not parsed on the request path.
Only top-level body keys are parsed; untouched values keep their exact bytes.
Rejections count in `lumewarp_requests_rejected_total{model,reason}`; calling a name on an endpoint of another type returns 400 and uses reason `model_type_mismatch`.

## Aliases

| Field | Default | Meaning |
| --- | --- | --- |
| `aliases[].name` | Required | Unique name across models, aliases and routers; callable and listed by `/v1/models`. `unknown` is reserved. |
| `aliases[].target` | Required | An existing model or router, never another alias. The alias shares its pool: same retries, fallbacks and breakers. |

## Routers

| Field | Default | Meaning |
| --- | --- | --- |
| `routers[].name` | Required | Unique name across models, aliases and routers; callable and listed by `/v1/models`. `unknown` is reserved. |
| `routers[].members` | Required without `traffic_split` | Nonempty list of `{model, weight, params}` on distinct existing models. `weight` defaults to `1`, is an unsigned 32-bit relative weight and belongs to this router, so a model can weigh differently in two routers. Zero is allowed, but a weighted router must have positive total weight; other built-in strategies ignore it. `params` is an object read only by a strategy that declares it. Ignored (may be empty) when `traffic_split` is set. |
| `routers[].strategy` | `weighted` | `weighted`, `least_in_flight`, `lowest_latency`, `vllm_load` or a router-strategy plugin loaded from `LUMEWARP_ROUTER_PLUGIN_DIR`; see "Load-aware routing". With `traffic_split` it picks the model inside the chosen split. An unknown name is refused. |
| `routers[].params` | `{}` | Object for a plugin strategy, at most 16 KiB. Built-in strategies take none. A WASM strategy is bounded by `plugin_timeout_ms`. |
| `routers[].retry` | Absent (one attempt) | `{max_attempts, retry_on_status, total_timeout_ms}`. An empty object enables two total attempts. `max_attempts` includes the first and is at most 5; `retry_on_status` defaults to `[429, 502, 503, 504]` and accepts only 400..599. `total_timeout_ms: 0` leaves only each upstream's header timeout. |
| `routers[].fallbacks` | `[]` | Existing model names, in order; never routers or aliases. Uses the router's attempt/deadline budget. Configure a retry budget of at least two attempts for failure-triggered fallback. |
| `routers[].traffic_split` | `[]` | Canary split: `[{name, members, weight}]`. A request picks a split with probability `weight / sum(weights)`, then a model inside it with `strategy`. At most 16 splits; names unique, 1..=32 of `[A-Za-z0-9_.-]` (they are the `split` metric label); each split has a nonempty list of members (`{model, weight, params}`) on distinct existing models; weights must sum to more than 0 (a single split may have weight 0: it only receives retries). |
| `routers[].sticky` | `none` | `none`, `header` (hash of the `sticky_header` value; requests without the header are not sticky), `team` (the key's team id) or `key` (the key id). Anonymous or team-less callers are not sticky for `team`/`key`. Requires `traffic_split`. |
| `routers[].sticky_header` | Empty | Request header hashed for `sticky: header`; required there, rejected otherwise. |
| `routers[].plugin_timeout_ms` | `0` (60 s) | WASM router strategies only: how long one `pick` may run before the gateway cancels it. Built-in strategies ignore it. |

## Retry and health behavior

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

## Load-aware routing and traffic splits

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
[config protocol](../config-protocol.md).

## Teams

| Field | Default | Meaning |
| --- | --- | --- |
| `teams[].id` | Required | Unique, stable team id. It is the `team` metric label value, so keep the number of teams bounded. |
| `teams[].name` | `id` | Display name. |
| `teams[].rate_limits` | Absent | Limits shared by **all** keys of the team (scope `team:<id>`); see rate limits below. |

## Keys

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
| `keys[].scopes` | Absent | Explicit scopes: a list of `{id, requests_per_minute, tokens_per_minute, max_parallel_requests}`. Replaces the team/key shorthand for this key and lets a standalone config reproduce the control plane's `org:`/`member:`/`sa:` levels. At most four; the gateway rejects a snapshot with more. |

`rate_limits` objects have `requests_per_minute`, `tokens_per_minute` and
`max_parallel_requests` (each `0`/absent = unlimited). They are compiled into
per-key `RateLimitScope`s (team scope first, then key scope); the control plane can
also emit `org:<id>` and `member:<id>`/`sa:<id>`, one per level that sets a limit.
A key has at most four scopes; a snapshot with more is NACKed INVALID_CONFIG.
Limiter state is
keyed by scope id, so it survives reloads and a limit change applies in place, and it
is local to one gateway instance (N instances allow up to N times the limit).
`requests_per_minute` and `tokens_per_minute` are sliding-window counters on
wall-clock minutes: the current and the previous minute count, with the previous one
fading out as the minute elapses, so a burst is not followed by a second full burst at
the next minute boundary. `max_parallel_requests` is an in-flight gauge: a request
holds its slots until the response body ends, errors or is dropped. A request
acquires all of its scopes or none: requests-per-minute and parallel slots are
checked before forwarding; `tokens_per_minute` is charged after usage is known and
blocks later requests while the window stays full. A rejection is 429
`rate_limit_exceeded` with `Retry-After` (1–60 s).

## Routing

`weighted` samples precomputed cumulative weights. `least_in_flight` selects the
smallest atomic in-flight count, scanning from a random offset to spread ties.
This is an approximate concurrent decision, not a global scheduling lock.
Reservations last until the upstream response body ends/errors or is dropped,
or the request fails/is cancelled before headers. Counters are keyed by
model name for the life of the process: every router holding the same
model shares one, and a recompiled snapshot keeps counting requests still
running on the previous one.
