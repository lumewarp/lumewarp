# Deploy with Helm

The chart in [`deploy/helm/lumewarp`](https://github.com/lumewarp/lumewarp/tree/main/deploy/helm/lumewarp)
deploys the control plane and a gateway (one replica or many) on Kubernetes. Each release
also publishes it as `oci://ghcr.io/lumewarp/charts/lumewarp`, versioned like the images.
For one host without Kubernetes, see [Deploy with Docker](./docker.md); for what the parts
are, see [Deploy](./index.md).

## Prerequisites

- Kubernetes 1.25+ and Helm 3.
- A reachable PostgreSQL 16+ database and a Secret containing its SQLAlchemy async URL
  (`postgresql+asyncpg://user:password@host:5432/db`).
- Container images for both components: the published ones (the default; an empty tag
  means the chart's app version) or your own built from `gateway/Dockerfile` and
  `control-plane/Dockerfile` ([Building images](./index.md#building-images)); set the
  repositories and tags in `values.yaml`.
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

The commands below install the published chart; from a checkout, use the path
`deploy/helm/lumewarp` instead of `$CHART` and drop `--version`.

```sh
CHART=oci://ghcr.io/lumewarp/charts/lumewarp
VERSION=0.1.0
```

The control plane creates gateway tokens itself, so a fresh install has no gateway token
yet. A token is bound to one gateway id. The example uses `gw-1`; the chart's
`gateway.gatewayId` defaults to `gw-1`, and the two must match (or both use the id you
chose). Install the control plane first, bootstrap a token, then enable the gateway:

```sh
helm install lumewarp "$CHART" --version "$VERSION" -n lumewarp \
  --set gateway.enabled=false

# Optional: the pre-install hook Job ran `migrate`. Create a gateway token:
kubectl -n lumewarp exec deploy/lumewarp-control-plane -- \
  python -m lumewarp_control_plane bootstrap --gateway-id gw-1

kubectl -n lumewarp create secret generic lumewarp-gateway \
  --from-literal=token='lwg-...'

helm upgrade lumewarp "$CHART" --version "$VERSION" -n lumewarp \
  --reuse-values --set gateway.enabled=true --set gateway.gatewayId=gw-1
```

The gateway runs one replica by default. `gateway.replicaCount` raises that: every replica
uses the same token and the same `gateway.gatewayId`, because one token identifies one
gateway and not one process ([Gateway replicas](#gateway-replicas)). All replicas load the
snapshot from the control plane and serve the same models.

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
| Gateway Deployment | One replica by default. The token is bound to `gateway.gatewayId`; boots from its state volume, then follows the config stream; `/readyz` stays 503 until a snapshot is loaded. |
| Gateway StatefulSet | Instead of the Deployment when `gateway.stateDir.persistence.enabled`: same pod, one ReadWriteOnce claim per pod from a `volumeClaimTemplate`. |
| Gateway Service | `http` (4000) and `admin` (9464, `/metrics` + `/readyz`) ports. |
| Gateway headless Service | Only with persistence: the governing Service of the StatefulSet (`clusterIP: None`). |
| Gateway PodDisruptionBudget / HorizontalPodAutoscaler | Optional, `gateway.podDisruptionBudget` and `gateway.autoscaling`; see [Gateway replicas](#gateway-replicas). |
| Valkey Deployment, Service and Secret | Only when `redis.enabled`: shared rate limits across gateway replicas; see [Shared rate limits](#shared-rate-limits). |
| ServiceMonitors | Optional: one for the gateway admin port, one for the control-plane `metrics` port. Requires the Prometheus Operator CRDs. |

Readiness/liveness:

- Control plane: `/readyz` requires a reachable database, migrations at head and live
  gRPC/compiler/rollover tasks; `/healthz` is liveness.
- Gateway: `/readyz` on the admin port requires a loaded snapshot; `/healthz` stays
  healthy during a control-plane outage so Kubernetes does not restart a gateway that is
  correctly serving its cached config.

## Gateway replicas

`gateway.replicaCount` sets how many pods serve one gateway. They share the token in
`gateway.token.secretName` and the id in `gateway.gatewayId`: the control plane accepts one
config stream per instance, and the Admin UI's Gateways page shows one row per gateway with
its instances and the version every instance has applied. A rolling update therefore never
produces two gateway identities, and the Service load balances across the pods.

Two consequences:

- **Rate limits count per instance by default.** Without a shared Redis a key's rpm, tpm and
  max_parallel apply to each pod, so three replicas admit up to three times the limit.
  Configure [shared rate limits](#shared-rate-limits) to count them once.
- **A fresh pod may be unready.** With the default state directory a new pod has no cached
  snapshot and `/readyz` answers 503 until the control plane sends one, while the other pods
  keep serving.

### State volume

`gateway.stateDir.persistence.enabled=false` (the default) renders the gateway as a Deployment
whose state directory is a per-pod `emptyDir`. Setting it to `true` renders a StatefulSet with
a `volumeClaimTemplate` instead: one ReadWriteOnce claim per pod, `size` and `storageClass`
from the values, created by the chart, so every pod keeps its cached snapshot across
restarts. The StatefulSet needs a governing headless Service (`lumewarp-gateway-headless`),
which the chart adds and which exposes `http` only, so Prometheus does not discover every pod a
second time; clients keep calling the regular Service. There is no `existingClaim` option: one
claim cannot serve several pods, and the chart refuses the value.

Kubernetes treats a StatefulSet's claim template as immutable: `gateway.stateDir.persistence.size`,
`.storageClass` and `.annotations` are fixed once the StatefulSet exists, and changing them makes
the next `helm upgrade` fail with `updates to statefulset spec ... are forbidden`. To change one,
delete the StatefulSet while leaving its pods and claims alone, then upgrade:

```sh
kubectl -n lumewarp delete statefulset lumewarp-gateway --cascade=orphan
helm upgrade lumewarp "$CHART" --version "$VERSION" -n lumewarp \
  --reuse-values --set gateway.stateDir.persistence.size=5Gi
```

The upgrade recreates the StatefulSet with the new template and adopts the running pods.
Existing claims keep their size — edit each one if the StorageClass allows expansion
(`state-lumewarp-gateway-<ordinal>`), or delete it and let its pod recreate it — and only
newly created pods get the new template.

Switching persistence on a live release changes the kind: Helm deletes the object it no longer
renders (the Deployment when persistence is turned on, the StatefulSet and its headless Service
when it is turned off) and creates the other, so the pods restart once. Claims created from the
`volumeClaimTemplate` are not part of the release and stay behind; delete them when they are no
longer needed. Upgrading a release that used the old single `lumewarp-gateway-state` PVC
deletes that PVC (the old template had no keep policy), which costs one cold boot per gateway
pod. A pod removed from a StatefulSet — scaled down, or in the middle of a rollout that shrinks
it — leaves its claim behind too.

### Autoscaling and disruptions

`gateway.autoscaling.enabled` renders an `autoscaling/v2` HorizontalPodAutoscaler and drops
the fixed replica count from the workload. It targets CPU utilization against
`gateway.resources.requests.cpu`, between `minReplicas` and `maxReplicas`, so it needs
metrics-server; with no CPU request it never scales.

`gateway.podDisruptionBudget.enabled` renders a `PodDisruptionBudget` with `minAvailable` or
`maxUnavailable`. It is only rendered when the gateway can have more than one pod
(`replicaCount > 1` or autoscaling on), because a budget over a single pod only blocks node
drains.

## Shared rate limits

The gateway decides admission locally and never calls Redis on the request path: it syncs its
counts with the other gateways in the background. rpm, tpm and max_parallel are counted per
gateway instance unless the gateways share a Redis, so several replicas of one gateway need
one to honour a key's limit once. A gateway keeps serving when Redis is unreachable; it falls
back to counting for itself, which makes the limits per instance again until the sync
recovers. The `lumewarp_shared_limits_*` metrics and the `LumewarpSharedLimitsFallback` alert
show both ([Observability](./observability.md)).

### Bundled Valkey

`redis.enabled=true` adds one Valkey (Redis protocol, BSD-licensed) to the release: a
ClusterIP Service on 6379, a Deployment of a pinned image, and a Secret with its password. It
keeps nothing on disk — every key the limiter writes expires within 150 s, so a restart only
costs a short fallback window — runs as the image's non-root user with a read-only root
filesystem, and answers `ping` for its probes. The gateway receives
`LUMEWARP_SHARED_LIMITS_REDIS_URL` built from that Service and the password, interpolated by
the kubelet from a `secretKeyRef`, so the password never appears in a manifest.

With `redis.auth.existingSecret` unset the chart creates the password Secret itself, once,
with a random 32-character password, and reuses it on every upgrade (`lookup`). `helm
template` has no cluster to ask and renders a new one each time. A password supplied through
`redis.auth.existingSecret` (or `gateway.sharedLimits.redis.existingSecret`) must be
URL-safe (no `@ : / % # ?`), because the chart interpolates it into the Redis URL. To supply
your own password,
set `redis.auth.existingSecret` and `existingSecretKey`: the value is the password alone,
without a URL around it, and it must be URL-safe because the gateway receives it inside a
`redis://` URL.

Valkey writes the password to a config file on its ephemeral `/data` volume at startup and runs
`valkey-server` from it, so the password is never an argument of the server process. The pods
read it once, at startup: after rotating the password Secret, restart the Valkey pod and the
gateway pods (`kubectl rollout restart deploy/<release>-lumewarp-redis` and
`deploy/<release>-lumewarp-gateway`, or `statefulset/` when persistence is on), otherwise they
keep using the old password until something restarts them.

### An existing Redis

To use a Redis you run yourself, managed or in another namespace, create a Secret with the
full URL (`rediss://` for TLS) and point the gateway at it:

```sh
kubectl -n lumewarp create secret generic lumewarp-redis-url \
  --from-literal=url='redis://:password@redis.example:6379/0'
```

```yaml
gateway:
  sharedLimits:
    redis:
      existingSecret: lumewarp-redis-url   # key: url
    syncMs: 100          # optional, 10-1000; the gateway default is 100
    namespace: default   # optional; the config scope by default
```

`redis.enabled` and `gateway.sharedLimits.redis.existingSecret` are mutually exclusive: a
release with both fails to render.

### Several releases on one Redis

One release can hold the gateway replicas and their Valkey; a gateway-only release elsewhere
(another cluster, `controlPlane.enabled=false`, `gateway.controlPlaneUrl` set) points at the
same Redis with `gateway.sharedLimits.redis.existingSecret`. Every gateway that should share
counts needs the same Redis and the same `gateway.sharedLimits.namespace`, which is the
config scope unless it is set. Router state — health, circuit breakers, latency — stays local
to each gateway.

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
| `gateway.replicaCount` | `1` | Pods serving the same gateway; they share the token and `gateway.gatewayId` and count rate limits per instance unless a Redis is shared. Ignored when autoscaling is on. |
| `gateway.autoscaling.enabled` / `.minReplicas` / `.maxReplicas` / `.targetCPUUtilizationPercentage` | `false` / `2` / `5` / `80` | HPA on CPU utilization over `gateway.resources.requests.cpu`; when enabled the workload has no fixed replica count. |
| `gateway.podDisruptionBudget.enabled` / `.minAvailable` / `.maxUnavailable` | `false` / `1` / empty | `PodDisruptionBudget`; set one of the two, and it is only rendered when the gateway can have more than one pod. |
| `gateway.image.repository` / `.tag` | `ghcr.io/lumewarp/lumewarp-gateway` / chart appVersion | Gateway image. |
| `gateway.token.secretName` / `.key` | `lumewarp-gateway` / `token` | Existing Secret with a control-plane-created gateway token. |
| `gateway.controlPlaneUrl` | empty | Defaults to the control-plane Service gRPC URL in this namespace. |
| `gateway.scope` | empty | Defaults to `controlPlane.scope`. |
| `gateway.gatewayId` | `gw-1` | Required; must equal the gateway id the token was issued for. It is never derived from the pod name. |
| `gateway.podSecurityContext` | UID/GID/fsGroup `65532`, `runAsNonRoot: true` | Matches the gateway image so emptyDir/PVC mounts are writable. Override only for a custom image with another UID/GID. |
| `gateway.stateDir.path` | `/var/lib/lumewarp` | Snapshot directory. |
| `gateway.stateDir.persistence.enabled` | `false` | `false` = Deployment with a per-pod `emptyDir`; `true` = StatefulSet with one ReadWriteOnce claim per pod. |
| `gateway.stateDir.persistence.size` / `.storageClass` / `.annotations` | `1Gi` / default class / `{}` | The claim of each pod when persistence is on. |
| `gateway.sharedLimits.redis.existingSecret` / `.existingSecretKey` | unset / `url` | Existing Secret with the full `redis://` or `rediss://` URL; mutually exclusive with `redis.enabled`. |
| `gateway.sharedLimits.syncMs` | `""` | `LUMEWARP_SHARED_LIMITS_SYNC_MS`, 10–1000; empty leaves the gateway default of 100. |
| `gateway.sharedLimits.namespace` | `""` | `LUMEWARP_SHARED_LIMITS_NAMESPACE`: Redis key namespace; empty uses the config scope. Gateways that share counts must agree. |
| `gateway.server.*` | see values | `server:` block of the gateway's local YAML (listeners/timeouts). `adminListen` must be reachable by probes. |
| `gateway.plugins.volume` | `{}` | Existing volume with the WASM components, as a Kubernetes volume source; empty loads no plugins and sets no directory variable. |
| `gateway.plugins.mountPath` / `.dirs` | `/etc/lumewarp/plugins` / `router`, `auth`, `a2a-middleware` | Mount point and the subdirectory each kind reads. |
| `gateway.plugins.memoryLimitBytes` | `""` | `LUMEWARP_PLUGIN_MEMORY_LIMIT_BYTES`; empty = the gateway default, 1 GiB. |
| `gateway.plugins.secretEnvs` | `[]` | Existing Secrets whose keys become `LUMEWARP_PLUGIN_SECRET_<NAME>` for `secret()`. |
| `gateway.publicUrl` | `""` | `LUMEWARP_GATEWAY_PUBLIC_URL`: external base URL for agent cards; empty answers 503 on A2A. |
| `gateway.idKey.secretName` / `.key` | unset / `gateway-id-key` | Existing Secret with `LUMEWARP_GATEWAY_ID_KEY` (base64, ≥32 bytes); shared by every gateway of the installation. |
| `serviceMonitor.enabled` | `false` | Render ServiceMonitors for the gateway admin port and the control-plane metrics port (each only if that component and its metrics are enabled). |
| `serviceMonitor.labels` | `{}` | Extra labels so your Prometheus selects the ServiceMonitor. |
| `redis.enabled` | `false` | Deploy one Valkey in the release for shared rate limits. |
| `redis.image.repository` / `.tag` | `valkey/valkey` / `8.1.11-alpine` | Pinned Valkey image. |
| `redis.auth.existingSecret` / `.existingSecretKey` | unset / `password` | Existing Secret holding the server password (URL-safe, password only); unset means the chart creates one once with a random value. |
| `redis.service.port` | `6379` | Service and container port. |
| `redis.resources` | `30m` CPU / `64Mi` | Requests of the Valkey pod. |

All pods honour `nodeSelector`, `tolerations`, `affinity`, `podSecurityContext`,
`securityContext` and `resources` (per component, the Valkey included). The control plane and
the gateway also take `extraEnv` and `extraArgs`. Global `imagePullSecrets`, `nameOverride`,
`fullnameOverride` and `commonLabels` apply to every object.

## Checking the chart

`deploy/helm/check.sh` lints the chart and renders a matrix of value sets — defaults,
replicas, autoscaling, persistence, the bundled Valkey, an external Redis, and combinations —
asserting the resulting kinds, replica counts, volume claims and environment variables, and
that a rejected combination fails. It uses `helm` from `PATH` or the `alpine/helm` image
through docker:

```sh
bash deploy/helm/check.sh
```

By hand:

```sh
helm lint deploy/helm/lumewarp --strict
helm template lumewarp deploy/helm/lumewarp -n lumewarp \
  --set controlPlane.databaseUrl.secretName=db \
  --set gateway.gatewayId=gw-1 \
  --set gateway.token.secretName=gw

# Expected to fail: the bundled Valkey and an external Redis URL are exclusive.
helm template lumewarp deploy/helm/lumewarp -n lumewarp \
  --set redis.enabled=true \
  --set gateway.sharedLimits.redis.existingSecret=redis-url
```

`kubectl port-forward svc/lumewarp-control-plane 8091:8091` reaches the UI/API; probe the
gateway with `kubectl port-forward svc/lumewarp-gateway 4000:4000` and
`curl localhost:4000/healthz`.
