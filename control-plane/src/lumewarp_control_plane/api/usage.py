"""Usage aggregation: monthly `usage_totals` fast path plus recent `usage_events`.

`from`/`to` are a half-open UTC range. `group_by` is a comma-separated subset of
`day,kind,target,method,tool,requested,model,upstream_model,alias,router,team,owner,key`;
the default is `day,requested`. `owner` groups by the key owner
(`owner_kind`, `owner_id`: a user or a service account). `kind` is the gateway protocol
(`llm`, `mcp`, `a2a`); `target`, `method` and `tool` are an MCP server or A2A agent, its
JSON-RPC method and the MCP tool or prompt. An installation admin's single `team` dimension
over whole UTC months is served from the pre-aggregated `usage_totals` table, which holds LLM
requests only: every other shape is aggregated from `usage_events`, limited to the caller's
readable teams.

Every row carries `error_count` (requests whose outcome is `upstream_error` or `rejected`, or
whose status is 4xx/5xx without being a client cancellation) and `total_duration_ms` (summed, so
the mean is the sum over the requests) beside the LLM amounts: the MCP and A2A tabs read them
instead of tokens and cost. The `usage_totals` fast path is LLM-only and reports both as 0.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query
from sqlalchemy import String, and_, case, cast, distinct, func, or_, select, tuple_
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
    RequestOutcome,
    ScopeType,
    Team,
    TrafficKind,
    UsageEvent,
    UsageTotal,
)

router = APIRouter(tags=["usage"])

DIMENSIONS = (
    "day",
    "kind",
    "target",
    "method",
    "tool",
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
    "kind": ("kind",),
    "target": ("target",),
    "method": ("method",),
    "tool": ("tool",),
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
# Dimensions that are plain columns of the event: no catalog name to resolve.
PLAIN_DIMENSIONS = frozenset({"kind", "target", "method", "tool"})

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
            # The totals table holds LLM requests only (usage.ingest), so a row from it is one.
            kind=TrafficKind.LLM,
            team_id=row[0],
            requests=int(row[1]),
            prompt_tokens=int(row[2]),
            completion_tokens=int(row[3]),
            total_tokens=int(row[2]) + int(row[3]),
            cost_nano_usd=int(row[4]),
            unknown_cost_count=int(row[5]),
            # Not aggregated into the totals: nothing reads it from this shape.
            error_count=0,
            total_duration_ms=0,
        )
        for row in rows[:limit]
    ]
    return Page[UsageRowOut](items=items, next_cursor=next_cursor)


_AMOUNTS = (
    "requests",
    "prompt_tokens",
    "completion_tokens",
    "cost_nano_usd",
    "unknown_cost_count",
    "error_count",
    "total_duration_ms",
)


def _events_query(
    groups: list[str],
    readable: set[uuid.UUID] | None,
    from_: datetime,
    to_: datetime,
    filters: list[Any],
) -> tuple[Any, dict[str, Any], list[str], bool]:
    """Aggregate raw rows first; resolve names/collect UI refs only after reduction.

    The default day/requested (and scope-only) load needs no catalog joins or
    distinct ref aggregates. Model groups need all provenance; alias/router groups
    need their own refs plus Served by. Upstream-model groups need Served by.
    """
    names = [column for group in groups for column in COLUMNS[group]]
    kinds = [kind.value for kind in CatalogKind if kind.value in groups]
    if any(name in groups for name in ("model", "alias", "router", "upstream_model")):
        kinds = (
            ["model", "alias", "router"]
            if "model" in groups
            else list(dict.fromkeys(["model", *kinds]))
        )
    upstream = "model" in groups or "upstream_model" in groups
    fields = {
        "day": func.date_trunc("day", func.timezone("UTC", UsageEvent.ts)),
        "kind": UsageEvent.kind,
        "target": UsageEvent.target,
        "method": UsageEvent.method,
        "tool": UsageEvent.tool,
        "requested": UsageEvent.requested,
        "team": UsageEvent.team_id,
        "owner_kind": UsageEvent.owner_kind,
        "owner_id": UsageEvent.owner_id,
        "key": UsageEvent.key_id,
        "upstream_model": UsageEvent.upstream_model,
    }
    raw_names = [name for name in names if name not in {kind.value for kind in CatalogKind}]
    for kind in kinds:
        fields[kind] = getattr(UsageEvent, kind)
        fields[f"{kind}_id"] = getattr(UsageEvent, f"{kind}_id")
        raw_names.extend([f"{kind}_id", kind])
    if upstream and "upstream_model" not in raw_names:
        raw_names.append("upstream_model")
    columns = [fields[name].label(name) for name in raw_names]
    # An error is an error outcome (the gateway reports one for a JSON-RPC error in a 200 too),
    # or a 4xx/5xx status that is not a client cancellation (499).
    failed = case(
        (
            or_(
                UsageEvent.outcome.in_([RequestOutcome.UPSTREAM_ERROR, RequestOutcome.REJECTED]),
                and_(
                    UsageEvent.status >= 400,
                    UsageEvent.outcome != RequestOutcome.CLIENT_CANCELLED,
                ),
            ),
            1,
        ),
        else_=0,
    )
    amounts = [
        func.count().label("requests"),
        func.coalesce(func.sum(UsageEvent.prompt_tokens), 0).label("prompt_tokens"),
        func.coalesce(func.sum(UsageEvent.completion_tokens), 0).label("completion_tokens"),
        func.coalesce(func.sum(UsageEvent.cost_nano_usd), 0).label("cost_nano_usd"),
        func.coalesce(func.sum(case((UsageEvent.cost_known.is_(False), 1), else_=0)), 0).label(
            "unknown_cost_count"
        ),
        func.coalesce(func.sum(failed), 0).label("error_count"),
        func.coalesce(func.sum(UsageEvent.duration_ms), 0).label("total_duration_ms"),
    ]
    raw = select(*columns, *amounts).where(UsageEvent.ts >= from_, UsageEvent.ts < to_, *filters)
    if readable is not None:
        raw = raw.where(UsageEvent.team_id.in_(readable))
    if columns:
        raw = raw.group_by(*columns)
    if not kinds:
        return raw, {name: fields[name] for name in names}, kinds, upstream

    reduced = raw.subquery("usage_reduced")
    fields = {name: reduced.c[name] for name in names if name not in kinds}
    joins = []
    refs = []
    for kind in kinds:
        catalog = aliased(CatalogName)
        joins.append((catalog, (catalog.id == reduced.c[f"{kind}_id"]) & (catalog.kind == kind)))
        fields[kind] = case(
            (catalog.id.is_not(None), "id:" + cast(catalog.id, String)),
            else_="name:" + reduced.c[kind],
        )
        ref = func.jsonb_build_object(
            "id", catalog.id, "name", func.coalesce(catalog.name, reduced.c[kind])
        )
        refs.append(cast(func.jsonb_agg(distinct(ref)), JSONB).label(f"{kind}_refs"))
    if upstream:
        refs.append(func.array_agg(distinct(reduced.c.upstream_model)).label("upstream_values"))
    # Keep caller's dimension order; ids unify historic spellings after rename.
    group_columns = [fields[name].label(name) for name in names]
    stmt = select(
        *group_columns,
        *(func.coalesce(func.sum(reduced.c[name]), 0).label(name) for name in _AMOUNTS),
        *refs,
    ).select_from(reduced)
    for catalog, condition in joins:
        stmt = stmt.outerjoin(catalog, condition)
    if group_columns:
        stmt = stmt.group_by(*group_columns)
    return stmt, {name: fields[name] for name in names}, kinds, upstream


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
    if readable is not None and not readable:
        return Page[UsageRowOut](items=[], next_cursor=None)
    stmt, fields, kinds, upstream = _events_query(groups, readable, from_, to_, filters)
    names = list(fields)
    group_columns = list(fields.values())
    if names:
        stmt = stmt.order_by(*group_columns)
        if cursor is not None:
            stmt = stmt.having(tuple_(*group_columns) > tuple_(*_decode_cursor(cursor, names)))
    rows = (await session.execute(stmt.limit(limit + 1))).mappings().all()
    next_cursor = None
    if names and len(rows) > limit:
        last = rows[limit - 1]
        next_cursor = _encode_cursor(
            [
                last[name].isoformat()
                if name == "day"
                else getattr(last[name], "value", None) or str(last[name])
                for name in names
            ]
        )
    items = []
    for row in rows[:limit]:
        provenance = {}
        for name in kinds:
            entries = sorted(
                row[f"{name}_refs"] or [], key=lambda item: (item["name"], item["id"] or "")
            )
            if len(entries) == 1:
                provenance[name] = entries[0]["name"]
                provenance[f"{name}_id"] = entries[0]["id"]
            provenance[
                {"model": "served_by", "alias": "via_aliases", "router": "via_routers"}[name]
            ] = [entry for entry in entries if entry["name"]]
        if upstream:
            values = row["upstream_values"] or []
            provenance["upstream_model"] = values[0] if len(values) == 1 else None
            provenance["upstream_models"] = sorted(value for value in values if value)
        # `kind`, `target`, `method` and `tool` are plain dimensions: no catalog refs.
        plain = {name: row[name] for name in names if name in PLAIN_DIMENSIONS}
        items.append(
            UsageRowOut(
                **provenance,
                **plain,
                date=row["day"].date() if "day" in row else None,
                requested=row.get("requested"),
                team_id=row.get("team"),
                owner_kind=row.get("owner_kind"),
                owner_id=row.get("owner_id"),
                key_id=row.get("key"),
                **{name: int(row[name]) for name in _AMOUNTS},
                total_tokens=int(row["prompt_tokens"]) + int(row["completion_tokens"]),
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
    kind: TrafficKind | None = None,
    target: str | None = None,
    method: str | None = None,
    tool: str | None = None,
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
    """Aggregate usage within the caller's existing team/key permission scope.

    Catalog identity uses request started_at (completion ts for old gateways).
    If no exact interval matches, a unique same-kind owner within
    LUMEWARP_USAGE_CATALOG_GRACE_SECONDS (default 300) is used. Same-kind name
    reuse within the grace window while a gateway lags can still misattribute
    to the new exact owner;
    config_version is not mapped to catalog history.

    Name filters select the current live id. Orphan rows sharing a live name
    remain visible in unfiltered groups but cannot be selected separately by
    that name/id; no orphan-only filter is provided. Catalog provenance refs
    are collected for catalog/upstream-model groupings, not scope-only/default
    day,requested loads. Empty upstream_model means unavailable; null means mixed.
    `kind`, `target`, `method` and `tool` filter the gateway protocol and what an
    MCP or A2A request named; the team totals fast path covers LLM requests only,
    so any of them (and every non-LLM kind) reads raw events.
    """
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
    if is_admin:
        # An installation admin reads every team; neither listing is needed.
        readable = None
        admin_teams = None
    else:
        candidates = set(await session.scalars(select(Team.id)))
        # A narrowing filter already proved TEAM_READ; otherwise limit to the readable teams.
        readable = None if scoped_team is not None else await authz.readable_teams(candidates)
        # Team admins keep whole-team views; a plain member only sees rows of their own
        # keys. This predicate is applied server-side and cannot be widened by
        # key_id/user_id/service_account_id/team_id filters or by grouping.
        admin_teams = await authz.admin_teams(candidates)
    use_totals = (
        is_admin
        and groups == ["team"]
        and requested is None
        # The totals table counts LLM requests only, so both of these ask the same question.
        and kind in (None, TrafficKind.LLM)
        and all(
            value is None
            for value in (
                target,
                method,
                tool,
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
    for catalog_kind, name, name_id in (
        (CatalogKind.MODEL, model, model_id),
        (CatalogKind.ALIAS, alias, alias_id),
        (CatalogKind.ROUTER, router, router_id),
    ):
        column = getattr(UsageEvent, catalog_kind.value)
        id_column = getattr(UsageEvent, f"{catalog_kind.value}_id")
        if name_id is not None:
            filters.append(id_column == name_id)
        if name is not None:
            current_id = await session.scalar(
                select(CatalogName.id).where(
                    CatalogName.kind == catalog_kind, CatalogName.name == name
                )
            )
            if current_id is not None:
                filters.append(id_column == current_id)
            else:
                # Do not mix a renamed, still-live item into a deleted item's
                # historical spelling. Clients select the live item by current name/id.
                live_ids = select(CatalogName.id).where(CatalogName.kind == catalog_kind)
                filters.append(
                    (column == name) & or_(id_column.is_(None), id_column.not_in(live_ids))
                )
    for column, value in (
        (UsageEvent.kind, kind),
        (UsageEvent.target, target),
        (UsageEvent.method, method),
        (UsageEvent.tool, tool),
    ):
        if value is not None:
            filters.append(column == value)
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
    if admin_teams is not None:
        # Per team: administered teams keep the whole view, every other team is narrowed
        # to the caller's own user-owned keys.
        filters.append(
            or_(
                UsageEvent.team_id.in_(admin_teams),
                and_(
                    UsageEvent.owner_kind == KeyOwnerKind.USER,
                    UsageEvent.owner_id == authz.user_id,
                ),
            )
        )
    return await _events_page(session, groups, readable, start, end, filters, limit, cursor)
