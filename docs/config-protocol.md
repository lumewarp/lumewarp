# Config stream protocol

Normative rules for `lumewarp.config.v1.ConfigService` and `lumewarp.usage.v1.UsageService`. The proto files are the wire contract; this document is the behavioural contract both sides are tested against.

## Terms

- **Scope**: one control-plane installation (`Snapshot.scope`). A gateway is configured with exactly one scope and never applies or boots from a snapshot of another scope.
- **Version**: strictly increasing per scope. Every published version has an immutable, complete `Snapshot` payload stored by the control plane.
- **Desired version**: the latest published version for the scope.
- **ACKed version**: per gateway session, the last version the gateway confirmed.

## Control plane: publishing

1. Every source mutation (API write, budget flip, rollover) and its audit row commit in one DB transaction together with an outbox row "recompile scope".
2. A single compiler loop per scope (serialized with a Postgres advisory lock) reads source state at one consistent snapshot, compiles, validates and stores `config_versions(scope, version, payload, digest, source_revision, created_at)`. A compile failure leaves the previous version current and marks the outbox row failed with a reason; it never publishes a partial snapshot.
3. A later compile always sees a later source revision; a slower, older compile must not publish after a newer one (check `source_revision` under the lock).
4. Rollback re-publishes an older version's **routing** content (upstreams, deployments, routes) as a new version; API keys, teams, policies and statuses are always taken from current source state, so a revoked key can never be revived by a rollback.
5. Retention: keep the latest version, the last 50 versions and anything referenced by a connected session; older payloads may be compacted. A gateway whose base was compacted gets a full snapshot.
6. Limits: a snapshot larger than 64 MiB or with more than 200k keys fails compilation. gRPC max message size is 64 MiB on both sides.

## Session state machine

```
gateway                                   control plane
  |-- Hello{gateway_id, have_version, scope, supported_capabilities} -->
  |                                       authenticate token; bind gateway_id
  |<-- full(desired) or delta(have_version -> desired) ---------------
  |   apply privately, persist, swap
  |-- Ack{version} or Nack{version, code, have_version} ------------->
  |<-- next update only after Ack/Nack of the previous one -----------
```

