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
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, literal, or_, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from lumewarp_control_plane import outbox
from lumewarp_control_plane._proto.lumewarp.config.v1 import snapshot_pb2 as pb
from lumewarp_control_plane.compiler.catalog_state import (
    capture_catalog_state,
    restore_catalog_state,
    set_target_revision,
)
from lumewarp_control_plane.compiler.compile import CompileError, compile_scope
from lumewarp_control_plane.compiler.plugins import check_stored_routers, check_stored_targets
from lumewarp_control_plane.compiler.validate import SnapshotInvalid, validate_snapshot
from lumewarp_control_plane.db import SessionFactory
from lumewarp_control_plane.metrics import DISCARD, Metrics
from lumewarp_control_plane.models import (
    ConfigOutbox,
    ConfigVersion,
    ConfigVersionKind,
    GatewaySession,
    OutboxStatus,
    Scope,
)
from lumewarp_control_plane.security import Principal
from lumewarp_control_plane.settings import get_settings

log = logging.getLogger(__name__)

# NOTIFY channel with payload "<scope>:<version>", sent on commit of a new version.
PUBLISHED_CHANNEL = "lumewarp_config_published"
SERIALIZATION_ATTEMPTS = 5
# A disconnected gateway stays visible for a day, then its row goes (D10).
GATEWAY_SESSION_RETENTION = timedelta(hours=24)
# How often the compiler loop deletes expired gateway rows; the work is a single statement.
GATEWAY_SESSION_CLEANUP_SECONDS = 3600.0


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
            catalog_state=await capture_catalog_state(session),
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


@dataclass
class _CompileOutcome:
    """What one transaction attempt compiled; reported only once it has committed."""

    duration: float | None = None
    failed: bool = False


