"""Policy inheritance and hierarchy narrowing (O1 M1/M5).

The API is the enforcement point that keeps source state valid: a child may only narrow
its parent. The compiler also intersects allowlists defensively, but accepting a
non-subset in the source would make the UI misleading and could turn an explicit deny-all
into an accidental grant after a later edit.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane.errors import ApiError
from lumewarp_control_plane.models import ApiKey, ModelAccess, Org, Project, Team

if TYPE_CHECKING:
    from lumewarp_control_plane.api.schemas import PolicyFields

_MODEL_UNIVERSAL = None


def violation(field: str, message: str) -> ApiError:
    return ApiError(422, "validation_error", f"{field}: {message}")


@dataclass(frozen=True)
class Policy:
    model_access: ModelAccess
    allowed_models: tuple[str, ...]
    rpm_limit: int | None = None
    tpm_limit: int | None = None
    max_parallel: int | None = None
    budget_nano_usd: int | None = None
    soft_budget_nano_usd: int | None = None


@dataclass(frozen=True)
class Effective:
    """Resolved policy from the root down to one level.

    `models is None` means the universal set (every model). Otherwise it is exactly the
    aliases still allowed at this level (possibly the empty deny-all set).
    """

    models: frozenset[str] | None
    rpm_limit: int | None = None
    tpm_limit: int | None = None
    max_parallel: int | None = None
    budget_nano_usd: int | None = None
    soft_budget_nano_usd: int | None = None


def policy_of(row: Org | Team | Project | ApiKey) -> Policy:
    return Policy(
        model_access=row.model_access,
        allowed_models=tuple(row.allowed_models),
        rpm_limit=row.rpm_limit,
        tpm_limit=row.tpm_limit,
        max_parallel=row.max_parallel,
        budget_nano_usd=row.budget_nano_usd,
        soft_budget_nano_usd=row.soft_budget_nano_usd,
    )


def apply_policy(row: Org | Team | Project | ApiKey, policy: Policy) -> None:
    row.model_access = policy.model_access
    row.allowed_models = list(policy.allowed_models)
    row.rpm_limit = policy.rpm_limit
    row.tpm_limit = policy.tpm_limit
    row.max_parallel = policy.max_parallel
    row.budget_nano_usd = policy.budget_nano_usd
    row.soft_budget_nano_usd = policy.soft_budget_nano_usd


def _limit(parent: int | None, own: int | None) -> int | None:
    if parent is None:
        return own
    if own is None:
        return parent
    return min(parent, own)


def effective(parent: Effective | None, policy: Policy) -> Effective:
    """Resolve one level, including INHERIT, against its parent's effective policy."""
    if policy.model_access is ModelAccess.INHERIT:
        models = parent.models if parent is not None else _MODEL_UNIVERSAL
    elif policy.model_access is ModelAccess.ALL:
        models = _MODEL_UNIVERSAL
    else:
        own = frozenset(policy.allowed_models)
        models = own if parent is None or parent.models is None else parent.models & own
    if parent is None:
        return Effective(
            models=models,
            rpm_limit=policy.rpm_limit,
            tpm_limit=policy.tpm_limit,
            max_parallel=policy.max_parallel,
            budget_nano_usd=policy.budget_nano_usd,
            soft_budget_nano_usd=policy.soft_budget_nano_usd,
        )
    return Effective(
        models=models,
        rpm_limit=_limit(parent.rpm_limit, policy.rpm_limit),
        tpm_limit=_limit(parent.tpm_limit, policy.tpm_limit),
        max_parallel=_limit(parent.max_parallel, policy.max_parallel),
        budget_nano_usd=_limit(parent.budget_nano_usd, policy.budget_nano_usd),
        soft_budget_nano_usd=_limit(parent.soft_budget_nano_usd, policy.soft_budget_nano_usd),
    )


def policy_from_fields(
    fields: PolicyFields, *, default_mode: ModelAccess, base: Policy | None = None
) -> Policy:
    """Build a proposed policy from a create/update payload.

    On create (`base is None`) omitted fields take `default_mode`/no limit. On update,
    omitted fields keep `base`; an explicit null clears a limit or budget.
    """
    supplied = fields.model_fields_set
    if base is None:
        mode = fields.model_access if "model_access" in supplied else default_mode
        if mode is ModelAccess.INHERIT and default_mode is ModelAccess.ALL:
            raise violation("model_access", "orgs cannot inherit")
        if fields.allowed_models and mode is not ModelAccess.LIST:
            raise violation("allowed_models", "only allowed when model_access is 'list'")
        allowed = tuple(fields.allowed_models or ()) if mode is ModelAccess.LIST else ()
        return _validated(
            Policy(
                model_access=mode,
                allowed_models=allowed,
                rpm_limit=fields.rpm_limit,
                tpm_limit=fields.tpm_limit,
                max_parallel=fields.max_parallel,
                budget_nano_usd=fields.budget_nano_usd,
                soft_budget_nano_usd=fields.soft_budget_nano_usd,
            )
        )

    mode = fields.model_access if "model_access" in supplied else base.model_access
    if mode is ModelAccess.INHERIT and default_mode is ModelAccess.ALL:
        raise violation("model_access", "orgs cannot inherit")
    if "allowed_models" in supplied:
        if mode is not ModelAccess.LIST and fields.allowed_models:
            raise violation("allowed_models", "only allowed when model_access is 'list'")
        allowed = tuple(fields.allowed_models or ())
    elif mode is ModelAccess.LIST:
        allowed = base.allowed_models if base.model_access is ModelAccess.LIST else ()
    else:
        allowed = ()
    return _validated(
        Policy(
            model_access=mode,
            allowed_models=allowed,
            rpm_limit=(fields.rpm_limit if "rpm_limit" in supplied else base.rpm_limit),
            tpm_limit=(fields.tpm_limit if "tpm_limit" in supplied else base.tpm_limit),
            max_parallel=(fields.max_parallel if "max_parallel" in supplied else base.max_parallel),
            budget_nano_usd=(
                fields.budget_nano_usd if "budget_nano_usd" in supplied else base.budget_nano_usd
            ),
            soft_budget_nano_usd=(
                fields.soft_budget_nano_usd
                if "soft_budget_nano_usd" in supplied
                else base.soft_budget_nano_usd
            ),
        )
    )


