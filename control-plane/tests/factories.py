"""Row builders for tests: users, teams, members, service accounts and keys.

All `make_*` helpers add the row to the session and flush it, so ids are populated.
"""

import itertools

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane.ids import uuid7
from lumewarp_control_plane.models import (
    Alias,
    ApiKey,
    CatalogKind,
    CatalogName,
    KeyOwnerKind,
    Model,
    PolicyGrant,
    Router,
    RouterFallback,
    RouterMember,
    Scope,
    ServiceAccount,
    Team,
    TeamMember,
    TeamRole,
    Upstream,
    UpstreamKind,
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


async def catalog_name(
    session: AsyncSession, name: str, kind: CatalogKind = CatalogKind.MODEL
) -> CatalogName:
    """The name's row, created if needed. A name made for a grant alone has no item yet, so
    the first item created under it decides its kind.
    """
    row = await session.scalar(select(CatalogName).where(CatalogName.name == name))
    if row is None:
        row = CatalogName(name=name, kind=kind)
        session.add(row)
    else:
        row.kind = kind
    await session.flush()
    return row


async def grant(
    session: AsyncSession, owner: Team | TeamMember | ServiceAccount, names: list[str]
) -> None:
    """Grant names to a policy owner, creating names that do not exist yet."""
    if isinstance(owner, Team):
        columns = {"team_id": owner.id}
    elif isinstance(owner, TeamMember):
        columns = {"team_id": owner.team_id, "user_id": owner.user_id}
    else:
        columns = {"team_id": owner.team_id, "service_account_id": owner.id}
    for name in names:
        row = await session.scalar(select(CatalogName).where(CatalogName.name == name))
        row = row or await catalog_name(session, name)
        session.add(PolicyGrant(name_id=row.id, **columns))
    await session.flush()
    await session.refresh(owner)


async def make_upstream(session: AsyncSession, name: str | None = None, **kw) -> Upstream:
    name = name or _unique("upstream")
    row = await session.scalar(select(Upstream).where(Upstream.name == name))
    if row is None:
        row = Upstream(
            name=name,
            kind=kw.pop("kind", UpstreamKind.VLLM),
            base_url=kw.pop("base_url", f"http://{name}/v1"),
            **kw,
        )
        session.add(row)
        await session.flush()
    return row


async def make_model(
    session: AsyncSession,
    name: str | None = None,
    upstream: Upstream | None = None,
    **kw,
) -> Model:
    upstream = upstream or await make_upstream(session)
    row = await catalog_name(session, name or _unique("model"))
    model = Model(
        name_id=row.id,
        upstream_id=upstream.id,
        upstream_model=kw.pop("upstream_model", f"{row.name}-name"),
        **kw,
    )
    session.add(model)
    await session.flush()
    await session.refresh(model)
    return model


async def make_router(
    session: AsyncSession,
    name: str | None = None,
    members: list[Model | tuple[Model, int]] = (),
    *,
    fallbacks: list[Model] = (),
    **kw,
) -> Router:
    """Members are models, or (model, weight) pairs."""
    row = await catalog_name(session, name or _unique("router"), CatalogKind.ROUTER)
    router = Router(name_id=row.id, **kw)
    session.add(router)
    await session.flush()
    for position, member in enumerate(members):
        model, weight = member if isinstance(member, tuple) else (member, 1)
        session.add(
            RouterMember(router_id=row.id, model_id=model.id, weight=weight, position=position)
        )
    for position, model in enumerate(fallbacks):
        session.add(RouterFallback(router_id=row.id, model_id=model.id, position=position))
    await session.flush()
    await session.refresh(router)
    return router


async def make_alias(session: AsyncSession, name: str, target: Model | Router) -> Alias:
    row = await catalog_name(session, name, CatalogKind.ALIAS)
    alias = Alias(name_id=row.id, target_name_id=target.id, target_kind=target.kind)
    session.add(alias)
    await session.flush()
    await session.refresh(alias)
    return alias


async def make_team(
    session: AsyncSession, name: str | None = None, *, allowed_models: list[str] = (), **policy
) -> Team:
    team = Team(name=name or _unique("team"), **policy)
    session.add(team)
    await session.flush()
    await grant(session, team, list(allowed_models))
    return team


async def make_member(
    session: AsyncSession, team: Team, user: User | None = None, *,
    role: TeamRole = TeamRole.MEMBER, allowed_models: list[str] = (), **policy,
) -> TeamMember:  # fmt: skip
    """Add `user` (a fresh one by default) to `team`; the user is `member.user_id`."""
    if user is None:
        user = await make_user(session)
    member = TeamMember(team_id=team.id, user_id=user.id, role=role, **policy)
    session.add(member)
    await session.flush()
    await grant(session, member, list(allowed_models))
    return member


async def make_service_account(
    session: AsyncSession,
    team: Team,
    name: str | None = None,
    *,
    allowed_models: list[str] = (),
    **policy,
) -> ServiceAccount:
    sa = ServiceAccount(team_id=team.id, name=name or _unique("sa"), **policy)
    session.add(sa)
    await session.flush()
    await grant(session, sa, list(allowed_models))
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
