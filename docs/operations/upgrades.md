# Upgrades and limits

## Database schema

The database baseline has been rebuilt to include zones. An existing development database
from the previous schema must be dropped and recreated before running the current
`lumewarp-control-plane migrate`; it is not upgraded in place, and its rows are not carried
over. The current migrations define a fresh schema. Back up any development data that must
be kept before recreating the database.

After the baseline is frozen for a release, schema changes are added as new Alembic
revisions. Downgrades are not supported: restore a database backup to roll back.

## Important limits

- **Control plane replicas must stay at 1.** One process runs the compiler loop and rollover
  scheduler for the whole installation; it iterates the live zones.
- **Many gateway processes, one token per zone.** A gateway is one process: each
  `gateway.replicaCount` (or autoscaled pod) uses a token for one zone and generates its own
  gateway id at startup. The Gateways page is viewed one zone at a time. Rate limits are
  counted per gateway unless the gateways share a Redis (`valkey.enabled`, or
  `gateway.sharedLimits.redis.existingSecret`).
- **Config versions and quarantine are per zone.** An `INVALID_CONFIG` NACK quarantines the
  version for every gateway of that zone, including gateways that connect later; a newer
  version supersedes it. `UNSUPPORTED_FEATURE` affects only the gateway that lacks the
  capability. Finish a gateway rollout before publishing features that the old build lacks.
- **Rollback is installation-wide.** Selecting a version in one zone restores the catalog
  captured by that version and recompiles every live zone from its own deployments and
  zone-bound resources. Current keys, grants and policies remain in effect.
- **Gateway state volume.** With the default `emptyDir`, a new pod has no cache and returns
  503 on `/readyz` and `/v1/*` until the first full snapshot arrives. With
  `gateway.persistence.enabled` the gateway is a StatefulSet with one `ReadWriteOnce` claim
  per pod, so a restarted pod serves from its cached snapshot; `size`, `storageClass` and
  `annotations` of that claim template cannot change once the StatefulSet exists (see
  [Deploy with Helm](./helm.md#state-volume)). The chart runs the gateway as UID/GID 65532
  with `fsGroup: 65532`, so mounted state directories are writable.
- **gRPC is plaintext by default.** Set `controlPlane.grpcTls.enabled` to terminate TLS on
  the control-plane listener, but note that the gateway verifies with the WebPKI root store
  only; a private CA must be added to the gateway image. Inside a trusted cluster, plaintext
  plus NetworkPolicy is the simple option.
- **Keep gRPC keepalive pings intact.** The gateway pings the config stream every 30 s. If
  an L7 proxy or mesh sidecar sits between gateway and control plane, allow pings without
  data at least every 10 s, or it will reset the stream with `too_many_pings` (see
  [config protocol](../config-protocol.md)).
- **Budgets are eventual.** A key can exceed its zone's budget briefly while usage batches
  are in flight; in-flight streams are not cut. Usage events are dropped when the gateway's
  bounded queue is full.
- **A plugin change is a restart.** The gateway reads plugin directories once at startup;
  replacing a component needs a new pod (`kubectl rollout restart`). Sessions and A2A task
  ids survive the restart because they are sealed under `gateway.idKey`, not held in memory.
