"""Gateway fleet status and gateway tokens (installation-level, admin group member only)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Request, status
from sqlalchemy import select, union

from lumewarp_control_plane import audit
from lumewarp_control_plane import grpc as grpc_services
from lumewarp_control_plane.api.common import (
    LimitDep,
    SessionDep,
    SettingsDep,
    conflict,
    mutation,
)
from lumewarp_control_plane.api.schemas import (
    GatewayOut,
    GatewayTokenCreate,
    GatewayTokenCreated,
    GatewayTokenOut,
    Page,
)
from lumewarp_control_plane.authz import Action, Authz
from lumewarp_control_plane.errors import not_found
from lumewarp_control_plane.models import GatewaySession, GatewayToken, Scope
from lumewarp_control_plane.security import SecretKind, generate_secret

router = APIRouter(tags=["gateways"])


@router.get("/gateways", response_model=Page[GatewayOut], operation_id="listGateways")
async def list_gateways(
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
    limit: LimitDep = 50,
    cursor: str | None = None,
) -> Page[GatewayOut]:
    await authz.require(Action.GATEWAY_READ)
    scope = settings.scope
    ids = union(
        select(GatewaySession.gateway_id).where(GatewaySession.scope == scope),
        select(GatewayToken.gateway_id).where(GatewayToken.scope == scope),
    ).subquery()
    id_stmt = select(ids.c.gateway_id).order_by(ids.c.gateway_id)
    if cursor is not None:
        id_stmt = id_stmt.where(ids.c.gateway_id > cursor)
    gateway_ids = list((await session.scalars(id_stmt.limit(limit + 1))).all())
    page = gateway_ids[:limit]
    sessions = {
        row.gateway_id: row
        for row in (
            await session.scalars(
                select(GatewaySession).where(
                    GatewaySession.scope == scope, GatewaySession.gateway_id.in_(page)
                )
            )
        ).all()
    }
    tokens: dict[str, list[GatewayToken]] = {}
    for row in (
        await session.scalars(
            select(GatewayToken)
            .where(GatewayToken.scope == scope, GatewayToken.gateway_id.in_(page))
            .order_by(GatewayToken.gateway_id, GatewayToken.created_at)
        )
    ).all():
        tokens.setdefault(row.gateway_id, []).append(row)
    items: list[GatewayOut] = []
    for gateway_id in page:
        row = sessions.get(gateway_id)
        items.append(
            GatewayOut(
                gateway_id=gateway_id,
                connected=row.connected if row is not None else False,
                gateway_version=row.gateway_version if row is not None else "",
                capabilities=row.capabilities if row is not None else [],
                acked_version=row.acked_version if row is not None else None,
                pending_version=row.pending_version if row is not None else None,
                last_nack_version=row.last_nack_version if row is not None else None,
                last_nack_code=row.last_nack_code if row is not None else None,
                last_nack_reason=row.last_nack_reason if row is not None else None,
                quarantined_version=row.quarantined_version if row is not None else None,
                connected_at=row.connected_at if row is not None else None,
                disconnected_at=row.disconnected_at if row is not None else None,
                last_seen_at=row.last_seen_at if row is not None else None,
                tokens=[
                    GatewayTokenOut.model_validate(token) for token in tokens.get(gateway_id, [])
                ],
            )
        )
    next_cursor = page[-1] if len(gateway_ids) > limit else None
    return Page[GatewayOut](items=items, next_cursor=next_cursor)


@router.post(
    "/gateways",
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
    await authz.require(Action.GATEWAY_TOKEN_MANAGE)
    new = generate_secret(SecretKind.GATEWAY_TOKEN)
    async with mutation(session):
        scope = await session.get(Scope, settings.scope)
        if scope is None:
            raise conflict(f"config scope {settings.scope!r} is not initialized")
        row = GatewayToken(
            scope=settings.scope,
            gateway_id=body.gateway_id,
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
            details={"gateway_id": row.gateway_id, "name": row.name},
        )
    return GatewayTokenCreated(token=GatewayTokenOut.model_validate(row), secret=new.secret)


@router.post(
    "/gateways/{gateway_id}/tokens/{token_id}/revoke",
    response_model=GatewayTokenOut,
    operation_id="revokeGatewayToken",
)
async def revoke_gateway_token(
    gateway_id: str,
    token_id: uuid.UUID,
    request: Request,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> GatewayTokenOut:
    await authz.require(Action.GATEWAY_TOKEN_MANAGE)
    async with mutation(session):
        row = await session.get(GatewayToken, token_id)
        if row is None or row.gateway_id != gateway_id or row.scope != settings.scope:
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
                details={"gateway_id": row.gateway_id, "name": row.name},
            )
    # Streams also re-check their token periodically; this makes revocation immediate.
    runtime = getattr(request.app.state, "runtime", None)
    server = getattr(runtime, "grpc_server", None)
    if server is not None and (registry := grpc_services.registry_for(server)) is not None:
        registry.close_token(token_id)
    return GatewayTokenOut.model_validate(row)
