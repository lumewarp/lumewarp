# Router strategies

A strategy decides which member of a router serves a request. Four are native code inside the gateway: `weighted`, `least_in_flight`, `lowest_latency` and `vllm_load`. Any other strategy is a WebAssembly component in the directory named by `LUMEWARP_ROUTER_PLUGIN_DIR`, loaded when the gateway starts. Installing or replacing one is a file write and a restart, not a gateway build: the proto, the control plane and the UI stay untouched, and the strategy's form appears from the manifest the component exports.

A strategy defines only its own settings. Names, the member list, weights, retries, fallbacks, traffic splits and stickiness belong to the gateway, so the guarantees around them (retry only before the first response byte, the retry budget) are not re-implemented per strategy.

[Gateway plugins](plugins.md) is the reference for the WIT world, the manifest, the schema subset, the sandbox and how the three plugin directories are loaded. This page is the router-strategy part: the contract, validation on write, building and installing a component, and the bench.

## The contract

A strategy crate depends on `gateway/crates/lumewarp-plugin-sdk`, implements the `lumewarp:plugin/router@1.0.0` world from `gateway/wit/plugin.wit`, and declares itself with one `register!`. `gateway/plugins/router/priority-failover` is the smallest complete crate; `gateway/plugins/router/cheapest-first` is a full example with a Python-derived pick table.

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

| `register!` field | Meaning |
|---|---|
| `kind` | `"router-strategy"`. Must match the directory the file is in; a mismatch refuses startup. |
| `name` | `^[a-z][a-z0-9_]{0,62}$`, unique within the kind, built-ins included. What routers store and the suffix of the capability `strategy:<name>`. |
| `title`, `description` | Shown in the UI, as plain text. `title` is at most 200 characters, `description` at most 2000, and neither may contain control characters. |
| `uses_weight` | Whether `Candidates::weighted` or `Candidate::weight` matter. `false` hides weights in the UI. Weights stay in the gateway either way. |
| `needs` | Signals the gateway must collect: `latency`, `vllm_load`. Static, so it is known without configuring anything. |
| `params`, `member_params` | What `configure` receives, and where `params_schema` and `member_schema` come from. `lumewarp_plugin_sdk::NoParams` when there are none. |
| `strategy` | A type implementing `Strategy` and `Configure<params, member_params>`. |

Use at most one `register!` per crate: one crate is one component and has one manifest. The macro generates the component's `manifest()` and its world exports, plus a `cargo test` test that fails when a schema leaves the subset.

**`configure`** runs once per router when a snapshot is applied, and in standalone mode when the YAML is loaded, for built-ins too. An error refuses the whole version (a NACK with `INVALID_CONFIG`). Its text is dropped because params may be private, so report only what you could log. Precompute tables here. `configure` receives every member of the router through all its traffic splits, in declaration order.

**`pick`** runs on the request path, once per attempt: no I/O, deterministic given the candidates, the request and the RNG draws the gateway supplies for that call, and no allocation in the common case. Candidates are the eligible models only. Return `Candidate.index`, or `None` when none is acceptable; the gateway then takes its fall-through paths. The call is bounded by `Router.plugin_timeout_ms` (0 = 60 s); a timeout is treated like a trap. Built-in strategies ignore the field.

**`member_index()`, not `index`, for member tables.** `Candidate.index` is the position in the current pool, which differs per traffic split. `Candidate::member_index()` is the position in `RouterInfo::members` and in `configure`'s `members` slice. It counts through all splits in order, so a model listed in two splits is two members with their own settings.

There is deliberately no prompt or body interface.

## Schema subset

