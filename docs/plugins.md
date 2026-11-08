# Gateway plugins

A plugin is a WebAssembly component that changes one behaviour of the data plane: which member of a router serves a request, how the gateway authenticates to an MCP server or an A2A agent, or what an A2A request or response contains. It is loaded from a directory at startup, so shipping one is a file and a restart, not a gateway build; the Rust SDK targets `wasm32-wasip2`. `gateway/wit/plugin.wit` fixes the function signatures; this page is the reference for writing a plugin and for the host that runs it.

Built-ins are native code inside the gateway, reported as plugins so the control plane and the UI handle them the same way.

## Kinds and capabilities

| `kind` | World | Directory | Capability | Built-ins |
| --- | --- | --- | --- | --- |
| `router-strategy` | `lumewarp:plugin/router@1.0.0` | `LUMEWARP_ROUTER_PLUGIN_DIR` | `strategy:<name>` | `weighted`, `least_in_flight`, `lowest_latency`, `vllm_load` |
| `upstream-auth` | `lumewarp:plugin/auth@1.0.0` | `LUMEWARP_AUTH_PLUGIN_DIR` | `auth:<name>` | `none`, `static_bearer`, `passthrough` |
| `a2a-middleware` | `lumewarp:plugin/middleware@1.0.0` | `LUMEWARP_A2A_MIDDLEWARE_DIR` | `a2a_middleware:<name>` | none |

One file is one plugin: it implements exactly one world and exports `manifest()`. A name is configuration — a router stores it in `strategy`, an MCP server or A2A agent in `auth.plugin` and `middleware.plugin` — so renaming or repurposing one is a breaking change.

The gateway advertises `strategy:<name>`, `auth:<name>` or `a2a_middleware:<name>` for every plugin it has, built-ins included. The gateway also advertises `mcp` and `a2a`: a snapshot that carries an MCP server requires `mcp`, and one that carries an A2A agent requires `a2a`. Requiring a capability the gateway does not advertise is NACKed `UNSUPPORTED_FEATURE`; using a field without its capability is `INVALID_CONFIG`. See "Routing capabilities" in [config-protocol.md](config-protocol.md).

A router strategy runs on the LLM request path, so the four built-in strategies are dispatched inline; an LLM request reaches WebAssembly only when a router names a WASM strategy. Traffic splits, retries, fallbacks, stickiness and circuit breakers belong to the gateway, never to a strategy.

## MCP and A2A endpoints

The gateway speaks MCP revision 2025-11-25 over Streamable HTTP, and proxies an upstream that negotiates 2025-06-18 without converting fields. `POST /mcp` is one virtual server for the key: tool and prompt names are `<server>__<name>`, split at the first `__`, and the answer is filtered by the key's grants. `POST /mcp/servers/<name>` is a single upstream with unprefixed names. Server and agent names match `^[a-z][a-z0-9_-]{0,62}$` and contain no `__`; they are unique within their kind, so the prefix is unambiguous.

The aggregate endpoint answers `initialize` itself and returns an opaque gateway `Mcp-Session-Id`: an AES-GCM token sealed under a key derived with HKDF from `LUMEWARP_GATEWAY_ID_KEY`, carrying the key id, each upstream's session id and negotiated version, and the issue time. It expires after 24 hours, and any gateway with the same id key can serve it, so sessions need no stickiness. `DELETE` is forwarded to every upstream; an upstream that answers 404 for its session makes the gateway answer 404, so the client initializes again. An upstream's request to the client gets the JSON-RPC id `"lw:<server>:<original id JSON>"`, and aggregate list cursors are sealed `(server position, upstream cursor)` values. `GET /mcp` answers 405; `GET /mcp/servers/<name>` is forwarded, so `list_changed` notifications arrive only on the single-server endpoint and an aggregate client re-lists.

The A2A endpoints use A2A v1.0 over JSON-RPC and SSE: `POST /a2a/<agent>` (including `SendStreamingMessage` and `SubscribeToTask`), `GET /a2a/<agent>/.well-known/agent-card.json`, and `GET /a2a` for the agents this key may use. Every JSON-RPC request must carry `A2A-Version: 1.0`; a missing or different version gets `VersionNotSupportedError` (`-32009`). Push-notification configuration is refused. The card the gateway serves is its own rewrite: `supportedInterfaces` keeps exactly one JSON-RPC 1.0 entry whose URL is `LUMEWARP_GATEWAY_PUBLIC_URL` plus `/a2a/<agent>` (the Host header is not trusted); `securitySchemes` and `securityRequirements` are replaced by an apiKey scheme in `X-Lumewarp-Key`; `signatures` are removed; and `capabilities.pushNotifications` is false. The `GetExtendedAgentCard` response is rewritten the same way.

MCP and A2A accept exactly one `X-Lumewarp-Key`; zero or several is 401. These endpoints authenticate only that header: an `Authorization: Bearer` or a Playground `lwpg.` token does not authenticate. A revoked, suspended or expired key is refused; a key with `KEY_STATUS_BUDGET_EXCEEDED` is accepted, because budget enforcement belongs to the LLM path. Every request is authorized by the key's compiled grants: a server, tool or agent the key does not have is invisible in a list and refused on a call.

