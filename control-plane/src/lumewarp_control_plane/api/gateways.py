"""Gateway fleet status and gateway tokens (installation admins only).

A gateway is one process, so the fleet list has exactly one row per gateway. Tokens are
zone credentials with a label: they are not bound to a gateway id and any number of
gateways may authenticate with one. Reads take a `zone` query parameter (default: the
default zone); a token's zone is fixed at creation.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Query, Request, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane import audit
from lumewarp_control_plane import grpc as grpc_services
from lumewarp_control_plane.api.common import (
    LimitDep,
    SessionDep,
    SettingsDep,
    conflict,
    enqueue_recompile,
    mutation,
    parse_uuid_cursor,
    validation_error,
)
from lumewarp_control_plane.api.schemas import (
    GatewayOut,
    GatewayTokenCreate,
    GatewayTokenCreated,
    GatewayTokenOut,
    GatewayTokenRef,
    Page,
)
from lumewarp_control_plane.authz import Action, Authz
from lumewarp_control_plane.errors import not_found
from lumewarp_control_plane.models import GatewaySession, GatewayToken, Zone
from lumewarp_control_plane.security import SecretKind, generate_secret

router = APIRouter(tags=["gateways"])


def _token_out(row: GatewayToken, connected_gateways: int = 0) -> GatewayTokenOut:
    return GatewayTokenOut(
        id=row.id,
        zone=row.zone,
        name=row.name,
        display_prefix=row.display_prefix,
        created_at=row.created_at,
        last_used_at=row.last_used_at,
        revoked_at=row.revoked_at,
        connected_gateways=connected_gateways,
    )


async def _connected_counts(
    session: AsyncSession, token_ids: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """How many connected gateway sessions each of `token_ids` has right now."""
    if not token_ids:
        return {}
    rows = await session.execute(
        select(GatewaySession.token_id, func.count())
        .where(
            GatewaySession.token_id.in_(token_ids),
            GatewaySession.connected.is_(True),
        )
        .group_by(GatewaySession.token_id)
    )
    return {token_id: count for token_id, count in rows}


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
    """One row per gateway process of one zone, connected ones first and then by gateway
    id.

    Requires an installation admin. A disconnected gateway stays visible for 24 h after its
    stream ended. Each row names the token its latest Hello authenticated with. Omit `zone`
    for the default zone.
    """
    await authz.require(Action.GATEWAY_READ)
    zone = zone or settings.zone
    stmt = (
        select(GatewaySession, GatewayToken)
        .join(GatewayToken, GatewayToken.id == GatewaySession.token_id)
        .where(GatewaySession.zone == zone)
    )
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
        await session.execute(
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
            token=GatewayTokenRef(
                id=token.id, name=token.name, display_prefix=token.display_prefix
            ),
        )
        for row, token in page
    ]
    last = page[-1][0] if page else None
    next_cursor = (
        f"{'c' if last.connected else 'd'}:{last.gateway_id}"
        if len(rows) > limit and last is not None
        else None
    )
    return Page[GatewayOut](items=items, next_cursor=next_cursor)


@router.get(
    "/gateway-tokens",
    response_model=Page[GatewayTokenOut],
    operation_id="listGatewayTokens",
)
async def list_gateway_tokens(
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
    limit: LimitDep = 50,
    cursor: str | None = None,
    zone: Annotated[str, Query(description="Config zone to list.")] | None = None,
) -> Page[GatewayTokenOut]:
    """Gateway tokens of one zone, newest first, with their connected gateway count.

    Requires an installation admin. A revoked token stays listed. Omit `zone` for the default
    zone.
    """
    await authz.require(Action.GATEWAY_READ)
    stmt = select(GatewayToken).where(GatewayToken.zone == (zone or settings.zone))
    if cursor is not None:
        stmt = stmt.where(GatewayToken.id < parse_uuid_cursor(cursor))
    rows = list(
        (await session.scalars(stmt.order_by(GatewayToken.id.desc()).limit(limit + 1))).all()
    )
    page = rows[:limit]
    counts = await _connected_counts(session, [row.id for row in page])
    return Page[GatewayTokenOut](
        items=[_token_out(row, counts.get(row.id, 0)) for row in page],
        next_cursor=str(page[-1].id) if len(rows) > limit else None,
    )


@router.post(
    "/gateway-tokens",
    response_model=GatewayTokenCreated,
    status_code=status.HTTP_201_CREATED,
    operation_id="createGatewayToken",
)
async def create_gateway_token(
    body: GatewayTokenCreate,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> GatewayTokenCreated:
    """Create a gateway token (`lwg-…`) with a label in a zone (default: the default zone).
    It is not bound to a gateway id, so any number of gateways may use it. The secret is in
    this response only. Requires an installation admin.
    """
    await authz.require(Action.GATEWAY_TOKEN_MANAGE)
    zone = body.zone or settings.zone
    new = generate_secret(SecretKind.GATEWAY_TOKEN)
    async with mutation(session):
        target = await session.get(Zone, zone)
        if target is None or target.archived_at is not None:
            raise conflict(f"config zone {zone!r} is not available")
        row = GatewayToken(
            zone=zone,
            name=body.name,
            token_hash=new.hash,
            display_prefix=new.display_prefix,
        )
        session.add(row)
        await session.flush()
        await session.refresh(row)
        audit.record(
            session,
            action="gateway_token.create",
            resource_type="gateway_token",
            resource_id=row.id,
            principal=authz.principal,
            details={"name": row.name, "zone": row.zone},
        )
        await enqueue_recompile(session, settings, "api.gateway_token.create", zone=zone)
    return GatewayTokenCreated(token=_token_out(row), secret=new.secret)


@router.post(
    "/gateway-tokens/{token_id}/revoke",
    response_model=GatewayTokenOut,
    operation_id="revokeGatewayToken",
)
async def revoke_gateway_token(
    token_id: uuid.UUID,
    request: Request,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> GatewayTokenOut:
    """Revoke a gateway token. Every config and usage stream authenticated with it is closed
    at once; other tokens are unaffected. Requires an installation admin.
    """
    await authz.require(Action.GATEWAY_TOKEN_MANAGE)
    async with mutation(session):
        row = await session.get(GatewayToken, token_id)
        if row is None:
            raise not_found("gateway token not found")
        if row.revoked_at is None:
            row.revoked_at = datetime.now(UTC)
            await session.flush()
            await session.refresh(row)
            audit.record(
                session,
                action="gateway_token.revoke",
                resource_type="gateway_token",
                resource_id=row.id,
                principal=authz.principal,
                details={"name": row.name},
            )
            await enqueue_recompile(session, settings, "api.gateway_token.revoke", zone=row.zone)
    # Streams also re-check their token periodically; this makes revocation immediate.
    runtime = getattr(request.app.state, "runtime", None)
    server = getattr(runtime, "grpc_server", None)
    if server is not None and (registry := grpc_services.registry_for(server)) is not None:
        registry.close_token(token_id)
    # A revoked token's streams are closed; their rows are mirrored disconnected later.
    return _token_out(row, 0)
