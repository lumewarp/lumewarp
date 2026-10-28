"""Built-in authorization backend: `users.is_superuser` and the `team_members` table.

Lumewarp itself is the IAM here, so it manages grants (member endpoints are writable).
Each call opens its own short session and reads committed state; `RequestAuthz` caches the
answers for one API request.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select

from lumewarp_control_plane.authz import CheckItem, Decision, Relation, Resource
from lumewarp_control_plane.db import SessionFactory
from lumewarp_control_plane.models import Team, TeamMember, TeamRole, User


def _uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


class BuiltinBackend:
    manages_grants = True

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def check(self, user_id: str, relation: Relation, resource: Resource) -> Decision:
        return (await self.batch_check([(user_id, relation, resource)]))[0]

    async def batch_check(self, items: Sequence[CheckItem]) -> list[Decision]:
        users = {u for u, _, _ in items if _uuid(u) is not None}
        if not users:
            return [Decision.DENY] * len(items)
        user_ids = {uuid.UUID(u) for u in users}
        async with self._session_factory() as session:
            active = {
                row.id: row.is_superuser
                for row in await session.execute(
                    select(User.id, User.is_superuser).where(
                        User.id.in_(user_ids), User.is_active.is_(True)
                    )
                )
            }
            roles = (
                {
                    (row.user_id, row.team_id): row.role
                    for row in await session.execute(
                        select(TeamMember.user_id, TeamMember.team_id, TeamMember.role).where(
                            TeamMember.user_id.in_(active)
                        )
                    )
                }
                if active
                else {}
            )
        return [self._decide(active, roles, *item) for item in items]

    @staticmethod
    def _decide(
        active: dict[uuid.UUID, bool],
        roles: dict[tuple[uuid.UUID, uuid.UUID], TeamRole],
        user_id: str,
        relation: Relation,
        resource: Resource,
    ) -> Decision:
        user = _uuid(user_id)
        if user is None or user not in active:
            return Decision.DENY
        if active[user]:
            # The superuser holds every relation.
            return Decision.ALLOW
        if relation is Relation.SUPERUSER or resource.type != "team":
            return Decision.DENY
        team = _uuid(resource.id)
        role = roles.get((user, team)) if team is not None else None
        if role is None:
            return Decision.DENY
        if relation is Relation.ADMIN and role is not TeamRole.ADMIN:
            return Decision.DENY
        return Decision.ALLOW

    async def list_teams(self, user_id: str, relation: Relation) -> list[str]:
        user = _uuid(user_id)
        if user is None:
            return []
        stmt = (
            select(TeamMember.team_id)
            .join(User, User.id == TeamMember.user_id)
            .where(TeamMember.user_id == user, User.is_active.is_(True))
        )
        if relation is Relation.ADMIN:
            stmt = stmt.where(TeamMember.role == TeamRole.ADMIN)
        async with self._session_factory() as session:
            superuser = await session.scalar(
                select(User.is_superuser).where(User.id == user, User.is_active.is_(True))
            )
            if superuser:
                stmt = select(Team.id)
            return [str(t) for t in await session.scalars(stmt)]
