# Router strategies

A strategy decides which member of a router serves a request. A new one is a Rust crate and a gateway rebuild: the proto, the control plane and the UI stay untouched, and the strategy's form appears from what the crate declares.

A strategy defines only its own settings. Names, the member list, weights, retries, fallbacks, traffic splits and stickiness belong to the gateway, so the guarantees around them (retry only before the first response byte, the retry budget) are not re-implemented per strategy.

## The contract

`gateway/crates/lumewarp-router-api`, `API_VERSION` 2. `gateway/strategies/priority-failover` is a complete, tested example.

```rust
use lumewarp_router_api::{
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

pub struct PriorityFailover { priorities: Box<[u32]> }

impl Configure<Params, MemberParams> for PriorityFailover {
    fn configure(_: &RouterInfo<'_>, params: Params, members: &[MemberParams]) -> Result<Self, ConfigError> {
        // Annotations above create no runtime check: enforce them here.
        if params.max_in_flight == Some(0) || members.iter().any(|m| m.priority == 0) {
            return Err(ConfigError("max_in_flight and priority must be at least 1".into()));
        }
        Ok(Self { priorities: members.iter().map(|m| m.priority).collect() })
    }
}

impl Strategy for PriorityFailover {
    fn pick(&self, _: &RequestInfo<'_>, c: &Candidates<'_>, _: &mut dyn RngCore) -> Option<usize> {
        c.iter().min_by_key(|m| self.priorities[m.member_index()]).map(|m| m.index)
    }
}

lumewarp_router_api::register! {
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
| `name` | `^[a-z][a-z0-9_]{0,62}$`. What routers store and the capability `strategy:<name>`. Two crates with one name make the gateway refuse it. |
| `title`, `description` | Shown in the UI, as plain text. |
| `uses_weight` | Whether `Candidates::weighted` or `Candidate::weight` matter. `false` hides weights in the UI. Weights stay in the gateway either way. |
| `needs` | Signals the gateway must collect: `latency`, `vllm_load`. Static, so it is known without configuring anything. |
| `params`, `member_params` | The types `configure` receives, and what the schemas come from. `lumewarp_router_api::NoParams` when there are none. |
| `strategy` | A type implementing `Strategy` and `Configure<params, member_params>`. |

Use at most one `register!` per module: it generates a test module with a fixed name.

**`configure`** runs once per router when a snapshot is applied, and in standalone mode when the YAML is loaded, for built-ins too. An error refuses the whole version (a NACK with `INVALID_CONFIG`). Its text is dropped because params may be private, so report only what you could log. Precompute tables here.

**`pick`** runs on the request path: no I/O, no blocking, no allocation in the common case, deterministic given the candidates, request and RNG. Candidates are the eligible models only. Return `Candidate.index`, or `None` when none is acceptable; the gateway then takes its fall-through paths.

**`member_index()`, not `index`, for member tables.** `Candidate.index` is the position in the current pool, which differs per traffic split. `Candidate::member_index()` is the position in `RouterInfo::members` and in `configure`'s `members` slice. It counts through all splits in order, so a model listed in two splits is two members with their own settings.

There is deliberately no prompt or body interface.

## Schema subset

`register!` derives JSON Schema from the same structs with `schemars` and normalizes it. `Option<T>` is an optional `T`; an integer `format` becomes `minimum`/`maximum` bounds, intersected with any declared ones, so they match the Rust integer type; a nested struct is inlined; doc comments become `description`.

**Schema annotations create no runtime checks.** `#[schemars(range(min = 1))]` changes only the emitted schema: serde ignores it, and the gateway calls `configure` with whatever deserializes. Standalone YAML and snapshots reach `configure` without passing the schema, so `configure` must enforce every limit the schema states, as the example does. Test it with one list of boundary payloads through each path, as `priority-failover/testdata/boundaries.json` does.

| Allowed | |
|---|---|
| Types | `string`, `integer`, `number`, `boolean`, arrays of strings, one level of nested objects |
| Keywords | `title`, `description`, `default`, `required`, `enum` (string, integer, number), `minimum`, `maximum`, `additionalProperties: false`, `x-lumewarp-widget` on a string array |

