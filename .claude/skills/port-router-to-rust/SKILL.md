---
name: port-router-to-rust
description: Port an existing Python router policy into a Lumewarp WASM router-strategy plugin written in Rust. Use when the user wants to move a custom router, a LiteLLM-style deployment chooser, or any Python class that picks a model onto the Lumewarp gateway; when they ask how to add or install a router strategy component; or when a Python policy needs to keep deciding routes on Lumewarp's request path. Covers the real register!/Configure contract, params and member_params schemas, the schema subset, a Python-vs-Rust comparison table, tests, building the component, installing it in the router plugin directory, standalone YAML and the bench.
---

# Port a Python router to a Rust router-strategy plugin

A Lumewarp router strategy is a Rust crate compiled to a WASM component and
loaded from `LUMEWARP_ROUTER_PLUGIN_DIR` when the gateway starts
(`docs/router-strategies.md`, `docs/plugins.md`, plugin ABI 1). The control
plane learns it from the gateway's `Hello.plugins`, validates router writes
against the schema the component's `manifest()` declares, and the UI renders
that schema. The gateway is not rebuilt, and nothing in `proto/`,
`control-plane/src/` or `ui/src/` names the strategy.

This skill takes the Python policy the user already runs and produces
`gateway/plugins/router/<name>/`, a built `.wasm` file installed in the router
plugin directory, tests that compare it with the Python, and the bench and
deployment steps.

Read before writing code:

- `docs/router-strategies.md` — the contract, validation on write, compatible
  changes, building and installing.
- `docs/plugins.md` — the manifest, the schema subset, the sandbox (no WASI),
  failure behaviour and metrics.
- `gateway/plugins/router/priority-failover/` — the smallest complete crate.
- `gateway/plugins/router/cheapest-first/` — this skill's worked example, with
  its Python input at `example/cheapest_first.py`.

## 1. Decide whether it can be a strategy

`pick` runs on the request path, inside the WASM sandbox. It may read only:

| Source | What it gives |
|---|---|
| `params` | Per-router settings, deserialized in `configure` before the request path |
| `member_params` | One settings object per router member, precomputed into a table in `configure` |
| `RouterInfo` | The router's name and its members (model name, weight, split), in `configure` |
| `Candidate` | `name`, `member_index`, `weight`, `in_flight`, `latency_ewma_us`, `load` |
| `RequestInfo` | Called name, team id, key id, `estimated_input_tokens` (the gateway passes `None` today), and headers named in `params.headers` |
| `RngCore` | Range draws, only if the policy is randomized and deterministic given the RNG stream |

It may not read the prompt, the response body, the database, the control plane,
another service, the clock, the filesystem, or global mutable state. Model
catalog prices are not visible either: if the Python policy used a price table,
it becomes member params (the worked example). The router world has no host
imports at all: no `secret()` and no `http-fetch` (those exist only for
`upstream-auth`).

Health, retries, retry budget, fallbacks, traffic splits, stickiness, circuit
breakers and weights belong to the gateway. `Candidates` already contains only
the members eligible for this attempt. A strategy selects; it does not retry,
report, log requests, or emit metrics.

If the policy needs one of the forbidden inputs, stop and tell the user it
cannot be a request-path strategy as written. Choices: move that part to the
control plane (a compiler plugin), or change the policy.

## 2. Inventory the Python decision

Write down every input the Python `choose` reads and where it lands:

| Python read | Rust replacement |
|---|---|
| Price/cost table in code or config | `MemberParams` field, one per member (prices are public; never a secret) |
| Health flag, breaker state | Nothing: `Candidates` is already filtered by the gateway |
| Requests in flight, queue length | `Candidate::in_flight()`, `Candidate::load()` |
| Latency sample | `Candidate::latency_ewma_us()`; declare `needs.latency` |
| vLLM metrics | `Candidate::load()`; declare `needs.vllm_load` |
| Deployment weight | `Candidate::weight()`; declare `uses_weight: true` if it decides |
| Header | `params.headers` (reserved, platform-owned) plus `RequestInfo::header` |
| Team, key, called name | `RequestInfo::{team_id, key_id, requested}` |
| Estimated prompt size | `RequestInfo::estimated_input_tokens`, but it is `None` today: do not rely on it |
| Prompt, response, embeddings | Not available: this cannot be a `pick` policy |
| DB/HTTP/clock/env in the decision | Not available inside the sandbox: stop, or move it to `configure`-time settings with no I/O |

