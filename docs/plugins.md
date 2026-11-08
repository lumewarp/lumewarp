# Gateway plugins

A plugin is a WebAssembly component that changes one behaviour of the data plane: which member of a router serves a request, how the gateway authenticates to an MCP server or an A2A agent, or what an A2A response contains. It is loaded from a directory at startup, so shipping one is a file and a restart, not a gateway build; the Rust SDK targets `wasm32-wasip2`. `gateway/wit/plugin.wit` fixes the function signatures; this page is the reference for writing a plugin and for the host that runs it.

Built-ins are native code inside the gateway, reported as plugins so the control plane and the UI handle them the same way.

## Kinds and capabilities

| `kind` | World | Directory | Capability | Built-ins |
| --- | --- | --- | --- | --- |
| `router-strategy` | `lumewarp:plugin/router@1.0.0` | `LUMEWARP_ROUTER_PLUGIN_DIR` | `strategy:<name>` | `weighted`, `least_in_flight`, `lowest_latency`, `vllm_load` |
| `upstream-auth` | `lumewarp:plugin/auth@1.0.0` | `LUMEWARP_AUTH_PLUGIN_DIR` | `auth:<name>` | `none`, `static_bearer`, `passthrough` |
| `a2a-middleware` | `lumewarp:plugin/middleware@1.0.0` | `LUMEWARP_A2A_MIDDLEWARE_DIR` | `a2a_middleware:<name>` | none |

One file is one plugin: it implements exactly one world and exports `manifest()`. A name is configuration — a router stores it in `strategy`, an MCP server or A2A agent in `auth.plugin` and `middleware.plugin` — so renaming or repurposing one is a breaking change.

The capability is how a snapshot gates the plugin: a gateway advertises `<kind>:<name>` for every plugin it has (built-ins included), and a snapshot that requires a capability it does not advertise is NACKed `UNSUPPORTED_FEATURE`. See "Routing capabilities" in [config-protocol.md](config-protocol.md).

A router strategy runs on the LLM request path, so the four built-in strategies are dispatched inline; an LLM request reaches WebAssembly only when a router names a WASM strategy. Traffic splits, retries, fallbacks, stickiness and circuit breakers belong to the gateway, never to a strategy.

## Loading

The gateway reads the three directories during startup, before it applies any configuration (a persisted snapshot in control-plane mode, the YAML in standalone mode). Every file whose name ends in `.wasm` in a directory is loaded, its `manifest()` is called once, and the result is checked; other files are ignored, and a variable that is unset or empty loads nothing of its kind.

The gateway refuses to start when a file

- does not implement the world its directory expects, or declares a `kind` other than that directory's;
- declares a name that is already taken in that kind, by another file or by a built-in;
- declares an ABI the gateway does not host (it hosts 1);
- has a manifest that is not a JSON object with the fields below, or has an unknown field, an invalid `kind` or a `name` outside `^[a-z][a-z0-9_]{0,62}$`;
- cannot be loaded as a component at all.

A configured directory that does not exist is fatal too. The error names the file and the reason; a gateway never skips one silently.

There is no hot reload. The files are read once, so replacing a plugin means writing the new file and restarting the process (the pod, under Kubernetes). A call never reads a directory.

`lumewarp plugins` prints the kind, name and title of every plugin the process would have, built-ins included; `--json` prints the same descriptors it reports in `Hello`. It reads the directories without contacting the control plane, so it is the check to run against a candidate file set before deploying.

Standalone YAML and a snapshot name plugins identically: `routers[].strategy`, and the `plugin` field of an upstream auth or an A2A middleware. The same load-time checks apply in both modes.

The Helm chart mounts one directory per kind into the gateway pod and sets the three variables; its values decide which file goes where.

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
| `title`, `description` | Shown in the UI, as plain text. |
| `params_schema` | JSON Schema of the settings `configure` receives: `Router.params_json` for a router strategy, `PluginRef.params_json` for the other two kinds. At most 16 KiB, inside the subset below. |
| `router` | Router strategies only; absent for the other kinds. |
| `router.uses_weight` | Whether member weights matter. `false` hides them in the UI; the gateway keeps them either way. |
| `router.needs` | Gateway signals the strategy reads: `latency`, `vllm_load`. Static, so it is known without configuring anything. |
| `router.member_schema` | JSON Schema of `RouterMember.params_json`, same limits. |

