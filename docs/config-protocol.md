# Config stream protocol

Normative rules for `lumewarp.config.v1.ConfigService` and `lumewarp.usage.v1.UsageService`. The proto files are the wire contract; this document is the behavioural contract both sides are tested against.

## Terms

- **Scope**: one control-plane installation (`Snapshot.scope`). A gateway is configured with exactly one scope and never applies or boots from a snapshot of another scope.
- **Version**: strictly increasing per scope. Every published version has an immutable, complete `Snapshot` payload stored by the control plane.
- **Desired version**: the latest published version for the scope.
- **ACKed version**: per gateway session, the last version the gateway confirmed.

## Control plane: publishing

1. Every source mutation (API write, budget flip, rollover) and its audit row commit in one DB transaction together with an outbox row "recompile scope".
2. A single compiler loop per scope (serialized with a Postgres advisory lock) reads source state at one consistent snapshot, compiles, validates and stores `config_versions(scope, version, payload, digest, source_revision, created_at)`. A compile failure leaves the previous version current and marks the outbox row failed with a reason; it never publishes a partial snapshot.
3. A later compile always sees a later source revision; a slower, older compile must not publish after a newer one (check `source_revision` under the lock).
4. Rollback re-publishes an older version's **routing** content (upstreams, deployments, routes) as a new version; API keys, teams, policies and statuses are always taken from current source state, so a revoked key can never be revived by a rollback.
5. Retention: keep the latest version, the last 50 versions and anything referenced by a connected session; older payloads may be compacted. A gateway whose base was compacted gets a full snapshot.
6. Limits: a snapshot larger than 64 MiB or with more than 200k keys fails compilation. gRPC max message size is 64 MiB on both sides.

## Session state machine

```
gateway                                   control plane
  |-- Hello{gateway_id, have_version, scope, supported_capabilities} -->
  |                                       authenticate token; bind gateway_id
  |<-- full(desired) or delta(have_version -> desired) ---------------
  |   apply privately, persist, swap
  |-- Ack{version} or Nack{version, code, have_version} ------------->
  |<-- next update only after Ack/Nack of the previous one -----------
```

- **At most one un-ACKed update per session.** If several versions are published while one is pending, the next update goes straight from the session's ACKed version to the current desired version (delta if the base is retained, otherwise full).
- The base of a delta is always the session's ACKed version (or Hello.have_version before the first ACK) — never a global "last sent" version.
- The first update after Hello is a delta only when `Hello.scope` matches, `have_version` is retained, `have_version <= desired` and `Hello.have_digest` equals the stored digest of `have_version`; otherwise full. Until the first ACK after a full, the session has **no** delta base (a NACKed full is never followed by a delta).
- Digest = `config_versions.digest` (sha256 of the control plane's serialized payload). It is sent as `ConfigUpdate.target_digest`; the gateway treats it as opaque, persists it with the snapshot and echoes it in `Hello.have_digest` (serialization is not byte-identical across languages, so the gateway never recomputes it). `have_version > desired` (control plane restored from backup) → full.
- ACK/NACK must name the session's pending version; anything else is a protocol error and the control plane closes the stream.
- NACK handling:
  - `BASE_MISMATCH` → send full(desired).
  - `INVALID_CONFIG` / `UNSUPPORTED_FEATURE` → quarantine that version for that gateway; do not resend it. Send again only when a newer version is published. Surface it in gateway status and metrics.
  - `PERSIST_FAILED` → retry the same version with exponential backoff (1 s → 60 s).
- ACK timeout: 30 s, then the control plane closes the stream; the gateway reconnects with backoff (0.5 s → 30 s, full jitter).
- Delta semantics: upserts replace the whole entity by key (name / key_id / team id); removes delete by key; `created_at_unix_ms` and `required_capabilities` replace the target metadata. Removing an entity that does not exist is not an error. A delta whose result fails validation is NACKed `INVALID_CONFIG`.

## Gateway: applying an update

```
receive → apply to a private copy → validate (capabilities, references, hash sizes, enums)
        → compile indexes, resolve upstream secrets from the environment
        → persist: write <state-dir>/snapshot.pb.tmp, fsync, rename to snapshot.pb, fsync dir
        → ArcSwap the new router
        → ACK
```

- One background apply task; blocking file I/O never runs on the request path.
- Persist failure → NACK `PERSIST_FAILED`, keep serving the previous version in memory.
- ACK means "active and durable": a gateway restarted right after ACK boots into that version.
- Validation failure (including an unresolvable `api_key_ref`) → NACK `INVALID_CONFIG`, keep the previous version.
- State survives swaps: per-deployment in-flight counters, rate-limit buckets (by `RateLimitScope.id`) and the metrics registry are process-lifetime, keyed by stable ids.

## Gateway: boot

- CP mode always requires keys: zero keys means every `/v1/*` request is 401.
- With a valid on-disk snapshot of the configured scope: serve it immediately, then connect in the background.
- Without one (missing, wrong scope, corrupt — file has a trailing sha256 and is size-capped): do not serve `/v1/*` (503 `not_ready`, readiness false) until the first full snapshot is applied. Never fall back to an open router.
- The gateway token is read from the environment and never written to disk. The state dir is created `0700`, the file `0600`.

## Usage reporting

- Events are produced off the request path into a bounded queue (default 100k events). Batches go out every 1 s or at 500 events. Failed reports retry with backoff from the same queue; when the queue is full, new events are dropped and `lumewarp_usage_events_dropped_total` increments.
- `request_id` is a UUIDv7 generated by the gateway at request start and reused for retries.
- `Report` is all-or-nothing and idempotent (`ON CONFLICT (request_id) DO NOTHING`); per new event the control plane adds the cost once to each of key, project, team and org totals for the event's UTC calendar month, in the same transaction, then evaluates budgets.
- Budget enforcement is **eventual**: once usage is ingested, an exhausted scope flips affected keys to `BUDGET_EXCEEDED` and a new version is published within 5 s. Lost events (gateway crash, full queue) are not recovered in P1, and streams in flight when a budget runs out are not cut.
- Month rollover is driven by a scheduler (runs at startup and at each UTC month boundary), not by incoming usage. While the control plane is down, blocked keys stay blocked.
