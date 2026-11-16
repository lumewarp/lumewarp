# Deploy with plain manifests

For a cluster without Helm, Lumewarp ships Kubernetes manifests: a kustomize base in
[`deploy/kubernetes`](https://github.com/lumewarp/lumewarp/tree/main/deploy/kubernetes) and a
single `lumewarp-<version>.yaml` file attached to every GitHub Release. Both are generated
from the [Helm chart](./helm.md) with its default shape, so they run the same control plane
and gateway; the chart stays the place for options such as autoscaling, persistence or
monitoring. For what the parts are, see [Deploy](./index.md).

## What the manifests contain

Everything lives in the `lumewarp` namespace, and image tags are the release version.

| Object | Notes |
| --- | --- |
| Namespace `lumewarp` | Created by the manifests. |
| `lumewarp-control-plane` Deployment and Service | One pod (`Recreate`): HTTP API and UI on 8091, gRPC on 50051, metrics on 9465. |
| `lumewarp-control-plane-migrate` Job | Runs `migrate` against the database. |
| `lumewarp-gateway` Deployment, Service and ConfigMap | One pod; `http` on 4000 and `admin` (`/metrics`, `/readyz`) on 9464. State is an `emptyDir`. |

The base has no Valkey, no persistent gateway state and no monitoring objects; see
[What is not included](#what-is-not-included).

## Prerequisites

- Kubernetes 1.25+ and `kubectl` (kustomize is built in).
- A reachable PostgreSQL 16+ database.

## Create the Secrets

The manifests reference Secrets by name and never contain a credential. Create two before
applying; the namespace comes from the manifests, so create it first:

```sh
kubectl create namespace lumewarp

kubectl -n lumewarp create secret generic lumewarp-control-plane \
  --from-literal=database-url='postgresql+asyncpg://lumewarp:secret@postgres.example:5432/lumewarp'

kubectl -n lumewarp create secret generic lumewarp-encryption-key \
  --from-literal=encryption-key="$(openssl rand -base64 48)"
```

| Secret | Key | Content |
| --- | --- | --- |
| `lumewarp-control-plane` | `database-url` | The SQLAlchemy async URL of the database. |
| `lumewarp-encryption-key` | `encryption-key` | `LUMEWARP_ENCRYPTION_KEY`, 32+ characters, given to both components; it seals the upstream API keys stored in the Admin UI, and changing it makes them unreadable ([Upstream API keys](./helm.md#upstream-api-keys)). |
| `lumewarp-gateway` | `token` | A gateway token, created after the control plane runs (below). |

## Apply and bootstrap

The control plane creates gateway tokens itself, so a fresh install has none. A token is a
scope credential, not bound to a gateway; any number of gateway pods may share it.

Apply the release asset, or the base from a checkout:

```sh
VERSION=0.2.0
kubectl apply -f "https://github.com/lumewarp/lumewarp/releases/download/v${VERSION}/lumewarp-${VERSION}.yaml"

# or, from a checkout
kubectl apply -k deploy/kubernetes/base
```

Then:

1. The migration Job runs `migrate`. The control plane reports `/readyz` only once the
   database is at the migration head, so it may start first and stays unready until the Job
   has finished. Wait for it with `kubectl -n lumewarp wait --for=condition=complete
   job/lumewarp-control-plane-migrate` and for the control plane with `kubectl -n lumewarp
   rollout status deploy/lumewarp-control-plane`.
2. Create a gateway token:

   ```sh
   kubectl -n lumewarp exec deploy/lumewarp-control-plane -- \
     python -m lumewarp_control_plane bootstrap --name lumewarp-gateway
   ```

3. Store it in the Secret the gateway reads:

   ```sh
   kubectl -n lumewarp create secret generic lumewarp-gateway \
     --from-literal=token='lwg-...'
   ```

The gateway pod references that Secret, so until it exists the pod waits with
`CreateContainerConfigError`; Kubernetes starts it once the Secret is there. If it still
waits, `kubectl -n lumewarp rollout restart deploy/lumewarp-gateway`. `/readyz` on the admin
port turns 200 when the gateway has loaded a snapshot.

Installation admins, first login and the break-glass password work as in
[Deploy with Helm](./helm.md#install): the first verified login whose email matches
`LUMEWARP_INITIAL_ADMIN_EMAIL` becomes the proxy_admin, so add that variable (and the
settings of [Control plane settings](./control-plane.md)) through an overlay. Reach the UI
with `kubectl -n lumewarp port-forward svc/lumewarp-control-plane 8091:8091`.

## Customize with an overlay

Do not edit the base; copy
[`overlays/example`](https://github.com/lumewarp/lumewarp/tree/main/deploy/kubernetes/overlays/example),
point its `resources` at the base (a path, or a remote reference to this repository) and
change it. The example shows three things:

- **Image tag.** `images:` pins both images; `kustomize edit set image` does the same from a
  script. Use your own repositories here for custom images.
- **Gateway replicas.** A JSON patch on `/spec/replicas` of the gateway Deployment. Every pod
  is its own gateway and all share the token, but rpm, tpm and max_parallel are counted per
  gateway unless they share a Redis ([Shared rate limits](./helm.md#shared-rate-limits)).
- **SSO.** Patches that append `LUMEWARP_OIDC_ISSUER`, `LUMEWARP_OIDC_CLIENT_ID`,
  `LUMEWARP_OIDC_CLIENT_SECRET`, `LUMEWARP_SECRET_KEY` and `LUMEWARP_PUBLIC_URL` to the
  control plane's `env`, the last two secrets from a Secret you create. Authorization
  backends and other settings are more environment entries in the same way.

```sh
kubectl kustomize deploy/kubernetes/overlays/example   # review the result
kubectl apply -k deploy/kubernetes/overlays/example
```

The control plane stays at one replica: the compiler and rollover scheduler run once per
installation. Upgrading means applying the manifests of the new version; the Job's pod
template is immutable, so delete the finished Job first
(`kubectl -n lumewarp delete job lumewarp-control-plane-migrate`) or the apply is rejected.
See [Upgrades and limits](./upgrades.md).

## What is not included

- **Monitoring.** The ServiceMonitors, PrometheusRule and Grafana dashboards need the
  Prometheus Operator CRDs and are not in the manifests. The gateway admin port and the
  control-plane metrics port serve `/metrics`, so any Prometheus can scrape them; for the
  Operator objects use the chart with `monitoring.*`
  ([Observability](./observability.md#alert-rules-and-grafana-dashboards)).
- **Shared rate limits.** No Valkey. Run your own Redis or Valkey, put its full URL in a
  Secret and add it to the gateway as `LUMEWARP_SHARED_LIMITS_REDIS_URL` in an overlay, or
  use the chart's `valkey.enabled`.
- **Gateway state volume, plugins and autoscaling.** The gateway keeps its snapshot in an
  `emptyDir`, so a new pod is unready until the control plane sends one. A persistent
  StatefulSet, plugin volumes, an HPA or a PodDisruptionBudget come from the chart
  ([Gateway replicas](./helm.md#gateway-replicas)).
- **A database, ingress and TLS.** Bring your own; to get a database with the install, use
  [lumewarp-stack](./helm.md#full-stack-lumewarp-stack).

## Regenerating

`deploy/kubernetes/base/` is generated; do not edit it. After a chart change, run

```sh
deploy/kubernetes/generate.sh
```

which renders `deploy/helm/lumewarp` with `deploy/kubernetes/values.yaml` (`helm` from `PATH`,
or the `alpine/helm` image through docker), removes the Helm-only labels and annotations and
writes one file per kind. CI regenerates the base and fails when it differs from what is
committed. `generate.sh --single FILE [--version X]` writes the one-file release asset.
To change what the manifests contain, edit `deploy/kubernetes/values.yaml` (Secret names,
for instance) and regenerate.
