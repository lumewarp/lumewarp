# Lumewarp Helm chart

Deploys the Lumewarp control plane and Rust gateway. The control plane owns Postgres,
compiles versioned config snapshots and serves the Admin UI; the gateway follows the
config stream, keeps the last ACKed snapshot on a local volume and never calls the
control plane from the request path.

This page covers installing the chart. Everything else is in the manual:
[Deploy with Helm](../../../docs/operations/helm.md) has the full values reference, upstream API
keys, gateway plugins, MCP and A2A, SSO, important limits and verifying a release, and
[Observability](../../../docs/operations/observability.md) the alert rules, dashboards and
request lookup in OTel.

## Prerequisites

- Kubernetes 1.25+ and Helm 3.
- A reachable PostgreSQL 16+ database and a Secret containing its SQLAlchemy async URL
  (`postgresql+asyncpg://user:password@host:5432/db`).
- Container images for both components, built from `gateway/Dockerfile` and
  `control-plane/Dockerfile`, or the published `ghcr.io/lumewarp/lumewarp-gateway` and
  `ghcr.io/lumewarp/lumewarp-control-plane`; set the repositories/tags in `values.yaml`
  (empty tags mean the chart's app version).
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

The gateway runs one replica by default. `gateway.replicaCount` (or `gateway.autoscaling`)
can raise that: every replica uses the same token and the same `gateway.gatewayId`, because a
token identifies one gateway and not one process, and the control plane shows the pods as that
gateway's instances. Rate limits are counted per instance unless the gateways share a Redis;
`redis.enabled` ships one with the release. The manual's
[Gateway replicas](../../../docs/operations/helm.md#gateway-replicas) and
[Shared rate limits](../../../docs/operations/helm.md#shared-rate-limits) have the details.

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
| Gateway StatefulSet | Instead of the Deployment when `gateway.stateDir.persistence.enabled`: one ReadWriteOnce claim per pod from a `volumeClaimTemplate`, plus the headless Service it needs. |
| Gateway Service | `http` (4000) and `admin` (9464, `/metrics` + `/readyz`) ports. |
| Gateway PodDisruptionBudget / HorizontalPodAutoscaler | Optional, `gateway.podDisruptionBudget` and `gateway.autoscaling`. |
| Valkey Deployment, Service and Secret | Only when `redis.enabled`: shared rate limits across gateway replicas, password in a Secret the chart creates once. |
| ServiceMonitors | Optional: one for the gateway admin port, one for the control-plane `metrics` port. Requires the Prometheus Operator CRDs. |

Readiness/liveness:

- Control plane: `/readyz` requires a reachable database, migrations at head and live
  gRPC/compiler/rollover tasks; `/healthz` is liveness.
- Gateway: `/readyz` on the admin port requires a loaded snapshot; `/healthz` stays
  healthy during a control-plane outage so Kubernetes does not restart a gateway that is
  correctly serving its cached config.

## Chart checks

```sh
# Lints and renders every supported value combination (replicas, autoscaling, persistence,
# the bundled Valkey, an external Redis) and checks the result.
bash deploy/helm/check.sh

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
