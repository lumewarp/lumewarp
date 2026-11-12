"""Organizations: the root of the hierarchy, its administration and its usage summary.

Only an installation admin may create, rename, set policy on or archive an org, and assign
or remove its admins; an org admin may read its own orgs and administers their teams by
inheritance (`RequestAuthz`). The matcher `Resource("org", id)` keeps the backend's direct
relation separate from that inheritance; this module never trusts an org from a payload.

Archiving is irreversible: an org can only be archived when it has no live team, and no
team may be created in it afterwards. There is no default org, so `POST /teams` takes an
explicit `org_id`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response, status
from sqlalchemy import and_, case, func, select, union
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane import audit
from lumewarp_control_plane.api.common import (
    LimitDep,
    MetricsDep,
    SessionDep,
    SettingsDep,
    aware,
    conflict,
    enqueue_recompile,
    ensure_grants_managed,
    ensure_not_null,
    ensure_range,
    lock_scope,
    mutation,
    parse_uuid_cursor,
    require_signed_in,
    suspend_ineligible_user_keys,
    uuid_page,
    validation_error,
)
from lumewarp_control_plane.api.policy import (
    apply_policy,
    grant_ids,
    policy_from_fields,
    policy_of,
    save_grants,
)
from lumewarp_control_plane.api.schemas import (
    OrgAdminOut,
    OrgCreate,
    OrgOut,
    OrgTeamUsageOut,
    OrgUpdate,
    OrgUsageOut,
    Page,
)
from lumewarp_control_plane.authz import Action, Authz, RequestAuthz
from lumewarp_control_plane.errors import not_found
from lumewarp_control_plane.models import (
    InheritedFrom,
    InstallationAdmin,
    ModelAccess,
    Org,
    OrgAdmin,
    Team,
    UsageEvent,
    User,
)
from lumewarp_control_plane.usage.org_budget import reset_org_budget_window
from lumewarp_control_plane.windows import month_window

router = APIRouter(tags=["orgs"])

FromQuery = Annotated[datetime | None, Query(alias="from")]
ToQuery = Annotated[datetime | None, Query(alias="to")]


def _admin_out(row: OrgAdmin, user: User) -> OrgAdminOut:
    return OrgAdminOut(
        org_id=row.org_id,
        user_id=row.user_id,
        email=user.email,
        display_name=user.display_name,
        is_active=user.is_active,
        assigned_by=row.assigned_by,
        assigned_at=row.assigned_at,
    )


async def _org_or_404(session: AsyncSession, org_id: uuid.UUID) -> Org:
    row = await session.get(Org, org_id)
    if row is None:
        raise not_found("org not found")
    return row


async def _readable_org_or_404(
    session: AsyncSession, authz: RequestAuthz, org_id: uuid.UUID
) -> Org:
    """404 when the org is missing or the caller does not administer it (no existence hint)."""
    row = await session.get(Org, org_id)
    if row is None or not await authz.is_org_admin(row.id):
        raise not_found("org not found")
    return row


@router.get("/orgs", response_model=Page[OrgOut], operation_id="listOrgs")
async def list_orgs(
    authz: Authz,
    session: SessionDep,
    limit: LimitDep = 50,
    cursor: str | None = None,
) -> Page[OrgOut]:
    """Live orgs: every one for an installation admin, the caller's own for an org admin."""
    stmt = select(Org).where(Org.deleted_at.is_(None)).order_by(Org.id)
    administered = await authz.admin_orgs(
        list(await session.scalars(select(Org.id).where(Org.deleted_at.is_(None))))
    )
    if administered is not None:
        stmt = stmt.where(Org.id.in_(administered))
    rows, next_cursor = await uuid_page(session, stmt, Org.id, limit, cursor)
    return Page[OrgOut](items=[OrgOut.model_validate(row) for row in rows], next_cursor=next_cursor)


