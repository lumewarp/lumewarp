"""Organizations: the grouping above teams.

The org write API (create/rename/archive, org admins, org quota) is later work. Two
operations exist now:

- `default_org` resolves the org a new team joins. `POST /teams` keeps its request shape,
  so the org cannot come from the payload: an org named `default` wins, and the first team
  creation creates it when no org exists yet (there is no seeding migration and no org
  write API). If the `default` org was renamed, the only live org is used; with several
  orgs and no `default` there is no safe answer, so team creation fails with a conflict
  instead of guessing. Archiving, which can change the live set, is not implemented yet.
`RequestAuthz` resolves team -> org from Postgres itself (never from a request payload),
so no caller passes an org.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane.errors import ApiError
from lumewarp_control_plane.models import Org

DEFAULT_ORG_NAME = "default"


async def live_orgs(session: AsyncSession) -> list[Org]:
    """Every non-archived org, by name."""
    return list(
        (
            await session.scalars(
                select(Org).where(Org.deleted_at.is_(None)).order_by(Org.name, Org.id)
            )
        ).all()
    )


async def default_org(session: AsyncSession) -> Org:
    """The org a new team joins, created on first use; see the module docstring."""
    rows = await live_orgs(session)
    for row in rows:
        if row.name == DEFAULT_ORG_NAME:
            return row
    if len(rows) == 1:
        return rows[0]
    if not rows:
        row = Org(name=DEFAULT_ORG_NAME)
        session.add(row)
        await session.flush()
        # Load the grants column property: `RowCount`-style lazy loads are not possible
        # from async code, and the caller validates the new team against this org.
        await session.refresh(row)
        return row
    raise ApiError(
        409,
        "conflict",
        "no default org: name one org 'default' (teams are created in the default org)",
    )
