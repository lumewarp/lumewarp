# Lumewarp gateway

The data plane: a Rust HTTP/1.1 gateway for OpenAI-compatible upstreams (vLLM, OpenAI), and
the MCP and A2A proxy. It serves requests from an immutable config `Snapshot`
(`proto/lumewarp/config/v1/snapshot.proto`); no control plane, database or Python is on the
request path.

- **Standalone** (`--config`): reads YAML at startup and converts it to a snapshot.
- **Control-plane** (`--control-plane`): boots from a cached snapshot, follows the versioned
  gRPC config stream and reports usage back.

Everything about running and configuring it is in the manual:

| | |
| --- | --- |
| [Standalone quick start](../docs/guide/quickstart-standalone.md) | A YAML file and a mock upstream. |
| [Gateway settings](../docs/operations/gateway.md) | Flags, control-plane mode, boot and readiness, usage reporting, shutdown, limitations. |
| [Gateway YAML](../docs/reference/gateway-yaml.md) | Every standalone field: upstreams, models, routers, teams, keys. |
| [Endpoints](../docs/using/endpoints.md) and [errors](../docs/using/errors.md) | What clients send and get back, passthrough rules, headers. |
| [Metrics](../docs/reference/metrics.md) and [observability](../docs/operations/observability.md) | `lumewarp_*` metrics, timing definitions, OTLP tracing. |
| [Gateway plugins](../docs/plugins.md) and [router strategies](../docs/router-strategies.md) | WASM plugins, MCP and A2A. |

`lumewarp openapi` prints the gateway's OpenAPI document, `lumewarp plugins [--json]` the
plugins it would load. [config.example.yaml](config.example.yaml) has OpenAI and vLLM
examples.

## Crates

| Crate | |
| --- | --- |
| `lumewarp-gateway` | The binary: listeners, auth, routing, proxying, MCP/A2A, config stream, usage, metrics, tracing. |
| `lumewarp-config` | Local gateway settings and the standalone YAML format. |
| `lumewarp-proto` | Types generated from `proto/`: the snapshot, the config stream and usage clients. |
| `lumewarp-router-api` | Candidate views and the built-in strategies, shared with the plugin host and the SDK. |
| `lumewarp-plugin-host` | Loads WASM components at startup and adapts them (`wit/plugin.wit`); links no WASI. |
| `lumewarp-plugin-sdk` | The SDK plugins are written with; example plugins are in `plugins/`. |

## Build and test

```sh
cargo fmt --all --check
cargo clippy --all-targets -- -D warnings
bash plugins/build.sh --tests        # example and test WASM plugins the tests load
cargo build --manifest-path ../bench/mock-upstream/Cargo.toml --bin mcp-a2a-mock
cargo test
```

`gateway/rust-toolchain.toml` adds the `wasm32-wasip2` target the plugins need. A change on
the request path also runs the `bench/` suite ([bench/README.md](../bench/README.md)); a
p99 overhead regression does not ship.

When the built-in plugins change, regenerate the control plane's copy of their descriptors:

```sh
cargo run -p lumewarp-gateway -- plugins --json \
  > ../control-plane/src/lumewarp_control_plane/compiler/builtin_plugins.json
```
