# Lumewarp control plane

Management API, config compiler, gateway-facing gRPC services (config stream and usage
ingest), budget/notifier workers and the plugin host. It never runs on the gateway request
path: the gateway only reads the immutable snapshot it last ACKed. Design:
[`../docs/architecture.md`](../docs/architecture.md); stream contract:
[`../docs/config-protocol.md`](../docs/config-protocol.md).

One Python process serves everything: FastAPI (API + Admin UI), `grpc.aio`
(`ConfigService`, `UsageService`, gRPC health), the compiler loop and the background
schedulers all start and stop in the FastAPI lifespan. Running more than one process per
installation is not supported in P1.

## Requirements

- Python 3.12+ and [uv](https://docs.astral.sh/uv/) (the repo has `uv.lock`).
- PostgreSQL 16+. The dev compose file runs one on `127.0.0.1:55432`.
- Node.js/pnpm only to build the Admin UI; the control plane serves static files and never
  runs Node at runtime.

## Quick start

From `control-plane/`:

```sh
../scripts/dev-db.sh up                          # postgres:16 on 127.0.0.1:55432
uv sync                                          # project + dev dependencies
uv run --no-sync lumewarp-control-plane migrate  # alembic upgrade head

# Creates the config scope and prints one `lwg-...` gateway token (shown once).
uv run --no-sync lumewarp-control-plane bootstrap --gateway-id gw-local

LUMEWARP_BOOTSTRAP_TOKEN=dev-admin \
  uv run --no-sync lumewarp-control-plane serve  # HTTP :8091, gRPC :50051
```

In another terminal:

```sh
curl -sS http://127.0.0.1:8091/readyz
curl -sS http://127.0.0.1:8091/healthz
```

Use `--no-sync` only when the environment is already synced; plain `uv run` also works.
`migrate` must run before `serve`: readiness checks that the database is at the migration
head. `bootstrap` also needs the schema, and creates the scope row if it does not exist.
Each `bootstrap` call creates a **new** token; old tokens stay valid until revoked.

Gateway side (see [gateway README](../gateway/README.md) for every flag):

```sh
export LUMEWARP_GATEWAY_TOKEN='lwg-...'
lumewarp --control-plane http://127.0.0.1:50051 \
  --gateway-token-env LUMEWARP_GATEWAY_TOKEN \
  --state-dir /tmp/lumewarp-state --scope default --gateway-id gw-local
```

The same token authenticates the config stream and usage reports. The control plane stores
only its SHA-256 hash.

## Ports and endpoints

| Listener | Default | Serves |
| --- | --- | --- |
| HTTP | `127.0.0.1:8091` | Admin UI at `/`, REST at `/api/v1`, `/docs` and `/openapi.json`, `/healthz` and `/readyz` |
| gRPC | `127.0.0.1:50051` | `lumewarp.config.v1.ConfigService`, `lumewarp.usage.v1.UsageService`, `grpc.health.v1.Health` |

Both listeners are plaintext unless `LUMEWARP_GRPC_TLS_CERT_FILE` and
`LUMEWARP_GRPC_TLS_KEY_FILE` configure TLS for the gRPC listener. The HTTP listener has no
TLS; terminate it outside the process. Set both binds to `0.0.0.0:...` in a container.

### Readiness and lifecycle

- `GET /readyz` returns 200 only when the database is reachable, migrations are at head,
  and the gRPC server, compiler loop, rollover scheduler and notifier loop are alive. It
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

| Command | What it does |
| --- | --- |
| `serve` | HTTP API + Admin UI (uvicorn, one worker), gRPC servers and background loops. `--http-bind` / `--grpc-bind` override the env settings. |
| `migrate [REV]` / `migrate --downgrade REV` | Apply or roll back Alembic migrations (default `head`). |
| `openapi [-o FILE]` | Print the OpenAPI document; builds the app without connecting to the database. |
| `bootstrap --gateway-id ID [--name N] [--scope S]` | Create the config scope row and a `lwg-...` gateway token; the secret goes to stdout once. |

The console script is `lumewarp-control-plane`; `python -m lumewarp_control_plane` is
equivalent.

## Settings

`pydantic-settings` reads these from the environment with the `LUMEWARP_` prefix; names are
case-insensitive. `src/lumewarp_control_plane/settings.py` is the source of truth.

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `LUMEWARP_DATABASE_URL` | `postgresql+asyncpg://lumewarp:lumewarp@127.0.0.1:55432/lumewarp` | SQLAlchemy async URL used by the API, gRPC services, workers and Alembic. |
| `LUMEWARP_DB_POOL_SIZE` | `10` | SQLAlchemy connection pool size per process. |
| `LUMEWARP_HTTP_BIND` | `127.0.0.1:8091` | HTTP API/UI bind address (`host:port`, IPv6 hosts in brackets). |
| `LUMEWARP_GRPC_BIND` | `127.0.0.1:50051` | Gateway-facing gRPC bind address. |
| `LUMEWARP_GRPC_TLS_CERT_FILE` / `LUMEWARP_GRPC_TLS_KEY_FILE` | unset | PEM certificate chain and key. Both or neither; unset means plaintext gRPC. |
| `LUMEWARP_BOOTSTRAP_TOKEN` | unset | Installation-level superuser Bearer token. Compared in constant time, never stored. Unset disables bootstrap access; PATs still work. |
| `LUMEWARP_SCOPE` | `default` | `Snapshot.scope` this installation publishes. Gateways must use the same value. |
| `LUMEWARP_MAX_SNAPSHOT_BYTES` | `67108864` (64 MiB) | Compilation fails above this serialized size. |
| `LUMEWARP_MAX_SNAPSHOT_KEYS` | `200000` | Compilation fails above this key count. |
| `LUMEWARP_GRPC_MAX_MESSAGE_BYTES` | `67108864` (64 MiB) | gRPC send/receive limit, additionally capped by grpcio's hard limit. |
| `LUMEWARP_ACK_TIMEOUT_SECONDS` | `30` | Close a config session that has not ACKed/NACKed within this time. |
| `LUMEWARP_CONFIG_VERSIONS_RETAINED` | `50` | Versions whose payloads compaction keeps (the latest is always kept; session-pinned bases are kept too). |
| `LUMEWARP_MAX_USAGE_BATCH_EVENTS` | `10000` | Maximum events per `UsageService.Report` (1–10000; keeps asyncpg's parameter count in range). |
| `LUMEWARP_BACKGROUND_ENABLED` | `true` | Start gRPC and the loops in `serve`. Tests set `false`; keep it true in production. |

## Bootstrap credentials and RBAC

There are four credential types, all stored only as SHA-256 hashes except the bootstrap
token, which is never stored:

| Credential | Prefix | Used by |
| --- | --- | --- |
| Bootstrap token | none | Installation superuser over the admin API/UI; comes from `LUMEWARP_BOOTSTRAP_TOKEN`. |
| Personal access token (PAT) | `lwp-` | A user over the admin API/UI; created under Settings in the UI or `POST /api/v1/users/me/tokens`. |
| Gateway token | `lwg-` | A gateway for the config stream and usage reporting; the first one comes from `bootstrap` (which also creates the scope), later ones from the gateways API/UI. |
| Virtual API key | `lw-` | Clients calling the gateway; created in a project; the gateway only receives its hash in the snapshot. |

The bootstrap token is not tied to a user: it is an installation superuser, has no PATs of
its own, and is meant to create the first users/memberships. Installation superusers
(bootstrap or a superuser PAT) can issue a PAT for an active user with
`POST /api/v1/users/{user_id}/tokens` using `{"name":"initial access"}` (optional
`expires_at`). The secret is returned only on creation; issuance is audited with the
actor and target user, never the secret. Org/team admins cannot use this endpoint.
RBAC roles are Owner, Admin,
Member and Viewer at organization/team scope; child scopes can only narrow their parent's
allowlists, limits and budgets. See `rbac.py` and the API schema for the exact matrix.

## Admin UI

The UI is a Vite SPA compiled to static files, not a Node runtime:

```sh
cd ../ui
pnpm install
pnpm build:cp    # builds ui/dist and copies it into control-plane/src/.../static
```

`static.py` mounts the build at `/`: hashed `assets/*` are immutable, other files are
revalidated, and unknown paths without a file extension fall back to `index.html` for
client-side routing. `/api/*`, `/healthz`, `/readyz` and `/docs` are never answered with the
SPA; missing assets are JSON 404s. Without a build the control plane still serves the API
and docs, and logs a warning at startup. Restart the process after copying a new build. The
wheel build includes the static directory as package data.

Log in at `/` with a PAT or the bootstrap token. The API types in `ui/src/api/schema.d.ts`
are generated from this service's OpenAPI document (`pnpm gen:api`).

## Plugins

Plugins run **only** in the control plane and produce declarative output that the compiler
bakes into the snapshot; they never run in the gateway request path. They are discovered
through Python entry points, so install the package into the same environment (`uv pip
install -e ./your-plugin`) and restart `serve`:

| Group | Purpose | P1 status |
| --- | --- | --- |
| `lumewarp.router` | Adjust deployment weights at compile time (GPU cost, time of day, ...). | Called by the compiler. |
| `lumewarp.notifier` | Deliver budget notifications for channels whose `kind` is the entry-point name. | Called by the notifier outbox loop. |
| `lumewarp.auth` | Map an external identity (OIDC, LDAP, ...) to a user and grants. | Protocol only; not called in P1 (P2 SSO). |

Every plugin declares `api_version = 1`; other versions are skipped with a warning, as are
plugins that fail to import. Entry points may name an instance, a class or a zero-argument
factory (classes/factories are called once at load).

```toml
# your-plugin/pyproject.toml
[project.entry-points."lumewarp.router"]
my-weights = "my_plugin.policy:Policy"
```

```python
# my_plugin/policy.py
from lumewarp_control_plane.plugins import WeightOverride


class Policy:
    api_version = 1

    def adjust(self, routes):  # sync or async
        return [WeightOverride(deployment="gpu-a", weight=3)]
```

Router plugins run in entry-point name order; each call, result iteration and validation
runs off the event loop with a 2 s budget. A plugin that raises, times out, returns invalid
output (unknown deployment, weight outside 0..2^31-1, too many overrides) or would leave a
weighted route with zero total weight is discarded as a whole; the compile continues.
Later plugins win on conflicts. Notifier plugins get the channel's `config` mapping and a
`BudgetEvent`; raising makes the outbox retry with backoff. The built-in notifier kind is
`webhook` (JSON POST of the event).

## Config publishing and usage

- Every source mutation (API write, budget flip, rollover) commits with an audit row and an
  "recompile" outbox row in one transaction. One compiler loop per scope, serialized with a
  Postgres advisory lock, expands inheritance and writes an immutable, complete
  `config_versions` snapshot with digest and source revision. A failed compile leaves the
  previous version current. Serialization conflicts retry with bounded jitter. Unchanged
  compiled content reuses the latest version, including after a control-plane restart;
  publication metadata is excluded from the content comparison.
- Gateways get a full snapshot, then deltas; each update is applied, validated, persisted
  and swapped by the gateway before it ACKs. NACK `BASE_MISMATCH` triggers a full,
  `INVALID_CONFIG`/`UNSUPPORTED_FEATURE` quarantines the version, `PERSIST_FAILED` retries.
  Rollback republishes an older version's **routing** only; keys and policies always come
  from current source state. Full rules: [config protocol](../docs/config-protocol.md).
- `UsageService.Report` is all-or-nothing and idempotent by `request_id`; each new event
  adds cost to key, project, team and org totals for the UTC calendar month in the same
  transaction, then budget evaluation flips exhausted scopes to `BUDGET_EXCEEDED` and
  publishes a new version (eventual; in-flight streams are not cut). Soft-limit
  notifications go through the notifier outbox. Budget publishing targets the protocol's
  5 s eventual-enforcement window; it is not a configurable per-flip timeout. The unused
  `LUMEWARP_BUDGET_FLIP_DEADLINE_SECONDS` setting has been removed.
- P1 stores usage in Postgres; ClickHouse is later. While the control plane is down the
  gateway keeps serving from its snapshot, but usage batches queue in memory and are lost
  if the queue fills or the gateway restarts.

## Deployment

The Helm chart in [`../deploy/helm/lumewarp`](../deploy/helm/lumewarp) deploys this
Deployment with `/readyz` and `/healthz` probes, the HTTP/gRPC Service, the DB URL Secret
and a migration hook Job. The control plane must stay at one replica.

## Tests

```sh
uv run ruff check . && uv run ruff format --check . && uv run pytest
```

Tests need PostgreSQL: each run creates `lumewarp_test_<random>` on the server from
`LUMEWARP_TEST_DATABASE_URL` (default: the dev DB above), migrates it, and drops it at the
end.

## Schema changes

Edit `models.py`, then with the dev DB at head:

```sh
uv run alembic revision --autogenerate -m "what changed"
```

Review the generated file (autogenerate duplicates enum CHECK constraints; keep the ones on
the `sa.Enum` columns). `tests/test_migrations.py` fails if models and migrations drift.

## Protobuf

Generated modules are checked in under `src/lumewarp_control_plane/_proto/`. After changing
`../proto/`, run `../scripts/gen-proto.sh` (idempotent; generator pinned via
`grpcio-tools`).

## Layout

| Module | Purpose |
| --- | --- |
| `settings.py`, `db.py`, `models.py`, `migrations/`, `migrate.py` | config, DB, schema |
| `security.py`, `rbac.py`, `errors.py`, `audit.py`, `outbox.py` | auth, RBAC matrix, error shape, audit, recompile outbox |
| `api/` | REST under `/api/v1` |
| `compiler/`, `plugins.py` | snapshots, versions, diff, rollback, plugin host |
| `grpc/`, `usage/`, `notifier/` | ConfigService/UsageService, budgets, notifications |
| `app.py`, `__main__.py`, `static.py` | app factory, lifespan, CLI, Admin UI static serving |
