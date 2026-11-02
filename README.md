# Lumewarp

A fast, manageable LLM gateway for OpenAI-compatible upstreams (vLLM, OpenAI).
Lumewarp has a Rust data plane (the gateway) and a Python control plane
(management API, config compiler, Admin UI). The request path only reads an
immutable in-memory config snapshot; it never touches Postgres or Python. Config
updates and usage events are asynchronous.

P2 is implemented: the control plane with Postgres, the versioned gRPC config
stream with on-disk snapshot boot, teams with members and service accounts owning
virtual keys, budgets and rate limits, usage accounting, plugins and the Admin UI
(P1); plus OIDC login for the Admin UI, pluggable authorization (built-in, OpenFGA
or HTTP) with automatic key suspension, retries/fallbacks/circuit breaking/health
checks, latency- and vLLM-load-aware routing, traffic splits, alert rules and
dashboards (P2). See the [architecture document](docs/architecture.md) for the
design and its “P1 已實作” / “P2 已實作” / “模型目錄已實作” status; deferred work includes
JWT/mTLS client auth, MCP/A2A and an HA control plane.

Clients call a name, and a name is a **model** (one upstream model), an **alias** (another
name for a model or router) or a **router** (a strategy over models, with retries, fallbacks
and traffic splits). Responses carry the called name, usage records what actually served it,
and names can be renamed without breaking references. New router strategies are Rust crates
([docs/router-strategies.md](docs/router-strategies.md)).

| Directory | What |
| --- | --- |
| `gateway/` | Data plane (Rust) |
| `control-plane/` | Management API, config compiler, plugins (Python, uv) |
| `ui/` | Admin UI (React + Vite, static; served by the control plane) |
| `proto/` | Config snapshot + config/usage gRPC schema |
| `bench/` | Mock upstream and Lumewarp-vs-LiteLLM load tests |
| `deploy/` | Helm chart, dev compose, Prometheus/Grafana scaffolding |

## Standalone gateway quick start

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
  - {name: mock-local, upstream: local, upstream_model: mock}
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
or `keys`). See the [gateway reference](gateway/README.md) for authentication,
every YAML field, header handling, errors, metrics, tracing, and limitations.

## Control-plane quick start

The control plane owns the config; the gateway follows it over gRPC and keeps the
last ACKed snapshot on disk so it can keep serving (and restart) while the control
plane is down.

Start the mock upstream as above, then Postgres and the control plane:

```sh
./scripts/dev-db.sh up
cd control-plane
uv sync

# Build the Admin UI once (Node 24 + pnpm); the control plane serves it at /.
# Skip this for an API-only setup.
(cd ../ui && pnpm install && pnpm build:cp)

uv run lumewarp-control-plane migrate
uv run lumewarp-control-plane bootstrap --gateway-id gw-local   # prints the lwg-... token once
LUMEWARP_DEV_MODE=true LUMEWARP_BREAK_GLASS_PASSWORD=dev-break-glass \
  LUMEWARP_SESSION_COOKIE_SECURE=false \
  uv run lumewarp-control-plane serve
```

The HTTP API/UI listens on `127.0.0.1:8091`; gateway-facing gRPC listens on
`127.0.0.1:50051`. The dev Postgres from `scripts/dev-db.sh` listens on
`127.0.0.1:55432`, so it does not collide with a local 5432.

Start the gateway in another terminal:

```sh
cd gateway
export LUMEWARP_GATEWAY_TOKEN='lwg-...'
cargo run --release -- --control-plane http://127.0.0.1:50051 \
  --gateway-token-env LUMEWARP_GATEWAY_TOKEN \
  --state-dir /tmp/lumewarp-state --scope default --gateway-id gw-local
```

The gateway is not ready until it has a first config version (`/v1/*` and the admin
`/readyz` return 503), and the process never falls back to an open router. The control
plane publishes an empty version when it starts, so here the gateway is ready at once
and answers 401 until a key exists. Open <http://127.0.0.1:8091/>, use the emergency login with the
password `dev-break-glass` (a plaintext password needs `LUMEWARP_DEV_MODE=true`; production
uses an argon2id hash and OIDC with an admin group, see the
[control plane README](control-plane/README.md#credentials-and-access-model)), then create:

- an upstream for `http://127.0.0.1:9100/v1` (kind `openai_compatible`),
- a model named `mock-local` using upstream model `mock`,
- a router named `demo` with that model as its member (`mock-local` is callable by
  itself too; a router adds strategy, retries and fallbacks, and an alias is one more name),
- a team and an API key for a service account of that team (the `lw-...` secret is
  shown once).

Creating those resources publishes a snapshot; the gateway applies it and accepts
the key:

```sh
curl -sS -H "Authorization: Bearer lw-..." -H 'Content-Type: application/json' \
  http://127.0.0.1:4000/v1/chat/completions \
  --data-binary '{"model":"demo","messages":[{"role":"user","content":"Hello"}]}'
```

Usage lands in the UI after the next report batch (about one second); budgets are
enforced eventually. See the [control-plane README](control-plane/README.md) for
settings, SSO, authorization backends, the catalog, plugins and operations, and
[`docs/config-protocol.md`](docs/config-protocol.md) for the stream contract.

## Container images

Both Dockerfiles use the repository root as build context so the gateway can read
`proto/` and the control-plane image can build `ui/`:

```sh
docker build -f gateway/Dockerfile -t ghcr.io/lumewarp/lumewarp-gateway:0.1.0 .
docker build -f control-plane/Dockerfile -t ghcr.io/lumewarp/lumewarp-control-plane:0.1.0 .
```

The gateway image is distroless, runs as non-root and includes a writable
`/var/lib/lumewarp` for the cached snapshot. The control-plane image runs the
uv-installed package on Python 3.12 as non-root and includes the built Admin UI.
The [Helm chart](deploy/helm/lumewarp/README.md) deploys both; set the image
repositories/tags in its values.

## More documentation

- [Gateway reference](gateway/README.md) — modes, flags, YAML schema, auth,
  streaming, errors, metrics, tracing.
- [Control plane](control-plane/README.md) — uv install, settings, readiness,
  RBAC, plugins, Admin UI, migrations.
- [Architecture](docs/architecture.md) and
  [config stream protocol](docs/config-protocol.md).
- [Helm chart](deploy/helm/lumewarp/README.md) and
  [benchmark guide](bench/README.md).

Licensed under Apache-2.0.