Everything else is outside it: `HashMap`, `Vec<u32>`, enums with data, `regex`, `length`, two levels of nesting, any `format` other than the integer ones (`Ipv4Addr` emits `format: ipv4`, which the control plane would not check), a range the type cannot hold or `minimum` above `maximum` (`range(max = 1000)` on a `u8` narrows to 255; `range(min = 300)` on it has no valid value). The test `register!` generates fails `cargo test` in the strategy crate, naming the field. Add `assert_schema_subset::<T>()` yourself for a helper type.

An `Option<T>` deserializes `null` as absent, but its schema says only `T`, so the control plane refuses an explicit `null`: leave the field out.

If a schema outside the subset or above 16 KiB does reach the control plane, it is not used: the UI falls back to a JSON editor and only `configure` validates those settings. Properties are listed by name, not in declaration order, because JSON objects are stored sorted.

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

A gateway counts while the token of its latest `Hello` is unrevoked and it is connected or was seen in the last 30 days. A report counts while the token that made it is unrevoked; another token of the same gateway keeps nothing alive, and counts from its own `Hello` on. A connected gateway has what its latest `Hello` lists, an offline one what it last reported. Each `Hello` replaces the reports under the gateway's session row lock, and a reader sees one `Hello`, never parts of two.

Each gateway counts for at most 128 strategies, descriptors first, then `strategy:<name>` capabilities, with names as in `register!`. The rest of a `Hello` is ignored, logged and counted in `lumewarp_cp_gateway_strategies_ignored_total`; the stream stays up.

Schemas are untrusted data from the gateway: capped at 16 KiB each, restricted to the subset before use (so no `$ref` to fetch and no regex to run), and rendered only as text. A bad schema in `Hello` never affects the stream. The `digest` only shows that a descriptor changed: nothing verifies it against the content and nothing is trusted, cached or skipped because of it.

## Compatible changes

Under one name, change a schema only by **adding optional fields**. Anything else (removing or renaming a field, making one required, narrowing a type or range) needs a **new strategy name**: routers already saved under the old name would turn `config_invalid`, and gateways on the new build would NACK them.

## No secrets in params

Params go into the snapshot, the API and the audit trail, and every gateway persists them to disk. Never put a credential there. Read secrets from the gateway's environment in `configure`, as `api_key_ref` does for upstreams.

## Reserved: `headers`

A strategy reads a request header only if it is named in `params.headers`, through `RequestInfo::header`. The field belongs to the platform: the gateway reads it and removes it before `configure`, so structs do not declare it and `deny_unknown_fields` does not reject it. Credentials (`authorization`, `proxy-authorization`, `cookie`, `set-cookie`, `x-api-key`) are refused by the control plane and the gateway regardless of any schema.

To give the UI a header picker for a field of your own, mark it:

```rust
#[schemars(extend("x-lumewarp-widget" = "header-list"))]
#[serde(default)]
pub copy_headers: Vec<String>,
```

The control plane applies the same credential rule to such a field, whatever else the schema says.

## Writing a strategy

1. Create `gateway/strategies/<name>/`, depending on `lumewarp-router-api`, `schemars` and `serde`. Workspace members include `strategies/*`.
2. Select it in the gateway, since a crate must be named to be linked: an optional dependency and a feature in `crates/lumewarp-gateway/Cargo.toml`, and in `src/main.rs`:

   ```rust
   #[cfg(feature = "strategy-my-name")]
   use lumewarp_strategy_my_name as _;
   ```

3. `cargo test -p <crate>`. Unit-test `pick` with a small `CandidateSource`; see the example crate.
4. `cargo build --release -p lumewarp-gateway --features strategy-my-name`, then `lumewarp strategies` lists it. `--json` prints the manifest.
5. Deploy. On connecting, the gateway reports the strategy in `Hello` and it shows up in `GET /api/v1/strategies`. Upgrade every gateway serving routers that will use it first.

In standalone mode, use it from the YAML: `routers: [{name: r, strategy: my_name, params: {...}, members: [{model: m, params: {...}}]}]`.

Bench `pick` with `bench/` before relying on it under load: a custom strategy costs one virtual call plus what it does.

## Changing a built-in

Built-ins go through the same declaration in `lumewarp-router-api` and keep enum dispatch, without a vtable. Their manifest is committed for gateway-less validation. After changing one:

    cd gateway && cargo run -p lumewarp-gateway -- strategies --json \
      > ../control-plane/src/lumewarp_control_plane/compiler/builtin_strategies.json

`cargo test` and a CI step fail while the two differ.
