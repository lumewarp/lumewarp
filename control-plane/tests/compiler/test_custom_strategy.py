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
from lumewarp_control_plane.models import CatalogKind, CatalogName, GatewaySession, Router
from tests import factories
from tests.compiler.helpers import OCT, SCOPE, add_router, load_version, seed_scope, touch
from tests.conftest import create_database, drop_database


def _wire_router(snapshot: pb.Snapshot, name: str = "custom") -> pb.Router:
    return next(r for r in snapshot.routers if r.name == name)


async def test_custom_strategy_compile_capability_warning_and_rollback(factory):
    await seed_scope(factory)
    async with factory() as s, s.begin():
        # Custom strategies, unlike weighted, can use zero-weight pools.
        a = await factories.make_model(s, "a")
        await factories.make_router(
            s, "custom", [(a, 0)], strategy="header_pin", params={"headers": ["x-pin"]}
        )
    async with factory() as s:
        result = await compile_scope(s, SCOPE, now=OCT, require_local_membership=True)
    router = _wire_router(result.snapshot)
    assert router.strategy == pb.ROUTING_STRATEGY_CUSTOM
    assert router.custom_strategy == "header_pin"
    assert json.loads(router.strategy_params_json) == {"headers": ["x-pin"]}
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
        config = await s.scalar(select(Router))
        assert config.strategy == "header_pin"
        config.params = {"headers": ["x-other"]}
    await touch(factory)
    assert await publish_pending(factory, SCOPE, require_local_membership=True) == 2
    async with factory() as s, s.begin():
        assert await rollback_routing(s, SCOPE, 1, require_local_membership=True) == 3
    assert _wire_router(await load_version(factory, 3)) == router
    async with factory() as s:
        compiled = await compile_scope(s, SCOPE, now=OCT, require_local_membership=True)
        assert _wire_router(compiled.snapshot) == router


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
        routers=[
            pb.Router(
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
        router_id = uuid.uuid4()
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO routes (id, model, strategy, custom_strategy, strategy_params)"
                    " VALUES (:id, 'custom', 'custom', 'header_pin', '{\"headers\":[\"x-pin\"]}')"
                ),
                {"id": router_id},
            )
        await migrate.downgrade(engine, "0004")
        async with engine.connect() as conn:
            assert (
                await conn.scalar(
                    text("SELECT strategy FROM routes WHERE id = :id"), {"id": router_id}
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
                    {"id": router_id},
                )
            ).one()
            assert tuple(row) == ("least_in_flight", None, None)
    finally:
        await engine.dispose()
        await drop_database(url)


@pytest.mark.parametrize(
    ("strategy", "params"),
    [("Bad", {}), ("ok", []), ("9lives", {})],
)
async def test_router_strategy_database_constraints(factory, strategy, params):
    with pytest.raises(IntegrityError):
        async with factory() as s, s.begin():
            name = CatalogName(name="invalid", kind=CatalogKind.ROUTER)
            s.add(name)
            await s.flush()
            s.add(Router(name_id=name.id, strategy=strategy, params=params))
            await s.flush()


async def test_custom_strategy_empty_params_compile_as_object(factory):
    await seed_scope(factory)
    async with factory() as s, s.begin():
        await add_router(s, "custom", [("a", None)], strategy="header_pin")
    async with factory() as s:
        compiled = await compile_scope(s, SCOPE, require_local_membership=True)
        assert json.loads(_wire_router(compiled.snapshot).strategy_params_json) == {}


def test_params_nesting_matches_gateway_recursion_budget():
    params = {}
    for _ in range(126):
        params = {"x": params}
    assert strategy_problem("ok", params) is None
    assert "nesting" in strategy_problem("ok", {"x": params})


@pytest.mark.parametrize(
    ("strategy", "params", "needle"),
    [
        ("Bad", {}, "must match"),
        ("weighted", {"x": 1}, "takes no params"),
        ("header_pin", {"headers": ["authorization"]}, "headers"),
    ],
)
def test_strategy_problem(strategy, params, needle):
    assert needle in strategy_problem(strategy, params)


@pytest.mark.parametrize(
    ("strategy", "params"),
    [("weighted", {}), ("vllm_load", {}), ("header_pin", {"headers": ["x-pin"]})],
)
def test_strategy_accepted(strategy, params):
    assert strategy_problem(strategy, params) is None
