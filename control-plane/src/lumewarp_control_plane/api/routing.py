"""Routing: upstreams, models and public routers.

Router validation mirrors the gateway (snapshot.rs::validate / validate_split and
router.rs::RoutingTable::compile) so a write the API accepts never makes the gateway NACK, plus
one control-plane rule: fallbacks never form a cycle across routers.
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
    HealthCheckIn,
    ModelCreate,
    ModelOut,
    ModelUpdate,
    Page,
    RetryPolicyIn,
    RetryPolicyOut,
    RouterCreate,
    RouterModelOut,
    RouterOut,
    RouterUpdate,
    TrafficSplitIn,
    TrafficSplitOut,
    UpstreamCreate,
    UpstreamOut,
    UpstreamUpdate,
)
from lumewarp_control_plane.authz import Action, Authz
from lumewarp_control_plane.compiler.strategy import strategy_problem
from lumewarp_control_plane.errors import not_found
from lumewarp_control_plane.models import (
    Model,
    Router,
    RouterFallback,
    RouterModel,
    RouterSplit,
    RouterSplitModel,
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


async def _existing_models(session: AsyncSession, ids: list[uuid.UUID], field_name: str) -> None:
    if not ids:
        return
    found = set((await session.scalars(select(Model.id).where(Model.id.in_(ids)))).all())
    missing = [str(item) for item in ids if item not in found]
    if missing:
        raise validation_error(field_name, f"unknown models: {', '.join(missing)}")


async def _total_weight(session: AsyncSession, ids: list[uuid.UUID]) -> int:
    return int(
        await session.scalar(
            select(func.coalesce(func.sum(Model.weight), 0)).where(Model.id.in_(ids))
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


# --------------------------------------------------------------------------- router state


@dataclass
class RouterSpec:
    """The full desired state of a router (create body, or stored state + PATCH)."""

    name: str
    strategy: RoutingStrategy
    model_ids: list[uuid.UUID]
    custom_strategy: str | None = None
    strategy_params: dict = field(default_factory=dict)
    retry: RetryPolicyIn | None = None
    fallback_router_ids: list[uuid.UUID] = field(default_factory=list)
    traffic_split: list[TrafficSplitIn] = field(default_factory=list)
    sticky: StickyBy = StickyBy.NONE
    sticky_header: str | None = None


async def _load_spec(session: AsyncSession, row: Router) -> RouterSpec:
    out = (await _routers_out(session, [row]))[0]
    return RouterSpec(
        name=row.name,
        strategy=out.strategy,
        custom_strategy=out.custom_strategy,
        strategy_params=out.strategy_params,
        model_ids=[d.model_id for d in out.models],
        retry=RetryPolicyIn(**out.retry.model_dump()) if out.retry else None,
        fallback_router_ids=list(out.fallback_router_ids),
        traffic_split=[TrafficSplitIn(**s.model_dump()) for s in out.traffic_split],
        sticky=row.sticky,
        sticky_header=row.sticky_header,
    )


async def _fallback_cycle(
    session: AsyncSession, router_id: uuid.UUID, fallbacks: list[uuid.UUID]
) -> list[uuid.UUID] | None:
    """A path fallback -> ... -> router_id over the other routers' fallbacks, or None.

    The graph was acyclic before this write and only `router_id`'s edges change, so the
    new edges close a cycle iff one of the fallbacks reaches `router_id`.
    """
    graph: dict[uuid.UUID, list[uuid.UUID]] = {}
    for edge in await session.scalars(
        select(RouterFallback)
        .where(RouterFallback.router_id != router_id)
        .order_by(RouterFallback.router_id, RouterFallback.position)
    ):
        graph.setdefault(edge.router_id, []).append(edge.fallback_router_id)
    for start in fallbacks:
        stack = [(start, [start])]
        seen: set[uuid.UUID] = set()
        while stack:
            node, path = stack.pop()
            if node == router_id:
                return path
            if node in seen:
                continue
            seen.add(node)
            stack.extend((nxt, [*path, nxt]) for nxt in graph.get(node, ()))
    return None


async def _validate_spec(
    session: AsyncSession, spec: RouterSpec, router_id: uuid.UUID | None
) -> None:
    """422 for anything the gateway would reject, plus fallback cycles."""
    if problem := strategy_problem(spec.strategy, spec.custom_strategy, spec.strategy_params):
        raise validation_error("strategy", problem)
    ensure_unique_ids(spec.model_ids)
    await _existing_models(session, spec.model_ids, "model_ids")
    for split in spec.traffic_split:
        await _existing_models(session, split.model_ids, "traffic_split")
    if not spec.traffic_split and not spec.model_ids:
        raise validation_error("model_ids", "pick at least one model or define a traffic_split")
    # Sticky (snapshot.rs::validate_split).
    if spec.sticky is StickyBy.HEADER and not spec.sticky_header:
        raise validation_error("sticky_header", "required when sticky is 'header'")
    if spec.sticky is not StickyBy.HEADER and spec.sticky_header:
        raise validation_error("sticky_header", "only used when sticky is 'header'")
    if spec.sticky is not StickyBy.NONE and not spec.traffic_split:
        raise validation_error("sticky", "sticky needs a traffic_split")
    # A weighted pool whose models all have weight 0 makes the gateway NACK.
    if spec.strategy is RoutingStrategy.WEIGHTED:
        if spec.traffic_split:
            for split in spec.traffic_split:
                if not await _total_weight(session, split.model_ids):
                    raise validation_error(
                        "traffic_split",
                        f"split {split.name!r} of a weighted router needs models with"
                        " a positive total weight",
                    )
        elif not await _total_weight(session, spec.model_ids):
            raise validation_error(
                "model_ids", "a weighted router needs models with a positive total weight"
            )
    # Fallbacks: known routers, no self reference, no cycle.
    ensure_unique_ids(spec.fallback_router_ids, "fallback_router_ids")
    if router_id is not None and router_id in spec.fallback_router_ids:
        raise validation_error("fallback_router_ids", "a router cannot fall back to itself")
    # Fallbacks spend the router's attempt budget: with a single attempt they never run.
    if spec.fallback_router_ids and (spec.retry is None or spec.retry.max_attempts == 1):
        raise validation_error(
            "fallback_router_ids", "fallbacks need a retry policy with at least 2 attempts"
        )
    if spec.fallback_router_ids:
        found = dict(
            (
                await session.execute(
                    select(Router.id, Router.name).where(Router.id.in_(spec.fallback_router_ids))
                )
            ).all()
        )
        missing = [str(i) for i in spec.fallback_router_ids if i not in found]
        if missing:
            raise validation_error("fallback_router_ids", f"unknown routers: {', '.join(missing)}")
        if router_id is not None:
            cycle = await _fallback_cycle(session, router_id, spec.fallback_router_ids)
            if cycle is not None:
                names = [spec.name]
                for node in cycle:
                    names.append(found.get(node) or await _name_of(session, node))
                raise validation_error(
                    "fallback_router_ids", f"fallbacks form a cycle: {' -> '.join(names)}"
                )


async def _name_of(session: AsyncSession, router_id: uuid.UUID) -> str:
    return await session.scalar(select(Router.name).where(Router.id == router_id)) or str(router_id)


async def _write_children(
    session: AsyncSession, row: Router, spec: RouterSpec, fields: set[str]
) -> None:
    """Replace the child rows named in `fields` with `spec`'s lists."""
    if "model_ids" in fields:
        await session.execute(delete(RouterModel).where(RouterModel.router_id == row.id))
        for position, model_id in enumerate(spec.model_ids):
            session.add(RouterModel(router_id=row.id, model_id=model_id, position=position))
    if "fallback_router_ids" in fields:
        await session.execute(delete(RouterFallback).where(RouterFallback.router_id == row.id))
        for position, fallback in enumerate(spec.fallback_router_ids):
            session.add(
                RouterFallback(router_id=row.id, fallback_router_id=fallback, position=position)
            )
    if "traffic_split" in fields:
        await session.execute(delete(RouterSplit).where(RouterSplit.router_id == row.id))
        await session.flush()
        for position, split in enumerate(spec.traffic_split):
            split_row = RouterSplit(
                router_id=row.id, name=split.name, weight=split.weight, position=position
            )
            session.add(split_row)
            await session.flush()
            for dep_position, model_id in enumerate(split.model_ids):
                session.add(
                    RouterSplitModel(
                        split_id=split_row.id, model_id=model_id, position=dep_position
                    )
                )


