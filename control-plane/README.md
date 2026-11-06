# Lumewarp control plane

Management API, config compiler, gateway-facing gRPC services (config stream and usage
ingest), the budget worker and the plugin host. It never runs on the gateway request
path: the gateway only reads the immutable snapshot it last ACKed. Design:
[`../docs/architecture.md`](../docs/architecture.md); stream contract:
[`../docs/config-protocol.md`](../docs/config-protocol.md).

One Python process serves everything: FastAPI (API + Admin UI), `grpc.aio`
(`ConfigService`, `UsageService`, gRPC health), the compiler loop, the background
schedulers and the Prometheus metrics listener all start and stop in the FastAPI lifespan. Running more than one process per
installation is not supported.

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

LUMEWARP_DEV_MODE=true LUMEWARP_BREAK_GLASS_PASSWORD=dev-break-glass \
  uv run --no-sync lumewarp-control-plane serve  # HTTP :8091, gRPC :50051
```

In another terminal:

```sh
curl -sS http://127.0.0.1:8091/readyz
curl -sS http://127.0.0.1:8091/healthz
curl -sS http://127.0.0.1:9465/metrics   # lumewarp_cp_* (see Metrics)
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

## Emergency login and trusted proxies

Use `lumewarp-control-plane hash-password` to generate an argon2id hash, then set
`LUMEWARP_BREAK_GLASS_PASSWORD_HASH`. Invalid or non-argon2id hashes prevent startup.
Plaintext `LUMEWARP_BREAK_GLASS_PASSWORD` is accepted only with `LUMEWARP_DEV_MODE=true`.
Emergency sessions expire after 3600 seconds by default; the configurable
`LUMEWARP_BREAK_GLASS_SESSION_TTL_SECONDS` is capped at 14400 seconds. Disabling or
changing the configured credential invalidates existing emergency sessions.

`LUMEWARP_TRUSTED_PROXIES` is a comma-separated list of proxy CIDRs, empty by default.
For example, `LUMEWARP_TRUSTED_PROXIES=10.20.0.0/24,2001:db8:1234::/64` trusts only
those socket peers to supply `X-Forwarded-For`. The control plane walks the chain
from right to left and stops at the first untrusted address. Configure only actual
proxy networks, and have the edge proxy overwrite or append the real client address.
Untrusted peers and malformed forwarding chains cannot select a forwarded identity.
The CLI disables Uvicorn's own proxy-header rewriting so this policy sees the socket
peer; alternate ASGI models must also disable upstream rewriting of `scope.client`.

`LUMEWARP_BREAK_GLASS_ALLOWED_CIDRS` optionally restricts the resolved client addresses
allowed to attempt emergency login, using the same comma-separated CIDR syntax.
Other addresses receive 404. This restriction bypasses neither password verification
nor per-client limiting. Keep proxy trust separate from allowed client networks.

The per-client limiter tracks at most 4096 addresses and allows five verification
attempts per 60-second window. A successful verification clears that client's attempts.
Global pressure adds at most one second of asynchronous delay, never a global deny:
one attacking identity cannot lock out another identity's correct password.
Every verified success/failure is audited; rate-limited attempts increment metrics and
produce at most one summary audit row per 60 seconds per CP process.

## Ports and endpoints

