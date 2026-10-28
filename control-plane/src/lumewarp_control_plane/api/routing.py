"""Routing: upstreams, deployments and public model routes."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Response, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane import audit
from lumewarp_control_plane.api.common import (
    LimitDep,
    SessionDep,
    SettingsDep,
    conflict,
    enqueue_recompile,
    mutation,
    uuid_page,
    validation_error,
)
from lumewarp_control_plane.api.policy import ensure_unique_ids
from lumewarp_control_plane.api.schemas import (
    DeploymentCreate,
    DeploymentOut,
    DeploymentUpdate,
    Page,
    RouteCreate,
    RouteDeploymentOut,
    RouteOut,
    RouteUpdate,
    UpstreamCreate,
    UpstreamOut,
    UpstreamUpdate,
)
from lumewarp_control_plane.authz import Action, Authz
from lumewarp_control_plane.errors import not_found
from lumewarp_control_plane.models import (
    Deployment,
    Route,
    RouteDeployment,
    RoutingStrategy,
    Upstream,
)

router = APIRouter(tags=["routing"])


def _non_null(body: object, *fields: str) -> None:
    supplied = body.model_fields_set  # type: ignore[attr-defined]
    for name in fields:
        if name in supplied and getattr(body, name) is None:
            raise validation_error(name, "cannot be null")


def _check_strategy(strategy: RoutingStrategy | None) -> None:
    if strategy is RoutingStrategy.LOWEST_LATENCY:
        raise validation_error("strategy", "lowest_latency is not supported by the P1 gateway")


async def _deployment_ids(session: SessionDep, ids: list[uuid.UUID]) -> list[uuid.UUID]:
    ensure_unique_ids(ids)
    found = set((await session.scalars(select(Deployment.id).where(Deployment.id.in_(ids)))).all())
    missing = [str(item) for item in ids if item not in found]
    if missing:
        raise validation_error("deployment_ids", f"unknown deployments: {', '.join(missing)}")
    return ids


async def _check_positive_weight(
    session: AsyncSession, strategy: RoutingStrategy, ids: list[uuid.UUID]
) -> None:
    """A weighted route whose deployments all have weight 0 makes the gateway NACK."""
    if strategy is not RoutingStrategy.WEIGHTED:
        return
    total = await session.scalar(
        select(func.coalesce(func.sum(Deployment.weight), 0)).where(Deployment.id.in_(ids))
    )
    if not total:
        raise validation_error(
            "deployment_ids", "a weighted route needs deployments with a positive total weight"
        )


async def _routes_out(session: AsyncSession, rows: list[Route]) -> list[RouteOut]:
    links: dict[uuid.UUID, list[RouteDeploymentOut]] = {row.id: [] for row in rows}
    if rows:
        found = await session.scalars(
            select(RouteDeployment)
            .where(RouteDeployment.route_id.in_(links))
            .order_by(RouteDeployment.route_id, RouteDeployment.position)
        )
        for link in found:
            links[link.route_id].append(
                RouteDeploymentOut(deployment_id=link.deployment_id, position=link.position)
            )
    return [
        RouteOut(
            id=row.id,
            model=row.model,
            strategy=row.strategy,
            deployments=links[row.id],
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
        for row in rows
    ]


async def _route_out(session: AsyncSession, row: Route) -> RouteOut:
    return (await _routes_out(session, [row]))[0]


# --------------------------------------------------------------------------- upstreams


@router.get("/upstreams", response_model=Page[UpstreamOut], operation_id="listUpstreams")
async def list_upstreams(
    authz: Authz,
    session: SessionDep,
    limit: LimitDep = 50,
    cursor: str | None = None,
) -> Page[UpstreamOut]:
    await authz.require(Action.ROUTING_READ)
    stmt = select(Upstream).order_by(Upstream.id)
    rows, next_cursor = await uuid_page(session, stmt, Upstream.id, limit, cursor)
    return Page[UpstreamOut](
        items=[UpstreamOut.model_validate(row) for row in rows], next_cursor=next_cursor
    )


@router.post(
    "/upstreams",
    response_model=UpstreamOut,
    status_code=status.HTTP_201_CREATED,
    operation_id="createUpstream",
)
async def create_upstream(
    body: UpstreamCreate,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> UpstreamOut:
    await authz.require(Action.ROUTING_MANAGE)
    async with mutation(session):
        row = Upstream(
            name=body.name,
            kind=body.kind,
            base_url=body.base_url,
            api_key_ref=body.api_key_ref,
            timeout_ms=body.timeout_ms,
            stream_usage_mode=body.stream_usage_mode,
        )
        session.add(row)
        await session.flush()
        await session.refresh(row)
        audit.record(
            session,
            action="upstream.create",
            resource_type="upstream",
            resource_id=row.id,
            principal=authz.principal,
            details={"name": row.name, "base_url": row.base_url},
        )
        await enqueue_recompile(session, settings, "api.upstream.create")
    return UpstreamOut.model_validate(row)


@router.get("/upstreams/{upstream_id}", response_model=UpstreamOut, operation_id="getUpstream")
async def get_upstream(upstream_id: uuid.UUID, authz: Authz, session: SessionDep) -> UpstreamOut:
    await authz.require(Action.ROUTING_READ)
    row = await session.get(Upstream, upstream_id)
    if row is None:
        raise not_found("upstream not found")
    return UpstreamOut.model_validate(row)


@router.patch("/upstreams/{upstream_id}", response_model=UpstreamOut, operation_id="updateUpstream")
async def update_upstream(
    upstream_id: uuid.UUID,
    body: UpstreamUpdate,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> UpstreamOut:
    await authz.require(Action.ROUTING_MANAGE)
    _non_null(body, "name", "kind", "base_url", "timeout_ms", "stream_usage_mode")
    async with mutation(session):
        row = await session.get(Upstream, upstream_id)
        if row is None:
            raise not_found("upstream not found")
        for name in ("name", "kind", "base_url", "timeout_ms", "stream_usage_mode"):
            if name in body.model_fields_set:
                setattr(row, name, getattr(body, name))
        if "api_key_ref" in body.model_fields_set:
            row.api_key_ref = body.api_key_ref
        await session.flush()
        await session.refresh(row)
        audit.record(
            session,
            action="upstream.update",
            resource_type="upstream",
            resource_id=row.id,
            principal=authz.principal,
            details={"name": row.name, "changes": body.model_dump(exclude_unset=True, mode="json")},
        )
        await enqueue_recompile(session, settings, "api.upstream.update")
    return UpstreamOut.model_validate(row)


@router.delete(
    "/upstreams/{upstream_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="deleteUpstream",
)
async def delete_upstream(
    upstream_id: uuid.UUID,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> Response:
    await authz.require(Action.ROUTING_MANAGE)
    async with mutation(session):
        row = await session.get(Upstream, upstream_id)
        if row is None:
            raise not_found("upstream not found")
        deployment = await session.scalar(
            select(Deployment.id).where(Deployment.upstream_id == row.id).limit(1)
        )
        if deployment is not None:
            raise conflict("upstream still has deployments")
        await session.delete(row)
        audit.record(
            session,
            action="upstream.delete",
            resource_type="upstream",
            resource_id=row.id,
            principal=authz.principal,
            details={"name": row.name},
        )
        await enqueue_recompile(session, settings, "api.upstream.delete")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- deployments


@router.get("/deployments", response_model=Page[DeploymentOut], operation_id="listDeployments")
async def list_deployments(
    authz: Authz,
    session: SessionDep,
    limit: LimitDep = 50,
    cursor: str | None = None,
) -> Page[DeploymentOut]:
    await authz.require(Action.ROUTING_READ)
    stmt = select(Deployment).order_by(Deployment.id)
    rows, next_cursor = await uuid_page(session, stmt, Deployment.id, limit, cursor)
    return Page[DeploymentOut](
        items=[DeploymentOut.model_validate(row) for row in rows],
        next_cursor=next_cursor,
    )


@router.post(
    "/deployments",
    response_model=DeploymentOut,
    status_code=status.HTTP_201_CREATED,
    operation_id="createDeployment",
)
async def create_deployment(
    body: DeploymentCreate,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> DeploymentOut:
    await authz.require(Action.ROUTING_MANAGE)
    async with mutation(session):
        upstream = await session.get(Upstream, body.upstream_id)
        if upstream is None:
            raise validation_error("upstream_id", "unknown upstream")
        row = Deployment(
            name=body.name,
            upstream_id=upstream.id,
            upstream_model=body.upstream_model,
            weight=body.weight,
            price_input_nano_usd_per_mtok=body.price_input_nano_usd_per_mtok,
            price_output_nano_usd_per_mtok=body.price_output_nano_usd_per_mtok,
        )
        session.add(row)
        await session.flush()
        await session.refresh(row)
        audit.record(
            session,
            action="deployment.create",
            resource_type="deployment",
            resource_id=row.id,
            principal=authz.principal,
            details={"name": row.name, "upstream": upstream.name},
        )
        await enqueue_recompile(session, settings, "api.deployment.create")
    return DeploymentOut.model_validate(row)


@router.get(
    "/deployments/{deployment_id}",
    response_model=DeploymentOut,
    operation_id="getDeployment",
)
async def get_deployment(
    deployment_id: uuid.UUID, authz: Authz, session: SessionDep
) -> DeploymentOut:
    await authz.require(Action.ROUTING_READ)
    row = await session.get(Deployment, deployment_id)
    if row is None:
        raise not_found("deployment not found")
    return DeploymentOut.model_validate(row)


@router.patch(
    "/deployments/{deployment_id}",
    response_model=DeploymentOut,
    operation_id="updateDeployment",
)
async def update_deployment(
    deployment_id: uuid.UUID,
    body: DeploymentUpdate,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> DeploymentOut:
    await authz.require(Action.ROUTING_MANAGE)
    _non_null(body, "name", "upstream_id", "upstream_model", "weight")
    async with mutation(session):
        row = await session.get(Deployment, deployment_id)
        if row is None:
            raise not_found("deployment not found")
        if body.upstream_id is not None:
            upstream = await session.get(Upstream, body.upstream_id)
            if upstream is None:
                raise validation_error("upstream_id", "unknown upstream")
            row.upstream_id = upstream.id
        for name in ("name", "upstream_model", "weight"):
            if name in body.model_fields_set:
                setattr(row, name, getattr(body, name))
        price_in = (
            body.price_input_nano_usd_per_mtok
            if "price_input_nano_usd_per_mtok" in body.model_fields_set
            else row.price_input_nano_usd_per_mtok
        )
        price_out = (
            body.price_output_nano_usd_per_mtok
            if "price_output_nano_usd_per_mtok" in body.model_fields_set
            else row.price_output_nano_usd_per_mtok
        )
        if (price_in is None) != (price_out is None):
            raise validation_error(
                "price_input_nano_usd_per_mtok",
                "price input and output must both be set or both be null",
            )
        row.price_input_nano_usd_per_mtok = price_in
        row.price_output_nano_usd_per_mtok = price_out
        await session.flush()
        if "weight" in body.model_fields_set:
            starved = await session.scalar(
                select(Route.model)
                .join(RouteDeployment, RouteDeployment.route_id == Route.id)
                .join(Deployment, Deployment.id == RouteDeployment.deployment_id)
                .where(
                    Route.strategy == RoutingStrategy.WEIGHTED,
                    Route.id.in_(
                        select(RouteDeployment.route_id).where(
                            RouteDeployment.deployment_id == row.id
                        )
                    ),
                )
                .group_by(Route.id, Route.model)
                .having(func.coalesce(func.sum(Deployment.weight), 0) == 0)
                .limit(1)
            )
            if starved is not None:
                raise validation_error(
                    "weight", f"would leave weighted route {starved!r} with no positive weight"
                )
        await session.refresh(row)
        audit.record(
            session,
            action="deployment.update",
            resource_type="deployment",
            resource_id=row.id,
            principal=authz.principal,
            details={"name": row.name, "changes": body.model_dump(exclude_unset=True, mode="json")},
        )
        await enqueue_recompile(session, settings, "api.deployment.update")
    return DeploymentOut.model_validate(row)


@router.delete(
    "/deployments/{deployment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="deleteDeployment",
)
async def delete_deployment(
    deployment_id: uuid.UUID,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> Response:
    await authz.require(Action.ROUTING_MANAGE)
    async with mutation(session):
        row = await session.get(Deployment, deployment_id)
        if row is None:
            raise not_found("deployment not found")
        link = await session.scalar(
            select(RouteDeployment.route_id).where(RouteDeployment.deployment_id == row.id).limit(1)
        )
        if link is not None:
            raise conflict("deployment is still used by a route")
        await session.delete(row)
        audit.record(
            session,
            action="deployment.delete",
            resource_type="deployment",
            resource_id=row.id,
            principal=authz.principal,
            details={"name": row.name},
        )
        await enqueue_recompile(session, settings, "api.deployment.delete")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- routes


@router.get("/routes", response_model=Page[RouteOut], operation_id="listRoutes")
async def list_routes(
    authz: Authz,
    session: SessionDep,
    limit: LimitDep = 50,
    cursor: str | None = None,
) -> Page[RouteOut]:
    await authz.require(Action.ROUTING_READ)
    stmt = select(Route).order_by(Route.id)
    rows, next_cursor = await uuid_page(session, stmt, Route.id, limit, cursor)
    return Page[RouteOut](items=await _routes_out(session, rows), next_cursor=next_cursor)


@router.post(
    "/routes",
    response_model=RouteOut,
    status_code=status.HTTP_201_CREATED,
    operation_id="createRoute",
)
async def create_route(
    body: RouteCreate,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> RouteOut:
    await authz.require(Action.ROUTING_MANAGE)
    _check_strategy(body.strategy)
    async with mutation(session):
        ids = await _deployment_ids(session, body.deployment_ids)
        await _check_positive_weight(session, body.strategy, ids)
        row = Route(model=body.model, strategy=body.strategy)
        session.add(row)
        await session.flush()
        for position, deployment_id in enumerate(ids):
            session.add(
                RouteDeployment(route_id=row.id, deployment_id=deployment_id, position=position)
            )
        await session.flush()
        await session.refresh(row)
        audit.record(
            session,
            action="route.create",
            resource_type="route",
            resource_id=row.id,
            principal=authz.principal,
            details={"model": row.model, "deployments": [str(i) for i in ids]},
        )
        await enqueue_recompile(session, settings, "api.route.create")
    return await _route_out(session, row)


@router.get("/routes/{route_id}", response_model=RouteOut, operation_id="getRoute")
async def get_route(route_id: uuid.UUID, authz: Authz, session: SessionDep) -> RouteOut:
    await authz.require(Action.ROUTING_READ)
    row = await session.get(Route, route_id)
    if row is None:
        raise not_found("route not found")
    return await _route_out(session, row)


@router.patch("/routes/{route_id}", response_model=RouteOut, operation_id="updateRoute")
async def update_route(
    route_id: uuid.UUID,
    body: RouteUpdate,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> RouteOut:
    await authz.require(Action.ROUTING_MANAGE)
    _non_null(body, "model", "strategy", "deployment_ids")
    if body.strategy is not None:
        _check_strategy(body.strategy)
    async with mutation(session):
        row = await session.get(Route, route_id)
        if row is None:
            raise not_found("route not found")
        if body.model is not None:
            row.model = body.model
        if body.strategy is not None:
            row.strategy = body.strategy
        if body.deployment_ids is not None:
            ids = await _deployment_ids(session, body.deployment_ids)
        elif body.strategy is not None:
            ids = list(
                (
                    await session.scalars(
                        select(RouteDeployment.deployment_id).where(
                            RouteDeployment.route_id == row.id
                        )
                    )
                ).all()
            )
        else:
            ids = []
        if ids:
            await _check_positive_weight(session, row.strategy, ids)
        if body.deployment_ids is not None:
            await session.execute(delete(RouteDeployment).where(RouteDeployment.route_id == row.id))
            for position, deployment_id in enumerate(ids):
                session.add(
                    RouteDeployment(route_id=row.id, deployment_id=deployment_id, position=position)
                )
        await session.flush()
        await session.refresh(row)
        audit.record(
            session,
            action="route.update",
            resource_type="route",
            resource_id=row.id,
            principal=authz.principal,
            details={
                "model": row.model,
                "changes": body.model_dump(exclude_unset=True, mode="json"),
            },
        )
        await enqueue_recompile(session, settings, "api.route.update")
    return await _route_out(session, row)


@router.delete(
    "/routes/{route_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="deleteRoute",
)
async def delete_route(
    route_id: uuid.UUID,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> Response:
    await authz.require(Action.ROUTING_MANAGE)
    async with mutation(session):
        row = await session.get(Route, route_id)
        if row is None:
            raise not_found("route not found")
        await session.delete(row)
        audit.record(
            session,
            action="route.delete",
            resource_type="route",
            resource_id=row.id,
            principal=authz.principal,
            details={"model": row.model},
        )
        await enqueue_recompile(session, settings, "api.route.delete")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
