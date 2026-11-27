"""Policy inheritance and narrowing (O1 M1/M5, four-level quota hierarchy).

The resolver lives in `quota.py`; this module is the write-side enforcement. A write may
only narrow its parent: a *changed* value may not exceed the parent's current effective
value (422). A value the write leaves unchanged is not rechecked, so a parent decrease
never invalidates child rows and a later unrelated child edit still succeeds. A parent
decrease itself always succeeds; the effective value is the minimum down the chain.

The compiler intersects allowlists defensively too, but accepting a non-subset in the
source would make the UI misleading and could turn an explicit deny-all into an
accidental grant after a later edit.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import TYPE_CHECKING

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane.errors import ApiError
from lumewarp_control_plane.models import (
    A2aAgent,
    A2aAgentGrant,
    ApiKey,
    CatalogName,
    McpDefinition,
    McpDefinitionKind,
    McpServer,
    McpServerGrant,
    MemberPolicy,
    ModelAccess,
    ModelGroup,
    ModelGroupMember,
    Org,
    PolicyGrant,
    PolicyGroupGrant,
    ServiceAccount,
    Team,
    ZoneLimit,
    ZoneMemberDefaults,
)
from lumewarp_control_plane.quota import (
    Effective,
    McpGrant,
    Policy,
    apply_policy,
    effective,
    org_effective,
    owner_effective,
    policy_of,
    team_effective,
)

if TYPE_CHECKING:
    from lumewarp_control_plane.api.schemas import PolicyFields

__all__ = [
    "Effective",
    "Policy",
    "apply_policy",
    "effective",
    "ensure_unique_ids",
    "expand_groups",
    "grant_ids",
    "group_ids",
    "mcp_grant_ids",
    "org_effective",
    "owner_effective_of",
    "policy_from_fields",
    "policy_of",
    "save_grants",
    "save_model_grants",
    "team_effective_of",
    "validate_key_models",
    "validate_owner_policy",
    "validate_team_policy",
]


def violation(field: str, message: str) -> ApiError:
    return ApiError(422, "validation_error", f"{field}: {message}")


async def grant_ids(session: AsyncSession, names: tuple[str, ...]) -> dict[str, uuid.UUID]:
    """Current id of every name; 422 listing the names that do not exist."""
    if not names:
        return {}
    rows = await session.execute(
        select(CatalogName.name, CatalogName.id).where(CatalogName.name.in_(names))
    )
    found = {name: name_id for name, name_id in rows}
    missing = sorted(set(names) - set(found))
    if missing:
        raise violation("allowed_models", f"unknown names: {', '.join(missing)}")
    return found


async def group_ids(session: AsyncSession, names: tuple[str, ...]) -> dict[str, uuid.UUID]:
    """Model group name -> id; 422 listing the groups that do not exist."""
    if not names:
        return {}
    rows = await session.execute(
        select(ModelGroup.name, ModelGroup.id).where(ModelGroup.name.in_(names))
    )
    found = {name: group_id for name, group_id in rows}
    missing = sorted(set(names) - set(found))
    if missing:
        raise violation("allowed_groups", f"unknown groups: {', '.join(missing)}")
    return found


async def expand_groups(session: AsyncSession, policy: Policy) -> Policy:
    """The policy with `group_models` set to the current members of its groups.

    422 for an unknown group. The narrowing checks run on the expanded set
    (`Policy.granted_models`), exactly what the compiler will intersect.
    """
    ids = await group_ids(session, policy.allowed_groups)
    if not ids:
        return replace(policy, group_models=())
    members = await session.scalars(
        select(CatalogName.name)
        .join(ModelGroupMember, ModelGroupMember.name_id == CatalogName.id)
        .where(ModelGroupMember.group_id.in_(ids.values()))
        .distinct()
    )
    return replace(policy, group_models=tuple(sorted(members)))


async def mcp_grant_ids(
    session: AsyncSession, grants: tuple[McpGrant, ...]
) -> dict[str, uuid.UUID]:
    """Server name -> id for a proposed MCP grant list; 422 for unknown or archived servers.

    Every named tool must be a known tool definition of its server (`mcp_definitions`), any
    state: the control plane is the only place the tool list exists (`docs/plugins.md`,
    "Catalog reports"), so a grant is always written against names it has seen. A tool
    granted while pending becomes usable when it is approved, without rewriting the grant.
    """
    if not grants:
        return {}
    names = sorted({grant.server for grant in grants if grant.server})
    rows = await session.execute(
        select(McpServer.name, McpServer.id).where(
            McpServer.name.in_(names), McpServer.deleted_at.is_(None)
        )
    )
    found = {name: server_id for name, server_id in rows}
    missing = sorted(set(names) - set(found))
    if missing:
        raise violation("mcp_servers", f"unknown servers: {', '.join(missing)}")
    known: dict[uuid.UUID, set[str]] = {}
    for server_id, tool in await session.execute(
        select(McpDefinition.server_id, McpDefinition.name).where(
            McpDefinition.server_id.in_(found.values()),
            McpDefinition.kind == McpDefinitionKind.TOOL,
        )
    ):
        known.setdefault(server_id, set()).add(tool)
    unknown: list[str] = []
    for grant in grants:
        if grant.all_tools:
            continue
        for tool in grant.tools:
            if tool not in known.get(found[grant.server], set()):
                unknown.append(f"{grant.server}/{tool}")
    if unknown:
        raise violation("mcp_servers", f"unknown tools: {', '.join(sorted(unknown))}")
    return found


async def agent_grant_ids(session: AsyncSession, names: tuple[str, ...]) -> dict[str, uuid.UUID]:
    """Agent name -> id; 422 listing the names that do not exist (or are archived)."""
    if not names:
        return {}
    rows = await session.execute(
        select(A2aAgent.name, A2aAgent.id).where(
            A2aAgent.name.in_(names), A2aAgent.deleted_at.is_(None)
        )
    )
    found = {name: agent_id for name, agent_id in rows}
    missing = sorted(set(names) - set(found))
    if missing:
        raise violation("a2a_agents", f"unknown agents: {', '.join(missing)}")
    return found


def _owner(row: Org | Team | MemberPolicy | ServiceAccount) -> dict[str, uuid.UUID | None]:
    if isinstance(row, Org):
        return {"org_id": row.id, "team_id": None, "user_id": None, "service_account_id": None}
    if isinstance(row, Team):
        return {"org_id": None, "team_id": row.id, "user_id": None, "service_account_id": None}
    if isinstance(row, MemberPolicy):
        return {
            "org_id": None,
            "team_id": row.team_id,
            "user_id": row.user_id,
            "service_account_id": None,
        }
    return {
        "org_id": None,
        "team_id": row.team_id,
        "user_id": None,
        "service_account_id": row.id,
    }


def _model_owner(
    row: Org | Team | MemberPolicy | ServiceAccount | ApiKey,
) -> dict[str, uuid.UUID | None]:
    """`_owner` plus the key shape, for `policy_grants` and `policy_group_grants`."""
    if isinstance(row, ApiKey):
        return {
            "org_id": None,
            "team_id": None,
            "user_id": None,
            "service_account_id": None,
            "key_id": row.id,
        }
    return {**_owner(row), "key_id": None}


async def save_model_grants(
    session: AsyncSession,
    row: Org | Team | MemberPolicy | ServiceAccount | ApiKey,
    policy: Policy,
) -> None:
    """Replace the row's direct model grants and group grants (422 for unknown names)."""
    ids = await grant_ids(session, policy.allowed_models)
    groups = await group_ids(session, policy.allowed_groups)
    owner = _model_owner(row)
    for grant_model in (PolicyGrant, PolicyGroupGrant):
        await session.execute(
            delete(grant_model).where(
                *(
                    column.is_(None) if value is None else column == value
                    for column, value in (
                        (grant_model.org_id, owner["org_id"]),
                        (grant_model.team_id, owner["team_id"]),
                        (grant_model.user_id, owner["user_id"]),
                        (grant_model.service_account_id, owner["service_account_id"]),
                        (grant_model.key_id, owner["key_id"]),
                    )
                )
            )
        )
    session.add_all(PolicyGrant(name_id=name_id, **owner) for name_id in ids.values())
    session.add_all(PolicyGroupGrant(group_id=group_id, **owner) for group_id in groups.values())
    await session.flush()


