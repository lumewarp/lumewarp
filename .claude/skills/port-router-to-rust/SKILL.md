---
name: port-router-to-rust
description: Port an existing Python router policy into a Lumewarp Rust strategy crate. Use when the user wants to move a custom router, a LiteLLM-style deployment chooser, or any Python class that picks a model into the Rust gateway; when they ask how to add a router strategy crate; or when a Python policy needs to keep deciding routes on Lumewarp's request path. Covers the real register!/Configure contract, params and member_params schemas, a Python-vs-Rust comparison table, tests, linking the feature, standalone YAML and the bench.
---

# Port a Python router to a Rust strategy

A Lumewarp router strategy is a Rust crate compiled into the gateway
(`docs/router-strategies.md`, API version 2). The control plane learns it from
the gateway's `Hello`, validates router writes against the schema the crate
declares, and the UI renders that schema. Nothing else changes.

This skill takes the Python policy the user already runs and produces
`gateway/strategies/<name>/`, the two edits that link it, tests that compare it
with the Python, and the bench and deployment steps.

Read before writing code:

- `docs/router-strategies.md` — the contract, the schema subset, the three
  validation layers.
- `gateway/strategies/priority-failover/` — the smallest complete crate.
- `gateway/strategies/cheapest-first/` — this skill's worked example, with its
  Python input at `example/cheapest_first.py`.

## 1. Decide whether it can be a strategy

`pick` runs on the request path. It may read only:

| Source | What it gives |
|---|---|
| `params` | Per-router settings, deserialized in `configure` before the request path |
| `member_params` | One settings object per router member, precomputed into a table in `configure` |
| `RouterInfo` | The router's name and its members (model name, weight, split), in `configure` |
| `Candidate` | `name`, `member_index`, `weight`, `in_flight`, `latency_ewma_us`, `load` |
| `RequestInfo` | Called name, team id, key id, `estimated_input_tokens` (the gateway passes `None` today), and headers named in `params.headers` |
| `RngCore` | Range draws, only if the policy is randomized and deterministic given the RNG stream |

It may not read the prompt, the response body, the database, the control plane,
another service, the clock, or global mutable state. Model catalog prices are
not visible either: if the Python policy used a price table, it becomes member
params (the worked example).

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
| DB/HTTP/env in the decision | `configure` may read the environment for secrets; never do I/O per request |

Record the tuning: tie-breaks, defaults, caps, and the "nothing acceptable"
case. Each becomes either a param, a member param, or the `None` return that
lets the gateway fall through.

## 3. Create the crate

    mkdir -p gateway/strategies/<name>/src gateway/strategies/<name>/testdata

`gateway/Cargo.toml` already has `strategies/*` as workspace members, so it
needs no edit. `Cargo.toml`, copying `gateway/strategies/cheapest-first`:

```toml
[package]
name = "lumewarp-strategy-<name>"
description = "<one line>"
version.workspace = true
edition.workspace = true
rust-version.workspace = true
license.workspace = true
repository.workspace = true
publish = false

[dependencies]
lumewarp-router-api = { path = "../../crates/lumewarp-router-api" }
schemars.workspace = true
serde.workspace = true

[dev-dependencies]
serde_json.workspace = true
```

The registered name uses underscores (`cheapest_first`); the directory, crate
and feature use dashes (`cheapest-first`, `strategy-cheapest-first`). The name
must match `^[a-z][a-z0-9_]{0,62}$` and must be unique: two crates with one
name make the gateway refuse it.

## 4. Settings, `register!`, `configure`, `pick`

Follow the types in `docs/router-strategies.md` exactly. The shape:

