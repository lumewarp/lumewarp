"""Usage aggregation: monthly `usage_totals` fast path plus recent `usage_events`.

`from`/`to` are a half-open UTC range. `group_by` is a comma-separated subset of
`day,model,team,owner,key`; the default is `day,model`. `owner` groups by the key owner
(`owner_kind`, `owner_id`: a user or a service account). A superuser's single `team`
dimension over whole UTC months is served from the pre-aggregated `usage_totals` table;
everything else is aggregated from `usage_events`, limited to the caller's readable teams.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query
from sqlalchemy import case, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane.api.common import (
    LimitDep,
    SessionDep,
    aware,
    ensure_key_read,
    ensure_range,
    ensure_team_read,
    month_start,
    validation_error,
)
from lumewarp_control_plane.api.schemas import Page, UsageRowOut
from lumewarp_control_plane.authz import Authz
from lumewarp_control_plane.errors import not_found
from lumewarp_control_plane.models import (
    ApiKey,
    KeyOwnerKind,
    ScopeType,
    Team,
    UsageEvent,
    UsageTotal,
)

router = APIRouter(tags=["usage"])

DIMENSIONS = ("day", "model", "team", "owner", "key")
# Output columns behind each dimension, in cursor order.
COLUMNS = {
    "day": ("day",),
    "model": ("model",),
    "team": ("team",),
    "owner": ("owner_kind", "owner_id"),
    "key": ("key",),
}
UUID_COLUMNS = {"team", "owner_id", "key"}

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


def _decode_cursor(cursor: str, columns: list[str]) -> list[Any]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode())
        values = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        raise validation_error("cursor", "invalid cursor") from exc
    if not isinstance(values, list) or len(values) != len(columns):
        raise validation_error("cursor", "invalid cursor")
    converted: list[Any] = []
    for name, value in zip(columns, values, strict=True):
        if name in UUID_COLUMNS:
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
        elif name == "owner_kind":
            if value not in {kind.value for kind in KeyOwnerKind}:
                raise validation_error("cursor", "invalid cursor")
            converted.append(value)
        else:
            converted.append(value)
    return converted


async def _check_scope_filters(
    session: AsyncSession,
    authz: Authz,
    team_id: uuid.UUID | None,
    user_id: uuid.UUID | None,
    service_account_id: uuid.UUID | None,
    key_id: uuid.UUID | None,
) -> uuid.UUID | None:
    """Resolve parent mismatches (404) before authorizing; returns the narrowest team."""
    if (user_id is not None or service_account_id is not None) and team_id is None:
        raise validation_error("team_id", "required with user_id or service_account_id")
    if user_id is not None and service_account_id is not None:
        raise validation_error("service_account_id", "cannot be combined with user_id")
    if key_id is not None:
        key = await session.get(ApiKey, key_id)
        if key is None:
            raise not_found("key not found")
        if team_id is not None and team_id != key.team_id:
            raise not_found("key_id does not match team_id")
        await ensure_key_read(authz, key)
        return key.team_id
    if team_id is not None:
        if await session.get(Team, team_id) is None:
            raise not_found("team not found")
        await ensure_team_read(authz, team_id)
    return team_id


async def _totals_page(
    session: AsyncSession,
    from_: datetime,
    to_: datetime,
    team_id: uuid.UUID | None,
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
            UsageTotal.scope_type == ScopeType.TEAM,
            UsageTotal.window_start >= from_,
            UsageTotal.window_start < to_,
        )
        .group_by(group_column)
        .order_by(group_column)
    )
    if team_id is not None:
        stmt = stmt.where(group_column == team_id)
    if cursor is not None:
        try:
            stmt = stmt.where(group_column > uuid.UUID(cursor))
        except ValueError as exc:
            raise validation_error("cursor", "invalid cursor") from exc
    rows = (await session.execute(stmt.limit(limit + 1))).all()
    next_cursor = str(rows[limit - 1][0]) if len(rows) > limit else None
    items = [
        UsageRowOut(
            team_id=row[0],
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
    readable: set[uuid.UUID] | None,
    from_: datetime,
    to_: datetime,
    filters: list[Any],
    limit: int,
    cursor: str | None,
) -> Page[UsageRowOut]:
    fields = {
        "day": func.date_trunc("day", func.timezone("UTC", UsageEvent.ts)),
        "model": UsageEvent.model,
        "team": UsageEvent.team_id,
        "owner_kind": UsageEvent.owner_kind,
        "owner_id": UsageEvent.owner_id,
        "key": UsageEvent.key_id,
    }
    names = [column for group in groups for column in COLUMNS[group]]
    group_columns = [fields[name] for name in names]
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
    for condition in filters:
        stmt = stmt.where(condition)
    if readable is not None:
        if not readable:
            return Page[UsageRowOut](items=[], next_cursor=None)
        stmt = stmt.where(UsageEvent.team_id.in_(readable))
    if names:
        stmt = stmt.group_by(*group_columns).order_by(*group_columns)
        if cursor is not None:
            values = _decode_cursor(cursor, names)
            stmt = stmt.where(tuple_(*group_columns) > tuple_(*values))
    rows = (await session.execute(stmt.limit(limit + 1))).all()
    if names and len(rows) > limit:
        last = rows[limit - 1]
        next_cursor = _encode_cursor(
            [
                last[index].isoformat()
                if name == "day"
                else getattr(last[index], "value", None) or str(last[index])
                for index, name in enumerate(names)
            ]
        )
    else:
        next_cursor = None
    n = len(names)
    items: list[UsageRowOut] = []
    for row in rows[:limit]:
        values = {name: row[index] for index, name in enumerate(names)}
        items.append(
            UsageRowOut(
                date=values["day"].date() if "day" in values else None,
                model=values.get("model"),
                team_id=values.get("team"),
                owner_kind=values.get("owner_kind"),
                owner_id=values.get("owner_id"),
                key_id=values.get("key"),
                requests=int(row[n]),
                prompt_tokens=int(row[n + 1]),
                completion_tokens=int(row[n + 2]),
                total_tokens=int(row[n + 1]) + int(row[n + 2]),
                cost_nano_usd=int(row[n + 3]),
                unknown_cost_count=int(row[n + 4]),
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
    authz: Authz,
    session: SessionDep,
    from_: FromQuery = None,
    to_: ToQuery = None,
    group_by: str | None = None,
    team_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    service_account_id: uuid.UUID | None = None,
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
    scoped_team = await _check_scope_filters(
        session, authz, team_id, user_id, service_account_id, key_id
    )
    superuser = await authz.is_superuser()
    # A narrowing filter already proved TEAM_READ; otherwise limit to the readable teams.
    readable = (
        None
        if superuser or scoped_team is not None
        else await authz.readable_teams(set(await session.scalars(select(Team.id))))
    )
    use_totals = (
        superuser
        and groups == ["team"]
        and model is None
        and user_id is None
        and service_account_id is None
        and key_id is None
        and _month_aligned(start, end)
    )
    if use_totals:
        return await _totals_page(session, start, end, team_id, limit, cursor)
    ensure_range(start, end)
    filters: list[Any] = []
    if model is not None:
        filters.append(UsageEvent.model == model)
    if scoped_team is not None:
        filters.append(UsageEvent.team_id == scoped_team)
    if user_id is not None:
        filters.append(UsageEvent.owner_kind == KeyOwnerKind.USER)
        filters.append(UsageEvent.owner_id == user_id)
    if service_account_id is not None:
        filters.append(UsageEvent.owner_kind == KeyOwnerKind.SERVICE_ACCOUNT)
        filters.append(UsageEvent.owner_id == service_account_id)
    if key_id is not None:
        filters.append(UsageEvent.key_id == key_id)
    return await _events_page(session, groups, readable, start, end, filters, limit, cursor)
