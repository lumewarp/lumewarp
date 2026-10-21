"""Teams under an organization."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Response, status
from sqlalchemy import select

from lumewarp_control_plane import audit, rbac
from lumewarp_control_plane.api.common import (
    LimitDep,
    SessionDep,
    SettingsDep,
    conflict,
    enqueue_recompile,
    ensure_not_null,
    ensure_org_read,
    ensure_parent_matches,
    ensure_team_read,
    lock_scope,
    mutation,
    uuid_page,
)
from lumewarp_control_plane.api.policy import (
    apply_policy,
    check_narrowing,
    org_effective,
    policy_from_fields,
    policy_of,
    validate_descendants_after_team_update,
)
from lumewarp_control_plane.api.schemas import Page, TeamCreate, TeamOut, TeamUpdate
from lumewarp_control_plane.errors import not_found
from lumewarp_control_plane.models import ModelAccess, Org, Project, Team
from lumewarp_control_plane.security import CurrentPrincipal

router = APIRouter(tags=["teams"])


@router.get("/orgs/{org_id}/teams", response_model=Page[TeamOut], operation_id="listTeams")
async def list_teams(
    org_id: uuid.UUID,
    principal: CurrentPrincipal,
    session: SessionDep,
    limit: LimitDep = 50,
    cursor: str | None = None,
) -> Page[TeamOut]:
    org = await session.get(Org, org_id)
    if org is None:
        raise not_found("org not found")
    ensure_org_read(principal, org)
    stmt = select(Team).where(Team.org_id == org.id).order_by(Team.id)
    readable = rbac.readable_team_ids(principal, org.id)
    if readable is not None:
        stmt = stmt.where(Team.id.in_(readable))
    rows, next_cursor = await uuid_page(session, stmt, Team.id, limit, cursor)
    return Page[TeamOut](
        items=[TeamOut.model_validate(row) for row in rows], next_cursor=next_cursor
    )


@router.post(
    "/orgs/{org_id}/teams",
    response_model=TeamOut,
    status_code=status.HTTP_201_CREATED,
    operation_id="createTeam",
)
async def create_team(
    org_id: uuid.UUID,
    body: TeamCreate,
    principal: CurrentPrincipal,
    session: SessionDep,
    settings: SettingsDep,
) -> TeamOut:
    async with mutation(session):
        await lock_scope(session, settings)
        org = await session.get(Org, org_id)
        if org is None:
            raise not_found("org not found")
        ensure_org_read(principal, org)
        rbac.require(principal, rbac.Action.TEAM_CREATE, rbac.ResourceScope(org.id))
        ensure_parent_matches(body.org_id, org.id, "org_id")
        policy = policy_from_fields(body, default_mode=ModelAccess.INHERIT)
        check_narrowing(await org_effective(session, org), policy)
        row = Team(org_id=org.id, name=body.name)
        apply_policy(row, policy)
        session.add(row)
        await session.flush()
        await session.refresh(row)
        audit.record(
            session,
            action="team.create",
            resource_type="team",
            resource_id=row.id,
            principal=principal,
            org_id=row.org_id,
            team_id=row.id,
            details={"name": row.name},
        )
        await enqueue_recompile(session, settings, "api.team.create")
    return TeamOut.model_validate(row)


@router.get("/teams/{team_id}", response_model=TeamOut, operation_id="getTeam")
async def get_team(team_id: uuid.UUID, principal: CurrentPrincipal, session: SessionDep) -> TeamOut:
    row = await session.get(Team, team_id)
    if row is None:
        raise not_found("team not found")
    ensure_team_read(principal, row)
    return TeamOut.model_validate(row)


@router.patch("/teams/{team_id}", response_model=TeamOut, operation_id="updateTeam")
async def update_team(
    team_id: uuid.UUID,
    body: TeamUpdate,
    principal: CurrentPrincipal,
    session: SessionDep,
    settings: SettingsDep,
) -> TeamOut:
    async with mutation(session):
        await lock_scope(session, settings)
        ensure_not_null(body, "name")
        row = await session.get(Team, team_id)
        if row is None:
            raise not_found("team not found")
        ensure_team_read(principal, row)
        rbac.require(principal, rbac.Action.TEAM_UPDATE, rbac.ResourceScope(row.org_id, row.id))
        ensure_parent_matches(body.org_id, row.org_id, "org_id")
        org = await session.get(Org, row.org_id)
        if org is None:
            raise not_found("team not found")
        base = policy_of(row)
        proposed = policy_from_fields(body, default_mode=ModelAccess.INHERIT, base=base)
        parent = await org_effective(session, org)
        check_narrowing(parent, proposed)
        changed = proposed != base
        if changed:
            await validate_descendants_after_team_update(session, row, parent, proposed)
            apply_policy(row, proposed)
        if body.name is not None:
            row.name = body.name
        await session.flush()
        await session.refresh(row)
        audit.record(
            session,
            action="team.update",
            resource_type="team",
            resource_id=row.id,
            principal=principal,
            org_id=row.org_id,
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
    principal: CurrentPrincipal,
    session: SessionDep,
    settings: SettingsDep,
) -> Response:
    async with mutation(session):
        row = await session.get(Team, team_id)
        if row is None:
            raise not_found("team not found")
        ensure_team_read(principal, row)
        rbac.require(principal, rbac.Action.TEAM_DELETE, rbac.ResourceScope(row.org_id, row.id))
        project = await session.scalar(select(Project.id).where(Project.team_id == row.id).limit(1))
        if project is not None:
            raise conflict("team still has projects; delete or move them first")
        await session.delete(row)
        audit.record(
            session,
            action="team.delete",
            resource_type="team",
            resource_id=row.id,
            principal=principal,
            org_id=row.org_id,
            team_id=row.id,
            details={"name": row.name},
        )
        await enqueue_recompile(session, settings, "api.team.delete")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
