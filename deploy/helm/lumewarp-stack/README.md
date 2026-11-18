# lumewarp-stack

The [`lumewarp`](../lumewarp) chart plus what it needs to run, installed in one step: an
optional bundled PostgreSQL (on by default) and an optional OpenFGA (off by default). Settings
of the lumewarp chart go under `lumewarp:`. The full reference is
[Deploy with Helm](../../../docs/operations/helm.md).

## Quick start (evaluation)

```bash
# From a checkout of the release tag; this chart is not published to a registry.
helm repo add openfga https://openfga.github.io/helm-charts
helm dependency build deploy/helm/lumewarp-stack
helm install lumewarp deploy/helm/lumewarp-stack -n lumewarp --create-namespace \
  --set global.encryptionKey.secretName=lumewarp-encryption   # Secret with encryption-key, 32+ chars
```

The control plane starts after the migration Job. Then create the gateway token as the
install notes say: sign in to the Admin UI and use **Gateways → New gateway token**, or use
`kubectl exec ... bootstrap` without the UI, and put the secret in a Secret named
`lumewarp-gateway`.

## Bundled PostgreSQL: evaluation and small installs only

One pod, one PVC, no replication and **no backups**. For production install with
`--set postgres.enabled=false --set lumewarp.controlPlane.databaseUrl.secretName=<Secret>` (key
`database-url`, a `postgresql+asyncpg://` URL to your own database). The Secret built for the
bundled database is named `lumewarp-stack-database` (fixed: umbrella values cannot use the
release name), so one namespace holds one stack.

## OpenFGA

```bash
helm install lumewarp deploy/helm/lumewarp-stack -n lumewarp -f deploy/helm/lumewarp-stack/values-openfga.yaml
```

`values-openfga.yaml` sets `openfga.enabled=true` and `lumewarp.controlPlane.authz.backend=openfga`
(the URL `http://lumewarp-openfga:8080` and the store/model init are already in `values.yaml`).
OpenFGA uses the database `openfga` of the bundled PostgreSQL, created when its data directory
is first initialised. With your own PostgreSQL set `openfga.datastore.uriSecret` to a Secret
whose `uri` key holds the `postgres://` URL.

## Migration

The lumewarp chart migrates in a pre-install hook, which cannot reach a database created in
the same release. The stack disables that hook and runs `migrate` as a Job (one per revision,
waiting for the bundled PostgreSQL). It runs alongside an upgrade's rollout, not before it;
the control plane is a single pod with the Recreate strategy.

## Dependencies and releases

`charts/` is not committed (`helm dependency build` fills it); `Chart.lock` is. The release job
points the `lumewarp` dependency at `oci://ghcr.io/lumewarp/charts` and publishes this chart to
the same registry.
