"""Routing: upstreams, deployments and public model routes.

Route validation mirrors the gateway (snapshot.rs::validate / validate_split and
router.rs::Router::compile) so a write the API accepts never makes the gateway NACK, plus
one control-plane rule: fallbacks never form a cycle across routes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

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
    lock_scope,
    mutation,
    uuid_page,
    validation_error,
)
from lumewarp_control_plane.api.policy import ensure_unique_ids
from lumewarp_control_plane.api.schemas import (
    CircuitBreakerIn,
    DeploymentCreate,
    DeploymentOut,
    DeploymentUpdate,
    HealthCheckIn,
    Page,
    RetryPolicyIn,
    RetryPolicyOut,
    RouteCreate,
    RouteDeploymentOut,
    RouteOut,
    RouteUpdate,
    TrafficSplitIn,
    TrafficSplitOut,
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
    RouteFallback,
    RouteSplit,
    RouteSplitDeployment,
    RoutingStrategy,
    StickyBy,
    Upstream,
)

router = APIRouter(tags=["routing"])


def _non_null(body: object, *fields: str) -> None:
    supplied = body.model_fields_set  # type: ignore[attr-defined]
    for name in fields:
        if name in supplied and getattr(body, name) is None:
            raise validation_error(name, "cannot be null")


async def _existing_deployments(
    session: AsyncSession, ids: list[uuid.UUID], field_name: str
) -> None:
    if not ids:
        return
    found = set((await session.scalars(select(Deployment.id).where(Deployment.id.in_(ids)))).all())
    missing = [str(item) for item in ids if item not in found]
    if missing:
        raise validation_error(field_name, f"unknown deployments: {', '.join(missing)}")


async def _total_weight(session: AsyncSession, ids: list[uuid.UUID]) -> int:
    return int(
        await session.scalar(
            select(func.coalesce(func.sum(Deployment.weight), 0)).where(Deployment.id.in_(ids))
        )
        or 0
    )


def _apply_health(row: Upstream, health: HealthCheckIn | None) -> None:
    row.health_check_path = health.path if health else None
    row.health_check_interval_ms = health.interval_ms if health else None
    row.health_check_timeout_ms = health.timeout_ms if health else None
    row.health_check_unhealthy_threshold = health.unhealthy_threshold if health else None
    row.health_check_healthy_threshold = health.healthy_threshold if health else None


def _apply_breaker(row: Upstream, breaker: CircuitBreakerIn | None) -> None:
    row.circuit_breaker_enabled = breaker is not None
    row.circuit_failure_threshold = breaker.failure_threshold if breaker else None
    row.circuit_open_ms = breaker.open_ms if breaker else None


# --------------------------------------------------------------------------- route state


@dataclass
class RouteSpec:
    """The full desired state of a route (create body, or stored state + PATCH)."""

    model: str
    strategy: RoutingStrategy
    deployment_ids: list[uuid.UUID]
    retry: RetryPolicyIn | None = None
    fallback_route_ids: list[uuid.UUID] = field(default_factory=list)
    traffic_split: list[TrafficSplitIn] = field(default_factory=list)
    sticky: StickyBy = StickyBy.NONE
    sticky_header: str | None = None


async def _load_spec(session: AsyncSession, row: Route) -> RouteSpec:
    out = (await _routes_out(session, [row]))[0]
    return RouteSpec(
        model=row.model,
        strategy=row.strategy,
        deployment_ids=[d.deployment_id for d in out.deployments],
        retry=RetryPolicyIn(**out.retry.model_dump()) if out.retry else None,
        fallback_route_ids=list(out.fallback_route_ids),
        traffic_split=[TrafficSplitIn(**s.model_dump()) for s in out.traffic_split],
        sticky=row.sticky,
        sticky_header=row.sticky_header,
    )


async def _fallback_cycle(
    session: AsyncSession, route_id: uuid.UUID, fallbacks: list[uuid.UUID]
) -> list[uuid.UUID] | None:
    """A path fallback -> ... -> route_id over the other routes' fallbacks, or None.

    The graph was acyclic before this write and only `route_id`'s edges change, so the
    new edges close a cycle iff one of the fallbacks reaches `route_id`.
    """
    graph: dict[uuid.UUID, list[uuid.UUID]] = {}
    for edge in await session.scalars(
        select(RouteFallback)
        .where(RouteFallback.route_id != route_id)
        .order_by(RouteFallback.route_id, RouteFallback.position)
    ):
        graph.setdefault(edge.route_id, []).append(edge.fallback_route_id)
    for start in fallbacks:
        stack = [(start, [start])]
        seen: set[uuid.UUID] = set()
        while stack:
            node, path = stack.pop()
            if node == route_id:
                return path
            if node in seen:
                continue
            seen.add(node)
            stack.extend((nxt, [*path, nxt]) for nxt in graph.get(node, ()))
    return None


async def _validate_spec(
    session: AsyncSession, spec: RouteSpec, route_id: uuid.UUID | None
) -> None:
    """422 for anything the gateway would reject, plus fallback cycles."""
    ensure_unique_ids(spec.deployment_ids)
    await _existing_deployments(session, spec.deployment_ids, "deployment_ids")
    for split in spec.traffic_split:
        await _existing_deployments(session, split.deployment_ids, "traffic_split")
    if not spec.traffic_split and not spec.deployment_ids:
        raise validation_error(
            "deployment_ids", "pick at least one deployment or define a traffic_split"
        )
    # Sticky (snapshot.rs::validate_split).
    if spec.sticky is StickyBy.HEADER and not spec.sticky_header:
        raise validation_error("sticky_header", "required when sticky is 'header'")
    if spec.sticky is not StickyBy.HEADER and spec.sticky_header:
        raise validation_error("sticky_header", "only used when sticky is 'header'")
    if spec.sticky is not StickyBy.NONE and not spec.traffic_split:
        raise validation_error("sticky", "sticky needs a traffic_split")
    # A weighted pool whose deployments all have weight 0 makes the gateway NACK.
    if spec.strategy is RoutingStrategy.WEIGHTED:
        if spec.traffic_split:
            for split in spec.traffic_split:
                if not await _total_weight(session, split.deployment_ids):
                    raise validation_error(
                        "traffic_split",
                        f"split {split.name!r} of a weighted route needs deployments with"
                        " a positive total weight",
                    )
        elif not await _total_weight(session, spec.deployment_ids):
            raise validation_error(
                "deployment_ids", "a weighted route needs deployments with a positive total weight"
            )
    # Fallbacks: known routes, no self reference, no cycle.
    ensure_unique_ids(spec.fallback_route_ids, "fallback_route_ids")
    if route_id is not None and route_id in spec.fallback_route_ids:
        raise validation_error("fallback_route_ids", "a route cannot fall back to itself")
    # Fallbacks spend the route's attempt budget: with a single attempt they never run.
    if spec.fallback_route_ids and (spec.retry is None or spec.retry.max_attempts == 1):
        raise validation_error(
            "fallback_route_ids", "fallbacks need a retry policy with at least 2 attempts"
        )
    if spec.fallback_route_ids:
        found = dict(
            (
                await session.execute(
                    select(Route.id, Route.model).where(Route.id.in_(spec.fallback_route_ids))
                )
            ).all()
        )
        missing = [str(i) for i in spec.fallback_route_ids if i not in found]
        if missing:
            raise validation_error("fallback_route_ids", f"unknown routes: {', '.join(missing)}")
        if route_id is not None:
            cycle = await _fallback_cycle(session, route_id, spec.fallback_route_ids)
            if cycle is not None:
                names = [spec.model]
                for node in cycle:
                    names.append(found.get(node) or await _model_of(session, node))
                raise validation_error(
                    "fallback_route_ids", f"fallbacks form a cycle: {' -> '.join(names)}"
                )


async def _model_of(session: AsyncSession, route_id: uuid.UUID) -> str:
    return await session.scalar(select(Route.model).where(Route.id == route_id)) or str(route_id)


async def _write_children(
    session: AsyncSession, row: Route, spec: RouteSpec, fields: set[str]
) -> None:
    """Replace the child rows named in `fields` with `spec`'s lists."""
    if "deployment_ids" in fields:
        await session.execute(delete(RouteDeployment).where(RouteDeployment.route_id == row.id))
        for position, deployment_id in enumerate(spec.deployment_ids):
            session.add(
                RouteDeployment(route_id=row.id, deployment_id=deployment_id, position=position)
            )
    if "fallback_route_ids" in fields:
        await session.execute(delete(RouteFallback).where(RouteFallback.route_id == row.id))
        for position, fallback in enumerate(spec.fallback_route_ids):
            session.add(
                RouteFallback(route_id=row.id, fallback_route_id=fallback, position=position)
            )
    if "traffic_split" in fields:
        await session.execute(delete(RouteSplit).where(RouteSplit.route_id == row.id))
        await session.flush()
        for position, split in enumerate(spec.traffic_split):
            split_row = RouteSplit(
                route_id=row.id, name=split.name, weight=split.weight, position=position
            )
            session.add(split_row)
            await session.flush()
            for dep_position, deployment_id in enumerate(split.deployment_ids):
                session.add(
                    RouteSplitDeployment(
                        split_id=split_row.id, deployment_id=deployment_id, position=dep_position
                    )
                )


