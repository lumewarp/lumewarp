---
name: lumewarp-plugin-dev
description: Write, test, build and install a Lumewarp plugin, in the user's own repository or in this one. Use when the user wants a custom router strategy (how a router picks its member model), a custom credential for an MCP server or A2A agent (token exchange, request signing), an A2A middleware that inspects or rewrites agent messages, a control-plane identity mapping (`lumewarp.auth`), or an authorization backend that reads team and org roles from their own IAM (`lumewarp.authz`); or when they ask what a Lumewarp plugin can do, which kind they need, or why one does not load. Starts from tested templates that depend on the SDK or the control plane by release tag. To port an existing Python router policy, use port-router-to-rust instead.
---

# Lumewarp plugin development

Lumewarp has five extension points. Three are WebAssembly components the gateway loads at
startup; two are Python packages the control plane loads through entry points. This skill
picks the right one, starts from a template in `templates/` that is known to build and pass
its tests outside this repository, and carries it through to a verified install.

References, loaded when needed (each is the manual's page, linked rather than copied):

- `references/plugins.md`: manifest, schema subset, sandbox, failure behaviour, metrics,
  control-plane validation, compatible changes, and the upstream-auth and A2A middleware
  contracts.
- `references/router-strategies.md`: the router strategy contract and its test table.
- `references/python-plugins.md`: `lumewarp.auth` and `lumewarp.authz`.

For porting a Python router that already exists, `port-router-to-rust` has the
comparison method and a worked example; the template here is the same crate shape.

## 1. Choose the extension point

| The user wants to | Kind | Template | Runs |
| --- | --- | --- | --- |
| Choose which member of a router serves a request | `router-strategy` | `templates/router-strategy` | Gateway, every LLM request on that router |
| Compute the credential sent to an MCP server or A2A agent | `upstream-auth` | `templates/upstream-auth` | Gateway, per outbound MCP/A2A request unless cached |
| Inspect, rewrite or filter A2A JSON-RPC messages | `a2a-middleware` | `templates/a2a-middleware` | Gateway, per A2A request, response and stream event |
| Decide who a first-time SSO login is (email, display name) or refuse it | `lumewarp.auth` | `templates/python-auth` | Control plane, first login only |
| Answer "is this user an admin or member of this team or org?" from their IAM | `lumewarp.authz` | `templates/python-authz` | Control plane, admin API and the key reconciler |

Say no, and explain why, when the request needs any of these; no plugin kind can do them:

- **Provider translation.** Upstreams are OpenAI-compatible only; nothing converts to another
  provider's API.
- **Python on the request path.** Python plugins run at login or when an administrator acts,
  never per request. Per-request decisions are WASM.
- **Retries, fallbacks, traffic splits, stickiness, circuit breaking, health.** The gateway
  owns them. A strategy picks among the candidates it is given, which are already eligible.
- **I/O from a router strategy or a middleware.** They have no network, file, clock, entropy
  or environment access, and no host imports. Only `upstream-auth` has `http-fetch` (to
  origins listed in `allowed_origins`), `secret()` and `now-unix-ms()`.
- **Reading prompts or responses in a router strategy.** It sees the called name, team and
  key ids, opted-in headers, and per-candidate signals (in flight, latency EWMA, vLLM load).
- **Changing an LLM upstream's credential.** `upstream-auth` applies to MCP servers and A2A
  agents only; LLM upstreams use their stored key.
- **Granting roles from identity claims.** `lumewarp.auth` says who the user is; it grants
  nothing. Roles come from Lumewarp's database or the `lumewarp.authz` backend.
- **Hot reload.** A new `.wasm` file takes effect after a gateway restart.

## 2. Start from the template

Work in the user's repository, not inside Lumewarp's, unless they are contributing an
example to `gateway/plugins/`.

1. Ask which Lumewarp release they deploy (the gateway and control plane image tag, such as
   `0.1.0`), unless it is already known. The templates pin `v0.1.0`; change the `tag` in
   `Cargo.toml` or the `@v...` in `pyproject.toml` to `v<that release>`. The plugin ABI
   (currently 1) is checked when the gateway loads the file.
2. Copy the template directory. For a WASM kind, also copy `templates/rust-toolchain.toml`
   next to its `Cargo.toml`; it installs the `wasm32-wasip2` target.
3. Rename everything that says `my`:
   - Rust: the package name in `Cargo.toml` (the `.wasm` file is named after it, with
     underscores), the struct, and `name`, `title`, `description` in `register!`. `name`
     matches `^[a-z][a-z0-9_]{0,62}$` and must not collide with a built-in (`weighted`,
     `least_in_flight`, `lowest_latency`, `vllm_load`, `none`, `static_bearer`,
     `passthrough`). It is what routers, MCP servers and A2A agents store, so renaming it
     later is a breaking change.
   - Python: the distribution name, the package directory under `src/`, and the entry point
     name (`my-auth` / `my-authz`), which is what `LUMEWARP_AUTHZ_BACKEND` names.
4. Run the template's tests once before changing anything (step 4), so a toolchain problem is
   not mistaken for a bug in the new code.

If the crate lives inside an existing Cargo workspace, delete the `[workspace]` line from its
`Cargo.toml` and add it to the workspace's members instead.

## 3. Implement

Replace the example behaviour; keep the structure. What every kind has to get right:

**All WASM kinds**

- `configure` runs when a configuration is applied, not per request. Parse, validate and
  precompute there; an `Err` refuses the whole config version (a NACK), so it must not
  contain settings or secrets.
- **Schema annotations are not checked at runtime.** `#[schemars(range(min = 1))]` only
  changes the schema; standalone YAML and snapshots reach `configure` without it. Enforce
  every stated bound in `configure` and test it.
- Settings structs use `#[serde(deny_unknown_fields)]` and stay inside the schema subset:
  strings, integers, numbers, booleans, string arrays, one level of nested structs, `Option`,
  string or number enums. No `HashMap`, no `Vec<u32>`, no enums with data, no `format` other
  than integer widths. The test `register!` generates fails `cargo test` and names the field.
- **Never put a secret in settings.** They go into snapshots, the API, the audit log and
  every gateway's disk. An `upstream-auth` plugin reads `LUMEWARP_PLUGIN_SECRET_<NAME>`
  through `secret()`; put the plugin's name in `<NAME>`.
- Instances are pooled and reused across requests and keys. Keep only configuration-derived
  values in the struct; never cache per-request, per-key or per-team data in statics.
- These trap in the sandbox: `HashMap::new()` and `HashSet::new()` (random seed; use
  `BTreeMap` or a fixed hasher), `println!`/`eprintln!`, `Instant::now()`/`SystemTime::now()`,
  file and socket access, `std::env`. A panic is a trap too.

**`router-strategy`** (`references/router-strategies.md`)

- `pick` returns `c.index` (pool position); per-member tables are indexed by
  `c.member_index()`. They differ under traffic splits.
- Return `None` when no candidate is acceptable; the gateway then retries or falls back.
- Declare `needs: Needs { latency: true, ..Needs::default() }` or `vllm_load` only for the signals read; an
  undeclared signal is always absent.
- Request headers: only those an administrator lists in the reserved `params.headers` reach
  `RequestInfo`. Do not declare that field in `Params`.
- No I/O and no heavy work: `pick` is on the LLM request path. Run the `bench/` A6 workload
  in the Lumewarp repository when performance matters.

**`upstream-auth`** (`references/plugins.md#upstream-auth`)

- `authorize` returns headers to add and `cache_ms`. `cache_ms: 0` is required when the
  result depends on the request (a body signature); otherwise cache as long as the credential
  lives, capped at one hour by the gateway.
- `http-fetch` reaches only origins an administrator lists in the reserved
  `params.allowed_origins` (not declared in `Params`). No redirects, 1 MiB response bodies.
- Do not return `Host`, `Content-Length`, `Cookie`, hop-by-hop headers, `Mcp-Session-Id`,
  `MCP-Protocol-Version`, `A2A-Version`, `Last-Event-ID` or any `X-Lumewarp-*` header; the
  client gets 502.
- Never include the token endpoint's response body in an error string.

**`a2a-middleware`** (`references/plugins.md#a2a-middleware`)

- Hooks see the JSON-RPC method and body with upstream ids. Do not change the method, add or
  remove id fields, or add push-notification configuration; the gateway refuses the result.
- `on_stream_event` may return `None` only for a `TaskArtifactUpdateEvent`.
- Errors are `RpcError { code, message }`, sent to the client as a JSON-RPC error.

**`lumewarp.auth`** (`references/python-plugins.md`)

- A class (instantiated once), an instance or a zero-argument factory with
  `api_version = 1` and `async map_identity(claims) -> IdentityMapping | None`. `None` or
  anything that is not an `IdentityMapping` refuses the login. Only the first plugin by
  entry-point name is used.
- The IdP must still report `email_verified: true`; the plugin cannot override that.
- Configuration comes from environment variables read in `__init__`.

**`lumewarp.authz`** (`references/python-plugins.md#writing-an-authorization-backend`)

- Answer direct relations only: an org `admin` is never reported as a team `admin` or
  `member`. A team `admin` implies `member`.
- Any failure is `Decision.ERROR` (or an exception); never turn an outage into `DENY`.
- `list_objects` is optional and saves one call per candidate; `changes(cursor)` is optional
  and lets the key reconciler react within seconds instead of on its periodic rescan.
- `manages_grants = False` makes Lumewarp's member endpoints read-only (409
  `managed_externally`).

