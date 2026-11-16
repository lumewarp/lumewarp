# Deploy with plain manifests

For a cluster without Helm, Lumewarp ships Kubernetes manifests for a complete installation:
PostgreSQL, OpenFGA, the control plane and a gateway. They come as a kustomize base in
[`deploy/kubernetes`](https://github.com/lumewarp/lumewarp/tree/main/deploy/kubernetes) and as
one `lumewarp-<version>.yaml` file attached to every GitHub Release. Both are generated from
the [lumewarp-stack chart](./helm.md#full-stack-lumewarp-stack), so they run what that chart
runs; the charts stay the place for options such as autoscaling, persistence or monitoring.
For what the parts are, see [Deploy](./index.md).

## What the manifests contain

Everything lives in the `lumewarp` namespace. The Lumewarp image tags are the release
version; PostgreSQL and OpenFGA images are pinned.

| Object | Notes |
| --- | --- |
| Namespace `lumewarp` | Created by the manifests. |
| `lumewarp-postgres` StatefulSet and Service | One PostgreSQL 17 pod with a 10 Gi volume claim, databases `lumewarp` and `openfga`. No replication and no backups. |
| `lumewarp-migrate-1` Job | The control plane's schema migration. Waits for PostgreSQL, then runs `migrate`; removed one hour after it finishes. |
| `lumewarp-openfga-migrate` Job | OpenFGA's schema migration (`openfga migrate`). Waits for PostgreSQL. |
| `lumewarp-openfga` Deployment and Service | One OpenFGA pod on 8080; it waits for its migration Job to complete (a ServiceAccount, Role and RoleBinding let it read the Job's status). |
| `lumewarp-control-plane` Deployment and Service | One pod (`Recreate`): HTTP API and UI on 8091, gRPC on 50051, metrics on 9465. Authorization is OpenFGA: an init container runs `openfga init`, which creates the store and writes the model. |
| `lumewarp-gateway` Deployment, Service and ConfigMap | One pod; `http` on 4000 and `admin` (`/metrics`, `/readyz`) on 9464. State is an `emptyDir`. |

The manifests have no Valkey, no persistent gateway state and no monitoring objects; see
[What is not included](#what-is-not-included).

## Prerequisites

- Kubernetes 1.25+ and `kubectl` (kustomize is built in).
- A default StorageClass for the PostgreSQL volume claim.

## Create the Secrets

The manifests reference Secrets by name and never contain a credential. From a checkout,
`deploy/kubernetes/create-secrets.sh` creates the namespace and the four Secrets with random
values; a Secret that already exists is left alone, so a rerun never changes a password the
database already uses. Without a checkout, the same by hand:

```sh
kubectl create namespace lumewarp
PASSWORD="$(openssl rand -hex 24)"

kubectl -n lumewarp create secret generic lumewarp-postgres --from-literal=password="$PASSWORD"
kubectl -n lumewarp create secret generic lumewarp-database \
  --from-literal=database-url="postgresql+asyncpg://lumewarp:${PASSWORD}@lumewarp-postgres:5432/lumewarp"
kubectl -n lumewarp create secret generic lumewarp-openfga-datastore \
  --from-literal=uri="postgres://lumewarp:${PASSWORD}@lumewarp-postgres:5432/openfga?sslmode=disable"
kubectl -n lumewarp create secret generic lumewarp-encryption-key \
  --from-literal=encryption-key="$(openssl rand -hex 32)"
```

| Secret | Key | Content |
| --- | --- | --- |
| `lumewarp-postgres` | `password` | The password of the PostgreSQL user `lumewarp`, set when the database is first initialised. |
| `lumewarp-database` | `database-url` | The control plane's SQLAlchemy async URL to the `lumewarp` database. |
| `lumewarp-openfga-datastore` | `uri` | OpenFGA's URL to the `openfga` database. |
| `lumewarp-encryption-key` | `encryption-key` | `LUMEWARP_ENCRYPTION_KEY`, 32+ characters, given to both Lumewarp components; it seals the upstream API keys stored in the Admin UI, and changing it makes them unreadable ([Upstream API keys](./helm.md#upstream-api-keys)). |
| `lumewarp-gateway` | `token` | A gateway token, created after the control plane runs (below). |

## Apply and bootstrap

Apply the release asset, or the base from a checkout:

```sh
VERSION=0.2.0
kubectl apply -f "https://github.com/lumewarp/lumewarp/releases/download/v${VERSION}/lumewarp-${VERSION}.yaml"

# or, from a checkout
kubectl apply -k deploy/kubernetes/base
```

Everything starts at once and waits for what it needs:

1. PostgreSQL initialises its volume and creates the `openfga` database.
2. Both migration Jobs wait for PostgreSQL, then migrate. OpenFGA starts once its Job has
   completed. The control plane reports `/readyz` only when its database is at the migration
   head, so it may start first and stays unready until `lumewarp-migrate-1` has finished.
3. The control plane's `openfga init` container waits for OpenFGA, creates the store
   `lumewarp` and writes the model.

```sh
kubectl -n lumewarp wait --for=condition=complete job/lumewarp-migrate-1 job/lumewarp-openfga-migrate --timeout=5m
kubectl -n lumewarp rollout status deploy/lumewarp-control-plane
```

The control plane creates gateway tokens itself, so a fresh install has none. A token is a
scope credential, not bound to a gateway; any number of gateway pods may share it:

```sh
kubectl -n lumewarp exec deploy/lumewarp-control-plane -- \
  python -m lumewarp_control_plane bootstrap --name lumewarp-gateway
kubectl -n lumewarp create secret generic lumewarp-gateway --from-literal=token='lwg-...'
```

The gateway pod references that Secret, so until it exists the pod waits with
`CreateContainerConfigError`; Kubernetes starts it once the Secret is there. If it still
waits, `kubectl -n lumewarp rollout restart deploy/lumewarp-gateway`. `/readyz` on the admin
port turns 200 when the gateway has loaded a snapshot.

Installation admins, first login and the break-glass password work as in
[Deploy with Helm](./helm.md#install): the first verified login whose email matches
`LUMEWARP_INITIAL_ADMIN_EMAIL` becomes the proxy_admin, so add that variable (and the
settings of [Control plane settings](./control-plane.md)) through an overlay. Team and org
roles live in OpenFGA ([OpenFGA](./identity.md#openfga)). Reach the UI with
`kubectl -n lumewarp port-forward svc/lumewarp-control-plane 8091:8091`.

## Customize with an overlay

Do not edit the base; copy
[`overlays/example`](https://github.com/lumewarp/lumewarp/tree/main/deploy/kubernetes/overlays/example),
point its `resources` at the base (a path, or a remote reference to this repository) and
change it. The example shows three things:

- **Image tag.** `images:` pins both Lumewarp images; `kustomize edit set image` does the
  same from a script. Use your own repositories here for custom images.
- **Gateway replicas.** A JSON patch on `/spec/replicas` of the gateway Deployment. Every pod
  is its own gateway and all share the token, but rpm, tpm and max_parallel are counted per
  gateway unless they share a Redis ([Shared rate limits](./helm.md#shared-rate-limits)).
- **SSO.** Patches that append `LUMEWARP_OIDC_ISSUER`, `LUMEWARP_OIDC_CLIENT_ID`,
  `LUMEWARP_OIDC_CLIENT_SECRET`, `LUMEWARP_SECRET_KEY` and `LUMEWARP_PUBLIC_URL` to the
  control plane's `env`, the last two secrets from a Secret you create. Other settings are
  more environment entries in the same way.

```sh
kubectl kustomize deploy/kubernetes/overlays/example   # review the result
kubectl apply -k deploy/kubernetes/overlays/example
```

The control plane stays at one replica: the compiler and rollover scheduler run once per
installation.

## Upgrading

Apply the manifests of the new version. A Job's pod template is immutable, so delete the
migration Jobs that still exist first, or the apply is rejected; applying then runs both
migrations again for the new version:

```sh
kubectl -n lumewarp delete job lumewarp-migrate-1 lumewarp-openfga-migrate --ignore-not-found
```

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
- **A production database, ingress and TLS.** The bundled PostgreSQL is one pod without
  backups. For production, point the control plane and OpenFGA at your own database (change
  the URLs in `lumewarp-database` and `lumewarp-openfga-datastore` and remove the
  `lumewarp-postgres` StatefulSet in an overlay), or use the [Helm chart](./helm.md).

## Regenerating

`deploy/kubernetes/base/` is generated; do not edit it. After a chart change, run

```sh
deploy/kubernetes/generate.sh
```

which renders `deploy/helm/lumewarp-stack` with `deploy/kubernetes/values.yaml` (`helm` from
`PATH`, or the `alpine/helm` image through docker; the OpenFGA chart is fetched from its
repository), removes the Helm-only labels, hook annotations and test pods, and writes one file
per kind. CI regenerates the base and fails when it differs from what is committed.
`generate.sh --single FILE [--version X]` writes the one-file release asset. To change what
the manifests contain, edit `deploy/kubernetes/values.yaml` and regenerate.
