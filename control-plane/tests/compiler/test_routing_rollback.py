"""Rollback must restore source state, not merely copy the old published payload."""

import pytest
from sqlalchemy import delete, select

from lumewarp_control_plane.compiler import compile_scope, publish_pending, rollback_routing
from lumewarp_control_plane.models import (
    Deployment,
    Route,
    RouteDeployment,
    RouteFallback,
    RouteSplit,
    RouteSplitDeployment,
    RoutingStrategy,
    StickyBy,
    StreamUsageMode,
    Upstream,
    UpstreamKind,
)
from tests.compiler.helpers import SCOPE, add_route, load_version, seed_scope, touch

GROUPS = [
    "upstream",
    "health_check",
    "health_defaults",
    "circuit_breaker",
    "breaker_defaults",
    "metrics_url",
    "deployment",
    "price",
    "free_price",
    "strategy",
    "custom_strategy",
    "custom_params",
    "retry",
    "retry_defaults",
    "fallbacks",
    "traffic_split",
    "sticky",
    "sticky_header",
    "members",
]


async def _seed(factory):
    await seed_scope(factory)
    async with factory() as session, session.begin():
        await add_route(session, "public", [("a", None), ("b", None)], upstream="primary")
        await add_route(session, "fallback-one", [("c", None)], upstream="secondary")
        await add_route(session, "fallback-two", [("b", None)], upstream="primary")


async def _set_group(session, group, configured):
    upstream = await session.scalar(select(Upstream).where(Upstream.name == "primary"))
    route = await session.scalar(select(Route).where(Route.name == "public"))
    deployments = {d.name: d for d in await session.scalars(select(Deployment))}
    deployment = deployments["a"]
    if group == "upstream":
        upstream.kind = UpstreamKind.OPENAI_COMPATIBLE if configured else UpstreamKind.VLLM
        upstream.base_url = "https://alternate/v1" if configured else "http://primary/v1"
        upstream.api_key_ref = "TEST_UPSTREAM_KEY" if configured else None
        upstream.timeout_ms = 1234 if configured else 0
        upstream.stream_usage_mode = (
            StreamUsageMode.PASSTHROUGH if configured else StreamUsageMode.INCLUDE_USAGE
        )
    elif group in ("health_check", "health_defaults"):
        explicit = configured and group == "health_check"
        upstream.health_check_interval_ms = 5000 if configured else None
        upstream.health_check_timeout_ms = 1200 if configured else None
        upstream.health_check_path = "/ready" if explicit else None
        upstream.health_check_unhealthy_threshold = 4 if explicit else None
        upstream.health_check_healthy_threshold = 2 if explicit else None
    elif group in ("circuit_breaker", "breaker_defaults"):
        explicit = configured and group == "circuit_breaker"
        upstream.circuit_breaker_enabled = configured
        upstream.circuit_failure_threshold = 7 if explicit else None
        upstream.circuit_open_ms = 12345 if explicit else None
    elif group == "metrics_url":
        upstream.metrics_url = "http://metrics:9000/custom" if configured else None
    elif group == "deployment":
        secondary = await session.scalar(select(Upstream).where(Upstream.name == "secondary"))
        deployment.upstream_id = secondary.id if configured else upstream.id
        deployment.upstream_model = "alternate-name" if configured else "a-name"
        deployment.weight = 7 if configured else 1
    elif group in ("price", "free_price"):
        deployment.price_input_nano_usd_per_mtok = (
            (0 if group == "free_price" else 123) if configured else None
        )
        deployment.price_output_nano_usd_per_mtok = (
            (0 if group == "free_price" else 456) if configured else None
        )
    elif group == "strategy":
        route.strategy = RoutingStrategy.LOWEST_LATENCY if configured else RoutingStrategy.WEIGHTED
    elif group in ("custom_strategy", "custom_params"):
        custom = configured or group == "custom_params"
        route.strategy = RoutingStrategy.CUSTOM if custom else RoutingStrategy.WEIGHTED
        route.custom_strategy = (
            ("header_pin" if configured else "first_healthy") if custom else None
        )
        route.strategy_params = (
            {"headers": ["x-pin"], "pin": "a" if configured else "b"} if custom else None
        )
    elif group in ("retry", "retry_defaults"):
        explicit = configured and group == "retry"
        route.retry_enabled = configured
        route.retry_max_attempts = 4 if explicit else None
        route.retry_on_status = [429, 500, 503] if explicit else []
        route.retry_total_timeout_ms = 1234 if explicit else 0
    elif group == "fallbacks":
        route.retry_enabled = True
        route.retry_max_attempts = 3
        await session.execute(delete(RouteFallback).where(RouteFallback.route_id == route.id))
        if configured:
            targets = {r.name: r for r in await session.scalars(select(Route))}
            for position, name in enumerate(("fallback-two", "fallback-one")):
                session.add(
                    RouteFallback(
                        route_id=route.id,
                        fallback_route_id=targets[name].id,
                        position=position,
                    )
                )
    elif group in ("traffic_split", "sticky", "sticky_header"):
        await session.execute(delete(RouteSplit).where(RouteSplit.route_id == route.id))
        route.sticky = StickyBy.NONE
        route.sticky_header = None
        if configured or group != "traffic_split":
            if group == "traffic_split":
                route.sticky = StickyBy.HEADER
                route.sticky_header = "x-session"
            elif group == "sticky":
                route.sticky = StickyBy.TEAM if configured else StickyBy.KEY
            else:
                route.sticky = StickyBy.HEADER
                route.sticky_header = "x-one" if configured else "x-two"
            for position, (name, weight, members) in enumerate(
                (("canary", 1, ("b", "a")), ("stable", 9, ("a", "b")))
            ):
                split = RouteSplit(route_id=route.id, name=name, weight=weight, position=position)
                session.add(split)
                await session.flush()
                for dep_position, member in enumerate(members):
                    session.add(
                        RouteSplitDeployment(
                            split_id=split.id,
                            deployment_id=deployments[member].id,
                            position=dep_position,
                        )
                    )
    elif group == "members":
        await session.execute(delete(RouteDeployment).where(RouteDeployment.route_id == route.id))
        for position, name in enumerate(("b", "a") if configured else ("a", "b")):
            session.add(
                RouteDeployment(
                    route_id=route.id,
                    deployment_id=deployments[name].id,
                    position=position,
                )
            )
    else:
        raise AssertionError(group)


