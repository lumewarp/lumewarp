import json

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine

from lumewarp_control_plane import migrate
from lumewarp_control_plane._proto.lumewarp.config.v1 import snapshot_pb2 as pb
from lumewarp_control_plane.compiler import (
    compile_scope,
    publish_pending,
    rollback_routing,
)
from lumewarp_control_plane.compiler.strategy import CustomRouteStrategy, strategy_problem
from lumewarp_control_plane.compiler.validate import SnapshotInvalid, validate_snapshot
from lumewarp_control_plane.models import Deployment, GatewaySession
from tests.compiler.helpers import OCT, SCOPE, add_route, load_version, seed_scope, touch
from tests.conftest import create_database, drop_database


async def test_custom_strategy_compile_capability_warning_and_rollback(factory):
    await seed_scope(factory)
    async with factory() as s, s.begin():
        route = await add_route(s, "custom", [("a", None)])
        dep = await s.scalar(select(Deployment))
        dep.weight = 0  # custom strategies, unlike weighted, can use zero-weight pools
        s.add(
            CustomRouteStrategy(route_id=route.id, name="header_pin", params={"headers": ["x-pin"]})
        )
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
        config = await s.scalar(select(CustomRouteStrategy))
        config.params = {"headers": ["x-other"]}
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
                model="m",
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
        await migrate.upgrade(engine, "0003")
        await migrate.upgrade(engine, "0005")
        async with engine.connect() as conn:
            assert (
                await conn.scalar(text("SELECT to_regclass('route_custom_strategies')")) is not None
            )
        await migrate.downgrade(engine, "0003")
        async with engine.connect() as conn:
            assert await conn.scalar(text("SELECT to_regclass('route_custom_strategies')")) is None
        await migrate.upgrade(engine, "0005")
    finally:
        await engine.dispose()
        await drop_database(url)


def test_custom_params_nesting_matches_gateway_recursion_budget():
    params = {}
    for _ in range(126):
        params = {"x": params}
    assert strategy_problem("custom", "ok", params) is None
    assert "nesting" in strategy_problem("custom", "ok", {"x": params})
