# Upgrades and limits

## Releases

Releases are tags `vX.Y.Z` (`vX.Y.Z-<pre>` for a pre-release). **While Lumewarp is 0.x
there is no upgrade path: drop the control-plane database before installing a new
release.** The gateway boots from its cached snapshot, so delete its state directory too
when the snapshot format changed; the release notes say when.

## Important limits

- **Control plane replicas must stay at 1.** The compiler and rollover scheduler are
  single-process; the chart uses `Recreate`.
- **Migration is a pre-upgrade hook.** The old control-plane pod is still running while
  the schema migrates (Rolling/Recreate), so expect a short outage window on upgrades.
- **Many gateway pods, one token.** A gateway is one process: `gateway.replicaCount` (or
  `gateway.autoscaling`) starts several pods that all use the token in
  `gateway.token.secretName`, and each pod generates its own gateway id at startup. The
  Gateways page shows one row per pod. Rate limits are
  counted per gateway unless the gateways share a Redis (`redis.enabled`, or
  `gateway.sharedLimits.redis.existingSecret`).
- **A rolling upgrade can leave an older gateway behind.** A gateway on the old build that
  lacks a plugin or capability NACKs `UNSUPPORTED_FEATURE`; only that gateway skips the
  version, while the others apply it, so the fleet can run different configs during the
  rollout. An `INVALID_CONFIG` NACK is different: it quarantines the version for the whole
  scope, including gateways that start later, until a newer version is published. Finish the
  rollout before publishing anything that needs the new build.
- **Gateway state volume.** With the default `emptyDir`, a new pod has no cache and returns
  503 on `/readyz` and `/v1/*` until the first full snapshot arrives. With
  `gateway.stateDir.persistence.enabled` the gateway is a StatefulSet with one
  `ReadWriteOnce` claim per pod, so a restarted pod serves from its cached snapshot; `size`,
  `storageClass` and `annotations` of that claim template cannot change once the StatefulSet
  exists (see [Deploy with Helm](./helm.md#state-volume)). The chart runs the gateway as
  UID/GID 65532 with `fsGroup: 65532`, so emptyDir and PVC mounts are writable; no manual
  `fsGroup` is needed. Override `gateway.podSecurityContext` only for a custom image with
  another UID/GID.
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
- **A plugin change is a restart.** The gateway reads the plugin directories once at
  startup and a call never reads a file, so replacing a component needs a new pod
  (recreate the Secret/ConfigMap or write the PVC, then `kubectl rollout restart`).
  Sessions and A2A task ids survive the restart because they are sealed under
  `gateway.idKey`, not held in memory.
