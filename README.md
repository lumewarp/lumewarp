# Lumewarp

A fast, manageable LLM gateway for OpenAI-compatible upstreams (vLLM, OpenAI).
Rust data plane, Python control plane, React admin UI. See [docs/architecture.md](docs/architecture.md).

| Directory | What |
| --- | --- |
| `gateway/` | Data plane (Rust) |
| `control-plane/` | Management API, config compiler, plugins (Python, uv) |
| `ui/` | Admin UI (React + Vite, static) |
| `proto/` | Config snapshot + config-stream gRPC schema |
| `bench/` | Mock upstream and Lumewarp-vs-LiteLLM load tests |
| `deploy/` | Helm chart, Prometheus rules, Grafana dashboards |

## Quick start

```sh
cd gateway && cargo run -- --config config.example.yaml   # :4000
cd control-plane && uv sync && uv run lumewarp-control-plane  # :8080
cd ui && pnpm install && pnpm dev
```

Licensed under Apache-2.0.
