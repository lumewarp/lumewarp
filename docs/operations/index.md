# Deploy

Lumewarp is two programs. The **gateway** (Rust) is what clients call; it serves from an
in-memory config snapshot and keeps the last good one on disk. The **control plane**
(Python, with PostgreSQL) owns the configuration, serves the Admin UI and the management
API, and streams config to gateways over gRPC. Clients never talk to the control plane, and
a gateway keeps serving while the control plane is down.

## Choose a way to deploy

| | Use it for | Page |
| --- | --- | --- |
| Helm | Kubernetes: the control plane, a gateway (several pods may share one token), migrations as a hook, optional ServiceMonitors. | [Deploy with Helm](./helm.md) |
| Docker Compose | One host: Postgres, the control plane and a gateway from the published images. | [Deploy with Docker](./docker.md) |
| Standalone gateway | A gateway alone, configured by one YAML file: no control plane, database, Admin UI, usage or self-service. | [Deploy with Docker](./docker.md#a-standalone-gateway), [Quick start](../guide/quickstart-standalone.md) |

## Images and chart

Each release publishes:

- `ghcr.io/lumewarp/lumewarp-gateway:<version>` and
  `ghcr.io/lumewarp/lumewarp-control-plane:<version>` (stable releases also `X.Y` and
  `latest`);
- the Helm chart at `oci://ghcr.io/lumewarp/charts/lumewarp`;
- on the GitHub release: the example WASM plugins with their `SHA256SUMS`, and both OpenAPI
  documents (`openapi-control-plane.json`, `openapi-gateway.json`).

| Image | Runs | User | Writable path |
| --- | --- | --- | --- |
| `lumewarp-gateway` | `lumewarp` (distroless). Arguments are the gateway's [flags](./gateway.md#flags). | 65532 | `/var/lib/lumewarp`, for the cached snapshot |
| `lumewarp-control-plane` | `python -m lumewarp_control_plane serve` (Python 3.12, with the Admin UI built in). The command is a [CLI command](./control-plane.md#cli): `migrate`, `bootstrap`, `hash-password`, ... | 10001 | none |

While Lumewarp is 0.x a release has no upgrade path: drop the control-plane database before
installing a new one ([Upgrades](./upgrades.md)).

### Building images

Both Dockerfiles use the repository root as build context, so the gateway can read `proto/`
and the control-plane image can build `ui/`:

```sh
docker build -f gateway/Dockerfile -t registry.example/lumewarp-gateway:dev .
docker build -f control-plane/Dockerfile -t registry.example/lumewarp-control-plane:dev .
```

## Ports

| Component | Port | What | Expose to |
| --- | --- | --- | --- |
| Gateway | 4000 | `/v1/*`, `/mcp`, `/a2a`: the API clients call | Clients |
| Gateway | 9464 | Admin: `/metrics`, `/readyz`, `/healthz`. **No authentication.** | Prometheus and probes only |
| Control plane | 8091 | Admin UI, `/api/v1`, `/healthz`, `/readyz`, `/docs` | Administrators and members (through HTTPS) |
| Control plane | 50051 | gRPC config stream and usage reports | Gateways only |
| Control plane | 9465 | Prometheus metrics (`lumewarp_cp_*`) | Prometheus only |

The listeners bind loopback by default; in a container set them to all interfaces and let
the platform decide what is reachable. Terminate TLS in front of both: the gateway and the
control plane's HTTP listener speak plain HTTP (the gRPC listener can use TLS,
[settings](./control-plane.md#settings)).

## What every deployment needs

| Setting | Where | Why |
| --- | --- | --- |
| `LUMEWARP_DATABASE_URL` | Control plane | PostgreSQL 16+, as a SQLAlchemy async URL. |
| `migrate`, then `bootstrap --name <label>` | Control plane CLI, once | Creates the schema, the config scope and the first gateway token (`lwg-…`, shown once; `--name` is optional). |
| Gateway token | Gateway | `--gateway-token-env` names the variable holding the token. The token authenticates the scope, so any number of gateways may share it; each process generates its own id. |
| A persistent `--state-dir` | Gateway | The last good snapshot, so a restart while the control plane is down still serves. |
| `LUMEWARP_BREAK_GLASS_PASSWORD_HASH` | Control plane | Emergency login, and the way to sign in before SSO is set up. An argon2id hash from `hash-password`. |
| `LUMEWARP_ENCRYPTION_KEY` | Control plane **and every gateway**, same value | Seals upstream API keys entered in the Admin UI. |
| OIDC settings and `LUMEWARP_SECRET_KEY` | Control plane | Sign-in for everyone else ([Sign-in and authorization](./identity.md)). |
| `LUMEWARP_GATEWAY_ID_KEY`, `LUMEWARP_GATEWAY_PUBLIC_URL` | Gateway | Only for MCP and A2A; without them those endpoints answer 503. |

Run exactly one control-plane process per installation.
