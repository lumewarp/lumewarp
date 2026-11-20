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

Both components start from one install. The chart creates the gateway credential Secret
`<fullname>-gateway-tokens` with a random 48-character token for `global.zone` (and every
zone in `global.gatewayTokens.zones`), keyed `LUMEWARP_GATEWAY_ZONE_<ID>_TOKEN`; the
control plane reads every key and the gateway reads its own zone's key. Read the token with:

```sh
kubectl -n lumewarp get secret lumewarp-gateway-tokens \
  -o jsonpath='{.data.LUMEWARP_GATEWAY_ZONE_DEFAULT_TOKEN}' | base64 -d
```

```sh
CHART=oci://ghcr.io/lumewarp/charts/lumewarp
VERSION=0.2.0   # a release; from a checkout use deploy/helm/lumewarp and drop --version

helm install lumewarp "$CHART" --version "$VERSION" -n lumewarp \
  --set global.encryptionKey.secretName=lumewarp-encryption
```

Set `global.gatewayTokens.secretName` to an existing Secret with those keys instead of the
chart-created one. More zones go in `global.gatewayTokens.zones`; each zone's gateway is its
own release with `global.zone` set.

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
