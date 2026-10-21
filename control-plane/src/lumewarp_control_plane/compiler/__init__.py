"""Config compiler (owner: C3): source state -> immutable versioned snapshots.

Behaviour is specified by docs/config-protocol.md ("Control plane: publishing").
"""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane._proto.lumewarp.config.v1 import config_service_pb2, snapshot_pb2
from lumewarp_control_plane.db import SessionFactory


class CompileError(Exception):
    """Source state cannot be compiled into a valid snapshot (e.g. dangling deployment,
    over the snapshot size/key limits). Nothing is published; the outbox row is marked
    failed with this message, which must not contain secrets.
    """


@dataclass(frozen=True)
class CompiledSnapshot:
    """Result of `compile_scope`, not yet published.

    `snapshot.version` is 0 here; `publish_pending` assigns the next version for the scope.
    """

    snapshot: snapshot_pb2.Snapshot
    source_revision: int
    key_count: int


async def compile_scope(session: AsyncSession, scope: str) -> CompiledSnapshot:
    """Compile the current source state of `scope` into a snapshot.

    Pure over source state read in `session`; the caller runs it in one REPEATABLE READ
    (or stricter) transaction so every table is read at one consistent snapshot, and
    `source_revision` is `scopes.source_revision` as seen by that transaction.

    Contract:
    - Expands org -> team -> project -> key inheritance into each key's EffectivePolicy.
      Model access: resolve INHERIT first (a level with INHERIT takes its parent's
      effective set unchanged; orgs are never INHERIT), then intersect: ALL is the
      universal set, LIST is exactly `allowed_models`. `allow_all_models` is true iff the
      resulting effective set is still universal, e.g. ALL -> INHERIT -> INHERIT -> INHERIT
      (the defaults) allows every model. A LIST with an empty list anywhere on the path is
      deny-all (`allow_all_models=false`, `allowed_models=[]`), never "all".
      Rate limits: one RateLimitScope per level that sets a limit (id "<level>:<uuid>").
      Status from lifecycle + expiry + budget state of the current UTC month (see
      models.ApiKey).
    - Validates references (routes -> deployments -> upstreams), unique names/hashes, hash
      length, and the limits in settings (max_snapshot_bytes, max_snapshot_keys).
    - Deterministic: same source state -> byte-identical serialization (sorted entities).

    Raises CompileError on invalid source state.
    """
    raise NotImplementedError


async def publish_pending(session_factory: SessionFactory) -> int | None:
    """One step of the compiler loop.

    Under a per-scope Postgres advisory lock: if there are pending `config_outbox` rows,
    compile, and if the compiled source_revision is newer than the latest published
    version's, store a new `config_versions` row (payload, sha256 digest, source_revision)
    and mark the covered outbox rows done; a compile failure marks them failed and keeps
    the previous version current. Returns the newly published version, or None if nothing
    was published.
    """
    raise NotImplementedError


def diff(old: snapshot_pb2.Snapshot, new: snapshot_pb2.Snapshot) -> config_service_pb2.Delta:
    """Delta that turns `old` into `new` (base_version=old.version, version=new.version).

    Upserts carry whole entities keyed by name / key_id / team id; removes list keys that
    disappeared; created_at_unix_ms and required_capabilities are copied from `new`.
    Applying the result to `old` must yield a snapshot equal to `new`.
    """
    raise NotImplementedError


async def rollback_routing(session: AsyncSession, scope: str, to_version: int) -> int:
    """Publish a new version whose routing content (upstreams, deployments, routes) comes
    from `to_version` while keys, teams, policies and statuses come from current source
    state, so a revoked key is never revived. Returns the new version number.
    Raises LookupError if `to_version` is unknown or compacted.
    """
    raise NotImplementedError
