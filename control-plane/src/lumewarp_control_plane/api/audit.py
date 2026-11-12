"""Scoped audit log reads.

Audit rows carry an optional `team_id` and `org_id`. Installation admins read everything
(including installation-wide rows with no team and rows of archived teams); a team admin
reads the rows of the teams they administer, never org-level events; an org admin also reads
its org's org-level events and its archived teams. A plain member reads none: the team's
audit is part of the team-admin view, and a deployment can tell a member apart from an admin
reliably.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import or_, select

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
from lumewarp_control_plane.authz import Action, Authz
from lumewarp_control_plane.errors import forbidden, not_found
from lumewarp_control_plane.models import AuditLog, Org, Team

FromQuery = Annotated[datetime | None, Query(alias="from")]
ToQuery = Annotated[datetime | None, Query(alias="to")]

router = APIRouter(tags=["audit"])


@router.get("/audit", response_model=Page[AuditOut], operation_id="listAudit")
async def list_audit(
    authz: Authz,
    session: SessionDep,
    team_id: uuid.UUID | None = None,
    org_id: uuid.UUID | None = None,
    actor_id: str | None = None,
    action: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    from_: FromQuery = None,
    to_: ToQuery = None,
    limit: LimitDep = 50,
    cursor: str | None = None,
) -> Page[AuditOut]:
    """Audit events, newest first. An installation admin reads everything. An org admin reads their
    orgs' events with `org_id`, org-level events included; a team admin reads their teams'
    events. A caller who administers nothing gets 403, and a `team_id` or `org_id` the caller
    cannot read answers 404.
    """
    start = aware(from_, "from")
    end = aware(to_, "to")

    stmt = select(AuditLog).order_by(AuditLog.id.desc())
    if team_id is not None:
        if await session.get(Team, team_id) is None:
            # A team row that no longer exists is installation-admin history only.
            if not await authz.is_admin():
                raise not_found("team not found")
        else:
            # Unknown and unreadable are indistinguishable (404) before the 403, as the
            # rest of the API does.
            await ensure_team_read(authz, team_id)
            await authz.require(Action.TEAM_ADMIN_READ, team_id)
        stmt = stmt.where(AuditLog.team_id == team_id)
    else:
        # Archived teams stay in the candidate set: their history remains attributable and
        # an org admin reads it. `org_id` adds the org-level events (org policy, org admins,
        # org budget), which a team admin never sees.
        admin_teams = await authz.admin_teams(set(await session.scalars(select(Team.id))))
        admin_orgs = await authz.admin_orgs(set(await session.scalars(select(Org.id))))
        if admin_teams is not None or admin_orgs is not None:
            filters = []
            if admin_teams:
                filters.append(AuditLog.team_id.in_(admin_teams))
            if admin_orgs:
                filters.append(AuditLog.org_id.in_(admin_orgs))
            if not filters:
                # A caller who administers nothing has no audit view at all, whether the
                # request is filtered or not.
                raise forbidden(f"{Action.TEAM_ADMIN_READ.value} not allowed")
            stmt = stmt.where(or_(*filters))
    if org_id is not None:
        # The org filter is the org admin's view: its own org's rows, including the
        # org-level events whose team_id is NULL. Unknown and unreadable are the same
        # 404, as for a team filter.
        if await session.get(Org, org_id) is None or not await authz.is_org_admin(org_id):
            raise not_found("org not found")
        stmt = stmt.where(AuditLog.org_id == org_id)
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