The Rust SDK builds this object from `register!` and `schemars`, so a manifest cannot drift from the code that reads the settings; see [Writing a plugin](#writing-a-plugin).

### What the gateway reports

`Hello.plugins` carries a `PluginDescriptor` per plugin, built-ins included with `builtin: true` and an empty `wasm_sha256`. A descriptor flattens the manifest (`params_schema_json`, `router.uses_weight`, `router.needs`, `router.member_schema_json`), adds the hex sha256 of the file and a `digest` that changes whenever any other field does. `Hello.plugin_abi` carries the ABI. `supported_capabilities` gains `<kind>:<name>` for every plugin.

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

Each gateway counts for at most 128 plugins per kind: descriptors first, then that kind's capabilities, with the names above. The rest of a `Hello` is ignored, logged and counted in `lumewarp_cp_gateway_plugins_ignored_total`; the stream stays up.

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

Plugins run as wasmtime components. Instances are pooled: a call checks one out, and two calls never run in one instance at the same time. An instance is instantiated and configured from the stored settings when the pool needs one, not once per request. Each instance is capped at 32 MiB of memory.

No WASI interface is linked: no filesystem, network, environment variables or clock. The only imports are the `host` interface of the `auth` world; routers and middleware have none, and a component that imports anything else does not load. A component never touches a socket, a file or the process environment directly.

Every request-path call has an epoch deadline:

| Kind | Deadline |
| --- | --- |
| `router-strategy` | 1 ms per `pick` |
| `upstream-auth` | 2 s per `authorize`, a `host.http-fetch` inside it included |
| `a2a-middleware` | 50 ms per hook |

Failure behaves per kind:

| Kind | A trap, a missed deadline, or an error |
| --- | --- |
| `router-strategy` | Treated as `pick` returning `none`: the request takes the gateway's fall-through paths. |
| `upstream-auth` | The client gets 502 and the call counts as an auth failure. |
| `a2a-middleware` | The client gets 502; when the response stream is already under way, its last event is a JSON-RPC error instead. |

Calls are counted and timed:

| Metric | |
| --- | --- |
| `lumewarp_plugin_calls_total{kind,plugin,result}` | Every call into a plugin. `result` is `ok`, `error` (the plugin returned an error), `timeout` or `trap`. A router `pick` returning `none` is `ok`: it answered. |
| `lumewarp_plugin_duration_seconds{kind,plugin}` | Histogram of the same calls, in seconds. |

Built-ins run inline and are not counted. No label carries a key, team or request, so cardinality is fixed by the loaded plugins. Calls also appear under the request's span in a trace.

## Router strategies

`configure` runs once per router when a snapshot is applied, and when standalone YAML is loaded. An error refuses the whole version — a NACK `INVALID_CONFIG` — and its text is logged, never sent anywhere, because a plugin can put its settings in it. Parse and precompute here, not in `pick`. `configure` receives every member of the router through all its traffic splits (`router-info.members`), in declaration order. The value it returns lives as long as the router's settings and is dropped when the router changes or disappears.

`pick` runs on the request path, once per attempt: no I/O, no blocking, deterministic given the candidates, the request and `seed`, which is fresh randomness per call. Return a candidate's `index`, or `none` when none is acceptable; the gateway then takes its fall-through paths (another attempt, a fallback, or the error a request with nothing left gets). The candidates are the eligible models only.

**`member-index`, not `index`, for member tables** (`member_index` and `index` in the SDK). `index` is the position in the current pool, which differs per traffic split, and it is what `pick` returns. `member-index` is the position in `router-info.members` and in the member list `configure` received; it counts through all splits in order, so a model listed in two splits is two members with their own settings.

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

A request to an MCP or A2A endpoint is authenticated by a Lumewarp API key in `X-Lumewarp-Key`. The gateway removes that header from every upstream request, and removes the client's `Authorization` unless the built-in `passthrough` is selected.

| Built-in | Behaviour |
| --- | --- |
| `none` | Sends nothing. |
| `static_bearer` | Sends `Authorization: Bearer <token>`, from `UpstreamAuth.secret_ref` (the name of a gateway environment variable) or `UpstreamAuth.secret_ciphertext` (sealed like `Upstream.api_key_ciphertext`, HKDF info `lumewarp/upstream-auth-secret/v1`). The ciphertext is opened when the snapshot is applied, never per request. |
| `passthrough` | Forwards the client's `Authorization` unchanged, so the upstream sees the user's own token. Its result is never cached. |

A WASM plugin is configured once per target when a configuration is applied — a snapshot, or standalone YAML — and `authorize` is called for a request whose headers are not cached. It returns the headers to add — replacing same-named ones — and `cache-ms`:

- The cache key is the target (`traffic` and `name`) plus the digest of the plugin's settings. Applying a snapshot with different settings is a new key; deleting the target drops its entries.
- `cache-ms` bounds how long the gateway reuses the result without calling `authorize` again. `0` means do not cache, and is required whenever the result depends on the request — a body signature, for instance. Nothing renews an entry, so a rotation or a revocation upstream is not noticed until it expires: keep the value as short as the exchange costs.

The `host` interface, for this kind only:

| Function | |
| --- | --- |
| `http-fetch(request)` | One HTTP request for a token exchange or a verification call. The URL must be under an origin listed in the reserved `params.allowed_origins` (absolute, no path); anything else fails without a connection. Response bodies are truncated at 1 MiB. It counts against the `authorize` deadline. |
| `secret(name)` | The value of the environment variable `LUMEWARP_PLUGIN_SECRET_<NAME>`, or `none` when it is unset or not allowed. Only names with that prefix are visible, and the host does not scope them per plugin: put the plugin's name in the name, as in `LUMEWARP_PLUGIN_SECRET_HMAC_SIGNATURE_KEY`. |
| `now-unix-ms()` | The current time, for a signed timestamp. |

`params.allowed_origins` is reserved in the same way as `params.headers`: the gateway reads it and removes it before `configure`, so a struct does not declare it. Without it a plugin cannot fetch anything.

**No secrets in params.** Settings go into the snapshot, the API and the audit trail, and every gateway persists them to disk. Never put a credential there: `static_bearer` takes its token from `secret_ref` or `secret_ciphertext`, a WASM plugin reads `LUMEWARP_PLUGIN_SECRET_*` through `secret()`. `allowed_origins` names origins, not credentials.

A plugin never sees the client's credentials: `authorization`, `x-lumewarp-key`, `cookie` and `proxy-authorization` are absent from `outbound-request`. `passthrough` is a built-in, so no plugin is involved in it.

## A2A middleware

At most one per agent: `A2aAgent.middleware`, which requires `a2a_middleware:<name>`. `configure` runs once per agent when a configuration is applied, and the value is dropped when the agent changes or disappears.

| Hook | |
| --- | --- |
| `on-request(method, body)` | The JSON-RPC request body before it goes upstream. Return the body to send. |
| `on-response(method, body)` | A whole, non-streaming JSON-RPC response body. Return the body to send back. |
| `on-stream-event(method, data)` | One SSE event's `data`: a JSON-RPC response object of a streaming method. Return the data to send, or `none` to drop the event. |

A hook returns an `rpc-error` instead of a body: the client receives a JSON-RPC error with that `code` and `message`, and an `on-request` error stops the call before it goes upstream. When the stream is already under way, the error is sent as its last event. A trap or a missed 50 ms deadline fails as [Sandbox](#sandbox) describes.

Dropping an event is how a middleware removes content — an artifact part of a given MIME type, for example — without rewriting the ones around it. A middleware sees the method and the bodies, and nothing else: no headers, no settings beyond its own params, no I/O.

## MCP definition review

The gateway learns a server's tools and prompts from the upstream, and decides which of them to serve. A snapshot carries only the approved hashes: the gateway filters list responses by them and never serves a definition from the snapshot, so what a client sees always came from the upstream.

### Definition hash

The hash is `sha256` over RFC 8785 (JCS) canonical JSON of a projection of the definition object:

| Definition | Keys kept |
| --- | --- |
| Tool | `name`, `title`, `description`, `inputSchema`, `outputSchema`, `annotations` |
| Prompt | `name`, `title`, `description`, `arguments` |

A key the upstream omitted is omitted from the projection; every other key it sent — `_meta` and anything vendor-specific — is dropped before hashing. The result is 32 bytes, stored in `McpDefinition.hash` and `ObservedDefinition.hash`. The server name is not part of it: (server, kind, name, hash) identifies a definition.

### Review modes

`McpServer.review_mode`:

| Mode | A name seen for the first time | A changed definition of a known name | Calls |
| --- | --- | --- | --- |
| `tofu` (default) | Served, and reported as auto-approved | Withheld until approved | Only tools with an approved definition |
| `manual` | Withheld until approved | Withheld until approved | Only tools with an approved definition |
| `off` | Served | Served | Not checked |

A withheld definition is removed from the `tools/list` or `prompts/list` response. Under `tofu` and `manual`, a `tools/call` that names a tool without an approved definition is refused before it reaches the upstream. A definition the control plane does not approve stays withheld while the upstream keeps sending it.

### Catalog reports

The control plane cannot fetch definitions itself when a server uses a WASM auth plugin or a user's token, so the gateway reports what it observed in `CatalogReport`, on the same config stream as `HealthReport`:

- every definition it withheld, and
- every definition `tofu` approved on first sight, once, so the control plane has the tool list that grants are written against.

Each report adds to what was reported before; unlike `HealthReport` it does not replace it. The bounds: a definition is at most 64 KiB, a gateway holds at most 200 unreported definitions per server, and the same hash is reported at most once per gateway per 10 minutes. Anything beyond that is dropped and counted in `lumewarp_mcp_catalog_reports_dropped_total`. Reports are untrusted data and are shown only as text.

### Discover now

For a server whose auth the control plane can reproduce — `none`, `static_bearer` or `passthrough` — the edit page can fetch `tools/list` and `prompts/list` once, using a token an administrator pastes for that call (`none` needs none). The token is not stored, not audited and not returned. The results are recorded like observed definitions, with the same hashes, so they can be approved before traffic has seen them.

A server behind a WASM auth plugin shows "observable from traffic only": the control plane does not run WASM, so those definitions arrive only in `CatalogReport`.

### Agent cards

The gateway fetches an A2A agent's card from `A2aAgent.card_url` (empty means the origin of `url` plus `/.well-known/agent-card.json`) with the agent's outbound auth, every 5 minutes by default, and reports it in `CatalogReport.cards` with the fetch time or the error. The control plane displays it, and a failed fetch keeps the last card that arrived.

A `passthrough` agent has no token of its own, so its fetch usually fails: an administrator pastes the card, which is stored as `A2aAgent.card_json` and used when the gateway has no fetched card. The card the gateway serves to clients is its own rewrite of it, with URLs pointing at the gateway and upstream signatures removed.

## Writing a plugin

`gateway/crates/lumewarp-plugin-sdk` has the WIT bindings, `register!`, the schema helpers and the subset assertion. A router strategy:

```rust
use lumewarp_plugin_sdk::{
    register, Candidate, ConfigError, Configure, RequestInfo, RouterInfo, Strategy,
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
    /// By `member_index`: stable across traffic splits.
    priorities: Vec<u32>,
}

impl Configure<Params, MemberParams> for PriorityFailover {
    fn configure(
        _: &RouterInfo,
        params: Params,
        members: &[MemberParams],
    ) -> Result<Self, ConfigError> {
        // The annotation above creates no runtime check: standalone YAML skips the schema.
        if params.max_in_flight == Some(0) || members.iter().any(|m| m.priority == 0) {
            return Err(ConfigError::new("max_in_flight and priority must be at least 1"));
        }
        Ok(Self { priorities: members.iter().map(|m| m.priority).collect() })
    }
}

impl Strategy for PriorityFailover {
    fn pick(&self, _: &RequestInfo, candidates: &[Candidate], _seed: u64) -> Option<u32> {
        candidates
            .iter()
            .min_by_key(|c| self.priorities[c.member_index as usize])
            .map(|c| c.index)
    }
}

register! {
    kind: "router-strategy",
    name: "priority_failover",
    title: "Priority failover",
    description: "Use the lowest priority number that is healthy.",
    params: Params,
    member_params: MemberParams,
    strategy: PriorityFailover,
}
```

| `register!` field | Meaning |
| --- | --- |
| `kind` | Which world the crate implements, and therefore which directory its file belongs in. |
| `name`, `title`, `description` | The manifest fields, with the name rules above. |
| `params` | The type `configure` receives, and what `params_schema` comes from. `lumewarp_plugin_sdk::NoParams` when there are none. |
| `member_params` | Router strategies only: the per-member type, and what `member_schema` comes from. |
| `strategy` / `authenticator` / `middleware` | The type implementing the kind's trait. |

The other two kinds use the same fields, with `authenticator` or `middleware` in place of `member_params` and `strategy`:

```rust
register! {
    kind: "upstream-auth",
    name: "hmac_signature",
    title: "HMAC signature",
    description: "Sign the request body with a shared secret.",
    params: Params,
    authenticator: HmacSignature,
}

register! {
    kind: "a2a-middleware",
    name: "strip_artifacts",
    title: "Strip artifacts",
    description: "Drop stream events that carry an artifact of a chosen MIME type.",
    params: Params,
    middleware: StripArtifacts,
}
```

`register!` generates the component's `manifest()` and its world exports from these fields and `schemars`, and a `cargo test` test that fails when a schema leaves the subset. Use at most one `register!` per crate: one crate is one component and has one manifest.

### Examples

| Path | Kind |
| --- | --- |
| `gateway/plugins/router/priority-failover` | `router-strategy` |
| `gateway/plugins/router/cheapest-first` | `router-strategy` |
| `gateway/plugins/auth/hmac-signature` | `upstream-auth` |
| `gateway/plugins/a2a-middleware/strip-artifacts` | `a2a-middleware` |

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

The build writes a component to the build's target directory, `target/wasm32-wasip2/release/lumewarp_plugin_priority_failover.wasm` (the crate name with underscores). Copy it into the directory for its kind and restart the gateway; `lumewarp plugins --json` then shows the descriptor the gateway reports in `Hello`.

Deploy the plugin to every gateway first. A gateway without it NACKs every version that references it, and the control plane refuses a write that names it unless the write is forced; see [Control-plane validation](#control-plane-validation).

`bench/` measures what a WASM strategy costs on the request path: run the A6 scope-overhead workload with the router on the WASM strategy and on a native one on the same machine. The budget is an addition below 50 µs at p99; a strategy that needs more should move work into `configure` or become a built-in.
