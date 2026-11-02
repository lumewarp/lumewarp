"""Usage aggregation: monthly `usage_totals` fast path plus recent `usage_events`.

`from`/`to` are a half-open UTC range. `group_by` is a comma-separated subset of
`day,requested,model,upstream_model,alias,router,team,owner,key`; the default is
`day,requested`. `owner` groups by the key owner
(`owner_kind`, `owner_id`: a user or a service account). A admin group member's single `team`
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
from sqlalchemy import String, case, cast, distinct, func, or_, select, tuple_
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

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
    CatalogKind,
    CatalogName,
    KeyOwnerKind,
    ScopeType,
    Team,
    UsageEvent,
    UsageTotal,
)

router = APIRouter(tags=["usage"])

DIMENSIONS = (
    "day",
    "requested",
    "model",
    "upstream_model",
    "alias",
    "router",
    "team",
    "owner",
    "key",
)
# Output columns behind each dimension, in cursor order.
COLUMNS = {
    "day": ("day",),
    "requested": ("requested",),
    "model": ("model",),
    "upstream_model": ("upstream_model",),
    "alias": ("alias",),
    "router": ("router",),
    "team": ("team",),
    "owner": ("owner_kind", "owner_id"),
    "key": ("key",),
}
UUID_COLUMNS = {"team", "owner_id", "key"}

FromQuery = Annotated[datetime | None, Query(alias="from")]
ToQuery = Annotated[datetime | None, Query(alias="to")]


def _parse_group_by(value: str | None) -> list[str]:
    if value is None:
        return ["day", "requested"]
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
        "requested": UsageEvent.requested,
        "team": UsageEvent.team_id,
        "owner_kind": UsageEvent.owner_kind,
        "owner_id": UsageEvent.owner_id,
        "key": UsageEvent.key_id,
    }
    catalogs = {kind.value: aliased(CatalogName) for kind in CatalogKind}
    refs = {}
    for name, catalog in catalogs.items():
        # Non-null, collision-free identity keys keep pagination stable even for
        # missing provenance. A live id and an orphan's spelling never coalesce.
        fields[name] = case(
            (catalog.id.is_not(None), "id:" + cast(catalog.id, String)),
            else_="name:" + getattr(UsageEvent, name),
        )
        refs[name] = func.jsonb_build_object(
            "id", catalog.id, "name", func.coalesce(catalog.name, getattr(UsageEvent, name))
        )
    fields["upstream_model"] = UsageEvent.upstream_model
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
    aggregates.extend(cast(func.jsonb_agg(distinct(refs[name])), JSONB) for name in catalogs)
    aggregates.append(func.array_agg(distinct(UsageEvent.upstream_model)))
    stmt = select(*group_columns, *aggregates).where(UsageEvent.ts >= from_, UsageEvent.ts < to_)
    for name, catalog in catalogs.items():
        stmt = stmt.outerjoin(
            catalog, (catalog.id == getattr(UsageEvent, f"{name}_id")) & (catalog.kind == name)
        )
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
        provenance: dict[str, Any] = {}
        for index, name in enumerate(catalogs):
            entries = sorted(
                row[n + 5 + index] or [], key=lambda item: (item["name"], item["id"] or "")
            )
            if len(entries) == 1:
                provenance[name] = entries[0]["name"]
                provenance[f"{name}_id"] = entries[0]["id"]
            provenance[
                {"model": "served_by", "alias": "via_aliases", "router": "via_routers"}[name]
            ] = [entry for entry in entries if entry["name"]]
        upstream_models = sorted(value for value in (row[n + 8] or []) if value)
        provenance["upstream_models"] = upstream_models
        if len(upstream_models) == 1:
            provenance["upstream_model"] = upstream_models[0]
        items.append(
            UsageRowOut(
                **provenance,
                date=values["day"].date() if "day" in values else None,
                requested=values.get("requested"),
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
    requested: str | None = None,
    model: str | None = None,
    upstream_model: str | None = None,
    alias: str | None = None,
    router: str | None = None,
    model_id: uuid.UUID | None = None,
    alias_id: uuid.UUID | None = None,
    router_id: uuid.UUID | None = None,
    alias_missing: Annotated[
        bool | None,
        Query(
            description=(
                "true: no alias; false: via any alias. "
                "alias=none selects the literal catalog name 'none'."
            )
        ),
    ] = None,
    router_missing: Annotated[
        bool | None,
        Query(
            description=(
                "true: no router; false: via any router. "
                "Independent of catalog names, including 'none'."
            )
        ),
    ] = None,
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
    is_admin = await authz.is_admin()
    # A narrowing filter already proved TEAM_READ; otherwise limit to the readable teams.
    readable = (
        None
        if is_admin or scoped_team is not None
        else await authz.readable_teams(set(await session.scalars(select(Team.id))))
    )
    use_totals = (
        is_admin
        and groups == ["team"]
        and requested is None
        and all(
            value is None
            for value in (
                model,
                upstream_model,
                alias,
                router,
                model_id,
                alias_id,
                router_id,
                alias_missing,
                router_missing,
            )
        )
        and user_id is None
        and service_account_id is None
        and key_id is None
        and _month_aligned(start, end)
    )
    if use_totals:
        return await _totals_page(session, start, end, team_id, limit, cursor)
    ensure_range(start, end)
    filters: list[Any] = []
    for kind, name, name_id in (
        (CatalogKind.MODEL, model, model_id),
        (CatalogKind.ALIAS, alias, alias_id),
        (CatalogKind.ROUTER, router, router_id),
    ):
        column = getattr(UsageEvent, kind.value)
        id_column = getattr(UsageEvent, f"{kind.value}_id")
        if name_id is not None:
            filters.append(id_column == name_id)
        if name is not None:
            current_id = await session.scalar(
                select(CatalogName.id).where(CatalogName.kind == kind, CatalogName.name == name)
            )
            if current_id is not None:
                filters.append(id_column == current_id)
            else:
                # Do not mix a renamed, still-live item into a deleted item's
                # historical spelling. Clients select the live item by current name/id.
                live_ids = select(CatalogName.id).where(CatalogName.kind == kind)
                filters.append(
                    (column == name) & or_(id_column.is_(None), id_column.not_in(live_ids))
                )
    if upstream_model is not None:
        filters.append(UsageEvent.upstream_model == upstream_model)
    for name, missing in (("alias", alias_missing), ("router", router_missing)):
        if missing is not None:
            column = getattr(UsageEvent, name)
            filters.append(column == "" if missing else column != "")
    if requested is not None:
        filters.append(UsageEvent.requested == requested)
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
