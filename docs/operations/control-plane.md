# Control plane settings

One Python process serves everything: FastAPI (API + Admin UI), `grpc.aio`
(`ConfigService`, `UsageService`, gRPC health), the compiler loop, the background
schedulers and the Prometheus metrics listener all start and stop in the FastAPI lifespan. Running more than one process per
installation is not supported.

## Requirements

- Python 3.12+ and [uv](https://docs.astral.sh/uv/) (the repo has `uv.lock`).
- PostgreSQL 16+. The dev compose file runs one on `127.0.0.1:55432`.
- Node.js/pnpm only to build the Admin UI; the control plane serves static files and never
  runs Node at runtime.

## Ports and endpoints

| Listener | Default | Serves |
| --- | --- | --- |
| HTTP | `127.0.0.1:8091` | Admin UI at `/`, REST at `/api/v1`, the user-facing MCP endpoint at `/mcp`, `/docs` and `/openapi.json`, `/healthz` and `/readyz` |
| gRPC | `127.0.0.1:50051` | `lumewarp.config.v1.ConfigService`, `lumewarp.usage.v1.UsageService`, `grpc.health.v1.Health`. Every gateway holds one stream; any number of gateways may connect with the same token ([protocol](../config-protocol.md#gateway-sessions)). |
| Metrics | `127.0.0.1:9465` | Prometheus text format on any path (`/metrics`); see [Metrics](../reference/metrics.md). `LUMEWARP_METRICS_BIND=""` disables it. |

All listeners are plaintext unless `LUMEWARP_GRPC_TLS_CERT_FILE` and
`LUMEWARP_GRPC_TLS_KEY_FILE` configure TLS for the gRPC listener. The HTTP listener has no
TLS; terminate it outside the process. Set the binds to `0.0.0.0:...` in a container. The
metrics listener is separate from the HTTP port on purpose: it is unauthenticated, so keep
it off the public admin ingress.

### Readiness and lifecycle

- `GET /readyz` returns 200 only when the database is reachable, migrations are at head,
  and the gRPC server, compiler loop, rollover scheduler and (with a metrics listener) the
  metrics gauge refresher are alive. It
  returns 503 with `{"status":"not_ready","checks":{...}}` otherwise.
- `GET /healthz` is liveness only: the HTTP process is answering.
- The gRPC server is the critical background service. If it stops unexpectedly the process
  logs a critical error and sends itself SIGTERM so Kubernetes restarts it; other loops
  failing make readiness fail without exiting.
- SIGTERM stops accepting gRPC streams, signals the loops and waits up to 5 s
  (`SHUTDOWN_GRACE_SECONDS`) before cancelling what is left.
- `serve --workers` must be 1. The CLI refuses any other value: there is exactly one
  compiler/scheduler instance per installation.

## CLI

`serve` creates the default zone named by `LUMEWARP_ZONE` and a zone for every configured
gateway token at startup if each row is missing. The insert is idempotent and leaves an
existing row alone, including an archived row. One compiler task iterates all live zones;
each zone has its own source revision, outbox and config version stream.

| Command | What it does |
| --- | --- |
| `serve` | HTTP API + Admin UI (uvicorn, one worker), gRPC servers and background loops. `--http-bind` / `--grpc-bind` override the env settings. |
| `migrate [REV]` | Apply Alembic migrations up to `REV` (default `head`). Downgrades are not supported; restore a database backup instead. |
| `openapi [-o FILE]` | Print the OpenAPI document; builds the app without connecting to the database. |
| `hash-password` | Read a password from stdin (or prompt for it) and print its argon2id hash for `LUMEWARP_BREAK_GLASS_PASSWORD_HASH`. |

The console script is `lumewarp-control-plane`; `python -m lumewarp_control_plane` is
equivalent.

## Settings

`pydantic-settings` reads these
from the environment with the `LUMEWARP_` prefix; names are
case-insensitive. `src/lumewarp_control_plane/settings.py` is the source of truth.

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `LUMEWARP_DATABASE_URL` | `postgresql+asyncpg://lumewarp:lumewarp@127.0.0.1:55432/lumewarp` | SQLAlchemy async URL used by the API, gRPC services, workers and Alembic. |
| `LUMEWARP_DB_POOL_SIZE` | `10` | SQLAlchemy connection pool size per process. |
| `LUMEWARP_HTTP_BIND` | `127.0.0.1:8091` | HTTP API/UI bind address (`host:port`, IPv6 hosts in brackets). |
| `LUMEWARP_GRPC_BIND` | `127.0.0.1:50051` | Gateway-facing gRPC bind address. |
| `LUMEWARP_METRICS_BIND` | `127.0.0.1:9465` | Prometheus metrics listener started by `serve` (`host:port`); empty string disables it. Not the admin API port, and not 9464 (the gateway admin listener's default). A bind failure aborts startup. |
| `LUMEWARP_GRPC_TLS_CERT_FILE` / `LUMEWARP_GRPC_TLS_KEY_FILE` | unset | PEM certificate chain and key. Both or neither; unset means plaintext gRPC. |
| `LUMEWARP_INITIAL_ADMIN_EMAIL` | unset | Email that becomes `proxy_admin` on its first OIDC login while no proxy_admin exists. Unset leaves break-glass as the only way to create the first one. |
| `LUMEWARP_ALERTMANAGER_TOKEN` | unset | Bearer token of the Alertmanager webhook receiver, at least 32 characters. Unset, `POST /api/v1/alerts/alertmanager` answers 404; an empty or short value aborts startup. See [Alerts and incidents](./alerts-and-incidents.md). |
| `LUMEWARP_API_MCP_ENABLED` | `true` | Serve the user-facing MCP endpoint at `/mcp` (`api_mcp/`), authenticated by personal access tokens. `false` makes `/mcp` and its protected-resource metadata 404. See [Use Lumewarp from an MCP client](../self-service/mcp-client.md). |
| `LUMEWARP_API_MCP_ALLOWED_ORIGINS` | empty | Comma-separated browser origins allowed to call `/mcp`, for example `https://app.example`. Empty allows only the origin of `LUMEWARP_PUBLIC_URL`, or no browser origin when that is unset; a request without an `Origin` header is always accepted. |
| `LUMEWARP_OIDC_REQUIRE_EMAIL_VERIFIED` | `true` | Require `email_verified: true` before an OIDC email links an existing account, creates a user or bootstraps the initial admin. `false` trusts the claim and startup logs a warning; see [Sign-in and authorization](./identity.md#sso-login-oidc). |
| `LUMEWARP_BREAK_GLASS_PASSWORD_HASH` | unset | Argon2id hash of the emergency password (`lumewarp-control-plane hash-password`); an invalid hash aborts startup. See [Emergency access](./identity.md#emergency-access-break-glass). |
| `LUMEWARP_BREAK_GLASS_PASSWORD` | unset | Plaintext emergency password; refused unless `LUMEWARP_DEV_MODE=true`. The hash wins if both are set. |
| `LUMEWARP_DEV_MODE` | `false` | Development only: allows the plaintext password above. |
| `LUMEWARP_BREAK_GLASS_SESSION_TTL_SECONDS` | `3600` | Emergency session lifetime, 60–14400. |
| `LUMEWARP_BREAK_GLASS_ALLOWED_CIDRS` | empty (any) | Client networks allowed to use the emergency login; others get 404. |
| `LUMEWARP_TRUSTED_PROXIES` | empty | Proxy networks whose `X-Forwarded-For` is followed to find the client address for the two limits above. |
| `LUMEWARP_AUTHZ_BACKEND` | `builtin` | Authorization backend for direct org/team relations: `builtin` (`org_admins` + `team_members`), `openfga`, `http` or a `lumewarp.authz` entry-point name. See [Authorization](../admin/index.md); the OpenFGA settings are in [Identity](./identity.md#openfga). |
| `LUMEWARP_ZONE` | `default` | Default config zone created at startup. Gateways authenticate into a zone with that zone's fixed environment token. |
| `LUMEWARP_GATEWAY_ZONE_<ID>_TOKEN` | unset | One fixed token per zone; `<ID>` is the uppercase zone ID with `-` replaced by `_`. Requires at least 32 characters. Declares the zone at startup; only its SHA-256 hash is kept in memory. |
| `LUMEWARP_MAX_SNAPSHOT_BYTES` | `67108864` (64 MiB) | Compilation fails above this serialized size. |
| `LUMEWARP_MAX_SNAPSHOT_KEYS` | `200000` | Compilation fails above this key count. |
| `LUMEWARP_GRPC_MAX_MESSAGE_BYTES` | `67108864` (64 MiB) | gRPC send/receive limit, additionally capped by grpcio's hard limit. |
| `LUMEWARP_ACK_TIMEOUT_SECONDS` | `30` | Close a config session that has not ACKed/NACKed within this time. |
| `LUMEWARP_CONFIG_VERSIONS_RETAINED` | `50` | Versions whose payloads compaction keeps (the latest is always kept; session-pinned bases are kept too). |
| `LUMEWARP_MAX_USAGE_BATCH_EVENTS` | `10000` | Maximum events per `UsageService.Report` (1–10000; keeps asyncpg's parameter count in range). |
| `LUMEWARP_BACKGROUND_ENABLED` | `true` | Start gRPC and the loops in `serve`. Tests set `false`; keep it true in production. |

To rotate a gateway token, set the same new value in the control plane's zone variable and
the gateway's environment variable, then restart both processes so they load it. There is
no grace period; gateways continue serving their last good snapshot while reconnecting.

Sign-in, session, authorization backend and key reconciler settings are listed in
[Sign-in and authorization](./identity.md).

## Admin UI

The UI is a Vite SPA compiled to static files, not a Node runtime:

```sh
cd ../ui
pnpm install
pnpm build:cp    # builds ui/dist and copies it into control-plane/src/.../static
```

`static.py` mounts the build at `/`: hashed `assets/*` are immutable, other files are
revalidated, and unknown paths without a file extension fall back to `index.html` for
client-side routing. `/api/*`, `/healthz`, `/readyz`, `/mcp`, `/.well-known/*` and `/docs` are
never answered with the SPA; missing assets are JSON 404s. Without a build the control plane still serves the API
and docs, and logs a warning at startup. Restart the process after copying a new build. The
wheel build includes the static directory as package data.

## Deployment

[Deploy with Helm](./helm.md) runs this service with `/readyz` and `/healthz` probes, the
HTTP/gRPC/metrics Service (plus an optional ServiceMonitor for the metrics port), the
database URL Secret and a migration hook Job. [Deploy with Docker](./docker.md) runs it with
Compose next to Postgres and a gateway. Either way the control plane stays at one replica.
