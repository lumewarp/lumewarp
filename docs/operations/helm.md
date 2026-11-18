# Deploy with Helm

The chart in [`deploy/helm/lumewarp`](https://github.com/lumewarp/lumewarp/tree/main/deploy/helm/lumewarp)
deploys the control plane and a gateway (one replica or many) on Kubernetes. Each release
also publishes it as `oci://ghcr.io/lumewarp/charts/lumewarp`, versioned like the images.
For one host without Kubernetes, see [Deploy with Docker](./docker.md); for a cluster
without Helm, [Deploy with plain manifests](./kubernetes.md); for a database and OpenFGA
in the same install, [Full stack: lumewarp-stack](#full-stack-lumewarp-stack). For what the
parts are, see [Deploy](./index.md).

## Prerequisites

- Kubernetes 1.25+ and Helm 3.
- A reachable PostgreSQL 16+ database and a Secret containing its SQLAlchemy async URL
  (`postgresql+asyncpg://user:password@host:5432/db`).
- Container images for both components: the published ones (the default; an empty tag
  means the chart's app version) or your own built from `gateway/Dockerfile` and
  `control-plane/Dockerfile` ([Building images](./index.md#building-images)); set the
  repositories and tags in `values.yaml` (`global.imageTag` sets both tags at once).
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

The control plane creates the config zone at startup and lets you create gateway tokens,
so a fresh install has no gateway token yet. A token is a zone credential with a label, not
bound to a gateway id; any number of gateway pods may share it. Install the control plane
first, create a token, then enable the gateway:

```sh
helm install lumewarp "$CHART" --version "$VERSION" -n lumewarp \
  --set gateway.enabled=false

# Wait for the pre-install migrate hook and the control plane.
kubectl -n lumewarp rollout status deploy/lumewarp-control-plane

# In another terminal: open http://127.0.0.1:8091, sign in, then Gateways ->
# New gateway token and copy the lwg-... secret.
kubectl -n lumewarp port-forward svc/lumewarp-control-plane 8091:8091

# Scripted alternative without the UI:
kubectl -n lumewarp exec deploy/lumewarp-control-plane -- \
  python -m lumewarp_control_plane bootstrap --name lumewarp-gateway

kubectl -n lumewarp create secret generic lumewarp-gateway \
  --from-literal=token='lwg-...'

helm upgrade lumewarp "$CHART" --version "$VERSION" -n lumewarp \
  --reuse-values --set gateway.enabled=true
```

The gateway runs one replica by default. `gateway.replicaCount` raises that: every replica
uses the same token and is its own gateway, with an id generated at startup, so every pod
loads the snapshot and serves the same models ([Gateway replicas](#gateway-replicas)).

Installation admins are stored in Postgres: the first verified login whose email matches
`controlPlane.initialAdminEmail` becomes the proxy_admin, who can then assign other admins (and
every admin can assign more). Break-glass can create the first proxy_admin too when the setting
is empty, and is the recovery path when the proxy_admin leaves. A user must sign in through OIDC
once before receiving an installation role (assign/transfer return 409 otherwise), because a new
identity cannot be linked to an already privileged account. Set
`controlPlane.breakGlass.passwordHashSecret.name` to an existing Secret holding an argon2id hash
(`lumewarp-control-plane hash-password`). `bootstrap` is not a required first step: `serve`
creates the zone row at startup, and `bootstrap` only creates a gateway token without the UI
(for scripted installs).

`controlPlane.databaseUrl.secretName` defaults to `lumewarp-control-plane` and
`gateway.token.secretName` to `lumewarp-gateway`; both must exist before the respective
pods start. `global.zone` (default `default`) is the config zone the control plane
publishes and the gateway follows.

## What gets deployed

| Object | Notes |
| --- | --- |
| Control-plane Deployment | One replica. Runs the HTTP API + UI, gRPC ConfigService/UsageService, compiler loop and monthly rollover. `strategy: Recreate`. |
| Control-plane Service | `http` (8091), `grpc` (50051) and `metrics` (9465, Prometheus `/metrics`; only with `controlPlane.metrics.enabled`) ports. |
| Migration Job | Helm `pre-install,pre-upgrade` hook running `python -m lumewarp_control_plane migrate` with the database Secret. |
| Gateway Deployment | One replica by default. Each pod is a gateway with an id generated at startup; all pods share the token. Boots from its state volume, then follows the config stream; `/readyz` stays 503 until a snapshot is loaded. |
| Gateway StatefulSet | Instead of the Deployment when `gateway.persistence.enabled`: same pod, one ReadWriteOnce claim per pod from a `volumeClaimTemplate`. |
| Gateway Service | `http` (4000) and `admin` (9464, `/metrics` + `/readyz`) ports. |
| Gateway headless Service | Only with persistence: the governing Service of the StatefulSet (`clusterIP: None`). |
| Gateway PodDisruptionBudget / HorizontalPodAutoscaler | Optional, `gateway.podDisruptionBudget` and `gateway.autoscaling`; see [Gateway replicas](#gateway-replicas). |
| Valkey Deployment, Service and Secret | Only when `valkey.enabled`: shared rate limits across gateways; see [Shared rate limits](#shared-rate-limits). |
| ServiceMonitors, PrometheusRule, Grafana dashboards | Optional, `monitoring.*`: ServiceMonitors for the gateway admin port and the control-plane `metrics` port, the alert rules, and a ConfigMap with the dashboards. The first two require the Prometheus Operator CRDs; see [Observability](./observability.md#alert-rules-and-grafana-dashboards). |

Readiness/liveness:

- Control plane: `/readyz` requires a reachable database, migrations at head and live
  gRPC/compiler/rollover tasks; `/healthz` is liveness.
- Gateway: `/readyz` on the admin port requires a loaded snapshot; `/healthz` stays
  healthy during a control-plane outage so Kubernetes does not restart a gateway that is
  correctly serving its cached config.

## Gateway replicas

`gateway.replicaCount` sets how many gateway processes serve the deployment; with
autoscaling the HorizontalPodAutoscaler chooses the number. Every pod is its own gateway:
it generates `<hostname>-<8 hex>` at startup and sends it as `Hello.gateway_id`, so the
Admin UI's Gateways page has one row per pod, connected first. All pods share the token in
`gateway.token.secretName` and keep their own generated id. A restart or a new pod is a new
gateway, and the old row stays listed for 24 hours after it disconnects. The
Service load balances across the live pods, and a rolling update adds new gateways instead
of reusing an identity.

Three consequences:

- **Rate limits count per gateway by default.** Without a shared Redis a key's rpm, tpm and
  max_parallel apply to each pod, so three gateways admit up to three times the limit.
  Configure [shared rate limits](#shared-rate-limits) to count them once.
- **A fresh pod may be unready.** With the default state directory a new pod has no cached
  snapshot and `/readyz` answers 503 until the control plane sends one, while the other
  gateways keep serving.
- **Rejections differ in blast radius.** A gateway that NACKs `INVALID_CONFIG` quarantines the
  version for the whole zone, including gateways that start later; one that NACKs
  `UNSUPPORTED_FEATURE` skips only that version, while the others apply it. A gateway that
  already applied a version keeps it until the next one. During a rolling upgrade the
  old-build pods can keep an older config and NACK every version that needs the new build,
  so finish the rollout before publishing such a config.

### State volume

`gateway.persistence.enabled=false` (the default) renders the gateway as a Deployment
whose state directory (`/var/lib/lumewarp`) is a per-pod `emptyDir`. Setting it to `true` renders a StatefulSet with
a `volumeClaimTemplate` instead: one ReadWriteOnce claim per pod, `size` and `storageClass`
from the values, created by the chart, so every pod keeps its cached snapshot across
restarts. The StatefulSet needs a governing headless Service (`lumewarp-gateway-headless`),
which the chart adds and which exposes `http` only, so Prometheus does not discover every pod a
second time; clients keep calling the regular Service. There is no `existingClaim` option: one claim cannot serve several pods.

Kubernetes treats a StatefulSet's claim template as immutable: `gateway.persistence.size`,
`.storageClass` and `.annotations` are fixed once the StatefulSet exists, and changing them makes
the next `helm upgrade` fail with `updates to statefulset spec ... are forbidden`. To change one,
delete the StatefulSet while leaving its pods and claims alone, then upgrade:

```sh
kubectl -n lumewarp delete statefulset lumewarp-gateway --cascade=orphan
helm upgrade lumewarp "$CHART" --version "$VERSION" -n lumewarp \
  --reuse-values --set gateway.persistence.size=5Gi
```

The upgrade recreates the StatefulSet with the new template and adopts the running pods.
Existing claims keep their size — edit each one if the StorageClass allows expansion
(`state-lumewarp-gateway-<ordinal>`), or delete it and let its pod recreate it — and only
newly created pods get the new template.

Switching persistence on a live release changes the kind: Helm deletes the object it no longer
renders (the Deployment when persistence is turned on, the StatefulSet and its headless Service
when it is turned off) and creates the other, so the pods restart once. Claims created from the
`volumeClaimTemplate` are not part of the release and stay behind; delete them when they are no
longer needed. A pod removed from a StatefulSet — scaled down, or in the middle of a rollout that shrinks
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
gateway unless the gateways share a Redis, so several gateway pods need one to honour a
key's limit once. A gateway keeps serving when Redis is unreachable; it falls back to
counting for itself, which makes the limits per gateway again until the sync recovers. The
`lumewarp_shared_limits_*` metrics and the `LumewarpSharedLimitsFallback` alert show both
([Observability](./observability.md)).

### Bundled Valkey

`valkey.enabled=true` adds one Valkey (Redis protocol, BSD-licensed) to the release: a
ClusterIP Service on 6379, a Deployment of a pinned image, and a Secret with its password. It
keeps nothing on disk — every key the limiter writes expires within 150 s, so a restart only
costs a short fallback window — runs as the image's non-root user with a read-only root
filesystem, and answers `ping` for its probes. The gateway receives
`LUMEWARP_SHARED_LIMITS_REDIS_URL` built from that Service and the password, interpolated by
the kubelet from a `secretKeyRef`, so the password never appears in a manifest.

With `valkey.auth.existingSecret` unset the chart creates the password Secret itself, once,
with a random 32-character password, and reuses it on every upgrade (`lookup`). `helm
template` has no cluster to ask and renders a new one each time. A password supplied through
`valkey.auth.existingSecret` (or `gateway.sharedLimits.redis.existingSecret`) must be
URL-safe (no `@ : / % # ?`), because the chart interpolates it into the Redis URL. The
Secret's key holds the password alone, without a URL around it; `valkey.auth.existingSecretKey`
names the key (default `password`).

Valkey writes the password to a config file on its ephemeral `/data` volume at startup and runs
`valkey-server` from it, so the password is never an argument of the server process. The pods
read it once, at startup: after rotating the password Secret, restart the Valkey pod and the
gateway pods (`kubectl rollout restart deploy/<release>-lumewarp-valkey` and
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
    namespace: default   # optional; the config zone by default
```

`valkey.enabled` and `gateway.sharedLimits.redis.existingSecret` are mutually exclusive: a
release with both fails to render.

### Several releases on one Redis

One release can hold the gateway replicas and their Valkey; a gateway-only release elsewhere
(another cluster, `controlPlane.enabled=false`, `gateway.controlPlaneUrl` set) points at the
same Redis with `gateway.sharedLimits.redis.existingSecret`. Every gateway that should share
counts needs the same Redis and the same `gateway.sharedLimits.namespace`, which is the
config zone unless it is set. Router state — health, circuit breakers, latency — stays local
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
global:
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
mounts the volume you give it at `/etc/lumewarp/plugins` and gives each kind a fixed
subdirectory:

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

Deploy the component to every gateway before a router, MCP server, A2A agent or LLM upstream names it. A
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
[Control plane](./control-plane.md)). Sign-in through an OIDC provider has its own values,
which the chart turns into `LUMEWARP_OIDC_ISSUER`, `LUMEWARP_OIDC_CLIENT_ID`,
`LUMEWARP_OIDC_CLIENT_SECRET`, `LUMEWARP_SECRET_KEY` and `LUMEWARP_PUBLIC_URL`; secrets
come from existing Secrets, never from `values.yaml`:

```sh
kubectl -n lumewarp create secret generic lumewarp-oidc \
  --from-literal=client-secret='...' \
  --from-literal=secret-key="$(openssl rand -base64 48)"
```

```yaml
controlPlane:
  oidc:
    issuer: https://keycloak.example.com/realms/lumewarp
    clientId: lumewarp-cp
    clientSecret:            # leave secretName empty for a public PKCE client
      secretName: lumewarp-oidc
      key: client-secret
    secretKey:               # required with an issuer: 32+ random characters
      secretName: lumewarp-oidc
      key: secret-key
    publicUrl: https://lumewarp.example.com
```

`secretKey` seals the OIDC login-state cookie (nonce and PKCE verifier); rotating it
invalidates pending logins only. A release with an `issuer` and no `secretKey.secretName`
fails to render, because the control plane refuses to start without the key. The variables
are rendered only when `issuer` is set. The IdP must allow the redirect URI
`<publicUrl>/api/v1/session/oidc/callback`. Settings the chart does not model, such as
`LUMEWARP_OIDC_SCOPES` and `LUMEWARP_OIDC_JIT_USERS`, go through `controlPlane.extraEnv`:

```yaml
controlPlane:
  extraEnv:
    - {name: LUMEWARP_OIDC_SCOPES, value: "openid email profile"}
```

The authorization backend has its own values. For [OpenFGA](./identity.md#openfga):

```yaml
controlPlane:
  authz:
    backend: openfga
    openfga:
      url: http://openfga.openfga:8080
      store: lumewarp          # by name; or storeId
      init: true               # init container: create the store and model when missing
      # Optional:
      # modelId: 01H...        # pin a model; empty = the store's latest
      # apiTokenSecret: {name: lumewarp-openfga, key: api-token}
      # objectTypes: {team: llm_team, org: llm_org}
      # relations: {team.admin: owner}
```

With `init: true` the pod waits until OpenFGA answers; an existing model is never replaced.
The `http` backend (`LUMEWARP_AUTHZ_HTTP_URL`, `LUMEWARP_AUTHZ_HTTP_TOKEN` and the optional
`LUMEWARP_AUTHZ_HTTP_LIST_URL` and `LUMEWARP_AUTHZ_HTTP_CHANGES_URL`) and plugins take their
settings through `extraEnv`, with
`controlPlane.authz.backend` naming them. Lumewarp answers direct `admin` (org/team) and
`member` (team) relations only; org administration is inherited from `team.org_id` by the
control plane.

## Values

[`values.yaml`](https://github.com/lumewarp/lumewarp/blob/main/deploy/helm/lumewarp/values.yaml)
carries a comment on every key; this table lists the ones you are likely to change. Every
component also takes `podAnnotations`, `podSecurityContext`, `securityContext`, `resources`,
`nodeSelector`, `tolerations` and `affinity` (the Valkey included), and the control plane and
the gateway take `extraEnv`. `nameOverride` and `fullnameOverride` are top-level.

| Value | Default | Meaning |
| --- | --- | --- |
| `global.imageTag` | empty | Tag of both images; empty means the chart's app version, and a component's own `image.tag` wins. |
| `global.imagePullSecrets` | `[]` | Pull secrets for every pod. |
| `global.commonLabels` | `{}` | Extra labels on every object. |
| `global.zone` | `default` | Config zone the control plane publishes and the gateway follows. |
| `global.encryptionKey.secretName` / `.key` | empty / `encryption-key` | Existing Secret with `LUMEWARP_ENCRYPTION_KEY` (32+ characters), given to both components; required once an upstream stores a key. |
| `controlPlane.enabled` | `true` | Deploy the control plane; always one pod. |
| `controlPlane.image.repository` / `.tag` | `ghcr.io/lumewarp/lumewarp-control-plane` / `global.imageTag` | Control-plane image. |
| `controlPlane.databaseUrl.secretName` / `.key` | `lumewarp-control-plane` / `database-url` | Existing Secret with `LUMEWARP_DATABASE_URL`. |
| `controlPlane.initialAdminEmail` | `""` | `LUMEWARP_INITIAL_ADMIN_EMAIL`: the email that becomes proxy_admin on its first verified login while no proxy_admin exists. Empty leaves emergency access only. |
| `controlPlane.breakGlass.passwordHashSecret.name` / `.key` | empty / `password-hash` | Existing Secret with `LUMEWARP_BREAK_GLASS_PASSWORD_HASH` (argon2id). Empty disables the emergency login. |
| `controlPlane.oidc.issuer` / `.clientId` / `.publicUrl` | empty | OIDC sign-in; the variables render only with an `issuer`. See [SSO](#sso-and-authorization-backend). |
| `controlPlane.oidc.clientSecret.secretName` / `.key` | empty / `client-secret` | Existing Secret with the client secret; empty for a public PKCE client. |
| `controlPlane.oidc.secretKey.secretName` / `.key` | empty / `secret-key` | Existing Secret with `LUMEWARP_SECRET_KEY`; required with an `issuer`. |
| `controlPlane.oidc.requireEmailVerified` | `true` | `LUMEWARP_OIDC_REQUIRE_EMAIL_VERIFIED`: require `email_verified` before an OIDC email links or creates a user; `false` trusts the claim. See [Sign-in and authorization](./identity.md#sso-login-oidc). |
| `controlPlane.authz.backend` | `builtin` | `LUMEWARP_AUTHZ_BACKEND`: `builtin`, `openfga`, `http` or a plugin name. |
| `controlPlane.authz.openfga.url` / `.store` / `.storeId` / `.modelId` / `.init` | empty / `lumewarp` / empty / empty / `false` | OpenFGA backend; needs `url` and a `store` or `storeId`. |
| `controlPlane.authz.openfga.apiTokenSecret.name` / `.key` | empty / `api-token` | Existing Secret with OpenFGA's bearer token. |
| `controlPlane.authz.openfga.objectTypes` / `.relations` | `{}` | Names that differ from Lumewarp's in an existing store. |
| `controlPlane.service.type` / `.httpPort` / `.grpcPort` / `.metricsPort` | `ClusterIP` / `8091` / `50051` / `9465` | Control-plane Service. |
| `controlPlane.metrics.enabled` | `true` | Serve `lumewarp_cp_*` metrics on `metricsPort`; off leaves the port unexposed. |
| `controlPlane.grpcTls.enabled` / `.secretName` / `.certKey` / `.keyKey` | `false` / empty / `tls.crt` / `tls.key` | TLS for the gateway-facing gRPC listener from one Secret; the gateway verifies with the WebPKI roots only. |
| `controlPlane.migrate.enabled` / `.backoffLimit` | `true` / `3` | The `pre-install,pre-upgrade` migration Job. |
| `controlPlane.terminationGracePeriodSeconds` | `20` | Pod grace period. |
| `gateway.enabled` | `true` | Deploy the gateway; set `false` for the first install, before a token exists. |
| `gateway.replicaCount` | `1` | Gateway pods; every pod is its own gateway. |
| `gateway.autoscaling.enabled` / `.minReplicas` / `.maxReplicas` / `.targetCPUUtilizationPercentage` | `false` / `2` / `5` / `80` | HorizontalPodAutoscaler; replaces `replicaCount`. |
| `gateway.podDisruptionBudget.enabled` / `.minAvailable` / `.maxUnavailable` | `false` / `1` / empty | Set one of the two; rendered only when the gateway can have more than one pod. |
| `gateway.image.repository` / `.tag` | `ghcr.io/lumewarp/lumewarp-gateway` / `global.imageTag` | Gateway image. |
| `gateway.token.secretName` / `.key` | `lumewarp-gateway` / `token` | Existing Secret with a control-plane-created gateway token, passed as `LUMEWARP_GATEWAY_TOKEN`; any number of gateways may use it. |
| `gateway.controlPlaneUrl` | empty | Defaults to this release's control-plane gRPC Service; required with `controlPlane.enabled=false`. |
| `gateway.server.shutdownGraceMs` / `.headerReadTimeoutMs` / `.bodyReadTimeoutMs` | `25000` / `30000` / `30000` | Listener timeouts; the listen addresses follow `service.port` and `service.adminPort`. |
| `gateway.persistence.enabled` | `false` | `false` = Deployment with a per-pod `emptyDir`; `true` = StatefulSet with one ReadWriteOnce claim per pod. |
| `gateway.persistence.size` / `.storageClass` / `.annotations` | `1Gi` / default class / `{}` | The claim of each pod; fixed once the StatefulSet exists. |
| `gateway.plugins.volume` | `{}` | Existing volume source mounted at `/etc/lumewarp/plugins`; empty loads no plugins. |
| `gateway.plugins.memoryLimitBytes` | `""` | `LUMEWARP_PLUGIN_MEMORY_LIMIT_BYTES`; empty = the gateway default, 1 GiB. |
| `gateway.plugins.secretEnvs` | `[]` | Existing Secrets whose keys become `LUMEWARP_PLUGIN_SECRET_<NAME>` for `secret()`. |
| `gateway.publicUrl` | `""` | `LUMEWARP_GATEWAY_PUBLIC_URL`: external base URL for agent cards; empty answers 503 on A2A. |
| `gateway.idKey.secretName` / `.key` | empty / `gateway-id-key` | Existing Secret with `LUMEWARP_GATEWAY_ID_KEY` (base64, 32+ bytes), identical on every gateway; empty answers 503 on MCP and A2A. |
| `gateway.sharedLimits.redis.existingSecret` / `.existingSecretKey` | empty / `url` | Existing Secret with the full `redis://` or `rediss://` URL; exclusive with `valkey.enabled`. |
| `gateway.sharedLimits.syncMs` / `.namespace` | `""` | `LUMEWARP_SHARED_LIMITS_SYNC_MS` (10–1000; empty = 100) and the Redis key namespace (empty = the zone; gateways that share counts must agree). |
| `gateway.service.type` / `.port` / `.adminPort` | `ClusterIP` / `4000` / `9464` | Gateway Service; the admin port serves `/metrics` and `/readyz`. |
| `gateway.otel.endpoint` / `.samplingRatio` | empty / `1` | OTLP/HTTP collector base URL (empty = no traces) and the share of requests exported, 0 to 1. |
| `gateway.terminationGracePeriodSeconds` | `35` | Must exceed `server.shutdownGraceMs` / 1000 + 5 s + margin. |
| `gateway.podSecurityContext` | UID/GID/fsGroup `65532`, `runAsNonRoot: true` | Matches the gateway image so the state volume is writable; override only for a custom image with another UID/GID. |
| `valkey.enabled` | `false` | Deploy one Valkey in the release for shared rate limits. |
| `valkey.image.repository` / `.tag` | `valkey/valkey` / `8.1.11-alpine` | Pinned Valkey image. |
| `valkey.auth.existingSecret` / `.existingSecretKey` | empty / `password` | Existing Secret holding the password alone (URL-safe); empty means the chart creates one once with a random value. |
| `valkey.service.port` | `6379` | Service and container port. |
| `monitoring.serviceMonitor.enabled` / `.interval` / `.scrapeTimeout` / `.labels` | `false` / `30s` / empty / `{}` | ServiceMonitors for the gateway admin port and the control-plane metrics port; `labels` lets your Prometheus select them. |
| `monitoring.prometheusRule.enabled` / `.labels` | `false` / `{}` | PrometheusRule with the alerts; `labels` must match your `ruleSelector`. |
| `monitoring.grafanaDashboards.enabled` / `.sidecarLabel` / `.sidecarLabelValue` | `false` / `grafana_dashboard` / `"1"` | ConfigMap with the dashboards for the Grafana sidecar. |

## Full stack: lumewarp-stack

[`deploy/helm/lumewarp-stack`](https://github.com/lumewarp/lumewarp/tree/main/deploy/helm/lumewarp-stack)
is an umbrella chart for an install that should work from one command. It depends on the
`lumewarp` chart of the same version and adds the pieces Lumewarp needs but does not ship:

- **A PostgreSQL database** (`postgres.enabled`, on by default): one pod with one
  PersistentVolumeClaim (`postgres.persistence.size`, default 10Gi) and a generated password
  kept across upgrades, wired into the `lumewarp-stack-database` Secret the control plane
  reads. Set `postgres.auth.existingSecret` to supply the password yourself.
- **OpenFGA** (`openfga.enabled`, off by default): the official OpenFGA chart, using a
  second database (`openfga`) on the bundled PostgreSQL, with the control plane's
  `authz.openfga.url` pointing at it and `init: true`, so the store and model are created at
  startup.

The stack's `global.*` values are the `lumewarp` chart's (`global.imageTag`, `global.zone`,
`global.encryptionKey`, ...); everything else of the `lumewarp` chart goes under a
`lumewarp:` key, for example `lumewarp.gateway.replicaCount`. The stack is not published to
the registry: install it from a checkout of the release tag, after fetching its dependencies
(the `lumewarp` chart beside it and the OpenFGA chart from its repository):

```sh
git clone --depth 1 --branch "v$VERSION" https://github.com/lumewarp/lumewarp.git
cd lumewarp
helm repo add openfga https://openfga.github.io/helm-charts
helm dependency build deploy/helm/lumewarp-stack

kubectl create namespace lumewarp
kubectl -n lumewarp create secret generic lumewarp-encryption \
  --from-literal=encryption-key="$(openssl rand -base64 48)"

helm install lumewarp deploy/helm/lumewarp-stack \
  -n lumewarp \
  --set global.encryptionKey.secretName=lumewarp-encryption \
  --set lumewarp.gateway.enabled=false
```

The `lumewarp` chart's own migration hook cannot run before the bundled database exists, so
the stack runs `migrate` as an ordinary Job that waits for PostgreSQL (`migrate.enabled`).
Creating the gateway token is the same step as in [Install](#install): the control plane has
to be running, so the first install keeps the gateway off, then you create the token in the
UI (or with `bootstrap`), create the `lumewarp-gateway` Secret and upgrade with
`--reuse-values --set lumewarp.gateway.enabled=true`.

### Bundled PostgreSQL caveats

The bundled database is for evaluation and small installs: a single pod, no replication,
no backups, no tuning. Everything Lumewarp stores, including key hashes and the audit trail,
lives in it, and deleting the release's claim deletes the data. For production point the
stack at a database you operate: create the Secret the control plane reads, name it, and
turn the bundled one off:

```sh
kubectl -n lumewarp create secret generic lumewarp-control-plane \
  --from-literal=database-url='postgresql+asyncpg://lumewarp:secret@postgres.example:5432/lumewarp'

helm install lumewarp deploy/helm/lumewarp-stack \
  -n lumewarp \
  --set postgres.enabled=false \
  --set lumewarp.controlPlane.databaseUrl.secretName=lumewarp-control-plane \
  --set global.encryptionKey.secretName=lumewarp-encryption \
  --set lumewarp.gateway.enabled=false
```

### Enabling OpenFGA

```sh
helm upgrade lumewarp deploy/helm/lumewarp-stack \
  -n lumewarp --reuse-values \
  --set openfga.enabled=true \
  --set lumewarp.controlPlane.authz.backend=openfga
```

The URL (`http://lumewarp-openfga:8080`), the init container and the datastore are filled in
for the bundled PostgreSQL. With `postgres.enabled=false`, OpenFGA needs its own datastore:
set `openfga.datastore.uriSecret` to a Secret whose key `uri` holds a `postgres://` URL. See
[Sign-in and authorization](./identity.md#openfga) for what the backend answers.

## Checking the chart

`deploy/helm/check.sh` lints the chart and renders a matrix of value sets — defaults,
replicas, autoscaling, persistence, the bundled Valkey, an external Redis, and combinations —
asserting the resulting kinds, replica counts, volume claims and environment variables, and
that a rejected combination fails. It uses `helm` from `PATH` or the `alpine/helm` image
through docker:

```sh
bash deploy/helm/check.sh
bash deploy/helm/lumewarp-stack/check.sh   # the full-stack chart (fetches the OpenFGA chart)
```

By hand:

```sh
helm lint deploy/helm/lumewarp --strict
helm template lumewarp deploy/helm/lumewarp -n lumewarp \
  --set controlPlane.databaseUrl.secretName=db \
  --set gateway.token.secretName=gw

# Expected to fail: the bundled Valkey and an external Redis URL are exclusive.
helm template lumewarp deploy/helm/lumewarp -n lumewarp \
  --set valkey.enabled=true \
  --set gateway.sharedLimits.redis.existingSecret=redis-url
```

`kubectl port-forward svc/lumewarp-control-plane 8091:8091` reaches the UI/API; probe the
gateway with `kubectl port-forward svc/lumewarp-gateway 4000:4000` and
`curl localhost:4000/healthz`.
