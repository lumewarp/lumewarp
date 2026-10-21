"""Membership grants (org-level owner/admin, team-level admin/member/viewer)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane import audit, rbac
from lumewarp_control_plane.api.common import (
    LimitDep,
    SessionDep,
    mutation,
    uuid_page,
    validation_error,
)
from lumewarp_control_plane.api.schemas import (
    MembershipCreate,
    MembershipOut,
    MembershipUpdate,
    Page,
)
from lumewarp_control_plane.errors import not_found
from lumewarp_control_plane.models import Membership, Org, Team, User
from lumewarp_control_plane.security import CurrentPrincipal

router = APIRouter(tags=["memberships"])

ORG_ROLES = {"owner", "admin"}
TEAM_ROLES = {"admin", "member", "viewer"}


async def _get_membership(session: AsyncSession, membership_id: uuid.UUID) -> Membership:
    row = await session.get(Membership, membership_id)
    if row is None:
        raise not_found("membership not found")
    return row


def _require_manage(
    principal: CurrentPrincipal, membership: Membership, role: str | None = None
) -> None:
    scope = rbac.ResourceScope(membership.org_id, membership.team_id)
    if membership.team_id is not None:
        rbac.require(principal, rbac.Action.TEAM_MEMBERSHIP_MANAGE, scope)
    elif role == "owner":
        rbac.require(principal, rbac.Action.ORG_GRANT_OWNER, scope)
    else:
        rbac.require(principal, rbac.Action.ORG_MEMBERSHIP_MANAGE, scope)


@router.get("/memberships", response_model=Page[MembershipOut], operation_id="listMemberships")
async def list_memberships(
    principal: CurrentPrincipal,
    session: SessionDep,
    org_id: uuid.UUID | None = None,
    team_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    limit: LimitDep = 50,
    cursor: str | None = None,
) -> Page[MembershipOut]:
    if team_id is not None:
        team = await session.get(Team, team_id)
        if team is None:
            raise not_found("team not found")
        if org_id is not None and org_id != team.org_id:
            raise not_found("team_id does not match org_id")
        rbac.require(
            principal,
            rbac.Action.TEAM_MEMBERSHIP_MANAGE,
            rbac.ResourceScope(team.org_id, team.id),
        )
    elif org_id is not None:
        org = await session.get(Org, org_id)
        if org is None:
            raise not_found("org not found")
        rbac.require(principal, rbac.Action.ORG_MEMBERSHIP_MANAGE, rbac.ResourceScope(org.id))
    elif not principal.is_superuser:
        raise validation_error("org_id", "required unless filtering as a superuser")

    stmt = select(Membership).order_by(Membership.id)
    if org_id is not None:
        stmt = stmt.where(Membership.org_id == org_id)
    if team_id is not None:
        stmt = stmt.where(Membership.team_id == team_id)
    if user_id is not None:
        stmt = stmt.where(Membership.user_id == user_id)
    rows, next_cursor = await uuid_page(session, stmt, Membership.id, limit, cursor)
    return Page[MembershipOut](
        items=[MembershipOut.model_validate(row) for row in rows],
        next_cursor=next_cursor,
    )


@router.post(
    "/memberships",
    response_model=MembershipOut,
    status_code=status.HTTP_201_CREATED,
    operation_id="createMembership",
)
async def create_membership(
    body: MembershipCreate, principal: CurrentPrincipal, session: SessionDep
) -> MembershipOut:
    async with mutation(session):
        user = await session.get(User, body.user_id)
        if user is None:
            raise validation_error("user_id", "unknown user")
        org = await session.get(Org, body.org_id)
        if org is None:
            raise validation_error("org_id", "unknown org")
        team: Team | None = None
        if body.team_id is not None:
            team = await session.get(Team, body.team_id)
            if team is None:
                raise validation_error("team_id", "unknown team")
            if team.org_id != org.id:
                raise not_found("team_id does not match org_id")
            if body.role not in TEAM_ROLES:
                raise validation_error("role", "team roles are admin, member, viewer")
            rbac.require(
                principal,
                rbac.Action.TEAM_MEMBERSHIP_MANAGE,
                rbac.ResourceScope(org.id, team.id),
            )
        else:
            if body.role not in ORG_ROLES:
                raise validation_error("role", "org roles are owner, admin")
            if body.role == "owner":
                rbac.require(principal, rbac.Action.ORG_GRANT_OWNER, rbac.ResourceScope(org.id))
            else:
                rbac.require(
                    principal,
                    rbac.Action.ORG_MEMBERSHIP_MANAGE,
                    rbac.ResourceScope(org.id),
                )
        row = Membership(
            user_id=user.id,
            org_id=org.id,
            team_id=team.id if team is not None else None,
            role=body.role,
        )
        session.add(row)
        await session.flush()
        await session.refresh(row)
        audit.record(
            session,
            action="membership.create",
            resource_type="membership",
            resource_id=row.id,
            principal=principal,
            org_id=row.org_id,
            team_id=row.team_id,
            details={"user_id": str(row.user_id), "role": row.role},
        )
    return MembershipOut.model_validate(row)


@router.patch(
    "/memberships/{membership_id}",
    response_model=MembershipOut,
    operation_id="updateMembership",
)
async def update_membership(
    membership_id: uuid.UUID,
    body: MembershipUpdate,
    principal: CurrentPrincipal,
    session: SessionDep,
) -> MembershipOut:
    async with mutation(session):
        row = await _get_membership(session, membership_id)
        if row.team_id is not None:
            if body.role not in TEAM_ROLES:
                raise validation_error("role", "team roles are admin, member, viewer")
        elif body.role not in ORG_ROLES:
            raise validation_error("role", "org roles are owner, admin")
        _require_manage(principal, row, body.role)
        row.role = body.role
        await session.flush()
        await session.refresh(row)
        audit.record(
            session,
            action="membership.update",
            resource_type="membership",
            resource_id=row.id,
            principal=principal,
            org_id=row.org_id,
            team_id=row.team_id,
            details={"user_id": str(row.user_id), "role": row.role},
        )
    return MembershipOut.model_validate(row)


@router.delete(
    "/memberships/{membership_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="deleteMembership",
)
async def delete_membership(
    membership_id: uuid.UUID, principal: CurrentPrincipal, session: SessionDep
) -> Response:
    async with mutation(session):
        row = await _get_membership(session, membership_id)
        _require_manage(principal, row)
        await session.delete(row)
        audit.record(
            session,
            action="membership.delete",
            resource_type="membership",
            resource_id=row.id,
            principal=principal,
            org_id=row.org_id,
            team_id=row.team_id,
            details={"user_id": str(row.user_id), "role": row.role},
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
