# Lumewarp Helm chart

Deploys the Lumewarp control plane and Rust gateway. The control plane owns Postgres,
compiles versioned config snapshots and serves the Admin UI; the gateway follows the
config stream and keeps the last ACKed snapshot on a local volume. Each release publishes
the chart as `oci://ghcr.io/lumewarp/charts/lumewarp`. For a database and OpenFGA in the
same install, use [`deploy/helm/lumewarp-stack`](../lumewarp-stack) from a checkout; for a cluster without
Helm, see the plain manifests in [`deploy/kubernetes`](../../kubernetes).

## Quick start

You need Kubernetes 1.25+, Helm 3 and a PostgreSQL 16+ database. Create the Secrets the
chart reads (names are values; these are the defaults):

```sh
kubectl create namespace lumewarp
kubectl -n lumewarp create secret generic lumewarp-control-plane \
  --from-literal=database-url='postgresql+asyncpg://lumewarp:secret@postgres.example:5432/lumewarp'
kubectl -n lumewarp create secret generic lumewarp-encryption \
  --from-literal=encryption-key="$(openssl rand -base64 48)"
```

A fresh install has no gateway token; create one from the control plane. Install with the
gateway off, create a token, then turn the gateway on:

```sh
CHART=oci://ghcr.io/lumewarp/charts/lumewarp
VERSION=0.2.0   # a release; from a checkout use deploy/helm/lumewarp and drop --version

helm install lumewarp "$CHART" --version "$VERSION" -n lumewarp \
  --set global.encryptionKey.secretName=lumewarp-encryption \
  --set gateway.enabled=false

# In another terminal: open the UI, sign in, then Gateways -> New gateway token
# and copy the lwg-... secret.
kubectl -n lumewarp port-forward svc/lumewarp-control-plane 8091:8091

# Scripted alternative without the UI:
kubectl -n lumewarp exec deploy/lumewarp-control-plane -- \
  python -m lumewarp_control_plane bootstrap --name lumewarp-gateway
kubectl -n lumewarp create secret generic lumewarp-gateway \
  --from-literal=token='lwg-...'

helm upgrade lumewarp "$CHART" --version "$VERSION" -n lumewarp \
  --reuse-values --set gateway.enabled=true
```

Probe the gateway with `kubectl -n lumewarp port-forward svc/lumewarp-gateway 4000:4000`
and `curl localhost:4000/healthz`.

## More

- [Deploy with Helm](../../../docs/operations/helm.md): every value, gateway replicas and
  the state volume, shared rate limits (Valkey), upstream API keys, plugins, MCP and A2A,
  SSO and authorization, and the `lumewarp-stack` umbrella chart.
- [Deploy with plain manifests](../../../docs/operations/kubernetes.md)
- [Observability](../../../docs/operations/observability.md): alerts, dashboards and
  tracing (`monitoring.*`).
- `bash deploy/helm/check.sh` lints the chart and renders its value matrix.