async def save_grants(
    session: AsyncSession, row: Org | Team | MemberPolicy | ServiceAccount, policy: Policy
) -> None:
    """Replace the row's grants. The row must be flushed; refresh it to read them back."""
    await save_model_grants(session, row, policy)
    await save_mcp_grants(session, row, policy)
    await save_agent_grants(session, row, policy)
    await session.flush()


async def _replace_target_grants(session, grant_model, owner: dict) -> None:
    """Delete one owner's rows of a target grant table (`mcp_server_grants` or
    `a2a_agent_grants`), matching exactly the four owner shapes `PolicyGrant` uses.
    """
    await session.execute(
        delete(grant_model).where(
            *(
                column.is_(None) if value is None else column == value
                for column, value in (
                    (grant_model.org_id, owner["org_id"]),
                    (grant_model.team_id, owner["team_id"]),
                    (grant_model.user_id, owner["user_id"]),
                    (grant_model.service_account_id, owner["service_account_id"]),
                )
            )
        )
    )


async def save_mcp_grants(
    session: AsyncSession, row: Org | Team | MemberPolicy | ServiceAccount, policy: Policy
) -> None:
    """Replace the row's MCP server grants (cleared when the level is not LIST)."""
    owner = _owner(row)
    await _replace_target_grants(session, McpServerGrant, owner)
    if policy.mcp_access is not ModelAccess.LIST:
        return
    ids = await mcp_grant_ids(session, policy.mcp_grants)
    for grant in policy.mcp_grants:
        session.add(
            McpServerGrant(
                server_id=ids[grant.server],
                all_tools=grant.all_tools,
                tools=[] if grant.all_tools else sorted(grant.tools),
                **owner,
            )
        )
    await session.flush()


