# Install

Lumewarp is two programs: the gateway (Rust) that clients call, and the control plane
(Python, with Postgres) that owns the configuration and serves the Admin UI. This page
installs both with the Helm chart. A gateway can also run alone from a YAML file:
[Quick start: standalone gateway](../guide/quickstart-standalone.md).

## Images and chart

A release publishes:

- `ghcr.io/lumewarp/lumewarp-gateway:<version>` and
  `ghcr.io/lumewarp/lumewarp-control-plane:<version>` (stable releases also `X.Y` and `latest`);
- the chart at `oci://ghcr.io/lumewarp/charts/lumewarp`;
- the example WASM plugins, attached to the GitHub release with their `SHA256SUMS`;
- both OpenAPI documents, attached as `openapi-control-plane.json` and `openapi-gateway.json`.

Both Dockerfiles use the repository root as build context so the gateway can read
`proto/` and the control-plane image can build `ui/`:

```sh
docker build -f gateway/Dockerfile -t ghcr.io/lumewarp/lumewarp-gateway:0.1.0 .
docker build -f control-plane/Dockerfile -t ghcr.io/lumewarp/lumewarp-control-plane:0.1.0 .
```

The gateway image is distroless, runs as non-root and includes a writable
`/var/lib/lumewarp` for the cached snapshot. The control-plane image runs the
uv-installed package on Python 3.12 as non-root and includes the built Admin UI.
The Helm chart below deploys both; set the image repositories and tags in its values.

## Prerequisites

- Kubernetes 1.25+ and Helm 3.
- A reachable PostgreSQL 16+ database and a Secret containing its SQLAlchemy async URL
  (`postgresql+asyncpg://user:password@host:5432/db`).
- Container images for both components, built from `gateway/Dockerfile` and
  `control-plane/Dockerfile`, or the published ones above; set the
  repositories/tags in `values.yaml`.
  - Gateway image: the `lumewarp` binary on `PATH`.
  - Control-plane image: the `lumewarp_control_plane` Python package installed for
    `python -m lumewarp_control_plane`; include the built Admin UI
    (`cd ui && pnpm build:cp`) if the UI should be served at `/`.

## Install

Create the database secret first:

```sh
kubectl create namespace lumewarp
kubectl -n lumewarp create secret generic lumewarp-control-plane \
  --from-literal=database-url='postgresql+asyncpg://lumewarp:secret@postgres.example:5432/lumewarp'
```

The control plane creates gateway tokens itself, so a fresh install has no gateway token
yet. A token is bound to one gateway id. The example uses `gw-1`; the chart's
`gateway.gatewayId` defaults to `gw-1`, and the two must match (or both use the id you
chose). Install the control plane first, bootstrap a token, then enable the gateway:

```sh
helm install lumewarp deploy/helm/lumewarp -n lumewarp \
  --set gateway.enabled=false

# Optional: the pre-install hook Job ran `migrate`. Create a gateway token:
kubectl -n lumewarp exec deploy/lumewarp-control-plane -- \
  python -m lumewarp_control_plane bootstrap --gateway-id gw-1

kubectl -n lumewarp create secret generic lumewarp-gateway \
  --from-literal=token='lwg-...'

helm upgrade lumewarp deploy/helm/lumewarp -n lumewarp \
  --reuse-values --set gateway.enabled=true --set gateway.gatewayId=gw-1
```

The gateway Deployment runs one replica: a token identifies one gateway, so do not
scale it. If you need more gateway instances, deploy another release (normally in a
separate namespace, or with `controlPlane.enabled=false`) with its own `gateway.gatewayId`
and token.

Installation admins are stored in Postgres: the first verified login whose email matches
`controlPlane.initialAdminEmail` becomes the proxy_admin, who can then assign other admins (and
every admin can assign more). Break-glass can create the first proxy_admin too when the setting
is empty, and is the recovery path when the proxy_admin leaves. A user must sign in through OIDC
once before receiving an installation role (assign/transfer return 409 otherwise), because a new
identity cannot be linked to an already privileged account. Set
`controlPlane.breakGlass.passwordHashSecret.name` to an existing Secret holding an argon2id hash
(`lumewarp-control-plane hash-password`). Neither replaces the first `bootstrap` call: that
command creates the scope row, and gateway tokens can only be created after the scope exists.

`controlPlane.databaseUrl.secretName` defaults to `lumewarp-control-plane` and
`gateway.token.secretName` to `lumewarp-gateway`; both must exist before the respective
pods start.

## What gets deployed

