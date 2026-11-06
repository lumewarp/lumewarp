"""Organizations: the grouping above teams.

`POST /teams` takes an explicit `org_id`; there is no implicit default org. `RequestAuthz`
resolves team -> org from Postgres itself (never from a request payload), so no caller
passes an org to an authorization check.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane.models import Org


async def live_orgs(session: AsyncSession) -> list[Org]:
    """Every non-archived org, by name."""
    return list(
        (
            await session.scalars(
                select(Org).where(Org.deleted_at.is_(None)).order_by(Org.name, Org.id)
            )
        ).all()
    )
