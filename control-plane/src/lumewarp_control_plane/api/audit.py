"""Scoped audit log reads.

Audit rows carry an optional `team_id`. Admin group members read everything (including
installation-wide rows with no team); everyone else reads the rows of the teams they are
a member of.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import select

from lumewarp_control_plane.api.common import (
    LimitDep,
    SessionDep,
    aware,
    ensure_range,
    ensure_team_read,
    parse_int_cursor,
    validation_error,
)
from lumewarp_control_plane.api.schemas import AuditOut, Page
from lumewarp_control_plane.authz import Authz
from lumewarp_control_plane.errors import not_found
from lumewarp_control_plane.models import AuditLog, Team

FromQuery = Annotated[datetime | None, Query(alias="from")]
ToQuery = Annotated[datetime | None, Query(alias="to")]

router = APIRouter(tags=["audit"])


@router.get("/audit", response_model=Page[AuditOut], operation_id="listAudit")
async def list_audit(
    authz: Authz,
    session: SessionDep,
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

    stmt = select(AuditLog).order_by(AuditLog.id.desc())
    if team_id is not None:
        if await session.get(Team, team_id) is None:
            # A deleted team's rows stay readable to admin group members only.
            if not await authz.is_admin():
                raise not_found("team not found")
        else:
            await ensure_team_read(authz, team_id)
        stmt = stmt.where(AuditLog.team_id == team_id)
    else:
        readable = await authz.readable_teams(set(await session.scalars(select(Team.id))))
        if readable is not None:
            stmt = stmt.where(AuditLog.team_id.in_(readable))
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
    if start is not None and end is not None:
        ensure_range(start, end)
    if cursor is not None:
        stmt = stmt.where(AuditLog.id < parse_int_cursor(cursor))
    rows = list((await session.scalars(stmt.limit(limit + 1))).all())
    next_cursor = str(rows[limit - 1].id) if len(rows) > limit else None
    return Page[AuditOut](
        items=[AuditOut.model_validate(row) for row in rows[:limit]],
        next_cursor=next_cursor,
    )
