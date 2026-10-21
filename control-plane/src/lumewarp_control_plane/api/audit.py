"""Scoped audit log reads.

Audit rows carry `org_id` and an optional `team_id`. Org owners/admins may read the whole
org; team admins may read their own team; members/viewers cannot read audit at all.
Installation-wide rows (org_id NULL) are superuser-only.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import or_, select

from lumewarp_control_plane import rbac
from lumewarp_control_plane.api.common import (
    LimitDep,
    SessionDep,
    aware,
    parse_int_cursor,
    validation_error,
)
from lumewarp_control_plane.api.schemas import AuditOut, Page
from lumewarp_control_plane.errors import forbidden, not_found
from lumewarp_control_plane.models import AuditLog, Org, Team
from lumewarp_control_plane.security import CurrentPrincipal

FromQuery = Annotated[datetime | None, Query(alias="from")]
ToQuery = Annotated[datetime | None, Query(alias="to")]

router = APIRouter(tags=["audit"])


def _audit_scope(principal: CurrentPrincipal) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
    org_admins: set[uuid.UUID] = set()
    team_admins: set[uuid.UUID] = set()
    for grant in principal.grants:
        if grant.team_id is None and grant.role in {"owner", "admin"}:
            org_admins.add(grant.org_id)
        elif grant.team_id is not None and grant.role == "admin":
            team_admins.add(grant.team_id)
    return org_admins, team_admins


@router.get("/audit", response_model=Page[AuditOut], operation_id="listAudit")
async def list_audit(
    principal: CurrentPrincipal,
    session: SessionDep,
    org_id: uuid.UUID | None = None,
    team_id: uuid.UUID | None = None,
    actor_id: str | None = None,
    action: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    from_: FromQuery = None,
    to_: ToQuery = None,
    limit: LimitDep = 50,
    cursor: str | None = None,
) -> Page[AuditOut]:
    start = aware(from_, "from")
    end = aware(to_, "to")
    org_admins, team_admins = _audit_scope(principal)

    if team_id is not None:
        team = await session.get(Team, team_id)
        if team is None:
            raise not_found("team not found")
        if org_id is not None and org_id != team.org_id:
            raise not_found("team_id does not match org_id")
        org_id = team.org_id
        if not principal.is_superuser and not rbac.authorize(
            principal, rbac.Action.AUDIT_READ, rbac.ResourceScope(team.org_id, team.id)
        ):
            raise forbidden("audit.read not allowed")
    elif org_id is not None:
        org = await session.get(Org, org_id)
        if org is None:
            raise not_found("org not found")
        if not principal.is_superuser and not rbac.authorize(
            principal, rbac.Action.AUDIT_READ, rbac.ResourceScope(org.id)
        ):
            raise forbidden("audit.read not allowed")

    stmt = select(AuditLog).order_by(AuditLog.id.desc())
    if not principal.is_superuser:
        if not org_admins and not team_admins:
            raise forbidden("audit.read not allowed")
        clauses = []
        if org_admins:
            clauses.append(AuditLog.org_id.in_(org_admins))
        if team_admins:
            clauses.append(AuditLog.team_id.in_(team_admins))
        stmt = stmt.where(or_(*clauses))
    if org_id is not None:
        stmt = stmt.where(AuditLog.org_id == org_id)
    if team_id is not None:
        stmt = stmt.where(AuditLog.team_id == team_id)
    if actor_id is not None:
        stmt = stmt.where(AuditLog.actor_id == actor_id)
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    if resource_type is not None:
        stmt = stmt.where(AuditLog.resource_type == resource_type)
    if resource_id is not None:
        stmt = stmt.where(AuditLog.resource_id == resource_id)
    if start is not None:
        stmt = stmt.where(AuditLog.ts >= start)
    if end is not None:
        stmt = stmt.where(AuditLog.ts < end)
    if start is not None and end is not None and end <= start:
        raise validation_error("to", "must be after from")
    if cursor is not None:
        stmt = stmt.where(AuditLog.id < parse_int_cursor(cursor))
    rows = list((await session.scalars(stmt.limit(limit + 1))).all())
    next_cursor = str(rows[limit - 1].id) if len(rows) > limit else None
    return Page[AuditOut](
        items=[AuditOut.model_validate(row) for row in rows[:limit]],
        next_cursor=next_cursor,
    )