| Object | Notes |
| --- | --- |
| Control-plane Deployment | One replica. Runs the HTTP API + UI, gRPC ConfigService/UsageService, compiler loop and monthly rollover. `strategy: Recreate`. |
| Control-plane Service | `http` (8091), `grpc` (50051) and `metrics` (9465, Prometheus `/metrics`; only with `controlPlane.metrics.enabled`) ports. |
| Migration Job | Helm `pre-install,pre-upgrade` hook running `python -m lumewarp_control_plane migrate` with the database Secret. |
| Gateway Deployment | One replica. The token is bound to `gateway.gatewayId`; boots from its state volume, then follows the config stream; `/readyz` stays 503 until a snapshot is loaded. |
| Gateway Service | `http` (4000) and `admin` (9464, `/metrics` + `/readyz`) ports. |
| Gateway state PVC | Only when `gateway.stateDir.persistence.enabled=true`; otherwise an `emptyDir` per pod. |
| ServiceMonitors | Optional: one for the gateway admin port, one for the control-plane `metrics` port. Requires the Prometheus Operator CRDs. |

Readiness/liveness:

- Control plane: `/readyz` requires a reachable database, migrations at head and live
  gRPC/compiler/rollover tasks; `/healthz` is liveness.
- Gateway: `/readyz` on the admin port requires a loaded snapshot; `/healthz` stays
  healthy during a control-plane outage so Kubernetes does not restart a gateway that is
  correctly serving its cached config.

## Upstream API keys

An upstream's API key can be entered in the Admin UI instead of naming a gateway
environment variable. It is stored sealed with `LUMEWARP_ENCRYPTION_KEY`, which both the
control plane and every gateway need. Create a Secret with at least 32 random characters
and point the chart at it:

```sh
kubectl create secret generic lumewarp-encryption \
  --from-literal=encryption-key="$(openssl rand -base64 48)"
```

```yaml
encryptionKey:
  secretName: lumewarp-encryption
  key: encryption-key
```

It is separate from `LUMEWARP_SECRET_KEY` (OIDC login cookie, control plane only).
Changing it makes the stored keys unreadable: gateways NACK the next snapshot until every
stored key is entered again.

## Gateway plugins (WASM)

The gateway loads WASM components from three directories at startup and never reads them
again. They are not in the gateway image: a plugin is one `.wasm` file in its kind's
directory, so installing or replacing one is a file write plus a pod restart. The chart
mounts one volume and gives each kind a subdirectory:

| Subdirectory | Variable | Advertised capability |
| --- | --- | --- |
| `router` | `LUMEWARP_ROUTER_PLUGIN_DIR` | `strategy:<name>` |
| `auth` | `LUMEWARP_AUTH_PLUGIN_DIR` | `auth:<name>` |
| `a2a-middleware` | `LUMEWARP_A2A_MIDDLEWARE_DIR` | `a2a_middleware:<name>` |

Build the example components with `bash gateway/plugins/build.sh` (see
[`docs/plugins.md`](../plugins.md) for the manifest, the sandbox and the
build of a new crate) and put the files on a volume:

```yaml
gateway:
  plugins:
    volume:
      persistentVolumeClaim:
        claimName: gateway-plugins   # holds router/, auth/ and a2a-middleware/
    memoryLimitBytes: "536870912"   # optional; the gateway default is 1 GiB
    secretEnvs:
      - gateway-plugin-secrets      # every key named LUMEWARP_PLUGIN_SECRET_<NAME>
```

A ConfigMap works when its keys are placed into the three directories:

```yaml
gateway:
  plugins:
    volume:
      configMap:
        name: gateway-plugins
        items:
          - {key: cheapest-first.wasm, path: router/cheapest-first.wasm}
          - {key: hmac-signature.wasm, path: auth/hmac-signature.wasm}
```

The default `volume: {}` mounts nothing and sets no directory variable, which is valid:
built-in strategies and built-in auth are native code. A directory that the variable
names but that does not exist refuses to start the gateway, so create all three
subdirectories on the volume even when one stays empty.

`memoryLimitBytes` caps all plugin instances together; each instance is capped at 32 MiB
on its own. Every key of every Secret in `secretEnvs` becomes an environment variable, so
name them `LUMEWARP_PLUGIN_SECRET_<NAME>`: an `upstream-auth` component reads them through
`secret()`, and the host exposes no variable without that prefix. The host does not scope
them per plugin, so put the plugin name in the key. No credential belongs in plugin
settings: those are stored in the snapshot and the audit trail.

Deploy the component to every gateway before a router, MCP server or A2A agent names it. A
gateway without it NACKs every version that references it — key revocations included —
until it is restarted with the file present.

### MCP and A2A endpoints