async def save_agent_grants(
    session: AsyncSession, row: Org | Team | MemberPolicy | ServiceAccount, policy: Policy
) -> None:
    """Replace the row's A2A agent grants (cleared when the level is not LIST)."""
    owner = _owner(row)
    await _replace_target_grants(session, A2aAgentGrant, owner)
    if policy.a2a_access is not ModelAccess.LIST:
        return
    ids = await agent_grant_ids(session, policy.a2a_agents)
    session.add_all(A2aAgentGrant(agent_id=agent_id, **owner) for agent_id in ids.values())
    await session.flush()


def _mcp_grants(fields: PolicyFields) -> tuple[McpGrant, ...]:
    """The submitted MCP grants, server-ordered and tool-sorted (a level's own list)."""
    return tuple(
        McpGrant(
            server=grant.server,
            all_tools=grant.all_tools,
            tools=() if grant.all_tools else tuple(sorted(set(grant.tools))),
        )
        for grant in sorted(fields.mcp_servers or (), key=lambda g: g.server)
    )


def _access(value: ModelAccess, name: str, default_mode: ModelAccess) -> ModelAccess:
    mode = value
    if mode is ModelAccess.INHERIT and default_mode is ModelAccess.ALL:
        raise violation(name, "teams and orgs cannot inherit")
    return mode


