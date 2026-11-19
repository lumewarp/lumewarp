"""Per-zone limits, budgets and member defaults.

Limits used to live on the org/team/member/service-account rows, so an installation could
only have one set. They are per zone now: the same grants apply in every zone, but prod may
allow 10 rpm where dev allows 100. A key keeps its own limits (it belongs to one zone).

Reads are exposed as `limits_by_zone` on every owner output; writes go through
`PUT /zones/{zone}/limits` (one owner per call) and
`PUT /zones/{zone}/member-defaults`. The narrowing rules are the existing ones, applied
inside one zone: a child may not exceed its parent's effective value *in that zone*.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime

from fastapi import APIRouter
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane import audit
from lumewarp_control_plane.api.common import (
    SessionDep,
    enqueue_recompile,
    lock_hierarchy,
    mutation,
    validation_error,
)
from lumewarp_control_plane.api.policy import (
    check_narrowing,
    org_effective_in,
    policy_of,
    team_effective_in,
)
from lumewarp_control_plane.api.schemas import (
    PolicyOut,
    TeamOut,
    ZoneLimitSet,
    ZoneLimitsOut,
    ZoneMemberDefaultsOut,
    ZoneMemberDefaultsSet,
)
from lumewarp_control_plane.authz import Action, Authz
from lumewarp_control_plane.errors import not_found
from lumewarp_control_plane.models import (
    MemberPolicy,
    ModelAccess,
    Org,
    OrgBudgetWindow,
    ServiceAccount,
    Team,
    TeamMember,
    Zone,
    ZoneLimit,
    ZoneMemberDefaults,
)
from lumewarp_control_plane.quota import Policy
from lumewarp_control_plane.settings import get_settings
from lumewarp_control_plane.windows import month_window

router = APIRouter(tags=["zones"])

_OWNER_FIELDS = ("org_id", "team_id", "user_id", "service_account_id")
_LIMIT_FIELDS = ("rpm_limit", "tpm_limit", "max_parallel", "budget_nano_usd")


async def zone_or_404(session: AsyncSession, zone: str) -> Zone:
    """A live zone, or 404 (the same answer for unknown and archived names)."""
    row = await session.get(Zone, zone)
    if row is None or row.archived_at is not None:
        raise not_found("zone not found")
    return row


def limits_out(row: ZoneLimit | None) -> ZoneLimitsOut:
    return ZoneLimitsOut(
        rpm_limit=getattr(row, "rpm_limit", None),
        tpm_limit=getattr(row, "tpm_limit", None),
        max_parallel=getattr(row, "max_parallel", None),
        budget_nano_usd=getattr(row, "budget_nano_usd", None),
    )


def member_defaults_out(row: ZoneMemberDefaults | None) -> ZoneMemberDefaultsOut:
    return ZoneMemberDefaultsOut(
        rpm_limit=getattr(row, "rpm_limit", None),
        tpm_limit=getattr(row, "tpm_limit", None),
        max_parallel=getattr(row, "max_parallel", None),
    )


def _owner_values(body: ZoneLimitSet | dict[str, uuid.UUID | None]) -> dict:
    if isinstance(body, dict):
        return {name: body.get(name) for name in _OWNER_FIELDS}
    return {name: getattr(body, name) for name in _OWNER_FIELDS}


async def limits_of(
    session: AsyncSession, zone: str, owner: dict[str, uuid.UUID | None]
) -> ZoneLimit | None:
    values = _owner_values(owner)
    return await session.scalar(
        select(ZoneLimit).where(
            ZoneLimit.zone == zone,
            *(
                getattr(ZoneLimit, name).is_(None)
                if value is None
                else getattr(ZoneLimit, name) == value
                for name, value in values.items()
            ),
        )
    )


async def _limits_rows(
    session: AsyncSession,
    kind: str,
    ids: set,
    *,
    zones: set[str] | None = None,
) -> list[ZoneLimit]:
    """All limit rows of one owner shape, optionally limited to zones."""
    stmt = select(ZoneLimit)
    if kind == "org":
        stmt = stmt.where(ZoneLimit.org_id.in_(ids))
    elif kind == "team":
        stmt = stmt.where(
            ZoneLimit.team_id.in_(ids),
            ZoneLimit.user_id.is_(None),
            ZoneLimit.service_account_id.is_(None),
            ZoneLimit.org_id.is_(None),
        )
    elif kind == "member":
        stmt = stmt.where(
            ZoneLimit.team_id.in_({team_id for team_id, _ in ids}),
            ZoneLimit.user_id.in_({user_id for _, user_id in ids}),
        )
    elif kind == "service_account":
        stmt = stmt.where(ZoneLimit.service_account_id.in_(ids))
    else:
        raise ValueError(f"unknown owner kind {kind!r}")
    if zones is not None:
        stmt = stmt.where(ZoneLimit.zone.in_(zones))
    return list((await session.scalars(stmt)).all())


def _owner_key(row: ZoneLimit) -> tuple:
    if row.org_id is not None:
        return ("org", row.org_id)
    if row.user_id is not None:
        return ("member", row.team_id, row.user_id)
    if row.service_account_id is not None:
        return ("service_account", row.service_account_id)
    return ("team", row.team_id)


async def limits_by_zone(
    session: AsyncSession,
    kind: str,
    ids: set,
) -> dict[tuple, dict[str, ZoneLimitsOut]]:
    """`owner key -> {zone: limits}` for one owner shape, in one query."""
    if not ids:
        return {}
    out: dict[tuple, dict[str, ZoneLimitsOut]] = {}
    for row in await _limits_rows(session, kind, ids):
        out.setdefault(_owner_key(row), {})[row.zone] = limits_out(row)
    return out


async def member_defaults_by_zone(
    session: AsyncSession, team_ids: set[uuid.UUID]
) -> dict[uuid.UUID, dict[str, ZoneMemberDefaultsOut]]:
    if not team_ids:
        return {}
    out: dict[uuid.UUID, dict[str, ZoneMemberDefaultsOut]] = {}
    for row in await session.scalars(
        select(ZoneMemberDefaults).where(ZoneMemberDefaults.team_id.in_(team_ids))
    ):
        out.setdefault(row.team_id, {})[row.zone] = member_defaults_out(row)
    return out


def apply_default_zone_limits(
    out: PolicyOut,
    by_zone: dict[str, ZoneLimitsOut],
    *,
    default_zone: str | None = None,
    member_defaults: dict[str, ZoneMemberDefaultsOut] | None = None,
) -> None:
    """Fill the legacy scalar fields (and `member_*_default`) from the default zone."""
    zone = default_zone or get_settings().zone
    current = by_zone.get(zone)
    if current is not None:
        out.rpm_limit = current.rpm_limit
        out.tpm_limit = current.tpm_limit
        out.max_parallel = current.max_parallel
        out.budget_nano_usd = current.budget_nano_usd
    out.limits_by_zone = by_zone
    if member_defaults is not None and isinstance(out, TeamOut):
        out.member_defaults_by_zone = member_defaults
        defaults = member_defaults.get(zone)
        if defaults is not None:
            out.member_rpm_default = defaults.rpm_limit
            out.member_tpm_default = defaults.tpm_limit
            out.member_max_parallel_default = defaults.max_parallel


def _proposed(base: Policy, body: ZoneLimitSet) -> Policy:
    return replace(
        base,
        rpm_limit=body.rpm_limit,
        tpm_limit=body.tpm_limit,
        max_parallel=body.max_parallel,
        budget_nano_usd=body.budget_nano_usd,
    )


async def _save_limits(
    session: AsyncSession, zone: str, owner: dict, body: ZoneLimitSet
) -> ZoneLimit | None:
    """Upsert the owner's row, or delete it when every value is null."""
    values = _owner_values(owner)
    row = await limits_of(session, zone, values)
    if all(getattr(body, name) is None for name in _LIMIT_FIELDS):
        if row is not None:
            await session.delete(row)
        return None
    if row is None:
        if values.get("user_id") is not None:
            # The member's row is an exception policy in every zone; limits need it as
            # the composite FK target, so an empty exception is created on first write.
            member = await session.get(MemberPolicy, (values["team_id"], values["user_id"]))
            if member is None:
                session.add(MemberPolicy(team_id=values["team_id"], user_id=values["user_id"]))
                await session.flush()
        row = ZoneLimit(zone=zone, **values)
        session.add(row)
    row.rpm_limit = body.rpm_limit
    row.tpm_limit = body.tpm_limit
    row.max_parallel = body.max_parallel
    row.budget_nano_usd = body.budget_nano_usd
    await session.flush()
    return row