async def _publish(factory):
    await touch(factory)
    return await publish_pending(factory, SCOPE, require_local_membership=True)


async def _rollback_and_compare(factory):
    before = await load_version(factory, 1)
    changed = await load_version(factory, 2)
    fields = ("upstreams", "deployments", "routes", "required_capabilities")
    assert any(list(getattr(before, f)) != list(getattr(changed, f)) for f in fields)
    async with factory() as session, session.begin():
        assert await rollback_routing(session, SCOPE, 1, require_local_membership=True) == 3
    restored = await load_version(factory, 3)
    async with factory() as session:
        recompiled = (await compile_scope(session, SCOPE, require_local_membership=True)).snapshot
    for field in fields:
        assert list(getattr(restored, field)) == list(getattr(before, field)), field
        assert list(getattr(recompiled, field)) == list(getattr(before, field)), field
    # The next ordinary publication must also retain the restored source routing.
    assert await _publish(factory) is None


@pytest.mark.parametrize("group", GROUPS)
@pytest.mark.parametrize("configured", [False, True], ids=["absent_or_first", "present_or_second"])
async def test_rollback_restores_routing_field_group(factory, group, configured):
    await _seed(factory)
    async with factory() as session, session.begin():
        await _set_group(session, group, configured)
    assert await _publish(factory) == 1
    async with factory() as session, session.begin():
        await _set_group(session, group, not configured)
    assert await _publish(factory) == 2
    await _rollback_and_compare(factory)


async def test_rollback_removes_new_routing_graph_without_restrict_conflicts(factory):
    await _seed(factory)
    assert await _publish(factory) == 1
    async with factory() as session, session.begin():
        late = await add_route(
            session, "late", [("late-deployment", None)], upstream="late-upstream"
        )
        route = await session.scalar(select(Route).where(Route.name == "public"))
        deployment = await session.scalar(
            select(Deployment).where(Deployment.name == "late-deployment")
        )
        route.retry_enabled = True
        route.retry_max_attempts = 2
        session.add(RouteFallback(route_id=route.id, fallback_route_id=late.id, position=0))
        split = RouteSplit(route_id=route.id, name="late", weight=1, position=0)
        session.add(split)
        await session.flush()
        session.add(
            RouteSplitDeployment(split_id=split.id, deployment_id=deployment.id, position=0)
        )
    assert await _publish(factory) == 2
    await _rollback_and_compare(factory)


async def test_rollback_recreates_removed_routes_before_fallback_edges(factory):
    await _seed(factory)
    async with factory() as session, session.begin():
        await _set_group(session, "fallbacks", True)
        await _set_group(session, "traffic_split", True)
        # Targets sort after public in the compiled snapshot and must be recreated
        # before its outgoing edges can satisfy the foreign keys.
        for route in await session.scalars(select(Route).where(Route.name != "public")):
            route.name = f"zz-{route.name}"
    assert await _publish(factory) == 1
    assert (await load_version(factory, 1)).routes[0].name == "public"
    async with factory() as session, session.begin():
        await session.execute(delete(RouteFallback))
        await session.execute(delete(RouteSplit))
        await session.execute(delete(Route).where(Route.name != "public"))
        await session.execute(delete(Deployment).where(Deployment.name == "c"))
        await session.execute(delete(Upstream).where(Upstream.name == "secondary"))
        route = await session.scalar(select(Route))
        route.sticky = StickyBy.NONE
        route.sticky_header = None
        route.retry_enabled = False
        route.retry_max_attempts = None
    assert await _publish(factory) == 2
    await _rollback_and_compare(factory)
