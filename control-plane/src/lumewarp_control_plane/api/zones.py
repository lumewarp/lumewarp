"""Zones: the independent config scopes of one installation.

Any signed-in caller may list the live zones (the Admin UI's zone switcher needs them).
Only installation admins create, edit and archive zones. A zone's `name` is a slug and is
immutable; every gateway token, snapshot, session and (later) key references it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Query, status
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
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
from lumewarp_control_plane.api.schemas import (
    Page,
    ZoneCreate,
    ZoneOut,
    ZoneUpdate,
)
from lumewarp_control_plane.authz import Action, Authz
from lumewarp_control_plane.compiler.validate import metrics_url_problem
from lumewarp_control_plane.errors import not_found
from lumewarp_control_plane.models import (
    ApiKey,
    ConfigOutbox,
    GatewaySession,
    GatewayToken,
    KeyLifecycle,
    Zone,
)
from lumewarp_control_plane.settings import Settings

router = APIRouter(tags=["zones"])


def _out(row: Zone, settings: Settings) -> ZoneOut:
    return ZoneOut(
        name=row.name,
        is_default=row.name == settings.zone,
        display_name=row.display_name,
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
    stmt = select(Zone).order_by(Zone.archived_at.is_not(None), Zone.name)
    if not (include_archived and await authz.is_admin()):
        stmt = stmt.where(Zone.archived_at.is_(None))
    rows = list((await session.scalars(stmt)).all())
    return Page[ZoneOut](items=[_out(row, settings) for row in rows], next_cursor=None)


@router.post(
    "/zones",
    response_model=ZoneOut,
    status_code=status.HTTP_201_CREATED,
    operation_id="createZone",
)
async def create_zone(
    body: ZoneCreate, authz: Authz, session: SessionDep, settings: SettingsDep
) -> ZoneOut:
    """Create a config zone. It starts empty: no gateway serves it and no upstream,
    key or snapshot belongs to it yet. Requires an installation admin.
    """
    await authz.require(Action.ZONE_MANAGE)
    async with mutation(session):
        row = Zone(
            name=body.name,
            display_name=body.display_name or body.name,
            color=body.color,
            playground_gateway_url=_playground_url(body.playground_gateway_url),
            key_min_creator_role=body.key_min_creator_role,
            key_max_lifetime_seconds=body.key_max_lifetime_seconds,
            key_expiry_required=body.key_expiry_required,
            key_max_per_member=body.key_max_per_member,
            service_account_keys_allowed=body.service_account_keys_allowed,
            playground_allowed=body.playground_allowed,
        )
        session.add(row)
        try:
            await session.flush()
        except IntegrityError as exc:
            raise conflict(f"zone {body.name!r} already exists") from exc
        audit.record(
            session,
            action="zone.create",
            resource_type="zone",
            resource_id=row.name,
            principal=authz.principal,
            details={"name": row.name, "display_name": row.display_name, "color": row.color},
        )
    await session.refresh(row)
    return _out(row, settings)


async def _live_zone(session: AsyncSession, name: str) -> Zone:
    row = await session.get(Zone, name)
    if row is None:
        raise not_found("zone not found")
    if row.archived_at is not None:
        raise conflict(f"zone {name!r} is archived")
    return row


@router.patch("/zones/{name}", response_model=ZoneOut, operation_id="updateZone")
async def update_zone(
    name: str, body: ZoneUpdate, authz: Authz, session: SessionDep, settings: SettingsDep
) -> ZoneOut:
    """Change a zone's display name, colour, Playground URL or key rules. The zone's `name`
    is immutable. Requires an installation admin.
    """
    await authz.require(Action.ZONE_MANAGE)
    ensure_not_null(
        body,
        "display_name",
        "color",
        "key_min_creator_role",
        "key_expiry_required",
        "service_account_keys_allowed",
        "playground_allowed",
    )
    async with mutation(session):
        row = await _live_zone(session, name)
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
            resource_id=row.name,
            principal=authz.principal,
            details={"name": row.name, "changes": changes},
        )
    await session.refresh(row)
    return _out(row, settings)


@router.delete("/zones/{name}", response_model=ZoneOut, operation_id="archiveZone")
async def archive_zone(
    name: str, authz: Authz, session: SessionDep, settings: SettingsDep
) -> ZoneOut:
    """Archive a zone: revoke its gateway tokens and stop compiling it. Requires an
    installation admin. Refused (409) for the default zone (`LUMEWARP_ZONE`), while a
    gateway session is still connected, and while a key of the zone is not revoked.
    """
    await authz.require(Action.ZONE_MANAGE)
    async with mutation(session):
        row = await session.get(Zone, name)
        if row is None:
            raise not_found("zone not found")
        if row.archived_at is not None:
            raise conflict(f"zone {name!r} is already archived")
        if name == settings.zone:
            raise conflict("the default zone cannot be archived")
        if blockers := await zone_archive_blockers(session, name):
            raise conflict("zone cannot be archived: " + "; ".join(blockers))
        now = datetime.now(UTC)
        tokens = list(
            (
                await session.scalars(
                    select(GatewayToken).where(
                        GatewayToken.zone == name, GatewayToken.revoked_at.is_(None)
                    )
                )
            ).all()
        )
        for token in tokens:
            token.revoked_at = now
            audit.record(
                session,
                action="gateway_token.revoke",
                resource_type="gateway_token",
                resource_id=token.id,
                principal=authz.principal,
                details={"name": token.name},
            )
        row.archived_at = now
        # Nothing will compile this zone again, so its pending requests are moot.
        await session.execute(
            delete(ConfigOutbox).where(ConfigOutbox.zone == name, ConfigOutbox.status == "pending")
        )
        await session.flush()
        audit.record(
            session,
            action="zone.archive",
            resource_type="zone",
            resource_id=row.name,
            principal=authz.principal,
            details={"name": row.name, "revoked_tokens": len(tokens)},
        )
    await session.refresh(row)
    return _out(row, settings)
