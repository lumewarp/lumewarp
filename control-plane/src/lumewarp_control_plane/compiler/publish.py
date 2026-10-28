"""Versioning: publish compiled snapshots, routing rollback, compaction, compiler loop.

Serialization: every publisher of a scope holds the same Postgres advisory lock
(`lock_key`). The compiler loop takes it session-level on its own connection *before*
starting the REPEATABLE READ transaction it compiles in, so its snapshot always includes
every version published before it got the lock. Rollback takes it transaction-level.
"""

import asyncio
import hashlib
import logging
import random
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from sqlalchemy import func, literal, or_, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from lumewarp_control_plane import outbox
from lumewarp_control_plane import plugins as plugin_api
from lumewarp_control_plane._proto.lumewarp.config.v1 import snapshot_pb2 as pb
from lumewarp_control_plane.compiler.compile import (
    ROUTING_STRATEGY,
    STREAM_USAGE_MODE,
    UPSTREAM_KIND,
    CompileError,
    compile_scope,
)
from lumewarp_control_plane.compiler.validate import SnapshotInvalid, validate_snapshot
from lumewarp_control_plane.db import SessionFactory
from lumewarp_control_plane.models import (
    ConfigOutbox,
    ConfigVersion,
    ConfigVersionKind,
    Deployment,
    GatewaySession,
    OutboxStatus,
    Route,
    RouteDeployment,
    Scope,
    StreamUsageMode,
    Upstream,
)
from lumewarp_control_plane.settings import get_settings

log = logging.getLogger(__name__)

# NOTIFY channel with payload "<scope>:<version>", sent on commit of a new version.
PUBLISHED_CHANNEL = "lumewarp_config_published"
MAX_ERROR_CHARS = 2000
SERIALIZATION_ATTEMPTS = 5


def _serialization_failure(exc: DBAPIError) -> bool:
    return getattr(exc.orig, "sqlstate", None) == "40001"


def lock_key(scope: str):
    """SQL expression for the scope's advisory lock id (bigint)."""
    return func.hashtextextended(literal(f"lumewarp.compile:{scope}"), 0)


def _engine(session_factory: SessionFactory) -> AsyncEngine:
    bind = session_factory.kw.get("bind")
    if not isinstance(bind, AsyncEngine):
        raise TypeError("session_factory must be bound to an AsyncEngine")
    return bind


async def _latest(session: AsyncSession, scope: str) -> tuple[int, int] | None:
    row = (
        await session.execute(
            select(ConfigVersion.version, ConfigVersion.source_revision)
            .where(ConfigVersion.scope == scope)
            .order_by(ConfigVersion.version.desc())
            .limit(1)
        )
    ).first()
    return (row.version, row.source_revision) if row else None


async def _mark(
    session: AsyncSession,
    scope: str,
    max_revision: int,
    status: OutboxStatus,
    *,
    version: int | None = None,
    error: str | None = None,
) -> None:
    await session.execute(
        update(ConfigOutbox)
        .where(
            ConfigOutbox.scope == scope,
            ConfigOutbox.status == OutboxStatus.PENDING,
            ConfigOutbox.source_revision <= max_revision,
        )
        .values(
            status=status,
            attempts=ConfigOutbox.attempts + 1,
            published_version=version,
            last_error=error,
            processed_at=func.now(),
        )
    )


async def store_version(
    session: AsyncSession,
    scope: str,
    snapshot: pb.Snapshot,
    *,
    version: int,
    source_revision: int,
    kind: ConfigVersionKind = ConfigVersionKind.COMPILE,
    rollback_of_version: int | None = None,
    now: datetime | None = None,
) -> int:
    """Stamp `snapshot` with version and time, check it against every gateway invariant,
    store it immutably, and NOTIFY. Caller holds the scope's advisory lock and commits.
    Raises CompileError (nothing stored) for a snapshot a gateway would reject.
    """
    settings = get_settings()
    now = now or datetime.now(UTC)
    snapshot.version = version
    snapshot.created_at_unix_ms = int(now.timestamp() * 1000)
    try:
        validate_snapshot(
            snapshot,
            max_bytes=settings.max_snapshot_bytes,
            max_keys=settings.max_snapshot_keys,
        )
    except SnapshotInvalid as exc:
        raise CompileError(str(exc)) from exc
    payload = snapshot.SerializeToString(deterministic=True)
    session.add(
        ConfigVersion(
            scope=scope,
            version=version,
            payload=payload,
            digest=hashlib.sha256(payload).digest(),
            size_bytes=len(payload),
            key_count=len(snapshot.api_keys),
            source_revision=source_revision,
            kind=kind,
            rollback_of_version=rollback_of_version,
        )
    )
    await session.flush()
    await session.execute(select(func.pg_notify(PUBLISHED_CHANNEL, f"{scope}:{version}")))
    return version


