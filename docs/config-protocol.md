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
4. Rollback restores models, aliases, routers, MCP servers and A2A agents and their names by id from the version's `catalog_state` and re-publishes its **routing** content (upstreams, models, aliases, routers, MCP servers, A2A agents) as a new version. Grants are not restored: grants on a catalog item deleted since then stay gone, while an MCP server or A2A agent absent from the state is archived rather than deleted, so its definitions and grants survive. Reviewed definition states are not part of the state: a rollback never re-decides a definition. API keys, teams, policies and statuses are always taken from current source state, so a revoked key can never be revived by a rollback. Restored routers meet the same plugin rules as a router write, against the gateways of today: a rejection is a 422 that changes nothing, and `force` saves past connected gateways lacking a strategy, with the same audit.
5. Retention: keep the latest version, the last 50 versions and anything referenced by a connected session; older payloads may be compacted. A gateway whose base was compacted gets a full snapshot.
6. Limits: a snapshot larger than 64 MiB or with more than 200k keys fails compilation. gRPC max message size is 64 MiB on both sides.

## Session state machine

```
gateway                                   control plane
  |-- Hello{gateway_id, have_version, scope, supported_capabilities, plugins, plugin_abi} -->
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
- `Hello.plugins` lists a `PluginDescriptor` per plugin the gateway has: built-ins and the WASM components loaded from its three plugin directories at startup. A descriptor carries the kind (`router-strategy`, `upstream-auth`, `a2a-middleware`), name, title, description, `builtin`, the params JSON Schema, the router-only `uses_weight`/`needs`/member schema, the component's `wasm_sha256` (empty for built-ins) and a `digest` that changes whenever any other field does. `Hello.plugin_abi` is the WIT package major version the gateway hosts (1). The list is untrusted and only informs the API and UI, which validate writes against its schemas: nothing in it can end the stream, and it does not gate snapshots, which `supported_capabilities` and the gateway's own `configure` still do. [Gateway plugins](plugins.md) has the manifest and schema rules.
  - Normalized off the event loop: at most 128 plugins per kind per gateway, descriptors first, then that kind's capabilities, names matching `^[a-z][a-z0-9_]{0,62}$`; each schema capped at 16 KiB and restricted to a subset, or dropped. The rest is ignored, logged and counted in `lumewarp_cp_gateway_plugins_ignored_total`. Other capabilities are capped at 64, of at most 128 characters each. The session's stored capabilities list every kept plugin.
  - The status row and the stored descriptors change in one transaction, under that row's lock, and only for the session owning the row. Session ids move forward, so a slower older connection neither overwrites a newer one's report nor replaces its stream: it ends with `UNAVAILABLE`. A reader sees one Hello, never parts of two.
  - A Hello replaces all the gateway reported before. A gateway counts while the token of its latest Hello is unrevoked and it is connected or was seen in the last 30 days; a report counts while the token that made it is unrevoked. A connected gateway has what its latest Hello lists, an offline one what it last reported.
- ACK/NACK must name the session's pending version; anything else is a protocol error and the control plane closes the stream.
- NACK handling:
  - `BASE_MISMATCH` → send full(desired).
  - `INVALID_CONFIG` / `UNSUPPORTED_FEATURE` → quarantine that version for that gateway; do not resend it. Send again only when a newer version is published. Surface it in gateway status and metrics.
  - `PERSIST_FAILED` → retry the same version with exponential backoff (1 s → 60 s).
- ACK timeout: 30 s, then the control plane closes the stream; the gateway reconnects with backoff (0.5 s → 30 s, full jitter).
- Keepalive: the gateway sends an HTTP/2 PING every 30 s, also while the stream is idle, and drops the connection if no ACK arrives within 10 s. The control plane must accept pings without data at least every 10 s (`grpc.http2.min_ping_interval_without_data_ms`, `grpc.keepalive_permit_without_calls`); gRPC core's default (5 min) answers them with GOAWAY `too_many_pings` and resets the stream every few minutes. The control plane pings every 60 s (20 s timeout) to notice gateways that vanished. Any L7 proxy between them must allow the same ping rate.
- Delta semantics: upserts replace the whole entity by key (name / key_id / team id); removes delete by key; MCP servers and A2A agents are keyed by name, and removing one drops its grants from the next compile; `created_at_unix_ms` and `required_capabilities` replace the target metadata. Removing an entity that does not exist is not an error. A delta whose result fails validation is NACKed `INVALID_CONFIG`.

## Catalog reports

A gateway reports what it observed upstream that the control plane cannot fetch
itself (it does not run WASM auth plugins and holds no user's token). A
`CatalogReport` travels on the same stream as `HealthReport`, any time after
`Hello`; unlike `HealthReport` it adds to what was reported before rather than
replacing it.

- `ObservedDefinition { server, server_id, kind, name, hash, definition_json,
  auto_approved, observed_at_unix_ms, config_version }`: one MCP tool or prompt.
  Every definition the gateway withheld is reported, and every definition TOFU
  served on first sight is reported once so grants can be written against it.
  `definition_json` is the canonical definition, at most 64 KiB; `hash` is its
  sha256. `auto_approved` is true only for a TOFU first sight.
- `AgentCardReport { agent, agent_id, card_json, error, fetched_at_unix_ms,
  config_version }`: the card the gateway fetched (rewritten as the gateway
  serves it) or the sanitized fetch error (at most 500 bytes); a failed fetch
  carries the last good card too. A `passthrough` agent usually has no card the
  gateway can fetch, and an administrator's pasted `card_json` stays the
  fallback.
- `server_id`/`agent_id` and `config_version` are provenance: the control plane
  discards a report whose id no longer matches the name, whose `config_version` it never
  published, or whose version was compiled before the target's last write
  (`McpServer.config_revision` / `A2aAgent.config_revision`). It recomputes the definition
  hash itself and honours `auto_approved` only when the name is also new to it and the server is not `MANUAL` (first
  writer wins).
- Bounds: a gateway holds at most 200 unreported definitions per server, reports
  the same (server, hash) at most once per gateway per 10 minutes, and drops what
  exceeds a definition's 64 KiB or a report's size limits, counting
  `lumewarp_mcp_catalog_reports_dropped_total{reason}`. The control plane shows
  the result as text only.

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
- Validation failure (including a WASM plugin `configure` error, an unresolvable `api_key_ref`, or an `api_key_ciphertext` that does not open with the gateway's `LUMEWARP_ENCRYPTION_KEY`) → NACK `INVALID_CONFIG`, keep the previous version.
- State survives swaps: per-model in-flight counters and circuit breakers,
  upstream health, the gateway retry budget, rate-limit buckets (by
  `RateLimitScope.id`) and the metrics registry are process-lifetime. Active
  probes are reconfigured only after an accepted snapshot, not during validation
  or compilation of a candidate that might fail persistence, and are diffed by
  (upstream, URL, `HealthCheck`, credentials): a delta that leaves an upstream's
  probe unchanged (e.g. key-only) keeps that probe running with its failure
  counts. Removing `Upstream.circuit_breaker` resets the breakers of its
  models to closed on activation.

### Names: models, aliases, routers

A snapshot has three kinds of callable name, unique across all three. `unknown`
is reserved in every kind.

- `Model { name, upstream, upstream_model, price }`: callable directly, as a
  single-attempt pool with no retry and no fallback.
- `Alias { name, target }`: a second name for a model or a router, one level
  only. It shares the target's pool and has no settings of its own.
- `Router { name, members, strategy, params_json, retry, fallbacks,
  traffic_split, sticky, sticky_header }`: a strategy pool.
  `RouterMember { model, weight, params_json }` holds a model and its weight in
  this router, so one model can have different weights in different routers.
  Members and `fallbacks` are always models, never routers or aliases. With
  `traffic_split`, members live in the splits and `Router.members` is ignored
  and may be empty.

`params_json` is empty or a JSON object, at most 16 KiB; empty means `{}`. A
built-in strategy takes no router params. `Router.strategy` is a name:
`weighted`, `least_in_flight`, `lowest_latency`, `vllm_load`, or a
`router-strategy` plugin the gateway loaded at startup (built-in or WASM).
`Router.plugin_timeout_ms` bounds one WASM `pick`; `0` means 60 s and built-in
strategies ignore it. The name's capability is `strategy:<name>`.

`ApiKey.policy.allowed_models` holds expanded names. A grant covers exactly the
named entry: a key allowed a router cannot call its members by name, and a key
allowed an alias cannot call its target by name, unless those names are granted
too. A rename therefore changes affected keys, which go out in the same delta
with the upserts and removes of the renamed entry and of whatever references it.
`GET /v1/models` lists every name the key may call, of any kind, sorted.

### MCP servers and A2A agents

A snapshot also carries the MCP servers and A2A agents the gateway proxies. A
server needs capability `mcp`; an agent needs `a2a`. Their names match
`^[a-z][a-z0-9_-]{0,62}$` and contain no `__` (the aggregate MCP endpoint splits
on the first `__`), and are unique within their kind. The snapshot and grants
reference them by name; usage carries the name as `target`; a catalog report
also carries the id so the control plane can reject stale data.

- `McpServer { id, name, url, auth, timeout_ms, review_mode, definitions }`:
  `url` is the upstream Streamable HTTP endpoint (absolute http(s), no
  userinfo); `timeout_ms` bounds one upstream HTTP request, `0` = the gateway
  default. `auth` is an `UpstreamAuth`: `plugin` names a built-in (`none`,
  `static_bearer`, `passthrough`) or a `upstream-auth` plugin, with
  `params_json` (at most 16 KiB, never a secret) and `timeout_ms` (WASM plugins only:
  one plugin call, `0` = 60 s; built-in auth ignores it). `static_bearer` takes its token
  from `secret_ref` (a gateway environment variable) or `secret_ciphertext` (sealed like
  `Upstream.api_key_ciphertext`), never from settings.
- `definitions` is the review state of every tool and prompt the control plane
  knows, never the definition itself: `McpDefinition { kind, name, hash,
  state }`. `hash` is 32 bytes of sha256 over the canonical definition JSON;
  `state` is `APPROVED`, `PENDING` or `REJECTED`, and `UNSPECIFIED` counts as
  pending. `review_mode` is `TOFU` (also the meaning of `UNSPECIFIED`),
  `MANUAL` or `OFF`. [Gateway plugins](plugins.md#mcp-definition-review) has
  what each mode does with an observed definition.
- `A2aAgent { id, name, url, card_url, auth, middleware, timeout_ms,
  card_json }`: `url` is the JSON-RPC endpoint; `card_url` empty means the
  origin of `url` plus `/.well-known/agent-card.json`. `middleware` is an
  optional `PluginRef` naming an `a2a-middleware` plugin (at most one per
  agent); `card_json` is the card served when the gateway has not fetched one
  (at most 64 KiB).
- `PluginRef { plugin, params_json, timeout_ms }` is the same reference an MCP
  server's `auth` and an A2A agent's `middleware` use. A plugin a gateway does
  not have is `UNSUPPORTED_FEATURE` for that gateway; using one without its
  `auth:<name>` or `a2a_middleware:<name>` capability is `INVALID_CONFIG`.
- `ApiKey.policy` carries the MCP and A2A grants: `allow_all_mcp` with
  `mcp_grants []McpGrant{server, all_tools, tools}`, and `allow_all_agents`
  with `allowed_agents`. A server absent from `mcp_grants` is invisible. A
  granted server exposes the tools its grant covers and all of its prompts.
  MCP `tools/call` is charged as one request against the key's
  `requests_per_minute` scopes; A2A is not rate limited, and neither charges
  tokens or budget.

### Key rotation grace

`ApiKey.previous_key_hash` and `ApiKey.previous_key_expires_at_unix_ms` carry the secret
being replaced while the control plane's rotation grace is open. The gateway indexes it as
the same key (same `key_id`, team, status, policy and limiter scopes), so usage during the
grace is attributed to one key. It stops authenticating at the earlier of its own expiry
and `ApiKey.expires_at_unix_ms`; a gateway that boots from its persisted snapshot enforces
that without the control plane. The control plane omits the pair from its next compile
after it expires, which removes it from every gateway through the normal delta. Only one
previous hash exists: rotating twice inside the grace ends the older grace immediately.
Standalone YAML has no previous-hash field.

### Routing capabilities

The gateway advertises `retry`, `fallbacks`, `health_check`, `circuit_breaker`,
`traffic_split`, `vllm_load`, `rotation_grace`, `model_chat_check`, `mcp`, `a2a`,
`strategy:<name>`, `auth:<name>` and `a2a_middleware:<name>`: the last three name
plugins the gateway has (built-ins included), and `mcp`/`a2a` gate MCP servers
and A2A agents. Compilers add a capability only when it is used:

| Capability | Fields |
| --- | --- |
| `retry` | Present `Router.retry`, including an empty message |
| `fallbacks` | Nonempty `Router.fallbacks` |
| `health_check` | Present `Upstream.health_check` |
| `circuit_breaker` | Present `Upstream.circuit_breaker`, including an empty message |
| `traffic_split` | Nonempty split, non-NONE sticky, or nonempty sticky header |
| `vllm_load` | Nonempty `Upstream.metrics_url` |
| `rotation_grace` | Nonempty `ApiKey.previous_key_hash` |
| `model_chat_check` | Present `Model.chat_check` |
| `mcp` | Nonempty `Snapshot.mcp_servers` |
| `a2a` | Nonempty `Snapshot.a2a_agents` |
| `strategy:<name>` | Any router with `Router.strategy = <name>`, including `strategy:weighted` |
| `auth:<name>` | Any MCP server or A2A agent whose `auth.plugin = <name>` |
| `a2a_middleware:<name>` | Any A2A agent whose `middleware.plugin = <name>` |

Using a field without its capability is `INVALID_CONFIG`; requiring an
unsupported capability is `UNSUPPORTED_FEATURE`, and so is a `Router.strategy`
this gateway does not have, whatever the snapshot lists. No field can bypass the
gate by omitting its capability. A snapshot with no routers requires no strategy
capability. Standalone YAML emits the same presence-based capabilities. Retry
attempts are capped at five; statuses must be 400..599; fallbacks must name
existing models. Health and chat-check intervals must be at least 1000 ms with a positive
timeout below the interval. Fallbacks do not recurse: a model has none. If all
candidates are unhealthy/open, routing tries one anyway, except that a half-open
trial already in progress is never duplicated (another candidate is used
instead). Circuit breaker state belongs to the model and is shared by every
router that holds it; its configuration comes from the upstream.

Response `model` contract: a successful (2xx) response carries the name the
client called, which may be a model, an alias or a router, in its top-level
`model` (the JSON body, or each SSE event's `data` JSON). Which model served it,
retry or fallback included, makes no difference. There are three exceptions:

- **Same name.** When the called name equals `Model.upstream_model` of the model
  that served, the response is not scanned and carries whatever the upstream
  sent, which may differ from what was asked for (OpenAI answers `gpt-4o` with a
  dated `gpt-4o-2024-08-06`). `Content-Length` and the client's `Accept-Encoding`
  are kept.
- **Compressed anyway.** When a rewrite is needed the gateway removes the client's
  `Accept-Encoding` from that upstream attempt. An upstream whose `Content-Encoding`
  still names anything other than `identity`, in any field or list item, is passed
  through byte for byte, not decompressed: the client sees the upstream's name, usage is
  `USAGE_STATE_INVALID` as for any encoded body, and
  `lumewarp_response_model_rewrite_compressed_total{model}` increments.
- **Errors.** Non-2xx responses are forwarded unchanged and may name the upstream
  model.
- **Not JSON or SSE.** Only `application/json`, `application/*+json` and
  `text/event-stream` are scanned, compared case-insensitively with parameters
  ignored. Any other declared `Content-Type`, an empty one, or more than one field
  makes the body opaque: the gateway leaves it alone, even for a `stream: true`
  request. Without a `Content-Type` the request decides, because OpenAI-compatible
  servers vary: `stream: true` is scanned as SSE, otherwise as JSON. Usage metering
  reads the body the same way. An opaque body is only observed: its bytes and
  `Content-Length` stay, nothing is filtered out, not even an event that looks like
  the forced usage event, and tokens found in it still count.

The rewrite never buffers: bytes are forwarded as each frame is scanned, whatever
the size of the body, and the length changes, so `Content-Length` is dropped and
the response is chunked. `Content-MD5`, `Digest`, `Content-Digest`, `Repr-Digest`
and `ETag` describe the upstream's bytes, so a rewritten response drops them from
its headers and trailers; other trailers stay. All the exceptions above keep them.
The scanner follows the structure, it does not validate:

| Upstream sends | Result |
| --- | --- |
| Top-level `model` with a string value | Value replaced; every other byte unchanged |
| Key spelled with escapes (`"m\u006fdel"`) | Same key to a JSON parser, so the same; the key's spelling is kept |
| Duplicate top-level `model` | All replaced |
| `model` with a non-string value | Unchanged |
| No top-level `model`, or only nested ones | Unchanged, none added |
| Body that is not a JSON object, including JSON preceded by a UTF-8 byte order mark | Unchanged |
| Invalid structure of the top-level object, mismatched brackets, nesting deeper than 128 | Unchanged from that byte on; a `model` already passed stays replaced, and the connection is not failed |
| Invalid scalars, commas in nested values, or string contents | Not checked: a later `model` is still replaced in a body that stays invalid |
| `model` string cut by EOF, or holding a raw control byte | The called name in full, then unchanged from the control byte on |
| Bytes after the top-level object closes | Unchanged |

SSE: the `data` lines of one event form one document, joined by a newline, and
each event starts over at the blank line. Comments and other fields pass through,
also between `data` lines. CR, LF and CRLF end a line. `[DONE]` and any other
non-object data, and events with no data, are unchanged. One UTF-8 byte order
mark at the very start of the body is skipped when reading fields and forwarded;
later it is part of an unknown field. The meter reads upstream
bytes before the rewrite, so token counts are unaffected; the SSE filter that
drops the forced usage event runs before it too.

The retry budget (`retry_budget_percent`, gateway-local) is funded only by
requests on routers with `Router.retry` set. A `Retry-After` cooldown is
capped at 60 s and at the upstream's `CircuitBreaker.open_ms`. An attempt cut
short by `RetryPolicy.total_timeout_ms` is not a breaker failure.

Traffic split validation (`INVALID_CONFIG` otherwise): at most 16 splits; names
unique and 1..=32 characters of `[A-Za-z0-9_.-]` (they are a metric label); each
split has a nonempty list of members on distinct existing models; split weights sum
to more than 0. With splits, `Router.members` is ignored and may be
empty. `STICKY_BY_HEADER` requires a valid `sticky_header`; `sticky_header` with
any other `sticky` is rejected, as is a non-NONE `sticky` without splits. A
nonempty `Upstream.metrics_url` must be an absolute http(s) URL without
userinfo. Sticky assignment uses a fixed-seed hash, so every gateway replica
puts the same header value / team / key in the same split for the same weights.
Latency EWMAs and scraped vLLM load are keyed by model name and survive
swaps; scrapers, like probes, are diffed only after a snapshot is accepted.

## Gateway: boot

- The three plugin directories are read before any configuration is applied (a
  persisted snapshot in control-plane mode, the YAML in standalone mode). A
  configured directory that is missing or unreadable, a file that does not
  implement its directory's world, a duplicate or built-in name, an unsupported
  ABI or an invalid manifest refuses to start: a gateway never skips a plugin
  silently. There is no hot reload. See [Gateway plugins](plugins.md#loading).
- CP mode always requires keys: zero keys means every `/v1/*` request is 401.
- With a valid on-disk snapshot of the configured scope: serve it immediately, then connect in the background.
- Without one (missing, wrong scope, corrupt, or written in an older format — the file starts with the format marker `LWSNAP02`, has a trailing sha256 and is size-capped): do not serve `/v1/*` (503 `not_ready`, readiness false) until the first full snapshot is applied. Never fall back to an open router. A file without the marker is not decoded: protobuf skips unknown fields, so an older file would otherwise decode into a snapshot with empty routers. `Hello` then offers version 0, the control plane sends a full, and the file is replaced.
- The gateway token is read from the environment and never written to disk. The state dir is created `0700`, the file `0600`.

## Usage reporting and attribution

- Usage is attributed to the key's team and to the key's owner (a team member or a service
  account). The event's `team_id` must match the key; the owner is read from the key, never
  from the event. There is no project level.
- Totals are kept per scope and UTC calendar month: the team (`usage_totals.scope_type=team`)
  and the key owner (`member`, with `member_scope_id = uuid5(team_id, user_id)`, or
  `service_account`, with the service account id). Org spend is derived on read: the sum of
  the org's teams' totals, so there is no `org` row and no drift. `member_scope_id` is
  derived, not stored, so removing and re-adding a member keeps the same total and bucket.
  There is no key-level total.
- A key has at most four rate-limit scopes, one per level that sets a limit:
  `org:<org_id>`, `team:<team_id>`, `member:<member_scope_id>` and
  `sa:<service_account_id>`, plus `key:<key_id>`. Every key of the same scope id shares that
  bucket within a gateway process. Control plane and gateway reject a fifth scope.
- Events are produced off the request path into a bounded queue (default 100k events). Batches go out every 1 s or at 500 events. Failed reports retry with backoff from the same queue; when the queue is full, new events are dropped and `lumewarp_usage_events_dropped_total` increments.
- `request_id` is a UUIDv7 generated by the gateway at request start and reused for retries.
- `Report` validates per event and is idempotent (`ON CONFLICT (request_id) DO NOTHING`). An invalid event (malformed field, unknown or mismatched key hierarchy, a `request_id` reused for different content) is skipped and logged by the control plane; the rest of the batch is recorded and `ReportResponse.accepted` counts what was kept, so one bad event never drops billable usage. Only batch-level errors (gateway identity, more than the configured maximum events) fail the call with `INVALID_ARGUMENT`. Per new event the control plane adds the cost once to the team total and once to the owner total for the event's UTC calendar month, in the same transaction, then evaluates budgets.
- `requested` and `model` are required except for `REQUEST_OUTCOME_REJECTED` events: a request rejected at authentication has no requested name yet (the body is not read; this is the only case where `requested` is empty), and one rejected by policy or for an unknown name (403/404/429) has no selected model. They are stored as empty strings.
- `requested` is the name the client sent, kept even when it does not exist, cut to 400 bytes on a character boundary with NUL replaced. `model` is the model that finally served the request. `alias` and `router` say how the request got there:

  | Call | `model` | `alias` | `router` | `fallback` |
  | --- | --- | --- | --- | --- |
  | Model `gpt-4o` | `gpt-4o` | | | false |
  | Alias `gpt-4` → model `gpt-4o` | `gpt-4o` | `gpt-4` | | false |
  | Router `coder`, a member serves | the member | | `coder` | false |
  | Alias `latest` → router `coder` | the member | `latest` | `coder` | false |
  | Same, members failed, fallback serves | the fallback | `latest` | `coder` | true |

  `upstream` and `upstream_model` are what the final attempt used, sent by the gateway so a later catalog change cannot misattribute them. `attempts` counts all upstream attempts of the request; 0 means none was made. A rejected request with a known name carries its `alias` and `router`.
- MCP and A2A requests emit one event per JSON-RPC request with `kind`
  (`MCP` or `A2A`), `target` (server or agent name), `method`, `tool` for MCP
  tool and prompt calls, `status` and the final outcome and duration. They carry
  no model, tokens, cost or `requested`, and the control plane records them
  without adding them to the team or owner totals; budgets and limits never see
  them. A streaming request's event is emitted when the stream ends, with the
  final outcome (`COMPLETED`, `UPSTREAM_ERROR`, or `CLIENT_CANCELLED`/499 when
  the client disappears).
- Budget enforcement is **eventual**: once usage is ingested, an exhausted scope flips affected keys to `BUDGET_EXCEEDED` and a new version is published within 5 s. Lost events (gateway crash, full queue) are not recovered, and streams in flight when a budget runs out are not cut.
- Month rollover is driven by a scheduler (runs at startup and at each UTC month boundary), not by incoming usage. While the control plane is down, blocked keys stay blocked.
