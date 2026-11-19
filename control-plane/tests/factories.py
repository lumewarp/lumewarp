"""Row builders for tests: users, teams, members, service accounts and keys.

All `make_*` helpers add the row to the session and flush it, so ids are populated.
"""

import itertools

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane.ids import uuid7
from lumewarp_control_plane.mcp import hashing
from lumewarp_control_plane.models import (
    A2aAgent,
    A2aAgentGrant,
    Alias,
    ApiKey,
    CatalogKind,
    CatalogName,
    KeyOwnerKind,
    McpDefinition,
    McpDefinitionKind,
    McpDefinitionState,
    McpReviewMode,
    McpServer,
    McpServerGrant,
    MemberPolicy,
    Model,
    ModelDeployment,
    Org,
    OrgAdmin,
    PolicyGrant,
    Router,
    RouterFallback,
    RouterMember,
    ServiceAccount,
    Team,
    TeamMember,
    TeamRole,
    Upstream,
    UpstreamKind,
    User,
    Zone,
    ZoneLimit,
    ZoneMemberDefaults,
)
from lumewarp_control_plane.security import SecretKind, generate_secret

# The config zone rows are built in. API tests point this at their own zone row
# (tests/api/conftest.py); compiler tests use the `default` zone they seed.
DEFAULT_ZONE = "default"

_seq = itertools.count(1)


def _unique(prefix: str) -> str:
    return f"{prefix}-{next(_seq)}-{uuid7().hex[-6:]}"


async def zone(session: AsyncSession, name: str = "default") -> Zone:
    """The zone row, created if absent (another fixture may have committed it)."""
    row = await session.get(Zone, name)
    if row is None:
        row = Zone(name=name)
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
    session: AsyncSession,
    owner: Org | Team | MemberPolicy | ServiceAccount,
    names: list[str],
) -> None:
    """Grant names to a policy owner, creating names that do not exist yet."""
    if isinstance(owner, Org):
        columns = {"org_id": owner.id}
    elif isinstance(owner, Team):
        columns = {"team_id": owner.id}
    elif isinstance(owner, MemberPolicy):
        columns = {"team_id": owner.team_id, "user_id": owner.user_id}
    else:
        columns = {"team_id": owner.team_id, "service_account_id": owner.id}
    for name in names:
        row = await session.scalar(select(CatalogName).where(CatalogName.name == name))
        row = row or await catalog_name(session, name)
        session.add(PolicyGrant(name_id=row.id, **columns))
    await session.flush()
    await session.refresh(owner)


async def ensure_zone(session: AsyncSession, name: str) -> None:
    """The zone row, created if the test did not seed it (an upstream/model needs one)."""
    if await session.get(Zone, name) is None:
        session.add(Zone(name=name))
        await session.flush()


async def make_upstream(
    session: AsyncSession, name: str | None = None, *, zone: str | None = None, **kw
) -> Upstream:
    zone = zone or DEFAULT_ZONE
    await ensure_zone(session, zone)
    name = name or _unique("upstream")
    row = await session.scalar(select(Upstream).where(Upstream.zone == zone, Upstream.name == name))
    if row is None:
        row = Upstream(
            name=name,
            zone=zone,
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
    *,
    zone: str | None = None,
    deploy: bool = True,
    **kw,
) -> Model:
    """A model definition, deployed in `zone` on `upstream` (a fresh one there by default)."""
    zone = zone or DEFAULT_ZONE
    await ensure_zone(session, zone)
    upstream = upstream or await make_upstream(session, zone=zone)
    upstream_model = kw.pop("upstream_model", None)
    row = await catalog_name(session, name or _unique("model"))
    model = Model(
        model_type=kw.pop("model_type", "llm"),
        name_id=row.id,
        **kw,
    )
    session.add(model)
    await session.flush()
    if deploy:
        session.add(
            ModelDeployment(
                name_id=model.id,
                zone=zone,
                upstream_id=upstream.id,
                upstream_model=upstream_model or f"{row.name}-name",
            )
        )
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


