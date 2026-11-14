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
uses an argon2id hash and OIDC, see the
[Sign-in and authorization](../operations/identity.md)), then create:

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
enforced eventually. See [Control plane](../operations/control-plane.md) for
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

The stack runs two gateway instances of one gateway id, `gw-dev`: <http://127.0.0.1:4000/>
(admin `127.0.0.1:9464`) and <http://127.0.0.1:4001/> (admin `127.0.0.1:9466`). They share
rate limits through a Valkey at `127.0.0.1:56379`, without persistence
([shared limits](../operations/gateway.md#shared-rate-limits)).