def policy_from_fields(
    fields: PolicyFields, *, default_mode: ModelAccess, base: Policy | None = None
) -> Policy:
    """Build a proposed policy from a create/update payload.

    On create (`base is None`) omitted fields take `default_mode`/no limit. On update,
    omitted fields keep `base`; an explicit null clears a limit or budget. Teams and orgs
    pass `default_mode=ALL` and cannot inherit; members and service accounts pass INHERIT.
    The MCP and A2A halves follow the model half exactly.
    """
    supplied = fields.model_fields_set
    if base is None:
        mode = fields.model_access if "model_access" in supplied else default_mode
        mode = _access(mode, "model_access", default_mode)
        if fields.allowed_models and mode is not ModelAccess.LIST:
            raise violation("allowed_models", "only allowed when model_access is 'list'")
        allowed = tuple(sorted(fields.allowed_models or ())) if mode is ModelAccess.LIST else ()
        if fields.allowed_groups and mode is not ModelAccess.LIST:
            raise violation("allowed_groups", "only allowed when model_access is 'list'")
        groups = tuple(sorted(fields.allowed_groups or ())) if mode is ModelAccess.LIST else ()
        mcp_access = fields.mcp_access if "mcp_access" in supplied else default_mode
        mcp_access = _access(mcp_access, "mcp_access", default_mode)
        if fields.mcp_servers and mcp_access is not ModelAccess.LIST:
            raise violation("mcp_servers", "only allowed when mcp_access is 'list'")
        a2a_access = fields.a2a_access if "a2a_access" in supplied else default_mode
        a2a_access = _access(a2a_access, "a2a_access", default_mode)
        if fields.a2a_agents and a2a_access is not ModelAccess.LIST:
            raise violation("a2a_agents", "only allowed when a2a_access is 'list'")
        return Policy(
            model_access=mode,
            allowed_models=allowed,
            allowed_groups=groups,
            mcp_access=mcp_access,
            mcp_grants=_mcp_grants(fields) if mcp_access is ModelAccess.LIST else (),
            a2a_access=a2a_access,
            a2a_agents=(
                tuple(sorted(fields.a2a_agents or ())) if a2a_access is ModelAccess.LIST else ()
            ),
        )

    mode = fields.model_access if "model_access" in supplied else base.model_access
    mode = _access(mode, "model_access", default_mode)
    if "allowed_models" in supplied:
        if mode is not ModelAccess.LIST and fields.allowed_models:
            raise violation("allowed_models", "only allowed when model_access is 'list'")
        allowed = tuple(sorted(fields.allowed_models or ()))
    elif mode is ModelAccess.LIST:
        allowed = base.allowed_models if base.model_access is ModelAccess.LIST else ()
    else:
        allowed = ()
    if "allowed_groups" in supplied:
        if mode is not ModelAccess.LIST and fields.allowed_groups:
            raise violation("allowed_groups", "only allowed when model_access is 'list'")
        groups = tuple(sorted(fields.allowed_groups or ()))
    elif mode is ModelAccess.LIST:
        groups = base.allowed_groups if base.model_access is ModelAccess.LIST else ()
    else:
        groups = ()
    if mode is not ModelAccess.LIST:
        groups = ()

    mcp_access = fields.mcp_access if "mcp_access" in supplied else base.mcp_access
    mcp_access = _access(mcp_access, "mcp_access", default_mode)
    if "mcp_servers" in supplied:
        if mcp_access is not ModelAccess.LIST and fields.mcp_servers:
            raise violation("mcp_servers", "only allowed when mcp_access is 'list'")
        mcp_grants = _mcp_grants(fields) if mcp_access is ModelAccess.LIST else ()
    elif mcp_access is ModelAccess.LIST:
        mcp_grants = base.mcp_grants if base.mcp_access is ModelAccess.LIST else ()
    else:
        mcp_grants = ()

    a2a_access = fields.a2a_access if "a2a_access" in supplied else base.a2a_access
    a2a_access = _access(a2a_access, "a2a_access", default_mode)
    if "a2a_agents" in supplied:
        if a2a_access is not ModelAccess.LIST and fields.a2a_agents:
            raise violation("a2a_agents", "only allowed when a2a_access is 'list'")
        a2a_agents = tuple(sorted(fields.a2a_agents or ()))
    elif a2a_access is ModelAccess.LIST:
        a2a_agents = base.a2a_agents if base.a2a_access is ModelAccess.LIST else ()
    else:
        a2a_agents = ()

    return Policy(
        model_access=mode,
        allowed_models=allowed,
        allowed_groups=groups,
        # The zone limit row is not part of `PolicyFields`: a write that also changes
        # limits merges them in its own transaction (`api.zone_limits`).
        rpm_limit=base.rpm_limit,
        tpm_limit=base.tpm_limit,
        max_parallel=base.max_parallel,
        budget_nano_usd=base.budget_nano_usd,
        mcp_access=mcp_access,
        mcp_grants=mcp_grants,
        a2a_access=a2a_access,
        a2a_agents=a2a_agents,
    )


def _field(prefix: str):
    return (lambda name: f"{prefix}.{name}") if prefix else (lambda name: name)


