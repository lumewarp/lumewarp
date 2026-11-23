"""Usage aggregation: monthly `usage_totals` fast path plus recent `usage_events`.

`from`/`to` are a half-open UTC range. `tz` is an IANA zone used to bucket `day` and
`hour`; the default is `UTC`. `group_by` is a comma-separated subset of
`zone,hour,day,kind,target,method,tool,requested,called,model,upstream_model,alias,router,status_class,outcome,fallback,team,owner,key`;
the default is `day,requested`. `hour` buckets by the local clock hour and returns the
bucket start as an aware instant; asking for it limits the range to 31 days. `owner`
groups by the key owner
(`owner_kind`, `owner_id`: a user or a service account). `kind` is the gateway protocol
(`llm`, `mcp`, `a2a`); `target`, `method` and `tool` are an MCP server or A2A agent, its
JSON-RPC method and the MCP tool or prompt. An installation admin's single `team` dimension
over whole UTC months is served from the pre-aggregated `usage_totals` table, which holds LLM
requests only: every other shape is aggregated from `usage_events`, limited to the caller's
readable teams.

`called` is the catalog identity of what the client called (`alias_id`, else `router_id`,
else `model_id`, else the literal `requested`), labelled with its current name; it merges a
rename that `requested` splits, and it is visible to members. `status_class` buckets the
HTTP status (`2xx`/`4xx`/`5xx`/`other`) and `outcome` is the gateway outcome. Every row also
carries fallback/retry counts and p50/p95/p99 request durations; the percentiles are null on
the monthly totals fast path, which stores no durations. `compare=previous` adds the
equal-length range immediately before `from` as ungrouped `previous` totals.

Only an installation admin sees routing internals: the `model`, `upstream_model`, `alias`,
`router` and `fallback` dimensions and filters, plus their
`served_by`/`via_*`/`upstream_model(s)` provenance. Every other caller sees the called name
(`requested` or `called`); asking for one of those dimensions or filters answers 403
`routing_detail_forbidden`.

A plain member sees the totals of every readable team (the whole team) when the request
neither groups nor filters by `owner` or `key`. Grouping by `owner`/`key`, or filtering by
`user_id`/`service_account_id`/`key_id`, keeps the narrower predicate: in a team the caller
does not administer, only their own user-owned keys are visible. This is what lets a member
read team totals without splitting out another member's keys or identity.

Every row carries `error_count` (requests whose outcome is `upstream_error` or `rejected`, or
whose status is 4xx/5xx without being a client cancellation) and `total_duration_ms` (summed, so
the mean is the sum over the requests) beside the LLM amounts: the MCP and A2A tabs read them
instead of tokens and cost. The `usage_totals` fast path is LLM-only and reports both as 0.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Query
from sqlalchemy import Integer, String, and_, case, cast, distinct, func, or_, select, tuple_
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
from lumewarp_control_plane.api.key_refs import key_refs
from lumewarp_control_plane.api.schemas import (
    Page,
    UsageLeaderboardRowOut,
    UsagePage,
    UsageRowOut,
    UsageTotalsOut,
)
from lumewarp_control_plane.authz import Authz
from lumewarp_control_plane.errors import not_found, routing_detail_forbidden
from lumewarp_control_plane.models import (
    ApiKey,
    CatalogKind,
    CatalogName,
    KeyOwnerKind,
    ModelType,
    RequestOutcome,
    ScopeType,
    ServiceAccount,
    Team,
    TrafficKind,
    UsageEvent,
    UsageTotal,
    User,
)

router = APIRouter(tags=["usage"])

DIMENSIONS = (
    "zone",
    "hour",
    "day",
    "endpoint",
    "model_type",
    "kind",
    "target",
    "method",
    "tool",
    "requested",
    "called",
    "model",
    "upstream_model",
    "alias",
    "router",
    "status_class",
    "outcome",
    "fallback",
    "team",
    "owner",
    "key",
)
# Output columns behind each dimension, in cursor order.
COLUMNS = {
    "zone": ("zone",),
    "hour": ("hour",),
    "day": ("day",),
    "endpoint": ("endpoint",),
    "model_type": ("model_type",),
    "kind": ("kind",),
    "target": ("target",),
    "method": ("method",),
    "tool": ("tool",),
    "requested": ("requested",),
    "called": ("called",),
    "model": ("model",),
    "upstream_model": ("upstream_model",),
    "alias": ("alias",),
    "router": ("router",),
    "status_class": ("status_class",),
    "outcome": ("outcome",),
    "fallback": ("fallback",),
    "team": ("team",),
    "owner": ("owner_kind", "owner_id"),
    "key": ("key",),
}
UUID_COLUMNS = {"team", "owner_id", "key"}
# Dimensions that name where a request was served. Only an installation admin may ask for
# them; everyone else groups by `requested`, the name the client actually called.
ROUTING_DIMENSIONS = frozenset({"model", "upstream_model", "alias", "router", "fallback"})
# Dimensions that are plain columns of the event: no catalog name to resolve.
PLAIN_DIMENSIONS = frozenset(
    {
        "zone",
        "kind",
        "target",
        "method",
        "tool",
        "endpoint",
        "model_type",
        "status_class",
        "outcome",
        "fallback",
    }
)

ENDPOINT_TYPES = {
    "chat_completions": "llm",
    "completions": "llm",
    "responses": "llm",
    "embeddings": "embedding",
    "audio_transcriptions": "transcription",
    "audio_translations": "transcription",
    "decisions": "decision",
    "systemone": "decision",
}
MODEL_TYPE_COLUMN = case(ENDPOINT_TYPES, value=UsageEvent.endpoint, else_=None)
# `called` is the catalog identity of what the client typed: an alias wins over the router
# behind it, and an unknown/rejected name has no id and keeps its literal spelling.
CALLED_ID = func.coalesce(UsageEvent.alias_id, UsageEvent.router_id, UsageEvent.model_id)
CALLED_LITERAL = func.coalesce(
    func.nullif(UsageEvent.alias, ""),
    func.nullif(UsageEvent.router, ""),
    UsageEvent.requested,
)
CALLED_KIND = case(
    (UsageEvent.alias_id.is_not(None), "alias"),
    (UsageEvent.router_id.is_not(None), "router"),
    (UsageEvent.model_id.is_not(None), "model"),
    else_="unknown",
)
STATUS_CLASS = case(
    (UsageEvent.status.between(200, 299), "2xx"),
    (UsageEvent.status.between(400, 499), "4xx"),
    (UsageEvent.status.between(500, 599), "5xx"),
    else_="other",
)

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
        elif name in ("day", "hour"):
            try:
                parsed = datetime.fromisoformat(value)
            except (ValueError, TypeError) as exc:
                raise validation_error("cursor", "invalid cursor") from exc
            if name == "hour":
                # The hour bucket is a timestamptz; compare it as the same instant.
                converted.append(
                    (parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed).astimezone(
                        UTC
                    )
                )
            else:
                if parsed.tzinfo is not None:
                    parsed = parsed.astimezone(UTC).replace(tzinfo=None)
                converted.append(parsed)
        elif name == "owner_kind":
            if value not in {kind.value for kind in KeyOwnerKind}:
                raise validation_error("cursor", "invalid cursor")
            converted.append(value)
        elif name == "outcome":
            if value not in {outcome.value for outcome in RequestOutcome}:
                raise validation_error("cursor", "invalid cursor")
            converted.append(value)
        elif name == "fallback":
            if value not in ("True", "False"):
                raise validation_error("cursor", "invalid cursor")
            converted.append(value == "True")
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
    zone: str | None,
    limit: int,
    cursor: str | None,
) -> Page[UsageRowOut]:
    group_columns = [UsageTotal.zone, UsageTotal.scope_id]
    stmt = (
        select(
            *group_columns,
            func.coalesce(func.sum(UsageTotal.request_count), 0),
            func.coalesce(func.sum(UsageTotal.prompt_tokens), 0),
            func.coalesce(func.sum(UsageTotal.completion_tokens), 0),
            func.coalesce(func.sum(UsageTotal.cost_nano_usd), 0),
            func.coalesce(func.sum(UsageTotal.unknown_cost_count), 0),
            func.coalesce(func.sum(UsageTotal.audio_ms), 0),
        )
        .where(
            UsageTotal.scope_type == ScopeType.TEAM,
            UsageTotal.window_start >= from_,
            UsageTotal.window_start < to_,
        )
        .group_by(*group_columns)
        .order_by(*group_columns)
    )
    if team_id is not None:
        stmt = stmt.where(UsageTotal.scope_id == team_id)
    if zone is not None:
        stmt = stmt.where(UsageTotal.zone == zone)
    if cursor is not None:
        # A team can appear once per zone, so the totals cursor is the (zone, team) pair.
        zone_cursor, team_cursor = _decode_cursor(cursor, ["zone", "team"])
        try:
            team_uuid = uuid.UUID(str(team_cursor))
        except (TypeError, ValueError) as exc:
            raise validation_error("cursor", "invalid cursor") from exc
        stmt = stmt.where(
            tuple_(UsageTotal.zone, UsageTotal.scope_id) > tuple_(str(zone_cursor), team_uuid)
        )
    rows = (await session.execute(stmt.limit(limit + 1))).all()
    next_cursor = (
        _encode_cursor([rows[limit - 1][0], str(rows[limit - 1][1])]) if len(rows) > limit else None
    )
    items = [
        UsageRowOut(
            zone=row[0],
            audio_ms=int(row[7]),
            # The totals table holds LLM requests only (usage.ingest), so a row from it is one.
            kind=TrafficKind.LLM,
            team_id=row[1],
            requests=int(row[2]),
            prompt_tokens=int(row[3]),
            completion_tokens=int(row[4]),
            total_tokens=int(row[3]) + int(row[4]),
            cost_nano_usd=int(row[5]),
            unknown_cost_count=int(row[6]),
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
    "audio_ms",
    "cost_nano_usd",
    "unknown_cost_count",
    "error_count",
    "total_duration_ms",
    "fallback_count",
    "retried_count",
)
_PERCENTILES = ("p50_duration_ms", "p95_duration_ms", "p99_duration_ms")


def _events_query(
    groups: list[str],
    readable: set[uuid.UUID] | None,
    from_: datetime,
    to_: datetime,
    filters: list[Any],
    tz_name: str,
) -> tuple[Any, dict[str, Any], list[str], bool]:
    """Aggregate raw rows first; resolve names/collect UI refs only after reduction.

    The default day/requested (and scope-only) load needs no catalog joins or
    distinct ref aggregates. Model groups need all provenance; alias/router groups
    need their own refs plus Served by. Upstream-model groups need Served by. A
    catalog dimension groups on its id (the literal spelling when there is none), so
    a rename merges into one row before the percentile is computed; the name and
    refs are resolved afterwards, which is also why a deleted id keeps the
    last/min spelling it was recorded under.
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
    called = "called" in groups
    # The minutes of the event's UTC offset that are not whole hours, as an interval.
    offset_minutes = func.make_interval(
        0,
        0,
        0,
        0,
        0,
        func.mod(
            cast(func.extract("minute", func.timezone(tz_name, UsageEvent.ts)), Integer)
            - cast(func.extract("minute", func.timezone("UTC", UsageEvent.ts)), Integer)
            + 60,
            60,
        ),
    )
    fields = {
        "zone": UsageEvent.zone,
        # `day` is the calendar day in the requested zone (a naive local wall-clock value).
        "day": func.date_trunc("day", func.timezone(tz_name, UsageEvent.ts)),
        # `hour` is the instant the event's local hour started, under the event's own offset:
        # truncate the UTC hour after shifting by the offset's minutes (30 for Asia/Kolkata,
        # 0 for whole-hour zones). It never goes through wall-clock time, so the two 01:00
        # hours of a DST fall-back stay two buckets.
        "hour": func.date_trunc("hour", UsageEvent.ts - offset_minutes) + offset_minutes,
        "kind": UsageEvent.kind,
        "endpoint": UsageEvent.endpoint,
        # Use a non-null cursor key; expose the absent type as null in the response.
        "model_type": func.coalesce(MODEL_TYPE_COLUMN, ""),
        "target": UsageEvent.target,
        "method": UsageEvent.method,
        "tool": UsageEvent.tool,
        "requested": UsageEvent.requested,
        "team": UsageEvent.team_id,
        "owner_kind": UsageEvent.owner_kind,
        "owner_id": UsageEvent.owner_id,
        "key": UsageEvent.key_id,
        "upstream_model": UsageEvent.upstream_model,
        "status_class": STATUS_CLASS,
        "outcome": UsageEvent.outcome,
        "fallback": UsageEvent.fallback,
    }
    group_exprs: list[Any] = [
        fields[name].label(name)
        for name in names
        if name not in {kind.value for kind in CatalogKind} and name != "called"
    ]
    aggregates: list[Any] = []
    for kind in kinds:
        id_column = getattr(UsageEvent, f"{kind}_id")
        raw_column = getattr(UsageEvent, kind)
        identity = func.coalesce(cast(id_column, String), raw_column)
        group_exprs.append(identity.label(f"{kind}_identity"))
        group_exprs.append(id_column.label(f"{kind}_id"))
        aggregates.append(func.min(raw_column).label(f"{kind}_name"))
    if called:
        group_exprs.append(
            func.coalesce(cast(CALLED_ID, String), CALLED_LITERAL).label("called_identity")
        )
        group_exprs.append(CALLED_ID.label("called_id"))
        group_exprs.append(CALLED_KIND.label("called_kind"))
        aggregates.append(func.min(CALLED_LITERAL).label("called_literal"))
        aggregates.append(func.array_agg(distinct(CALLED_LITERAL)).label("called_names"))
    if upstream:
        aggregates.append(
            func.array_agg(distinct(UsageEvent.upstream_model)).label("upstream_values")
        )
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
        func.coalesce(func.sum(UsageEvent.audio_ms), 0).label("audio_ms"),
        func.coalesce(func.sum(UsageEvent.cost_nano_usd), 0).label("cost_nano_usd"),
        func.coalesce(func.sum(case((UsageEvent.cost_known.is_(False), 1), else_=0)), 0).label(
            "unknown_cost_count"
        ),
        func.coalesce(func.sum(failed), 0).label("error_count"),
        func.coalesce(func.sum(UsageEvent.duration_ms), 0).label("total_duration_ms"),
        func.coalesce(func.sum(case((UsageEvent.fallback.is_(True), 1), else_=0)), 0).label(
            "fallback_count"
        ),
        func.coalesce(func.sum(case((UsageEvent.attempts > 1, 1), else_=0)), 0).label(
            "retried_count"
        ),
        func.percentile_cont(0.5)
        .within_group(UsageEvent.duration_ms.asc())
        .label("p50_duration_ms"),
        func.percentile_cont(0.95)
        .within_group(UsageEvent.duration_ms.asc())
        .label("p95_duration_ms"),
        func.percentile_cont(0.99)
        .within_group(UsageEvent.duration_ms.asc())
        .label("p99_duration_ms"),
    ]
    raw = select(*group_exprs, *aggregates, *amounts).where(
        UsageEvent.ts >= from_, UsageEvent.ts < to_, *filters
    )
    if readable is not None:
        raw = raw.where(UsageEvent.team_id.in_(readable))
    if group_exprs:
        raw = raw.group_by(*group_exprs)
    if not kinds and not called:
        return raw, {name: fields[name] for name in names}, kinds, upstream

    reduced = raw.subquery("usage_reduced")
    fields = {name: reduced.c[name] for name in names if name not in kinds and name != "called"}
    joins = []
    refs = []
    for kind in kinds:
        catalog = aliased(CatalogName)
        joins.append((catalog, (catalog.id == reduced.c[f"{kind}_id"]) & (catalog.kind == kind)))
        fields[kind] = case(
            (catalog.id.is_not(None), "id:" + cast(catalog.id, String)),
            else_="name:" + reduced.c[f"{kind}_name"],
        )
        ref = func.jsonb_build_object(
            "id", catalog.id, "name", func.coalesce(catalog.name, reduced.c[f"{kind}_name"])
        )
        refs.append(cast(func.jsonb_agg(distinct(ref)), JSONB).label(f"{kind}_refs"))
    if called:
        catalog = aliased(CatalogName)
        joins.append((catalog, catalog.id == reduced.c["called_id"]))
        fields["called"] = case(
            (catalog.id.is_not(None), "id:" + cast(catalog.id, String)),
            else_="name:" + reduced.c["called_literal"],
        )
        ref = func.jsonb_build_object(
            "id",
            catalog.id,
            "name",
            func.coalesce(catalog.name, reduced.c["called_literal"]),
        )
        refs.append(cast(func.jsonb_agg(distinct(ref)), JSONB).label("called_refs"))
        # jsonb, not array_agg: Postgres cannot stack arrays of different lengths.
        refs.append(
            func.jsonb_agg(distinct(func.to_jsonb(reduced.c["called_names"]))).label(
                "called_name_arrays"
            )
        )
        refs.append(func.min(reduced.c["called_kind"]).label("called_kind"))
    if upstream:
        refs.append(
            func.jsonb_agg(distinct(func.to_jsonb(reduced.c.upstream_values))).label(
                "upstream_arrays"
            )
        )
    # Keep caller's dimension order; ids unify historic spellings after rename.
    group_columns = [fields[name].label(name) for name in names]
    stmt = select(
        *group_columns,
        *(func.coalesce(func.sum(reduced.c[name]), 0).label(name) for name in _AMOUNTS),
        # Exact only while each outer group is one reduced row, which holds for every
        # grouping except merged deleted spellings; there it reports the smallest
        # per-row percentile. Re-check this if the inner grouping changes.
        *(func.min(reduced.c[name]).label(name) for name in _PERCENTILES),
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
    tz_name: str,
    limit: int,
    cursor: str | None,
) -> Page[UsageRowOut]:
    if readable is not None and not readable:
        return Page[UsageRowOut](items=[], next_cursor=None)
    # The zone is part of every grouped row. No dimension (an aggregate over the whole
    # range) stays a single all-zones row, so its `zone` is null.
    if groups and "zone" not in groups:
        groups = ["zone", *groups]
    stmt, fields, kinds, upstream = _events_query(groups, readable, from_, to_, filters, tz_name)
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
                if name in ("day", "hour")
                else getattr(last[name], "value", None) or str(last[name])
                for name in names
            ]
        )
    page_rows = rows[:limit]
    # The key dimension is the only grouped shape that carries an id; resolve the whole
    # page's refs in one query and leave `key` null for every other shape.
    refs = await key_refs(session, [row.get("key") for row in page_rows])
    items = []
    for row in page_rows:
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
            arrays = row["upstream_arrays"] or []
            raw_values = {value for array in arrays for value in (array or [])}
            provenance["upstream_model"] = next(iter(raw_values)) if len(raw_values) == 1 else None
            provenance["upstream_models"] = sorted(value for value in raw_values if value)
        called = None
        called_kind = None
        called_names: list[str] = []
        if "called" in names:
            entries = row["called_refs"] or []
            if entries:
                entry = next((item for item in entries if item["id"]), entries[0])
                called = entry["name"]
            called_kind = row["called_kind"]
            arrays = row["called_name_arrays"] or []
            called_names = sorted({value for array in arrays for value in (array or []) if value})
        # `kind`, `target`, `method` and `tool` are plain dimensions: no catalog refs.
        plain = {name: row[name] for name in names if name in PLAIN_DIMENSIONS}
        if plain.get("model_type") == "":
            plain["model_type"] = None
        items.append(
            UsageRowOut(
                **provenance,
                **plain,
                date=row["day"].date() if "day" in row else None,
                hour=row.get("hour"),
                requested=row.get("requested"),
                called=called,
                called_kind=called_kind,
                called_names=called_names,
                team_id=row.get("team"),
                owner_kind=row.get("owner_kind"),
                owner_id=row.get("owner_id"),
                key_id=row.get("key"),
                key=refs.get(row.get("key")),
                **{name: int(row[name]) for name in _AMOUNTS},
                total_tokens=int(row["prompt_tokens"]) + int(row["completion_tokens"]),
                **{
                    name: None if row.get(name) is None else float(row[name])
                    for name in _PERCENTILES
                },
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


@router.get("/usage", response_model=UsagePage, operation_id="getUsage")
async def get_usage(
    authz: Authz,
    session: SessionDep,
    from_: FromQuery = None,
    to_: ToQuery = None,
    group_by: Annotated[
        str | None,
        Query(
            description=(
                "Comma-separated dimensions to group by; default `day,requested`. `hour` limits"
                " the range to 31 days."
            )
        ),
    ] = None,
    tz: Annotated[
        str,
        Query(
            description=(
                "IANA time zone used to bucket `day` and `hour`, e.g. `Asia/Taipei`;"
                " default `UTC`. An unknown zone is 422."
            )
        ),
    ] = "UTC",
    team_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    service_account_id: uuid.UUID | None = None,
    key_id: uuid.UUID | None = None,
    kind: TrafficKind | None = None,
    endpoint: str | None = None,
    model_type: ModelType | None = None,
    target: str | None = None,
    method: str | None = None,
    tool: str | None = None,
    zone: Annotated[
        str | None, Query(description="Only this zone; omitted reads every zone.")
    ] = None,
    requested: str | None = None,
    called: Annotated[
        str | None,
        Query(
            description=(
                "Only rows whose called catalog item (or literal spelling) has this name;"
                " resolved like the `model` filter."
            )
        ),
    ] = None,
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
    compare: Annotated[
        Literal["previous"] | None,
        Query(
            description=(
                "`previous` adds the equal-length range immediately before `from` as"
                " ungrouped `previous` totals; omitted leaves it null."
            )
        ),
    ] = None,
) -> UsagePage:
    """Aggregate usage within the caller's existing team/key permission scope.

    `day,requested` is the default; `day` is the calendar day in `tz`, and `hour` gives
    aware one-hour buckets (at most a 31-day range). `requested` is the literal name the
    client called, so a renamed model, alias or router appears as separate before/after
    names. `called` groups by the catalog item instead (the alias, else the router, else the
    model, else the literal), so a rename merges; it is visible to every caller, as are
    `status_class` and `outcome`. Only an installation admin may group or filter by `model`,
    `upstream_model`, `alias`, `router` or `fallback` and their ids/`_missing` variants. Any
    other caller gets 403 `routing_detail_forbidden` and never receives `served_by`,
    `via_aliases`, `via_routers` or `upstream_model(s)`.

    Every row carries fallback/retry counts and p50/p95/p99 durations. The percentiles are
    null on the monthly totals fast path, which stores no durations. `compare=previous`
    adds the equal-length range immediately before `from` as ungrouped totals.

    A plain member gets whole-team totals when neither `owner` nor `key` is grouped and no
    `user_id`/`service_account_id`/`key_id` filter is set. Grouping or filtering by owner or
    key keeps the own-user-keys predicate inside teams the caller does not administer.

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
    try:
        zone_info = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise validation_error("tz", f"unknown IANA time zone {tz!r}") from exc
    tz_name = zone_info.key
    if "hour" in groups and end - start > timedelta(days=31):
        raise validation_error("to", "a range grouped by hour must not exceed 31 days")
    is_admin = await authz.is_admin()
    if not is_admin:
        # Refuse before any scope lookup: a caller without routing access learns nothing
        # about a team's routing shape, and the answer does not depend on which team exists.
        if ROUTING_DIMENSIONS.intersection(groups):
            raise routing_detail_forbidden()
        routing_filters = (
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
        if any(value is not None for value in routing_filters):
            raise routing_detail_forbidden()
    scoped_team = await _check_scope_filters(
        session, authz, team_id, user_id, service_account_id, key_id
    )
    if is_admin:
        # An installation admin reads every team; neither listing is needed.
        readable = None
        admin_teams = None
    else:
        candidates = set(await session.scalars(select(Team.id)))
        # A narrowing filter already proved TEAM_READ; otherwise limit to the readable teams.
        readable = None if scoped_team is not None else await authz.readable_teams(candidates)
        admin_teams = await authz.admin_teams(candidates)
    use_totals = (
        is_admin
        and tz_name == "UTC"
        and groups == ["team"]
        and requested is None
        and called is None
        and compare is None
        # The totals table counts LLM requests only, so both of these ask the same question.
        and kind in (None, TrafficKind.LLM)
        and all(
            value is None
            for value in (
                endpoint,
                model_type,
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
        and zone is None
        and _month_aligned(start, end)
    )
    if use_totals:
        page = await _totals_page(session, start, end, team_id, zone, limit, cursor)
        previous = None
    else:
        ensure_range(start, end)
        filters: list[Any] = []
        if zone is not None:
            filters.append(UsageEvent.zone == zone)
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
        if called is not None:
            called_id = await session.scalar(
                select(CatalogName.id).where(CatalogName.name == called)
            )
            if called_id is not None:
                filters.append(CALLED_ID == called_id)
            else:
                live_ids = select(CatalogName.id)
                filters.append(
                    and_(
                        CALLED_LITERAL == called,
                        or_(CALLED_ID.is_(None), CALLED_ID.not_in(live_ids)),
                    )
                )
        for column, value in (
            (UsageEvent.kind, kind),
            (UsageEvent.endpoint, endpoint),
            (MODEL_TYPE_COLUMN, model_type),
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
            # A team admin keeps the whole-team view. A plain member sees whole-team totals only
            # when the result cannot identify another member: no `owner`/`key` dimension and no
            # owner/key filter. `requested`, `called`, `day`, `team`, `endpoint` and the rest are
            # team totals, so widening them to every readable team (the `readable` filter above,
            # or `scoped_team`) reveals no other member's keys. Any per-owner or per-key shape
            # keeps today's predicate: in a team the caller does not administer, only their own
            # user-owned keys. This is applied server-side and cannot be widened by a filter.
            splits_owner = (
                "owner" in groups
                or "key" in groups
                or user_id is not None
                or service_account_id is not None
                or key_id is not None
            )
            if splits_owner:
                filters.append(
                    or_(
                        UsageEvent.team_id.in_(admin_teams),
                        and_(
                            UsageEvent.owner_kind == KeyOwnerKind.USER,
                            UsageEvent.owner_id == authz.user_id,
                        ),
                    )
                )
        page = await _events_page(
            session, groups, readable, start, end, filters, tz_name, limit, cursor
        )
        previous = None
        if compare == "previous":
            span = end - start
            prior = await _events_page(
                session, [], readable, start - span, start, filters, tz_name, 1, None
            )
            if prior.items:
                row = prior.items[0]
                previous = UsageTotalsOut(
                    requests=row.requests,
                    prompt_tokens=row.prompt_tokens,
                    completion_tokens=row.completion_tokens,
                    total_tokens=row.total_tokens,
                    audio_ms=row.audio_ms,
                    cost_nano_usd=row.cost_nano_usd,
                    unknown_cost_count=row.unknown_cost_count,
                    error_count=row.error_count,
                    total_duration_ms=row.total_duration_ms,
                    fallback_count=row.fallback_count,
                    retried_count=row.retried_count,
                    p50_duration_ms=row.p50_duration_ms,
                    p95_duration_ms=row.p95_duration_ms,
                    p99_duration_ms=row.p99_duration_ms,
                )
    return UsagePage(items=page.items, next_cursor=page.next_cursor, previous=previous)


LeaderboardBy = Literal["requested", "called", "key", "owner"]


@router.get(
    "/usage/leaderboard",
    response_model=Page[UsageLeaderboardRowOut],
    operation_id="getUsageLeaderboard",
)
async def get_usage_leaderboard(
    authz: Authz,
    session: SessionDep,
    by: Annotated[
        LeaderboardBy,
        Query(
            description=(
                "Board to rank: `requested` (literal called spelling), `called` (called"
                " catalog item, renames merged), `key` (API key) or `owner` (user or"
                " service account)."
            )
        ),
    ],
    from_: Annotated[
        datetime | None,
        Query(
            alias="from",
            description=(
                "Start of the range (inclusive); defaults to the start of the current UTC month."
            ),
        ),
    ] = None,
    to_: Annotated[
        datetime | None,
        Query(alias="to", description="End of the range (exclusive); defaults to now."),
    ] = None,
    team_id: Annotated[
        uuid.UUID | None,
        Query(description="Only rows of this team; it must be readable by the caller."),
    ] = None,
    kind: Annotated[
        TrafficKind,
        Query(description="Gateway protocol to rank; `llm` by default."),
    ] = TrafficKind.LLM,
    zone: Annotated[
        str | None, Query(description="Only this zone; omitted ranks every zone.")
    ] = None,
    limit: Annotated[
        int,
        Query(ge=1, le=50, description="How many rows to return, 1 to 50; default 10."),
    ] = 10,
    order: Annotated[
        Literal["tokens", "cost"],
        Query(
            description=(
                "What ranks the rows: `tokens` (total tokens, then spend; the default) or"
                " `cost` (spend, then total tokens)."
            )
        ),
    ] = "tokens",
) -> Page[UsageLeaderboardRowOut]:
    """Rank called names, keys or owners inside the caller's readable teams.

    Rows are ordered by total tokens (input plus output) descending, then by spend, or with
    `order=cost` by spend, then by total tokens; `rank`
    restarts at 1 on each page. `by=requested` aggregates the literal name the client called
    across the readable teams (a renamed model, alias or router shows as separate names).
    `by=called` groups the called catalog item instead (the alias, else the router, else the
    model, else the literal), so a rename merges; it is visible to every caller. `by=key`
    and `by=owner` group within the team and return `team_id`. Without a `zone` filter rows
    merge every zone of that identity and their `zone` is null; with one they stay per zone.

    An installation admin ranks every team. Everyone else ranks only their readable teams;
    a plain member therefore sees another member in the ranking but not their identity:
    `key` is null and `label` is `Key #<rank>` for a key the caller may not read, and
    `owner_kind`/`owner_id` are null with `label` `Member #<rank>` for an owner the caller
    may not read. The caller's own owner row is always named and has `is_me=true`.
    """
    now = datetime.now(UTC)
    start = (aware(from_, "from") or month_start(now)).astimezone(UTC)
    end = (aware(to_, "to") or now).astimezone(UTC)
    if end <= start:
        raise validation_error("to", "must be after from")
    ensure_range(start, end)
    scoped_team = await _check_scope_filters(session, authz, team_id, None, None, None)
    is_admin = await authz.is_admin()
    if is_admin:
        readable = None
        admin_teams = None
    else:
        candidates = set(await session.scalars(select(Team.id)))
        readable = None if scoped_team is not None else await authz.readable_teams(candidates)
        admin_teams = await authz.admin_teams(candidates)
    if readable is not None and not readable:
        return Page[UsageLeaderboardRowOut](items=[], next_cursor=None)

    # Without a zone filter the board merges every zone into one row per identity; with it,
    # rows stay per zone. `zone` in the response is null for a merged row.
    zone_group = [UsageEvent.zone] if zone is not None else []
    extra_columns: list[Any] = []
    if by == "requested":
        group_columns = [*zone_group, UsageEvent.requested]
    elif by == "called":
        group_columns = [
            *zone_group,
            func.coalesce(cast(CALLED_ID, String), CALLED_LITERAL).label("called_identity"),
            CALLED_ID.label("called_id"),
        ]
        extra_columns = [
            func.min(CALLED_LITERAL).label("called_literal"),
            func.min(CALLED_KIND).label("called_kind"),
        ]
    elif by == "key":
        group_columns = [*zone_group, UsageEvent.team_id, UsageEvent.key_id]
    else:
        group_columns = [
            *zone_group,
            UsageEvent.team_id,
            UsageEvent.owner_kind,
            UsageEvent.owner_id,
        ]
    total_tokens = func.coalesce(func.sum(UsageEvent.prompt_tokens), 0) + func.coalesce(
        func.sum(UsageEvent.completion_tokens), 0
    )
    spend = func.coalesce(func.sum(UsageEvent.cost_nano_usd), 0)
    stmt = (
        select(
            *group_columns,
            *extra_columns,
            func.count().label("requests"),
            func.coalesce(func.sum(UsageEvent.prompt_tokens), 0).label("prompt_tokens"),
            func.coalesce(func.sum(UsageEvent.completion_tokens), 0).label("completion_tokens"),
            total_tokens.label("total_tokens"),
            spend.label("cost_nano_usd"),
            func.coalesce(func.sum(case((UsageEvent.cost_known.is_(False), 1), else_=0)), 0).label(
                "unknown_cost_count"
            ),
        )
        .where(UsageEvent.ts >= start, UsageEvent.ts < end, UsageEvent.kind == kind)
        .group_by(*group_columns)
        .order_by(
            *(
                (spend.desc(), total_tokens.desc())
                if order == "cost"
                else (total_tokens.desc(), spend.desc())
            ),
            *group_columns,
        )
        .limit(limit)
    )
    if scoped_team is not None:
        stmt = stmt.where(UsageEvent.team_id == scoped_team)
    elif readable is not None:
        stmt = stmt.where(UsageEvent.team_id.in_(readable))
    if zone is not None:
        stmt = stmt.where(UsageEvent.zone == zone)
    rows = (await session.execute(stmt)).mappings().all()

    admin_team_ids = admin_teams or set()

    def may_read_owner(team: uuid.UUID, owner_kind: KeyOwnerKind, owner_id: uuid.UUID) -> bool:
        if is_admin or team in admin_team_ids:
            return True
        return owner_kind is KeyOwnerKind.USER and owner_id == authz.user_id

    refs = await key_refs(session, [row["key_id"] for row in rows]) if by == "key" else {}
    called_names: dict[uuid.UUID, str] = {}
    if by == "called":
        called_ids = {row["called_id"] for row in rows if row["called_id"] is not None}
        if called_ids:
            for name_id, name in await session.execute(
                select(CatalogName.id, CatalogName.name).where(CatalogName.id.in_(called_ids))
            ):
                called_names[name_id] = name
    # Resolve the names of visible owners only: a hidden row keeps no id and no name.
    wanted_users: set[uuid.UUID] = set()
    wanted_accounts: set[uuid.UUID] = set()
    if by == "owner":
        for row in rows:
            if may_read_owner(row["team_id"], row["owner_kind"], row["owner_id"]):
                target = wanted_users if row["owner_kind"] is KeyOwnerKind.USER else wanted_accounts
                target.add(row["owner_id"])
    user_labels: dict[uuid.UUID, str] = {}
    if wanted_users:
        for user_id, display_name, email in await session.execute(
            select(User.id, User.display_name, User.email).where(User.id.in_(wanted_users))
        ):
            user_labels[user_id] = display_name or email
    account_labels: dict[uuid.UUID, str] = {}
    if wanted_accounts:
        for account_id, name in await session.execute(
            select(ServiceAccount.id, ServiceAccount.name).where(
                ServiceAccount.id.in_(wanted_accounts)
            )
        ):
            account_labels[account_id] = name

    items: list[UsageLeaderboardRowOut] = []
    for rank, row in enumerate(rows, start=1):
        values: dict[str, Any] = {
            "rank": rank,
            "zone": row.get("zone"),
            "requests": int(row["requests"]),
            "prompt_tokens": int(row["prompt_tokens"]),
            "completion_tokens": int(row["completion_tokens"]),
            "total_tokens": int(row["total_tokens"]),
            "cost_nano_usd": int(row["cost_nano_usd"]),
            "unknown_cost_count": int(row["unknown_cost_count"]),
        }
        if by == "requested":
            values["requested"] = row["requested"]
            values["label"] = row["requested"] or ""
        elif by == "called":
            label = called_names.get(row["called_id"]) or row["called_literal"] or ""
            values["called"] = label
            values["called_kind"] = row["called_kind"]
            values["label"] = label
        elif by == "key":
            values["team_id"] = row["team_id"]
            ref = refs.get(row["key_id"])
            readable_key = ref is not None and (
                is_admin
                or ref.team_id in admin_team_ids
                or (ref.user_id is not None and ref.user_id == authz.user_id)
            )
            if readable_key:
                values["key"] = ref
                values["label"] = ref.name
            else:
                values["label"] = f"Key #{rank}"
        else:
            team = row["team_id"]
            owner_kind = row["owner_kind"]
            owner_id = row["owner_id"]
            values["team_id"] = team
            values["is_me"] = owner_kind is KeyOwnerKind.USER and owner_id == authz.user_id
            if may_read_owner(team, owner_kind, owner_id):
                values["owner_kind"] = owner_kind
                values["owner_id"] = owner_id
                labels = user_labels if owner_kind is KeyOwnerKind.USER else account_labels
                values["label"] = labels.get(owner_id) or f"Member #{rank}"
            else:
                values["label"] = f"Member #{rank}"
        items.append(UsageLeaderboardRowOut(**values))
    return Page[UsageLeaderboardRowOut](items=items, next_cursor=None)
