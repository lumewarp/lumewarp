"""Zones: the independent config scopes of one installation.

A zone is declared by its `LUMEWARP_GATEWAY_ZONE_<ID>_TOKEN` at control-plane startup; the
API cannot create one. Any signed-in caller may list the live zones (the Admin UI's zone
switcher needs them). Only installation admins edit and archive zones. A zone's `id` is a
slug and is immutable; every snapshot, session and key references it. Its `name` is the
display name and may change; `token_configured` says whether the environment has a token
for it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane import audit
from lumewarp_control_plane.api.common import (
    SessionDep,
    SettingsDep,
    conflict,
    ensure_not_null,
    mutation,
    validation_error,
)
from lumewarp_control_plane.api.schemas import Page, ZoneOut, ZoneUpdate
from lumewarp_control_plane.authz import Action, Authz
from lumewarp_control_plane.compiler.validate import metrics_url_problem
from lumewarp_control_plane.errors import not_found
from lumewarp_control_plane.models import (
    ApiKey,
    ConfigOutbox,
    GatewaySession,
    KeyLifecycle,
    Zone,
)
from lumewarp_control_plane.settings import Settings

router = APIRouter(tags=["zones"])


def _out(row: Zone, settings: Settings) -> ZoneOut:
    return ZoneOut(
        id=row.id,
        name=row.name,
        is_default=row.id == settings.zone,
        color=row.color,
        archived_at=row.archived_at,
        created_at=row.created_at,
        playground_gateway_url=row.playground_gateway_url,
        key_min_creator_role=row.key_min_creator_role,
        key_max_lifetime_seconds=row.key_max_lifetime_seconds,
        key_expiry_required=row.key_expiry_required,
        key_max_per_member=row.key_max_per_member,
        service_account_keys_allowed=row.service_account_keys_allowed,
        playground_allowed=row.playground_allowed,
        token_configured=row.id in settings.gateway_zone_tokens,
    )


def _playground_url(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if problem := metrics_url_problem(value):
        raise validation_error("playground_gateway_url", problem)
    return value


async def zone_archive_blockers(session: AsyncSession, zone: str) -> list[str]:
    """Why the zone cannot be archived right now; empty means it can.

    A gateway still streaming this zone's config would have no control plane to talk to
    afterwards, so it must disconnect first; a key that is not revoked would disappear from
    every snapshot, so it must be revoked or left for later.
    """
    reasons: list[str] = []
    connected = await session.scalar(
        select(func.count()).where(GatewaySession.zone == zone, GatewaySession.connected.is_(True))
    )
    if connected:
        reasons.append(f"{connected} gateway session(s) are connected")
    live_keys = await session.scalar(
        select(func.count()).where(ApiKey.zone == zone, ApiKey.lifecycle != KeyLifecycle.REVOKED)
    )
    if live_keys:
        reasons.append(f"{live_keys} key(s) are not revoked")
    return reasons


@router.get("/zones", response_model=Page[ZoneOut], operation_id="listZones")
async def list_zones(
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
    include_archived: Annotated[
        bool, Query(description="Installation admins also get archived zones.")
    ] = False,
) -> Page[ZoneOut]:
    """The config zones. Any signed-in caller sees the live ones; an installation admin may
    ask for archived zones too. Archived zones are read-only history.
    """
    stmt = select(Zone).order_by(Zone.archived_at.is_not(None), Zone.id)
    if not (include_archived and await authz.is_admin()):
        stmt = stmt.where(Zone.archived_at.is_(None))
    rows = list((await session.scalars(stmt)).all())
    return Page[ZoneOut](items=[_out(row, settings) for row in rows], next_cursor=None)


async def _live_zone(session: AsyncSession, zone: str) -> Zone:
    row = await session.get(Zone, zone)
    if row is None:
        raise not_found("zone not found")
    if row.archived_at is not None:
        raise conflict(f"zone {zone!r} is archived")
    return row


@router.patch("/zones/{zone}", response_model=ZoneOut, operation_id="updateZone")
async def update_zone(
    zone: str, body: ZoneUpdate, authz: Authz, session: SessionDep, settings: SettingsDep
) -> ZoneOut:
    """Change a zone's name, colour, Playground URL or key rules. The zone's `id` is
    immutable. Requires an installation admin.
    """
    await authz.require(Action.ZONE_MANAGE)
    ensure_not_null(
        body,
        "name",
        "color",
        "key_min_creator_role",
        "key_expiry_required",
        "service_account_keys_allowed",
        "playground_allowed",
    )
    async with mutation(session):
        row = await _live_zone(session, zone)
        changes: dict[str, object] = {}
        for field in body.model_fields_set:
            value = getattr(body, field)
            if field == "playground_gateway_url":
                value = _playground_url(value)
            setattr(row, field, value)
            changes[field] = value
        await session.flush()
        audit.record(
            session,
            action="zone.update",
            resource_type="zone",
            resource_id=row.id,
            principal=authz.principal,
            details={"id": row.id, "changes": changes},
        )
    await session.refresh(row)
    return _out(row, settings)


@router.delete("/zones/{zone}", response_model=ZoneOut, operation_id="archiveZone")
async def archive_zone(
    zone: str, authz: Authz, session: SessionDep, settings: SettingsDep
) -> ZoneOut:
    """Archive a zone: stop compiling it, and refuse its gateways even if their env token
    still exists. Requires an installation admin. Refused (409) for the default zone
    (`LUMEWARP_ZONE`), while a gateway session is still connected, and while a key of the
    zone is not revoked.
    """
    await authz.require(Action.ZONE_MANAGE)
    async with mutation(session):
        row = await session.get(Zone, zone)
        if row is None:
            raise not_found("zone not found")
        if row.archived_at is not None:
            raise conflict(f"zone {zone!r} is already archived")
        if zone == settings.zone:
            raise conflict("the default zone cannot be archived")
        if blockers := await zone_archive_blockers(session, zone):
            raise conflict("zone cannot be archived: " + "; ".join(blockers))
        row.archived_at = datetime.now(UTC)
        # Nothing will compile this zone again, so its pending requests are moot.
        await session.execute(
            delete(ConfigOutbox).where(ConfigOutbox.zone == zone, ConfigOutbox.status == "pending")
        )
        await session.flush()
        audit.record(
            session,
            action="zone.archive",
            resource_type="zone",
            resource_id=row.id,
            principal=authz.principal,
            details={"id": row.id},
        )
    await session.refresh(row)
    return _out(row, settings)
