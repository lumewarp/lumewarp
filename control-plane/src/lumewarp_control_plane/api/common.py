"""Shared dependencies and small helpers for the admin API."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Any

from fastapi import Depends, Query, Request
from sqlalchemy import Select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane import outbox, rbac
from lumewarp_control_plane.db import get_session
from lumewarp_control_plane.errors import ApiError, not_found
from lumewarp_control_plane.models import ApiKey, Org, Project, Team
from lumewarp_control_plane.security import Principal
from lumewarp_control_plane.settings import Settings

SessionDep = Annotated[AsyncSession, Depends(get_session)]
LimitDep = Annotated[int, Query(ge=1, le=500)]


def app_settings(request: Request) -> Settings:
    return request.app.state.settings


SettingsDep = Annotated[Settings, Depends(app_settings)]


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


def org_scope(org: Org) -> rbac.ResourceScope:
    return rbac.ResourceScope(org.id)


def team_scope(team: Team) -> rbac.ResourceScope:
    return rbac.ResourceScope(team.org_id, team.id)


def project_scope(project: Project) -> rbac.ResourceScope:
    return rbac.ResourceScope(project.org_id, project.team_id)


def key_scope(key: ApiKey) -> rbac.ResourceScope:
    return rbac.ResourceScope(key.org_id, key.team_id)


def ensure_org_read(principal: Principal, org: Org) -> None:
    if not rbac.authorize(principal, rbac.Action.ORG_READ, org_scope(org)):
        raise not_found("org not found")


def ensure_team_read(principal: Principal, team: Team) -> None:
    if not rbac.authorize(principal, rbac.Action.READ, team_scope(team)):
        raise not_found("team not found")


def ensure_project_read(principal: Principal, project: Project) -> None:
    if not rbac.authorize(principal, rbac.Action.READ, project_scope(project)):
        raise not_found("project not found")


def ensure_key_read(principal: Principal, key: ApiKey) -> None:
    if not rbac.authorize(principal, rbac.Action.READ, key_scope(key)):
        raise not_found("key not found")


def ensure_parent_matches(provided: uuid.UUID | None, actual: uuid.UUID, field: str) -> None:
    if provided is not None and provided != actual:
        raise not_found(f"{field} does not match the URL")


def aware(value: datetime | None, field: str) -> datetime | None:
    if value is not None and value.tzinfo is None:
        raise validation_error(field, "must include a timezone")
    return value


def month_start(ts: datetime) -> datetime:
    return datetime(ts.year, ts.month, 1, tzinfo=ts.tzinfo)
