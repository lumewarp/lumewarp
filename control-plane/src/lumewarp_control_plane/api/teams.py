"""Teams: the top-level grouping that owns members, service accounts and keys."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Response, status
from sqlalchemy import select

from lumewarp_control_plane import audit
from lumewarp_control_plane.api.common import (
    LimitDep,
    SessionDep,
    SettingsDep,
    conflict,
    enqueue_recompile,
    ensure_not_null,
    get_team_or_404,
    lock_scope,
    mutation,
    uuid_page,
)
from lumewarp_control_plane.api.policy import (
    apply_policy,
    grant_ids,
    policy_from_fields,
    policy_of,
    save_grants,
    validate_owners_after_team_update,
)
from lumewarp_control_plane.api.schemas import Page, TeamBasicOut, TeamCreate, TeamOut, TeamUpdate
from lumewarp_control_plane.authz import Action, Authz
from lumewarp_control_plane.errors import not_found
from lumewarp_control_plane.models import ApiKey, ModelAccess, ServiceAccount, Team

router = APIRouter(tags=["teams"])


@router.get(
    "/teams",
    response_model=Page[TeamOut | TeamBasicOut],
    operation_id="listTeams",
)
async def list_teams(
    authz: Authz,
    session: SessionDep,
    limit: LimitDep = 50,
    cursor: str | None = None,
) -> Page[TeamOut | TeamBasicOut]:
    stmt = select(Team).order_by(Team.id)
    is_admin = await authz.is_admin()
    if not is_admin:
        candidates = set(await session.scalars(select(Team.id)))
        readable = await authz.readable_teams(candidates)
        if readable is not None:
            stmt = stmt.where(Team.id.in_(readable))
    rows, next_cursor = await uuid_page(session, stmt, Team.id, limit, cursor)
    # The full team policy only goes to team admins; every other member gets the basic
    # record and reads their own effective policy from /users/me.
    admin_teams = None if is_admin else await authz.admin_teams([row.id for row in rows])
    items = [
        TeamOut.model_validate(row)
        if admin_teams is None or row.id in admin_teams
        else TeamBasicOut.model_validate(row)
        for row in rows
    ]
    return Page[TeamOut | TeamBasicOut](items=items, next_cursor=next_cursor)


@router.post(
    "/teams",
    response_model=TeamOut,
    status_code=status.HTTP_201_CREATED,
    operation_id="createTeam",
)
async def create_team(
    body: TeamCreate,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> TeamOut:
    await authz.require(Action.TEAM_CREATE)
    async with mutation(session):
        await lock_scope(session, settings)
        policy = policy_from_fields(body, default_mode=ModelAccess.ALL)
        row = Team(
            name=body.name,
            max_keys_per_member=body.max_keys_per_member,
            max_member_key_lifetime_seconds=body.max_member_key_lifetime_seconds,
        )
        apply_policy(row, policy)
        session.add(row)
        await session.flush()
        await save_grants(session, row, policy)
        await session.refresh(row)
        audit.record(
            session,
            action="team.create",
            resource_type="team",
            resource_id=row.id,
            principal=authz.principal,
            team_id=row.id,
            details={"name": row.name},
        )
        await enqueue_recompile(session, settings, "api.team.create")
    return TeamOut.model_validate(row)


@router.get(
    "/teams/{team_id}",
    response_model=TeamOut | TeamBasicOut,
    operation_id="getTeam",
)
async def get_team(team_id: uuid.UUID, authz: Authz, session: SessionDep) -> TeamOut | TeamBasicOut:
    row = await get_team_or_404(session, authz, team_id)
    if await authz.allowed(Action.TEAM_ADMIN_READ, row.id):
        return TeamOut.model_validate(row)
    return TeamBasicOut.model_validate(row)


@router.patch("/teams/{team_id}", response_model=TeamOut, operation_id="updateTeam")
async def update_team(
    team_id: uuid.UUID,
    body: TeamUpdate,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> TeamOut:
    async with mutation(session):
        await lock_scope(session, settings)
        ensure_not_null(body, "name")
        row = await get_team_or_404(session, authz, team_id)
        base = policy_of(row)
        proposed = policy_from_fields(body, default_mode=ModelAccess.ALL, base=base)
        changed = proposed != base
        key_limit_fields = ("max_keys_per_member", "max_member_key_lifetime_seconds")
        key_limits_changed = any(
            field in body.model_fields_set and getattr(body, field) != getattr(row, field)
            for field in key_limit_fields
        )
        settings_changed = (body.name is not None and body.name != row.name) or key_limits_changed
        # Decide from what actually changes: a form that resends the current policy must
        # still be usable by a team admin who may not set quota.
        if changed:
            await authz.require(Action.TEAM_QUOTA_SET, row.id)
        if settings_changed or not changed:
            await authz.require(Action.TEAM_SETTINGS_UPDATE, row.id)
        if changed:
            await grant_ids(session, proposed.allowed_models)
            await validate_owners_after_team_update(session, row, proposed)
            apply_policy(row, proposed)
            await save_grants(session, row, proposed)
        if body.name is not None:
            row.name = body.name
        for field in key_limit_fields:
            if field in body.model_fields_set:
                setattr(row, field, getattr(body, field))
        await session.flush()
        await session.refresh(row)
        audit.record(
            session,
            action="team.update",
            resource_type="team",
            resource_id=row.id,
            principal=authz.principal,
            team_id=row.id,
            details={
                "name": row.name,
                "policy_changed": changed,
                "changes": body.model_dump(exclude_unset=True, mode="json"),
            },
        )
        await enqueue_recompile(session, settings, "api.team.update")
    return TeamOut.model_validate(row)


@router.delete(
    "/teams/{team_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="deleteTeam",
)
async def delete_team(
    team_id: uuid.UUID,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> Response:
    async with mutation(session):
        await lock_scope(session, settings)
        row = await session.get(Team, team_id)
        if row is None:
            raise not_found("team not found")
        if not await authz.allowed(Action.TEAM_READ, row.id):
            raise not_found("team not found")
        await authz.require(Action.TEAM_DELETE, row.id)
        key = await session.scalar(select(ApiKey.id).where(ApiKey.team_id == row.id).limit(1))
        if key is not None:
            raise conflict("team still has keys (kept for usage history); revoke is not enough")
        account = await session.scalar(
            select(ServiceAccount.id).where(ServiceAccount.team_id == row.id).limit(1)
        )
        if account is not None:
            raise conflict("team still has service accounts; delete them first")
        await session.delete(row)
        audit.record(
            session,
            action="team.delete",
            resource_type="team",
            resource_id=row.id,
            principal=authz.principal,
            team_id=row.id,
            details={"name": row.name},
        )
        await enqueue_recompile(session, settings, "api.team.delete")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