## 4. Test

WASM kinds, in the crate directory:

```sh
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo test
```

`cargo test` runs the generated schema-subset test plus the crate's own tests. Keep the
template's pattern: the router test configures the strategy through `lookup(<name>)`, the
same path the gateway uses, and checks `pick` on hand-built pools; the auth and middleware
tests exercise pure functions, because `secret()`, `http-fetch` and `now-unix-ms()` are host
imports that exist only inside the gateway. Add a test for every bound `configure` enforces.

Python kinds, in the package directory:

```sh
uv run pytest
```

The authz template subclasses `AuthzContract` from
`lumewarp_control_plane.authz.contract_tests`, the conformance suite every built-in backend
passes; keep it, and adapt only the fake of the external system. Both templates also load
the plugin through the control plane's own loader, which catches entry point and
`api_version` mistakes.

## 5. Build and verify the load (WASM)

```sh
cargo build --release --target wasm32-wasip2
# -> target/wasm32-wasip2/release/<package_name_with_underscores>.wasm
```

Put the file in a directory for its kind and ask the gateway what it would load, without a
control plane:

```sh
mkdir -p plugins/router plugins/auth plugins/a2a-middleware
cp target/wasm32-wasip2/release/lumewarp_plugin_my_strategy.wasm plugins/router/
docker run --rm -v "$PWD/plugins:/plugins:ro" \
  -e LUMEWARP_ROUTER_PLUGIN_DIR=/plugins/router \
  -e LUMEWARP_AUTH_PLUGIN_DIR=/plugins/auth \
  -e LUMEWARP_A2A_MIDDLEWARE_DIR=/plugins/a2a-middleware \
  ghcr.io/lumewarp/lumewarp-gateway:<release> plugins --json
```

