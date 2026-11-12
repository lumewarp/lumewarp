<p align="center"><img src="docs/public/logo.svg" alt="Lumewarp" width="96" height="96"></p>

# Lumewarp

A fast, manageable LLM gateway for OpenAI-compatible upstreams (vLLM, OpenAI).
Lumewarp has a Rust data plane (the gateway) and a Python control plane
(management API, config compiler, Admin UI). The request path only reads an
immutable in-memory config snapshot; it never touches Postgres or Python. Config
updates and usage events are asynchronous.

Clients call a name, and a name is a **model** (one upstream model), an **alias** (another
name for a model or router) or a **router** (a strategy over models, with retries, fallbacks
and traffic splits). Orgs, teams, members and service accounts own API keys with budgets and
rate limits, and any SSO user can manage their own keys. MCP servers and A2A agents are
proxied on the same gateway. Router strategies, upstream auth and A2A middleware can be
extended with WASM plugins.

## Documentation

The manual is published at <https://lumewarp.github.io/lumewarp/> (source in [`docs/`](docs/index.md)), organised by reader:

| | |
| --- | --- |
| [Getting started](docs/guide/index.md) | What Lumewarp is, concepts, the two quick starts. |
| [Calling the gateway](docs/using/index.md) | Endpoints, errors, MCP and A2A, for client authors. |
| [Your keys](docs/self-service/index.md) | Self-service keys and the Playground. |
| [Administration](docs/admin/index.md) | Roles, teams, keys and limits, the catalog, routers, usage and audit. |
| [Operations](docs/operations/index.md) | Deploying with [Helm](docs/operations/helm.md) or [Docker](docs/operations/docker.md), configuring and observing the control plane and the gateway. |
| [Extending](docs/extending/index.md) | Gateway WASM plugins, control-plane Python plugins, the plugin development skill. |
| [Reference](docs/reference/control-plane-api.md) | Both OpenAPI documents, gateway YAML, errors, metrics. |

To read it as a site with search and the API explorers (needs Node 24 and pnpm, plus uv and
cargo, which generate the API reference):

```sh
cd docs && pnpm install && pnpm dev
```

[`docs/architecture.md`](docs/architecture.md) (in Traditional Chinese) is the design and
the list of what is implemented and what is deferred.

## Quick start

Standalone mode reads one YAML file and needs no control plane or database. With the
repository's OpenAI-compatible mock as the upstream:

```sh
cargo build --release --manifest-path bench/mock-upstream/Cargo.toml
bench/mock-upstream/target/release/mock-upstream --listen 127.0.0.1:9100 &

cat > /tmp/lumewarp.yaml <<'EOF'
server: {listen: 127.0.0.1:4000, admin_listen: 127.0.0.1:9464}
upstreams:
  - {name: local, kind: openai_compatible, base_url: 'http://127.0.0.1:9100/v1'}
models:
  - {name: mock-local, upstream: local, upstream_model: mock, type: llm}
EOF
cargo run --release --manifest-path gateway/Cargo.toml -- --config /tmp/lumewarp.yaml
```

```sh
curl -sS -H 'Content-Type: application/json' http://127.0.0.1:4000/v1/chat/completions \
  --data-binary '{"model":"mock-local","messages":[{"role":"user","content":"Hello"}]}'
```

The [standalone quick start](docs/guide/quickstart-standalone.md) continues from here, and
the [control-plane quick start](docs/guide/quickstart-control-plane.md) adds Postgres, the
Admin UI and a gateway that follows it. `make dev` starts the whole stack with an IdP and
[demo accounts](docs/guide/quickstart-control-plane.md#the-full-development-stack).

## Repository

| Directory | What |
| --- | --- |
| `gateway/` | Data plane (Rust); [building and testing](gateway/README.md) |
| `control-plane/` | Management API, config compiler, Python plugins (uv); [building and testing](control-plane/README.md) |
| `ui/` | Admin UI (React + Vite with shadcn/ui on Radix, static; served by the control plane) |
| `proto/` | Config snapshot and config/usage gRPC schema |
| `bench/` | Mock upstream and Lumewarp-vs-LiteLLM load tests; [bench/README.md](bench/README.md) |
| `deploy/` | Helm chart ([README](deploy/helm/lumewarp/README.md)), Docker Compose deployment (`docker/`), dev compose, Prometheus rules, Grafana dashboards |
| `docs/` | The manual (VitePress) |
| `.claude/skills/` | Claude Code skills: `lumewarp-plugin-dev` and `port-router-to-rust` |

## Container images

Both Dockerfiles use the repository root as build context so the gateway can read
`proto/` and the control-plane image can build `ui/`:

```sh
docker build -f gateway/Dockerfile -t ghcr.io/lumewarp/lumewarp-gateway:0.1.0 .
docker build -f control-plane/Dockerfile -t ghcr.io/lumewarp/lumewarp-control-plane:0.1.0 .
```

## Releases and CI

Releases are tags `vX.Y.Z` (`vX.Y.Z-<pre>` for a pre-release). While Lumewarp is
0.x there is no upgrade path: drop the control-plane database before installing
a new release.

```sh
scripts/release.sh 0.2.0                 # sets every version, commits "Release v0.2.0", tags it
git push origin main && git push origin v0.2.0
```

- `ci` runs on pull requests and pushes to `main`. Each job runs only when its
  paths changed since the last successful run on `main`, a newer push cancels an older run,
  and a merge whose tree already passed on its PR is not tested again. The push of a
  `Release v…` commit runs every job. A documentation-only change runs only `docs`, which
  builds the manual and fails on a dead internal link; `plugin-templates` builds the plugin
  skill's templates against the SDK and the control plane in the tree.
- `release` (on the tag) reuses that full run, or runs `ci` itself when there is
  none, then runs load-e2e, the identity e2e and the bench, which never run on
  push. Only when all pass does it push `ghcr.io/lumewarp/lumewarp-gateway` and
  `lumewarp-control-plane` (`X.Y.Z`; stable releases also `X.Y` and `latest`),
  the chart to `oci://ghcr.io/lumewarp/charts/lumewarp`, and create the GitHub
  release with the example WASM plugins and both OpenAPI documents attached.
- `bench` and `Identity e2e gate` also run by hand (`workflow_dispatch`).

Licensed under Apache-2.0.
