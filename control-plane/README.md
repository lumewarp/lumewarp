# Lumewarp control plane

Management API, config compiler, gateway-facing gRPC services (config stream and usage
ingest), the budget worker and the Python plugin host. It never runs on the gateway request
path: the gateway only reads the immutable snapshot it last ACKed.

One Python process serves everything: FastAPI (API and Admin UI), `grpc.aio`
(`ConfigService`, `UsageService`, gRPC health), the compiler loop, the background schedulers
and the Prometheus metrics listener. Running more than one process per installation is not
supported.

Everything about running and administering it is in the manual:

| | |
| --- | --- |
| [Control-plane quick start](../docs/guide/quickstart-control-plane.md) | Postgres, the Admin UI and a gateway that follows it. |
| [Control plane settings](../docs/operations/control-plane.md) | Requirements, ports, readiness, CLI, every setting, the Admin UI build. |
| [Sign-in and authorization](../docs/operations/identity.md) | Credentials, OIDC, break-glass, authorization backends, the key reconciler. |
| [Administration](../docs/admin/index.md) | Roles, [zones](../docs/admin/zones.md), tenancy, keys and limits, the catalog, routers, usage and audit, config versions. |
| [Control plane API](../docs/reference/control-plane-api.md) | The OpenAPI document (`python -m lumewarp_control_plane openapi`). |
| [Python plugins](../docs/extending/python-plugins.md) | `lumewarp.auth` and `lumewarp.authz`. |
| [Config stream protocol](../docs/config-protocol.md) | The gRPC contract with the gateway. |

## Development

From `control-plane/`, with Python 3.12+, [uv](https://docs.astral.sh/uv/) and the dev
Postgres (`../scripts/dev-db.sh up`, on `127.0.0.1:55432`):

```sh
uv sync
uv run ruff check . && uv run ruff format --check . && uv run pytest
```

Tests create `lumewarp_test_<random>` on the server from `LUMEWARP_TEST_DATABASE_URL`
(default: the dev database), migrate it, and drop it at the end.

### Schema changes

The database baseline has been rebuilt to include zones. An existing development database
from the previous schema must be dropped and recreated; the current baseline is not an
in-place migration for that database. The rebuilt baseline is
`20261106_0001_baseline.py`. Until it is frozen for a release it may be regenerated; after
that, every schema change is a new revision that preserves existing rows (backfill a new
required column, for instance with a server default). Never edit a revision that shipped in
a release. Downgrades are not supported: `downgrade()` raises, and going back means
restoring a database backup.

Edit `models.py`, then with the dev database at head:

```sh
uv run alembic revision --autogenerate -m "what changed"
```

Review the generated file: autogenerate does not compare CHECK constraints, so add, replace
or drop them yourself, and make sure existing rows satisfy the new ones.
`tests/test_migrations.py::test_models_match_migrations` fails if models and migrations
drift. `tests/test_migration_upgrade.py` verifies that downgrade is refused; there is no
in-place migration path from the previous development schema.

### Protobuf

Generated modules are checked in under `src/lumewarp_control_plane/_proto/`. After changing
`../proto/`, run `../scripts/gen-proto.sh` (idempotent; the generator is pinned through
`grpcio-tools`).

### OpenAPI

FastAPI generates the document. `tests/api/test_openapi.py` requires a description on every
operation and request field, security on every non-public operation, and a typed 2xx
response; request-field descriptions come from `api/field_docs.py`. The Admin UI's types are
generated from it (`cd ../ui && pnpm gen:api`).

### Layout

| Module | Purpose |
| --- | --- |
| `settings.py`, `db.py`, `models.py`, `migrations/`, `migrate.py` | Configuration, database, schema |
| `metrics.py` | Prometheus registry, metrics listener, database-backed gauge refresher |
| `security.py`, `authz/`, `errors.py`, `audit.py`, `outbox.py` | Authentication, action matrix and authorization backends, error shape, audit, recompile outbox |
| `api/` | REST under `/api/v1` |
| `compiler/` | Snapshots, versions, diff, rollback, plugin descriptors |
| `grpc/`, `usage/`, `mcp/` | ConfigService and UsageService, budgets, catalog reports |
| `app.py`, `__main__.py`, `static.py` | App factory, lifespan, CLI, Admin UI static serving |
