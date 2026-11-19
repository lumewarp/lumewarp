"""Config outbox: "recompile this zone" requests written inside mutating transactions."""

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane.models import ConfigOutbox, Zone

# NOTIFY channel; delivered on commit so the compiler loop can wake up without polling.
RECOMPILE_CHANNEL = "lumewarp_recompile"

# One transaction-level advisory lock serializes every hierarchy mutation (orgs, teams,
# members, names, grants, policies), whichever zone a write ends up touching.
HIERARCHY_LOCK_KEY = 0x4C57_4849_4552


async def lock_hierarchy(session: AsyncSession) -> None:
    """Serialize hierarchy mutations for the rest of the transaction (no zone row needed)."""
    await session.execute(select(func.pg_advisory_xact_lock(HIERARCHY_LOCK_KEY)))


async def enqueue_recompile(session: AsyncSession, reason: str, zone: str | None = None) -> int:
    """Record that a zone must be recompiled, in the caller's transaction.

    Call this in every transaction that changes compiled source state (API writes, budget
    flips, rollover) together with the audit row, before commit. `zone=None` means a
    hierarchy-wide write (orgs, teams, members, names, grants): every live zone is bumped
    and gets its own outbox row. A named zone is a zone-bound write.

    A hierarchy-wide write gives every live zone the same revision (one above the highest
    live zone); a zone-bound write increments its own row. Both take the hierarchy lock:
    the fan-out's max is read from the statement snapshot, so a concurrent zone-bound bump
    could otherwise end equal to the fan-out's revision and the compiler would treat the
    hierarchy change as already published. Returns the revision; raises LookupError when
    no live zone matches.
    """
    await lock_hierarchy(session)
    if zone is None:
        next_revision = select(func.max(Zone.source_revision) + 1).where(Zone.archived_at.is_(None))
        stmt = (
            update(Zone)
            .where(Zone.archived_at.is_(None))
            .values(source_revision=next_revision.scalar_subquery())
            .returning(Zone.id, Zone.source_revision)
        )
    else:
        stmt = (
            update(Zone)
            .where(Zone.archived_at.is_(None), Zone.id == zone)
            .values(source_revision=Zone.source_revision + 1)
            .returning(Zone.id, Zone.source_revision)
        )
    rows = (await session.execute(stmt)).all()
    if not rows:
        raise LookupError(f"unknown config zone {zone!r}")
    session.add_all(
        ConfigOutbox(zone=name, reason=reason, source_revision=revision) for name, revision in rows
    )
    for name, _revision in rows:
        await session.execute(select(func.pg_notify(RECOMPILE_CHANNEL, name)))
    await session.flush()
    return rows[0][1]
