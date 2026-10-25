# Lumewarp Helm chart

Deploys the Lumewarp control plane and Rust gateway. The control plane owns Postgres,
compiles versioned config snapshots and serves the Admin UI; the gateway follows the
config stream, keeps the last ACKed snapshot on a local volume and never calls the
control plane from the request path.

Design: [`docs/architecture.md`](../../../docs/architecture.md). Stream and boot
semantics: [`docs/config-protocol.md`](../../../docs/config-protocol.md).

## Prerequisites

- Kubernetes 1.25+ and Helm 3.
- A reachable PostgreSQL 16+ database and a Secret containing its SQLAlchemy async URL
  (`postgresql+asyncpg://user:password@host:5432/db`).
- Container images for both components, built from `gateway/Dockerfile` and
  `control-plane/Dockerfile` (no images are published yet); push them to your registry and
  set the repositories/tags in `values.yaml`.
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

The gateway Deployment runs one replica in P1: a token identifies one gateway, so do not
scale it. If you need more gateway instances, deploy another release (normally in a
separate namespace, or with `controlPlane.enabled=false`) with its own `gateway.gatewayId`
and token.

Alternatively set `controlPlane.bootstrapToken.secretName` to an existing Secret holding
`LUMEWARP_BOOTSTRAP_TOKEN` for UI/API superuser access. It does not replace the first
`bootstrap` call: that command also creates the scope row, and gateway tokens can only be
created after the scope exists.

`controlPlane.databaseUrl.secretName` defaults to `lumewarp-control-plane` and
`gateway.token.secretName` to `lumewarp-gateway`; both must exist before the respective
pods start.

## What gets deployed

| Object | Notes |
| --- | --- |
| Control-plane Deployment | One replica. Runs the HTTP API + UI, gRPC ConfigService/UsageService, compiler loop, monthly rollover and notifier loop. `strategy: Recreate`. |
| Control-plane Service | `http` (8091) and `grpc` (50051) ports. |
| Migration Job | Helm `pre-install,pre-upgrade` hook running `python -m lumewarp_control_plane migrate` with the database Secret. |
| Gateway Deployment | One replica. The token is bound to `gateway.gatewayId`; boots from its state volume, then follows the config stream; `/readyz` stays 503 until a snapshot is loaded. |
| Gateway Service | `http` (4000) and `admin` (9464, `/metrics` + `/readyz`) ports. |
| Gateway state PVC | Only when `gateway.stateDir.persistence.enabled=true`; otherwise an `emptyDir` per pod. |
| ServiceMonitor | Optional, gateway admin port only. Requires the Prometheus Operator CRDs. |

Readiness/liveness:

- Control plane: `/readyz` requires a reachable database, migrations at head and live
  gRPC/compiler/rollover/notifier tasks; `/healthz` is liveness.
- Gateway: `/readyz` on the admin port requires a loaded snapshot; `/healthz` stays
  healthy during a control-plane outage so Kubernetes does not restart a gateway that is
  correctly serving its cached config.

## Important P1 limits

- **Control plane replicas must stay at 1.** The compiler, rollover scheduler and
  notifier loop are single-process; the chart uses `Recreate`.
- **Migration is a pre-upgrade hook.** The old control-plane pod is still running while
  the schema migrates (Rolling/Recreate), so expect a short outage window on upgrades.
- **One gateway replica and one token per gateway id.** P1 binds each gateway token to a
  single `gateway_id`. `gateway.replicaCount` must stay 1; the chart rejects higher
  values. `gateway.gatewayId` must equal the id used when the token was issued. Deploy one
  release per gateway identity if you need more gateway instances.
- **Gateway state volume.** With the default `emptyDir`, a new pod has no cache and returns
  503 on `/readyz` and `/v1/*` until the first full snapshot arrives. A chart-created PVC
  must be `ReadWriteOnce` (other access modes are rejected), and an `existingClaim` must
  also be RWO and used only by this single replica. The chart runs the gateway as
  UID/GID 65532 with `fsGroup: 65532`, so both emptyDir and PVC mounts are writable; no
  manual `fsGroup` is needed. Override `gateway.podSecurityContext` only for a custom
  image with another UID/GID.
- **gRPC is plaintext by default.** Set `controlPlane.grpcTls.enabled` to terminate TLS on
  the control-plane listener, but note that the gateway verifies with the WebPKI root
  store only; a private CA must be added to the gateway image. Inside a trusted cluster,
  plaintext plus NetworkPolicy is the simple option.
- **Keep gRPC keepalive pings intact.** The gateway pings the config stream every 30 s.
  If an L7 proxy or mesh sidecar (Envoy, ingress) sits between gateway and control plane,
  allow pings without data at least every 10 s, or it will reset the stream with
  `too_many_pings` (see `docs/config-protocol.md`).
- **Budgets are eventual.** A key can exceed its budget briefly while usage batches are in
  flight; in-flight streams are not cut. Usage events are dropped when the gateway's
  bounded queue is full.
- **The control plane has no Prometheus endpoint in P1.** `serviceMonitor` scrapes only
  the gateway.

## Values

Common values (see [`values.yaml`](values.yaml) for the complete list):

| Value | Default | Meaning |
| --- | --- | --- |
| `controlPlane.enabled` | `true` | Deploy the control plane. |
| `controlPlane.replicaCount` | `1` | Must stay 1 in P1. |
| `controlPlane.image.repository` / `.tag` | `ghcr.io/lumewarp/lumewarp-control-plane` / chart appVersion | Control-plane image. |
| `controlPlane.databaseUrl.secretName` / `.key` | `lumewarp-control-plane` / `database-url` | Existing Secret with `LUMEWARP_DATABASE_URL`. |
| `controlPlane.bootstrapToken.secretName` / `.key` | unset / `bootstrap-token` | Optional `LUMEWARP_BOOTSTRAP_TOKEN`. |
| `controlPlane.scope` | `default` | Config scope published to gateways. |
| `controlPlane.service.httpPort` / `.grpcPort` | `8091` / `50051` | Service and container ports. |
| `controlPlane.grpcTls.enabled` | `false` | Serve gateway gRPC over TLS from a Secret. |
| `controlPlane.migrate.enabled` | `true` | Run the migration hook Job. |
| `gateway.enabled` | `true` | Deploy the gateway. |
| `gateway.replicaCount` | `1` | Must stay 1 in P1; one token per gateway id. |
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
| `serviceMonitor.enabled` | `false` | Render a ServiceMonitor for the gateway admin port. |
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
# deferred in P1.
helm template lumewarp deploy/helm/lumewarp -n lumewarp \
  --set gateway.replicaCount=2
```

`kubectl port-forward svc/lumewarp-control-plane 8091:8091` reaches the UI/API; probe the
gateway with `kubectl port-forward svc/lumewarp-gateway 4000:4000` and
`curl localhost:4000/healthz`.
