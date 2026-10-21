"""Usage aggregation: monthly `usage_totals` fast path plus recent `usage_events`.

`from`/`to` are a half-open UTC range. `group_by` is a comma-separated subset of
`day,model,org,team,project,key`; the default is `day,model`. A single scope dimension over
whole UTC months is served from the pre-aggregated `usage_totals` table (superuser only);
everything else is aggregated from `usage_events`.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query
from sqlalchemy import case, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane import rbac
from lumewarp_control_plane.api.common import (
    LimitDep,
    SessionDep,
    aware,
    ensure_key_read,
    ensure_project_read,
    ensure_range,
    ensure_team_read,
    month_start,
    validation_error,
)
from lumewarp_control_plane.api.schemas import Page, UsageRowOut
from lumewarp_control_plane.errors import forbidden, not_found
from lumewarp_control_plane.models import (
    ApiKey,
    Org,
    Project,
    ScopeType,
    Team,
    UsageEvent,
    UsageTotal,
)
from lumewarp_control_plane.security import CurrentPrincipal

router = APIRouter(tags=["usage"])

DIMENSIONS = ("day", "model", "org", "team", "project", "key")
SCOPE_TYPES = {
    "org": ScopeType.ORG,
    "team": ScopeType.TEAM,
    "project": ScopeType.PROJECT,
    "key": ScopeType.KEY,
}

FromQuery = Annotated[datetime | None, Query(alias="from")]
ToQuery = Annotated[datetime | None, Query(alias="to")]


def _parse_group_by(value: str | None) -> list[str]:
    if value is None:
        return ["day", "model"]
    groups: list[str] = []
    for raw in value.split(","):
        name = raw.strip()
        if not name:
            continue
        if name not in DIMENSIONS:
            raise validation_error("group_by", f"unknown dimension {name!r}")
        if name not in groups:
            groups.append(name)
    return groups


def _encode_cursor(values: list[Any]) -> str:
    encoded = json.dumps(values, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(encoded).decode()


def _decode_cursor(cursor: str, groups: list[str]) -> list[Any]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode())
        values = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        raise validation_error("cursor", "invalid cursor") from exc
    if not isinstance(values, list) or len(values) != len(groups):
        raise validation_error("cursor", "invalid cursor")
    converted: list[Any] = []
    for name, value in zip(groups, values, strict=True):
        if name in ("org", "team", "project", "key"):
            try:
                converted.append(uuid.UUID(value))
            except (ValueError, TypeError) as exc:
                raise validation_error("cursor", "invalid cursor") from exc
        elif name == "day":
            try:
                parsed = datetime.fromisoformat(value)
            except (ValueError, TypeError) as exc:
                raise validation_error("cursor", "invalid cursor") from exc
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(UTC).replace(tzinfo=None)
            converted.append(parsed)
        else:
            converted.append(value)
    return converted


def _scope_admin_condition(principal: CurrentPrincipal) -> Any:
    org_admins: set[uuid.UUID] = set()
    teams: set[uuid.UUID] = set()
    for grant in principal.grants:
        if grant.team_id is None and grant.role in {"owner", "admin"}:
            org_admins.add(grant.org_id)
        elif grant.team_id is not None:
            teams.add(grant.team_id)
    clauses = []
    if org_admins:
        clauses.append(UsageEvent.org_id.in_(org_admins))
    if teams:
        clauses.append(UsageEvent.team_id.in_(teams))
    return or_(*clauses) if clauses else None


async def _check_scope_filters(
    session: AsyncSession,
    principal: CurrentPrincipal,
    org_id: uuid.UUID | None,
    team_id: uuid.UUID | None,
    project_id: uuid.UUID | None,
    key_id: uuid.UUID | None,
) -> None:
    """Resolve parent mismatches (404) before authorizing the narrowest scope."""
    if key_id is not None:
        key = await session.get(ApiKey, key_id)
        if key is None:
            raise not_found("key not found")
        if org_id is not None and org_id != key.org_id:
            raise not_found("key_id does not match org_id")
        if team_id is not None and team_id != key.team_id:
            raise not_found("key_id does not match team_id")
        if project_id is not None and project_id != key.project_id:
            raise not_found("key_id does not match project_id")
        ensure_key_read(principal, key)
        return
    if project_id is not None:
        project = await session.get(Project, project_id)
        if project is None:
            raise not_found("project not found")
        if org_id is not None and org_id != project.org_id:
            raise not_found("project_id does not match org_id")
        if team_id is not None and team_id != project.team_id:
            raise not_found("project_id does not match team_id")
        ensure_project_read(principal, project)
        return
    if team_id is not None:
        team = await session.get(Team, team_id)
        if team is None:
            raise not_found("team not found")
        if org_id is not None and org_id != team.org_id:
            raise not_found("team_id does not match org_id")
        ensure_team_read(principal, team)
        return
    if org_id is not None:
        org = await session.get(Org, org_id)
        if org is None or not rbac.authorize(
            principal, rbac.Action.ORG_READ, rbac.ResourceScope(org.id)
        ):
            raise not_found("org not found")
        if not rbac.authorize(principal, rbac.Action.READ, rbac.ResourceScope(org.id)):
            raise forbidden("read not allowed for the whole org; filter by team_id")


async def _totals_page(
    session: AsyncSession,
    dimension: str,
    from_: datetime,
    to_: datetime,
    filters: dict[str, uuid.UUID],
    limit: int,
    cursor: str | None,
) -> Page[UsageRowOut]:
    group_column = UsageTotal.scope_id
    stmt = (
        select(
            group_column,
            func.coalesce(func.sum(UsageTotal.request_count), 0),
            func.coalesce(func.sum(UsageTotal.prompt_tokens), 0),
            func.coalesce(func.sum(UsageTotal.completion_tokens), 0),
            func.coalesce(func.sum(UsageTotal.cost_nano_usd), 0),
            func.coalesce(func.sum(UsageTotal.unknown_cost_count), 0),
        )
        .where(
            UsageTotal.scope_type == SCOPE_TYPES[dimension],
            UsageTotal.window_start >= from_,
            UsageTotal.window_start < to_,
        )
        .group_by(group_column)
        .order_by(group_column)
    )
    if dimension in filters:
        stmt = stmt.where(group_column == filters[dimension])
    if cursor is not None:
        try:
            stmt = stmt.where(group_column > uuid.UUID(cursor))
        except ValueError as exc:
            raise validation_error("cursor", "invalid cursor") from exc
    rows = (await session.execute(stmt.limit(limit + 1))).all()
    next_cursor = str(rows[limit - 1][0]) if len(rows) > limit else None
    items = [
        UsageRowOut(
            **{dimension + "_id": row[0]},
            requests=int(row[1]),
            prompt_tokens=int(row[2]),
            completion_tokens=int(row[3]),
            total_tokens=int(row[2]) + int(row[3]),
            cost_nano_usd=int(row[4]),
            unknown_cost_count=int(row[5]),
        )
        for row in rows[:limit]
    ]
    return Page[UsageRowOut](items=items, next_cursor=next_cursor)


async def _events_page(
    session: AsyncSession,
    groups: list[str],
    principal: CurrentPrincipal,
    from_: datetime,
    to_: datetime,
    filters: dict[str, Any],
    model: str | None,
    limit: int,
    cursor: str | None,
) -> Page[UsageRowOut]:
    fields = {
        "day": func.date_trunc("day", func.timezone("UTC", UsageEvent.ts)),
        "model": UsageEvent.model,
        "org": UsageEvent.org_id,
        "team": UsageEvent.team_id,
        "project": UsageEvent.project_id,
        "key": UsageEvent.key_id,
    }
    group_columns = [fields[name] for name in groups]
    aggregates = [
        func.count().label("requests"),
        func.coalesce(func.sum(UsageEvent.prompt_tokens), 0).label("prompt_tokens"),
        func.coalesce(func.sum(UsageEvent.completion_tokens), 0).label("completion_tokens"),
        func.coalesce(func.sum(UsageEvent.cost_nano_usd), 0).label("cost_nano_usd"),
        func.coalesce(func.sum(case((UsageEvent.cost_known.is_(False), 1), else_=0)), 0).label(
            "unknown_cost_count"
        ),
    ]
    stmt = select(*group_columns, *aggregates).where(UsageEvent.ts >= from_, UsageEvent.ts < to_)
    if model is not None:
        stmt = stmt.where(UsageEvent.model == model)
    column_filters = {
        "org": UsageEvent.org_id,
        "team": UsageEvent.team_id,
        "project": UsageEvent.project_id,
        "key": UsageEvent.key_id,
    }
    for name, value in filters.items():
        if name in column_filters:
            stmt = stmt.where(column_filters[name] == value)
    if not principal.is_superuser:
        allowed = _scope_admin_condition(principal)
        if allowed is None:
            return Page[UsageRowOut](items=[], next_cursor=None)
        stmt = stmt.where(allowed)
    if groups:
        stmt = stmt.group_by(*group_columns).order_by(*group_columns)
        if cursor is not None:
            values = _decode_cursor(cursor, groups)
            stmt = stmt.where(tuple_(*group_columns) > tuple_(*values))
    rows = (await session.execute(stmt.limit(limit + 1))).all()
    if groups and len(rows) > limit:
        last = rows[limit - 1]
        next_cursor = _encode_cursor(
            [
                last[index].isoformat() if groups[index] == "day" else str(last[index])
                for index in range(len(groups))
            ]
        )
    else:
        next_cursor = None
    items: list[UsageRowOut] = []
    for row in rows[:limit]:
        values = {name: row[index] for index, name in enumerate(groups)}
        items.append(
            UsageRowOut(
                date=values["day"].date() if "day" in values else None,
                model=values.get("model"),
                org_id=values.get("org"),
                team_id=values.get("team"),
                project_id=values.get("project"),
                key_id=values.get("key"),
                requests=int(row[len(groups)]),
                prompt_tokens=int(row[len(groups) + 1]),
                completion_tokens=int(row[len(groups) + 2]),
                total_tokens=int(row[len(groups) + 1]) + int(row[len(groups) + 2]),
                cost_nano_usd=int(row[len(groups) + 3]),
                unknown_cost_count=int(row[len(groups) + 4]),
            )
        )
    return Page[UsageRowOut](items=items, next_cursor=next_cursor)


def _month_aligned(from_: datetime, to_: datetime) -> bool:
    return (
        from_.tzinfo is not None
        and to_.tzinfo is not None
        and from_ == month_start(from_)
        and from_ < to_
        and to_ == month_start(to_)
    )


@router.get("/usage", response_model=Page[UsageRowOut], operation_id="getUsage")
async def get_usage(
    principal: CurrentPrincipal,
    session: SessionDep,
    from_: FromQuery = None,
    to_: ToQuery = None,
    group_by: str | None = None,
    org_id: uuid.UUID | None = None,
    team_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    key_id: uuid.UUID | None = None,
    model: str | None = None,
    limit: LimitDep = 50,
    cursor: str | None = None,
) -> Page[UsageRowOut]:
    now = datetime.now(UTC)
    start = (aware(from_, "from") or month_start(now)).astimezone(UTC)
    end = (aware(to_, "to") or now).astimezone(UTC)
    if end <= start:
        raise validation_error("to", "must be after from")
    groups = _parse_group_by(group_by)
    await _check_scope_filters(session, principal, org_id, team_id, project_id, key_id)
    filters = {
        name: value
        for name, value in (
            ("org", org_id),
            ("team", team_id),
            ("project", project_id),
            ("key", key_id),
        )
        if value is not None
    }
    use_totals = (
        principal.is_superuser
        and len(groups) == 1
        and groups[0] in SCOPE_TYPES
        and model is None
        and set(filters) <= set(groups)
        and _month_aligned(start, end)
    )
    if use_totals:
        return await _totals_page(session, groups[0], start, end, filters, limit, cursor)
    ensure_range(start, end)
    return await _events_page(session, groups, principal, start, end, filters, model, limit, cursor)
