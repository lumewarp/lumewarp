# Quick start: standalone gateway

Standalone mode reads one YAML file and needs no control plane or database.
Start a local OpenAI-compatible mock in a separate terminal:

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
models:
  - {name: mock-local, upstream: local, upstream_model: mock, type: llm}
routers:
  - {name: demo, members: [{model: mock-local}]}
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

The gateway requires an API key as soon as the YAML contains one (`server.api_keys`
or `keys`). See the [gateway reference](../operations/gateway.md) for authentication,
every YAML field, header handling, errors, metrics, tracing, and limitations.

## Next

- Add keys, teams and limits to the YAML: [Gateway YAML](../reference/gateway-yaml.md).
- Let a control plane own the configuration instead: [Quick start: with the control plane](./quickstart-control-plane.md).