@router.post(
    "/orgs",
    response_model=OrgOut,
    status_code=status.HTTP_201_CREATED,
    operation_id="createOrg",
)
async def create_org(body: OrgCreate, authz: Authz, session: SessionDep) -> OrgOut:
    """Create an org. Requires an installation admin."""
    await authz.require(Action.ORG_MANAGE)
    policy = policy_from_fields(body, default_mode=ModelAccess.ALL)
    await grant_ids(session, policy.allowed_models)
    async with mutation(session):
        row = Org(name=body.name)
        apply_policy(row, policy)
        session.add(row)
        await session.flush()
        await save_grants(session, row, policy)
        await session.refresh(row)
        audit.record(
            session,
            action="org.create",
            resource_type="org",
            resource_id=row.id,
            principal=authz.principal,
            org_id=row.id,
            details={"name": row.name},
        )
    return OrgOut.model_validate(row)


@router.get("/orgs/{org_id}", response_model=OrgOut, operation_id="getOrg")
async def get_org(org_id: uuid.UUID, authz: Authz, session: SessionDep) -> OrgOut:
    """Readable by installation admins and by the org's own admins; anyone else gets 404."""
    row = await _readable_org_or_404(session, authz, org_id)
    return OrgOut.model_validate(row)


def _budget_change_can_clear(old: int | None, new: int | None) -> bool:
    """A budget change that may turn an exceeded org back into a usable one.

    A raise, a removal, or setting a budget where none was (no window row exists then, so
    the delete is a harmless no-op). Lowering never clears, so its window row stays.
    """
    return old is None or new is None or new > old