LIMIT_FIELDS = ("rpm_limit", "tpm_limit", "max_parallel", "budget_nano_usd")
MEMBER_DEFAULT_FIELDS = (
    "member_rpm_default",
    "member_tpm_default",
    "member_max_parallel_default",
)


def _limits(kw: dict) -> dict:
    """Pop the zone-limit fields from a factory kwargs dict."""
    return {name: kw.pop(name) for name in LIMIT_FIELDS if name in kw}


async def _zone_limits(session: AsyncSession, zone: str | None = None, **owner) -> None:
    limits = owner.pop("limits")
    if limits:
        session.add(ZoneLimit(zone=zone or DEFAULT_ZONE, **owner, **limits))
        await session.flush()


async def make_org(session: AsyncSession, name: str | None = None, **kw) -> Org:
    """An org. A named org is reused so a test can put several teams in one; an unnamed one
    is fresh (there is no implicit default org any more)."""
    zone = kw.pop("zone", None)
    await ensure_zone(session, zone or DEFAULT_ZONE)
    limits = _limits(kw)
    if name is None:
        row = Org(name=_unique("org"), **kw)
        session.add(row)
        await session.flush()
        await _zone_limits(session, zone, org_id=row.id, limits=limits)
        return row
    row = await session.scalar(select(Org).where(Org.name == name))
    if row is None:
        row = Org(name=name, **kw)
        session.add(row)
        await session.flush()
        await _zone_limits(session, zone, org_id=row.id, limits=limits)
    return row


async def make_org_admin(session: AsyncSession, org: Org, user: User | None = None) -> OrgAdmin:
    """Make `user` (a fresh one by default) an org admin; returns the grant row."""
    if user is None:
        user = await make_user(session)
    row = OrgAdmin(org_id=org.id, user_id=user.id)
    session.add(row)
    await session.flush()
    return row


async def make_team(
    session: AsyncSession,
    name: str | None = None,
    *,
    org: Org | None = None,
    allowed_models: list[str] = (),
    **policy,
) -> Team:
    org = org or await make_org(session)
    zone = policy.pop("zone", None)
    await ensure_zone(session, zone or DEFAULT_ZONE)
    limits = _limits(policy)
    defaults = {name: policy.pop(name) for name in MEMBER_DEFAULT_FIELDS if name in policy}
    team = Team(name=name or _unique("team"), org_id=org.id, **policy)
    session.add(team)
    await session.flush()
    await _zone_limits(session, zone, team_id=team.id, limits=limits)
    if defaults:
        session.add(
            ZoneMemberDefaults(
                zone=zone or DEFAULT_ZONE,
                team_id=team.id,
                rpm_limit=defaults.get("member_rpm_default"),
                tpm_limit=defaults.get("member_tpm_default"),
                max_parallel=defaults.get("member_max_parallel_default"),
            )
        )
        await session.flush()
    await grant(session, team, list(allowed_models))
    return team


async def make_member(
    session: AsyncSession, team: Team, user: User | None = None, *,
    role: TeamRole = TeamRole.MEMBER, allowed_models: list[str] = (), **policy,
) -> TeamMember:  # fmt: skip
    """Add `user` (a fresh one by default) to `team`; the user is `member.user_id`.

    Policy kwargs create the separate `member_policies` row (an exception policy); a
    membership alone has none, exactly like an external-backend member.
    """
    if user is None:
        user = await make_user(session)
    member = TeamMember(team_id=team.id, user_id=user.id, role=role)
    session.add(member)
    await session.flush()
    zone = policy.pop("zone", None)
    limits = _limits(policy)
    if policy or allowed_models or limits:
        row = MemberPolicy(team_id=team.id, user_id=user.id, **policy)
        session.add(row)
        await session.flush()
        await _zone_limits(session, zone, team_id=team.id, user_id=user.id, limits=limits)
        await grant(session, row, list(allowed_models))
    return member


