"""Organizations: CRUD and policy validation."""

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
    mutation,
    uuid_page,
)
from lumewarp_control_plane.api.policy import (
    apply_policy,
    policy_from_fields,
    policy_of,
    validate_descendants_after_org_update,
)
from lumewarp_control_plane.api.schemas import OrgCreate, OrgOut, OrgUpdate, Page
from lumewarp_control_plane.errors import not_found
from lumewarp_control_plane.models import ModelAccess, Org, Team
from lumewarp_control_plane.security import CurrentPrincipal

router = APIRouter(tags=["orgs"])


@router.get("/orgs", response_model=Page[OrgOut], operation_id="listOrgs")
async def list_orgs(
    principal: CurrentPrincipal,
    session: SessionDep,
    limit: LimitDep = 50,
    cursor: str | None = None,
) -> Page[OrgOut]:
    stmt = select(Org).order_by(Org.id)
    readable = rbac.readable_org_ids(principal)
    if readable is not None:
        stmt = stmt.where(Org.id.in_(readable))
    rows, next_cursor = await uuid_page(session, stmt, Org.id, limit, cursor)
    return Page[OrgOut](items=[OrgOut.model_validate(row) for row in rows], next_cursor=next_cursor)


@router.post(
    "/orgs",
    response_model=OrgOut,
    status_code=status.HTTP_201_CREATED,
    operation_id="createOrg",
)
async def create_org(
    body: OrgCreate,
    principal: CurrentPrincipal,
    session: SessionDep,
    settings: SettingsDep,
) -> OrgOut:
    rbac.require(principal, rbac.Action.ORG_CREATE, rbac.ResourceScope())
    policy = policy_from_fields(body, default_mode=ModelAccess.ALL)
    async with mutation(session):
        row = Org(name=body.name)
        apply_policy(row, policy)
        session.add(row)
        await session.flush()
        await session.refresh(row)
        audit.record(
            session,
            action="org.create",
            resource_type="org",
            resource_id=row.id,
            principal=principal,
            org_id=row.id,
            details={"name": row.name},
        )
        await enqueue_recompile(session, settings, "api.org.create")
    return OrgOut.model_validate(row)


@router.get("/orgs/{org_id}", response_model=OrgOut, operation_id="getOrg")
async def get_org(org_id: uuid.UUID, principal: CurrentPrincipal, session: SessionDep) -> OrgOut:
    row = await session.get(Org, org_id)
    if row is None:
        raise not_found("org not found")
    ensure_org_read(principal, row)
    return OrgOut.model_validate(row)


@router.patch("/orgs/{org_id}", response_model=OrgOut, operation_id="updateOrg")
async def update_org(
    org_id: uuid.UUID,
    body: OrgUpdate,
    principal: CurrentPrincipal,
    session: SessionDep,
    settings: SettingsDep,
) -> OrgOut:
    async with mutation(session):
        ensure_not_null(body, "name")
        row = await session.get(Org, org_id)
        if row is None:
            raise not_found("org not found")
        ensure_org_read(principal, row)
        rbac.require(principal, rbac.Action.ORG_UPDATE, rbac.ResourceScope(row.id))
        base = policy_of(row)
        proposed = policy_from_fields(body, default_mode=ModelAccess.ALL, base=base)
        changed = proposed != base
        if changed:
            await validate_descendants_after_org_update(session, row, proposed)
            apply_policy(row, proposed)
        if body.name is not None:
            row.name = body.name
        await session.flush()
        await session.refresh(row)
        audit.record(
            session,
            action="org.update",
            resource_type="org",
            resource_id=row.id,
            principal=principal,
            org_id=row.id,
            details={
                "name": row.name,
                "policy_changed": changed,
                "changes": body.model_dump(exclude_unset=True, mode="json"),
            },
        )
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
    principal: CurrentPrincipal,
    session: SessionDep,
    settings: SettingsDep,
) -> Response:
    rbac.require(principal, rbac.Action.ORG_DELETE, rbac.ResourceScope())
    async with mutation(session):
        row = await session.get(Org, org_id)
        if row is None:
            raise not_found("org not found")
        team_count = await session.scalar(select(Team.id).where(Team.org_id == row.id).limit(1))
        if team_count is not None:
            raise conflict("org still has teams; delete or move them first")
        await session.delete(row)
        audit.record(
            session,
            action="org.delete",
            resource_type="org",
            resource_id=row.id,
            principal=principal,
            org_id=row.id,
            details={"name": row.name},
        )
        await enqueue_recompile(session, settings, "api.org.delete")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
