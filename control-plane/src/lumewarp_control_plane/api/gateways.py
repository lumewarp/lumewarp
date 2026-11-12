"""Gateway fleet status and gateway tokens (installation admins only)."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
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
    GatewayInstanceOut,
    GatewayOut,
    GatewayTokenCreate,
    GatewayTokenCreated,
    GatewayTokenOut,
    Page,
)
from lumewarp_control_plane.authz import Action, Authz
from lumewarp_control_plane.errors import not_found
from lumewarp_control_plane.models import GatewayQuarantine, GatewaySession, GatewayToken, Scope
from lumewarp_control_plane.security import SecretKind, generate_secret

router = APIRouter(tags=["gateways"])


def _capabilities(instances: list[GatewaySession]) -> list[str]:
    """A capability every connected instance reports: a replica without it would NACK."""
    reported = [set(row.capabilities) for row in instances if row.connected]
    if not reported:
        return []
    return sorted(reported[0].intersection(*reported[1:]))


def _version(instances: list[GatewaySession]) -> str:
    """The build version, or the distinct ones joined by `, ` when they differ."""
    return ", ".join(sorted({row.gateway_version for row in instances if row.gateway_version}))


def _earliest(values: Iterable[datetime | None]) -> datetime | None:
    return min((value for value in values if value is not None), default=None)


def _latest(values: Iterable[datetime | None]) -> datetime | None:
    return max((value for value in values if value is not None), default=None)


def _aggregate(instances: list[GatewaySession]) -> dict:
    """The gateway-level state derived from its instances (docs/config-protocol.md).

    `acked_version` is the lowest ACKed version of the connected instances (all of them when
    none is connected), `pending_version` is set while a connected instance has an update in
    flight, and the last NACK is the newest one of a live instance, ignoring versions the
    gateway has since ACKed.
    """
    connected = [row for row in instances if row.connected]
    live = connected or instances
    acked = min((row.acked_version for row in live if row.acked_version is not None), default=None)
    # A NACK only counts while it is still true: from a live instance, of a version newer than
    # the one the gateway has applied. A replaced pod's row keeps its NACK for up to 24 h, and
    # another instance's later ACK clears it here without waiting for that row to go away.
    nacked = [
        row
        for row in live
        if row.last_nack_version is not None and (acked is None or row.last_nack_version > acked)
    ]
    newest_nack = max(
        nacked, key=lambda row: row.last_seen_at or datetime.min.replace(tzinfo=UTC), default=None
    )
    return {
        "connected": bool(connected),
        "gateway_version": _version(live),
        "capabilities": _capabilities(instances),
        "acked_version": acked,
        "pending_version": max(
            (row.pending_version for row in connected if row.pending_version is not None),
            default=None,
        ),
        "last_nack_version": newest_nack.last_nack_version if newest_nack else None,
        "last_nack_code": newest_nack.last_nack_code if newest_nack else None,
        "last_nack_reason": newest_nack.last_nack_reason if newest_nack else None,
        "connected_at": _earliest(row.connected_at for row in live),
        "disconnected_at": None if connected else _latest(row.disconnected_at for row in instances),
        "last_seen_at": _latest(row.last_seen_at for row in instances),
    }


@router.get("/gateways", response_model=Page[GatewayOut], operation_id="listGateways")
async def list_gateways(
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
    limit: LimitDep = 50,
    cursor: str | None = None,
) -> Page[GatewayOut]:
    """Gateways with their tokens, their instances and the config version each last
    acknowledged. Requires an installation admin.
    """
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
    instances: dict[str, list[GatewaySession]] = {}
    for row in (
        await session.scalars(
            select(GatewaySession)
            .where(GatewaySession.scope == scope, GatewaySession.gateway_id.in_(page))
            .order_by(GatewaySession.gateway_id, GatewaySession.instance_id)
        )
    ).all():
        instances.setdefault(row.gateway_id, []).append(row)
    quarantines = {
        gateway_id: version
        for gateway_id, version in (
            await session.execute(
                select(GatewayQuarantine.gateway_id, GatewayQuarantine.quarantined_version).where(
                    GatewayQuarantine.scope == scope, GatewayQuarantine.gateway_id.in_(page)
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
        rows = instances.get(gateway_id, [])
        items.append(
            GatewayOut(
                gateway_id=gateway_id,
                **_aggregate(rows),
                quarantined_version=quarantines.get(gateway_id),
                instances=[
                    GatewayInstanceOut(
                        instance_id=row.instance_id,
                        connected=row.connected,
                        peer=row.peer,
                        gateway_version=row.gateway_version,
                        acked_version=row.acked_version,
                        pending_version=row.pending_version,
                        last_nack_version=row.last_nack_version,
                        connected_at=row.connected_at,
                        disconnected_at=row.disconnected_at,
                        last_seen_at=row.last_seen_at,
                        shared_limits_enabled=row.shared_limits_enabled,
                        shared_limits_namespace=row.shared_limits_namespace,
                    )
                    for row in rows
                ],
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
    """Create a gateway token (`lwg-…`) bound to one gateway id. The secret is in this response
    only. Requires an installation admin.
    """
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
    """Requires an installation admin. The gateway's open config and usage streams are closed at
    once.
    """
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
