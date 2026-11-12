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
- TPOT metrics; JWT/mTLS client authentication;
- an idle timeout on upstream response bodies after headers (including
  non-stream bodies); `timeout_ms` stops at response headers;
- client TLS termination (terminate it in front of the gateway).

MCP and A2A are served on the same listener: `POST /mcp`, `POST /mcp/servers/<name>`,
`POST /a2a/<agent>`, the agent-card endpoint and `GET /a2a`. They authenticate only
`X-Lumewarp-Key`, read the snapshot, and need `LUMEWARP_GATEWAY_ID_KEY` (both) and
`LUMEWARP_GATEWAY_PUBLIC_URL` (A2A). Their settings, routes and the three WASM plugin
directories are documented in [gateway plugins](../plugins.md).

See the [architecture](../architecture.md) for planned capabilities, not a
statement that those features ship today.
