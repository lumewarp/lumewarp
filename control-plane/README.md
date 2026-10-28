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
uv sync                                          # runtime + dev dependencies
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
| `LUMEWARP_AUTHZ_BACKEND` | `builtin` | Authorization backend: `builtin` (users and `team_members`) or a `lumewarp.authz` entry-point name. See [Authorization](#authorization). |
| `LUMEWARP_SCOPE` | `default` | `Snapshot.scope` this installation publishes. Gateways must use the same value. |
| `LUMEWARP_MAX_SNAPSHOT_BYTES` | `67108864` (64 MiB) | Compilation fails above this serialized size. |
| `LUMEWARP_MAX_SNAPSHOT_KEYS` | `200000` | Compilation fails above this key count. |
| `LUMEWARP_GRPC_MAX_MESSAGE_BYTES` | `67108864` (64 MiB) | gRPC send/receive limit, additionally capped by grpcio's hard limit. |
| `LUMEWARP_ACK_TIMEOUT_SECONDS` | `30` | Close a config session that has not ACKed/NACKed within this time. |
| `LUMEWARP_CONFIG_VERSIONS_RETAINED` | `50` | Versions whose payloads compaction keeps (the latest is always kept; session-pinned bases are kept too). |
| `LUMEWARP_MAX_USAGE_BATCH_EVENTS` | `10000` | Maximum events per `UsageService.Report` (1–10000; keeps asyncpg's parameter count in range). |
| `LUMEWARP_BACKGROUND_ENABLED` | `true` | Start gRPC and the loops in `serve`. Tests set `false`; keep it true in production. |

## Credentials and access model

There are four credential types, all stored only as SHA-256 hashes except the bootstrap
token, which is never stored:

| Credential | Prefix | Used by |
| --- | --- | --- |
| Bootstrap token | none | Installation superuser over the admin API/UI; comes from `LUMEWARP_BOOTSTRAP_TOKEN`. |
| Personal access token (PAT) | `lwp-` | A user over the admin API/UI; created under Settings in the UI or `POST /api/v1/users/me/tokens`. |
| Gateway token | `lwg-` | A gateway for the config stream and usage reporting; the first one comes from `bootstrap` (which also creates the scope), later ones from the gateways API/UI. |
| Virtual API key | `lw-` | Clients calling the gateway; owned by a team member or a service account of a team; the gateway only receives its hash in the snapshot. |

The bootstrap token is not tied to a user: it is an installation superuser, has no PATs of
its own, and is meant to create the first users and teams. Installation superusers
(bootstrap or a superuser PAT) can issue a PAT for an active user with
`POST /api/v1/users/{user_id}/tokens` using `{"name":"initial access"}` (optional
`expires_at`). The secret is returned only on creation; issuance is audited with the
actor and target user, never the secret. Team admins cannot use this endpoint.

### Tenancy model

There is exactly one installation and no org or project level:

- **Team**: a name plus policy (`model_access` and `allowed_models`, `rpm_limit`,
  `tpm_limit`, `max_parallel`, `budget_nano_usd`, `soft_budget_nano_usd`).
- **Team member**: a user in a team with role `admin` or `member` and the member's own
  narrowing policy. For regular users, membership determines team visibility
  (`team_members` for the built-in backend); a superuser sees every team.
- **Service account**: a non-human key owner in one team, with its own narrowing policy,
  managed by the team's admins. It is unaffected by people joining or leaving.
- **API key**: belongs to one team and is owned by a member (`owner_kind=user`) or a service
  account (`owner_kind=service_account`). Keys carry no policy; the compiler narrows the
  team policy by the owner policy into each key's effective policy. The snapshot carries
  only the key id and hash, team id, status, expiry and effective policy; owner identity
  stays in the control plane.

Removing a member suspends that member's user-owned keys in the team
(`suspended_reason=owner_removed`); deactivating a user suspends their user-owned keys
(`owner_deactivated`). Revoked keys are kept for usage history. A key or a service
account prevents team deletion.

An active owner who regains team membership may resume their own `owner_removed`
key. An `admin` suspension still requires team-admin permission to resume.

Usage is attributed to the key's team and owner: the event's `team_id` must match the
key, and the owner (member or service account) is read from the key. A member's
usage/rate-limit scope id is `member_scope_id = uuid5(team_id, user_id)`; it is derived, so
removing and re-adding a member keeps the same total and bucket. A service account uses
its own id. Rate-limit scopes are `team:<team_id>`, `member:<member_scope_id>` and
`sa:<service_account_id>`.

### Authorization

OpenFGA Check and BatchCheck always request `HIGHER_CONSISTENCY`, including
checks made by the key reconciler, so suspension decisions do not use cached
permission results.

With an external authorization backend, `GET /api/v1/teams/{id}/members` still
lists local `team_members` rows, not the external directory. An externally
managed team with no local rows returns `{"items": [], "next_cursor": null}`;
membership writes remain disabled. Session discovery and `/users/me` resolve
superuser status through the configured backend. On backend ERROR, session
discovery falls back to the local flag for display only; `/users/me` and
privileged API operations continue to fail closed with HTTP 503.

Authorization has two layers. The action matrix below stays in Lumewarp; a backend only
answers relation questions. A superuser passes every check, and an admin has every member
permission. `admin` implies `member`, and an installation `superuser` implies `admin` of
every team.

| Action | Who |
| --- | --- |
| `TEAM_CREATE`, `TEAM_DELETE`, `USER_MANAGE`, `ROUTING_MANAGE`, `GATEWAY_TOKEN_MANAGE`, `GATEWAY_READ`, `CONFIG_READ`, `CONFIG_ROLLBACK`, `NOTIFIER_MANAGE` | Installation superuser |
| `ROUTING_READ`, `OWN_TOKENS` | Any authenticated caller |
| `TEAM_UPDATE`, `TEAM_MEMBERS_MANAGE`, `SERVICE_ACCOUNT_MANAGE`, `KEY_MANAGE_ANY` | Team admin |
| `TEAM_READ`, `KEY_CREATE_OWN`, `KEY_MANAGE_OWN` | Team member |

`LUMEWARP_AUTHZ_BACKEND` selects the backend: `builtin` (the default) reads
`users.is_superuser` and `team_members`; any other value names a `lumewarp.authz` entry
point. The entry point may be an async `check` function, an object with an async `check`
method, or a class/zero-argument factory returning one; it is loaded once at startup. An
unknown name with no matching entry point aborts startup.

The only required method is:

```python
from lumewarp_control_plane.authz import Decision, Relation, Resource


async def check(user_id: str, relation: Relation, resource: Resource) -> Decision: ...
```

`Relation` is `superuser` (installation only), `admin` or `member` (team). `Resource` is
`installation:default` or a team. Return `Decision.ALLOW`, `Decision.DENY` or
`Decision.ERROR`. `ERROR` means the backend is unavailable or timed out: the admin API
answers `503 authz_unavailable` and background jobs skip the round; it is never treated
as a deny. The gateway never calls the backend; it only sees compiled snapshots.

Optional members are filled in from `check` when absent:

- `async batch_check(items)` returns one `Decision` per `(user_id, relation, resource)` in
  the same order; the default runs `check` with bounded concurrency.
- `async list_teams(user_id, relation)` returns the team ids where the user holds the
  relation; Lumewarp intersects the result with the candidate teams. Without it, the
  candidates are filtered with `batch_check`. A raised exception or an `ERROR` decision
  makes the admin API answer 503.
- `manages_grants: bool` defaults to `False`: memberships live in the external system and
  the member write endpoints answer `409 managed_externally`. The built-in backend sets it
  to `True`.

```toml
# pyproject.toml
[project.entry-points."lumewarp.authz"]
my-authz = "my_plugin.authz:check"
```

The backend conformance suite is importable from
`lumewarp_control_plane.authz.contract_tests`
(`src/lumewarp_control_plane/authz/contract_tests.py`). Run it against a backend before
setting `LUMEWARP_AUTHZ_BACKEND`; it covers the implied relations, inactive and unknown
users, batch ordering, `list_teams` consistency and `ERROR` handling.

Two external backends ship with the control plane:

- `openfga`: `LUMEWARP_OPENFGA_URL`, `LUMEWARP_OPENFGA_STORE_ID`,
  `LUMEWARP_OPENFGA_MODEL_ID`, optional `LUMEWARP_OPENFGA_API_TOKEN`. Users are
  `user:<lumewarp user uuid>`, resources `installation:default` and `team:<uuid>`; the
  model is `deploy/openfga/model.fga`. Implements `batch_check`, `list_teams`
  (ListObjects) and `changes` (ReadChanges). `scripts/dev-idp.sh up` starts a local
  OpenFGA, writes the model and prints these variables.
- `http`: `LUMEWARP_AUTHZ_HTTP_URL` (+ optional `LUMEWARP_AUTHZ_HTTP_TOKEN`).
  `check` becomes `POST {"user_id", "relation", "resource": {"type", "id"}}` answered by
  `{"decision": "allow" | "deny"}`; anything else (non-2xx, timeout, bad body) is `ERROR`.

Every backend call is bounded by `LUMEWARP_AUTHZ_TIMEOUT_SECONDS` (default 2 s); a
timeout is `ERROR`.

### Key reconciler

A user-owned key stays active only while its owner is a `member` of the key's team. The
`key_reconciler` background loop enforces this for external backends (the built-in
backend already suspends keys in the same transaction that removes the member):

- With `changes()` (OpenFGA) it polls every `LUMEWARP_KEY_RECONCILE_POLL_SECONDS`
  (default 2 s) and rechecks only the users that changed, so a removed member's keys are
  rejected by the gateway within about 5 s.
- Every `LUMEWARP_KEY_RECONCILE_FULL_SECONDS` (default 300 s), and whenever the change
  cursor is reset, it rechecks every active user-owned key (in slices).
- An explicit `DENY` suspends that owner's keys in that team with
  `suspended_reason=owner_removed`, audited with the system actor `key_reconciler`, and
  publishes a new snapshot. `ERROR` or a timeout never suspends anything.
- Restoring membership does not resume keys automatically; the owner or a team admin
  resumes them. Service-account keys are never touched: rotate them when people leave.
- `LUMEWARP_KEY_RECONCILE_ENABLED=false` turns the loop off.

## SSO login (OIDC)

The admin UI can sign in through any OIDC provider (Keycloak in dev). The control plane
runs the authorization code flow with PKCE itself (Authlib) and issues a server-side
session cookie; the IdP only authenticates, it never grants permissions (no groups or
role claims are read).

| Setting | Meaning |
| --- | --- |
| `LUMEWARP_OIDC_ISSUER` | Issuer URL; unset disables SSO. |
| `LUMEWARP_OIDC_CLIENT_ID`, `LUMEWARP_OIDC_CLIENT_SECRET` | Client credentials (secret optional for a public PKCE client). |
| `LUMEWARP_OIDC_SCOPES` | Default `openid email profile`. |
| `LUMEWARP_PUBLIC_URL` | External base URL; the redirect URI is `<public_url>/api/v1/session/oidc/callback`. |
| `LUMEWARP_OIDC_JIT_USERS` | Create a user with no grants on first login (default true). |
| `LUMEWARP_SESSION_TTL_SECONDS` | Absolute session lifetime (default 12 h). |
| `LUMEWARP_SESSION_COOKIE_SECURE` | `Secure` cookie flag (default true; turn off only for plain-http dev). |

- Users are identified by the token's `iss` + `sub`. On a first login with a verified
  email, the identity links to an existing non-superuser account with that email that
  has no identity from this issuer yet; otherwise a new user is created (JIT, only with a
  verified email). Superusers cannot be linked by email; they keep using the bootstrap
  token or a PAT.
- Inactive users cannot log in, and their existing sessions stop working immediately.
- The cookie `lumewarp_session` is httpOnly and `SameSite=Lax`. Cookie-authenticated
  writes need the `X-Lumewarp-CSRF` header with the `csrf_token` from
  `GET /api/v1/session`. Bearer callers (bootstrap token, PATs) never need it; if both a
  bearer and a cookie are sent, the bearer wins.
- `POST /api/v1/session/logout` ends the Lumewarp session (not the IdP session).
- IdP and authorization backend are kept in sync by the deployer: disabling a person in
  the IdP stops new logins, while revoking their team grants (OpenFGA) or removing them
  (built-in) is what suspends their keys.

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
| `lumewarp.auth` | Map an external identity (OIDC, LDAP, ...) to a user's email and display name. | Called on a first SSO login; the first plugin (by entry-point name) wins. |

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

## Routing

Upstreams, deployments and routes are installation-wide (`/api/v1/upstreams`,
`/deployments`, `/routes`): any authenticated principal may read them, writes need a
superuser. Every write is audited and enqueues a recompile. P2 adds resilience settings
that compile 1:1 into the snapshot (`proto/lumewarp/config/v1/snapshot.proto`):

| API field | Where | Snapshot / default |
| --- | --- | --- |
| `health_check {path, interval_ms, timeout_ms, unhealthy_threshold, healthy_threshold}` | upstream | `Upstream.health_check`; `null` = no active probing; null path = `/models`, null thresholds = 3 / 1 |
| `circuit_breaker {failure_threshold, open_ms}` | upstream | `Upstream.circuit_breaker`; `null` = never opens; `{}` = gateway defaults (5 failures, 30000 ms) |
| `metrics_url` | upstream | `Upstream.metrics_url` for `vllm_load`; `null` = base URL minus `/v1` plus `/metrics` |
| `strategy` | route | `weighted`, `least_in_flight`, `lowest_latency` (EWMA), `vllm_load` |
| `retry {max_attempts, retry_on_status, total_timeout_ms}` | route | `ModelRoute.retry`; `null` = one attempt. `max_attempts: null` is emitted as 0, which the gateway reads as its default of 2 |
| `fallback_route_ids` (ordered) | route | `ModelRoute.fallbacks` (aliases). Needs `retry` with at least 2 attempts to take effect |
| `traffic_split [{name, weight, deployment_ids}]` | route | `ModelRoute.traffic_split`; with a split the route's own `deployment_ids` may be empty and are ignored |
| `sticky` (`none`/`header`/`team`/`key`), `sticky_header` | route | `ModelRoute.sticky` / `sticky_header` |

Validation mirrors the gateway's snapshot checks so an accepted write never makes a gateway
NACK (422 otherwise): health interval >= 1000 ms and 0 < timeout < interval, path absolute
without query/fragment; `metrics_url` plain http(s) without userinfo; `max_attempts` 1..5,
statuses 400..599 and unique; fallbacks exist, are unique, never the route itself and never
form a cycle across routes (route writes take the scope lock, so concurrent edits cannot
race into one); at most 16 splits, names 1..32 of `[A-Za-z0-9_.-]` and unique, each split
non-empty with existing, unique deployments, weights >= 0 summing to more than 0;
`sticky_header` (an HTTP header name, stored lower-case) required iff `sticky=header`, and
any sticky mode needs a split; a `weighted` route needs a positive total deployment weight
per split (or over its deployments without a split), also checked when a deployment's
weight changes. Deleting a route used as another route's fallback, or a deployment used by
a route or a split, is 409. PATCH replaces lists that are given and keeps omitted fields;
`retry: null` removes the policy, and leaving `sticky=header` drops the header.

The compiler lists a capability in `required_capabilities` exactly when a field needs it
(the gateway's rule): `health_check`, `circuit_breaker`, `vllm_load` (metrics URL or
strategy), `retry`, `fallbacks`, `traffic_split` (split or sticky), `lowest_latency`.
Defensively, a fallback to a route that is left out of the snapshot is dropped, and a route
whose split ends up empty or (weighted) without weight is left out, each with a warning.

Storage (migration `0003`): scalar settings are columns on `upstreams`/`routes` with CHECK
constraints; the ordered lists are child tables with foreign keys: `route_fallbacks`
(RESTRICT on the target route), `route_splits` and `route_split_deployments` (RESTRICT on
the deployment). Downgrading to `0002` drops these settings and maps `vllm_load` to
`least_in_flight`.

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
- `UsageService.Report` validates per event (an invalid event is skipped and logged, the
  rest of the batch is kept; `REJECTED` events may omit model/deployment) and is
  idempotent by `request_id`; each new event
  adds cost once to the team total and once to the key owner's (member or service account)
  total for the UTC calendar month in the same transaction, then budget evaluation flips
  exhausted scopes to `BUDGET_EXCEEDED` and publishes a new version (eventual; in-flight
  streams are not cut). Soft-limit
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
| `security.py`, `authz/`, `errors.py`, `audit.py`, `outbox.py` | auth, action matrix and authorization backends, error shape, audit, recompile outbox |
| `api/` | REST under `/api/v1` |
| `compiler/`, `plugins.py` | snapshots, versions, diff, rollback, plugin host |
| `grpc/`, `usage/`, `notifier/` | ConfigService/UsageService, budgets, notifications |
| `app.py`, `__main__.py`, `static.py` | app factory, lifespan, CLI, Admin UI static serving |
