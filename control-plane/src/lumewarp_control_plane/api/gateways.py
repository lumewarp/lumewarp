"""Gateway fleet status and lifecycle (installation admins only).

A gateway is one stream: the control plane assigns its id at handshake and every reconnect
is a new row. Reads take a `zone` query parameter (default: the default zone); the only
lifecycle action is deleting a disconnected gateway row.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, status
from sqlalchemy import and_, or_, select

from lumewarp_control_plane import audit
from lumewarp_control_plane.api.common import (
    LimitDep,
    SessionDep,
    SettingsDep,
    conflict,
    mutation,
    validation_error,
)
from lumewarp_control_plane.api.schemas import GatewayOut, Page
from lumewarp_control_plane.authz import Action, Authz
from lumewarp_control_plane.errors import not_found
from lumewarp_control_plane.models import GatewaySession

router = APIRouter(tags=["gateways"])


def _cursor(value: str) -> tuple[bool, str]:
    """Parse `c:<id>` (last row was connected) or `d:<id>` (disconnected)."""
    prefix, separator, gateway_id = value.partition(":")
    if separator and gateway_id and prefix in ("c", "d"):
        return prefix == "c", gateway_id
    raise validation_error("cursor", "invalid cursor")


@router.get("/gateways", response_model=Page[GatewayOut], operation_id="listGateways")
async def list_gateways(
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
    limit: LimitDep = 50,
    cursor: str | None = None,
    zone: Annotated[str, Query(description="Config zone to list.")] | None = None,
) -> Page[GatewayOut]:
    """One row per gateway stream of one zone, connected ones first and then by gateway id.

    Requires an installation admin. A disconnected gateway stays visible for 24 h after its
    stream ended. Omit `zone` for the default zone.
    """
    await authz.require(Action.GATEWAY_READ)
    stmt = select(GatewaySession).where(GatewaySession.zone == (zone or settings.zone))
    if cursor is not None:
        after_connected, gateway_id = _cursor(cursor)
        if after_connected:
            stmt = stmt.where(
                or_(
                    and_(
                        GatewaySession.connected.is_(True),
                        GatewaySession.gateway_id > gateway_id,
                    ),
                    GatewaySession.connected.is_(False),
                )
            )
        else:
            stmt = stmt.where(
                GatewaySession.connected.is_(False), GatewaySession.gateway_id > gateway_id
            )
    rows = (
        await session.scalars(
            stmt.order_by(GatewaySession.connected.desc(), GatewaySession.gateway_id).limit(
                limit + 1
            )
        )
    ).all()
    page = rows[:limit]
    items = [
        GatewayOut(
            zone=row.zone,
            gateway_id=row.gateway_id,
            connected=row.connected,
            peer=row.peer,
            gateway_version=row.gateway_version,
            capabilities=list(row.capabilities),
            acked_version=row.acked_version,
            pending_version=row.pending_version,
            last_nack_version=row.last_nack_version,
            last_nack_code=row.last_nack_code,
            last_nack_reason=row.last_nack_reason,
            connected_at=row.connected_at,
            disconnected_at=row.disconnected_at,
            last_seen_at=row.last_seen_at,
            shared_limits_enabled=row.shared_limits_enabled,
            shared_limits_namespace=row.shared_limits_namespace,
        )
        for row in page
    ]
    last = page[-1] if page else None
    next_cursor = (
        f"{'c' if last.connected else 'd'}:{last.gateway_id}"
        if len(rows) > limit and last is not None
        else None
    )
    return Page[GatewayOut](items=items, next_cursor=next_cursor)


@router.delete(
    "/gateways/{zone}/{gateway_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="deleteGateway",
)
async def delete_gateway(zone: str, gateway_id: str, authz: Authz, session: SessionDep) -> None:
    """Delete a disconnected gateway row and its plugin reports.

    Requires an installation admin. A connected gateway cannot be deleted (409); it has to
    disconnect first. A disconnected row is deleted automatically 24 h after its stream
    ended, so this only shortens that. Unknown rows are 404.
    """
    await authz.require(Action.GATEWAY_MANAGE)
    async with mutation(session):
        row = await session.get(GatewaySession, (zone, gateway_id))
        if row is None:
            raise not_found("gateway not found")
        if row.connected:
            raise conflict("a connected gateway cannot be deleted")
        await session.delete(row)
        audit.record(
            session,
            action="gateway.delete",
            resource_type="gateway",
            resource_id=f"{zone}/{gateway_id}",
            principal=authz.principal,
            details={"zone": zone, "gateway_id": gateway_id},
        )
