"""Config outbox: "recompile this scope" requests written inside mutating transactions."""

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane.models import ConfigOutbox, Scope

# NOTIFY channel; delivered on commit so the compiler loop can wake up without polling.
RECOMPILE_CHANNEL = "lumewarp_recompile"


async def enqueue_recompile(session: AsyncSession, scope: str, reason: str) -> int:
    """Record that `scope` must be recompiled, in the caller's transaction.

    Call this in every transaction that changes compiled source state (API writes, budget
    flips, rollover) together with the audit row, before commit. It bumps
    `scopes.source_revision`, which also row-locks the scope until commit, so concurrent
    writers serialize and each committed mutation has a distinct, increasing revision.
    Returns that revision. Raises LookupError if the scope row does not exist.
    """
    revision = (
        await session.execute(
            update(Scope)
            .where(Scope.name == scope)
            .values(source_revision=Scope.source_revision + 1)
            .returning(Scope.source_revision)
        )
    ).scalar_one_or_none()
    if revision is None:
        raise LookupError(f"unknown config scope {scope!r}")
    session.add(ConfigOutbox(scope=scope, reason=reason, source_revision=revision))
    await session.execute(select(func.pg_notify(RECOMPILE_CHANNEL, scope)))
    await session.flush()
    return revision