async def _publish_locked(
    session: AsyncSession,
    scope: str,
    now: datetime | None,
    require_local_membership: bool,
    outcome: _CompileOutcome,
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
    started = time.perf_counter()
    try:
        compiled = await compile_scope(
            session,
            scope,
            now=now,
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
                if (
                    hashlib.sha256(payload).digest() == previous.digest
                    and await capture_catalog_state(session) == previous.catalog_state
                ):
                    outcome.duration = time.perf_counter() - started
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
        outcome.duration = time.perf_counter() - started
    except CompileError as exc:
        outcome.duration = time.perf_counter() - started
        outcome.failed = True
        log.error("compile of scope %s failed: %s", scope, exc)
        # Only the class is stored: exception text may quote configuration or payloads.
        await _mark(session, scope, revision, OutboxStatus.FAILED, error=type(exc).__name__)
        return None
    await _mark(session, scope, revision, OutboxStatus.DONE, version=version)
    return version


async def publish_pending(
    session_factory: SessionFactory,
    scope: str | None = None,
    *,
    now: datetime | None = None,
    require_local_membership: bool,
    metrics: Metrics = DISCARD,
) -> int | None:
    """One step of the compiler loop for `scope` (default settings.scope).

    Under the scope's advisory lock, in one REPEATABLE READ transaction: if there are
    pending `config_outbox` rows, compile; if the source revision is newer and the
    compiled content changed, store a new `config_versions` row. Mark covered outbox
    rows done even when content is unchanged. Serialization failures retry the entire
    transaction with bounded full jitter and a fresh snapshot. A compile failure marks
    rows failed and keeps the previous version current; nothing partial is published.
    Returns the newly published version, or None if nothing was published.

    Compile duration and failures are recorded for the committed attempt only, and the
    `lumewarp_cp_config_version` gauge is set to a newly published version.
    """
    scope = scope or get_settings().scope
    async with _engine(session_factory).connect() as conn:
        await conn.execute(select(func.pg_advisory_lock(lock_key(scope))))
        await conn.commit()
        try:
            await conn.execution_options(isolation_level="REPEATABLE READ")
            for attempt in range(SERIALIZATION_ATTEMPTS):
                outcome = _CompileOutcome()
                try:
                    async with AsyncSession(bind=conn, expire_on_commit=False) as session:
                        async with session.begin():
                            version = await _publish_locked(
                                session,
                                scope,
                                now,
                                require_local_membership,
                                outcome,
                            )
                    if outcome.duration is not None:
                        metrics.compile_duration.observe(outcome.duration)
                    if outcome.failed:
                        metrics.compile_failures.inc()
                    if version is not None:
                        metrics.config_version.set(version)
                    return version
                except DBAPIError as exc:
                    if not _serialization_failure(exc) or attempt == SERIALIZATION_ATTEMPTS - 1:
                        raise
                    log.info("retrying serialization conflict for scope %s", scope)
                    await asyncio.sleep(random.uniform(0, min(0.01 * 2**attempt, 0.2)))
        finally:
            await conn.execute(select(func.pg_advisory_unlock(lock_key(scope))))
            await conn.commit()


# --------------------------------------------------------------------------- rollback


async def rollback_routing(
    session: AsyncSession,
    scope: str,
    to_version: int,
    *,
    now: datetime | None = None,
    require_local_membership: bool,
    force: bool = False,
    principal: Principal | None = None,
) -> int:
    """Publish a new version whose routing content (upstreams, models, aliases, routers,
    MCP servers, A2A agents) comes from `to_version` while keys, teams, policies and
    statuses come from current source state, so a revoked key is never revived. Returns the
    new version number.

    Runs in the caller's transaction (the caller writes the audit row and commits). The
    catalog source tables are restored by id from the version's `catalog_state`, so later
    compiles keep the rolled-back routing and grants keep following their names across
    renames. The published routing is exactly `to_version`'s.
    Gateways may have changed since `to_version`, so its routers meet the same rule as a
    router write (`force` included, audited as `principal`).
    Raises LookupError if `to_version` is unknown or compacted, CompileError if the
    resulting snapshot is invalid, PluginRejected if a router's strategy, an MCP server's
    auth plugin or an A2A agent's auth/middleware plugin cannot be saved.
    """
    # Lock order: advisory lock, then the scope row lock, before any source read or write.
    # Catalog writers take only the scope row lock, so this serializes with them and no
    # lock cycle can form between rollback and a rename, delete or upstream change.
    await session.execute(select(func.pg_advisory_xact_lock(lock_key(scope))))
    locked = await session.scalar(select(Scope.name).where(Scope.name == scope).with_for_update())
    if locked is None:
        raise LookupError(f"unknown config scope {scope!r}")
    row = await session.get(ConfigVersion, (scope, to_version))
    if row is None or row.payload is None or row.catalog_state is None:
        raise LookupError(f"config version {to_version} of scope {scope!r} is not retained")
    old = pb.Snapshot.FromString(row.payload)
    try:
        await restore_catalog_state(session, row.catalog_state)
    except ValueError as exc:
        raise CompileError(str(exc)) from exc
    # Under the locks above. The registry read takes none, so the order stays as documented.
    await check_stored_routers(session, scope, force=force, principal=principal, now=now)
    await check_stored_targets(session, scope, force=force, principal=principal, now=now)
    revision = await outbox.enqueue_recompile(session, scope, f"rollback to {to_version}")
    # The restored rows are what this rollback version publishes: their provenance is this
    # revision, not the historical one the sidecar carried (N1). Without this, a report from
    # the configuration being replaced (same id, older version) would pass the revision check
    # and could auto-approve the old upstream's definitions under the restored target.
    await set_target_revision(session, revision)
    compiled = await compile_scope(
        session,
        scope,
        now=now,
        require_local_membership=require_local_membership,
    )
    snap = compiled.snapshot
    # Definition reviews are current state, never versioned: keep what the compile of the
    # restored source just produced for each server, and put it back after the old
    # snapshot's entities replace the current ones (a rejected hash must not come back).
    definitions = {server.id: server.definitions for server in snap.mcp_servers}
    # MCP servers and agents are routing content too, restored from the old snapshot.
    for field in ("upstreams", "models", "aliases", "routers", "mcp_servers", "a2a_agents"):
        del getattr(snap, field)[:]
        getattr(snap, field).extend(getattr(old, field))
    for server in snap.mcp_servers:
        current = definitions.get(server.id)
        if current is not None:
            del server.definitions[:]
            server.definitions.extend(current)

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
        .values(payload=None, catalog_state=None, compacted_at=func.now())
        .execution_options(synchronize_session=False)
    )
    return result.rowcount or 0


# --------------------------------------------------------------------------- housekeeping


async def delete_stale_gateway_sessions(
    session: AsyncSession, scope: str, *, now: datetime | None = None
) -> int:
    """Delete the rows of gateways disconnected for more than a day (D10).

    A row outlives its stream so an offline gateway stays visible, but not forever: the
    compiler loop calls this. The gateway's plugin reports go with it (ON DELETE CASCADE).
    """
    cutoff = (now or datetime.now(UTC)) - GATEWAY_SESSION_RETENTION
    result = await session.execute(
        delete(GatewaySession).where(
            GatewaySession.scope == scope,
            GatewaySession.connected.is_(False),
            GatewaySession.disconnected_at < cutoff,
        )
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
    metrics: Metrics = DISCARD,
) -> None:
    """Poll the outbox every `interval_seconds` and publish until `stop_event` is set.

    After each publish, calls `on_published(scope, version)` (e.g.
    `SessionRegistry.notify_published`) and compacts old payloads. Once an hour it also
    deletes gateway rows that have been disconnected for a day. Errors are logged
    and retried with backoff (up to 5 s); the loop only returns when stopped.
    """
    scope = scope or get_settings().scope
    backoff = interval_seconds
    next_cleanup = 0.0
    while not stop_event.is_set():
        if time.monotonic() >= next_cleanup:
            next_cleanup = time.monotonic() + GATEWAY_SESSION_CLEANUP_SECONDS
            try:
                async with session_factory() as session, session.begin():
                    stale = await delete_stale_gateway_sessions(session, scope)
                if stale:
                    log.info("deleted %d stale gateway rows of scope %s", stale, scope)
            except Exception:
                log.warning("gateway cleanup failed for scope %s", scope, exc_info=True)
        try:
            version = await publish_pending(
                session_factory,
                scope,
                require_local_membership=require_local_membership,
                metrics=metrics,
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