async def _publish_locked(
    session: AsyncSession,
    scope: str,
    now: datetime | None,
    router_plugins: Sequence[plugin_api.LoadedPlugin] | None,
    require_local_membership: bool,
) -> int | None:
    pending = await session.scalar(
        select(func.count()).where(
            ConfigOutbox.scope == scope, ConfigOutbox.status == OutboxStatus.PENDING
        )
    )
    if not pending:
        return None
    revision = await session.scalar(select(Scope.source_revision).where(Scope.name == scope))
    if revision is None:
        return None
    latest = await _latest(session, scope)
    if latest is not None and latest[1] >= revision:
        # This source revision is already published (e.g. by a rollback): nothing newer.
        await _mark(session, scope, revision, OutboxStatus.DONE, version=latest[0])
        return None
    version = (latest[0] if latest else 0) + 1
    try:
        compiled = await compile_scope(
            session,
            scope,
            now=now,
            router_plugins=router_plugins,
            require_local_membership=require_local_membership,
        )
        if latest is not None:
            previous = await session.get(ConfigVersion, (scope, latest[0]))
            if previous.payload is not None:
                # Ignore publication metadata when comparing content. The stored wire
                # digest and payload remain immutable (including their original stamp).
                old = pb.Snapshot.FromString(previous.payload)
                compiled.snapshot.version = old.version
                compiled.snapshot.created_at_unix_ms = old.created_at_unix_ms
                payload = compiled.snapshot.SerializeToString(deterministic=True)
                if hashlib.sha256(payload).digest() == previous.digest:
                    await _mark(session, scope, revision, OutboxStatus.DONE, version=latest[0])
                    return None
        await store_version(
            session,
            scope,
            compiled.snapshot,
            version=version,
            source_revision=compiled.source_revision,
            now=now,
        )
    except CompileError as exc:
        log.error("compile of scope %s failed: %s", scope, exc)
        await _mark(session, scope, revision, OutboxStatus.FAILED, error=str(exc)[:MAX_ERROR_CHARS])
        return None
    await _mark(session, scope, revision, OutboxStatus.DONE, version=version)
    return version


async def publish_pending(
    session_factory: SessionFactory,
    scope: str | None = None,
    *,
    now: datetime | None = None,
    router_plugins: Sequence[plugin_api.LoadedPlugin] | None = None,
    require_local_membership: bool,
) -> int | None:
    """One step of the compiler loop for `scope` (default settings.scope).

    Under the scope's advisory lock, in one REPEATABLE READ transaction: if there are
    pending `config_outbox` rows, compile; if the source revision is newer and the
    compiled content changed, store a new `config_versions` row. Mark covered outbox
    rows done even when content is unchanged. Serialization failures retry the entire
    transaction with bounded full jitter and a fresh snapshot. A compile failure marks
    rows failed and keeps the previous version current; nothing partial is published.
    Returns the newly published version, or None if nothing was published.
    """
    scope = scope or get_settings().scope
    async with _engine(session_factory).connect() as conn:
        await conn.execute(select(func.pg_advisory_lock(lock_key(scope))))
        await conn.commit()
        try:
            await conn.execution_options(isolation_level="REPEATABLE READ")
            for attempt in range(SERIALIZATION_ATTEMPTS):
                try:
                    async with AsyncSession(bind=conn, expire_on_commit=False) as session:
                        async with session.begin():
                            return await _publish_locked(
                                session, scope, now, router_plugins, require_local_membership
                            )
                except DBAPIError as exc:
                    if not _serialization_failure(exc) or attempt == SERIALIZATION_ATTEMPTS - 1:
                        raise
                    log.info("retrying serialization conflict for scope %s", scope)
                    await asyncio.sleep(random.uniform(0, min(0.01 * 2**attempt, 0.2)))
        finally:
            await conn.execute(select(func.pg_advisory_unlock(lock_key(scope))))
            await conn.commit()


