# Quick start: with the control plane

The control plane owns the config; the gateway follows it over gRPC and keeps the
last ACKed snapshot on disk so it can keep serving (and restart) while the control
plane is down.

Start the mock upstream as above, then Postgres and the control plane:

```sh
./scripts/dev-db.sh up
cd control-plane
uv sync

# Build the Admin UI once (Node 24 + pnpm); the control plane serves it at /.
# `pnpm check` runs the type check and the component guard (shadcn/ui only, no legacy
# classes or native elements outside ui/src/components/ui).
# Skip this for an API-only setup.
(cd ../ui && pnpm install && pnpm check && pnpm build:cp)

uv run lumewarp-control-plane migrate
export LUMEWARP_GATEWAY_ZONE_DEFAULT_TOKEN='replace-with-a-random-secret-at-least-32-characters'
LUMEWARP_DEV_MODE=true LUMEWARP_BREAK_GLASS_PASSWORD=dev-break-glass \
  LUMEWARP_SESSION_COOKIE_SECURE=false \
  uv run lumewarp-control-plane serve
```

The HTTP API/UI listens on `127.0.0.1:8091`; gateway-facing gRPC listens on
`127.0.0.1:50051`. The dev Postgres from `scripts/dev-db.sh` listens on
`127.0.0.1:55432`, so it does not collide with a local 5432.

Open <http://127.0.0.1:8091/> and use the emergency login with the password
`dev-break-glass` (a plaintext password needs `LUMEWARP_DEV_MODE=true`; production uses an
argon2id hash and OIDC, see [Sign-in and authorization](../operations/identity.md)). The
control plane creates the default zone and reads its fixed gateway token from
`LUMEWARP_GATEWAY_ZONE_DEFAULT_TOKEN` at startup. The token must be at least 32 characters;
the control plane keeps only its SHA-256 hash in memory. There is no token-creation UI, API
or CLI.

Start the gateway in another terminal with that token:

```sh
cd gateway
# Use exactly the value configured as LUMEWARP_GATEWAY_ZONE_DEFAULT_TOKEN above.
export LUMEWARP_GATEWAY_TOKEN='replace-with-a-random-secret-at-least-32-characters'
cargo run --release -- --control-plane http://127.0.0.1:50051 \
  --gateway-token-env LUMEWARP_GATEWAY_TOKEN \
  --state-dir /tmp/lumewarp-state --zone default
```

The gateway is not ready until it has a first config version (`/v1/*` and the admin
`/readyz` return 503), and the process never falls back to an open router. The control
plane publishes an empty version when it starts, so here the gateway is ready at once
and answers 401 until a key exists. In the UI, create:

- an upstream in the default zone for `http://127.0.0.1:9100/v1` (kind `openai_compatible`),
- a global model named `mock-local` deployed in the default zone using upstream model `mock`,
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
enforced eventually. The default zone is ready for a first install; use the header switcher
to view other zones. See [Zones](../admin/zones.md). See [Control plane](../operations/control-plane.md) for
settings, SSO, authorization backends, the catalog, plugins and operations, and
[`docs/config-protocol.md`](../config-protocol.md) for the stream contract.

## The full development stack

`make dev` starts the stack with these development-only accounts, defined in
`deploy/dev/keycloak/realm.json`, `deploy/dev/compose.yaml` and `scripts/dev.sh`.

| Login | Where | Username | Password | Access |
| --- | --- | --- | --- | --- |
| SSO | <http://127.0.0.1:5173/> | `alice@example.com` | `lumewarp` | Installation admin: first login becomes `proxy_admin` through `LUMEWARP_INITIAL_ADMIN_EMAIL` |
| SSO | <http://127.0.0.1:5173/> | `bob@example.com` | `lumewarp` | Plain user: no team and no grants until an admin adds them |
| SSO | <http://127.0.0.1:5173/> | `carol@example.com` | `lumewarp` | Plain user: no team and no grants until an admin adds them |
| Emergency | <http://127.0.0.1:5173/> | none | `dev-break-glass` | Emergency admin session; `LUMEWARP_BREAK_GLASS_PASSWORD` overrides the password |
| Keycloak console | <http://127.0.0.1:18080/> | `admin` | `lumewarp` | Administers the IdP, not Lumewarp |
| Open WebUI | <http://127.0.0.1:3000/> | none | none | Chat client with the demo key: the gateway's models, and its MCP tools as the `Lumewarp` tool server; `DEV_OPENWEBUI=0` skips it |

The seeded demo API key is in `.dev/demo-key`. Postgres is at `127.0.0.1:55432`, with
database, user and password all `lumewarp`.

Every start also runs `uv run lumewarp-control-plane seed-dev` against the running control
plane, so the pages and dashboards are populated without using the UI first. That seed adds
the orgs `acme` and `research` (the existing `default` stays), the teams `search`,
`platform` and `ml-research`, six users as members or team admins (one of them an org
admin), a service account with one key per zone in every team, five priced models
(`llama-3.1-8b`, `qwen2.5-32b`, `gpt-4o-mini`, `bge-m3`, `whisper-large-v3`) deployed on the
mock upstream in both zones, the aliases `chat-default` and `embed`, the routers `chat` (with session affinity) and
`chat-ab`, a model group, MCP/A2A grants for `example` and `helper`, and org, team, member
and service-account limits and budgets above the seeded spend. It then ingests 30 UTC days
of usage in both zones, ending now: weekday/weekend and hour-of-day curves, calls through
models, aliases and routers, streaming and non-streaming, embeddings, transcription, MCP
`tools/call` and A2A requests, and about 3% 429/5xx with their outcomes. Key secrets are in
`.dev/seed-keys` (not the database); the run deletes and re-ingests only the seed teams'
`usage_events` and rebuilds their monthly totals, so the window slides forward and other
teams' history is untouched. `seed-dev --days N --seed N --no-usage` changes the window or
seeds the catalog only; it refuses to run unless `LUMEWARP_DEV_MODE` is on and the database
is loopback.

The stack runs two gateways sharing the default zone's fixed token. The control plane
assigns each stream's gateway id in its first `Welcome`; a reconnect gets a new id and row:
<http://127.0.0.1:4000/> (admin `127.0.0.1:9464`) and <http://127.0.0.1:4001/> (admin
`127.0.0.1:9466`). They share rate limits through a Valkey at `127.0.0.1:56379`, without
persistence ([shared limits](../operations/gateway.md#shared-rate-limits)).

Authorization uses the built-in backend, and OpenFGA is not started. Use
`DEV_AUTHZ=openfga make dev` to also start an OpenFGA at `127.0.0.1:18081`, create its store
and make it the authorization backend.
