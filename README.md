# Lumewarp

A fast, manageable LLM gateway for OpenAI-compatible upstreams (vLLM, OpenAI).
P0 ships the standalone Rust data plane. The Python control plane and React admin
UI are part of the [architecture roadmap](docs/architecture.md).

| Directory | What |
| --- | --- |
| `gateway/` | Data plane (Rust) |
| `control-plane/` | Management API, config compiler, plugins (Python, uv) |
| `ui/` | Admin UI (React + Vite, static) |
| `proto/` | Config snapshot + config-stream gRPC schema |
| `bench/` | Mock upstream and Lumewarp-vs-LiteLLM load tests |
| `deploy/` | Helm chart, Prometheus rules, Grafana dashboards |

## Gateway quick start

Start a local OpenAI-compatible mock (separate terminal):

```sh
cargo build --release --manifest-path bench/mock-upstream/Cargo.toml
bench/mock-upstream/target/release/mock-upstream --listen 127.0.0.1:9100
```

Save as `/tmp/lumewarp.yaml` (local demo; no client auth):

```yaml
server:
  listen: 127.0.0.1:4000
  admin_listen: 127.0.0.1:9464
upstreams:
  - {name: local, kind: openai_compatible, base_url: 'http://127.0.0.1:9100/v1'}
deployments:
  - {name: mock-local, upstream: local, upstream_model: mock}
routes:
  - {model: demo, deployments: [mock-local]}
```

Run the gateway, then use another terminal for curl:

```sh
cargo run --release --manifest-path gateway/Cargo.toml -- --config /tmp/lumewarp.yaml
```

```sh
curl -sS -H 'Content-Type: application/json' http://127.0.0.1:4000/v1/chat/completions \
  --data-binary '{"model":"demo","messages":[{"role":"user","content":"Hello"}]}'
curl -sS -N -H 'Content-Type: application/json' http://127.0.0.1:4000/v1/chat/completions \
  --data-binary '{"model":"demo","messages":[{"role":"user","content":"Hello"}],"stream":true}'
curl -sS http://127.0.0.1:9464/metrics
```

See the [gateway reference](gateway/README.md) for authentication, every YAML
field, header handling, errors, metrics, tracing, and P0 limits. See the
[benchmark guide](bench/README.md) for load tests and measurement definitions.

Licensed under Apache-2.0.