def check_narrowing(
    parent: Effective,
    policy: Policy,
    prefix: str = "",
    *,
    check_models: bool = True,
    base: Policy | None = None,
) -> None:
    """Raise 422 if a changed value widens a limit or (with `check_models`) a model list.

    ALL below the root is never a widening: like INHERIT it resolves to the parent's set
    (`quota.effective`, the compiler's `narrow()`), so a team at ALL under a restricted
    org is simply "everything the org allows".

    `base` is the stored policy of the same level: fields it leaves unchanged are never
    rechecked, so a parent that shrank below them is legitimate. On create (`base is
    None`) every supplied value counts as changed.
    """
    field = _field(prefix)

    def changed(name: str) -> bool:
        if base is None:
            return True
        return getattr(base, name) != getattr(policy, name)

    if check_models and (
        changed("allowed_models") or changed("allowed_groups") or changed("model_access")
    ):
        # Groups are expanded first (`expand_groups`): a group whose members reach outside
        # the parent is refused like the same names listed one by one.
        if policy.model_access is ModelAccess.LIST and parent.models is not None:
            own = set(policy.granted_models)
            if not own <= set(parent.models):
                extra = sorted(own - set(parent.models))
                raise violation(
                    field("allowed_models"),
                    f"models not allowed by parent: {', '.join(extra)}",
                )
    if changed("mcp_access") or changed("mcp_grants"):
        if policy.mcp_access is ModelAccess.LIST and parent.mcp is not None:
            for grant in policy.mcp_grants:
                if grant.server not in parent.mcp:
                    raise violation(
                        field("mcp_servers"),
                        f"servers not allowed by parent: {grant.server}",
                    )
                parent_tools = parent.mcp[grant.server]
                if parent_tools is None:
                    continue
                if grant.all_tools:
                    raise violation(
                        field("mcp_servers"),
                        f"tools of {grant.server!r} are restricted by the parent; name them",
                    )
                extra = sorted(set(grant.tools) - parent_tools)
                if extra:
                    raise violation(
                        field("mcp_servers"),
                        f"tools of {grant.server!r} not allowed by parent: {', '.join(extra)}",
                    )
    if changed("a2a_access") or changed("a2a_agents"):
        if policy.a2a_access is ModelAccess.LIST and parent.agents is not None:
            own = set(policy.a2a_agents)
            if not own <= set(parent.agents):
                extra = sorted(own - set(parent.agents))
                raise violation(
                    field("a2a_agents"),
                    f"agents not allowed by parent: {', '.join(extra)}",
                )
    for name, parent_value, own_value in (
        ("rpm_limit", parent.rpm_limit, policy.rpm_limit),
        ("tpm_limit", parent.tpm_limit, policy.tpm_limit),
        ("max_parallel", parent.max_parallel, policy.max_parallel),
        ("budget_nano_usd", parent.budget_nano_usd, policy.budget_nano_usd),
    ):
        if not changed(name):
            continue
        if parent_value is not None and own_value is not None and own_value > parent_value:
            raise violation(
                field(name),
                f"must be <= parent value {parent_value} (got {own_value})",
            )


async def _org_of(session: AsyncSession, team: Team) -> Org:
    org = await session.get(Org, team.org_id)
    if org is None:
        raise violation("org_id", "team has no existing org")
    return org


# ------------------------------------------------------------------ per-zone limits


def _owner_values(
    owner: dict[str, uuid.UUID | None],
) -> dict[str, uuid.UUID | None]:
    """The four zone-limit owner columns, dropping the key shape."""
    return {
        "org_id": owner.get("org_id"),
        "team_id": owner.get("team_id"),
        "user_id": owner.get("user_id"),
        "service_account_id": owner.get("service_account_id"),
    }


def owner_of(
    row: Org | Team | MemberPolicy | ServiceAccount,
) -> dict[str, uuid.UUID | None]:
    """The zone-limit owner shape of a policy row."""
    return _owner_values(_owner(row))