| Listener | Default | Serves |
| --- | --- | --- |
| HTTP | `127.0.0.1:8091` | Admin UI at `/`, REST at `/api/v1`, `/docs` and `/openapi.json`, `/healthz` and `/readyz` |
| gRPC | `127.0.0.1:50051` | `lumewarp.config.v1.ConfigService`, `lumewarp.usage.v1.UsageService`, `grpc.health.v1.Health` |
| Metrics | `127.0.0.1:9465` | Prometheus text format on any path (`/metrics`); see [Metrics](#metrics). `LUMEWARP_METRICS_BIND=""` disables it. |

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
| `LUMEWARP_METRICS_BIND` | `127.0.0.1:9465` | Prometheus metrics listener started by `serve` (`host:port`); empty string disables it. Not the admin API port, and not 9464 (the gateway admin listener's default). A bind failure aborts startup. |
| `LUMEWARP_GRPC_TLS_CERT_FILE` / `LUMEWARP_GRPC_TLS_KEY_FILE` | unset | PEM certificate chain and key. Both or neither; unset means plaintext gRPC. |
| `LUMEWARP_INITIAL_ADMIN_EMAIL` | unset | Email that becomes `proxy_admin` on its first verified OIDC login while no proxy_admin exists. Unset leaves break-glass as the only way to create the first one. |
| `LUMEWARP_BREAK_GLASS_PASSWORD_HASH` | unset | Argon2id hash of the emergency password (`lumewarp-control-plane hash-password`); an invalid hash aborts startup. See [Emergency access](#emergency-access-break-glass). |
| `LUMEWARP_BREAK_GLASS_PASSWORD` | unset | Plaintext emergency password; refused unless `LUMEWARP_DEV_MODE=true`. The hash wins if both are set. |
| `LUMEWARP_DEV_MODE` | `false` | Development only: allows the plaintext password above. |
| `LUMEWARP_BREAK_GLASS_SESSION_TTL_SECONDS` | `3600` | Emergency session lifetime, 60–14400. |
| `LUMEWARP_BREAK_GLASS_ALLOWED_CIDRS` | empty (any) | Client networks allowed to use the emergency login; others get 404. |
| `LUMEWARP_TRUSTED_PROXIES` | empty | Proxy networks whose `X-Forwarded-For` is followed to find the client address for the two limits above. |
| `LUMEWARP_AUTHZ_BACKEND` | `builtin` | Authorization backend for direct org/team relations: `builtin` (`org_admins` + `team_members`), `openfga`, `http` or a `lumewarp.authz` entry-point name. See [Authorization](#authorization). |
| `LUMEWARP_SCOPE` | `default` | `Snapshot.scope` this installation publishes. Gateways must use the same value. |
| `LUMEWARP_MAX_SNAPSHOT_BYTES` | `67108864` (64 MiB) | Compilation fails above this serialized size. |
| `LUMEWARP_MAX_SNAPSHOT_KEYS` | `200000` | Compilation fails above this key count. |
| `LUMEWARP_GRPC_MAX_MESSAGE_BYTES` | `67108864` (64 MiB) | gRPC send/receive limit, additionally capped by grpcio's hard limit. |
| `LUMEWARP_ACK_TIMEOUT_SECONDS` | `30` | Close a config session that has not ACKed/NACKed within this time. |
| `LUMEWARP_CONFIG_VERSIONS_RETAINED` | `50` | Versions whose payloads compaction keeps (the latest is always kept; session-pinned bases are kept too). |
| `LUMEWARP_MAX_USAGE_BATCH_EVENTS` | `10000` | Maximum events per `UsageService.Report` (1–10000; keeps asyncpg's parameter count in range). |
| `LUMEWARP_BACKGROUND_ENABLED` | `true` | Start gRPC and the loops in `serve`. Tests set `false`; keep it true in production. |

## Credentials and access model

Three token types, stored only as SHA-256 hashes, and two kinds of cookie session:

| Credential | Prefix | Used by |
| --- | --- | --- |
| Personal access token (PAT) | `lwp-` | A user over the admin API/UI; created under Settings in the UI or `POST /api/v1/users/me/tokens`. |
| Gateway token | `lwg-` | A gateway for the config stream and usage reporting; the first one comes from `bootstrap` (which also creates the scope), later ones from the gateways API/UI. |
| Virtual API key | `lw-` | Clients calling the gateway; owned by a team member or a service account of a team; the gateway only receives its hash in the snapshot. |
| OIDC session | cookie | A user logged in through [SSO](#sso-login-oidc). |
| Emergency session | cookie | The [break-glass](#emergency-access-break-glass) login: an installation admin that is not a user. |

The bearer scheme accepts PATs only. Nothing else authenticates to the admin API. No
credential is stored in plaintext: the OIDC nonce and PKCE verifier live only in the
encrypted login cookie, and an upstream stores only its `api_key_ref` name.

Installation admins live in Postgres (`installation_admins`). The first verified login whose
email matches `LUMEWARP_INITIAL_ADMIN_EMAIL` becomes the `proxy_admin`; break-glass can create
the first one when the setting is empty, and is the recovery path afterwards. Any installation
admin can then assign more admins. A user must have signed in through OIDC at least once before
receiving an installation role: otherwise the identity-link protection would leave a privileged
account unable to log in. Assign and transfer return 409 for an account with no linked identity;
let the person sign in once first. An installation admin can issue a PAT for an active user with
`POST /api/v1/users/{user_id}/tokens` using `{"name":"initial access"}` (optional
`expires_at`). The secret is returned only on creation; issuance is audited with the
actor and target user, never the secret. Team admins cannot use this endpoint.

### Emergency access (break-glass)

For when the IdP or the IAM is down, or before either exists. `POST /api/v1/session/break-glass`
with `{"password": ...}` (the UI's emergency login) sets the same `lumewarp_session` cookie as
OIDC, and writes need the same CSRF header:

- The session is an installation admin with no user behind it: it passes every check without
  asking the authorization backend, so it works while the backend is down. It has no PATs and
  belongs to no team.
- Break-glass can create the first proxy_admin or assign admins, but the target must have signed
  in through OIDC once first (409 otherwise); a newly created account has no linked identity for
  the role.
- It ends at its TTL, on logout, and at once when the configured credential changes.
- Failed attempts are delayed and limited per client address (429 `rate_limited`). The endpoint
  answers 404 when no credential is configured or the address is outside
  `LUMEWARP_BREAK_GLASS_ALLOWED_CIDRS`.
- Attempts are audited as `session.break_glass.login` with actor type `break_glass` (a burst of
  rate-limited ones as one summary row), and counted in `lumewarp_cp_break_glass_logins_total`.

Production uses the hash: `lumewarp-control-plane hash-password` reads the password from stdin
or a prompt and prints it for `LUMEWARP_BREAK_GLASS_PASSWORD_HASH`.

### Tenancy model

There is exactly one installation. Below it: org -> team -> key owners -> keys.

- **Org**: the root of the quota hierarchy: `model_access` and `allowed_models`,
  `rpm_limit`, `tpm_limit`, `max_parallel` and `budget_nano_usd`. Only an installation
  admin may create one (`POST /orgs`), rename or set its policy (`PATCH /orgs/{id}`),
  archive it (`DELETE /orgs/{id}`) or assign/remove its admins
  (`PUT|DELETE /orgs/{id}/admins/{user_id}`); an org admin reads its own orgs
  (`GET /orgs`, `GET /orgs/{id}`) and their per-team spend (`GET /orgs/{id}/usage`, summed
  from `usage_events` at query time). Admin assignment needs the built-in backend
  (`manages_grants`; an external backend answers 409) and a target that has signed in
  once. An **org admin** holds `admin` on `org:<id>`; `RequestAuthz` inherits that for
  every team of the org. `POST /teams` takes an explicit `org_id`: there is no default
  org. Archiving an org is irreversible and only allowed once it has no live team; after
  that no team can be created in it.
- **Team**: a name, its `org_id`, a policy (`model_access` and `allowed_models`,
  `rpm_limit`, `tpm_limit`, `max_parallel`, `budget_nano_usd`), the member defaults
  `member_rpm_default`, `member_tpm_default` and `member_max_parallel_default`, and the
  self-service limits `max_keys_per_member` (non-revoked user keys per member; revoking
  frees a slot) and `max_member_key_lifetime_seconds`. `DELETE /teams/{id}` archives:
  in one transaction it locks the config scope, sets `deleted_at`, revokes every key of
  the team (including the rotation-grace hash), deactivates its service accounts and
  frees the name, which is unique only among live teams. Archived teams stay in the
  database for usage and audit (`GET /usage?team_id=`, `GET /audit?team_id=`, the org
  usage summary) but every write path and every team-scoped read answers 404.
- **Team member**: a user in a team with role `admin` or `member`. The member's own
  exception policy and grants live in `member_policies`, keyed by `(team_id, user_id)` and
  deliberately independent of `team_members`: an external authorization backend has no
  membership rows, yet per-member quotas still apply. Direct membership determines team
  visibility (`team_members` for the built-in backend) and is the only way to own a key;
  team administration is also inherited from an org admin; an installation admin sees
  every team.
- **Service account**: a non-human key owner in one team, with its own narrowing policy,
  managed by the team's admins. It takes no member defaults and is unaffected by people
  joining or leaving. `PATCH /service-accounts/{id}` with `{"is_active": false}`
  deactivates it: the compiler leaves its keys out of the snapshot (and includes the
  non-revoked ones again after reactivation). Archiving a team deactivates all of its
  accounts.
- **API key**: belongs to one team and is owned by a member (`owner_kind=user`) or a service
  account (`owner_kind=service_account`). A key carries only its own `rpm_limit`,
  `tpm_limit` and `max_parallel` (NULL = no key level). The compiler resolves the whole
  chain into each key's effective policy. The snapshot carries only the key id and hash,
  team id, status, expiry and effective policy; owner identity stays in the control plane.

**Quota hierarchy.** One resolver (`quota.py`) computes org -> team -> owner for the
compiler, `/users/me` and every write validation. Models are the intersection down the
chain (`INHERIT`/`ALL` keep the parent, `LIST` intersects). Limits are the minimum down the
chain; a member's effective limits are `exception ?? team member_default`, then clamped by
the team, and a service account never takes the member defaults. Remaining budget is
`min(org, team, owner)`, each from its own accumulated spend: the org has no
`usage_totals` row, its spend is the sum of every one of its teams (archived included).

Writes only check values they actually change: a changed value may not exceed the parent's
current effective value (422), while an unchanged old value is not rechecked, so a parent
decrease always succeeds and the effective value clamps. Team totals and models are
`TEAM_QUOTA_SET` (org level and above); member defaults, member exceptions, service
accounts and any key's limits are team-admin-and-above. A key's owner may change their own
key's limits within their effective limits; a service-account key's limits only by team
admin and above. Every key has at most four rate-limit scopes (org, team, owner, key); the
control plane (`compiler/validate.py`) and the gateway (`snapshot.rs`) both reject more.
A key without limits emits no key scope and costs nothing on the request path.

A member key can be created with an optional `expires_at`. When the team sets
`max_member_key_lifetime_seconds`, a member key created without one gets an expiry at that
cap, and a supplied expiry beyond it is a 422. Rotation and `PATCH /keys/{id}` enforce the
same cap (a never-expiring key gets a deadline at rotation; an existing later expiry must
be shortened first). Team admins and installation admins are subject to these checks;
service-account keys are not member keys and are exempt. Lowering the caps never revokes
existing keys: lowering the lifetime cap rejects later creates, rotations or expiry
extensions that would exceed it, while lowering the count cap only rejects later creates,
because rotation does not add a key.

`POST /keys/{id}/rotate` accepts an optional `{"grace_seconds": N}` (0 by default, at most
86400). With a grace, the replaced hash stays valid until its own expiry (bounded by the
key's `expires_at`) and authenticates as the same `key_id`, so usage stays on one key. The
gateway stores it next to the current hash and enforces that expiry from its persisted
snapshot even while the control plane is down. The control plane drops an expired previous
hash on its next compile, which sends the delta that removes it. Only one previous hash is
kept: rotating again inside the grace ends the older grace immediately. Revoking clears
both hashes. The response exposes `previous_key_expires_at` but never the old secret.

`GET /users/me` returns, for every team the caller belongs to, the effective model names
(the whole chain, org included; `all_models` when that intersection is universal), the
effective rate limits (org, team and member defaults included), the team's member-key limits
with the caller's current non-revoked key count (`key_limits`), and the current UTC-month
org/team/member budgets with the remaining amount computed server-side. The org and team
budget numbers themselves are only shown to a team admin or installation admin; a plain
member sees the computed remainder. It never returns the team's full allowlist to a member
whose own policy is narrower.

`POST /teams/{id}/keys` (create), `POST /keys/{id}/rotate` and `PATCH /keys/{id}` are rate
limited per principal and operation: 30 attempts per minute each, independent of the target
team or key. The check runs after authentication, before target lookup and authorization.
Installation admins and break-glass sessions are exempt; a member who is only a team admin
is not. A refusal is a 429 `rate_limited` with `Retry-After`, counted in
`lumewarp_cp_key_writes_total{result="rate_limited"}` and deliberately not audited: only
successful writes are audited, so a client cannot flood the audit table. A limits-only
`PATCH` never touches the key's lifecycle or suspension reason.

Removing a member suspends that member's user-owned keys in the team
(`suspended_reason=owner_removed`); deactivating a user suspends their user-owned keys
(`owner_deactivated`). Revoked keys are kept for usage history; a key or a service
account no longer prevents archiving a team.

An active owner may resume a key they suspended themselves (`owner`) and, once they
are a member again, one suspended by their removal (`owner_removed`). An `admin`
suspension needs a team admin; `owner_deactivated` needs an administrator after the
account is reactivated. Nothing can be resumed while the key's owner is inactive.

Usage is attributed to the key's team and owner: the event's `team_id` must match the
key, and the owner (member or service account) is read from the key. A member's
usage/rate-limit scope id is `member_scope_id = uuid5(team_id, user_id)`; it is derived, so
removing and re-adding a member keeps the same total and bucket. A service account uses
its own id. Rate-limit scopes are `org:<org_id>`, `team:<team_id>`,
`member:<member_scope_id>`, `sa:<service_account_id>` and `key:<key_id>`; a level that sets
no limit emits no scope. Rate limits are counted per gateway process: N gateway instances
allow up to N times the configured limit, by design.

Org budgets are derived: the org's monthly spend is the sum of its teams' `usage_totals`
(archived included), and only orgs with a budget are checked after an ingest. The first
crossing inserts `org_budget_windows` with `ON CONFLICT DO NOTHING`, so the audit row and
the recompile happen exactly once even when several teams of one org report concurrently;
a batch that cannot see a sibling's uncommitted total is caught by the next ingest or
compile. Raising a budget (or removing it) deletes the month's window row, so a later
crossing audits and recompiles again instead of staying silent.

### Authorization

OpenFGA Check and BatchCheck always request `HIGHER_CONSISTENCY`, including
checks made by the key reconciler, so suspension decisions do not use cached
permission results.

With an external authorization backend, `GET /api/v1/teams/{id}/members` still
lists local `team_members` rows, not the external directory. An externally
managed team with no local rows returns `{"items": [], "next_cursor": null}`;
membership writes remain disabled. Session discovery and `/users/me` resolve
`is_admin` through the configured backend. On backend ERROR both report `is_admin: false` with
`authz_unavailable: true` for display, and privileged API operations fail closed with HTTP 503.

Authorization has two layers. The action matrix below stays in Lumewarp; a backend only
answers direct relation questions. Inheritance is applied by `RequestAuthz`, never by a
backend, and a team's org always comes from Postgres (never from a request payload):

```
is_installation_admin        = break-glass or a row in installation_admins
is_org_admin(org)            = is_installation_admin or admin of org:<id>
team_admin_capability(team)  = is_org_admin(team.org_id) or admin of team:<id>
team_read_capability(team)   = team_admin_capability(team) or member of team:<id>
```

An *installation admin* is a break-glass session, or a user with a row in `installation_admins`
(`proxy_admin` or `admin`). The role is read from Postgres once per request, so a revocation
applies on the caller's next request. It is never asked of an authorization backend, and a PAT
carries its holder's installation role. `proxy_admin` is unique: the first verified login
matching `LUMEWARP_INITIAL_ADMIN_EMAIL` creates it while no proxy_admin exists (or break-glass
assigns one), and it can be transferred afterwards. Every installation admin can assign and remove
`admin` rows; nobody can remove or demote the `proxy_admin` through those endpoints. The
`proxy_admin` account itself is protected: only that account and break-glass may deactivate,
delete, change its email or mint a PAT for it.

The endpoints are `GET /api/v1/installation-admins` (the rows with user email, display name and
active state), `PUT /api/v1/installation-admins/{user_id}` with `{"role":"admin"}` (idempotent;
`proxy_admin` is rejected as a normal assignment), `DELETE /api/v1/installation-admins/{user_id}`
(removes an `admin`; the proxy_admin must be transferred) and
`POST /api/v1/installation-admins/transfer` with `{"user_id": ...}` (the previous proxy_admin
becomes an `admin`). Assign and transfer require the target to have linked an IdP identity
(signed in once); an account that has never signed in gets 409 `the user must sign in once
before receiving an installation role`. Assign, remove and transfer are audited; the first-login
bootstrap is audited as `installation_admin.bootstrap`.

An *org admin* holds `admin` on one org (`org_admins` rows for the built-in backend). Its teams
are administered by inheritance: it reads every team of the org (with the whole-team policy
view), reads their members, service accounts, keys, usage and audit, changes their settings,
sets their quota and assigns their team admins. It never reaches another org, and it does not
become a team *member*: a direct membership is still required to own a key (and the key
reconciler only looks at direct memberships).

| Action | Installation admin | Org admin | Team admin | Member |
| --- | --- | --- | --- | --- |
| `ORG_MANAGE`, `INSTALLATION_ADMIN_MANAGE`, `USER_MANAGE`, `ROUTING_MANAGE`, `ROUTING_READ`, `GATEWAY_TOKEN_MANAGE`, `GATEWAY_READ`, `CONFIG_READ`, `CONFIG_ROLLBACK` | yes | | | |
| `TEAM_CREATE`, `TEAM_DELETE`, `TEAM_QUOTA_SET`, `TEAM_ADMIN_ASSIGN` | yes | its org's teams | | |
| `TEAM_SETTINGS_UPDATE`, `TEAM_MEMBERS_MANAGE`, `TEAM_ADMIN_READ`, `SERVICE_ACCOUNT_MANAGE`, `KEY_MANAGE_ANY` | yes | inherited | yes | |
| `TEAM_READ`, `KEY_CREATE_OWN`, `KEY_MANAGE_OWN` | yes | inherited | yes | yes |
| `CATALOG_READ`, `OWN_TOKENS` | yes | yes | yes | yes |

`TEAM_QUOTA_SET` covers a team's model access, allowlist, rate limits, parallelism and budget;
`TEAM_ADMIN_ASSIGN` covers adding an admin, promoting or demoting an existing member (including
the caller themself) and removing an admin. `TEAM_CREATE` takes the `org_id` from the body and
`TEAM_DELETE` archives the team named in the URL; both are org-level, checked on the org of the
target, so an org admin may use them in its own org and a team admin cannot use them at all. A
team admin keeps `TEAM_SETTINGS_UPDATE` (the team name and the member key limits). A team update
decides from what actually changes (`proposed != base`), so resending the current policy values
is not a quota change. `ORG_MANAGE` is installation-level: create, rename, policy, archive and
org admin assignment.

`KEY_CREATE_OWN` passes for an org admin (it has the team's read capability) but creating a
user-owned key also requires a direct membership, so an org admin that is not a member of the
team gets `422 not a member of this team`. The same rule keeps `/users/me` honest: `teams` lists
direct memberships only, and `admin_orgs` (id and name) lists the orgs the caller administers;
an installation admin administers every org and gets an empty `admin_orgs`, and break-glass has
no user.

`TEAM_READ` is the team's basic record (`id`, `org_id`, `name`, timestamps and the member key
limits); a member's effective policy comes from `/users/me`, never from the team. `TeamOut` (with
the team policy) is only returned to a team admin or above; every other member gets
`TeamBasicOut`. `TEAM_ADMIN_READ` is the whole-team view: the member and service-account lists,
every key, the team's usage and its audit. A plain member gets `403` on those lists (a non-member
still gets `404`, and another org's team is a non-member), and `GET /keys/{id}` is admin-or-owner
only. `GET /teams/{id}/keys` for a plain member is forced to their own user-owned keys, ANDed
with whatever filters they sent, so asking for someone else returns nothing. Usage has the same
server-side owner predicate: a plain member sees rows of their own keys in teams they belong to,
including grouped totals, and cannot widen it with `key_id`/`user_id`/`service_account_id`.
Team-admin views and audit cover direct team admins and every inherited org team; a caller who
administers no team gets `403`, filtered or not. Audit also matches `org_id`, so an org admin
reads its org's org-level events (org policy, org admins, org budget) and the rows of its
archived teams; a team admin reads only rows attached to its teams.

`ROUTING_READ` is installation-level (upstreams, models, aliases, routers and strategies,
including their `GET`s). `CATALOG_READ` is the one routing read every authenticated caller
needs: `GET /catalog` returns only names, ids and kinds, which is what the allowlist pickers use.

`LUMEWARP_AUTHZ_BACKEND` selects the backend: `builtin` (the default), `openfga`, `http`, or
any other value naming a `lumewarp.authz` entry point. Every backend, the three built-ins
included, loads through that one entry point (they are registered in this project's
`pyproject.toml`), so there is no special case in `load_authorizer`. Every backend answers
direct relations only; installation roles are Lumewarp's own Postgres data and are never sent
to a backend. `builtin` answers `admin` on orgs from `org_admins` and `admin`/`member` on teams
from `team_members`.

A factory entry point may take one argument, `AuthzContext(settings, session_factory, metrics)`,
or none. It may also name an async `check` function or an object with an async `check` method
directly. An unknown name with no matching entry point aborts startup.

The only required method is:

```python
from lumewarp_control_plane.authz import Decision, Relation, Resource


async def check(user_id: str, relation: Relation, resource: Resource) -> Decision: ...
```

`Relation` is `admin` (on an org or a team) or `member` (on a team). `Resource.type` is `"org"`
or `"team"` and `id` is the UUID. `member` **must mean a direct team membership**: an org admin
does not inherit it, or `_is_member`, `/users/me` and the key reconciler would be wrong. A team
`admin` must imply `member` (team admins hold keys). Return `Decision.ALLOW`, `Decision.DENY` or
`Decision.ERROR`. `ERROR` means the backend is unavailable or timed out: the admin API answers
`503 authz_unavailable` and background jobs skip the round; it is never treated as a deny, and an
error while resolving an inherited capability is a 503 too, never an empty list. The gateway
never calls the backend; it only sees compiled snapshots.

Optional members are filled in from `check` when absent:

- `async batch_check(items)` returns one `Decision` per `(user_id, relation, resource)` in
  the same order; the default runs `check` with bounded concurrency.
- `async list_objects(user_id, relation, resource_type)` returns the object ids where the user
  holds the direct relation (`"org"` or `"team"`); Lumewarp intersects the result with the
  candidates. The older team-only `async list_teams(user_id, relation)` is still accepted.
  Without either, candidates are filtered with `batch_check`. A raised exception or an `ERROR`
  decision makes the admin API answer 503.
- `manages_grants: bool` defaults to `False`: memberships live in the external system and the
  member write endpoints answer `409 managed_externally`. The built-in backend sets it to `True`.

```toml
# pyproject.toml
[project.entry-points."lumewarp.authz"]
my-authz = "my_plugin.authz:check"
```

The backend conformance suite is importable from
`lumewarp_control_plane.authz.contract_tests`
(`src/lumewarp_control_plane/authz/contract_tests.py`). Run it against a backend before
setting `LUMEWARP_AUTHZ_BACKEND`; it covers the implied relations, org admin as a direct
relation only, inactive and unknown users, batch ordering, `list_objects` consistency and
`ERROR` handling. `tests/authz` runs it for the built-in backend, the HTTP backend and a live
OpenFGA when `LUMEWARP_TEST_OPENFGA_URL` is set (the dev instance from `scripts/dev-idp.sh up`
is `http://127.0.0.1:18081`).

Two external backends ship with the control plane:

- `openfga`: `LUMEWARP_OPENFGA_URL`, `LUMEWARP_OPENFGA_STORE_ID`,
  `LUMEWARP_OPENFGA_MODEL_ID`, optional `LUMEWARP_OPENFGA_API_TOKEN`. Users are
  `user:<lumewarp user uuid>`; the standalone model is `deploy/openfga/model.fga` (`org` with
  `admin`, `team` with `admin`/`member`, `admin` implies `member`, no org -> team relation).
  Implements `batch_check`, `list_objects` (ListObjects) and `changes` (ReadChanges), all
  through the same name mapping:
  - `LUMEWARP_OPENFGA_OBJECT_TYPES='{"team": "llm_team", "org": "llm_org"}'` renames types;
  - `LUMEWARP_OPENFGA_RELATIONS='{"team.admin": "owner"}'` renames relations.

  Defaults are today's names. `deploy/openfga/model-renamed.fga` is the same model under
  `llm_org`/`llm_team` with `owner` as the team admin relation. `changes()` maps every changed
  tuple back to Lumewarp users; a change it cannot map to users (a userset or wildcard subject)
  asks the reconciler for a full rescan instead of being dropped. In a store shared with other
  applications, a change to any other object type (or to a relation Lumewarp does not map) makes
  the reconciler check that user, or rescan everything when no user can be named: expect extra
  checks in a busy store. `scripts/dev-idp.sh up`
  starts a local OpenFGA, writes the model and prints these variables.
- `http`: `LUMEWARP_AUTHZ_HTTP_URL` (+ optional `LUMEWARP_AUTHZ_HTTP_TOKEN`).
  `check` becomes `POST {"user_id", "relation", "resource": {"type", "id"}}` answered by
  `{"decision": "allow" | "deny"}`, with `type` `"org"` or `"team"`; anything else (non-2xx,
  timeout, bad body) is `ERROR`.

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
session cookie. The IdP authenticates only: installation roles live in Postgres and team roles
never come from the IdP.

| Setting | Meaning |
| --- | --- |
| `LUMEWARP_SECRET_KEY` | At least 32 bytes of random text; **required when SSO is enabled**. Derives the AES-256-GCM key for the login-state cookie. `serve` refuses to start with `LUMEWARP_OIDC_ISSUER` set and no key. Changing it invalidates pending logins only. |
| `LUMEWARP_OIDC_ISSUER` | Issuer URL; unset disables SSO. |
| `LUMEWARP_OIDC_CLIENT_ID`, `LUMEWARP_OIDC_CLIENT_SECRET` | Client credentials (secret optional for a public PKCE client). |
| `LUMEWARP_OIDC_SCOPES` | Default `openid email profile`. |
| `LUMEWARP_INITIAL_ADMIN_EMAIL` | The first verified login with this email becomes `proxy_admin` while no proxy_admin exists. Changing the setting afterwards never moves the role. |
| `LUMEWARP_PUBLIC_URL` | External base URL; the redirect URI is `<public_url>/api/v1/session/oidc/callback`. |
| `LUMEWARP_OIDC_JIT_USERS` | Create a user with no grants on first login (default true). |
| `LUMEWARP_SESSION_TTL_SECONDS` | Absolute session lifetime (default 12 h). |
| `LUMEWARP_SESSION_COOKIE_SECURE` | `Secure` cookie flag (default true; turn off only for plain-http dev). |

With the Helm chart, pass `LUMEWARP_SECRET_KEY` through `controlPlane.extraEnv` with a
`secretKeyRef`, as in the [chart README's SSO
example](../deploy/helm/lumewarp/README.md#sso-and-authorization-backend).

- Users are identified by the token's `iss` + `sub`. On a first login with a verified
  email, the identity links to an existing account with that email that
  has no identity from this issuer yet; otherwise a new user is created (JIT, only with a
  verified email). An account holding an installation role is never linked by email: a login
  with a new IdP identity for such an account is refused.
- No IdP claim grants any Lumewarp role: groups and roles claims are ignored. Installation
  roles are read from Postgres on every request; org and team relations come from the
  authorization backend, which only ever answers direct relations (`RequestAuthz` applies
  the org -> team inheritance).
- Inactive users cannot log in, and their existing sessions stop working immediately.
- The one-time login state is the encrypted cookie `lumewarp_oidc_state` (HttpOnly,
  `SameSite=Lax`, `Secure` per `LUMEWARP_SESSION_COOKIE_SECURE`, path
  `/api/v1/session/oidc`). It carries the state, the OIDC nonce and the PKCE code verifier,
  sealed with AES-256-GCM under a key derived from `LUMEWARP_SECRET_KEY`. The database row
  keeps only the sha256 of the state, return path, redirect URI and expiry, and is deleted on
  first use: a replayed callback, a tampered cookie, a cookie sealed with another key and a
  state from another login all fail as `invalid_state`.
- The cookie `lumewarp_session` is httpOnly and `SameSite=Lax`. Cookie-authenticated
  writes need the `X-Lumewarp-CSRF` header with the `csrf_token` from
  `GET /api/v1/session`. Bearer callers (PATs) never need it; if both a
  bearer and a cookie are sent, the bearer wins.
- `POST /api/v1/session/logout` ends the Lumewarp session (not the IdP session).
- IdP and authorization backend are kept in sync by the deployer: disabling a person in
  the IdP stops new logins, while revoking their team grants (OpenFGA) or removing them
  (built-in) is what suspends their keys.

## Audit log

Every mutation writes an `audit_log` row in the same transaction. Reads are scoped: an
installation admin sees everything (including rows without a team and rows of archived teams), a
team admin sees the rows of the teams they administer (filtered or not), an org admin additionally
sees its org-level events (`org_id` set, `team_id` NULL: org policy, org admins, org budget) and
the rows of its archived teams, and a plain member has no audit view (`403`). `details` is an
allowlist per action (`DETAIL_ALLOWLIST` in `audit.py`): only fields classified for that
action are stored, and anything else is dropped rather than redacted. Nested objects such
as `changes`, `settings` and `traffic_split` have their own field lists, and free-form
strategy params are never recorded. The failure column (`config_outbox.last_error`)
keeps only the exception class name. A test scans every
`audit.record(action=...)` call site, so a new action without a classification fails the
suite, and `tests/test_schema_secrets.py` fails when a new Text/String/JSONB column is not
classified as non-secret.

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

Log in at `/` through SSO or the emergency login, whichever is configured. `/my-keys` is the home
of a user who is neither an installation admin nor a team admin: their teams' effective models,
limits and remaining budget, and the keys they own. A team's **Keys** tab is where a team admin
manages every key in the team; a plain member sees only their own keys there, and the
team-admin tabs (Members, Service accounts) are not mounted for them at all. Team settings show
the quota read-only to a team admin (the name and key limits stay editable) and only the name
and key limits to a plain member. There is no separate `/keys` page. The API
types in `ui/src/api/schema.d.ts`
are generated from this service's OpenAPI document (`pnpm gen:api`).

## Plugins

Plugins run **only** in the control plane and produce declarative output that the compiler
bakes into the snapshot; they never run in the gateway request path. They are discovered
through Python entry points, so install the package into the same environment (`uv pip
install -e ./your-plugin`) and restart `serve`:

| Group | Purpose | Status |
| --- | --- | --- |
| `lumewarp.auth` | Map an external identity (OIDC, LDAP, ...) to a user's email and display name. | Called on a first SSO login; the first plugin (by entry-point name) wins. |
| `lumewarp.authz` | Answer relation questions. | Named by `LUMEWARP_AUTHZ_BACKEND`, see [Authorization](#authorization), which has its own contract (no `api_version`). |

Router policies are not Python plugins: a strategy is a Rust crate compiled into the
gateway, see [router strategies](../docs/router-strategies.md).

Every auth plugin declares `api_version = 1`; other versions are skipped with a warning, as
are plugins that fail to import. Entry points may name an instance, a class or a zero-argument
factory (classes/factories are called once at load).

```toml
# your-plugin/pyproject.toml
[project.entry-points."lumewarp.auth"]
my-idp = "my_plugin.auth:Identity"
```

Auth plugins implement `async map_identity(claims) -> IdentityMapping | None`, with
`IdentityMapping(email, display_name="")`. Anything but a mapping, or an invalid email or name,
refuses the login. The mapping says who the user is and grants nothing; the email must still be
verified by the IdP.

## Routing

The catalog is installation-wide. A client calls a *name*, and a name is one of three kinds,
unique across all three (`catalog_names`):

| Endpoint | What |
| --- | --- |
| `/api/v1/upstreams` | OpenAI-compatible endpoints. |
| `/api/v1/models` | A name for one model of one upstream (`upstream_id`, `upstream_model`), with its price. One try per call. |
| `/api/v1/aliases` | Another name for a model or a router (`target`, its id). One level only; not for another alias. Authorized on its own. |
| `/api/v1/routers` | A name that spreads requests over models: `strategy`, `params`, `members [{model_id, weight, params}]`. Members and fallbacks are models only. |
| `/api/v1/strategies` | Read-only: each strategy's descriptor, schema and the gateways that have it. |
| `/api/v1/catalog` | Read-only: every name and its kind, for permission pickers. |

`/catalog` is readable by any authenticated principal; the other reads (`/upstreams`, `/models`,
`/aliases`, `/routers`, `/strategies`) need `ROUTING_READ`, which is installation-level. Writes
need `ROUTING_MANAGE`. Every write is audited and enqueues a recompile. A name that a router, alias or fallback references cannot be
deleted (409, listing who references it); deleting a name deletes the grants on it, with an
audit row. See [the architecture notes](../docs/architecture.md#模型目錄已實作) for the reasoning.

**Renames.** `PATCH` with `name` works on all three kinds and references are by id, so
routers, aliases and grants follow. The old name is a 404 at once, except that a model's
`keep_old_name_as_alias` (the UI's default) adds an alias under the old name, with a copy of
the model's grants. `catalog_name_history` keeps old names for usage attribution; audit has
`model.rename`, `router.rename` and `alias.rename`. Metric labels switch to the new name, so
the old name's time series ends there, as usual in Prometheus. Usage rows keep the spelling of
their time and are filtered and grouped by id.

**Grants** (`policy_grants`, by id) follow the called name: a key allowed a router cannot call
its members by name, a key allowed an alias cannot call its target by name, and neither
direction is implied. A router's fallback is not re-checked. The API still reads and writes
`allowed_models` as names, which must exist; a member's or service account's list must be a
subset of its team's.

Settings that compile 1:1 into the snapshot (`proto/lumewarp/config/v1/snapshot.proto`):

| API field | Where | Snapshot / default |
| --- | --- | --- |
| `health_check {path, interval_ms, timeout_ms, unhealthy_threshold, healthy_threshold}` | upstream | `Upstream.health_check`; `null` = no active probing; null path = `/models`, null thresholds = 3 / 1 |
| `circuit_breaker {failure_threshold, open_ms}` | upstream | `Upstream.circuit_breaker`; `null` = never opens; `{}` = gateway defaults (5 failures, 30000 ms). Breaker state is per model |
| `metrics_url` | upstream | `Upstream.metrics_url` for `vllm_load`; `null` = base URL minus `/v1` plus `/metrics` |
| `strategy`, `params` | router | `Router.strategy`, a name: `weighted`, `least_in_flight`, `lowest_latency` (EWMA), `vllm_load`, or a strategy compiled into a gateway; `Router.params_json` |
| `members [{model_id, weight, params}]` | router | `Router.members`. Weight is per router; only `weighted` reads it |
| `retry {max_attempts, retry_on_status, total_timeout_ms}` | router | `Router.retry`; `null` = one attempt. `max_attempts: null` is emitted as 0, which the gateway reads as its default of 2 |
| `fallback_model_ids` (ordered) | router | `Router.fallbacks` (model names). Needs `retry` with at least 2 attempts |
| `traffic_split [{name, weight, members}]` | router | `Router.traffic_split`; with a split the router's own `members` stay empty |
| `sticky` (`none`/`header`/`team`/`key`), `sticky_header` | router | `Router.sticky` / `sticky_header` |

Validation mirrors the gateway's snapshot checks so an accepted write never makes a gateway
NACK (422 otherwise): health interval >= 1000 ms and 0 < timeout < interval, path absolute
without query/fragment; `metrics_url` plain http(s) without userinfo or query; `max_attempts` 1..5,
statuses 400..599 and unique; members and fallbacks exist, are models and are unique; at most
16 splits, names 1..32 of `[A-Za-z0-9_.-]` and unique, each split non-empty, weights >= 0
summing to more than 0; `sticky_header` (an HTTP header name, stored lower-case) required iff
`sticky=header`, and any sticky mode needs a split; a `weighted` router needs a positive total
member weight per split (or over its members without a split). PATCH replaces lists that are
given and keeps omitted fields; `retry: null` removes the policy, and leaving `sticky=header`
drops the header.

**Strategies.** `params` and member `params` are validated against the schema the strategy
declares, as reported by gateways in `Hello`, and against the committed built-in manifest
`compiler/builtin_strategies.json`, so built-ins work with no gateway connected. An unreported
strategy or a schema failure is 422. A connected gateway lacking the strategy is 422 unless the
write has `force`, audited as `router.force_save`, because that gateway will NACK the version.
Stored rows that no longer validate read `config_invalid: true` with `config_problems` and are
not modified. Full rules: [router strategies](../docs/router-strategies.md).

The compiler lists a capability in `required_capabilities` exactly when a field needs it
(the gateway's rule): `health_check`, `circuit_breaker`, `vllm_load` (a metrics URL), `retry`,
`fallbacks`, `traffic_split` (split or sticky) and `strategy:<name>` for every strategy in use.
`lowest_latency` is not a capability of its own any more: it is `strategy:lowest_latency`.

**Rollback** restores models, aliases, routers and their names by id from the version's
`catalog_state`, recreating deleted items under their old id, and re-publishes that version's
routing as a new version. Grants are not routing config: grants on an item deleted since then
do not come back. Keys, teams and policies come from current state, so a revoked key stays
revoked. It goes through the same strategy checks as a router write.

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
  Rollback restores the catalog by id and republishes an older version's **routing** only; keys
  and policies always come from current source state. Full rules: [config protocol](../docs/config-protocol.md).
- `UsageService.Report` validates per event (an invalid event is skipped and logged, the
  rest of the batch is kept; `REJECTED` events may omit model/model) and is
  idempotent by `request_id`; each new event
  adds cost once to the team total and once to the key owner's (member or service account)
  total for the UTC calendar month in the same transaction, then budget evaluation flips
  exhausted scopes to `BUDGET_EXCEEDED` and publishes a new version (eventual; in-flight
  streams are not cut). Budget publishing targets the protocol's 5 s
  eventual-enforcement window; it is not a configurable per-flip timeout. The unused
  `LUMEWARP_BUDGET_FLIP_DEADLINE_SECONDS` setting has been removed.
- Usage is stored in Postgres; ClickHouse is later. While the control plane is down the
  gateway keeps serving from its snapshot, but usage batches queue in memory and are lost
  if the queue fills or the gateway restarts.

## Metrics

`serve` exposes Prometheus metrics on `LUMEWARP_METRICS_BIND` (default
`127.0.0.1:9465`, text format on any path). Each app owns its own `CollectorRegistry`
(`metrics.py`); there are no process/platform collectors and no `*_created` series. Labels
are bounded enums only: no key, user, team or gateway ids. Counters move only after the
transaction that makes the event real has committed. The alert rules
(`../deploy/prometheus/rules.yaml`) and the control-plane/usage Grafana dashboards consume
these names.

| Metric | Type | Labels | Updated |
| --- | --- | --- | --- |
| `lumewarp_cp_compile_duration_seconds` | histogram | | Compiler loop (`compiler/publish.py`), per committed compile: snapshot compile + validation, including compiles whose content is unchanged and failed ones. |
| `lumewarp_cp_compile_failures_total` | counter | | A compile that raised `CompileError` (outbox rows marked failed; previous version stays current). |
| `lumewarp_cp_config_version` | gauge | | Latest published version of `LUMEWARP_SCOPE`: set on publish, and refreshed from `config_versions` every 5 s (covers API rollbacks and restarts). |
| `lumewarp_cp_outbox_pending` | gauge | | Pending `config_outbox` rows of the scope; one `COUNT` every 5 s by a background task, never per scrape. |
| `lumewarp_cp_gateway_sessions` | gauge | | Connected config-stream sessions in this process (`grpc/` session registry). |
| `lumewarp_cp_gateway_acks_total` | counter | `result` = `ack` \| `nack` | Each valid ACK/NACK of a pending update. Protocol errors are not counted. |
| `lumewarp_cp_gateway_strategies_ignored_total` | counter | | Strategies and capabilities of a Hello ignored: past the per-gateway limit of 128 strategies, or an invalid name. The stream stays up. |
| `lumewarp_cp_usage_events_ingested_total` | counter | | Events accepted by a committed `UsageService.Report` (an identical replay is accepted again but not re-costed). |
| `lumewarp_cp_usage_events_skipped_total` | counter | `reason` = `invalid_event` \| `request_id_conflict` \| `key_mismatch` | Events skipped by per-event validation of a committed report: malformed fields; a request id reused with different data in the batch or vs. a stored event; unknown key or key not in the event's team. Batch-level rejections (`INVALID_ARGUMENT`) count nothing. |
| `lumewarp_cp_authz_checks_total` | counter | `backend`, `decision` = `allow` \| `deny` \| `error` | Every `Authorizer.check` and every item of `Authorizer.batch_check` (native batch failures count one `error` per item). `backend` is `LUMEWARP_AUTHZ_BACKEND`. |
| `lumewarp_cp_keys_suspended_total` | counter | `reason` = `owner_removed` \| `owner_deactivated` \| `admin` \| `owner` | Keys moved from active to suspended: key reconciler and member removal (`owner_removed`), user deactivation (`owner_deactivated`), `PATCH /keys/{id}` (`admin`/`owner`). |
| `lumewarp_cp_key_reconcile_last_success_timestamp_seconds` | gauge | | Unix time of the key reconciler's last fully successful step: no exception, a decision for every owner checked and, with `changes()`, a readable change stream. 0 until the first success; not exported when `LUMEWARP_KEY_RECONCILE_ENABLED=false`. |
| `lumewarp_cp_key_writes_total` | counter | `operation` = `create` \| `rotate`, `result` = `success` \| `rate_limited` | Self-service key writes: a successful committed create/rotate, and an attempt rejected by the in-process per-principal, per-operation limiter. |

## Deployment

The Helm chart in [`../deploy/helm/lumewarp`](../deploy/helm/lumewarp) deploys this
service with `/readyz` and `/healthz` probes, the HTTP/gRPC/metrics Service (plus an
optional ServiceMonitor for the metrics port), the DB URL Secret and a migration hook Job.
The control plane must stay at one replica.

## Tests

```sh
uv run ruff check . && uv run ruff format --check . && uv run pytest
```

Tests need PostgreSQL: each run creates `lumewarp_test_<random>` on the server from
`LUMEWARP_TEST_DATABASE_URL` (default: the dev DB above), migrates it, and drops it at the
end.

## Schema changes

The history is one baseline, `20261106_0001_baseline.py`, generated from `models.py`:
Lumewarp has never been deployed, so there is no upgrade path from an earlier revision and
no data-preserving migration code. A fresh database runs that one file. `orgs` start empty
and `POST /teams` takes an explicit `org_id`; nothing is seeded.

Edit `models.py`, then with the dev DB at head:

```sh
uv run alembic revision --autogenerate -m "what changed"
```

Review the generated file (autogenerate duplicates enum CHECK constraints; keep the ones on
the `sa.Enum` columns). `tests/test_migrations.py::test_models_match_migrations` fails if
models and migrations drift.

## Protobuf

Generated modules are checked in under `src/lumewarp_control_plane/_proto/`. After changing
`../proto/`, run `../scripts/gen-proto.sh` (idempotent; generator pinned via
`grpcio-tools`).

## Layout

| Module | Purpose |
| --- | --- |
| `settings.py`, `db.py`, `models.py`, `migrations/`, `migrate.py` | config, DB, schema |
| `metrics.py` | Prometheus registry, metrics listener, DB-backed gauge refresher |
| `security.py`, `authz/`, `errors.py`, `audit.py`, `outbox.py` | auth, action matrix and authorization backends, error shape, audit, recompile outbox |
| `api/` | REST under `/api/v1` |
| `compiler/`, `plugins.py` | snapshots, versions, diff, rollback, plugin host |
| `grpc/`, `usage/` | ConfigService/UsageService, budgets |
| `app.py`, `__main__.py`, `static.py` | app factory, lifespan, CLI, Admin UI static serving |