The MCP and A2A endpoints need two more settings; LLM traffic reads neither.

```yaml
gateway:
  publicUrl: https://gateway.example.com  # LUMEWARP_GATEWAY_PUBLIC_URL
  idKey:
    secretName: lumewarp-gateway-id       # LUMEWARP_GATEWAY_ID_KEY from this Secret
    key: gateway-id-key
```

```sh
kubectl -n lumewarp create secret generic lumewarp-gateway-id \
  --from-literal=gateway-id-key="$(openssl rand -base64 48)"
```

`publicUrl` is the external base URL an agent card points at (the Host header is never
trusted) and must be an absolute http(s) URL without a query. The id key (base64, at least
32 bytes) MACs A2A task and context ids and seals MCP sessions, so it must be identical on
every gateway that serves the same installation; the chart takes it only from a Secret.
When the id key is unset both MCP and A2A answer 503; when only `publicUrl` is unset,
MCP still works and A2A answers 503. Nothing else is affected.

Both are read at startup: changing `publicUrl` or rotating the id key is a rollout, and a
rotation invalidates MCP sessions and the A2A task ids already handed out.

## SSO and authorization backend

The control plane reads every setting from `LUMEWARP_*` environment variables (see the
[Control plane](./control-plane.md)); pass SSO and authorization
settings through `controlPlane.extraEnv`, secrets through `secretKeyRef`:

```yaml
controlPlane:
  extraEnv:
    - name: LUMEWARP_PUBLIC_URL
      value: https://lumewarp.example.com
    - name: LUMEWARP_OIDC_ISSUER
      value: https://keycloak.example.com/realms/lumewarp
    - name: LUMEWARP_OIDC_CLIENT_ID
      value: lumewarp-cp
    - name: LUMEWARP_OIDC_CLIENT_SECRET
      valueFrom: {secretKeyRef: {name: lumewarp-oidc, key: client-secret}}
    # Required whenever LUMEWARP_OIDC_ISSUER is set: at least 32 random bytes. It seals
    # the OIDC login-state cookie (nonce + PKCE verifier); rotate it to invalidate pending
    # logins only. Keep it in a Secret, never in values.yaml.
    - name: LUMEWARP_SECRET_KEY
      valueFrom: {secretKeyRef: {name: lumewarp-oidc, key: secret-key}}
    # Optional: grants in OpenFGA instead of the built-in membership table.
    - name: LUMEWARP_AUTHZ_BACKEND
      value: openfga
    - name: LUMEWARP_OPENFGA_URL
      value: http://openfga.openfga:8080
    - name: LUMEWARP_OPENFGA_STORE_ID
      value: 01H...
    - name: LUMEWARP_OPENFGA_MODEL_ID
      value: 01H...
    # Optional: when the store names its objects/relations differently.
    - name: LUMEWARP_OPENFGA_OBJECT_TYPES
      value: '{"team": "llm_team", "org": "llm_org"}'
    - name: LUMEWARP_OPENFGA_RELATIONS
      value: '{"team.admin": "owner"}'
```

The IdP must allow the redirect URI `<LUMEWARP_PUBLIC_URL>/api/v1/session/oidc/callback`.
Write the OpenFGA model from `deploy/openfga/model.fga` before switching the backend;
`deploy/openfga/model-renamed.fga` is the same model under the names of the mapping example
above. Lumewarp answers direct `admin` (org/team) and `member` (team) relations only; org
administration is inherited from `team.org_id` by the control plane.

## Values