A2A task and context ids belong to the key that minted them, not to the member: another key of the same user cannot use them. The gateway hands out opaque `lwt.` ids that bind the upstream id to the agent and to the key. `ListTasks` is forwarded only for an agent whose outbound auth is `passthrough` (the upstream then isolates by the user's own token), and the task ids it returns are wrapped for the caller's key; for any other auth the gateway answers `UnsupportedOperationError` (`-32004`).

MCP `tools/call` is charged as one request against the key's existing `requests_per_minute` scopes; tokens, concurrency and budgets are not charged, and A2A is not rate limited.

Bodies are bounded: an MCP or A2A request body over 4 MiB is refused with 413; a non-streaming response over 16 MiB, or one SSE event over 1 MiB, fails with 502. Both are counted in `lumewarp_mcp_requests_total` or `lumewarp_a2a_requests_total` with that status.

| Variable | |
| --- | --- |
| `LUMEWARP_GATEWAY_ID_KEY` | Base64, at least 32 bytes, identical on every gateway. It is the key material for the A2A task and context id MAC and, through HKDF, for the key that seals MCP sessions; it is never in a snapshot. A malformed value is logged and treated as unset. When it is unset, the MCP and A2A endpoints answer 503 (MCP sessions cannot be sealed); LLM traffic is unaffected. |
| `LUMEWARP_GATEWAY_PUBLIC_URL` | External base URL for agent cards, for example `https://gateway.example.com`; an absolute http(s) URL without a query or userinfo. Agent card URLs are built from it, never from the Host header. A malformed value is logged and treated as unset; the A2A endpoints answer 503 when it is unset, and MCP and LLM traffic are unaffected. |

The rules behind definition review, including what a TOFU gateway decides on its own, are in [MCP definition review](#mcp-definition-review).

## Loading

The gateway reads the three directories during startup, before it applies any configuration (a persisted snapshot in control-plane mode, the YAML in standalone mode). Every file whose name ends in `.wasm` in a directory is loaded, its `manifest()` is called once, and the result is checked; other files are ignored, and a variable that is unset or empty loads nothing of its kind.

The gateway refuses to start when a file

- does not implement the world its directory expects, or declares a `kind` other than that directory's;
- declares a name that is already taken in that kind, by another file or by a built-in;
- declares an ABI the gateway does not host (it hosts 1);
- has a manifest that is not a JSON object with the fields below, or has an unknown field, an invalid `kind` or a `name` outside `^[a-z][a-z0-9_]{0,62}$`;
- has a `title` over 200 characters, a `description` over 2000, or a control character in either;
- has a schema over 16 KiB or outside the subset, router metadata on a kind that has none or missing on a router strategy, or an unknown `needs` signal;
- cannot be loaded as a component at all.

A configured directory that is missing or unreadable is fatal too. The error names the file and the reason; a gateway never skips one silently.

There is no hot reload. The files are read once, so replacing a plugin means writing the new file and restarting the process (the pod, under Kubernetes). A call never reads a directory.

`lumewarp plugins` prints the numeric proto kind, name and title of every plugin the process would have, built-ins included; `--json` prints the descriptor array it reports in `Hello`. It reads the directories without contacting the control plane, so it is the check to run against a candidate file set before deploying.

Standalone YAML and a snapshot name plugins by the same names: `routers[].strategy` for a router, and the plugin reference in an MCP server's auth or an A2A agent's middleware. The same load-time checks apply in both modes.

## The manifest

`manifest()` returns one JSON object. A router strategy with settings:

```json
{
  "abi": 1,
  "kind": "router-strategy",
  "name": "cheapest_first",
  "title": "Cheapest first",
  "description": "Use the eligible member with the lowest declared input price.",
  "params_schema": {
    "type": "object",
    "properties": {
      "max_input_price_nano_usd_per_mtok": {
        "type": "integer",
        "title": "Max input price (nano-USD per Mtok)",
        "minimum": 1
      }
    },
    "additionalProperties": false
  },
  "router": {
    "uses_weight": false,
    "needs": [],
    "member_schema": {
      "type": "object",
      "properties": {
        "input_price_nano_usd_per_mtok": {
          "type": "integer",
          "title": "Input price (nano-USD per Mtok)"
        }
      },
      "additionalProperties": false
    }
  }
}
```

| Field | Meaning |
| --- | --- |
| `abi` | `1`: the plugin ABI, which is the major version of the WIT package. |
| `kind` | `router-strategy`, `upstream-auth` or `a2a-middleware`. Must match the world the file implements and the directory it is in. |
| `name` | `^[a-z][a-z0-9_]{0,62}$`, unique within the kind, built-ins included. The suffix of the capability. |
| `title`, `description` | Shown in the UI, as plain text. `title` is at most 200 characters, `description` at most 2000, and neither may contain control characters; a manifest over either refuses to load. |
| `params_schema` | JSON Schema of the settings `configure` receives: `Router.params_json` for a router strategy, `PluginRef.params_json` for the other two kinds. At most 16 KiB, inside the subset below. |
| `router` | Router strategies only; absent for the other kinds. |
| `router.uses_weight` | Whether member weights matter. `false` hides them in the UI; the gateway keeps them either way. |
| `router.needs` | Gateway signals the strategy reads: `latency`, `vllm_load`. Static, so it is known without configuring anything. |
| `router.member_schema` | JSON Schema of `RouterMember.params_json`, same limits. |

The Rust SDK builds this object from `register!` and `schemars`, so a manifest cannot drift from the code that reads the settings; see [Writing a plugin](#writing-a-plugin).

### What the gateway reports

`Hello.plugins` carries a `PluginDescriptor` per plugin, built-ins included with `builtin: true` and an empty `wasm_sha256`. A router descriptor flattens the manifest (`params_schema_json`, `router.uses_weight`, `router.needs`, `router.member_schema_json`); the other kinds have no `router`. The descriptor adds the hex sha256 of the file and a `digest` that changes whenever any other field does. `Hello.plugin_abi` carries the ABI. `supported_capabilities` gains `strategy:<name>`, `auth:<name>` or `a2a_middleware:<name>` for every plugin.

`digest` and `wasm_sha256` are diagnostic: nothing verifies them against the content, and nothing is trusted, cached or skipped because of them.

### Control-plane validation

A write is a router, an MCP server or an A2A agent that names a plugin. Every path that saves one — and a rollback, which restores stored ones — applies the same rule, for all three kinds:

| Situation | What the write gets |
| --- | --- |
| No gateway reports it and it is not built in | 422. `force` does not help. |
| A connected gateway lacks it | 422 listing them. `force: true` saves and audits a forced save naming the missing gateways. That gateway NACKs the whole version, so it stops receiving every config change, key revocations included, until it is upgraded or the reference to the plugin goes away. |
| Gateways with different schemas (rolling upgrade) | The settings must pass every connected gateway's schema. Forms show the newest connected one. |
| No gateway connected | Built-ins validate against the descriptors committed with the control plane; other plugins against the most recent report from any gateway. |
| Stored settings fail a schema after an upgrade | The entity reads `config_invalid: true` with `config_problems`. The data is not altered, and writes that do not touch the plugin or its settings still save. |

Which gateways count: a gateway counts while the token of its latest `Hello` is unrevoked and it is connected or was seen in the last 30 days. A connected gateway has what its latest `Hello` lists; an offline one what it last reported. A report counts while the token that made it is unrevoked. Each `Hello` replaces the reports under the gateway's session row lock, and a reader sees one `Hello`, never parts of two.

Each gateway counts for at most 128 plugins per kind: descriptors first, then that kind's capabilities, with the names above. The rest of a `Hello` is ignored, logged and counted by the control plane; the stream stays up.

Schemas are untrusted data from a gateway: capped at 16 KiB each, restricted to the subset before use — so there is no `$ref` to fetch and no regex to run — and rendered only as text. A bad schema in `Hello` never affects the stream.

### Compatible changes

Under one name, change a schema only by **adding optional fields**. Anything else — removing or renaming a field, making one required, narrowing a type or range — needs a **new plugin name**: stored settings under the old name would turn `config_invalid`, and gateways on the new build would NACK them.

A change that leaves the schema alone is free: rewrite the body, rebuild the file, and the descriptors differ only in `digest` and `wasm_sha256`. Gateways may run different versions of the same plugin while an upgrade rolls out; stored settings that pass every connected gateway's schema keep working.

## Schema subset

The SDK derives JSON Schema from the same structs `configure` deserializes, with `schemars`, and normalizes it. `Option<T>` is an optional `T`; an integer `format` becomes `minimum`/`maximum` bounds, intersected with any declared ones, so they match the Rust integer type; a nested struct is inlined; doc comments become `description`.

**Schema annotations create no runtime checks.** `#[schemars(range(min = 1))]` changes only the emitted schema: serde ignores it, and `configure` is called with whatever deserializes. A snapshot and standalone YAML both reach `configure` without passing the schema. `configure` must enforce every limit the schema states, and a boundary payload list through each path is how to prove it, as `gateway/plugins/router/priority-failover/testdata/boundaries.json` does.

| Allowed | |
| --- | --- |
| Types | `string`, `integer`, `number`, `boolean`, arrays of strings, one level of nested objects |
| Keywords | `title`, `description`, `default`, `required`, `enum` (string, integer, number), `minimum`, `maximum`, `additionalProperties: false`, `x-lumewarp-widget` on a string array |

Everything else is outside it: `HashMap`, `Vec<u32>`, enums with data, `regex`, `length`, two levels of nesting, any `format` other than the integer ones (`Ipv4Addr` emits `format: ipv4`, which the control plane would not check), a range the type cannot hold or `minimum` above `maximum` (`range(max = 1000)` on a `u8` narrows to 255; `range(min = 300)` on it has no valid value). The test `register!` generates fails `cargo test` in the plugin crate, naming the field. Add `assert_schema_subset::<T>()` yourself for a helper type.

An `Option<T>` deserializes `null` as absent, but its schema says only `T`, so the control plane refuses an explicit `null`: leave the field out.

If a schema outside the subset or above 16 KiB reaches the control plane, it is not used: the UI falls back to a JSON editor and only `configure` validates those settings. Properties are listed by name, not in declaration order, because JSON objects are stored sorted.

## Sandbox

Plugins run as wasmtime components. Instances are pooled: a call checks one out, and two calls never run in one instance at the same time. An instance is configured from the stored settings when it is created, not per request.

A request-path plugin call is cancelled by its configured timeout or when the request goes away. The timeout defaults to 60 s and is set per use; there is no fixed request-path limit in code other than that default. WASM calls run asynchronously and yield through the epoch mechanism, so a slow plugin delays only the request that called it and never occupies a tokio worker. An infinite loop therefore holds that request until the timeout expires or its client disconnects. Slowness is visible in `lumewarp_plugin_duration_seconds`, not as an error result.

The timeout is configured like an LLM route's: `Router.plugin_timeout_ms` for a WASM router strategy, and `PluginRef.timeout_ms` for an MCP server's or A2A agent's `auth` and for an A2A agent's `middleware`; `0` means 60 s. Built-in plugins ignore it, and the UI exposes the field on each form. These are plugin call timeouts, not the MCP server's or A2A agent's `timeout_ms` (the upstream HTTP request timeout). The async call is wrapped in a timeout; when it expires the in-flight call is dropped, that Store is discarded, and the call is counted `timeout`. Waiting for a pool instance counts towards the timeout. An auth plugin's `http-fetch` runs inside the auth call's timeout.

Each instance is capped at 32 MiB of memory. All plugin instances together share a process-wide budget, 1 GiB by default, set with `LUMEWARP_PLUGIN_MEMORY_LIMIT_BYTES`; the Store limits also bound table elements, tables and instances, and an over-limit `table.grow` returns -1. The number of idle instances is bounded, and one target's pool may run at most a quarter of the calls the budget admits, so a stuck plugin cannot take that gate from the others.

Instances are reused across requests and across keys. A guest's globals, statics and heap state persist for the life of the instance, so a plugin must not rely on them for correctness and must not keep a key's, team's or request's data in them: two calls in one instance can belong to different keys. Only configuration-derived values computed in `configure` belong in long-lived state; do not add request- or key-derived data to it.

No WASI implementation is linked. The host links every `wasi:*` import a component declares to a stub that traps when called, so a component built with the normal Rust `wasm32-wasip2` standard library still loads, but it has no filesystem, network, environment variables, clock or entropy: a call into one of those imports traps. The only other imports a component may declare are `lumewarp:plugin/types@1.0.0` and, for `upstream-auth` only, the `host` interface; any other unknown import refuses the plugin at startup. A component never touches a socket, a file or the process environment directly.

The guest also traps if it reaches WASI for entropy or time: `HashMap::new()` (its `RandomState` asks `wasi:random`), `println!` (stdout) and `Instant::now()` (clock) all trap. Use a fixed hasher (`BuildHasherDefault`) or `BTreeMap`; only an `upstream-auth` plugin has `host.now-unix-ms()` for time.

Failure behaves per kind:

| Kind | A trap | A timeout | An error |
| --- | --- | --- | --- |
| `router-strategy` | Treated as `pick` returning `none`: the request takes the gateway's fall-through paths. | Same fall-through as a trap. | A `pick` cannot return an error. |
| `upstream-auth` | The client gets 502 and the call counts as an auth failure. | The client gets 504. | An `Err` from `authorize`, or a header the gateway refuses, gets 502 and counts as an auth failure. |
| `a2a-middleware` | The client gets 502; when the response stream is already under way, its last event is a JSON-RPC error instead. | The client gets 504; when the response stream is already under way, its last event is a JSON-RPC error instead. | The hook's `rpc-error`, or a result the gateway refuses, is sent as a JSON-RPC error; on a stream already under way, as its last event. |

A breaker pauses a plugin after consecutive trap, error or timeout results; while it is open, a router `pick` does not run and takes the fall-through paths.

Calls are counted and timed:

| Metric | |
| --- | --- |
| `lumewarp_plugin_calls_total{kind,plugin,result}` | Every request-path call into a plugin. `result` is `ok`, `error` (the plugin returned an error), `trap` or `timeout` (the call's configured timeout expired). A router `pick` returning `none` because it rejected every candidate is `ok`: it answered. |
| `lumewarp_plugin_duration_seconds{kind,plugin}` | Histogram of the same calls, in seconds; a slow plugin is visible here. |

`manifest` and `configure` run at startup or when a version is applied and are not counted in these metrics. Built-ins run inline and are not counted. No label carries a key, team or request, so cardinality is fixed by the loaded plugins. Request-path calls also appear under the request's span in a trace.

## Router strategies

`configure` runs once per router when a snapshot is applied, and when standalone YAML is loaded. An error refuses the whole version — a NACK `INVALID_CONFIG` — and the plugin's text is never part of the NACK, because a plugin can put its settings in it. Parse and precompute here, not in `pick`. `configure` receives every member of the router through all its traffic splits (`router-info.members`), in declaration order. The value it returns lives as long as the router's settings and is dropped when the router changes or disappears.

`pick` runs on the request path, once per attempt: no I/O, deterministic given the candidates, the request and the RNG draws the gateway supplies for that call. Return a candidate's `index`, or `none` when none is acceptable; the gateway then takes its fall-through paths (another attempt, a fallback, or the error a request with nothing left gets). The candidates are the eligible models only. The `pick` timeout is `Router.plugin_timeout_ms` (0 = 60 s; built-in strategies ignore it); a timeout is treated like a trap and takes the same fall-through paths.

**`member-index`, not `index`, for member tables** (`member_index()` and `index` in the SDK). `index` is the position in the current pool, which differs per traffic split, and it is what `pick` returns. `member_index()` is the position in `router-info.members` and in the member list `configure` received; it counts through all splits in order, so a model listed in two splits is two members with their own settings.

`router.needs` decides what the gateway collects: `latency` fills `latency-ewma-us`, `vllm_load` fills `load`. A signal that is not declared is absent on every candidate, and an absent signal is the plugin's own case to handle.

Request headers are opt-in through a reserved field. `params.headers` lists the header names the strategy may read; it belongs to the platform, so the gateway reads it and removes it before `configure`, and a struct does not declare it — `deny_unknown_fields` does not reject it. `request-info.headers` holds those names only, lowercased. Credential headers (`authorization`, `proxy-authorization`, `cookie`, `set-cookie`, `x-api-key`) are refused by the control plane and the gateway whatever a schema says.

To give the UI a header picker for a field of your own, mark it:

```rust
#[schemars(extend("x-lumewarp-widget" = "header-list"))]
#[serde(default)]
pub copy_headers: Vec<String>,
```

The control plane applies the same credential rule to such a field. There is deliberately no prompt or body interface.

## Upstream auth

An upstream-auth plugin produces the headers the gateway sends to one MCP server (`McpServer.auth`) or one A2A agent (`A2aAgent.auth`). LLM upstreams are not involved: they keep `api_key_ref` or a sealed key.

Every outbound request is prepared by one header policy. The gateway removes the client's `X-Lumewarp-Key`, `Cookie` and `Proxy-Authorization`. It removes `Authorization` too, unless the selected built-in is `passthrough`, which forwards it unchanged. A WASM plugin therefore starts from a request with no client credentials and returns headers that are added, replacing same-named ones. A returned header that is hop-by-hop, or is `Host`, `Content-Length`, `Cookie`, `Mcp-Session-Id`, `MCP-Protocol-Version`, `A2A-Version`, `Last-Event-ID` or starts with `X-Lumewarp-`, is refused: the call counts as an auth failure and the client gets 502.

| Built-in | Behaviour |
| --- | --- |
| `none` | Adds no credential header. |
| `static_bearer` | Sends `Authorization: Bearer <token>`, from `UpstreamAuth.secret_ref` (the name of a gateway environment variable) or `UpstreamAuth.secret_ciphertext` (sealed like `Upstream.api_key_ciphertext`, HKDF info `lumewarp/upstream-auth-secret/v1`). The ciphertext is opened when the snapshot is applied, never per request. |
| `passthrough` | Forwards the client's `Authorization` unchanged, so the upstream sees the user's own token. Its result is never cached. |

A WASM plugin is configured once per target, not once per request; a `configure` error refuses the version, as for a router strategy. `authorize` is called for a request whose headers are not cached. It returns the headers to add — replacing same-named ones — and `cache-ms`:

- The cache key is the traffic (`mcp` or `a2a`), the target id (`McpServer.id` or `A2aAgent.id`) and the digest of the plugin's settings. Applying a snapshot with different settings is a new key; deleting the target drops its entries.
- `cache-ms` bounds how long the gateway reuses the result without calling `authorize` again. `0` means do not cache, and is required whenever the result depends on the request — a body signature, for instance. The gateway caps the value at one hour. Nothing renews an entry, so a rotation or a revocation upstream is not noticed until it expires: keep the value as short as the exchange costs.

The call timeout is `McpServer.auth.plugin.timeout_ms` or `A2aAgent.auth.plugin.timeout_ms` (0 = 60 s; built-in auth plugins ignore it). It is not the server's or agent's `timeout_ms`, which bounds the upstream HTTP request.

The `host` interface, for this kind only:

| Function | |
| --- | --- |
| `http-fetch(request)` | One HTTP request for a token exchange or a verification call. The URL must be under an origin listed in the reserved `params.allowed_origins` (absolute, no path); anything else fails without a connection. Redirects are not followed, and response bodies are truncated at 1 MiB. It runs inside the auth call's timeout. |
| `secret(name)` | The value of the environment variable `LUMEWARP_PLUGIN_SECRET_<NAME>`, or `none` when it is unset or not allowed. `NAME` matches `^[A-Z0-9_]+$`, and only variables with the `LUMEWARP_PLUGIN_SECRET_` prefix are visible; the host does not scope them per plugin, so put the plugin's name in the name, as in `LUMEWARP_PLUGIN_SECRET_HMAC_SIGNATURE_KEY`. |
| `now-unix-ms()` | The current time, for a signed timestamp. |

`params.allowed_origins` is reserved in the same way as `params.headers`: the gateway reads it and removes it before `configure`, so a struct does not declare it. Without it a plugin cannot fetch anything.

**No secrets in params.** Settings go into the snapshot, the API and the audit trail, and every gateway persists them to disk. Never put a credential there: `static_bearer` takes its token from `secret_ref` or `secret_ciphertext`, a WASM plugin reads `LUMEWARP_PLUGIN_SECRET_*` through `secret()`. `allowed_origins` names origins, not credentials.

A plugin never sees the client's credentials: `authorization`, `x-lumewarp-key`, `cookie` and `proxy-authorization` are absent from `outbound-request`. `passthrough` is a built-in, so no plugin is involved in it.

## A2A middleware

At most one per agent: `A2aAgent.middleware`, which requires `a2a_middleware:<name>`. `configure` runs once per agent when a configuration is applied, and the value is dropped when the agent changes or disappears. An error refuses the version, as for a router strategy. The call timeout is `A2aAgent.middleware.timeout_ms` (0 = 60 s).

The pipeline for one request is fixed: authenticate the key, check the agent grant, parse the JSON-RPC request and require A2A v1.0 (`A2A-Version: 1.0`; missing or other gets `-32009`), unwrap task and context ids, run `on-request`, re-check its result, remove the client's credential headers, run the outbound auth, and send. The re-check refuses, as a JSON-RPC error, a result that

- changes the method (the body's method must equal the `method` argument);
- adds or removes a task or context id field;
- adds push-notification configuration;
- is not a JSON-RPC message of the same shape; or
- exceeds the L10 body bound (4 MiB for a request, 16 MiB for a non-streaming response, 1 MiB per SSE event).

Hooks see upstream ids: the gateway unwraps ids before `on-request` and wraps them after `on-response` and `on-stream-event`, so a hook never sees an `lwt.` id. Id fields are the A2A `taskId`, `contextId`, `referenceTaskIds` and the `id` a task request names; the check does not rewrite arbitrary JSON values. On a response, the output's set of task and context ids must be a subset of the input's: removing an id is allowed, adding one is refused. Each SSE event is handled on its own.

| Hook | |
| --- | --- |
| `on-request(method, body)` | The JSON-RPC request body before it goes upstream, with upstream ids. Return the body to send. |
| `on-response(method, body)` | A whole, non-streaming JSON-RPC response body, with upstream ids. Return the body to send back. |
| `on-stream-event(method, data)` | One SSE event's `data`: a JSON-RPC response object of a streaming method, with upstream ids. Return the data to send, or `none` to drop the event. |

`method` is the v1.0 JSON-RPC method name (`SendMessage`, `SendStreamingMessage`, `GetTask`, `ListTasks`, `CancelTask`, `SubscribeToTask`, `GetExtendedAgentCard`).

A hook returns an `rpc-error` instead of a body: the client receives a JSON-RPC error with that `code` and `message`, and an `on-request` error stops the call before it goes upstream. When the stream is already under way, the error is sent as its last event. A trap or a timeout fails as [Sandbox](#sandbox) describes.

Dropping an event is how a middleware removes content — an artifact part of a given MIME type, for example — without rewriting the ones around it. Only a `TaskArtifactUpdateEvent` may be dropped; returning `none` for any other event type is an error, because the task and status events around it carry the stream's state.

A middleware sees the method and the bodies, and nothing else: no headers, no settings beyond its own params, no I/O.

## MCP definition review

The gateway learns a server's tools and prompts from the upstream, and decides which of them to serve. A snapshot carries each known definition's name, hash and state — approved, pending or rejected; an unspecified state counts as pending — never the definition itself. Under `tofu` and `manual` the gateway serves only what the upstream sent, and it drops a definition whose hash the snapshot does not approve — except a TOFU first sight.

### Definition hash

The hash is `sha256` over RFC 8785 (JCS) canonical JSON of a projection of the definition object:

| Definition | Keys kept |
| --- | --- |
| Tool | `name`, `title`, `description`, `inputSchema`, `outputSchema`, `annotations` |
| Prompt | `name`, `title`, `description`, `arguments` |

A key the upstream omitted is omitted from the projection; every other key it sent — `_meta` and anything vendor-specific — is dropped before hashing. The result is 32 bytes, stored in `McpDefinition.hash` and `ObservedDefinition.hash`. The server name is not part of it: (server, kind, name, hash) identifies a definition. The control plane recomputes the hash from the reported definition; it does not trust the hash a gateway supplies.

### Review modes

`McpServer.review_mode` decides what a list response shows. `MCP_REVIEW_MODE_UNSPECIFIED` means `tofu`. `MCP_DEFINITION_STATE_UNSPECIFIED` counts as pending: the name is known and its hash is not approved.

| Mode | A name absent from the snapshot | A known name with a new hash |
| --- | --- | --- |
| `tofu` (default) | Served and reported as auto-approved | Withheld until approved |
| `manual` | Withheld until approved | Withheld until approved |
| `off` | Served | Served |

A withheld definition is removed from the `tools/list` or `prompts/list` response.

A call (`tools/call`, `prompts/get`) is gated separately, and hiding a changed definition from a list is not enough to pass it. Under `tofu` and `manual`, a call is allowed only when the last hash the gateway observed for that (server, kind, name) is approved. If the gateway has never observed the name, the snapshot decides: an approved definition allows the call; a name the snapshot knows only as pending (including unspecified) or rejected is refused; a name the snapshot does not know is a first sight under `tofu` and served, and refused under `manual`. `off` never refuses. The gateway keeps the last observed hash in memory per (server, kind, name) and drops it when the server leaves the snapshot. A definition the control plane does not approve stays withheld while the upstream keeps sending it.

TOFU is a local decision: a gateway serves a first sight at once and reports it, so two gateways can briefly serve different definitions of the same new name before the first report reaches the control plane and a snapshot reaches the others. The control plane records the first report for a name (first writer wins) and treats any different hash that arrives after that as pending. This detects a changed definition; it does not attest what the upstream executes, and a definition that never changes can still behave differently.

### Catalog reports

The control plane cannot fetch definitions itself when a server uses a WASM auth plugin or a user's token, so the gateway reports what it observed in `CatalogReport`, on the same config stream as `HealthReport`:

- every definition it withheld, and
- every definition `tofu` approved on first sight, once, so the control plane has the tool list that grants are written against.

Each report adds to what was reported before; unlike `HealthReport` it does not replace it. A definition report carries the server name and id, the kind, the upstream name, the canonical definition JSON and its hash, the observation time, `auto_approved`, and the config version the gateway was serving. A card report carries the agent name and id, the card, the fetch time or error, and the same config version. The control plane discards a report whose id no longer matches the name, whose `config_version` it never published, or whose version was compiled before the target's last write; it honours `auto_approved` only when the name is also new to it and the server is not `MANUAL`.

The bounds: a definition is at most 64 KiB, a gateway holds at most 200 unreported definitions per server, and the same (server, hash) is reported at most once per gateway per 10 minutes. Anything beyond that is dropped and counted in `lumewarp_mcp_catalog_reports_dropped_total`. Reports are untrusted data and are shown only as text.

### Discover now

For a server whose auth the control plane can reproduce — `none`, `static_bearer` or `passthrough` — the edit page can fetch `tools/list` and `prompts/list` once. `none` needs no token; `static_bearer` uses the server's stored secret (an environment reference or the sealed ciphertext) or a token an administrator pastes for that call; `passthrough` needs the administrator to paste the user's token. A pasted token is used once, not stored, not audited and not returned. The results are recorded like observed definitions, with the same hashes, so they can be approved before traffic has seen them, but only when the URL and the saved configuration are unchanged while the call runs; otherwise the answer is a preview and nothing is recorded.

A server behind a WASM auth plugin shows "observable from traffic only": the control plane does not run WASM, so those definitions arrive only in `CatalogReport`.

### Agent cards

The gateway fetches an A2A agent's card from `A2aAgent.card_url` (empty means the origin of `url` plus `/.well-known/agent-card.json`) with the agent's outbound auth, every 5 minutes by default, and reports it in `CatalogReport.cards` with the fetch time or the error. The credential is attached only when `card_url` is on the same origin as `url`; a card URL on another origin is fetched without it. The report carries the card as the gateway serves it (the rewrite), so the control plane displays what clients receive; a failed fetch keeps the last card that arrived.

A `passthrough` agent has no token of its own, so its fetch usually fails: an administrator pastes the card, which is stored as `A2aAgent.card_json` and used when the gateway has no fetched card. The card the gateway serves to clients is its own rewrite, as described in [MCP and A2A endpoints](#mcp-and-a2a-endpoints).

## Writing a plugin

`gateway/crates/lumewarp-plugin-sdk` has the WIT bindings, `register!`, the schema helpers and the subset assertion. A router strategy:

```rust
use lumewarp_plugin_sdk::{
    Candidates, ConfigError, Configure, Needs, RequestInfo, RngCore, RouterInfo, Strategy,
};
use schemars::JsonSchema;
use serde::Deserialize;

#[derive(Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct Params {
    /// Skip members with more requests in flight than this.
    #[schemars(title = "Max in flight", range(min = 1))]
    pub max_in_flight: Option<u32>,
}

#[derive(Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct MemberParams {
    /// Lower numbers are used first.
    #[schemars(title = "Priority", range(min = 1))]
    pub priority: u32,
}

pub struct PriorityFailover {
    max_in_flight: usize,
    /// By `Candidate::member_index`: stable across traffic-split pools.
    priorities: Box<[u32]>,
}

impl Configure<Params, MemberParams> for PriorityFailover {
    fn configure(
        _: &RouterInfo<'_>,
        params: Params,
        members: &[MemberParams],
    ) -> Result<Self, ConfigError> {
        // The annotation above creates no runtime check: standalone YAML skips the schema.
        if params.max_in_flight == Some(0) {
            return Err(ConfigError("max_in_flight must be at least 1".into()));
        }
        if members.iter().any(|m| m.priority == 0) {
            return Err(ConfigError("priority must be at least 1".into()));
        }
        Ok(Self {
            max_in_flight: params.max_in_flight.map_or(usize::MAX, |max| max as usize),
            priorities: members.iter().map(|m| m.priority).collect(),
        })
    }
}

impl Strategy for PriorityFailover {
    fn pick(
        &self,
        _: &RequestInfo<'_>,
        candidates: &Candidates<'_>,
        _: &mut dyn RngCore,
    ) -> Option<usize> {
        candidates
            .iter()
            .filter(|c| c.in_flight() <= self.max_in_flight)
            .min_by_key(|c| self.priorities[c.member_index()])
            .map(|c| c.index)
    }
}

lumewarp_plugin_sdk::register! {
    kind: "router-strategy",
    name: "priority_failover",
    title: "Priority failover",
    description: "Use the lowest priority number that is healthy.",
    uses_weight: false,
    needs: Needs::default(),
    params: Params,
    member_params: MemberParams,
    strategy: PriorityFailover,
}
```

The SDK's router traits are the gateway's read-only policy views, so a policy can be unit-tested natively; the generated component glue converts to and from the WIT records. `Candidate::index` is the pool position `pick` returns, and `Candidate::member_index()` indexes the per-member settings `configure` received.

Each kind has one configuration trait and one behaviour trait:

| Kind | Traits |
| --- | --- |
| `router-strategy` | `Configure<Params, MemberParams>` and `Strategy` |
| `upstream-auth` | `ConfigureAuth<Params>` and `Authenticator` |
| `a2a-middleware` | `ConfigureMiddleware<Params>` and `Middleware` |

`Strategy::pick` takes `&RequestInfo`, `&Candidates` and `&mut dyn RngCore` and returns `Option<usize>`; `Configure::configure` takes `&RouterInfo`, the params and one member-params value per member. `Authenticator::authorize` takes an owned `OutboundRequest` and returns `Result<AuthResult, String>`; `ConfigureAuth::configure` takes `&TargetInfo` and the params. `Middleware`'s hooks take the method as a `String` and the body as `Vec<u8>` and return `Result<Vec<u8>, RpcError>`, or `Result<Option<Vec<u8>>, RpcError>` for the stream event; `ConfigureMiddleware::configure` takes `&AgentInfo` and the params.

| `register!` field | Meaning |
| --- | --- |
| `kind` | Which world the crate implements, and therefore which directory its file belongs in. |
| `name`, `title`, `description` | The manifest fields, with the name rules above. |
| `uses_weight`, `needs` | Router strategies only: the manifest's `router` metadata. Both may be omitted, which means `false` and `Needs::default()`. |
| `params` | The type `configure` receives, and what `params_schema` comes from. `lumewarp_plugin_sdk::NoParams` when there are none. |
| `member_params` | Router strategies only: the per-member type, and what `member_schema` comes from. |
| `strategy` / `authenticator` / `middleware` | The type implementing the kind's trait. |

The other two kinds use the same fields, without `member_params`, `uses_weight` or `needs`. Each has its own `Params` type and a type implementing the kind's traits:

```rust
lumewarp_plugin_sdk::register! {
    kind: "upstream-auth",
    name: "hmac_signature",
    title: "HMAC signature",
    description: "Sign each upstream request with a shared secret (x-hmac-signature).",
    params: Params,
    authenticator: HmacSignature,
}

lumewarp_plugin_sdk::register! {
    kind: "a2a-middleware",
    name: "strip_artifacts",
    title: "Strip artifacts",
    description: "Drop stream events that carry an artifact of a chosen MIME type.",
    params: Params,
    middleware: StripArtifacts,
}
```

`register!` generates the component's `manifest()` and its world exports from these fields and `schemars`, and a `cargo test` test that fails when a schema leaves the subset. Use at most one `register!` per crate: one crate is one component and has one manifest.

### Example and test components

| Path | Kind |
| --- | --- |
| `gateway/plugins/router/priority-failover` | `router-strategy`; unit tests and boundary payloads |
| `gateway/plugins/router/cheapest-first` | `router-strategy`; Python-derived pick table |
| `gateway/plugins/router/loop-test` | `router-strategy`; always loops, for the timeout test |
| `gateway/plugins/router/misbehave-test` | `router-strategy`; answers an ineligible index or hangs in `configure`, for host tests |
| `gateway/plugins/auth/hmac-signature` | `upstream-auth`; signs each request with `x-hmac-signature`, secret from `secret()` |
| `gateway/plugins/auth/noop-test` | `upstream-auth`; returns no headers |
| `gateway/plugins/auth/fixture` | `upstream-auth`; cache, `http-fetch`, `secret`, error and timeout modes |
| `gateway/plugins/a2a-middleware/noop-test` | `a2a-middleware`; passes bodies through |
| `gateway/plugins/a2a-middleware/strip-artifacts/fixtures/misbehave` | `a2a-middleware`; method, id, push, drop and slow modes |

`loop-test`, `misbehave-test`, `noop-test`, `fixture` and the `misbehave` fixtures are test
fixtures, built only with `--tests`; the two router strategies, `hmac-signature` and
`strip-artifacts` are examples built by default.

`cheapest-first` is also the worked example of `.claude/skills/port-router-to-rust/SKILL.md`, which turns a policy that already runs in Python into a plugin crate.

### Build and test

```sh
cd gateway
cargo test --manifest-path plugins/router/priority-failover/Cargo.toml

rustup target add wasm32-wasip2      # once per machine
cargo build --release --target wasm32-wasip2 \
  --manifest-path plugins/router/priority-failover/Cargo.toml
```

`cargo test` runs the schema test and the crate's unit tests: the router examples test `pick` against a table in `testdata/`, and `configure` against boundary payloads, because the schema enforces nothing at runtime.

The example crates are their own Cargo workspace (`[workspace]` in `Cargo.toml`), so the build writes the component to the crate's target directory: `plugins/router/priority-failover/target/wasm32-wasip2/release/lumewarp_plugin_priority_failover.wasm` (the crate name with underscores). Copy it into the directory for its kind and restart the gateway; `lumewarp plugins --json` then shows the descriptor the gateway reports in `Hello`.

`bash gateway/plugins/build.sh` builds the example components it lists (the two router strategies, `hmac-signature` and `strip-artifacts`) and copies them to `gateway/target/plugins/<kind>/<name>.wasm`; `--tests` also builds the six test fixtures into `gateway/target/plugins-test/<kind>/`. A new crate is built with the `cargo build` command above.

Deploy the plugin to every gateway first. A gateway without it NACKs every version that references it, and the control plane refuses a write that names it unless the write is forced; see [Control-plane validation](#control-plane-validation).

`bench/` compares Lumewarp with LiteLLM running the same routing policy and reports the difference; it sets no plugin performance budget. The A6 scope-overhead workload shows what a WASM strategy costs on the request path.