@router.patch("/orgs/{org_id}", response_model=OrgOut, operation_id="updateOrg")
async def update_org(
    org_id: uuid.UUID,
    body: OrgUpdate,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> OrgOut:
    """Rename or set the org policy; installation admins only."""
    await authz.require(Action.ORG_MANAGE)
    ensure_not_null(body, "name")
    async with mutation(session):
        await lock_scope(session, settings)
        row = await session.get(Org, org_id, with_for_update=True)
        if row is None:
            raise not_found("org not found")
        if row.deleted_at is not None:
            raise conflict("the org is archived")
        base = policy_of(row)
        proposed = policy_from_fields(body, default_mode=ModelAccess.ALL, base=base)
        changed = proposed != base
        if changed:
            await grant_ids(session, proposed.allowed_models)
            apply_policy(row, proposed)
            await save_grants(session, row, proposed)
        if body.name is not None:
            row.name = body.name
        if proposed.budget_nano_usd != base.budget_nano_usd and _budget_change_can_clear(
            base.budget_nano_usd, proposed.budget_nano_usd
        ):
            # The month's "already reported" marker only records the old budget's crossing;
            # without this a later crossing would neither audit nor recompile again.
            await reset_org_budget_window(session, row.id, month_window(datetime.now(UTC))[0])
        await session.flush()
        await session.refresh(row)
        audit.record(
            session,
            action="org.update",
            resource_type="org",
            resource_id=row.id,
            principal=authz.principal,
            org_id=row.id,
            details={
                "name": row.name,
                "policy_changed": changed,
                "changes": body.model_dump(exclude_unset=True, mode="json"),
            },
        )
        if changed:
            await enqueue_recompile(session, settings, "api.org.update")
    return OrgOut.model_validate(row)


@router.delete(
    "/orgs/{org_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="deleteOrg",
)
async def delete_org(
    org_id: uuid.UUID,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> Response:
    """Archive an org. Irreversible; refused while it has a live team."""
    await authz.require(Action.ORG_MANAGE)
    async with mutation(session):
        await lock_scope(session, settings)
        row = await session.get(Org, org_id, with_for_update=True)
        if row is None:
            raise not_found("org not found")
        if row.deleted_at is not None:
            raise conflict("the org is already archived")
        live = await session.scalar(
            select(func.count())
            .select_from(Team)
            .where(Team.org_id == row.id, Team.deleted_at.is_(None))
        )
        if live:
            raise conflict("the org still has live teams; archive them first")
        row.deleted_at = datetime.now(UTC)
        await session.flush()
        audit.record(
            session,
            action="org.archive",
            resource_type="org",
            resource_id=row.id,
            principal=authz.principal,
            org_id=row.id,
            details={"name": row.name},
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/orgs/{org_id}/admins",
    response_model=Page[OrgAdminOut],
    operation_id="listOrgAdmins",
)
async def list_org_admins(
    org_id: uuid.UUID,
    authz: Authz,
    session: SessionDep,
    limit: LimitDep = 50,
    cursor: str | None = None,
) -> Page[OrgAdminOut]:
    """The org's admins: local admin grants plus every installation admin (inherited).

    Installation admins only. Under an external authorization backend the local rows are
    not the source of truth (the backend answers the relation), so the list is
    informational and the writes answer 409. An installation admin is listed with
    `inherited_from: installation` and cannot be removed here.
    """
    await authz.require(Action.ORG_MANAGE)
    await _org_or_404(session, org_id)
    ids = union(
        select(OrgAdmin.user_id.label("user_id")).where(OrgAdmin.org_id == org_id),
        select(InstallationAdmin.user_id),
    ).subquery()
    stmt = select(ids.c.user_id).order_by(ids.c.user_id)
    if cursor is not None:
        stmt = stmt.where(ids.c.user_id > parse_uuid_cursor(cursor))
    page_ids = list((await session.scalars(stmt.limit(limit + 1))).all())
    next_cursor = str(page_ids[limit - 1]) if len(page_ids) > limit else None
    page_ids = page_ids[:limit]
    users = {
        user.id: user for user in await session.scalars(select(User).where(User.id.in_(page_ids)))
    }
    local = {
        row.user_id: row
        for row in await session.scalars(
            select(OrgAdmin).where(OrgAdmin.org_id == org_id, OrgAdmin.user_id.in_(page_ids))
        )
    }
    installation = {
        row.user_id: row
        for row in await session.scalars(
            select(InstallationAdmin).where(InstallationAdmin.user_id.in_(page_ids))
        )
    }
    items = []
    for user_id in page_ids:
        row = local.get(user_id)
        role = installation.get(user_id)
        if row is not None:
            out = _admin_out(row, users[user_id])
        else:
            assert role is not None
            out = _admin_out(
                OrgAdmin(
                    org_id=org_id,
                    user_id=user_id,
                    assigned_by=role.assigned_by,
                    assigned_at=role.assigned_at,
                ),
                users[user_id],
            )
        out.inherited_from = InheritedFrom.INSTALLATION if role is not None else None
        out.direct = row is not None
        items.append(out)
    return Page[OrgAdminOut](items=items, next_cursor=next_cursor)


@router.put(
    "/orgs/{org_id}/admins/{user_id}",
    response_model=OrgAdminOut,
    operation_id="putOrgAdmin",
)
async def put_org_admin(
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    request: Request,
    authz: Authz,
    session: SessionDep,
) -> OrgAdminOut:
    """Give `user_id` the org admin relation. Idempotent; installation admins only."""
    await authz.require(Action.ORG_MANAGE)
    async with mutation(session):
        org = await _org_or_404(session, org_id)
        if org.deleted_at is not None:
            raise conflict("the org is archived")
        ensure_grants_managed(request)
        user = await session.get(User, user_id)
        if user is None:
            raise not_found("user not found")
        row = await session.get(OrgAdmin, (org.id, user.id), with_for_update=True)
        if row is None:
            if not user.is_active:
                raise conflict("the user is not active")
            await require_signed_in(session, user.id)
            row = OrgAdmin(org_id=org.id, user_id=user.id, assigned_by=authz.user_id)
            session.add(row)
            await session.flush()
            audit.record(
                session,
                action="org.admin.assign",
                resource_type="org_admin",
                resource_id=user.id,
                principal=authz.principal,
                org_id=org.id,
                details={"org_id": org.id, "user_id": user.id},
            )
    return _admin_out(row, user)


@router.delete(
    "/orgs/{org_id}/admins/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="deleteOrgAdmin",
)
async def delete_org_admin(
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    request: Request,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
    metrics: MetricsDep,
) -> Response:
    """Remove an org admin relation. Installation admins only.

    The user's keys in the org's teams are suspended (`owner_removed`) where they are
    neither a direct member nor an installation admin.
    """
    await authz.require(Action.ORG_MANAGE)
    async with mutation(session):
        await lock_scope(session, settings)
        org = await _org_or_404(session, org_id)
        if org.deleted_at is not None:
            raise conflict("the org is archived")
        ensure_grants_managed(request)
        row = await session.get(OrgAdmin, (org.id, user_id), with_for_update=True)
        if row is None:
            raise not_found("org admin not found")
        await session.delete(row)
        teams = set(await session.scalars(select(Team.id).where(Team.org_id == org.id)))
        suspended = await suspend_ineligible_user_keys(
            session, authz.authorizer, authz.principal, user_id=user_id, team_ids=teams
        )
        audit.record(
            session,
            action="org.admin.remove",
            resource_type="org_admin",
            resource_id=user_id,
            principal=authz.principal,
            org_id=org.id,
            details={"org_id": org.id, "user_id": user_id, "suspended_keys": suspended},
        )
        if suspended:
            await enqueue_recompile(session, settings, "api.org.admin.delete")
    metrics.count_committed_suspensions(session)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/orgs/{org_id}/usage", response_model=OrgUsageOut, operation_id="getOrgUsage")
async def get_org_usage(
    org_id: uuid.UUID,
    authz: Authz,
    session: SessionDep,
    from_: FromQuery = None,
    to_: ToQuery = None,
) -> OrgUsageOut:
    """Spend per team, summed from `usage_events` at query time.

    Archived teams are included (their history stays attributable), and every team of the
    org appears even without events. The default range is the current UTC month.
    """
    org = await _readable_org_or_404(session, authz, org_id)
    start, end = month_window(datetime.now(UTC))
    start = aware(from_, "from") or start
    end = aware(to_, "to") or end
    if end <= start:
        raise validation_error("to", "must be after from")
    ensure_range(start, end)

    event_filter = and_(
        UsageEvent.team_id == Team.id,
        UsageEvent.ts >= start,
        UsageEvent.ts < end,
    )
    rows = (
        await session.execute(
            select(
                Team.id,
                Team.name,
                Team.deleted_at,
                func.count(UsageEvent.id),
                func.coalesce(func.sum(UsageEvent.prompt_tokens), 0),
                func.coalesce(func.sum(UsageEvent.completion_tokens), 0),
                func.coalesce(func.sum(UsageEvent.cost_nano_usd), 0),
                func.coalesce(func.sum(case((UsageEvent.cost_known.is_(False), 1), else_=0)), 0),
            )
            .select_from(Team)
            .outerjoin(UsageEvent, event_filter)
            .where(Team.org_id == org.id)
            .group_by(Team.id, Team.name, Team.deleted_at)
            .order_by(Team.name, Team.id)
        )
    ).all()
    teams = [
        OrgTeamUsageOut(
            team_id=team_id,
            team_name=name,
            archived=deleted_at is not None,
            requests=requests,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            cost_nano_usd=cost,
            unknown_cost_count=unknown,
        )
        for (
            team_id,
            name,
            deleted_at,
            requests,
            prompt_tokens,
            completion_tokens,
            cost,
            unknown,
        ) in rows
    ]
    return OrgUsageOut(
        org_id=org.id,
        org_name=org.name,
        teams=teams,
        requests=sum(row.requests for row in teams),
        prompt_tokens=sum(row.prompt_tokens for row in teams),
        completion_tokens=sum(row.completion_tokens for row in teams),
        total_tokens=sum(row.total_tokens for row in teams),
        cost_nano_usd=sum(row.cost_nano_usd for row in teams),
        unknown_cost_count=sum(row.unknown_cost_count for row in teams),
    )
