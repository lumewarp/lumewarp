"""Projects under a team."""

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
    ensure_parent_matches,
    ensure_project_read,
    ensure_team_read,
    lock_scope,
    mutation,
    uuid_page,
)
from lumewarp_control_plane.api.policy import (
    apply_policy,
    check_narrowing,
    policy_from_fields,
    policy_of,
    team_effective,
    validate_descendants_after_project_update,
)
from lumewarp_control_plane.api.schemas import (
    Page,
    ProjectCreate,
    ProjectOut,
    ProjectUpdate,
)
from lumewarp_control_plane.errors import not_found
from lumewarp_control_plane.models import ApiKey, ModelAccess, Project, Team
from lumewarp_control_plane.security import CurrentPrincipal

router = APIRouter(tags=["projects"])


@router.get(
    "/teams/{team_id}/projects",
    response_model=Page[ProjectOut],
    operation_id="listProjects",
)
async def list_projects(
    team_id: uuid.UUID,
    principal: CurrentPrincipal,
    session: SessionDep,
    limit: LimitDep = 50,
    cursor: str | None = None,
) -> Page[ProjectOut]:
    team = await session.get(Team, team_id)
    if team is None:
        raise not_found("team not found")
    ensure_team_read(principal, team)
    stmt = select(Project).where(Project.team_id == team.id).order_by(Project.id)
    rows, next_cursor = await uuid_page(session, stmt, Project.id, limit, cursor)
    return Page[ProjectOut](
        items=[ProjectOut.model_validate(row) for row in rows], next_cursor=next_cursor
    )


@router.post(
    "/teams/{team_id}/projects",
    response_model=ProjectOut,
    status_code=status.HTTP_201_CREATED,
    operation_id="createProject",
)
async def create_project(
    team_id: uuid.UUID,
    body: ProjectCreate,
    principal: CurrentPrincipal,
    session: SessionDep,
    settings: SettingsDep,
) -> ProjectOut:
    async with mutation(session):
        await lock_scope(session, settings)
        team = await session.get(Team, team_id)
        if team is None:
            raise not_found("team not found")
        ensure_team_read(principal, team)
        rbac.require(
            principal, rbac.Action.PROJECT_CREATE, rbac.ResourceScope(team.org_id, team.id)
        )
        ensure_parent_matches(body.team_id, team.id, "team_id")
        ensure_parent_matches(body.org_id, team.org_id, "org_id")
        policy = policy_from_fields(body, default_mode=ModelAccess.INHERIT)
        check_narrowing(await team_effective(session, team), policy)
        row = Project(org_id=team.org_id, team_id=team.id, name=body.name)
        apply_policy(row, policy)
        session.add(row)
        await session.flush()
        await session.refresh(row)
        audit.record(
            session,
            action="project.create",
            resource_type="project",
            resource_id=row.id,
            principal=principal,
            org_id=row.org_id,
            team_id=row.team_id,
            details={"name": row.name},
        )
        await enqueue_recompile(session, settings, "api.project.create")
    return ProjectOut.model_validate(row)


@router.get("/projects/{project_id}", response_model=ProjectOut, operation_id="getProject")
async def get_project(
    project_id: uuid.UUID, principal: CurrentPrincipal, session: SessionDep
) -> ProjectOut:
    row = await session.get(Project, project_id)
    if row is None:
        raise not_found("project not found")
    ensure_project_read(principal, row)
    return ProjectOut.model_validate(row)


@router.patch("/projects/{project_id}", response_model=ProjectOut, operation_id="updateProject")
async def update_project(
    project_id: uuid.UUID,
    body: ProjectUpdate,
    principal: CurrentPrincipal,
    session: SessionDep,
    settings: SettingsDep,
) -> ProjectOut:
    async with mutation(session):
        await lock_scope(session, settings)
        ensure_not_null(body, "name")
        row = await session.get(Project, project_id)
        if row is None:
            raise not_found("project not found")
        ensure_project_read(principal, row)
        rbac.require(
            principal,
            rbac.Action.PROJECT_UPDATE,
            rbac.ResourceScope(row.org_id, row.team_id),
        )
        ensure_parent_matches(body.team_id, row.team_id, "team_id")
        ensure_parent_matches(body.org_id, row.org_id, "org_id")
        team = await session.get(Team, row.team_id)
        if team is None:
            raise not_found("project not found")
        base = policy_of(row)
        proposed = policy_from_fields(body, default_mode=ModelAccess.INHERIT, base=base)
        parent = await team_effective(session, team)
        check_narrowing(parent, proposed)
        changed = proposed != base
        if changed:
            await validate_descendants_after_project_update(session, row, parent, proposed)
            apply_policy(row, proposed)
        if body.name is not None:
            row.name = body.name
        await session.flush()
        await session.refresh(row)
        audit.record(
            session,
            action="project.update",
            resource_type="project",
            resource_id=row.id,
            principal=principal,
            org_id=row.org_id,
            team_id=row.team_id,
            details={
                "name": row.name,
                "policy_changed": changed,
                "changes": body.model_dump(exclude_unset=True, mode="json"),
            },
        )
        await enqueue_recompile(session, settings, "api.project.update")
    return ProjectOut.model_validate(row)


@router.delete(
    "/projects/{project_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="deleteProject",
)
async def delete_project(
    project_id: uuid.UUID,
    principal: CurrentPrincipal,
    session: SessionDep,
    settings: SettingsDep,
) -> Response:
    async with mutation(session):
        row = await session.get(Project, project_id)
        if row is None:
            raise not_found("project not found")
        ensure_project_read(principal, row)
        rbac.require(
            principal,
            rbac.Action.PROJECT_DELETE,
            rbac.ResourceScope(row.org_id, row.team_id),
        )
        key = await session.scalar(select(ApiKey.id).where(ApiKey.project_id == row.id).limit(1))
        if key is not None:
            raise conflict(
                "project has keys; keys are kept for usage history even once revoked, "
                "so a project that ever had a key cannot be deleted"
            )
        await session.delete(row)
        audit.record(
            session,
            action="project.delete",
            resource_type="project",
            resource_id=row.id,
            principal=principal,
            org_id=row.org_id,
            team_id=row.team_id,
            details={"name": row.name},
        )
        await enqueue_recompile(session, settings, "api.project.delete")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