def _owner_details(owner: dict) -> dict[str, str | None]:
    return {name: str(value) if value is not None else None for name, value in owner.items()}


@router.put("/zones/{zone}/limits", response_model=ZoneLimitsOut, operation_id="putZoneLimits")
async def put_zone_limits(
    zone: str, body: ZoneLimitSet, authz: Authz, session: SessionDep
) -> ZoneLimitsOut:
    """Set one owner's rate limits and monthly budget in one zone.

    The owner is an org, a team, a member (`team_id` + `user_id`) or a service account
    (`team_id` + `service_account_id`). Requires the same role as the old inline edit: an
    installation admin for an org, an org admin for a team, a team admin for a member or
    service account. A changed value may not exceed its parent's effective value in this
    zone. Sending every limit as null removes the row (no limit at this level).
    """
    async with mutation(session):
        await lock_hierarchy(session)
        zone_row = await zone_or_404(session, zone)
        org: Org | None = None
        team: Team | None = None
        base: Policy
        parent = None
        if body.org_id is not None:
            await authz.require(Action.ORG_MANAGE)
            org = await session.get(Org, body.org_id)
            if org is None or org.deleted_at is not None:
                raise not_found("org not found")
            owner = {"org_id": org.id}
            current = await limits_of(session, zone, owner)
            base = policy_of(org, current)
        elif body.team_id is not None:
            team = await session.get(Team, body.team_id)
            if team is None or team.deleted_at is not None:
                raise not_found("team not found")
            # A caller who cannot read the team must not learn it exists.
            if not await authz.allowed(Action.TEAM_READ, team.id):
                raise not_found("team not found")
            if body.user_id is not None:
                await authz.require(Action.TEAM_MEMBERS_MANAGE, team.id)
                membership = await session.get(TeamMember, (team.id, body.user_id))
                if membership is None:
                    raise not_found("member not found")
                owner = {"team_id": team.id, "user_id": body.user_id}
                policy_row = await session.get(MemberPolicy, (team.id, body.user_id))
                current = await limits_of(session, zone, owner)
                base = (
                    policy_of(policy_row, current)
                    if policy_row is not None
                    else Policy(ModelAccess.INHERIT, ())
                )
                parent = await team_effective_in(session, zone, team)
            elif body.service_account_id is not None:
                await authz.require(Action.SERVICE_ACCOUNT_MANAGE, team.id)
                account = await session.get(ServiceAccount, body.service_account_id)
                if account is None or account.team_id != team.id:
                    raise not_found("service account not found")
                owner = {"team_id": team.id, "service_account_id": account.id}
                current = await limits_of(session, zone, owner)
                base = policy_of(account, current)
                parent = await team_effective_in(session, zone, team)
            else:
                await authz.require(Action.TEAM_QUOTA_SET, team.id)
                owner = {"team_id": team.id}
                current = await limits_of(session, zone, owner)
                base = policy_of(team, current)
                parent = await org_effective_in(session, zone, await _org(session, team))
        else:  # pragma: no cover - the schema enforces an owner
            raise validation_error("org_id", "provide an owner")
        proposed = _proposed(base, body)
        if parent is not None:
            check_narrowing(parent, proposed, check_models=False, base=base)
        row = await _save_limits(session, zone, owner, body)
        if body.org_id is not None and base.budget_nano_usd != proposed.budget_nano_usd:
            if base.budget_nano_usd is None or proposed.budget_nano_usd is None:
                await _reset_org_window(session, zone, body.org_id)
            elif proposed.budget_nano_usd > base.budget_nano_usd:
                await _reset_org_window(session, zone, body.org_id)
        audit.record(
            session,
            action="zone.limits.update",
            resource_type="zone",
            resource_id=zone_row.name,
            principal=authz.principal,
            team_id=team.id if team is not None else None,
            org_id=org.id if org is not None else team.org_id if team is not None else None,
            details={
                "zone": zone_row.name,
                "owner": _owner_details(owner),
                "changes": body.model_dump(exclude_unset=True, mode="json"),
            },
        )
        await enqueue_recompile(session, "api.zone_limits.update", zone_row.name)
    return limits_out(row)


