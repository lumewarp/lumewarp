# Gateway settings

A Rust HTTP/1.1 gateway for OpenAI-compatible HTTP/HTTPS upstreams, including
vLLM and OpenAI. It compiles only from a protobuf config `Snapshot`
(`proto/lumewarp/config/v1/snapshot.proto`). No control plane, database, or
Python is in the request path.

Two modes are available:

- **standalone** (`--config`): read YAML at startup and convert it to a snapshot.
- **control-plane** (`--control-plane`): boot from a cached snapshot, follow the
  versioned gRPC config stream and report usage back to the control plane.

Every YAML field is in the [Gateway YAML reference](../reference/gateway-yaml.md).

## Control-plane mode

Standalone mode (`--config`) reads the whole configuration from YAML. In
control-plane mode the gateway follows the control plane's config stream and
reports usage to the same endpoint:

```sh
# Create a gateway token with a label (`bootstrap --name gateway`, or create one
# in the Admin UI); the token authenticates the scope and stores only its SHA-256
# hash. Any number of gateways may use it.
export LUMEWARP_GATEWAY_TOKEN='lwg-...'

lumewarp \
  --control-plane http://127.0.0.1:50051 \
  --gateway-token-env LUMEWARP_GATEWAY_TOKEN \
  --state-dir /tmp/lumewarp-state \
  --scope default \
  --local-config /etc/lumewarp/local.yaml
```

