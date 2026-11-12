# Deploy with Docker

[`deploy/docker/`](https://github.com/lumewarp/lumewarp/tree/main/deploy/docker) runs
Lumewarp on one Docker host with Compose: PostgreSQL, the control plane and one gateway, from
the published images. For Kubernetes use [Helm](./helm.md); for what each part needs, see
[Deploy](./index.md).

| File | |
| --- | --- |
| `compose.yaml` | The services: `postgres`, a one-shot `migrate`, `control-plane`, and `gateway` (in the `gateway` profile, so it starts once it has a token). |
| `gateway.yaml` | The gateway's local settings: its two listeners. |
| `env.example` | Every setting the file reads; copy it to `.env`. |

What the host exposes:

| Port | Service | Bound to |
| --- | --- | --- |
| 4000 | Gateway API | all interfaces |
| 9464 | Gateway admin (`/metrics`, `/readyz`; no authentication) | `127.0.0.1` |
| 8091 | Control plane: Admin UI and API | `127.0.0.1` |
| 9465 | Control plane metrics | `127.0.0.1` |

The gRPC port (50051) and Postgres stay on the Compose network. Put a TLS reverse proxy in
front of 8091 (and of 4000 if clients are not on the same network) before exposing them.

## 1. Settings

```sh
cp -r deploy/docker /srv/lumewarp && cd /srv/lumewarp
cp env.example .env
```

In `.env`, set at least:

- `POSTGRES_PASSWORD`.
- `LUMEWARP_ENCRYPTION_KEY`: at least 32 random characters (`openssl rand -base64 48`). The
  gateway gets the same value.
- `LUMEWARP_BREAK_GLASS_PASSWORD_HASH`: the emergency login, used to sign in the first time.
  Create the hash with the image itself:

  ```sh
  docker compose run --rm -T control-plane hash-password <<< 'choose a long password'
  ```

  Paste it with every `$` doubled (`$$argon2id$$v=19$$…`): Compose reads `$` in `.env` as the
  start of a variable.

`LUMEWARP_SESSION_COOKIE_SECURE=true` keeps session cookies HTTPS-only, which is right
behind a TLS proxy. Browsers accept them on `http://localhost` too; set it to `false` only
to sign in over plain HTTP on another host name.

## 2. Start the control plane

```sh
docker compose up -d
curl -s http://127.0.0.1:8091/readyz
```

`migrate` runs first and exits; the control plane starts when it succeeded, and `/readyz`
answers `"status":"ready"` once the database, migrations and background tasks are up.
`migrate` runs again on every `up`, which is harmless at the same version.

## 3. Create the gateway token and start the gateway

```sh
docker compose run --rm control-plane bootstrap --gateway-id gw-1
```

The `lwg-…` token is printed once. Put it in `.env` as `LUMEWARP_GATEWAY_TOKEN` (and keep
`LUMEWARP_GATEWAY_ID=gw-1`: a token is bound to the id it was issued for), then:

```sh
docker compose --profile gateway up -d
curl -s http://127.0.0.1:9464/readyz     # "ready" once the first config version is applied
```

Changing `.env` recreates the control plane too, so the gateway may log one `config stream
ended` while it reconnects; that is expected. Pass `--profile gateway` to every later
`docker compose` command, or set `COMPOSE_PROFILES=gateway` in `.env`, so the gateway is
included.

## 4. First sign-in

Open <http://127.0.0.1:8091/>, enter the password you hashed as the **Break-glass
password**, and
create an upstream, a model, an org, a team and a key (the
[control-plane quick start](../guide/quickstart-control-plane.md) walks through it). Each
change publishes a config version that the gateway applies within seconds; then:

```sh
curl -sS -H "Authorization: Bearer lw-..." -H 'Content-Type: application/json' \
  http://127.0.0.1:4000/v1/chat/completions \
  --data-binary '{"model":"<model name>","messages":[{"role":"user","content":"Hello"}]}'
```

An upstream on the Docker host itself is not `127.0.0.1` from inside the gateway container:
use the host's address on the Compose network, or put the upstream on the same network.

For everyone else, set up SSO: add the `LUMEWARP_OIDC_*` settings and `LUMEWARP_SECRET_KEY`
from [Sign-in and authorization](./identity.md) to `.env`, with
`LUMEWARP_INITIAL_ADMIN_EMAIL` naming the person who becomes `proxy_admin` at first login.

## Operating it

| Task | How |
| --- | --- |
| Logs | `docker compose --profile gateway logs -f gateway control-plane` |
| Another gateway token | `docker compose run --rm control-plane bootstrap --gateway-id <id>`, or **Gateways → New gateway token** in the Admin UI |
| MCP and A2A | Set `LUMEWARP_GATEWAY_ID_KEY` (`openssl rand -base64 32`) and `LUMEWARP_GATEWAY_PUBLIC_URL` in `.env` |
| WASM plugins | Mount a directory into the gateway and set `LUMEWARP_ROUTER_PLUGIN_DIR`, `LUMEWARP_AUTH_PLUGIN_DIR` and `LUMEWARP_A2A_MIDDLEWARE_DIR` ([Gateway plugins](../plugins.md#loading)); plugin secrets are `LUMEWARP_PLUGIN_SECRET_*` variables |
| Metrics | Scrape `127.0.0.1:9464/metrics` and `127.0.0.1:9465/metrics` ([Metrics, alerts and tracing](./observability.md)) |
| Backups | The `postgres` volume holds everything; the gateway's `gateway-state` volume is a cache it rebuilds from the control plane |
| A new release | Lumewarp 0.x keeps no upgrade path: `docker compose --profile gateway down -v` (this **deletes the database**), set `LUMEWARP_VERSION`, and start again from step 2 |

The gateway keeps serving its last config while the control plane is stopped, and a
restarted gateway boots from the snapshot in its volume.

Both containers run as non-root users without a shell in the gateway image (distroless), so
debug from the logs and the admin endpoints.

## A standalone gateway

Without a control plane, the gateway image serves from one YAML file
([Gateway YAML](../reference/gateway-yaml.md)). Listeners must bind all interfaces inside
the container:

```yaml
# lumewarp.yaml
server:
  listen: 0.0.0.0:4000
  admin_listen: 0.0.0.0:9464
  api_keys: ["<sha256 of the client key>"]
upstreams:
  - {name: vllm, kind: vllm, base_url: 'http://vllm.internal:8000/v1', api_key_env: VLLM_API_KEY}
models:
  - {name: llama-3.1-8b, upstream: vllm, upstream_model: meta-llama/Llama-3.1-8B-Instruct, type: llm}
```

```sh
docker run -d --name lumewarp-gateway --restart unless-stopped \
  -p 4000:4000 -p 127.0.0.1:9464:9464 \
  -v "$PWD/lumewarp.yaml:/etc/lumewarp/lumewarp.yaml:ro" \
  -e VLLM_API_KEY \
  ghcr.io/lumewarp/lumewarp-gateway:0.1.0 --config /etc/lumewarp/lumewarp.yaml
```

The YAML is read once at startup: restart the container to apply a change. Generate the
client key and its hash as in [Gateway YAML](../reference/gateway-yaml.md#server).
