# Routers

A router is a name that spreads requests over member models with a strategy, and adds
retries, fallbacks and traffic splits. This page lists the settings and their rules; how
each behaves at request time is described under [Retry and health behavior](#retry-and-health-behavior).

![Editing a router](/screenshots/router-edit.png)

## Settings

Settings that compile 1:1 into the snapshot (`proto/lumewarp/config/v1/snapshot.proto`):

| API field | Where | Snapshot / default |
| --- | --- | --- |
| `health_url`, `health_check {interval_ms, timeout_ms, unhealthy_threshold, healthy_threshold}` | upstream | `Upstream.health_check`; `null` = no active probing; null `health_url` = `base_url` without a trailing `/v1`, plus `/health`; null thresholds = 3 / 1 |
| `circuit_breaker {failure_threshold, open_ms}` | upstream | `Upstream.circuit_breaker`; `null` = never opens; `{}` = gateway defaults (5 failures, 30000 ms). Breaker state is per model |
| `metrics_url` | upstream | `Upstream.metrics_url` for `vllm_load`; `null` = base URL minus `/v1` plus `/metrics` |
| `strategy`, `params` | router | `Router.strategy`, a name: `weighted`, `least_in_flight`, `lowest_latency` (EWMA), `vllm_load`, or a router-strategy plugin a gateway reports; `Router.params_json` |
| `members [{model_id, weight, params}]` | router | `Router.members`. Weight is per router; only `weighted` reads it |
| `retry {max_attempts, retry_on_status, total_timeout_ms}` | router | `Router.retry`; `null` = one attempt. `max_attempts: null` is emitted as 0, which the gateway reads as its default of 2 |
| `fallback_model_ids` (ordered) | router | `Router.fallbacks` (model names). Needs `retry` with at least 2 attempts |
| `traffic_split [{name, weight, members}]` | router | `Router.traffic_split`; with a split the router's own `members` stay empty |
| `sticky` (`none`/`header`/`team`/`key`), `sticky_header` | router | `Router.sticky` / `sticky_header` |

Validation mirrors the gateway's snapshot checks so an accepted write never makes a gateway
NACK (422 otherwise): health interval >= 1000 ms and 0 < timeout < interval, path absolute
without query/fragment; `metrics_url` plain http(s) without userinfo or query; `max_attempts` 1..5,
statuses 400..599 and unique; members and fallbacks exist, are models and are unique; at most
16 splits, names 1..32 of `[A-Za-z0-9_.-]` and unique, each split non-empty, weights >= 0
summing to more than 0; `sticky_header` (an HTTP header name, stored lower-case) required iff
`sticky=header`, and any sticky mode needs a split; a `weighted` router needs a positive total
member weight per split (or over its members without a split). PATCH replaces lists that are
given and keeps omitted fields; `retry: null` removes the policy, and leaving `sticky=header`
drops the header.

## Strategies and plugins

A router's `params` and member `params` are validated against the schema the
strategy declares, as reported by gateways in `Hello.plugins`, and against the committed
built-in manifest `compiler/builtin_plugins.json`, so built-ins work with no gateway connected.
An unreported strategy or a schema failure is 422. A connected gateway lacking the strategy is
422 unless the write has `force`, audited as `router.force_save`, because that gateway will NACK
the version. Stored rows that no longer validate read `config_invalid: true` with
`config_problems` and are not modified. The same rule lives in
`compiler.plugins.validate_plugin_params(session, kind, name, params, *, force)` and is what an
MCP server's or A2A agent's auth plugin (kinds `upstream-auth` and `a2a-middleware`) uses. Full
rules: [gateway plugins](../plugins.md).

The compiler lists a capability in `required_capabilities` exactly when a field needs it
(the gateway's rule): `health_check`, `circuit_breaker`, `vllm_load` (a metrics URL), `retry`,
`fallbacks`, `traffic_split` (split or sticky) and `strategy:<name>` for every strategy in use.
`lowest_latency` is not a capability of its own any more: it is `strategy:lowest_latency`.

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
