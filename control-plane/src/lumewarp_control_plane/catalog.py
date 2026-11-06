"""Catalog names: the namespace shared by models, aliases and routers.

Everything refers to a name by id, so a rename only touches `catalog_names` (plus history).
"""

import uuid
from collections.abc import Iterable

from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane import audit
from lumewarp_control_plane.compiler.validate import RESERVED_ALIASES
from lumewarp_control_plane.models import (
    ActorType,
    Alias,
    CatalogKind,
    CatalogName,
    CatalogNameHistory,
    PolicyGrant,
    Router,
    RouterFallback,
    RouterMember,
    RouterSplit,
    RouterSplitMember,
)
from lumewarp_control_plane.security import Principal


class NameProblem(ValueError):
    """The name cannot be used (taken across kinds, or reserved)."""


async def _ensure_free(session: AsyncSession, name: str) -> None:
    if name in RESERVED_ALIASES:
        raise NameProblem(f"{name!r} is reserved")
    kind = await session.scalar(select(CatalogName.kind).where(CatalogName.name == name))
    if kind is not None:
        raise NameProblem(f"{name!r} is already used by a {kind.value}")


async def claim_name(session: AsyncSession, name: str, kind: CatalogKind) -> CatalogName:
    await _ensure_free(session, name)
    row = CatalogName(name=name, kind=kind)
    session.add(row)
    await session.flush()
    return row


async def rename(session: AsyncSession, name_id: uuid.UUID, new_name: str) -> str:
    """Rename the id and record the old name, which is then free for anyone. Returns it."""
    row = await session.get(CatalogName, name_id, populate_existing=True)
    assert row is not None
    await _ensure_free(session, new_name)
    old_name = row.name
    row.name = new_name
    session.add(CatalogNameHistory(name_id=row.id, kind=row.kind, old_name=old_name))
    # Flushed now: a caller may immediately claim the old name, and the unit of work would
    # otherwise order that INSERT before this UPDATE.
    await session.flush()
    return old_name


async def referrers(session: AsyncSession, name_id: uuid.UUID) -> list[str]:
    """What still points at this name, as sorted display strings; deletion needs none."""
    found: set[str] = {
        f"alias {name}"
        for name in await session.scalars(select(Alias.name).where(Alias.target_name_id == name_id))
    }
    found.update(
        f"router {name}"
        for name in await session.scalars(
            select(Router.name)
            .join(RouterMember, RouterMember.router_id == Router.name_id)
            .where(RouterMember.model_id == name_id)
        )
    )
    found.update(
        f"router {name}"
        for name in await session.scalars(
            select(Router.name)
            .join(RouterSplit, RouterSplit.router_id == Router.name_id)
            .join(RouterSplitMember, RouterSplitMember.split_id == RouterSplit.id)
            .where(RouterSplitMember.model_id == name_id)
        )
    )
    found.update(
        f"router {name} (fallback)"
        for name in await session.scalars(
            select(Router.name)
            .join(RouterFallback, RouterFallback.router_id == Router.name_id)
            .where(RouterFallback.model_id == name_id)
        )
    )
    return sorted(found)


async def audit_grant_removal(
    session: AsyncSession,
    names: Iterable[tuple[uuid.UUID, str]],
    *,
    principal: Principal | None = None,
) -> int:
    """Audit every grant on these (id, name) pairs; the caller deletes the names, which
    cascades to the grants. Without a principal the actor is the rollback.
    """
    actor = (
        {"principal": principal}
        if principal is not None
        else {"actor_type": ActorType.SYSTEM, "actor_id": "rollback"}
    )
    count = 0
    for name_id, name in names:
        grants = await session.execute(
            select(
                PolicyGrant.id,
                PolicyGrant.org_id,
                PolicyGrant.team_id,
                PolicyGrant.user_id,
                PolicyGrant.service_account_id,
            )
            .where(PolicyGrant.name_id == name_id)
            .order_by(PolicyGrant.id)
        )
        for grant in grants:
            count += 1
            audit.record(
                session,
                action="grant.delete",
                resource_type="grant",
                resource_id=grant.id,
                team_id=grant.team_id,
                details={
                    "name": name,
                    "name_id": name_id,
                    "org_id": grant.org_id,
                    "user_id": grant.user_id,
                    "service_account_id": grant.service_account_id,
                },
                **actor,
            )
    return count


async def delete_name(session: AsyncSession, name_id: uuid.UUID) -> None:
    """Delete the name; the kind row and the grants go with it (ON DELETE CASCADE)."""
    await session.execute(delete(CatalogName).where(CatalogName.id == name_id))


async def copy_grants(session: AsyncSession, source: uuid.UUID, target: uuid.UUID) -> int:
    """Give `target` every grant of `source`. Returns how many were copied."""
    grants = (
        await session.execute(
            select(
                PolicyGrant.org_id,
                PolicyGrant.team_id,
                PolicyGrant.user_id,
                PolicyGrant.service_account_id,
            ).where(PolicyGrant.name_id == source)
        )
    ).all()
    for org_id, team_id, user_id, service_account_id in grants:
        session.add(
            PolicyGrant(
                name_id=target,
                org_id=org_id,
                team_id=team_id,
                user_id=user_id,
                service_account_id=service_account_id,
            )
        )
    await session.flush()
    return len(grants)


async def record_renames(
    session: AsyncSession, renames: Iterable[tuple[uuid.UUID, CatalogKind, str]]
) -> None:
    """History for names a rollback changed; timestamps come from the database."""
    rows = [{"name_id": i, "kind": k, "old_name": n} for i, k, n in renames]
    if rows:
        await session.execute(insert(CatalogNameHistory), rows)


__all__ = [
    "NameProblem",
    "audit_grant_removal",
    "claim_name",
    "copy_grants",
    "delete_name",
    "record_renames",
    "referrers",
    "rename",
]
