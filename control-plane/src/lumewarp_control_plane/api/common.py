"""Shared dependencies and small helpers for the admin API."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import Depends, Query, Request
from sqlalchemy import Select, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane import audit, outbox
from lumewarp_control_plane.authz import Action, RequestAuthz
from lumewarp_control_plane.db import get_session
from lumewarp_control_plane.errors import ApiError, not_found
from lumewarp_control_plane.metrics import DISCARD, Metrics, note_suspended
from lumewarp_control_plane.models import ApiKey, KeyLifecycle, KeyOwnerKind, Scope, Team
from lumewarp_control_plane.security import Principal
from lumewarp_control_plane.settings import Settings

SessionDep = Annotated[AsyncSession, Depends(get_session)]
LimitDep = Annotated[int, Query(ge=1, le=500)]
MAX_QUERY_RANGE = timedelta(days=400)


def app_settings(request: Request) -> Settings:
    return request.app.state.settings


SettingsDep = Annotated[Settings, Depends(app_settings)]


def app_metrics(request: Request) -> Metrics:
    return getattr(request.app.state, "metrics", DISCARD)


MetricsDep = Annotated[Metrics, Depends(app_metrics)]


def validation_error(field: str, message: str) -> ApiError:
    return ApiError(422, "validation_error", f"{field}: {message}")


def conflict(message: str) -> ApiError:
    return ApiError(409, "conflict", message)


def ensure_not_null(body: object, *fields: str) -> None:
    """Reject an explicit null for a field that is nullable only because PATCH omits it."""
    supplied = getattr(body, "model_fields_set", set())
    for name in fields:
        if name in supplied and getattr(body, name) is None:
            raise validation_error(name, "cannot be null")


@asynccontextmanager
async def mutation(session: AsyncSession) -> AsyncIterator[None]:
    """One transaction for the row change, its audit row and the outbox request."""
    try:
        async with session.begin():
            yield
    except IntegrityError as exc:
        raise conflict("database constraint violated") from exc


async def enqueue_recompile(session: AsyncSession, settings: Settings, reason: str) -> int:
    try:
        return await outbox.enqueue_recompile(session, settings.scope, reason)
    except LookupError as exc:
        raise conflict(f"config scope {settings.scope!r} is not initialized") from exc


async def lock_scope(session: AsyncSession, settings: Settings) -> None:
    """Take the scope row lock first thing in a hierarchy mutation.

    Narrowing validation reads ancestors and descendants; without the lock a parent
    narrowing and a child widening can both validate against the old state and both commit.
    Holding it from the start makes the later reads see the latest committed state.
    """
    locked = await session.scalar(
        select(Scope.name).where(Scope.name == settings.scope).with_for_update()
    )
    if locked is None:
        raise conflict(f"config scope {settings.scope!r} is not initialized")


def ensure_range(start: datetime, end: datetime) -> None:
    if end - start > MAX_QUERY_RANGE:
        raise validation_error("from", f"range must not exceed {MAX_QUERY_RANGE.days} days")


def parse_uuid_cursor(cursor: str) -> uuid.UUID:
    try:
        return uuid.UUID(cursor)
    except ValueError as exc:
        raise validation_error("cursor", "invalid cursor") from exc


def parse_int_cursor(cursor: str) -> int:
    try:
        return int(cursor)
    except ValueError as exc:
        raise validation_error("cursor", "invalid cursor") from exc


async def uuid_page(
    session: AsyncSession,
    stmt: Select[Any],
    id_column: Any,
    limit: int,
    cursor: str | None,
) -> tuple[list[Any], str | None]:
    """Run an id-ordered query and return (rows, next_cursor)."""
    if cursor is not None:
        stmt = stmt.where(id_column > parse_uuid_cursor(cursor))
    rows = list((await session.scalars(stmt.limit(limit + 1))).all())
    next_cursor = str(rows[limit - 1].id) if len(rows) > limit else None
    return rows[:limit], next_cursor


async def ensure_team_read(authz: RequestAuthz, team_id: uuid.UUID) -> None:
    """404 unless the caller may read the team (so unreadable and unknown look the same)."""
    if not await authz.allowed(Action.TEAM_READ, team_id):
        raise not_found("team not found")


async def ensure_key_read(authz: RequestAuthz, key: ApiKey) -> None:
    if not await authz.allowed(Action.TEAM_READ, key.team_id):
        raise not_found("key not found")


async def get_team_or_404(session: AsyncSession, authz: RequestAuthz, team_id: uuid.UUID) -> Team:
    team = await session.get(Team, team_id)
    if team is None:
        raise not_found("team not found")
    await ensure_team_read(authz, team.id)
    return team


def ensure_grants_managed(request: Request) -> None:
    """Member writes are read-only when an external system owns the memberships."""
    if not request.app.state.authorizer.manages_grants:
        raise ApiError(409, "managed_externally", "memberships are managed by the authz backend")


async def suspend_user_keys(
    session: AsyncSession,
    principal: Principal,
    *,
    user_id: uuid.UUID,
    team_id: uuid.UUID | None,
    reason: str,
) -> int:
    """Suspend every non-revoked key a user owns (in one team, or everywhere) and audit each
    one. Returns the number of keys changed; the caller enqueues the recompile.
    """
    stmt = select(ApiKey).where(
        ApiKey.owner_kind == KeyOwnerKind.USER,
        ApiKey.user_id == user_id,
        ApiKey.lifecycle != KeyLifecycle.REVOKED,
    )
    if team_id is not None:
        stmt = stmt.where(ApiKey.team_id == team_id)
    keys = (await session.scalars(stmt.order_by(ApiKey.id))).all()
    note_suspended(session, reason, sum(k.lifecycle is KeyLifecycle.ACTIVE for k in keys))
    for key in keys:
        key.lifecycle = KeyLifecycle.SUSPENDED
        key.suspended_reason = reason
        audit.record(
            session,
            action="key.suspend",
            resource_type="key",
            resource_id=key.id,
            principal=principal,
            team_id=key.team_id,
            details={"name": key.name, "user_id": user_id, "reason": reason},
        )
    return len(keys)


def ensure_parent_matches(provided: uuid.UUID | None, actual: uuid.UUID, field: str) -> None:
    if provided is not None and provided != actual:
        raise not_found(f"{field} does not match the URL")


def aware(value: datetime | None, field: str) -> datetime | None:
    if value is not None and value.tzinfo is None:
        raise validation_error(field, "must include a timezone")
    return value


def month_start(ts: datetime) -> datetime:
    return datetime(ts.year, ts.month, 1, tzinfo=ts.tzinfo)