```rust
use lumewarp_router_api::{
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

lumewarp_router_api::register! {
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
- **Return `c.index`, not `c.member_index()`.** Return `None` when nothing is
  acceptable; the gateway then takes its fall-through paths.
- **`params` are public:** they go into the snapshot, the API, the audit trail
  and every gateway's disk. Put no credential there. Read a secret from the
  environment in `configure` (as upstream `api_key_ref` does).
- **`ConfigError` text is dropped** before it reaches the caller (params may be
  private). Say what is safe to log; do not include param values.
- **Headers are opt-in:** only names in `params.headers` are readable through
  `RequestInfo::header`. Do not declare `headers`; the gateway owns and removes
  that field. Credential headers are refused.
- **One `register!` per module.** The macro generates a test module with a
  fixed name; two in one module do not compile. Registering a schema outside
  the supported subset fails that generated test in `cargo test`, naming the
  field.

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

Use `lookup(name)` and the factory, not the concrete type: that is the path the
gateway takes (`create` deserializes the JSON the control plane stores). Cover:

1. **The Python comparison table.** Keep the Python policy and a generator
   beside it, run the Python to write the expected picks, and read the file in
   Rust with `include_str!`. Never hand-write the expected column. In the
   worked example:

       python3 .claude/skills/port-router-to-rust/example/gen_pick_table.py

   The generator writes `gateway/strategies/cheapest-first/testdata/pick-table.json`;
   the test `pick_matches_the_python_policy` compares `pick` with it. Add a
   scenario for every tie-break, every cap, empty eligible sets, and every
   "unknown" case.

2. **`configure` refusals.** The schema is not enforced at runtime: pass JSON
   that the schema rejects (unknown fields, wrong types, out-of-range values)
   to the factory and assert that `configure` rejects it too.

3. **Split/member-index behaviour.** Put the same model in two splits with
   different member params and assert the pick uses the right entry, in both
   pool orders. This is the test that catches `index` instead of
   `member_index`.

4. **Allocation.** If the policy claims to allocate nothing, count allocations
   around `pick` (the worked example has a small test allocator) or state that
   the gateway's path test covers it.

5. **End to end through a snapshot.** Add `tests/e2e/test_strategy_<name>.py`
   modelled on `tests/e2e/test_strategy_crate.py`: prove nothing under
   `proto/`, `control-plane/src/` or `ui/src/` names it, build a gateway with
   the feature (or use an env-provided binary), start it against the control
   plane, save a router using the strategy, call it, and assert the usage row's
   `model` is the member the policy should have chosen. The example also asserts
   that the control plane enforces the derived schema and that the gateway's
   `capabilities` in `Hello` carry `strategy:<name>`. From the repo root:

       flock /tmp/lumewarp-e2e.lock scripts/e2e-p1.sh -k <name>

   Build the feature gateway into its own target directory
   (`CARGO_TARGET_DIR=<repo>/gateway/target/<name>-e2e`), never the default
   `gateway/target/debug/lumewarp`: `tests/e2e/test_p2_routing.py` asserts that
   binary's exact list of `strategy:` capabilities, and a backup/restore can be
   skipped by a timeout or an interrupt.

Run:

    cd gateway && cargo test -p lumewarp-strategy-<name>
    cd gateway && cargo fmt --all --check
    cd gateway && cargo clippy --all-targets -- -D warnings

Prove each test once by breaking the behaviour and watching it fail, then put
the change back.

## 7. Link it into the gateway

A crate must be named to be linked. Two files, both named in
`docs/router-strategies.md`:

`gateway/crates/lumewarp-gateway/Cargo.toml`:

```toml
[features]
strategy-<name> = ["dep:lumewarp-strategy-<name>"]