- **At most one un-ACKed update per session.** If several versions are published while one is pending, the next update goes straight from the session's ACKed version to the current desired version (delta if the base is retained, otherwise full).
- The base of a delta is always the session's ACKed version (or Hello.have_version before the first ACK) — never a global "last sent" version.
- The first update after Hello is a delta only when `Hello.scope` matches, `have_version` is retained, `have_version <= desired` and `Hello.have_digest` equals the stored digest of `have_version`; otherwise full. Until the first ACK after a full, the session has **no** delta base (a NACKed full is never followed by a delta).
- Digest = `config_versions.digest` (sha256 of the control plane's serialized payload). It is sent as `ConfigUpdate.target_digest`; the gateway treats it as opaque, persists it with the snapshot and echoes it in `Hello.have_digest` (serialization is not byte-identical across languages, so the gateway never recomputes it). `have_version > desired` (control plane restored from backup) → full.
- ACK/NACK must name the session's pending version; anything else is a protocol error and the control plane closes the stream.
- NACK handling:
  - `BASE_MISMATCH` → send full(desired).
  - `INVALID_CONFIG` / `UNSUPPORTED_FEATURE` → quarantine that version for that gateway; do not resend it. Send again only when a newer version is published. Surface it in gateway status and metrics.
  - `PERSIST_FAILED` → retry the same version with exponential backoff (1 s → 60 s).
- ACK timeout: 30 s, then the control plane closes the stream; the gateway reconnects with backoff (0.5 s → 30 s, full jitter).
- Keepalive: the gateway sends an HTTP/2 PING every 30 s, also while the stream is idle, and drops the connection if no ACK arrives within 10 s. The control plane must accept pings without data at least every 10 s (`grpc.http2.min_ping_interval_without_data_ms`, `grpc.keepalive_permit_without_calls`); gRPC core's default (5 min) answers them with GOAWAY `too_many_pings` and resets the stream every few minutes. The control plane pings every 60 s (20 s timeout) to notice gateways that vanished. Any L7 proxy between them must allow the same ping rate.
- Delta semantics: upserts replace the whole entity by key (name / key_id / team id); removes delete by key; `created_at_unix_ms` and `required_capabilities` replace the target metadata. Removing an entity that does not exist is not an error. A delta whose result fails validation is NACKed `INVALID_CONFIG`.

## Gateway: applying an update

```
receive → apply to a private copy → validate (capabilities, references, hash sizes, enums)
        → compile indexes, resolve upstream secrets from the environment
        → persist: write <state-dir>/snapshot.pb.tmp, fsync, rename to snapshot.pb, fsync dir
        → ArcSwap the new router
        → ACK
```

- One background apply task; blocking file I/O never runs on the request path.
- Persist failure → NACK `PERSIST_FAILED`, keep serving the previous version in memory.
- ACK means "active and durable": a gateway restarted right after ACK boots into that version.
- Validation failure (including an unresolvable `api_key_ref`) → NACK `INVALID_CONFIG`, keep the previous version.
- State survives swaps: per-deployment in-flight counters and circuit breakers,
  upstream health, the gateway retry budget, rate-limit buckets (by
  `RateLimitScope.id`) and the metrics registry are process-lifetime. Active
  probes are reconfigured only after an accepted snapshot, not during validation
  or compilation of a candidate that might fail persistence, and are diffed by
  (upstream, URL, `HealthCheck`, credentials): a delta that leaves an upstream's
  probe unchanged (e.g. key-only) keeps that probe running with its failure
  counts. Removing `Upstream.circuit_breaker` resets the breakers of its
  deployments to closed on activation.

### Routing capabilities

The gateway advertises `retry`, `fallbacks`, `health_check`, `circuit_breaker`,
`traffic_split`, `lowest_latency` and `vllm_load`. Compilers add a capability
only when its field is set:

| Capability | Fields |
| --- | --- |
| `retry` | Present `ModelRoute.retry`, including an empty message |
| `fallbacks` | Nonempty `ModelRoute.fallbacks` |
| `health_check` | Present `Upstream.health_check` |
| `circuit_breaker` | Present `Upstream.circuit_breaker`, including an empty message |
| `traffic_split` | Nonempty split, non-NONE sticky, or nonempty sticky header |
| `lowest_latency` | LOWEST_LATENCY strategy |
| `vllm_load` | VLLM_LOAD strategy or nonempty metrics URL |

Using a field without its capability is `INVALID_CONFIG`; requiring an
unsupported capability is `UNSUPPORTED_FEATURE`. No field can bypass the gate
by omitting its capability. Standalone YAML emits the same presence-based
capabilities. Retry attempts are capped at five; statuses must be 400..599;
fallback references must exist and cannot name the original route. Health
intervals must be at least 1000 ms with a positive timeout below the interval.
Fallbacks do not recurse. If all candidates are unhealthy/open, routing tries one
anyway, except that a half-open trial already in progress is never duplicated
(another candidate is used instead).

Response `model` contract: responses from the requested route's own
deployments (`ModelRoute.deployments` / splits, including retries) are
forwarded unchanged and carry `Deployment.upstream_model`. Successful responses
served by a deployment of a `ModelRoute.fallbacks` route have their top-level
`model` (JSON body, or each SSE event's `data` JSON) rewritten to the public
`ModelRoute.name` the client requested. Error responses are not rewritten.

The retry budget (`retry_budget_percent`, gateway-local) is funded only by
requests on routes with `ModelRoute.retry` set. A `Retry-After` cooldown is
capped at 60 s and at the upstream's `CircuitBreaker.open_ms`. An attempt cut
short by `RetryPolicy.total_timeout_ms` is not a breaker failure.

Traffic split validation (`INVALID_CONFIG` otherwise): at most 16 splits; names
unique and 1..=32 characters of `[A-Za-z0-9_.-]` (they are a metric label); each
split names a nonempty list of distinct existing deployments; split weights sum
to more than 0. With splits, `ModelRoute.deployments` is ignored and may be
empty. `STICKY_BY_HEADER` requires a valid `sticky_header`; `sticky_header` with
any other `sticky` is rejected, as is a non-NONE `sticky` without splits. A
nonempty `Upstream.metrics_url` must be an absolute http(s) URL without
userinfo. Sticky assignment uses a fixed-seed hash, so every gateway replica
puts the same header value / team / key in the same split for the same weights.
Latency EWMAs and scraped vLLM load are keyed by deployment name and survive
swaps; scrapers, like probes, are diffed only after a snapshot is accepted.

## Gateway: boot

- CP mode always requires keys: zero keys means every `/v1/*` request is 401.
- With a valid on-disk snapshot of the configured scope: serve it immediately, then connect in the background.
- Without one (missing, wrong scope, corrupt — file has a trailing sha256 and is size-capped): do not serve `/v1/*` (503 `not_ready`, readiness false) until the first full snapshot is applied. Never fall back to an open router.
- The gateway token is read from the environment and never written to disk. The state dir is created `0700`, the file `0600`.

## Usage reporting and attribution

- Usage is attributed to the key's team and to the key's owner (a team member or a service
  account). The event's `team_id` must match the key; the owner is read from the key, never
  from the event. There is no org or project level.
- Totals are kept per scope and UTC calendar month: the team (`usage_totals.scope_type=team`)
  and the key owner (`member`, with `member_scope_id = uuid5(team_id, user_id)`, or
  `service_account`, with the service account id). `member_scope_id` is derived, not stored,
  so removing and re-adding a member keeps the same total and bucket. The same ids are the
  rate-limit scopes: `team:<team_id>`, `member:<member_scope_id>` and
  `sa:<service_account_id>`; every key of the same team or owner shares that bucket within
  a gateway process. There is no key-level total.
- Events are produced off the request path into a bounded queue (default 100k events). Batches go out every 1 s or at 500 events. Failed reports retry with backoff from the same queue; when the queue is full, new events are dropped and `lumewarp_usage_events_dropped_total` increments.
- `request_id` is a UUIDv7 generated by the gateway at request start and reused for retries.
- `Report` validates per event and is idempotent (`ON CONFLICT (request_id) DO NOTHING`). An invalid event (malformed field, unknown or mismatched key hierarchy, a `request_id` reused for different content) is skipped and logged by the control plane; the rest of the batch is recorded and `ReportResponse.accepted` counts what was kept, so one bad event never drops billable usage. Only batch-level errors (gateway identity, more than the configured maximum events) fail the call with `INVALID_ARGUMENT`. Per new event the control plane adds the cost once to the team total and once to the owner total for the event's UTC calendar month, in the same transaction, then evaluates budgets.
- `requested` and `deployment` are required except for `REQUEST_OUTCOME_REJECTED` events: a request rejected at authentication has no model yet (the body is not read), and one rejected by policy (403/429) has no deployment. They are stored as empty strings.
- Budget enforcement is **eventual**: once usage is ingested, an exhausted scope flips affected keys to `BUDGET_EXCEEDED` and a new version is published within 5 s. Lost events (gateway crash, full queue) are not recovered in P1, and streams in flight when a budget runs out are not cut.
- Month rollover is driven by a scheduler (runs at startup and at each UTC month boundary), not by incoming usage. While the control plane is down, blocked keys stay blocked.