async def _org(session: AsyncSession, team: Team) -> Org:
    org = await session.get(Org, team.org_id)
    if org is None or org.deleted_at is not None:
        raise not_found("org not found")
    return org


async def _reset_org_window(session: AsyncSession, zone: str, org_id: uuid.UUID) -> None:
    """A raised/removed org budget may clear an exceeded state in this zone."""
    await session.execute(
        delete(OrgBudgetWindow).where(
            OrgBudgetWindow.zone == zone,
            OrgBudgetWindow.org_id == org_id,
            OrgBudgetWindow.window_start == month_window(datetime.now(UTC))[0],
        )
    )


@router.put(
    "/zones/{zone}/member-defaults",
    response_model=ZoneMemberDefaultsOut,
    operation_id="putZoneMemberDefaults",
)
async def put_zone_member_defaults(
    zone: str, body: ZoneMemberDefaultsSet, authz: Authz, session: SessionDep
) -> ZoneMemberDefaultsOut:
    """Set a team's default member limits in one zone (team-admin write).

    The defaults are the member level's fallback when a member has no exception of their
    own; a changed default may not exceed the team's effective limit in this zone. Sending
    every value as null deletes the row.
    """
    async with mutation(session):
        await lock_hierarchy(session)
        zone_row = await zone_or_404(session, zone)
        team = await session.get(Team, body.team_id)
        if team is None or team.deleted_at is not None:
            raise not_found("team not found")
        if not await authz.allowed(Action.TEAM_READ, team.id):
            raise not_found("team not found")
        await authz.require(Action.TEAM_SETTINGS_UPDATE, team.id)
        parent = await team_effective_in(session, zone, team)
        current = await session.get(ZoneMemberDefaults, (zone, team.id))
        base = Policy(
            ModelAccess.INHERIT,
            (),
            rpm_limit=getattr(current, "rpm_limit", None),
            tpm_limit=getattr(current, "tpm_limit", None),
            max_parallel=getattr(current, "max_parallel", None),
        )
        proposed = replace(
            base,
            rpm_limit=body.rpm_limit,
            tpm_limit=body.tpm_limit,
            max_parallel=body.max_parallel,
        )
        check_narrowing(parent, proposed, check_models=False, base=base)
        if body.rpm_limit is None and body.tpm_limit is None and body.max_parallel is None:
            if current is not None:
                await session.delete(current)
            row = None
        else:
            if current is None:
                row = ZoneMemberDefaults(zone=zone, team_id=team.id)
                session.add(row)
            else:
                row = current
            row.rpm_limit = body.rpm_limit
            row.tpm_limit = body.tpm_limit
            row.max_parallel = body.max_parallel
            await session.flush()
        audit.record(
            session,
            action="zone.member_defaults.update",
            resource_type="zone",
            resource_id=zone_row.name,
            principal=authz.principal,
            team_id=team.id,
            org_id=team.org_id,
            details={
                "zone": zone_row.name,
                "team_id": str(team.id),
                "changes": body.model_dump(exclude_unset=True, mode="json"),
            },
        )
        await enqueue_recompile(session, "api.zone_member_defaults.update", zone_row.name)
    return member_defaults_out(row)