def _apply_scalars(row: Router, spec: RouterSpec) -> None:
    row.name = spec.name
    row.strategy = spec.strategy
    row.custom_strategy = spec.custom_strategy
    row.strategy_params = spec.strategy_params if spec.strategy is RoutingStrategy.CUSTOM else None
    row.retry_enabled = spec.retry is not None
    row.retry_max_attempts = spec.retry.max_attempts if spec.retry else None
    row.retry_on_status = list(spec.retry.retry_on_status) if spec.retry else []
    row.retry_total_timeout_ms = spec.retry.total_timeout_ms if spec.retry else 0
    row.sticky = spec.sticky
    row.sticky_header = spec.sticky_header


async def _routers_out(session: AsyncSession, rows: list[Router]) -> list[RouterOut]:
    ids = [row.id for row in rows]
    links: dict[uuid.UUID, list[RouterModelOut]] = {i: [] for i in ids}
    fallbacks: dict[uuid.UUID, list[uuid.UUID]] = {i: [] for i in ids}
    splits: dict[uuid.UUID, list[TrafficSplitOut]] = {i: [] for i in ids}
    if rows:
        for link in await session.scalars(
            select(RouterModel)
            .where(RouterModel.router_id.in_(ids))
            .order_by(RouterModel.router_id, RouterModel.position)
        ):
            links[link.router_id].append(
                RouterModelOut(model_id=link.model_id, position=link.position)
            )
        for edge in await session.scalars(
            select(RouterFallback)
            .where(RouterFallback.router_id.in_(ids))
            .order_by(RouterFallback.router_id, RouterFallback.position)
        ):
            fallbacks[edge.router_id].append(edge.fallback_router_id)
        split_rows = (
            await session.scalars(
                select(RouterSplit)
                .where(RouterSplit.router_id.in_(ids))
                .order_by(RouterSplit.router_id, RouterSplit.position)
            )
        ).all()
        members: dict[uuid.UUID, list[uuid.UUID]] = {s.id: [] for s in split_rows}
        if split_rows:
            for member in await session.scalars(
                select(RouterSplitModel)
                .where(RouterSplitModel.split_id.in_(members))
                .order_by(RouterSplitModel.split_id, RouterSplitModel.position)
            ):
                members[member.split_id].append(member.model_id)
        for s in split_rows:
            splits[s.router_id].append(
                TrafficSplitOut(name=s.name, weight=s.weight, model_ids=members[s.id])
            )
    return [
        RouterOut(
            id=row.id,
            name=row.name,
            strategy=row.strategy,
            custom_strategy=row.custom_strategy,
            strategy_params=row.strategy_params or {},
            models=links[row.id],
            retry=RetryPolicyOut(
                max_attempts=row.retry_max_attempts,
                retry_on_status=list(row.retry_on_status),
                total_timeout_ms=row.retry_total_timeout_ms,
            )
            if row.retry_enabled
            else None,
            fallback_router_ids=fallbacks[row.id],
            traffic_split=splits[row.id],
            sticky=row.sticky,
            sticky_header=row.sticky_header,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
        for row in rows
    ]


async def _router_out(session: AsyncSession, row: Router) -> RouterOut:
    return (await _routers_out(session, [row]))[0]


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
        model = await session.scalar(select(Model.id).where(Model.upstream_id == row.id).limit(1))
        if model is not None:
            raise conflict("upstream still has models")
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


# --------------------------------------------------------------------------- models


@router.get("/models", response_model=Page[ModelOut], operation_id="listModels")
async def list_models(
    authz: Authz,
    session: SessionDep,
    limit: LimitDep = 50,
    cursor: str | None = None,
) -> Page[ModelOut]:
    await authz.require(Action.ROUTING_READ)
    stmt = select(Model).order_by(Model.id)
    rows, next_cursor = await uuid_page(session, stmt, Model.id, limit, cursor)
    return Page[ModelOut](
        items=[ModelOut.model_validate(row) for row in rows],
        next_cursor=next_cursor,
    )


@router.post(
    "/models",
    response_model=ModelOut,
    status_code=status.HTTP_201_CREATED,
    operation_id="createModel",
)
async def create_model(
    body: ModelCreate,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> ModelOut:
    await authz.require(Action.ROUTING_MANAGE)
    async with mutation(session):
        upstream = await session.get(Upstream, body.upstream_id)
        if upstream is None:
            raise validation_error("upstream_id", "unknown upstream")
        row = Model(
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
            action="model.create",
            resource_type="model",
            resource_id=row.id,
            principal=authz.principal,
            details={"name": row.name, "upstream": upstream.name},
        )
        await enqueue_recompile(session, settings, "api.model.create")
    return ModelOut.model_validate(row)


@router.get(
    "/models/{model_id}",
    response_model=ModelOut,
    operation_id="getModel",
)
async def get_model(model_id: uuid.UUID, authz: Authz, session: SessionDep) -> ModelOut:
    await authz.require(Action.ROUTING_READ)
    row = await session.get(Model, model_id)
    if row is None:
        raise not_found("model not found")
    return ModelOut.model_validate(row)


@router.patch(
    "/models/{model_id}",
    response_model=ModelOut,
    operation_id="updateModel",
)
async def update_model(
    model_id: uuid.UUID,
    body: ModelUpdate,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> ModelOut:
    await authz.require(Action.ROUTING_MANAGE)
    _non_null(body, "name", "upstream_id", "upstream_model", "weight")
    async with mutation(session):
        await lock_scope(session, settings)
        row = await session.get(Model, model_id)
        if row is None:
            raise not_found("model not found")
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
                select(Router.name)
                .join(RouterModel, RouterModel.router_id == Router.id)
                .join(Model, Model.id == RouterModel.model_id)
                .where(
                    Router.strategy == RoutingStrategy.WEIGHTED,
                    Router.id.in_(
                        select(RouterModel.router_id).where(RouterModel.model_id == row.id)
                    ),
                )
                .group_by(Router.id, Router.name)
                .having(func.coalesce(func.sum(Model.weight), 0) == 0)
                .limit(1)
            )
            if starved is None:
                starved = await session.scalar(
                    select(Router.name)
                    .join(RouterSplit, RouterSplit.router_id == Router.id)
                    .join(RouterSplitModel, RouterSplitModel.split_id == RouterSplit.id)
                    .join(Model, Model.id == RouterSplitModel.model_id)
                    .where(
                        Router.strategy == RoutingStrategy.WEIGHTED,
                        RouterSplit.id.in_(
                            select(RouterSplitModel.split_id).where(
                                RouterSplitModel.model_id == row.id
                            )
                        ),
                    )
                    .group_by(RouterSplit.id, Router.name)
                    .having(func.coalesce(func.sum(Model.weight), 0) == 0)
                    .limit(1)
                )
            if starved is not None:
                raise validation_error(
                    "weight", f"would leave weighted router {starved!r} with no positive weight"
                )
        await session.refresh(row)
        audit.record(
            session,
            action="model.update",
            resource_type="model",
            resource_id=row.id,
            principal=authz.principal,
            details={"name": row.name, "changes": body.model_dump(exclude_unset=True, mode="json")},
        )
        await enqueue_recompile(session, settings, "api.model.update")
    return ModelOut.model_validate(row)


@router.delete(
    "/models/{model_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="deleteModel",
)
async def delete_model(
    model_id: uuid.UUID,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> Response:
    await authz.require(Action.ROUTING_MANAGE)
    async with mutation(session):
        row = await session.get(Model, model_id)
        if row is None:
            raise not_found("model not found")
        link = await session.scalar(
            select(RouterModel.router_id).where(RouterModel.model_id == row.id).limit(1)
        )
        if link is None:
            link = await session.scalar(
                select(RouterSplitModel.split_id)
                .where(RouterSplitModel.model_id == row.id)
                .limit(1)
            )
        if link is not None:
            raise conflict("model is still used by a router")
        await session.delete(row)
        audit.record(
            session,
            action="model.delete",
            resource_type="model",
            resource_id=row.id,
            principal=authz.principal,
            details={"name": row.name},
        )
        await enqueue_recompile(session, settings, "api.model.delete")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- routers


@router.get("/routers", response_model=Page[RouterOut], operation_id="listRouters")
async def list_routers(
    authz: Authz,
    session: SessionDep,
    limit: LimitDep = 50,
    cursor: str | None = None,
) -> Page[RouterOut]:
    await authz.require(Action.ROUTING_READ)
    stmt = select(Router).order_by(Router.id)
    rows, next_cursor = await uuid_page(session, stmt, Router.id, limit, cursor)
    return Page[RouterOut](items=await _routers_out(session, rows), next_cursor=next_cursor)


@router.post(
    "/routers",
    response_model=RouterOut,
    status_code=status.HTTP_201_CREATED,
    operation_id="createRouter",
)
async def create_router(
    body: RouterCreate,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> RouterOut:
    await authz.require(Action.ROUTING_MANAGE)
    spec = RouterSpec(
        name=body.name,
        strategy=body.strategy,
        custom_strategy=body.custom_strategy,
        strategy_params=body.strategy_params,
        model_ids=list(body.model_ids),
        retry=body.retry,
        fallback_router_ids=list(body.fallback_router_ids),
        traffic_split=list(body.traffic_split),
        sticky=body.sticky,
        sticky_header=body.sticky_header,
    )
    async with mutation(session):
        # Serializes routing writes so concurrent fallback edits cannot form a cycle.
        await lock_scope(session, settings)
        # A new router has no incoming fallbacks, so it cannot close a cycle.
        await _validate_spec(session, spec, None)
        row = Router(name=spec.name)
        _apply_scalars(row, spec)
        session.add(row)
        await session.flush()
        await _write_children(
            session,
            row,
            spec,
            {"model_ids", "fallback_router_ids", "traffic_split", "strategy"},
        )
        await session.flush()
        await session.refresh(row)
        audit.record(
            session,
            action="router.create",
            resource_type="router",
            resource_id=row.id,
            principal=authz.principal,
            details={
                "name": row.name,
                "models": [str(i) for i in spec.model_ids],
                "settings": body.model_dump(
                    mode="json",
                    exclude={"name", "model_ids", "strategy_params"},
                    exclude_defaults=True,
                ),
            },
        )
        await enqueue_recompile(session, settings, "api.router.create")
    return await _router_out(session, row)


@router.get("/routers/{router_id}", response_model=RouterOut, operation_id="getRouter")
async def get_router(router_id: uuid.UUID, authz: Authz, session: SessionDep) -> RouterOut:
    await authz.require(Action.ROUTING_READ)
    row = await session.get(Router, router_id)
    if row is None:
        raise not_found("router not found")
    return await _router_out(session, row)


@router.patch("/routers/{router_id}", response_model=RouterOut, operation_id="updateRouter")
async def update_router(
    router_id: uuid.UUID,
    body: RouterUpdate,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> RouterOut:
    await authz.require(Action.ROUTING_MANAGE)
    _non_null(
        body,
        "name",
        "strategy",
        "model_ids",
        "fallback_router_ids",
        "traffic_split",
        "sticky",
        "strategy_params",
    )
    fields = body.model_fields_set
    async with mutation(session):
        await lock_scope(session, settings)
        row = await session.get(Router, router_id)
        if row is None:
            raise not_found("router not found")
        spec = await _load_spec(session, row)
        for name in fields:
            setattr(spec, name, getattr(body, name))
        if spec.strategy is not RoutingStrategy.CUSTOM and "strategy" in fields:
            if "custom_strategy" not in fields:
                spec.custom_strategy = None
            if "strategy_params" not in fields:
                spec.strategy_params = {}
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
            action="router.update",
            resource_type="router",
            resource_id=row.id,
            principal=authz.principal,
            details={
                "name": row.name,
                "changes": body.model_dump(
                    exclude_unset=True, mode="json", exclude={"strategy_params"}
                ),
            },
        )
        await enqueue_recompile(session, settings, "api.router.update")
    return await _router_out(session, row)


@router.delete(
    "/routers/{router_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="deleteRouter",
)
async def delete_router(
    router_id: uuid.UUID,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> Response:
    await authz.require(Action.ROUTING_MANAGE)
    async with mutation(session):
        await lock_scope(session, settings)
        row = await session.get(Router, router_id)
        if row is None:
            raise not_found("router not found")
        user = await session.scalar(
            select(Router.name)
            .join(RouterFallback, RouterFallback.router_id == Router.id)
            .where(RouterFallback.fallback_router_id == row.id)
            .order_by(Router.name)
            .limit(1)
        )
        if user is not None:
            raise conflict(f"router is a fallback of router {user!r}")
        await session.delete(row)
        audit.record(
            session,
            action="router.delete",
            resource_type="router",
            resource_id=row.id,
            principal=authz.principal,
            details={"name": row.name},
        )
        await enqueue_recompile(session, settings, "api.router.delete")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