[dependencies]
lumewarp-strategy-<name> = { path = "../../strategies/<name>", optional = true }
```

`gateway/crates/lumewarp-gateway/src/main.rs`:

```rust
// A strategy crate must be named to be linked; its `register!` does the rest.
#[cfg(feature = "strategy-my-strategy")]
use lumewarp_strategy_my_strategy as _;
```

(`register!` would say `name: "my_strategy"`.) The feature, dependency and
path use the dashed form; the `register!` name uses underscores.

Then:

    cd gateway
    cargo build -p lumewarp-gateway --features strategy-<name>
    ./target/debug/lumewarp strategies
    ./target/debug/lumewarp strategies --json
    cargo test --features strategy-<name>
    cargo test --all-features

The listing must show the title; `--json` is the manifest the control plane
receives through `Hello`. Commit the updated `gateway/Cargo.lock` (CI runs
`cargo run --locked`).

Both test forms must pass: `crates/lumewarp-gateway/tests/strategies.rs` compares
the built-in entries with the committed manifest and checks the custom entries
against the features compiled into the binary, so a strategy feature does not
make the manifest test fail. Add the new name to its
`compiled_custom_strategies()` list next to the feature.

Standalone mode, with the same schema and the same `configure` checks:

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

    cd gateway && ./target/debug/lumewarp --config /tmp/cost-aware.yaml

Invalid settings exit non-zero with `router "<name>" strategy configuration
failed`; the param values are not printed.

## 8. Bench the request path

A custom strategy costs one virtual call plus whatever `pick` does. Bench it
with the real driver (`bench/README.md`), never by writing a micro-benchmark
that skips the gateway.

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

Then, from the repo root (the release build in `scripts/run.sh` has no features,
so build the binary first and pass it through `LUMEWARP_BIN`):

    (cd bench/mock-upstream && cargo build --release)
    (cd gateway && cargo build --release -p lumewarp-gateway --features strategy-<name>)
    LUMEWARP_BIN=$PWD/gateway/target/release/lumewarp bench/scripts/run.sh --quick --no-build

`--quick` is 1000 RPS for 15 s plus SSE clients; use `--full` for the
300/1000/5000/10000 RPS run. Compare overhead p99 with the built-in run, not
absolute latency. Revert the config edit afterwards.

## 9. Deploy

1. Upgrade every gateway that will serve a router using the strategy first: a
   gateway without the crate NACKs the whole config version, including key
   revocations. `force: true` saves a router a connected gateway cannot serve,
   audits `router.force_save`, and leaves that gateway behind.
2. `GET /api/v1/strategies` shows the descriptor, its schemas and the gateways
   that report it. The UI form is built from the schema.
3. Saved routers are validated against every connected gateway's schema;
   `configure` is still the final authority on the gateway.
4. Under one name, only **add optional fields**. Removing, renaming, making a
   field required, or narrowing a type/range needs a new strategy name; saved
   routers would otherwise turn `config_invalid` or be NACKed.

## Worked example: `CheapestFirstRouter` → `cheapest-first`

Input: `example/cheapest_first.py`. It chooses the healthy deployment with the
lowest input price (nano-USD per Mtok from a code table), ties by fewer
requests in flight then configuration order, puts unpriced deployments last,
and supports a maximum input price. The port is
`gateway/strategies/cheapest-first/`.

| Python | Rust in the crate | Why |
|---|---|---|
| `prices: dict[str, int]` in the constructor | `MemberParams::input_price_nano_usd_per_mtok: Option<u64>` | A strategy cannot read the catalog; per-member settings are what reaches it |
| Calls `choose(healthy)` with the caller's filtered list | `Candidates` | The gateway filters health, breakers, splits and exclusions before `pick` |
| `position` in the loop | `Candidate::member_index()` in the sort key | Split pools reorder members; the Python's "configuration order" is member order |
| `price is None` sorted last | First tuple key `price.is_none()` | Unpriced is a deliberate last resort, not a zero price |
| `max_input_price` also refuses unpriced | The `filter` before `min_by_key` | An unknown price cannot be shown to be under the cap |
| Returns `None` | Returns `None` | No acceptable candidate: the gateway's fall-through paths |
| `in_flight` tie-break | `Candidate::in_flight()` | Third tuple key, then `member_index()` |

`testdata/pick-table.json` is generated by the Python in 13 scenarios (cheapest,
both tie-breaks, zero price, five unpriced cases, four cap cases, ineligible
priced members, empty pool); the Rust test compares every row. The crate
additionally tests split member indices, `configure` refusals, the descriptor
and zero allocations in `pick`.

## Checklist

- [ ] The policy can run from `params`/`member_params`/`Candidate`/`RequestInfo` only.
- [ ] `configure` enforces every bound the schema states and precomputes tables.
- [ ] `pick` uses `member_index()` for member tables and allocates nothing.
- [ ] The unpriced/unknown rule is written down and covered by a table case.
- [ ] The comparison table is generated by the Python, not by hand.
- [ ] `cargo test -p lumewarp-strategy-<name>`, `cargo test --features
      strategy-<name>`, `cargo test --all-features`, `cargo fmt --all --check`
      and `cargo clippy --all-targets -- -D warnings` pass.
- [ ] The e2e proves `Hello` → router → served request through a snapshot.
- [ ] The feature, the `use` line and `Cargo.lock` are committed.
- [ ] Standalone YAML accepts valid settings and refuses invalid ones.
- [ ] The bench was run with the strategy wired into the bench router.
