"""The quota hierarchy: org -> team -> owner (member / service account) -> key.

One resolver serves the compiler, `/users/me` and every write path, so the value a write
is validated against is the same value the gateway enforces:

- models: the intersection down the chain (`ModelAccess.INHERIT`/`ALL` keep the parent,
  `LIST` intersects). An org is the root and is never INHERIT.
- limits: the minimum down the chain. A member's exception value wins over the team's
  `member_*_default`; the result is still clamped by the team. Service accounts have no
  member default: only their own policy (INHERIT by default) applies.
- budgets: not combined into one number. Each level keeps its own monthly budget and
  spend; the remaining amount is `min(org, team, owner)` of each level's own remaining
  (see `remaining_nano_usd`).
- keys add one last level to the limits (`ApiKey.rpm_limit` etc.) and form their own
  limiter scope. A key without a limit emits no scope.

The org's spend is not a row in `usage_totals`: it is the sum of its teams' monthly
totals, so it never drifts from the teams beneath it (`usage.org_budget`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from lumewarp_control_plane.models import (
    ApiKey,
    MemberPolicy,
    ModelAccess,
    Org,
    ScopeType,
    ServiceAccount,
    Team,
)

# None means the universal model set (every name in the catalog).
MODEL_UNIVERSAL: frozenset[str] | None = None


@dataclass(frozen=True)
class McpGrant:
    """One level's grant of an MCP server: every approved tool, or exactly `tools`."""

    server: str
    all_tools: bool
    tools: tuple[str, ...] = ()


# server -> allowed tools (None = every approved tool), or None for every server.
McpSet = dict[str, frozenset[str] | None] | None
AGENT_UNIVERSAL: frozenset[str] | None = None


@dataclass(frozen=True)
class Policy:
    """The stored policy of one level, before inheritance is resolved.

    `mcp_access`/`mcp_grants` and `a2a_access`/`a2a_agents` mirror
    `model_access`/`allowed_models` exactly, one level lower on the wire: an MCP grant
    carries its server and, optionally, the tools it is narrowed to.
    """

    model_access: ModelAccess
    allowed_models: tuple[str, ...]
    rpm_limit: int | None = None
    tpm_limit: int | None = None
    max_parallel: int | None = None
    budget_nano_usd: int | None = None
    mcp_access: ModelAccess = ModelAccess.INHERIT
    mcp_grants: tuple[McpGrant, ...] = ()
    a2a_access: ModelAccess = ModelAccess.INHERIT
    a2a_agents: tuple[str, ...] = ()


@dataclass(frozen=True)
class Effective:
    """Resolved policy from the root down to one level.

    `models is None` means the universal set (every model). Otherwise it is exactly the
    names still allowed at this level (possibly the empty deny-all set). `mcp is None`
    means every MCP server with every approved tool; otherwise it maps each server still
    allowed to its allowed tools (None = all of them). `agents is None` is every agent.
    """

    models: frozenset[str] | None
    rpm_limit: int | None = None
    tpm_limit: int | None = None
    max_parallel: int | None = None
    budget_nano_usd: int | None = None
    mcp: McpSet = None
    agents: frozenset[str] | None = None


PolicyRow = Org | Team | MemberPolicy | ServiceAccount


def policy_of(row: PolicyRow) -> Policy:
    """The row's own (stored) policy. The row's `allowed_models` is a loaded column."""
    return Policy(
        model_access=row.model_access,
        allowed_models=tuple(row.allowed_models or ()),
        rpm_limit=row.rpm_limit,
        tpm_limit=row.tpm_limit,
        max_parallel=row.max_parallel,
        budget_nano_usd=row.budget_nano_usd,
        mcp_access=row.mcp_access,
        mcp_grants=tuple(
            McpGrant(
                server=grant["server"],
                all_tools=bool(grant["all_tools"]),
                tools=tuple(grant.get("tools") or ()),
            )
            for grant in (row.mcp_servers or ())
        ),
        a2a_access=row.a2a_access,
        a2a_agents=tuple(row.a2a_agents or ()),
    )


def apply_policy(row: PolicyRow, policy: Policy) -> None:
    """Scalar policy columns; `save_grants` writes the lists once the row has its id."""
    row.model_access = policy.model_access
    row.rpm_limit = policy.rpm_limit
    row.tpm_limit = policy.tpm_limit
    row.max_parallel = policy.max_parallel
    row.budget_nano_usd = policy.budget_nano_usd
    row.mcp_access = policy.mcp_access
    row.a2a_access = policy.a2a_access


def mcp_set(policy: Policy) -> McpSet:
    """The level's own MCP set: `{server: tools or None}` (None = all tools).

    A repeated server merges like the gateway's grants: `all_tools` ORs and the named
    tools union. Empty names are ignored.
    """
    if policy.mcp_access is not ModelAccess.LIST:
        return None
    merged: dict[str, frozenset[str] | None] = {}
    for grant in policy.mcp_grants:
        server = grant.server
        if not server:
            continue
        if grant.all_tools or merged.get(server, frozenset()) is None:
            merged[server] = None
        else:
            merged[server] = merged.get(server, frozenset()) | frozenset(
                tool for tool in grant.tools if tool
            )
    return merged


def _mcp_narrow(parent: McpSet, own: McpSet) -> McpSet:
    """One level of MCP inheritance: INHERIT/ALL keep the parent, LIST intersects."""
    if parent is None:
        return own
    if own is None:
        return parent
    narrowed: dict[str, frozenset[str] | None] = {}
    for server, parent_tools in parent.items():
        if server not in own:
            continue
        own_tools = own[server]
        if parent_tools is None:
            narrowed[server] = own_tools
        elif own_tools is None:
            narrowed[server] = parent_tools
        else:
            narrowed[server] = parent_tools & own_tools
    return narrowed