Record the tuning: tie-breaks, defaults, caps, and the "nothing acceptable"
case. Each becomes either a param, a member param, or the `None` return that
lets the gateway fall through.

## 3. Create the crate

    mkdir -p gateway/plugins/router/<name>
    cp gateway/plugins/router/priority-failover/Cargo.toml \
       gateway/plugins/router/<name>/
    cp -r gateway/plugins/router/priority-failover/src \
          gateway/plugins/router/priority-failover/testdata \
          gateway/plugins/router/<name>/

The example is its own Cargo workspace (`[workspace]` in `Cargo.toml`), so it is
not a member of `gateway/Cargo.toml` and that file needs no edit. `Cargo.toml`:

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

[dev-dependencies]
serde_json = "1"
```

The registered name uses underscores (`cheapest_first`); the directory and crate
use dashes (`cheapest-first`). The `register!` name must match
`^[a-z][a-z0-9_]{0,62}$` and must be unique across the kind, built-ins
included: a duplicate refuses gateway startup.

## 4. Settings, `register!`, `configure`, `pick`

Follow the types in `docs/router-strategies.md` exactly. The shape:

```rust
use lumewarp_plugin_sdk::{
    Candidates, ConfigError, Configure, Needs, RequestInfo, RngCore, RouterInfo, Strategy,
};
use schemars::JsonSchema;
use serde::Deserialize;

#[derive(Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct Params {
    /// Shown under the field in the UI.
    #[schemars(title = "Max in flight", range(min = 1))]
    pub max_in_flight: Option<u32>,
}

#[derive(Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct MemberParams {
    /// One object per router member.
    pub tier: u32,
}

pub struct MyStrategy {
    tiers: Box<[u32]>,
}

impl Configure<Params, MemberParams> for MyStrategy {
    fn configure(
        _: &RouterInfo<'_>,
        params: Params,
        members: &[MemberParams],
    ) -> Result<Self, ConfigError> {
        // Annotations shape the schema only; enforce every bound here too.
        if params.max_in_flight == Some(0) {
            return Err(ConfigError("max_in_flight must be at least 1".into()));
        }
        Ok(Self {
            tiers: members.iter().map(|m| m.tier).collect(),
        })
    }
}

impl Strategy for MyStrategy {
    fn pick(
        &self,
        _: &RequestInfo<'_>,
        candidates: &Candidates<'_>,
        _: &mut dyn RngCore,
    ) -> Option<usize> {
        candidates
            .iter()
            .min_by_key(|c| self.tiers[c.member_index()])
            .map(|c| c.index)
    }
}

