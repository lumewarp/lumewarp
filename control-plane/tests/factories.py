"""Row builders for tests: users, teams, members, service accounts and keys.

All `make_*` helpers add the row to the session and flush it, so ids are populated.
"""

import itertools

from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane.ids import uuid7
from lumewarp_control_plane.models import (
    ApiKey,
    KeyOwnerKind,
    Scope,
    ServiceAccount,
    Team,
    TeamMember,
    TeamRole,
    User,
)
from lumewarp_control_plane.security import SecretKind, generate_secret

_seq = itertools.count(1)


def _unique(prefix: str) -> str:
    return f"{prefix}-{next(_seq)}-{uuid7().hex[-6:]}"


async def scope(session: AsyncSession, name: str = "default") -> Scope:
    row = Scope(name=name)
    session.add(row)
    await session.flush()
    return row


async def make_user(
    session: AsyncSession, email: str | None = None, *,
    is_active: bool = True, **kw,
) -> User:  # fmt: skip
    user = User(
        email=email or f"{_unique('user')}@example.com",
        is_active=is_active,
        **kw,
    )
    session.add(user)
    await session.flush()
    return user


async def make_team(session: AsyncSession, name: str | None = None, **policy) -> Team:
    team = Team(name=name or _unique("team"), **policy)
    session.add(team)
    await session.flush()
    return team


async def make_member(
    session: AsyncSession, team: Team, user: User | None = None, *,
    role: TeamRole = TeamRole.MEMBER, **policy,
) -> TeamMember:  # fmt: skip
    """Add `user` (a fresh one by default) to `team`; the user is `member.user_id`."""
    if user is None:
        user = await make_user(session)
    member = TeamMember(team_id=team.id, user_id=user.id, role=role, **policy)
    session.add(member)
    await session.flush()
    return member


async def make_service_account(
    session: AsyncSession, team: Team, name: str | None = None, **policy
) -> ServiceAccount:
    sa = ServiceAccount(team_id=team.id, name=name or _unique("sa"), **policy)
    session.add(sa)
    await session.flush()
    return sa


def new_key(team: Team, owner: User | ServiceAccount, **kw) -> ApiKey:
    """An unsaved key of `team` owned by a user or a service account."""
    secret = generate_secret(SecretKind.API_KEY)
    owner_cols = (
        {"owner_kind": KeyOwnerKind.USER, "user_id": owner.id}
        if isinstance(owner, User)
        else {"owner_kind": KeyOwnerKind.SERVICE_ACCOUNT, "service_account_id": owner.id}
    )
    return ApiKey(
        team_id=team.id,
        name=kw.pop("name", "key"),
        key_hash=kw.pop("key_hash", secret.hash),
        display_prefix=secret.display_prefix,
        **owner_cols,
        **kw,
    )


async def make_key(session: AsyncSession, team: Team, owner: User | ServiceAccount, **kw) -> ApiKey:
    """A key of `team`. A user owner is not required to be a member (the compiler
    suspends such keys), so tests can build that state.
    """
    key = new_key(team, owner, **kw)
    session.add(key)
    await session.flush()
    return key