async def zone_limits_of(
    session: AsyncSession, zone: str, owner: dict[str, uuid.UUID | None]
) -> ZoneLimit | None:
    """The owner's limit row in one zone, or None."""
    values = _owner_values(owner)
    return await session.scalar(
        select(ZoneLimit).where(
            ZoneLimit.zone == zone,
            *(
                column.is_(None) if value is None else column == value
                for column, value in (
                    (ZoneLimit.org_id, values["org_id"]),
                    (ZoneLimit.team_id, values["team_id"]),
                    (ZoneLimit.user_id, values["user_id"]),
                    (ZoneLimit.service_account_id, values["service_account_id"]),
                )
            ),
        )
    )


async def zone_limits_batch(
    session: AsyncSession,
    zone: str,
    *,
    org_ids: set[uuid.UUID] | None = None,
    team_ids: set[uuid.UUID] | None = None,
    member_pairs: set[tuple[uuid.UUID, uuid.UUID]] | None = None,
    service_account_ids: set[uuid.UUID] | None = None,
) -> dict[tuple[str, uuid.UUID] | tuple[str, uuid.UUID, uuid.UUID], ZoneLimit]:
    """Every limit row of one zone for the requested owner ids, keyed by shape.

    Keys are `("org", id)`, `("team", id)`, `("member", team_id, user_id)` and
    `("service_account", id)`.
    """
    clauses = []
    if org_ids:
        clauses.append(and_(ZoneLimit.org_id.in_(org_ids), ZoneLimit.zone == zone))
    if team_ids:
        clauses.append(
            and_(
                ZoneLimit.team_id.in_(team_ids),
                ZoneLimit.user_id.is_(None),
                ZoneLimit.service_account_id.is_(None),
                ZoneLimit.org_id.is_(None),
                ZoneLimit.zone == zone,
            )
        )
    if member_pairs:
        clauses.append(
            and_(
                ZoneLimit.team_id.in_({team_id for team_id, _ in member_pairs}),
                ZoneLimit.user_id.in_({user_id for _, user_id in member_pairs}),
                ZoneLimit.zone == zone,
            )
        )
    if service_account_ids:
        clauses.append(
            and_(
                ZoneLimit.service_account_id.in_(service_account_ids),
                ZoneLimit.zone == zone,
            )
        )
    if not clauses:
        return {}
    out: dict[tuple, ZoneLimit] = {}
    for row in await session.scalars(select(ZoneLimit).where(or_(*clauses))):
        if row.org_id is not None:
            out[("org", row.org_id)] = row
        elif row.user_id is not None:
            out[("member", row.team_id, row.user_id)] = row
        elif row.service_account_id is not None:
            out[("service_account", row.service_account_id)] = row
        else:
            out[("team", row.team_id)] = row
    return out


async def member_defaults_of(
    session: AsyncSession, zone: str, team_id: uuid.UUID
) -> ZoneMemberDefaults | None:
    return await session.get(ZoneMemberDefaults, (zone, team_id))


async def member_defaults_batch(
    session: AsyncSession, zone: str, team_ids: set[uuid.UUID]
) -> dict[uuid.UUID, ZoneMemberDefaults]:
    if not team_ids:
        return {}
    rows = await session.scalars(
        select(ZoneMemberDefaults).where(
            ZoneMemberDefaults.zone == zone, ZoneMemberDefaults.team_id.in_(team_ids)
        )
    )
    return {row.team_id: row for row in rows}


async def org_effective_in(session: AsyncSession, zone: str, org: Org) -> Effective:
    """The org's effective policy in one zone."""
    return org_effective(org, await zone_limits_of(session, zone, {"org_id": org.id}))


async def team_effective_in(session: AsyncSession, zone: str, team: Team) -> Effective:
    """The team's effective policy in one zone: org ∩ team, with each level's zone limits."""
    org = await _org_of(session, team)
    return team_effective(
        await org_effective_in(session, zone, org),
        team,
        await zone_limits_of(session, zone, {"team_id": team.id}),
    )


async def team_effective_of(
    session: AsyncSession, team: Team, zone: str | None = None
) -> Effective:
    """The team's effective policy, in `zone` or the default zone without one."""
    return await team_effective_in(session, zone or _default_zone(), team)