def _limit(parent: int | None, own: int | None) -> int | None:
    if parent is None:
        return own
    if own is None:
        return parent
    return min(parent, own)


def effective(parent: Effective | None, policy: Policy) -> Effective:
    """Resolve one level, including INHERIT and ALL, against its parent's effective policy.

    ALL keeps the parent's set, exactly like the compiler's `narrow()`. Only the root (no
    parent) makes ALL universal. Source can legitimately hold a member at ALL under a
    team that later shrank to LIST: team updates deliberately do not rewrite owner lists,
    and the compiler's intersection still limits the key, so this function must not widen
    that member back to the whole catalog.
    """
    if policy.model_access is ModelAccess.LIST:
        own = frozenset(policy.allowed_models)
        models = own if parent is None or parent.models is None else parent.models & own
    else:
        models = parent.models if parent is not None else MODEL_UNIVERSAL
    own_mcp = mcp_set(policy)
    mcp = own_mcp if parent is None else _mcp_narrow(parent.mcp, own_mcp)
    if policy.a2a_access is ModelAccess.LIST:
        own_agents = frozenset(policy.a2a_agents)
        agents = (
            own_agents if parent is None or parent.agents is None else parent.agents & own_agents
        )
    else:
        agents = parent.agents if parent is not None else AGENT_UNIVERSAL
    if parent is None:
        return Effective(
            models=models,
            rpm_limit=policy.rpm_limit,
            tpm_limit=policy.tpm_limit,
            max_parallel=policy.max_parallel,
            budget_nano_usd=policy.budget_nano_usd,
            mcp=mcp,
            agents=agents,
        )
    return Effective(
        models=models,
        rpm_limit=_limit(parent.rpm_limit, policy.rpm_limit),
        tpm_limit=_limit(parent.tpm_limit, policy.tpm_limit),
        max_parallel=_limit(parent.max_parallel, policy.max_parallel),
        budget_nano_usd=_limit(parent.budget_nano_usd, policy.budget_nano_usd),
        mcp=mcp,
        agents=agents,
    )


def org_effective(org: Org) -> Effective:
    """The root of the hierarchy; an org cannot inherit."""
    return effective(None, policy_of(org))


def team_effective(org_effective_policy: Effective, team: Team) -> Effective:
    return effective(org_effective_policy, policy_of(team))


def member_defaults(team: Team) -> Policy:
    """The member level's default limits, as a policy with no models and no budget."""
    return Policy(
        model_access=ModelAccess.INHERIT,
        allowed_models=(),
        rpm_limit=team.member_rpm_default,
        tpm_limit=team.member_tpm_default,
        max_parallel=team.member_max_parallel_default,
    )


def owner_effective(
    team_effective_policy: Effective,
    team: Team,
    policy: Policy | None,
    *,
    service_account: bool,
) -> Effective:
    """One key owner's effective policy: `exception ?? default`, clamped by the team.

    `policy is None` is the external-backend case: there is no `member_policies` row, so
    the team's member defaults and model set apply. A service account never takes the
    member defaults; its own row (INHERIT by default) narrows the team.
    """
    own = policy if policy is not None else Policy(ModelAccess.INHERIT, ())
    resolved = effective(team_effective_policy, own)
    if service_account:
        return resolved
    default = member_defaults(team)
    return Effective(
        models=resolved.models,
        rpm_limit=_limit(
            team_effective_policy.rpm_limit,
            resolved.rpm_limit if own.rpm_limit is not None else default.rpm_limit,
        ),
        tpm_limit=_limit(
            team_effective_policy.tpm_limit,
            resolved.tpm_limit if own.tpm_limit is not None else default.tpm_limit,
        ),
        max_parallel=_limit(
            team_effective_policy.max_parallel,
            resolved.max_parallel if own.max_parallel is not None else default.max_parallel,
        ),
        budget_nano_usd=resolved.budget_nano_usd,
        # The member defaults carry no MCP or A2A policy: those come from the level only.
        mcp=resolved.mcp,
        agents=resolved.agents,
    )


def key_effective(owner_effective_policy: Effective, key: ApiKey) -> Effective:
    """The key level: only its own limits, clamped by its owner's effective values."""
    return Effective(
        models=owner_effective_policy.models,
        rpm_limit=_limit(owner_effective_policy.rpm_limit, key.rpm_limit),
        tpm_limit=_limit(owner_effective_policy.tpm_limit, key.tpm_limit),
        max_parallel=_limit(owner_effective_policy.max_parallel, key.max_parallel),
        budget_nano_usd=owner_effective_policy.budget_nano_usd,
        mcp=owner_effective_policy.mcp,
        agents=owner_effective_policy.agents,
    )


def resolve_effective(
    org: Org,
    team: Team,
    *,
    owner_policy: Policy | None = None,
    service_account: bool = False,
) -> Effective:
    """The one resolver: org -> team -> owner. See the module docstring."""
    return owner_effective(
        team_effective(org_effective(org), team),
        team,
        owner_policy,
        service_account=service_account,
    )


@dataclass(frozen=True)
class BudgetLevel:
    """One level's own budget and spend in a window.

    The effective remaining amount is the minimum of each level's `budget - spent`, not a
    single accumulated number: each level spent its own way.
    """

    level: ScopeType
    scope_id: uuid.UUID
    budget_nano_usd: int
    spent_nano_usd: int

    @property
    def remaining_nano_usd(self) -> int:
        return max(self.budget_nano_usd - self.spent_nano_usd, 0)


def remaining_nano_usd(levels: list[BudgetLevel]) -> int | None:
    """MIN of the level remainders; None when no level sets a budget."""
    return min((level.remaining_nano_usd for level in levels), default=None)