Common values (see [`values.yaml`](https://github.com/lumewarp/lumewarp/blob/main/deploy/helm/lumewarp/values.yaml) for the complete list):

| Value | Default | Meaning |
| --- | --- | --- |
| `controlPlane.enabled` | `true` | Deploy the control plane. |
| `controlPlane.replicaCount` | `1` | Must stay 1. |
| `controlPlane.image.repository` / `.tag` | `ghcr.io/lumewarp/lumewarp-control-plane` / chart appVersion | Control-plane image. |
| `controlPlane.databaseUrl.secretName` / `.key` | `lumewarp-control-plane` / `database-url` | Existing Secret with `LUMEWARP_DATABASE_URL`. |
| `controlPlane.initialAdminEmail` | `""` | `LUMEWARP_INITIAL_ADMIN_EMAIL`: the email that becomes proxy_admin on its first verified login while no proxy_admin exists. Empty leaves emergency access only. |
| `controlPlane.breakGlass.passwordHashSecret.name` / `.key` | unset / `password-hash` | Existing Secret with `LUMEWARP_BREAK_GLASS_PASSWORD_HASH` (argon2id). Unset disables the emergency login. |
| `controlPlane.scope` | `default` | Config scope published to gateways. |
| `controlPlane.service.httpPort` / `.grpcPort` | `8091` / `50051` | Service and container ports. |
| `controlPlane.service.metricsPort` | `9465` | Control-plane Prometheus listener (`LUMEWARP_METRICS_BIND`), separate from the API port. |
| `controlPlane.metrics.enabled` | `true` | Serve and expose `lumewarp_cp_*` metrics; `false` leaves `LUMEWARP_METRICS_BIND` empty. |
| `controlPlane.grpcTls.enabled` | `false` | Serve gateway gRPC over TLS from a Secret. |
| `controlPlane.migrate.enabled` | `true` | Run the migration hook Job. |
| `gateway.enabled` | `true` | Deploy the gateway. |
| `gateway.replicaCount` | `1` | Must stay 1; one token per gateway id. |
| `gateway.image.repository` / `.tag` | `ghcr.io/lumewarp/lumewarp-gateway` / chart appVersion | Gateway image. |
| `gateway.token.secretName` / `.key` | `lumewarp-gateway` / `token` | Existing Secret with a control-plane-created gateway token. |
| `gateway.controlPlaneUrl` | empty | Defaults to the control-plane Service gRPC URL in this namespace. |
| `gateway.scope` | empty | Defaults to `controlPlane.scope`. |
| `gateway.gatewayId` | `gw-1` | Required; must equal the gateway id the token was issued for. It is never derived from the pod name. |
| `gateway.podSecurityContext` | UID/GID/fsGroup `65532`, `runAsNonRoot: true` | Matches the gateway image so emptyDir/PVC mounts are writable. Override only for a custom image with another UID/GID. |
| `gateway.stateDir.path` | `/var/lib/lumewarp` | Snapshot directory. |
| `gateway.stateDir.persistence.enabled` | `false` | Use a RWO PVC instead of `emptyDir`. |
| `gateway.stateDir.persistence.existingClaim` | empty | Use an existing RWO claim; it must be used by this single replica only. |
| `gateway.server.*` | see values | `server:` block of the gateway's local YAML (listeners/timeouts). `adminListen` must be reachable by probes. |
| `gateway.plugins.volume` | `{}` | Existing volume with the WASM components, as a Kubernetes volume source; empty loads no plugins and sets no directory variable. |
| `gateway.plugins.mountPath` / `.dirs` | `/etc/lumewarp/plugins` / `router`, `auth`, `a2a-middleware` | Mount point and the subdirectory each kind reads. |
| `gateway.plugins.memoryLimitBytes` | `""` | `LUMEWARP_PLUGIN_MEMORY_LIMIT_BYTES`; empty = the gateway default, 1 GiB. |
| `gateway.plugins.secretEnvs` | `[]` | Existing Secrets whose keys become `LUMEWARP_PLUGIN_SECRET_<NAME>` for `secret()`. |
| `gateway.publicUrl` | `""` | `LUMEWARP_GATEWAY_PUBLIC_URL`: external base URL for agent cards; empty answers 503 on A2A. |
| `gateway.idKey.secretName` / `.key` | unset / `gateway-id-key` | Existing Secret with `LUMEWARP_GATEWAY_ID_KEY` (base64, ≥32 bytes); shared by every gateway of the installation. |
| `serviceMonitor.enabled` | `false` | Render ServiceMonitors for the gateway admin port and the control-plane metrics port (each only if that component and its metrics are enabled). |
| `serviceMonitor.labels` | `{}` | Extra labels so your Prometheus selects the ServiceMonitor. |

All pods honour `nodeSelector`, `tolerations`, `affinity`, `resources`,
`podSecurityContext`, `securityContext`, `extraEnv` and `extraArgs` (per component), plus
global `imagePullSecrets`, `nameOverride`, `fullnameOverride` and `commonLabels`.

## Verify a release

```sh
helm lint deploy/helm/lumewarp
helm template lumewarp deploy/helm/lumewarp -n lumewarp \
  --set controlPlane.databaseUrl.secretName=db \
  --set gateway.gatewayId=gw-1 \
  --set gateway.token.secretName=gw

# Expected to fail: one token is bound to one gateway id; multi-replica gateways are
# deferred.
helm template lumewarp deploy/helm/lumewarp -n lumewarp \
  --set gateway.replicaCount=2
```

`kubectl port-forward svc/lumewarp-control-plane 8091:8091` reaches the UI/API; probe the
gateway with `kubectl port-forward svc/lumewarp-gateway 4000:4000` and
`curl localhost:4000/healthz`.