async def owner_effective_in(
    session: AsyncSession,
    zone: str,
    team: Team,
    policy: Policy | None,
    owner: dict[str, uuid.UUID | None],
    *,
    service_account: bool = False,
) -> Effective:
    """The owner's effective policy in one zone: exception ?? defaults, clamped by team."""
    return owner_effective(
        await team_effective_in(session, zone, team),
        await member_defaults_of(session, zone, team.id),
        policy,
        service_account=service_account,
    )


async def owner_effective_of(
    session: AsyncSession,
    team: Team,
    policy: Policy | None,
    *,
    service_account: bool = False,
    zone: str | None = None,
    limits: ZoneLimit | None = None,
) -> Effective:
    """The owner's effective policy; `zone` defaults to the default zone.

    `limits` is the owner's zone-limit row when the caller already loaded it; without it
    the owner's policy carries no limits of its own and only the team's zone limit and the
    member defaults apply.
    """
    zone = zone or _default_zone()
    resolved = policy
    if resolved is not None and limits is not None:
        resolved = replace(
            resolved,
            rpm_limit=limits.rpm_limit,
            tpm_limit=limits.tpm_limit,
            max_parallel=limits.max_parallel,
            budget_nano_usd=limits.budget_nano_usd,
        )
    return owner_effective(
        await team_effective_in(session, zone, team),
        await member_defaults_of(session, zone, team.id),
        resolved,
        service_account=service_account,
    )


def _default_zone() -> str:
    from lumewarp_control_plane.settings import get_settings

    return get_settings().zone


def _models_changed(base: Policy, proposed: Policy) -> bool:
    return (base.model_access, base.allowed_models, base.allowed_groups) != (
        proposed.model_access,
        proposed.allowed_models,
        proposed.allowed_groups,
    )


async def validate_team_policy(
    session: AsyncSession, zone: str, team: Team, proposed: Policy, base: Policy | None
) -> None:
    """A changed team value may not exceed the org's current effective value in `zone`."""
    parent = await org_effective_in(session, zone, await _org_of(session, team))
    check_narrowing(
        parent,
        await expand_groups(session, proposed),
        check_models=base is None or _models_changed(base, proposed),
        base=base,
    )


async def validate_owner_policy(
    session: AsyncSession,
    zone: str,
    team: Team,
    proposed: Policy,
    base: Policy | None = None,
) -> None:
    """Names exist and changed limits/models stay within the team's effective zone policy."""
    models_changed = base is None or _models_changed(base, proposed)
    if models_changed:
        await grant_ids(session, proposed.allowed_models)
    check_narrowing(
        await team_effective_in(session, zone, team),
        await expand_groups(session, proposed),
        check_models=models_changed,
        base=base,
    )


async def validate_key_limits(
    owner: Effective, *, rpm_limit: int | None, tpm_limit: int | None, max_parallel: int | None
) -> None:
    """A key's limits may not exceed its owner's effective values (422)."""
    for name, parent_value, value in (
        ("rpm_limit", owner.rpm_limit, rpm_limit),
        ("tpm_limit", owner.tpm_limit, tpm_limit),
        ("max_parallel", owner.max_parallel, max_parallel),
    ):
        if parent_value is not None and value is not None and value > parent_value:
            raise violation(name, f"must be <= owner value {parent_value} (got {value})")


async def validate_key_models(
    session: AsyncSession, owner: Effective, proposed: Policy, base: Policy | None
) -> Policy:
    """A key's model list: names and groups exist, and a changed list stays within the
    owner's effective set (422). Returns the proposed policy with its groups expanded.

    Only INHERIT and LIST are accepted; a key never widens to ALL. As for the other
    levels, an unchanged list is not rechecked after the owner shrank.
    """
    if proposed.model_access is ModelAccess.ALL:
        raise violation("model_access", "a key is 'inherit' or 'list'")
    expanded = await expand_groups(session, proposed)
    if base is None or _models_changed(base, proposed):
        await grant_ids(session, proposed.allowed_models)
        # The key policy carries only its model half: limits and MCP/A2A stay None/INHERIT
        # and are never flagged here (key limits have `validate_key_limits`).
        check_narrowing(owner, expanded, base=base)
    return expanded


def ensure_unique_ids(ids: list[uuid.UUID], field: str = "model_ids") -> None:
    if len(set(ids)) != len(ids):
        raise violation(field, "must not contain duplicate ids")
