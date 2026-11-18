"""Catalog source state is per zone: deployments, MCP/A2A and rollback."""

from sqlalchemy import select

from lumewarp_control_plane import outbox
from lumewarp_control_plane._proto.lumewarp.config.v1 import snapshot_pb2 as pb
from lumewarp_control_plane.compiler import compile_zone, publish_pending, rollback_routing
from lumewarp_control_plane.models import (
    ConfigVersion,
    ModelDeployment,
    RouterSplit,
    RouterSplitMember,
)
from tests import factories
from tests.compiler.helpers import seed_scope

PROD = "prod"
DEV = "dev"


async def _compile(factory, zone):
    async with factory() as session:
        return await compile_zone(session, zone, require_local_membership=False)


async def _publish(factory, zone):
    async with factory() as session, session.begin():
        await outbox.enqueue_recompile(session, "test")
    version = await publish_pending(factory, zone, require_local_membership=False)
    assert version is not None
    return version


async def test_a_model_is_emitted_only_where_it_is_deployed(factory) -> None:
    await seed_scope(factory, PROD)
    await seed_scope(factory, DEV)
    async with factory() as session, session.begin():
        both = await factories.make_model(session, "both", zone=PROD)
        dev_upstream = await factories.make_upstream(session, "dev-up", zone=DEV)
        session.add(
            ModelDeployment(
                name_id=both.id, zone=DEV, upstream_id=dev_upstream.id, upstream_model="dev-name"
            )
        )
        await factories.make_model(session, "only-dev", zone=DEV)
        await session.flush()

    prod = (await _compile(factory, PROD)).snapshot
    dev = (await _compile(factory, DEV)).snapshot
    assert [m.name for m in prod.models] == ["both"]
    assert prod.models[0].upstream_model != "dev-name"
    assert sorted(m.name for m in dev.models) == ["both", "only-dev"]
    assert next(m for m in dev.models if m.name == "both").upstream_model == "dev-name"


async def test_router_members_without_a_deployment_are_dropped(factory) -> None:
    await seed_scope(factory, PROD)
    await seed_scope(factory, DEV)
    async with factory() as session, session.begin():
        deployed = await factories.make_model(session, "deployed", zone=PROD)
        only_dev = await factories.make_model(session, "only-dev", zone=DEV)
        await factories.make_router(session, "mixed", [deployed, only_dev])
        await factories.make_router(session, "empty-in-prod", [only_dev])
        await session.flush()

    prod = await _compile(factory, PROD)
    dev = (await _compile(factory, DEV)).snapshot
    assert [r.name for r in prod.snapshot.routers] == ["mixed"]
    assert [m.model for m in prod.snapshot.routers[0].members] == ["deployed"]
    assert any("not available in this zone" in w for w in prod.warnings)
    assert sorted(r.name for r in dev.routers) == ["empty-in-prod", "mixed"]


async def test_a_split_that_loses_every_member_drops_the_router(factory) -> None:
    await seed_scope(factory, PROD)
    await seed_scope(factory, DEV)
    async with factory() as session, session.begin():
        only_dev = await factories.make_model(session, "only-dev", zone=DEV)
        router = await factories.make_router(session, "splits")
        split = RouterSplit(router_id=router.id, name="only", weight=1, position=0)
        session.add(split)
        await session.flush()
        session.add(RouterSplitMember(split_id=split.id, model_id=only_dev.id, position=0))
        await session.flush()

    prod = await _compile(factory, PROD)
    assert [r.name for r in prod.snapshot.routers] == []
    assert any("only-dev" in w and "not available in this zone" in w for w in prod.warnings)


async def test_an_alias_of_an_absent_target_is_dropped(factory) -> None:
    await seed_scope(factory, PROD)
    await seed_scope(factory, DEV)
    async with factory() as session, session.begin():
        model = await factories.make_model(session, "only-dev", zone=DEV)
        await factories.make_alias(session, "nick", model)
        await session.flush()

    prod = await _compile(factory, PROD)
    dev = (await _compile(factory, DEV)).snapshot
    assert [a.name for a in prod.snapshot.aliases] == []
    assert any("alias 'nick'" in w for w in prod.warnings)
    assert [(a.name, a.target) for a in dev.aliases] == [("nick", "only-dev")]


async def test_mcp_servers_and_agents_are_per_zone(factory) -> None:
    await seed_scope(factory, PROD)
    await seed_scope(factory, DEV)
    async with factory() as session, session.begin():
        await factories.make_mcp_server(session, "shared", zone=PROD)
        await factories.make_mcp_server(session, "shared", zone=DEV)
        await factories.make_mcp_server(session, "prod-only", zone=PROD)
        await factories.make_agent(session, "shared", zone=PROD)
        await factories.make_agent(session, "dev-only", zone=DEV)
        await session.flush()

    prod = (await _compile(factory, PROD)).snapshot
    dev = (await _compile(factory, DEV)).snapshot
    assert sorted(s.name for s in prod.mcp_servers) == ["prod-only", "shared"]
    assert [s.name for s in dev.mcp_servers] == ["shared"]
    assert [a.name for a in prod.a2a_agents] == ["shared"]
    assert [a.name for a in dev.a2a_agents] == ["dev-only"]


async def test_rollback_restores_deployments_in_every_zone(factory) -> None:
    await seed_scope(factory, PROD)
    await seed_scope(factory, DEV)
    async with factory() as session, session.begin():
        model = await factories.make_model(session, "m", zone=PROD)
        dev_upstream = await factories.make_upstream(session, "dev-up", zone=DEV)
        session.add(
            ModelDeployment(
                name_id=model.id, zone=DEV, upstream_id=dev_upstream.id, upstream_model="dev-old"
            )
        )
        await factories.make_router(session, "r", [model])
        await session.flush()
        model_id = model.id
        prod_upstream_id = await session.scalar(
            select(ModelDeployment.upstream_id).where(
                ModelDeployment.name_id == model_id, ModelDeployment.zone == PROD
            )
        )
    version = await _publish(factory, PROD)
    assert await _publish(factory, DEV) is not None

    async with factory() as session, session.begin():
        await session.execute(
            ModelDeployment.__table__.update()
            .where(ModelDeployment.name_id == model_id)
            .values(upstream_model="changed")
        )
        dev = await session.get(ModelDeployment, (model_id, DEV))
        dev.upstream_model = "dev-changed"

    async with factory() as session, session.begin():
        await rollback_routing(session, PROD, version, require_local_membership=False)

    async with factory() as session:
        prod = await session.get(ModelDeployment, (model_id, PROD))
        dev = await session.get(ModelDeployment, (model_id, DEV))
        assert prod is not None and dev is not None
        assert prod.upstream_model != "changed"
        assert prod.upstream_id == prod_upstream_id
        assert dev.upstream_model == "dev-old"

    # Every zone published the restored deployment, not only the anchor zone: rollback
    # compiles each zone from its own restored rows.
    dev_snapshot = await _load(factory, DEV)
    assert next(m for m in dev_snapshot.models if m.name == "m").upstream_model == "dev-old"


async def _load(factory, zone) -> pb.Snapshot:
    async with factory() as session:
        latest = await session.scalar(
            select(ConfigVersion.version)
            .where(ConfigVersion.zone == zone)
            .order_by(ConfigVersion.version.desc())
        )
        assert latest is not None
        row = await session.get(ConfigVersion, (zone, latest))
        return pb.Snapshot.FromString(row.payload)
