import json
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from lumewarp_control_plane import migrate
from lumewarp_control_plane._proto.lumewarp.config.v1 import snapshot_pb2 as pb
from lumewarp_control_plane.compiler import (
    compile_scope,
    publish_pending,
    rollback_routing,
)
from lumewarp_control_plane.compiler.strategy import strategy_problem
from lumewarp_control_plane.compiler.validate import SnapshotInvalid, validate_snapshot
from lumewarp_control_plane.models import Deployment, GatewaySession, Route, RoutingStrategy
from tests.compiler.helpers import OCT, SCOPE, add_route, load_version, seed_scope, touch
from tests.conftest import create_database, drop_database


async def test_custom_strategy_compile_capability_warning_and_rollback(factory):
    await seed_scope(factory)
    async with factory() as s, s.begin():
        route = await add_route(s, "custom", [("a", None)])
        dep = await s.scalar(select(Deployment))
        dep.weight = 0  # custom strategies, unlike weighted, can use zero-weight pools
        route.strategy = RoutingStrategy.CUSTOM
        route.custom_strategy = "header_pin"
        route.strategy_params = {"headers": ["x-pin"]}
    async with factory() as s:
        result = await compile_scope(s, SCOPE, now=OCT, require_local_membership=True)
    route = result.snapshot.routes[0]
    assert route.strategy == pb.ROUTING_STRATEGY_CUSTOM
    assert route.custom_strategy == "header_pin"
    assert json.loads(route.strategy_params_json) == {"headers": ["x-pin"]}
    assert list(result.snapshot.required_capabilities) == ["strategy:header_pin"]
    assert len(result.warnings) == 1 and "not advertised" in result.warnings[0]
    async with factory() as s, s.begin():
        s.add(
            GatewaySession(
                scope=SCOPE,
                gateway_id="custom-gateway",
                connected=True,
                capabilities=["strategy:header_pin"],
            )
        )
    async with factory() as s:
        assert not (await compile_scope(s, SCOPE, now=OCT, require_local_membership=True)).warnings
    await touch(factory)
    assert await publish_pending(factory, SCOPE, require_local_membership=True) == 1
    async with factory() as s, s.begin():
        config = await s.scalar(select(Route))
        assert config.strategy is RoutingStrategy.CUSTOM
        assert config.custom_strategy == "header_pin"
        config.strategy_params = {"headers": ["x-other"]}
    await touch(factory)
    assert await publish_pending(factory, SCOPE, require_local_membership=True) == 2
    async with factory() as s, s.begin():
        assert await rollback_routing(s, SCOPE, 1, require_local_membership=True) == 3
    assert (await load_version(factory, 3)).routes[0] == route
    async with factory() as s:
        compiled = await compile_scope(s, SCOPE, now=OCT, require_local_membership=True)
        assert compiled.snapshot.routes[0] == route


@pytest.mark.parametrize(
    "params",
    [
        "[]",
        '{"headers":"x-pin"}',
        '{"headers":["bad name"]}',
        '{"x":NaN}',
        '{"x":"' + "a" * 16384 + '"}',
    ],
)
def test_custom_strategy_mirror_rejects_invalid_params(params):
    snap = pb.Snapshot(
        required_capabilities=["strategy:header_pin"],
        routes=[
            pb.ModelRoute(
                name="m",
                strategy=pb.ROUTING_STRATEGY_CUSTOM,
                custom_strategy="header_pin",
                strategy_params_json=params,
            )
        ],
    )
    with pytest.raises(SnapshotInvalid, match="strategy|headers"):
        validate_snapshot(snap, max_bytes=64 * 1024 * 1024, max_keys=200_000)


async def test_custom_strategy_migration_up_down():
    url = await create_database()
    engine = create_async_engine(url)
    try:
        await migrate.upgrade(engine, "0004")
        await migrate.upgrade(engine, "0005")
        route_id = uuid.uuid4()
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO routes (id, model, strategy, custom_strategy, strategy_params)"
                    " VALUES (:id, 'custom', 'custom', 'header_pin', '{\"headers\":[\"x-pin\"]}')"
                ),
                {"id": route_id},
            )
        await migrate.downgrade(engine, "0004")
        async with engine.connect() as conn:
            assert (
                await conn.scalar(
                    text("SELECT strategy FROM routes WHERE id = :id"), {"id": route_id}
                )
                == "least_in_flight"
            )
            assert (
                await conn.scalar(
                    text(
                        "SELECT count(*) FROM information_schema.columns"
                        " WHERE table_name = 'routes'"
                        " AND column_name IN ('custom_strategy', 'strategy_params')"
                    )
                )
                == 0
            )
        await migrate.upgrade(engine, "0005")
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT strategy, custom_strategy, strategy_params FROM routes"
                        " WHERE id = :id"
                    ),
                    {"id": route_id},
                )
            ).one()
            assert tuple(row) == ("least_in_flight", None, None)
    finally:
        await engine.dispose()
        await drop_database(url)


@pytest.mark.parametrize(
    ("strategy", "name", "params"),
    [
        (RoutingStrategy.CUSTOM, None, {}),
        (RoutingStrategy.CUSTOM, "Bad", {}),
        (RoutingStrategy.CUSTOM, "ok", []),
        (RoutingStrategy.WEIGHTED, "ok", None),
        (RoutingStrategy.WEIGHTED, None, {}),
    ],
)
async def test_route_custom_strategy_database_constraints(factory, strategy, name, params):
    with pytest.raises(IntegrityError):
        async with factory() as s, s.begin():
            s.add(
                Route(
                    name="invalid", strategy=strategy, custom_strategy=name, strategy_params=params
                )
            )
            await s.flush()


@pytest.mark.parametrize("params", [None, {}])
async def test_custom_strategy_nullable_params_compile_as_object(factory, params):
    await seed_scope(factory)
    async with factory() as s, s.begin():
        route = await add_route(s, "custom", [("a", None)])
        route.strategy = RoutingStrategy.CUSTOM
        route.custom_strategy = "header_pin"
        route.strategy_params = params
    async with factory() as s:
        compiled = await compile_scope(s, SCOPE, require_local_membership=True)
        assert json.loads(compiled.snapshot.routes[0].strategy_params_json) == {}


def test_custom_params_nesting_matches_gateway_recursion_budget():
    params = {}
    for _ in range(126):
        params = {"x": params}
    assert strategy_problem("custom", "ok", params) is None
    assert "nesting" in strategy_problem("custom", "ok", {"x": params})
