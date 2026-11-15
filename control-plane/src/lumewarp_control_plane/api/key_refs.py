"""One reference shape for every API key the API shows.

The request log, usage, audit, key listings and the Playground all name a key the same
way: its org, team, owner, name, prefix and id. The names come from the key's own team and
org rows, so they are only ever returned alongside a key the caller may already see; this
loader never widens a listing. `KeyRefOut` is defined with the other response shapes in
`schemas.py` and re-exported here.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane.api.schemas import KeyRefOut
from lumewarp_control_plane.models import ApiKey, Org, ServiceAccount, Team

__all__ = ["KeyRefOut", "key_refs"]


async def key_refs(
    session: AsyncSession, ids: Iterable[uuid.UUID | None]
) -> dict[uuid.UUID, KeyRefOut]:
    """Load the refs of `ids` in one query; unknown or null ids are omitted.

    The caller passes ids of keys it may already show; this only resolves their names. An
    empty set answers without touching the database.
    """
    wanted = {key_id for key_id in ids if key_id is not None}
    if not wanted:
        return {}
    rows = (
        await session.execute(
            select(ApiKey, Team.name, Org.id, Org.name, ServiceAccount.name)
            .join(Team, Team.id == ApiKey.team_id)
            .outerjoin(Org, Org.id == Team.org_id)
            .outerjoin(ServiceAccount, ServiceAccount.id == ApiKey.service_account_id)
            .where(ApiKey.id.in_(wanted))
        )
    ).all()
    return {
        key.id: KeyRefOut(
            id=key.id,
            name=key.name,
            display_prefix=key.display_prefix,
            team_id=key.team_id,
            team_name=team_name,
            org_id=org_id,
            org_name=org_name,
            owner_kind=key.owner_kind,
            user_id=key.user_id,
            service_account_id=key.service_account_id,
            service_account_name=account_name,
            lifecycle=key.lifecycle,
        )
        for key, team_name, org_id, org_name, account_name in rows
    }
