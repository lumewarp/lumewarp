"""Policy inheritance and narrowing (O1 M1/M5).

Two levels: a team, and the owners of keys under it (team members, service accounts). The
API is the enforcement point that keeps source state valid: an owner may only narrow its
team. The compiler also intersects allowlists defensively, but accepting a non-subset in
the source would make the UI misleading and could turn an explicit deny-all into an
accidental grant after a later edit.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane.errors import ApiError
from lumewarp_control_plane.models import (
    CatalogName,
    ModelAccess,
    PolicyGrant,
    ServiceAccount,
    Team,
    TeamMember,
)

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


def policy_of(row: Team | TeamMember | ServiceAccount) -> Policy:
    return Policy(
        model_access=row.model_access,
        allowed_models=tuple(row.allowed_models),
        rpm_limit=row.rpm_limit,
        tpm_limit=row.tpm_limit,
        max_parallel=row.max_parallel,
        budget_nano_usd=row.budget_nano_usd,
        soft_budget_nano_usd=row.soft_budget_nano_usd,
    )


def apply_policy(row: Team | TeamMember | ServiceAccount, policy: Policy) -> None:
    """Scalar policy columns; `save_grants` writes the list once the row has its id."""
    row.model_access = policy.model_access
    row.rpm_limit = policy.rpm_limit
    row.tpm_limit = policy.tpm_limit
    row.max_parallel = policy.max_parallel
    row.budget_nano_usd = policy.budget_nano_usd
    row.soft_budget_nano_usd = policy.soft_budget_nano_usd


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


def _owner(row: Team | TeamMember | ServiceAccount) -> dict[str, uuid.UUID | None]:
    if isinstance(row, Team):
        return {"team_id": row.id, "user_id": None, "service_account_id": None}
    if isinstance(row, TeamMember):
        return {"team_id": row.team_id, "user_id": row.user_id, "service_account_id": None}
    return {"team_id": row.team_id, "user_id": None, "service_account_id": row.id}


async def save_grants(
    session: AsyncSession, row: Team | TeamMember | ServiceAccount, policy: Policy
) -> None:
    """Replace the row's grants. The row must be flushed; refresh it to read them back."""
    ids = await grant_ids(session, policy.allowed_models)
    owner = _owner(row)
    await session.execute(
        delete(PolicyGrant).where(
            *(
                column.is_(None) if value is None else column == value
                for column, value in (
                    (PolicyGrant.team_id, owner["team_id"]),
                    (PolicyGrant.user_id, owner["user_id"]),
                    (PolicyGrant.service_account_id, owner["service_account_id"]),
                )
            )
        )
    )
    session.add_all(PolicyGrant(name_id=name_id, **owner) for name_id in ids.values())
    await session.flush()


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
    omitted fields keep `base`; an explicit null clears a limit or budget. Teams pass
    `default_mode=ALL` and cannot inherit; members and service accounts pass INHERIT.
    """
    supplied = fields.model_fields_set
    if base is None:
        mode = fields.model_access if "model_access" in supplied else default_mode
        if mode is ModelAccess.INHERIT and default_mode is ModelAccess.ALL:
            raise violation("model_access", "teams cannot inherit")
        if fields.allowed_models and mode is not ModelAccess.LIST:
            raise violation("allowed_models", "only allowed when model_access is 'list'")
        allowed = tuple(sorted(fields.allowed_models or ())) if mode is ModelAccess.LIST else ()
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
        raise violation("model_access", "teams cannot inherit")
    if "allowed_models" in supplied:
        if mode is not ModelAccess.LIST and fields.allowed_models:
            raise violation("allowed_models", "only allowed when model_access is 'list'")
        allowed = tuple(sorted(fields.allowed_models or ()))
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


def check_narrowing(
    parent: Effective, policy: Policy, prefix: str = "", *, check_models: bool = True
) -> None:
    """Raise 422 if `policy` widens a limit or, with `check_models`, exceeds the parent's
    names.
    """
    field = (lambda name: f"{prefix}.{name}") if prefix else (lambda name: name)

    if check_models and policy.model_access is ModelAccess.ALL and parent.models is not None:
        raise violation(field("model_access"), "cannot widen to all under a restricted parent")
    if check_models and policy.model_access is ModelAccess.LIST and parent.models is not None:
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


def team_effective(team: Team) -> Effective:
    return effective(None, policy_of(team))


async def validate_owner_policy(
    session: AsyncSession, team: Team, proposed: Policy, base: Policy | None = None
) -> None:
    """Names exist and stay within the team, limits narrow it. A list the write leaves
    unchanged is not rechecked: it may legitimately exceed a team that shrank later.
    """
    models_changed = base is None or (base.model_access, base.allowed_models) != (
        proposed.model_access,
        proposed.allowed_models,
    )
    if models_changed:
        await grant_ids(session, proposed.allowed_models)
    check_narrowing(team_effective(team), proposed, check_models=models_changed)


async def validate_owners_after_team_update(
    session: AsyncSession, team: Team, proposed: Policy
) -> None:
    """Limits must still narrow the proposed team policy. Model lists are left alone when
    the team shrinks: owners keep their settings and the effective set is the intersection
    (`compiler.narrow`).
    """
    team_eff = effective(None, proposed)
    members = (
        await session.scalars(
            select(TeamMember).where(TeamMember.team_id == team.id).order_by(TeamMember.user_id)
        )
    ).all()
    for member in members:
        check_narrowing(
            team_eff, policy_of(member), f"members.{member.user_id}", check_models=False
        )
    accounts = (
        await session.scalars(
            select(ServiceAccount)
            .where(ServiceAccount.team_id == team.id)
            .order_by(ServiceAccount.id)
        )
    ).all()
    for account in accounts:
        check_narrowing(
            team_eff, policy_of(account), f"service_accounts.{account.id}", check_models=False
        )


def ensure_unique_ids(ids: list[uuid.UUID], field: str = "model_ids") -> None:
    if len(set(ids)) != len(ids):
        raise violation(field, "must not contain duplicate ids")