(With a `lumewarp` binary built from source, run `lumewarp plugins --json` with the same
variables.) The plugin must be listed with its name, title and schemas. A refusal names the
file and the reason: wrong directory for the kind, a duplicate name, a schema outside the
subset or over 16 KiB, an unknown import. See `references/plugins.md#loading`.

For a router strategy, also send a request through a standalone gateway:
`templates/router-strategy/standalone.yaml` configures a router named `prio` that uses the
template's strategy over two models on an OpenAI-compatible upstream at
`127.0.0.1:9100`. Point the upstream at any OpenAI-compatible server (the Lumewarp
repository's `bench/mock-upstream`, started with `--listen 127.0.0.1:9100`, needs no model),
then:

```sh
LUMEWARP_ROUTER_PLUGIN_DIR=$PWD/plugins/router lumewarp --config standalone.yaml
curl -sS -H 'content-type: application/json' \
  -d '{"model":"prio","messages":[{"role":"user","content":"hi"}]}' \
  http://127.0.0.1:4000/v1/chat/completions
curl -s http://127.0.0.1:9464/metrics | grep lumewarp_plugin_calls_total
```

Standalone YAML has no MCP servers or A2A agents, so `upstream-auth` and `a2a-middleware`
are verified end to end with a control plane: select the plugin under Auth on an MCP server
or A2A agent, or under Middleware on an agent, in the Admin UI.

## 6. Install

**WASM kinds.** Install the file on **every** gateway before any configuration names it: a
gateway without it rejects the whole config version, and the control plane refuses a save
that names a plugin a connected gateway lacks unless the save is forced.

- Helm: put the files in the volume `gateway.plugins.volume` mounts (`router/`, `auth/`,
  `a2a-middleware/` below `gateway.plugins.mountPath`, all three present even if empty), add
  plugin secrets as `LUMEWARP_PLUGIN_SECRET_*` keys of a Secret listed in
  `gateway.plugins.secretEnvs`, then `kubectl rollout restart` the gateway.
- Elsewhere: set `LUMEWARP_ROUTER_PLUGIN_DIR`, `LUMEWARP_AUTH_PLUGIN_DIR`,
  `LUMEWARP_A2A_MIDDLEWARE_DIR`, and restart.

Once the gateways report it, the plugin appears in the Admin UI's strategy, auth or
middleware picker with a form rendered from its schema.

To change a published plugin, only add optional fields under the same name. Removing or
renaming a field, making one required, or narrowing a type or range needs a new `name`
(`references/plugins.md#compatible-changes`).

**Python kinds.** Install the package into the control plane's environment and restart it.
The control plane image keeps its virtualenv at `/app/.venv`:

```dockerfile
FROM ghcr.io/lumewarp/lumewarp-control-plane:<release>
USER root
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY . /src/plugin
RUN uv pip install --python /app/.venv/bin/python /src/plugin && rm -rf /src/plugin
USER 10001:10001
```

Point the Helm chart's `controlPlane.image` at the derived image. An auth plugin is used as
soon as it is installed. An authz backend is selected with
`LUMEWARP_AUTHZ_BACKEND=<entry point name>` (in `controlPlane.extraEnv`, with the backend's
own settings); an unknown name aborts startup.

## Checklist

- [ ] The request fits one extension point and none of the "say no" cases.
- [ ] The SDK or control-plane tag matches the deployed release.
- [ ] Every `my` name is replaced; the plugin `name` is final.
- [ ] `configure` enforces every bound the schema states, with tests.
- [ ] No secret in settings; no per-request state kept between calls.
- [ ] fmt, clippy and tests pass (or `uv run pytest`, contract suite included).
- [ ] `plugins --json` lists the component (WASM), or the loader test passes (Python).
- [ ] Installed on every gateway before any configuration names it.