async def make_service_account(
    session: AsyncSession,
    team: Team,
    name: str | None = None,
    *,
    allowed_models: list[str] = (),
    **policy,
) -> ServiceAccount:
    zone = policy.pop("zone", None)
    limits = _limits(policy)
    sa = ServiceAccount(team_id=team.id, name=name or _unique("sa"), **policy)
    session.add(sa)
    await session.flush()
    await _zone_limits(session, zone, team_id=team.id, service_account_id=sa.id, limits=limits)
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
        zone=kw.pop("zone", None) or DEFAULT_ZONE,
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
    await ensure_zone(session, key.zone)
    session.add(key)
    await session.flush()
    return key


# --------------------------------------------------------------------------- mcp / a2a


async def make_mcp_server(
    session: AsyncSession,
    name: str | None = None,
    *,
    definitions: list[McpDefinition] | None = None,
    **kw,
) -> McpServer:
    server = McpServer(
        name=name or _unique("server").replace("_", "-"),
        zone=kw.pop("zone", None) or DEFAULT_ZONE,
        url=kw.pop("url", f"http://{name or 'mcp'}.test/mcp"),
        review_mode=kw.pop("review_mode", McpReviewMode.TOFU),
        **kw,
    )
    session.add(server)
    await session.flush()
    for definition in definitions or ():
        definition.server_id = server.id
        session.add(definition)
    await session.flush()
    return server


def tool_definition(
    name: str = "echo",
    *,
    description: str = "Echoes text",
    schema: dict | None = None,
) -> tuple[McpDefinition, hashing.Observed]:
    """An unsaved tool definition row and the observation it must hash to."""
    observed = hashing.observe(
        McpDefinitionKind.TOOL,
        {"name": name, "description": description, "inputSchema": schema or {"type": "object"}},
    )
    assert observed is not None
    return new_definition(McpDefinitionKind.TOOL, observed), observed


def new_definition(
    kind: McpDefinitionKind,
    observed: hashing.Observed,
    *,
    state: McpDefinitionState = McpDefinitionState.APPROVED,
    server_id=None,
) -> McpDefinition:
    return McpDefinition(
        server_id=server_id,
        kind=kind,
        name=observed.name,
        hash=observed.hash,
        definition_json=observed.canonical_json,
        state=state,
    )


async def make_agent(session: AsyncSession, name: str | None = None, **kw) -> A2aAgent:
    agent = A2aAgent(
        name=name or _unique("agent").replace("_", "-"),
        zone=kw.pop("zone", None) or DEFAULT_ZONE,
        url=kw.pop("url", f"http://{name or 'agent'}.test/a2a"),
        **kw,
    )
    session.add(agent)
    await session.flush()
    return agent


def _target_owner(owner: Org | Team | MemberPolicy | ServiceAccount) -> dict:
    if isinstance(owner, Org):
        return {"org_id": owner.id}
    if isinstance(owner, Team):
        return {"team_id": owner.id}
    if isinstance(owner, MemberPolicy):
        return {"team_id": owner.team_id, "user_id": owner.user_id}
    return {"team_id": owner.team_id, "service_account_id": owner.id}


async def mcp_grant(
    session: AsyncSession,
    owner: Org | Team | MemberPolicy | ServiceAccount,
    server: McpServer | str,
    *,
    all_tools: bool = True,
    tools: list[str] = (),
) -> McpServerGrant:
    """Grant a server to one policy owner; `server` may be a name for a new server."""
    if isinstance(server, str):
        server = await make_mcp_server(session, server)
    row = McpServerGrant(
        server_id=server.id, all_tools=all_tools, tools=list(tools), **_target_owner(owner)
    )
    session.add(row)
    await session.flush()
    return row


async def agent_grant(
    session: AsyncSession,
    owner: Org | Team | MemberPolicy | ServiceAccount,
    agent: A2aAgent | str,
) -> A2aAgentGrant:
    if isinstance(agent, str):
        agent = await make_agent(session, agent)
    row = A2aAgentGrant(agent_id=agent.id, **_target_owner(owner))
    session.add(row)
    await session.flush()
    return row