lumewarp_plugin_sdk::register! {
    kind: "router-strategy",
    name: "my_strategy",
    title: "My strategy",
    description: "One sentence for the UI.",
    uses_weight: false,
    needs: Needs::default(),
    params: Params,
    member_params: MemberParams,
    strategy: MyStrategy,
}
```

Rules that the compiler will not catch:

- **`configure` is the authority.** `schemars` annotations and `serde` types
  only describe and parse; standalone YAML and snapshots reach `configure`
  without passing the control plane's schema. Check every range there.
- **Precompute tables in `configure`** (`Box<[T]>`, one entry per
  `RouterInfo::members`), and index them with `Candidate::member_index()`.
  Never use `Candidate::index` for a member table: a traffic split gives a
  model its own entry and its own settings, and pools are laid out differently.
- **`pick` must be cheap and deterministic:** no I/O, no locks, no allocation
  in the common case, no `format!`/`Vec`/`sort`/`collect`. Build boxes and
  arrays in `configure`; compare stack tuples in `pick`. Randomized policies
  must draw only through `RngCore`.
- **No WASI in the guest.** `HashMap::new()` asks for entropy and traps
  (`wasi:random`); `println!`, `Instant::now()` and `std::env` trap too. Use a
  `BTreeMap` or a fixed hasher (`BuildHasherDefault`), and no clock. Do not put
  request- or key-derived data in globals: instances are pooled and reused
  across requests and keys.
- **Return `c.index`, not `c.member_index()`.** Return `None` when nothing is
  acceptable; the gateway then takes its fall-through paths.
- **`params` are public:** they go into the snapshot, the API, the audit trail
  and every gateway's disk. Put no credential there.
- **`ConfigError` text is dropped** before it reaches the caller (params may be
  private). Say what is safe to log; do not include param values.
- **Headers are opt-in:** only names in `params.headers` are readable through
  `RequestInfo::header`. Do not declare `headers`; the gateway owns and removes
  that field. Credential headers are refused.
- **One `register!` per crate.** The macro generates the component's exports,
  `manifest()` and a test that fails `cargo test` when a schema leaves the
  subset, naming the field.

## 5. Port the decision

Translate the inventory from step 2 into the table/index shape. Keep the
Python's tie-breaks explicit: if the Python iterated in configuration order,
key by `member_index()` so a split pool cannot change the winner. Decide what
"unknown" means for optional member settings (an unpriced member, a missing
tier): the worked example's rule is priced members first, unpriced members only
when no priced member is eligible, and a configured cap excludes unknown
prices. Whatever the rule is, state it in the doc comment and test it.

Do not re-implement health checks, retries or fallbacks. A policy that said
"retry the next cheapest on 503" becomes "pick the cheapest"; the router's
`retry` and `fallbacks` do the rest.

## 6. Test against the Python and the contract

`register!` also registers the strategy natively on non-WASM targets, so the
crate's own tests can call the same factory path the gateway uses
(`lookup(name)`, `descriptors()`) without a gateway. Cover:

1. **The Python comparison table.** Keep the Python policy and a generator
   beside it, run the Python to write the expected picks, and read the file in
   Rust with `include_str!`. Never hand-write the expected column. In the
   worked example:

       python3 .claude/skills/port-router-to-rust/example/gen_pick_table.py

   The generator writes `gateway/plugins/router/cheapest-first/testdata/pick-table.json`;
   the test `pick_matches_the_python_policy` compares `pick` with it. Add a
   scenario for every tie-break, every cap, empty eligible sets, and every
   "unknown" case.

2. **`configure` refusals.** The schema is not enforced at runtime: pass JSON
   that the schema rejects (unknown fields, wrong types, out-of-range values)
   to the factory and assert that `configure` rejects it too. Also test a list
   of boundary payloads through both the control plane and standalone YAML, as
   `priority-failover/testdata/boundaries.json` and
   `tests/e2e/test_strategy_plugin.py` do.

3. **Split/member-index behaviour.** Put the same model in two splits with
   different member params and assert the pick uses the right entry, in both
   pool orders. This is the test that catches `index` instead of
   `member_index`.

4. **Allocation.** If the policy claims to allocate nothing, count allocations
   around `pick` with a small test allocator (the worked example has one).

5. **End to end through a snapshot.** Add
   `tests/e2e/test_strategy_<name>.py` modelled on
   `tests/e2e/test_strategy_cheapest_first.py`: the script builds the components
   with `bash gateway/plugins/build.sh`, the test starts a gateway with
   `LUMEWARP_ROUTER_PLUGIN_DIR` set through `stack.start_gateway(extra_env=...)`,
   saves a router using the strategy through the admin API, calls it, and asserts
   the usage row's `model` is the member the policy should have chosen. Also
   assert that `GET /plugins?kind=router-strategy` shows the descriptor and that
   the control plane enforces its schema (422 on bad member params).
   `tests/e2e/test_strategy_plugin.py` is the generic variant for
   `priority_failover`, with the same boundary payloads through the API and
   standalone YAML.

Run:

    cd gateway && cargo test --manifest-path plugins/router/<name>/Cargo.toml
    cargo fmt --manifest-path plugins/router/<name>/Cargo.toml -- --check
    cargo clippy --manifest-path plugins/router/<name>/Cargo.toml \
      --all-targets -- -D warnings
    cargo fmt --all --check

The component crate is outside the gateway workspace, so `cargo clippy
--workspace` does not lint it; the `--manifest-path` call above does. The last
`cargo fmt --all --check` covers the gateway itself if you changed anything
there.

Prove each test once by breaking the behaviour and watching it fail, then put
the change back.

## 7. Build and install the component

The crate is one component. Build it for the plugin target (once per machine,
add the target):

    rustup target add wasm32-wasip2
    cd gateway
    cargo build --release --target wasm32-wasip2 \
      --manifest-path plugins/router/<name>/Cargo.toml

Because the crate is its own workspace, the file is
`plugins/router/<name>/target/wasm32-wasip2/release/lumewarp_plugin_<name_with_underscores>.wasm`.

Copy it into the directory `LUMEWARP_ROUTER_PLUGIN_DIR` names and restart the
gateway. The file name is yours; the plugin name comes from the manifest:

    cp plugins/router/<name>/target/wasm32-wasip2/release/lumewarp_plugin_<name_with_underscores>.wasm \
       /etc/lumewarp/plugins/router/<name>.wasm
    LUMEWARP_ROUTER_PLUGIN_DIR=/etc/lumewarp/plugins/router ./target/debug/lumewarp plugins

`plugins` lists built-ins and every loaded component; `--json` prints the
descriptor array the gateway reports in `Hello`. A file that does not implement
the router world, declares another kind, reuses a name or has an invalid
manifest refuses startup. There is no hot reload: replacing the file requires a
restart.

For a local run, `bash gateway/plugins/build.sh` builds the committed examples
into `gateway/target/plugins/router/`, so
`LUMEWARP_ROUTER_PLUGIN_DIR=gateway/target/plugins/router` is enough.

Commit the crate and its `Cargo.lock`. There is no feature flag, no gateway
dependency and no `gateway/Cargo.lock` change: the gateway binary is the same
for every router strategy.

Standalone mode uses the same component and the same schema checks:

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

    LUMEWARP_ROUTER_PLUGIN_DIR=gateway/target/plugins/router \
      ./gateway/target/debug/lumewarp --config /tmp/cost-aware.yaml

Invalid settings exit non-zero with `router "<name>" strategy configuration
failed`; the param values are not printed.

## 8. Bench the request path

A WASM strategy costs one host call, the wire copy of the request and the
eligible candidates, plus whatever `pick` does; a native built-in does not cross
the boundary. Bench it with the real driver (`bench/README.md`), never by
writing a micro-benchmark that skips the gateway. There is no fixed latency
budget for a plugin: report the numbers, and `lumewarp_plugin_duration_seconds`
shows slowness in production.

The bench always calls the router named `mock` in `bench/lumewarp/config.yaml`.
Make a local, uncommitted edit that points that router at the strategy, e.g.:

```yaml
  - name: mock
    strategy: cheapest_first
    params: {}
    members:
      - model: mock-model
        params: {input_price_nano_usd_per_mtok: 1}
