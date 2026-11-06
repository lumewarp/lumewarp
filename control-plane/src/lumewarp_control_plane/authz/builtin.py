"""Built-in authorization backend: the `org_admins` and `team_members` tables.

Lumewarp itself is the IAM here, so it manages grants (member and org-admin endpoints are
writable). Each call opens its own short session and reads committed state; `RequestAuthz`
caches the answers for one API request. Installation roles live in Postgres too, but they
are not a relation and are never routed through this backend.

The backend answers direct relations only: `admin` on an org or a team, and `member` on a
team. It knows nothing about which team belongs to which org, so an org admin is not a team
member here; `RequestAuthz` applies that inheritance itself.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Literal

from sqlalchemy import select

from lumewarp_control_plane.authz import (
    AuthzContext,
    CheckItem,
    Decision,
    Relation,
    Resource,
)
from lumewarp_control_plane.db import SessionFactory
from lumewarp_control_plane.models import OrgAdmin, TeamMember, TeamRole, User


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
                uid: False
                for uid in await session.scalars(
                    select(User.id).where(User.id.in_(user_ids), User.is_active.is_(True))
                )
            }
            roles: dict[tuple[uuid.UUID, uuid.UUID], TeamRole] = {}
            org_admins: set[tuple[uuid.UUID, uuid.UUID]] = set()
            if active:
                roles = {
                    (row.user_id, row.team_id): row.role
                    for row in await session.execute(
                        select(TeamMember.user_id, TeamMember.team_id, TeamMember.role).where(
                            TeamMember.user_id.in_(active)
                        )
                    )
                }
                # Keyed by user: one org admin in the batch must not grant anyone else.
                org_admins = {
                    (row.user_id, row.org_id)
                    for row in await session.execute(
                        select(OrgAdmin.user_id, OrgAdmin.org_id).where(
                            OrgAdmin.user_id.in_(active)
                        )
                    )
                }
        return [self._decide(active, roles, org_admins, *item) for item in items]

    @staticmethod
    def _decide(
        active: dict[uuid.UUID, bool],
        roles: dict[tuple[uuid.UUID, uuid.UUID], TeamRole],
        org_admins: set[tuple[uuid.UUID, uuid.UUID]],
        user_id: str,
        relation: Relation,
        resource: Resource,
    ) -> Decision:
        user = _uuid(user_id)
        if user is None or user not in active:
            return Decision.DENY
        target = _uuid(resource.id)
        if target is None:
            return Decision.DENY
        if resource.type == "org":
            # The only relation on an org is `admin`; `member` is not a team membership.
            if relation is Relation.ADMIN and (user, target) in org_admins:
                return Decision.ALLOW
            return Decision.DENY
        if resource.type != "team":
            return Decision.DENY
        role = roles.get((user, target))
        if role is None:
            return Decision.DENY
        if relation is Relation.ADMIN and role is not TeamRole.ADMIN:
            return Decision.DENY
        return Decision.ALLOW

    async def list_objects(
        self, user_id: str, relation: Relation, resource_type: Literal["org", "team"]
    ) -> list[str]:
        user = _uuid(user_id)
        if user is None:
            return []
        if resource_type == "org":
            if relation is not Relation.ADMIN:
                return []
            stmt = (
                select(OrgAdmin.org_id)
                .join(User, User.id == OrgAdmin.user_id)
                .where(OrgAdmin.user_id == user, User.is_active.is_(True))
            )
        else:
            stmt = (
                select(TeamMember.team_id)
                .join(User, User.id == TeamMember.user_id)
                .where(TeamMember.user_id == user, User.is_active.is_(True))
            )
            if relation is Relation.ADMIN:
                stmt = stmt.where(TeamMember.role == TeamRole.ADMIN)
        async with self._session_factory() as session:
            return [str(t) for t in await session.scalars(stmt)]


def create(context: AuthzContext) -> BuiltinBackend:
    """`lumewarp.authz` entry point factory."""
    if context.session_factory is None:
        raise RuntimeError("the builtin authz backend needs a session factory")
    return BuiltinBackend(context.session_factory)