`register!` derives JSON Schema from the same structs with `schemars` and normalizes it. `Option<T>` is an optional `T`; an integer `format` becomes `minimum`/`maximum` bounds, intersected with any declared ones, so they match the Rust integer type; a nested struct is inlined; doc comments become `description`. The allowed types and keywords, and what falls outside them, are listed under [Schema subset](plugins.md#schema-subset).

**Schema annotations create no runtime checks.** `#[schemars(range(min = 1))]` changes only the emitted schema: serde ignores it, and the gateway calls `configure` with whatever deserializes. Standalone YAML and snapshots reach `configure` without passing the schema, so `configure` must enforce every limit the schema states, as the example does. Test it with one list of boundary payloads through each path, as `gateway/plugins/router/priority-failover/testdata/boundaries.json` does.

## Three layers of validation

1. The UI validates against the schema.
2. The control plane validates every router write with `jsonschema`: params, each member's params, split members too.
3. `configure` is the final authority. `deny_unknown_fields` makes it exact.

| Situation | What a router write gets |
|---|---|
| No gateway reports it and it is not built in | 422. `force` does not help. |
| A connected gateway lacks it | 422 listing them. `force: true` saves and writes the audit action `router.force_save`. That gateway will NACK the whole version, so it stops receiving every config change, key revocations included, until it is upgraded or the router changes. |
| Gateways with different schemas (rolling upgrade) | Must pass every connected gateway's schema. Forms show the newest connected one. |
| No gateway connected | Built-ins validate against the manifest committed with the control plane; custom ones against the most recent report. |
| Stored settings fail a schema after an upgrade | The router reads `config_invalid: true` with `config_problems`. The data is not altered. Writes that do not touch strategy, params, members or splits still save. |

A rollback restores routers from an old version, but the gateways are today's, so every restored router meets the same rules, with the same `force` and the same audit. A rejection is a 422 and changes nothing.

The unit is the gateway *instance*, one process of a gateway id (several may share its token). An instance counts while the token of its latest `Hello` is unrevoked and it is connected or was disconnected less than 24 h ago, when its row is deleted. A report counts while the token that made it is unrevoked; another token of the same instance keeps nothing alive, and counts from its own `Hello` on. A connected instance has what its latest `Hello` lists, an offline one what it last reported. A strategy is available only while every connected instance reports it. Each `Hello` replaces the instance's reports under its session row lock, and a reader sees one `Hello`, never parts of two.

Each gateway counts for at most 128 strategies, descriptors first, then `strategy:<name>` capabilities, with names as in `register!`. The rest of a `Hello` is ignored, logged and counted in `lumewarp_cp_gateway_plugins_ignored_total`; the stream stays up.

Schemas are untrusted data from the gateway: capped at 16 KiB each, restricted to the subset before use (so no `$ref` to fetch and no regex to run), and rendered only as text. A bad schema in `Hello` never affects the stream. The `digest` and `wasm_sha256` only show that a descriptor changed: nothing verifies them against the content and nothing is trusted, cached or skipped because of them.

## Compatible changes

Under one name, change a schema only by **adding optional fields**. Anything else (removing or renaming a field, making one required, narrowing a type or range) needs a **new strategy name**: routers already saved under the old name would turn `config_invalid`, and gateways on the new build would NACK them.

A change that leaves the schema alone is free: rewrite the body, rebuild the file, and the descriptors differ only in `digest` and `wasm_sha256`. Gateways may run different versions of the same strategy while an upgrade rolls out.

## No secrets in params

Params go into the snapshot, the API and the audit trail, and every gateway persists them to disk. Never put a credential there. A WASM router strategy has no secret access at all: unlike `upstream-auth`, the router world has no `secret()` or `http-fetch`.

## Reserved: `headers`

A strategy reads a request header only if it is named in `params.headers`. The field belongs to the platform: the gateway reads it and removes it before `configure`, so structs do not declare it and `deny_unknown_fields` does not reject it. Credentials (`authorization`, `proxy-authorization`, `cookie`, `set-cookie`, `x-api-key`, `x-lumewarp-key`) are refused by the control plane and the gateway regardless of any schema.

To give the UI a header picker for a field of your own, mark it:

```rust
#[schemars(extend("x-lumewarp-widget" = "header-list"))]
#[serde(default)]
pub copy_headers: Vec<String>,
```

The control plane applies the same credential rule to such a field, whatever else the schema says.

## Writing a strategy

1. Start from `gateway/plugins/router/priority-failover`: copy its `Cargo.toml`, `src/` and `testdata/` (not `target/`) to `gateway/plugins/router/<name>/`. The example is its own Cargo workspace (`[workspace]` in `Cargo.toml`), so it is not a member of `gateway/Cargo.toml` and needs no edit there.

   ```toml
   [package]
   name = "lumewarp-plugin-<name>"
   version = "0.1.0"
   edition = "2021"
   license = "Apache-2.0"
   publish = false

   [lib]
   crate-type = ["cdylib", "rlib"]

   [workspace]

   [dependencies]
   lumewarp-plugin-sdk = { path = "../../../crates/lumewarp-plugin-sdk" }
   schemars = { version = "1", features = ["derive"] }
   serde = { version = "1", features = ["derive"] }

   [profile.release]
   lto = true
   panic = "abort"
   opt-level = "z"
   codegen-units = 1
   ```

2. From `gateway/`, run `cargo test --manifest-path plugins/router/<name>/Cargo.toml`. This runs the generated schema-subset test and your unit tests, which call `pick` and `configure` natively through the SDK's re-exported `lookup`/`descriptors` helpers. Compare `pick` against a table generated from your reference implementation, and test `configure` with the same boundary payloads the control plane and standalone YAML will send.

3. Build the component (once per machine, add the target):

   ```sh
   rustup target add wasm32-wasip2
   cd gateway
   cargo build --release --target wasm32-wasip2 \
     --manifest-path plugins/router/<name>/Cargo.toml
   ```

   Because the crate is its own workspace, the file is
   `plugins/router/<name>/target/wasm32-wasip2/release/lumewarp_plugin_<name_with_underscores>.wasm`.

4. Copy it into the directory the gateway reads at startup and restart the gateway. The file name is yours; the plugin name comes from the manifest.

   ```sh
   cp plugins/router/<name>/target/wasm32-wasip2/release/lumewarp_plugin_<name_with_underscores>.wasm \
      /etc/lumewarp/plugins/router/<name>.wasm
   LUMEWARP_ROUTER_PLUGIN_DIR=/etc/lumewarp/plugins/router ./target/debug/lumewarp plugins
   ```

   `plugins` lists the built-ins and every loaded component; `--json` prints the descriptor array the gateway reports in `Hello`. The gateway never reloads a directory: a replaced file needs a process restart. A file that does not implement the router world, declares another kind, reuses a name or has an invalid manifest refuses startup.

5. Deploy the component to every gateway before a router names it. A gateway without it NACKs every version that references it, and the control plane refuses a write that names it unless the write is forced.

6. `bash gateway/plugins/build.sh` builds the committed examples (`priority-failover`, `cheapest-first`) into `gateway/target/plugins/router/`; `LUMEWARP_ROUTER_PLUGIN_DIR=gateway/target/plugins/router` is enough for a local gateway.

In standalone mode, use it from the YAML:

```yaml
routers:
  - name: cost-aware
    strategy: cheapest_first
    params: {}
    members:
      - model: dear-model
        params: {input_price_nano_usd_per_mtok: 300}
      - model: cheap-model
        params: {input_price_nano_usd_per_mtok: 100}
```

## Bench

A WASM strategy costs one host call, the wire copy of the request and the eligible candidates, plus whatever `pick` does; a native built-in does not cross the boundary. Bench it with the real driver (`bench/README.md`), never with a micro-benchmark that skips the gateway.

The bench calls the router named `mock` in `bench/lumewarp/config.yaml`. Make a local, uncommitted edit that points that router at the strategy, e.g.:

```yaml
  - name: mock
    strategy: cheapest_first
    params: {}
    members:
      - model: mock-model
        params: {input_price_nano_usd_per_mtok: 1}
```

Then, from the repo root (the gateway binary itself needs no special feature; it only needs the directory):

    (cd bench/mock-upstream && cargo build --release)
    (cd gateway && cargo build --release -p lumewarp-gateway)
    LUMEWARP_ROUTER_PLUGIN_DIR=$PWD/gateway/target/plugins/router \
      LUMEWARP_BIN=$PWD/gateway/target/release/lumewarp \
      bench/scripts/run.sh --quick --no-build

`--quick` is 1000 RPS for 15 s plus SSE clients; use `--full` for the 300/1000/5000/10000 RPS run. Compare overhead p99 with the same gateway using the native `weighted` strategy, not absolute latency. There is no fixed latency budget for a plugin: slowness is reported, and `lumewarp_plugin_duration_seconds` shows it. Revert the config edit afterwards.

## Porting a Python router

`.claude/skills/port-router-to-rust/SKILL.md` turns a policy that already runs in Python into a component crate: which inputs `pick` may read (and which ones mean it cannot be a strategy), the Python-to-Rust inventory table, the crate skeleton, tests compared against the Python, installing the file and the bench. Its worked example is `gateway/plugins/router/cheapest-first/`, ported from `.claude/skills/port-router-to-rust/example/cheapest_first.py`. `testdata/pick-table.json` is generated by the Python (`example/gen_pick_table.py`), not written by hand, and `tests/e2e/test_strategy_cheapest_first.py` proves it from `Hello` to the member that serves a request.

## Changing a built-in

Built-ins go through the same `register!` declaration in `lumewarp-plugin-sdk`/`lumewarp-router-api` and keep native dispatch, without crossing into WebAssembly. Their descriptor is committed for gateway-less validation. After changing one, with the three plugin directories unset so only built-ins are listed:

    cd gateway && cargo run -p lumewarp-gateway -- plugins --json \
      > ../control-plane/src/lumewarp_control_plane/compiler/builtin_plugins.json

CI diffs the two and fails while they differ.