# --------------------------------------------------------------------------- rollback


async def _restore_routing(session: AsyncSession, old: pb.Snapshot) -> None:
    """Make the routing source tables equal to `old`'s routing content."""
    kind = {v: k for k, v in UPSTREAM_KIND.items()}
    mode = {v: k for k, v in STREAM_USAGE_MODE.items()}
    strategy = {v: k for k, v in ROUTING_STRATEGY.items()}

    upstreams = {u.name: u for u in await session.scalars(select(Upstream))}
    deployments = {d.name: d for d in await session.scalars(select(Deployment))}
    routes = {r.model: r for r in await session.scalars(select(Route))}
    want_routes = {r.model for r in old.routes}
    want_deployments = {d.name for d in old.deployments}
    want_upstreams = {u.name for u in old.upstreams}

    for rd in await session.scalars(select(RouteDeployment)):
        await session.delete(rd)
    for model, r in routes.items():
        if model not in want_routes:
            await session.delete(r)
    await session.flush()

    for u in old.upstreams:
        row = upstreams.get(u.name) or Upstream(name=u.name)
        row.kind = kind[u.kind]
        row.base_url = u.base_url
        row.api_key_ref = u.api_key_ref or None
        row.timeout_ms = u.timeout_ms
        row.stream_usage_mode = mode.get(u.stream_usage_mode, StreamUsageMode.INCLUDE_USAGE)
        session.add(row)
        upstreams[u.name] = row
    await session.flush()

    for name, d in deployments.items():
        if name not in want_deployments:
            await session.delete(d)
    await session.flush()
    for d in old.deployments:
        row = deployments.get(d.name) or Deployment(name=d.name)
        row.upstream_id = upstreams[d.upstream].id
        row.upstream_model = d.upstream_model
        row.weight = d.weight
        has_price = d.HasField("price")
        row.price_input_nano_usd_per_mtok = d.price.input_nano_usd_per_mtok if has_price else None
        row.price_output_nano_usd_per_mtok = d.price.output_nano_usd_per_mtok if has_price else None
        session.add(row)
        deployments[d.name] = row
    await session.flush()

    for name, u in upstreams.items():
        if name not in want_upstreams:
            await session.delete(u)
    for r in old.routes:
        row = routes.get(r.model) or Route(model=r.model)
        row.strategy = strategy[r.strategy]
        session.add(row)
        await session.flush()
        for position, name in enumerate(r.deployments):
            session.add(
                RouteDeployment(
                    route_id=row.id, deployment_id=deployments[name].id, position=position
                )
            )
    await session.flush()


async def rollback_routing(
    session: AsyncSession,
    scope: str,
    to_version: int,
    *,
    now: datetime | None = None,
    router_plugins: Sequence[plugin_api.LoadedPlugin] | None = None,
    require_local_membership: bool,
) -> int:
    """Publish a new version whose routing content (upstreams, deployments, routes) comes
    from `to_version` while keys, teams, policies and statuses come from current source
    state, so a revoked key is never revived. Returns the new version number.

    Runs in the caller's transaction (the caller writes the audit row and commits). The
    routing source tables are rewritten to match `to_version`, so later compiles keep the
    rolled-back routing instead of silently reverting it. The published routing is exactly
    `to_version`'s (router plugins are not re-applied to it).
    Raises LookupError if `to_version` is unknown or compacted, CompileError if the
    resulting snapshot is invalid.
    """
    await session.execute(select(func.pg_advisory_xact_lock(lock_key(scope))))
    row = await session.get(ConfigVersion, (scope, to_version))
    if row is None or row.payload is None:
        raise LookupError(f"config version {to_version} of scope {scope!r} is not retained")
    old = pb.Snapshot.FromString(row.payload)
    # deployments.weight is a Postgres INTEGER; versions published before plugin weights
    # were capped could carry larger uint32 weights. Refuse cleanly instead of a DB error.
    too_heavy = [d.name for d in old.deployments if d.weight > plugin_api.MAX_WEIGHT]
    if too_heavy:
        raise CompileError(
            f"version {to_version} has deployment weights above {plugin_api.MAX_WEIGHT} "
            f"({', '.join(too_heavy)}); it cannot be restored"
        )

    await _restore_routing(session, old)
    revision = await outbox.enqueue_recompile(session, scope, f"rollback to {to_version}")
    compiled = await compile_scope(
        session,
        scope,
        now=now,
        router_plugins=router_plugins or [],
        require_local_membership=require_local_membership,
    )
    snap = compiled.snapshot
    for field in ("upstreams", "deployments", "routes"):
        del getattr(snap, field)[:]
        getattr(snap, field).extend(getattr(old, field))

    latest = await _latest(session, scope)
    version = (latest[0] if latest else 0) + 1
    await store_version(
        session,
        scope,
        snap,
        version=version,
        source_revision=revision,
        kind=ConfigVersionKind.ROLLBACK,
        rollback_of_version=to_version,
        now=now,
    )
    await _mark(session, scope, revision, OutboxStatus.DONE, version=version)
    return version


