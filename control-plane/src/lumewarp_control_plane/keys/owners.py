"""Who may own a user key in a team, and who administers a team or org by inheritance.

Administration is inherited downwards (installation admin -> every org -> every team; org
admin -> the org's teams), and so is the right to hold one's own keys: a user may own keys
in a team when they are a direct member, an installation admin, or an admin of the team's
org. The authorization backend still answers direct relations only; the inheritance is
applied here and in `RequestAuthz`, never in a backend.

Installation roles and team -> org are read from the caller's session, so a write sees its
own uncommitted changes (a membership or org admin just removed). With the built-in backend
(`manages_grants`) the direct relations are read from the session too, for the same reason;
an external backend is asked through `batch_check`.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane.authz import (
    Authorizer,
    CheckItem,
    Decision,
    Relation,
    org_resource,
    team_resource,
)
from lumewarp_control_plane.models import (
    InheritedFrom,
    InstallationAdmin,
    OrgAdmin,
    Team,
    TeamMember,
)

Pair = tuple[uuid.UUID, uuid.UUID]


async def _direct(
    session: AsyncSession,
    authorizer: Authorizer,
    relation: Relation,
    pairs: Sequence[Pair],
) -> list[Decision]:
    """Direct `relation` of (user, team) pairs for MEMBER, (user, org) pairs for ADMIN."""
    if not pairs:
        return []
    if authorizer.manages_grants:
        if relation is Relation.MEMBER:
            column = tuple_(TeamMember.user_id, TeamMember.team_id)
            stmt = select(TeamMember.user_id, TeamMember.team_id)
        else:
            column = tuple_(OrgAdmin.user_id, OrgAdmin.org_id)
            stmt = select(OrgAdmin.user_id, OrgAdmin.org_id)
        found = set((await session.execute(stmt.where(column.in_(list(set(pairs)))))).all())
        return [Decision.ALLOW if pair in found else Decision.DENY for pair in pairs]
    resource = team_resource if relation is Relation.MEMBER else org_resource
    items: list[CheckItem] = [(str(user), relation, resource(target)) for user, target in pairs]
    return await authorizer.batch_check(items)


async def key_owner_decisions(
    session: AsyncSession, authorizer: Authorizer, pairs: Sequence[Pair]
) -> list[Decision]:
    """For each (user, team): ALLOW when the user may own keys in the team.

    ERROR only when no source allowed it and at least one could not be asked; callers never
    treat ERROR as a deny (the API answers 503, background jobs skip the pair).
    """
    if not pairs:
        return []
    users = {user for user, _ in pairs}
    teams = {team for _, team in pairs}
    installation = set(
        await session.scalars(
            select(InstallationAdmin.user_id).where(InstallationAdmin.user_id.in_(users))
        )
    )
    team_org = {
        team_id: org_id
        for team_id, org_id in await session.execute(
            select(Team.id, Team.org_id).where(Team.id.in_(teams))
        )
    }
    result = [Decision.ALLOW if user in installation else Decision.DENY for user, _ in pairs]
    open_ = [i for i, decision in enumerate(result) if decision is not Decision.ALLOW]
    member = await _direct(session, authorizer, Relation.MEMBER, [pairs[i] for i in open_])
    for i, decision in zip(open_, member, strict=True):
        result[i] = decision
    open_ = [
        i
        for i, decision in enumerate(result)
        if decision is not Decision.ALLOW and pairs[i][1] in team_org
    ]
    org_pairs = [(pairs[i][0], team_org[pairs[i][1]]) for i in open_]
    org_admin = await _direct(session, authorizer, Relation.ADMIN, org_pairs)
    for i, decision in zip(open_, org_admin, strict=True):
        if decision is Decision.ALLOW or result[i] is Decision.DENY:
            result[i] = decision
    return result


async def inherited_admins(
    session: AsyncSession, org_id: uuid.UUID | None
) -> dict[uuid.UUID, InheritedFrom]:
    """Users who administer `org_id` (and so its teams) by inheritance, from local rows.

    Every installation admin, plus the org's local `org_admins` rows when `org_id` is given.
    An external backend cannot list who holds a relation, so its org admins appear only as
    far as local rows exist; they can still own keys, which is decided by `check`.
    """
    found = {
        user_id: InheritedFrom.INSTALLATION
        for user_id in await session.scalars(select(InstallationAdmin.user_id))
    }
    if org_id is not None:
        for user_id in await session.scalars(
            select(OrgAdmin.user_id).where(OrgAdmin.org_id == org_id)
        ):
            found.setdefault(user_id, InheritedFrom.ORG)
    return found