def _apply_scalars(row: Route, spec: RouteSpec) -> None:
    row.model = spec.model
    row.strategy = spec.strategy
    row.retry_enabled = spec.retry is not None
    row.retry_max_attempts = spec.retry.max_attempts if spec.retry else None
    row.retry_on_status = list(spec.retry.retry_on_status) if spec.retry else []
    row.retry_total_timeout_ms = spec.retry.total_timeout_ms if spec.retry else 0
    row.sticky = spec.sticky
    row.sticky_header = spec.sticky_header


async def _routes_out(session: AsyncSession, rows: list[Route]) -> list[RouteOut]:
    ids = [row.id for row in rows]
    links: dict[uuid.UUID, list[RouteDeploymentOut]] = {i: [] for i in ids}
    fallbacks: dict[uuid.UUID, list[uuid.UUID]] = {i: [] for i in ids}
    splits: dict[uuid.UUID, list[TrafficSplitOut]] = {i: [] for i in ids}
    if rows:
        for link in await session.scalars(
            select(RouteDeployment)
            .where(RouteDeployment.route_id.in_(ids))
            .order_by(RouteDeployment.route_id, RouteDeployment.position)
        ):
            links[link.route_id].append(
                RouteDeploymentOut(deployment_id=link.deployment_id, position=link.position)
            )
        for edge in await session.scalars(
            select(RouteFallback)
            .where(RouteFallback.route_id.in_(ids))
            .order_by(RouteFallback.route_id, RouteFallback.position)
        ):
            fallbacks[edge.route_id].append(edge.fallback_route_id)
        split_rows = (
            await session.scalars(
                select(RouteSplit)
                .where(RouteSplit.route_id.in_(ids))
                .order_by(RouteSplit.route_id, RouteSplit.position)
            )
        ).all()
        members: dict[uuid.UUID, list[uuid.UUID]] = {s.id: [] for s in split_rows}
        if split_rows:
            for member in await session.scalars(
                select(RouteSplitDeployment)
                .where(RouteSplitDeployment.split_id.in_(members))
                .order_by(RouteSplitDeployment.split_id, RouteSplitDeployment.position)
            ):
                members[member.split_id].append(member.deployment_id)
        for s in split_rows:
            splits[s.route_id].append(
                TrafficSplitOut(name=s.name, weight=s.weight, deployment_ids=members[s.id])
            )
    return [
        RouteOut(
            id=row.id,
            model=row.model,
            strategy=row.strategy,
            deployments=links[row.id],
            retry=RetryPolicyOut(
                max_attempts=row.retry_max_attempts,
                retry_on_status=list(row.retry_on_status),
                total_timeout_ms=row.retry_total_timeout_ms,
            )
            if row.retry_enabled
            else None,
            fallback_route_ids=fallbacks[row.id],
            traffic_split=splits[row.id],
            sticky=row.sticky,
            sticky_header=row.sticky_header,
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
            metrics_url=body.metrics_url,
        )
        _apply_health(row, body.health_check)
        _apply_breaker(row, body.circuit_breaker)
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
        if "metrics_url" in body.model_fields_set:
            row.metrics_url = body.metrics_url
        if "health_check" in body.model_fields_set:
            _apply_health(row, body.health_check)
        if "circuit_breaker" in body.model_fields_set:
            _apply_breaker(row, body.circuit_breaker)
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
        await lock_scope(session, settings)
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
            if starved is None:
                starved = await session.scalar(
                    select(Route.model)
                    .join(RouteSplit, RouteSplit.route_id == Route.id)
                    .join(RouteSplitDeployment, RouteSplitDeployment.split_id == RouteSplit.id)
                    .join(Deployment, Deployment.id == RouteSplitDeployment.deployment_id)
                    .where(
                        Route.strategy == RoutingStrategy.WEIGHTED,
                        RouteSplit.id.in_(
                            select(RouteSplitDeployment.split_id).where(
                                RouteSplitDeployment.deployment_id == row.id
                            )
                        ),
                    )
                    .group_by(RouteSplit.id, Route.model)
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
        if link is None:
            link = await session.scalar(
                select(RouteSplitDeployment.split_id)
                .where(RouteSplitDeployment.deployment_id == row.id)
                .limit(1)
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
    spec = RouteSpec(
        model=body.model,
        strategy=body.strategy,
        deployment_ids=list(body.deployment_ids),
        retry=body.retry,
        fallback_route_ids=list(body.fallback_route_ids),
        traffic_split=list(body.traffic_split),
        sticky=body.sticky,
        sticky_header=body.sticky_header,
    )
    async with mutation(session):
        # Serializes routing writes so concurrent fallback edits cannot form a cycle.
        await lock_scope(session, settings)
        # A new route has no incoming fallbacks, so it cannot close a cycle.
        await _validate_spec(session, spec, None)
        row = Route(model=spec.model, strategy=spec.strategy)
        _apply_scalars(row, spec)
        session.add(row)
        await session.flush()
        await _write_children(
            session, row, spec, {"deployment_ids", "fallback_route_ids", "traffic_split"}
        )
        await session.flush()
        await session.refresh(row)
        audit.record(
            session,
            action="route.create",
            resource_type="route",
            resource_id=row.id,
            principal=authz.principal,
            details={
                "model": row.model,
                "deployments": [str(i) for i in spec.deployment_ids],
                "settings": body.model_dump(
                    mode="json", exclude={"model", "deployment_ids"}, exclude_defaults=True
                ),
            },
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
    _non_null(
        body, "model", "strategy", "deployment_ids", "fallback_route_ids", "traffic_split", "sticky"
    )
    fields = body.model_fields_set
    async with mutation(session):
        await lock_scope(session, settings)
        row = await session.get(Route, route_id)
        if row is None:
            raise not_found("route not found")
        spec = await _load_spec(session, row)
        for name in fields:
            setattr(spec, name, getattr(body, name))
        # Leaving header stickiness drops the header unless the body names one.
        if spec.sticky is not StickyBy.HEADER and "sticky_header" not in fields:
            spec.sticky_header = None
        await _validate_spec(session, spec, row.id)
        _apply_scalars(row, spec)
        await _write_children(session, row, spec, fields)
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
        await lock_scope(session, settings)
        row = await session.get(Route, route_id)
        if row is None:
            raise not_found("route not found")
        user = await session.scalar(
            select(Route.model)
            .join(RouteFallback, RouteFallback.route_id == Route.id)
            .where(RouteFallback.fallback_route_id == row.id)
            .order_by(Route.model)
            .limit(1)
        )
        if user is not None:
            raise conflict(f"route is a fallback of route {user!r}")
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
