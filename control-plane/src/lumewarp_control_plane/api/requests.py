"""The request log: one row per gateway request, newest first.

Separate from the audit log (administrative changes) and from `/usage` (aggregates). A row
shows the name-resolution chain the gateway recorded: `requested` -> `alias` -> `router` ->
(`fallback`) -> `model` -> `upstream`/`upstream_model`. No prompt or response content is
stored; the request id finds the details in OTel.

The permission scope is exactly `/usage`'s, and the parent checks are shared with it rather
than copied. A plain member sees their own user-owned keys only, a team admin (inherited org
admin included) the whole team, and an installation admin everything. Only an installation
admin sees the routing chain: for every other caller `alias`, `router`, `fallback`, `model`,
`upstream`, `upstream_model` and `attempts` are null, and the `model`/`alias`/`router`
filters answer 403 `routing_detail_forbidden`.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import Field
from sqlalchemy import and_, or_, select, tuple_

from lumewarp_control_plane.api.common import (
    LimitDep,
    SessionDep,
    aware,
    ensure_range,
    validation_error,
)
from lumewarp_control_plane.api.key_refs import KeyRefOut, key_refs
from lumewarp_control_plane.api.schemas import Page, ReadModel
from lumewarp_control_plane.api.usage import _check_scope_filters
from lumewarp_control_plane.authz import Authz
from lumewarp_control_plane.errors import routing_detail_forbidden
from lumewarp_control_plane.models import (
    CatalogKind,
    CatalogName,
    KeyOwnerKind,
    PlaygroundRequest,
    RequestOutcome,
    Team,
    TrafficKind,
    UsageEvent,
    UsageState,
)

router = APIRouter(tags=["usage"])

FromQuery = Annotated[
    datetime | None,
    Query(alias="from", description="Start of the range (inclusive); defaults to 24 hours ago."),
]
ToQuery = Annotated[
    datetime | None,
    Query(alias="to", description="End of the range (exclusive); defaults to now."),
]


class RequestOut(ReadModel):
    """One gateway request: the name the client called and where it landed.

    The routing fields (`alias`, `router`, `fallback`, `model`, `upstream`,
    `upstream_model`, `attempts`) are filled only for an installation admin; every other
    caller receives them as null.
    """

    request_id: uuid.UUID
    ts: datetime
    kind: TrafficKind
    endpoint: str
    requested: str
    alias: str | None = None
    alias_id: uuid.UUID | None = None
    router: str | None = None
    router_id: uuid.UUID | None = None
    fallback: bool | None = None
    model: str | None = None
    model_id: uuid.UUID | None = None
    upstream: str | None = None
    upstream_model: str | None = None
    attempts: int | None = None
    status: int
    outcome: RequestOutcome
    stream: bool
    prompt_tokens: int
    completion_tokens: int
    audio_ms: int
    cost_nano_usd: int
    usage_state: UsageState
    duration_ms: int
    key_id: uuid.UUID
    key: KeyRefOut | None = Field(
        default=None, description="The key as it is now; null when its row no longer exists."
    )
    team_id: uuid.UUID
    owner_kind: KeyOwnerKind
    owner_id: uuid.UUID
    playground_user_id: uuid.UUID | None = None


def _encode_cursor(ts: datetime, request_id: uuid.UUID) -> str:
    raw = json.dumps([ts.isoformat(), str(request_id)], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        values = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        if not isinstance(values, list) or len(values) != 2:
            raise ValueError("cursor shape")
        ts_value, request_id_value = values
        if not isinstance(ts_value, str) or not isinstance(request_id_value, str):
            raise ValueError("cursor shape")
        ts = datetime.fromisoformat(ts_value)
        if ts.tzinfo is None:
            raise ValueError("cursor timestamp")
        request_id = uuid.UUID(request_id_value)
    except (TypeError, ValueError) as exc:
        raise validation_error("cursor", "invalid cursor") from exc
    return ts.astimezone(UTC), request_id


async def _name_filter(session: Any, catalog_kind: CatalogKind, name: str) -> Any:
    """Match a current live name, or an orphan row still carrying a deleted spelling.

    The same rule `/usage` applies: a renamed, still-live item is selected by its current
    name/id only, never mixed into a deleted item's historical spelling.
    """
    current_id = await session.scalar(
        select(CatalogName.id).where(CatalogName.kind == catalog_kind, CatalogName.name == name)
    )
    column = getattr(UsageEvent, catalog_kind.value)
    id_column = getattr(UsageEvent, f"{catalog_kind.value}_id")
    if current_id is not None:
        return id_column == current_id
    live_ids = select(CatalogName.id).where(CatalogName.kind == catalog_kind)
    return (column == name) & or_(id_column.is_(None), id_column.not_in(live_ids))


@router.get("/requests", response_model=Page[RequestOut], operation_id="listRequests")
async def list_requests(
    authz: Authz,
    session: SessionDep,
    kind: Annotated[
        TrafficKind,
        Query(
            description=(
                "Gateway protocol; `llm` by default. MCP and A2A rows need `mcp` or `a2a`."
            )
        ),
    ] = TrafficKind.LLM,
    team_id: Annotated[uuid.UUID | None, Query(description="Only requests of this team.")] = None,
    key_id: Annotated[
        uuid.UUID | None, Query(description="Only requests made with this key.")
    ] = None,
    user_id: Annotated[
        uuid.UUID | None,
        Query(description="Only requests whose key is owned by this user; requires `team_id`."),
    ] = None,
    service_account_id: Annotated[
        uuid.UUID | None,
        Query(
            description=(
                "Only requests whose key is owned by this service account; requires `team_id`."
            )
        ),
    ] = None,
    requested: Annotated[
        str | None, Query(description="Only requests that called this name.")
    ] = None,
    model: Annotated[
        str | None, Query(description="Only requests served by this model name.")
    ] = None,
    alias: Annotated[
        str | None, Query(description="Only requests that went through this alias name.")
    ] = None,
    router: Annotated[
        str | None, Query(description="Only requests that went through this router name.")
    ] = None,
    status: Annotated[
        int | None,
        Query(ge=100, le=599, description="Only responses with this HTTP status."),
    ] = None,
    outcome: Annotated[
        RequestOutcome | None, Query(description="Only requests with this outcome.")
    ] = None,
    request_id: Annotated[
        uuid.UUID | None, Query(description="Only the request with this gateway request id.")
    ] = None,
    from_: FromQuery = None,
    to_: ToQuery = None,
    limit: LimitDep = 50,
    cursor: Annotated[
        str | None,
        Query(description="Opaque cursor from the previous page's `next_cursor`."),
    ] = None,
) -> Page[RequestOut]:
    """One row per gateway request, newest first: the name the client called and where it
    landed (`requested` -> `alias` -> `router` -> `model` -> `upstream`), with status,
    tokens, cost and duration. No prompt or response content is stored; look the request
    up in OTel by `request_id`.

    The default range is the last 24 hours and the default kind is `llm`; MCP and A2A rows
    need an explicit `kind`. The permission scope is `/usage`'s: a plain member sees their
    own user-owned keys only, a team admin (inherited org admin included) their teams, and
    an installation admin everything. An out-of-scope `team_id` or `key_id` answers exactly
    as it does on `/usage` (404 or 403). Only an installation admin sees the routing chain:
    for every other caller the `model`, `alias`, `router`, `upstream`, `upstream_model`,
    `fallback` and `attempts` fields are null, and those name filters answer 403
    `routing_detail_forbidden`. `playground_user_id` is set when the request came from the
    Admin UI Playground.
    """
    now = datetime.now(UTC)
    start = (aware(from_, "from") or now - timedelta(hours=24)).astimezone(UTC)
    end = (aware(to_, "to") or now).astimezone(UTC)
    if end <= start:
        raise validation_error("to", "must be after from")
    ensure_range(start, end)
    is_admin = await authz.is_admin()
    if not is_admin and any(name is not None for name in (model, alias, router)):
        # Refuse before resolving a name or checking scope: no routing shape leaks either way.
        raise routing_detail_forbidden()
    scoped_team = await _check_scope_filters(
        session, authz, team_id, user_id, service_account_id, key_id
    )
    if is_admin:
        readable = None
        admin_teams = None
    else:
        candidates = set(await session.scalars(select(Team.id)))
        # A narrowing filter already proved TEAM_READ; otherwise limit to readable teams.
        readable = None if scoped_team is not None else await authz.readable_teams(candidates)
        # Narrower than `/usage`, which sums whole teams for a plain member: requests are
        # per-request, so a plain member only ever sees rows of their own user-owned keys.
        admin_teams = await authz.admin_teams(candidates)
    if readable is not None and not readable:
        return Page[RequestOut](items=[], next_cursor=None)

    filters: list[Any] = [
        UsageEvent.kind == kind,
        UsageEvent.ts >= start,
        UsageEvent.ts < end,
    ]
    for catalog_kind, name in (
        (CatalogKind.MODEL, model),
        (CatalogKind.ALIAS, alias),
        (CatalogKind.ROUTER, router),
    ):
        if name is not None:
            filters.append(await _name_filter(session, catalog_kind, name))
    for column, value in (
        (UsageEvent.requested, requested),
        (UsageEvent.status, status),
        (UsageEvent.outcome, outcome),
        (UsageEvent.request_id, request_id),
    ):
        if value is not None:
            filters.append(column == value)
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
    if readable is not None:
        filters.append(UsageEvent.team_id.in_(readable))
    if admin_teams is not None:
        filters.append(
            or_(
                UsageEvent.team_id.in_(admin_teams),
                and_(
                    UsageEvent.owner_kind == KeyOwnerKind.USER,
                    UsageEvent.owner_id == authz.user_id,
                ),
            )
        )

    stmt = (
        select(UsageEvent, PlaygroundRequest.user_id)
        .outerjoin(PlaygroundRequest, PlaygroundRequest.request_id == UsageEvent.request_id)
        .where(*filters)
        .order_by(UsageEvent.ts.desc(), UsageEvent.request_id.desc())
    )
    if cursor is not None:
        cursor_ts, cursor_request_id = _decode_cursor(cursor)
        stmt = stmt.where(
            tuple_(UsageEvent.ts, UsageEvent.request_id) < tuple_(cursor_ts, cursor_request_id)
        )
    rows = (await session.execute(stmt.limit(limit + 1))).all()
    next_cursor = None
    if len(rows) > limit:
        last_event = rows[limit - 1][0]
        next_cursor = _encode_cursor(last_event.ts, last_event.request_id)
    page_rows = rows[:limit]
    # One query for the page's key refs, never one per row. A key row that is gone leaves
    # `key` null while `key_id` keeps pointing at it.
    refs = await key_refs(session, [event.key_id for event, _ in page_rows])
    items = [
        RequestOut(
            request_id=event.request_id,
            ts=event.ts,
            kind=event.kind,
            endpoint=event.endpoint,
            requested=event.requested,
            # Routing internals are for an installation admin only. Everything below is
            # null for a member or team/org admin, whatever the event recorded.
            alias=event.alias if is_admin else None,
            alias_id=event.alias_id if is_admin else None,
            router=event.router if is_admin else None,
            router_id=event.router_id if is_admin else None,
            fallback=event.fallback if is_admin else None,
            model=event.model if is_admin else None,
            model_id=event.model_id if is_admin else None,
            upstream=event.upstream if is_admin else None,
            upstream_model=event.upstream_model if is_admin else None,
            attempts=event.attempts if is_admin else None,
            status=event.status,
            outcome=event.outcome,
            stream=event.stream,
            prompt_tokens=event.prompt_tokens,
            completion_tokens=event.completion_tokens,
            audio_ms=event.audio_ms,
            cost_nano_usd=event.cost_nano_usd,
            usage_state=event.usage_state,
            duration_ms=event.duration_ms,
            key_id=event.key_id,
            key=refs.get(event.key_id),
            team_id=event.team_id,
            owner_kind=event.owner_kind,
            owner_id=event.owner_id,
            playground_user_id=playground_user_id,
        )
        for event, playground_user_id in page_rows
    ]
    return Page[RequestOut](items=items, next_cursor=next_cursor)