```

Then, from the repo root (the gateway binary needs no special feature; it only
needs the directory):

    (cd bench/mock-upstream && cargo build --release)
    (cd gateway && cargo build --release -p lumewarp-gateway)
    LUMEWARP_ROUTER_PLUGIN_DIR=$PWD/gateway/target/plugins/router \
      LUMEWARP_BIN=$PWD/gateway/target/release/lumewarp \
      bench/scripts/run.sh --quick --no-build

`--quick` is 1000 RPS for 15 s plus SSE clients; use `--full` for the
300/1000/5000/10000 RPS run. Compare overhead p99 with the same gateway using
the native `weighted` strategy, not absolute latency. For the LiteLLM
comparison, run the same routing policy there (a LiteLLM custom routing
strategy) and report the difference; do not add a plugin-specific budget.
Revert the config edit afterwards.

## 9. Deploy

1. Put the component on every gateway that will serve a router using it,
   before saving the router. A gateway without it NACKs the whole config
   version, including key revocations. `force: true` saves a router a connected
   gateway cannot serve, audits `router.force_save`, and leaves that gateway
   behind.
2. `GET /api/v1/plugins?kind=router-strategy` shows the descriptor, its schemas
   and the gateways that report it. The UI form is built from the schema.
3. Saved routers are validated against every connected gateway's schema;
   `configure` is still the final authority on the gateway.
4. Under one name, only **add optional fields**. Removing, renaming, making a
   field required, or narrowing a type/range needs a new strategy name; saved
   routers would otherwise turn `config_invalid` or be NACKed. Changing only
   the code (schema unchanged) is free: replace the file and restart.

## Worked example: `CheapestFirstRouter` → `cheapest-first`

Input: `example/cheapest_first.py`. It chooses the healthy deployment with the
lowest input price (nano-USD per Mtok from a code table), ties by fewer
requests in flight then configuration order, puts unpriced deployments last,
and supports a maximum input price. The port is
`gateway/plugins/router/cheapest-first/`, and `testdata/pick-table.json` is
generated by the Python in 13 scenarios (cheapest, both tie-breaks, zero price,
five unpriced cases, four cap cases, ineligible priced members, empty pool);
the Rust test compares every row.

| Python | Rust in the crate | Why |
|---|---|---|
| `prices: dict[str, int]` in the constructor | `MemberParams::input_price_nano_usd_per_mtok: Option<u64>` | A strategy cannot read the catalog; per-member settings are what reaches it |
| Calls `choose(healthy)` with the caller's filtered list | `Candidates` | The gateway filters health, breakers, splits and exclusions before `pick` |
| `position` in the loop | `Candidate::member_index()` in the sort key | Split pools reorder members; the Python's "configuration order" is member order |
| `price is None` sorted last | First tuple key `price.is_none()` | Unpriced is a deliberate last resort, not a zero price |
| `max_input_price` also refuses unpriced | The `filter` before `min_by_key` | An unknown price cannot be shown to be under the cap |
| Returns `None` | Returns `None` | No acceptable candidate: the gateway's fall-through paths |
| `in_flight` tie-break | `Candidate::in_flight()` | Third tuple key, then `member_index()` |

The crate additionally tests split member indices, `configure` refusals, the
descriptor and zero allocations in `pick`. The e2e
`tests/e2e/test_strategy_cheapest_first.py` proves the component from `Hello`
to the member that serves a request.

## Checklist

- [ ] The policy can run from `params`/`member_params`/`Candidate`/`RequestInfo` only.
- [ ] `configure` enforces every bound the schema states and precomputes tables.
- [ ] `pick` uses `member_index()` for member tables and allocates nothing.
- [ ] No `HashMap::new()`, `println!`, clock or environment access in the guest.
- [ ] The unpriced/unknown rule is written down and covered by a table case.
- [ ] The comparison table is generated by the Python, not by hand.
- [ ] `cargo test --manifest-path plugins/router/<name>/Cargo.toml`, the crate's
      clippy and `cargo fmt --manifest-path plugins/router/<name>/Cargo.toml --
      --check` pass.
- [ ] The component builds for `wasm32-wasip2` and `lumewarp plugins` lists it
      from its directory.
- [ ] The e2e proves `Hello` → router → served request through a snapshot.
- [ ] The crate and its `Cargo.lock` are committed; no gateway feature or
      `gateway/Cargo.lock` change.
- [ ] Standalone YAML accepts valid settings and refuses invalid ones.
- [ ] The bench was run with the strategy wired into the bench router.