The token is read from the named environment variable, sent as
`authorization: Bearer <token>` on the config stream and usage reports, and never
written to disk. The gateway generates its id at startup (`<hostname>-<8 hex>`, the
hostname reduced to `[A-Za-z0-9._-]` plus random hex), sends it as
`Hello.gateway_id`, and keeps it for the process lifetime: a restart is a new gateway
and appears as a new row on the Gateways page, while the old row stays visible for 24
hours. Every process needs its own hostname and `--state-dir` (the directory holds that
process's last good snapshot); several gateways may share one token, and the control plane
keys each stream by its generated id
([protocol](../config-protocol.md#gateway-sessions)). The Helm chart runs every pod as its
own gateway. It runs the pod as UID/GID 65532 with
`fsGroup: 65532` so the mounted state directory is writable. `--local-config` is a YAML
file with only the `server:` block described below (listener, admin listener, timeouts);
upstreams, routers, teams and keys (including `server.api_keys`) are ignored in this mode.

### Flags

| Flag | Environment variable | Default | Meaning |
| --- | --- | --- | --- |
| `--config`, `-c` | `LUMEWARP_CONFIG` | required for standalone | Standalone YAML. Conflicts with every control-plane flag. |
| `--control-plane URL` | `LUMEWARP_CONTROL_PLANE` | unset | Control-plane gRPC URL (`http://` or `https://`). Enables control-plane mode and requires the three flags below. |
| `--gateway-token-env NAME` | none | required in control-plane mode | Name of the environment variable that holds the gateway token. |
| `--state-dir DIR` | none | required in control-plane mode | Directory for the last good snapshot. It is created `0700` on the first successful persist. |
| `--scope SCOPE` | none | required in control-plane mode | Config scope this gateway serves. Must match the control plane and the cached snapshot. |
| `--local-config FILE` | none | unset | YAML with only `server:` listener/timeout settings. Without it the defaults from the YAML reference apply. |
| `--shared-limits-redis-url URL` | `LUMEWARP_SHARED_LIMITS_REDIS_URL` | unset | `redis://` or `rediss://` endpoint used to share rate-limit counters between gateways. Empty/unset keeps the limits per gateway. The value is never logged. |
| `--shared-limits-sync-ms MS` | `LUMEWARP_SHARED_LIMITS_SYNC_MS` | `100` | Background sync interval, 10–1000 ms; it doubles up to 1 s while a sync takes more than half of it. |
| `--shared-limits-namespace NAME` | `LUMEWARP_SHARED_LIMITS_NAMESPACE` | `--scope` in control-plane mode | Redis key namespace. Required with a URL in standalone mode; gateways share counters only when both the Redis endpoint and the namespace match. |
| none | `LUMEWARP_ENCRYPTION_KEY` | unset | Opens upstream API keys the control plane stores sealed (`api_key_ciphertext`). Must equal the control plane's value; a snapshot with a sealed key is NACKed without it. Read when a snapshot is applied, never per request. |

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
[the table](../config-protocol.md). Events
enter a bounded in-memory queue (100,000) and are reported every 1 s or every
500 events. Failed batches retry from the queue with backoff; when the queue is
full new events are dropped and `lumewarp_usage_events_dropped_total`
increments. There is no disk spool: a gateway crash loses queued events, and a
long control-plane outage eventually drops them. On shutdown the reporter
flushes for up to 5 s.

For chat/completions streams the gateway injects `stream_options.include_usage: true` when the
upstream's `stream_usage_mode` is `include_usage` (the default) and the client
did not ask for usage; the extra usage-only chunk is stripped before it reaches
the client. `passthrough` disables the injection for upstreams that do not
support it. Non-stream usage is parsed from a bounded copy of the response body.

## Shared rate limits

By default each gateway counts a key's `requests_per_minute`, `tokens_per_minute` and
`max_parallel_requests` for itself, so N gateways allow up to N times a configured
limit. Setting `--shared-limits-redis-url` (both modes) makes the gateways that use the
same Redis and namespace share one window instead.

The request path never touches Redis. A background task every
`--shared-limits-sync-ms` sends this gateway's dirty per-minute counts (absolute
values, so a retry overwrites and never double-counts) and its in-flight gauge in one
pipelined script per 250 scopes, then applies the other gateways' counts to the local
limiter. Only scopes whose counters changed since the last successful sync are written;
a clean scope with counts in the current or previous minute is a read-only entry. Dirty,
in-flight, finite-share and multi-gateway scopes are read every interval; a clean scope
with only local counts and one gateway is read every fifth interval (it notices a
joining gateway within at most five intervals). Key TTLs are set whenever a write
creates a field or key and refreshed at most every five seconds per scope; the in-flight
hash is extended on every sync, so one missed refresh cannot make every gateway's slots
disappear. The interval doubles (up to 1 s) when a sync takes more than half of it; only
a successful sync steps it back down, and a connect failure leaves it unchanged. A sync
that finds a connect in flight waits for it inside its own timeout. The interval must
exceed the Redis round-trip time plus the script time; measure it on the deployment's
Redis.

Between two syncs a gateway may use at most `ceil(headroom / n)` of a scope. `n` is per
dimension: for rpm and tpm it counts the gateways with a positive count in the current
or previous minute, and for max_parallel the gateways with a fresh in-flight entry
(including zero). With one gateway the behaviour is exactly the per-gateway limiter. A
window may over-admit by about `n` requests, plus up to five sync intervals of traffic
when a gateway first joins a scope (a clean gateway refreshes its read-only view every
fifth interval).

The store layout is namespaced as `{lw:<namespace>}:<scope>:`: one hash per scope,
dimension and minute (`HSET` of the gateway's absolute count under its gateway id,
150 s expiry) and one hash per scope for in-flight values (`<n>:<redis_unix_ms>`,
entries older than 3 s ignored and deleted, 10 s expiry extended on every sync). A zero
count is an `HDEL`, not a zero-valued field or key. A key created by a write always gets
its expiry immediately, so even a gateway killed in the refresh gap leaves nothing
permanent. In-flight stamps and the staleness check use Redis's clock, so gateway clock
skew cannot hide another gateway's slots; the minute keys use the gateway's wall clock,
so keep hosts on NTP to line the windows up. A restarted process is a new gateway id,
so its old fields expire with their keys. One Redis or Valkey 6.2+ endpoint is supported
(`rediss://` for TLS); Cluster and Sentinel are not.

Redis is never on the critical path. After one second without a successful sync the
gateway falls back to counting for itself (the full local limit, not `limit / n`), so N
gateways allow up to N times the limit again; `/readyz` is unaffected. The next
successful sync resumes sharing; counts admitted during the outage stay dirty and are
written then (their minute windows expire as usual). A timed-out sync whose write did
reach Redis is harmless: the retry sends the same absolute counts and overwrites them.
An idle gateway still sends a `PING`, so it leaves fallback and does not page. The
`lumewarp_shared_limits_*` metrics and the `LumewarpSharedLimitsFallback` alert show
the state ([Observability](./observability.md)).

The URL may carry a password: set `LUMEWARP_SHARED_LIMITS_REDIS_URL` rather than passing
`--shared-limits-redis-url`, because command-line arguments are visible in `ps`. The value
is never logged.

Measured on a local `redis:7` (Docker on WSL2), one 250-scope chunk took about 5.8 ms for
dirty counters and 4.4 ms for read-only entries (4.0 ms when the scope has only one
dimension). These are aggregate per-Redis figures, across every gateway on that Redis:
with the five-interval read cadence, 1,000 clean recently used keys cost about 35 ms of
Redis CPU per second per gateway, so four gateways with 1,000 clean keys each use about
0.14 s/s (roughly 14% of one Redis); 1,000 dirty keys per gateway cost about 0.23 s/s, so
about four gateways with that much dirty traffic saturate this host. A dedicated Redis is
faster; measure the deployment's own limit.

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
- TPOT metrics; JWT/mTLS client authentication;
- an idle timeout on upstream response bodies after headers (including
  non-stream bodies); `timeout_ms` stops at response headers;
- client TLS termination (terminate it in front of the gateway).

Budgets are eventual and in-flight streams are not cut; usage events are lost when the
bounded queue is full or the gateway crashes. Without a shared Redis, rate limits are
per gateway (see above).

MCP and A2A are served on the same listener: `POST /mcp`, `POST /mcp/servers/<name>`,
`POST /a2a/<agent>`, the agent-card endpoint and `GET /a2a`. They authenticate only
`X-Lumewarp-Key`, read the snapshot, and need `LUMEWARP_GATEWAY_ID_KEY` (both) and
`LUMEWARP_GATEWAY_PUBLIC_URL` (A2A). Their settings, routes and the three WASM plugin
directories are documented in [gateway plugins](../plugins.md).

See the [architecture](../architecture.md) for planned capabilities, not a
statement that those features ship today.