# --------------------------------------------------------------------------- compaction


async def compact_versions(session: AsyncSession, scope: str, *, retain: int | None = None) -> int:
    """Drop the payload of versions no longer needed, keeping: the latest `retain`
    versions (default settings.config_versions_retained, always including the latest)
    and every version a connected gateway session has ACKed, pending or announced in Hello.
    Metadata (digest, revision) stays. Returns the number of versions compacted.
    """
    retain = max(1, retain if retain is not None else get_settings().config_versions_retained)
    recent = (
        select(ConfigVersion.version)
        .where(ConfigVersion.scope == scope)
        .order_by(ConfigVersion.version.desc())
        .limit(retain)
    )
    # Session pins are evaluated inside the same UPDATE statement (one snapshot), never
    # from a set read earlier: a gateway whose session row committed before the statement
    # keeps its base. One that commits later and finds its base compacted gets a full
    # snapshot, which the protocol allows.
    pinned = (
        select(literal(1))
        .select_from(GatewaySession)
        .where(
            GatewaySession.scope == ConfigVersion.scope,
            GatewaySession.connected.is_(True),
            or_(
                GatewaySession.acked_version == ConfigVersion.version,
                GatewaySession.pending_version == ConfigVersion.version,
                GatewaySession.hello_have_version == ConfigVersion.version,
            ),
        )
        .exists()
    )
    result = await session.execute(
        update(ConfigVersion)
        .where(
            ConfigVersion.scope == scope,
            ConfigVersion.payload.is_not(None),
            ConfigVersion.version.not_in(recent.scalar_subquery()),
            ~pinned,
        )
        .values(payload=None, compacted_at=func.now())
        .execution_options(synchronize_session=False)
    )
    return result.rowcount or 0


# --------------------------------------------------------------------------- loop


async def run_compiler_loop(
    session_factory: SessionFactory,
    stop_event: asyncio.Event,
    *,
    scope: str | None = None,
    interval_seconds: float = 0.2,
    on_published: Callable[[str, int], None] | None = None,
    require_local_membership: bool,
) -> None:
    """Poll the outbox every `interval_seconds` and publish until `stop_event` is set.

    After each publish, calls `on_published(scope, version)` (e.g.
    `SessionRegistry.notify_published`) and compacts old payloads. Errors are logged and
    retried with backoff (up to 5 s); the loop only returns when stopped.
    """
    scope = scope or get_settings().scope
    router_plugins = plugin_api.load_router_plugins()
    backoff = interval_seconds
    while not stop_event.is_set():
        try:
            version = await publish_pending(
                session_factory,
                scope,
                router_plugins=router_plugins,
                require_local_membership=require_local_membership,
            )
            if version is not None:
                log.info("published config version %d for scope %s", version, scope)
                if on_published is not None:
                    on_published(scope, version)
                async with session_factory() as session, session.begin():
                    await compact_versions(session, scope)
            backoff = interval_seconds
        except DBAPIError as exc:
            if _serialization_failure(exc):
                log.warning(
                    "compiler serialization retries exhausted for scope %s; retrying", scope
                )
            else:
                log.exception("compiler loop step failed for scope %s", scope)
            backoff = min(backoff * 2, 5.0)
        except Exception:
            log.exception("compiler loop step failed for scope %s", scope)
            backoff = min(backoff * 2, 5.0)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=backoff)
        except TimeoutError:
            pass