def _validated(policy: Policy) -> Policy:
    if policy.model_access is ModelAccess.INHERIT and policy.allowed_models:
        raise violation("allowed_models", "inherit cannot carry an allowlist")
    if (
        policy.budget_nano_usd is not None
        and policy.soft_budget_nano_usd is not None
        and policy.soft_budget_nano_usd > policy.budget_nano_usd
    ):
        raise violation("soft_budget_nano_usd", "must not exceed budget_nano_usd")
    return policy


def check_narrowing(parent: Effective, policy: Policy, prefix: str = "") -> None:
    """Raise 422 if `policy` is not a subset of `parent` (or widens a limit)."""
    field = (lambda name: f"{prefix}.{name}") if prefix else (lambda name: name)

    if policy.model_access is ModelAccess.ALL and parent.models is not None:
        raise violation(field("model_access"), "cannot widen to all under a restricted parent")
    if policy.model_access is ModelAccess.LIST and parent.models is not None:
        own = set(policy.allowed_models)
        if not own <= set(parent.models):
            extra = sorted(own - set(parent.models))
            raise violation(
                field("allowed_models"),
                f"models not allowed by parent: {', '.join(extra)}",
            )
    for name, parent_value, own_value in (
        ("rpm_limit", parent.rpm_limit, policy.rpm_limit),
        ("tpm_limit", parent.tpm_limit, policy.tpm_limit),
        ("max_parallel", parent.max_parallel, policy.max_parallel),
        ("budget_nano_usd", parent.budget_nano_usd, policy.budget_nano_usd),
        ("soft_budget_nano_usd", parent.soft_budget_nano_usd, policy.soft_budget_nano_usd),
    ):
        if parent_value is not None and own_value is not None and own_value > parent_value:
            raise violation(
                field(name),
                f"must be <= parent value {parent_value} (got {own_value})",
            )


async def org_effective(session: AsyncSession, org: Org) -> Effective:
    return effective(None, policy_of(org))


async def team_effective(session: AsyncSession, team: Team) -> Effective:
    org = await session.get(Org, team.org_id)
    if org is None:  # schema FK should make this impossible
        raise violation("team", "org not found")
    return effective(await org_effective(session, org), policy_of(team))


async def project_effective(session: AsyncSession, project: Project) -> Effective:
    team = await session.get(Team, project.team_id)
    if team is None:
        raise violation("project", "team not found")
    return effective(await team_effective(session, team), policy_of(project))


async def key_effective(session: AsyncSession, key: ApiKey) -> Effective:
    project = await session.get(Project, key.project_id)
    if project is None:
        raise violation("key", "project not found")
    return effective(await project_effective(session, project), policy_of(key))


async def parent_effective_for_org(session: AsyncSession, org: Org) -> Effective:
    return await org_effective(session, org)


async def parent_effective_for_team(session: AsyncSession, team: Team) -> Effective:
    return await team_effective(session, team)


async def parent_effective_for_project(session: AsyncSession, project: Project) -> Effective:
    return await project_effective(session, project)


async def validate_descendants_after_org_update(
    session: AsyncSession, org: Org, proposed: Policy
) -> None:
    org_eff = effective(None, proposed)
    teams = (
        await session.scalars(select(Team).where(Team.org_id == org.id).order_by(Team.id))
    ).all()
    for team in teams:
        await _check_team_subtree(session, team, org_eff, f"teams.{team.id}")


async def validate_descendants_after_team_update(
    session: AsyncSession, team: Team, parent: Effective, proposed: Policy
) -> None:
    team_eff = effective(parent, proposed)
    projects = (
        await session.scalars(
            select(Project).where(Project.team_id == team.id).order_by(Project.id)
        )
    ).all()
    for project in projects:
        await _check_project_subtree(session, project, team_eff, f"projects.{project.id}")


async def _check_team_subtree(
    session: AsyncSession, team: Team, parent: Effective, prefix: str
) -> None:
    policy = policy_of(team)
    check_narrowing(parent, policy, prefix)
    await validate_descendants_after_team_update(session, team, parent, policy)


async def _check_project_subtree(
    session: AsyncSession, project: Project, parent: Effective, prefix: str
) -> None:
    policy = policy_of(project)
    check_narrowing(parent, policy, prefix)
    project_eff = effective(parent, policy)
    keys = (
        await session.scalars(
            select(ApiKey).where(ApiKey.project_id == project.id).order_by(ApiKey.id)
        )
    ).all()
    for key in keys:
        check_narrowing(project_eff, policy_of(key), f"{prefix}.keys.{key.id}")


async def validate_descendants_after_project_update(
    session: AsyncSession, project: Project, parent: Effective, proposed: Policy
) -> None:
    project_eff = effective(parent, proposed)
    keys = (
        await session.scalars(
            select(ApiKey).where(ApiKey.project_id == project.id).order_by(ApiKey.id)
        )
    ).all()
    for key in keys:
        check_narrowing(project_eff, policy_of(key), f"keys.{key.id}")


def ensure_unique_ids(ids: list[uuid.UUID], field: str = "deployment_ids") -> None:
    if len(set(ids)) != len(ids):
        raise violation(field, "must not contain duplicate ids")
