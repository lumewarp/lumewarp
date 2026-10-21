# Lumewarp control plane

Management API, config compiler, gRPC config/usage services and plugin host. Never on the
gateway request path. Design: `../docs/architecture.md`; stream contract:
`../docs/config-protocol.md`.

## Dev setup

```sh
../scripts/dev-db.sh up                          # postgres:16 on 127.0.0.1:55432 (lumewarp/lumewarp)
uv sync
uv run python -m lumewarp_control_plane migrate  # alembic upgrade head
LUMEWARP_BOOTSTRAP_TOKEN=dev-admin \
  uv run python -m lumewarp_control_plane serve  # HTTP 127.0.0.1:8091, gRPC 127.0.0.1:50051
curl http://127.0.0.1:8091/readyz
```

`scripts/dev-db.sh down` stops the DB, `reset` wipes its volume.

## CLI

| Command | What it does |
| --- | --- |
| `serve` | HTTP API (uvicorn, one worker) + gRPC server + background loops |
| `migrate [REV]` / `migrate --downgrade REV` | apply / roll back migrations |
| `openapi [-o FILE]` | print the OpenAPI document; needs no database |
| `bootstrap --gateway-id ID` | create the config scope row and print a new `lwg-` gateway token (shown once) |

## Settings (`LUMEWARP_*` env)

| Variable | Default |
| --- | --- |
| `LUMEWARP_DATABASE_URL` | `postgresql+asyncpg://lumewarp:lumewarp@127.0.0.1:55432/lumewarp` |
| `LUMEWARP_HTTP_BIND` / `LUMEWARP_GRPC_BIND` | `127.0.0.1:8091` / `127.0.0.1:50051` |
| `LUMEWARP_GRPC_TLS_CERT_FILE` / `..._KEY_FILE` | unset = plaintext gRPC (keep it behind a trusted boundary) |
| `LUMEWARP_BOOTSTRAP_TOKEN` | unset; when set, a Bearer token with installation superuser rights |
| `LUMEWARP_SCOPE` | `default` (the `Snapshot.scope` this installation publishes) |

Limits (`MAX_SNAPSHOT_BYTES`, `MAX_SNAPSHOT_KEYS`, `GRPC_MAX_MESSAGE_BYTES`, `ACK_TIMEOUT_SECONDS`, ...)
are in `src/lumewarp_control_plane/settings.py`.

## Tests

```sh
uv run ruff check . && uv run ruff format --check . && uv run pytest
```

Tests need Postgres: each run creates `lumewarp_test_<random>` on the server from
`LUMEWARP_TEST_DATABASE_URL` (default: the dev DB above), migrates it, and drops it at the end.

## Schema changes

Edit `models.py`, then with the dev DB at head:

```sh
uv run alembic revision --autogenerate -m "what changed"
```

Review the generated file (autogenerate duplicates enum CHECK constraints; keep the ones on
the `sa.Enum` columns). `tests/test_migrations.py` fails if models and migrations drift.

## Protobuf

Generated modules are checked in under `src/lumewarp_control_plane/_proto/`. After changing
`../proto/`, run `../scripts/gen-proto.sh` (idempotent; generator pinned via `grpcio-tools`).

## Layout

| Module | Owner (P1) | Purpose |
| --- | --- | --- |
| `settings.py`, `db.py`, `models.py`, `migrations/`, `migrate.py` | C1 | config, DB, schema |
| `security.py`, `rbac.py`, `errors.py`, `audit.py`, `outbox.py` | C1 | auth, RBAC matrix, error shape, audit, recompile outbox |
| `api/` | C2 | REST under `/api/v1` |
| `compiler/`, `plugins.py` | C3 | snapshots, versions, diff, rollback, plugins |
| `grpc/`, `usage/`, `notifier/` | C4 | ConfigService/UsageService, budgets, notifications |
| `app.py`, `__main__.py` | integration | app factory, lifespan, CLI |
