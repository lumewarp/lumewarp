"""Rollback sidecars preserve source identities independently of wire names."""

import copy
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from lumewarp_control_plane.compiler import publish_pending, rollback_routing
from lumewarp_control_plane.compiler.catalog_state import (
    capture_catalog_state,
    restore_catalog_state,
)
from lumewarp_control_plane.models import (
    Alias,
    ApiKey,
    AuditLog,
    CatalogName,
    CatalogNameHistory,
    ConfigVersion,
    KeyLifecycle,
    Model,
    ModelAccess,
    PolicyGrant,
    Router,
    RouterFallback,
    RouterSplit,
    RouterSplitMember,
    Team,
    Upstream,
    UpstreamKind,
)
from tests import factories
from tests.compiler.helpers import (
    SCOPE,
    add_hierarchy,
    add_key,
    add_router,
    load_version,
    seed_scope,
    touch,
)


async def test_restore_recreates_deleted_ids_and_preserves_changed_policy(factory):
    await seed_scope(factory)
    async with factory() as session, session.begin():
        primary = await add_router(session, "public", [("model", 7)])
        backup = await add_router(session, "backup", [("backup-model", 8)])
        backup_model = await session.scalar(select(Model).where(Model.name == "backup-model"))
        primary.retry_enabled = True
        primary.retry_max_attempts = 3
        session.add(RouterFallback(router_id=primary.id, model_id=backup_model.id, position=0))
        split = RouterSplit(router_id=primary.id, name="stable", weight=100, position=0)
        session.add(split)
        await session.flush()
        model_id = await session.scalar(select(Model.id).where(Model.name == "model"))
        session.add(RouterSplitMember(split_id=split.id, model_id=model_id, position=0))
        team, user = await add_hierarchy(session)
        key = await add_key(session, team, user)
        await session.flush()
        expected = await capture_catalog_state(session)
        primary_id, backup_id, key_id = primary.id, backup.id, key.id
    await touch(factory)
    assert await publish_pending(factory, SCOPE, require_local_membership=True) == 1
    before = await load_version(factory, 1)

    async with factory() as session, session.begin():
        # Rename one retained identity, remove another, and repoint a retained model
        # to an upstream that did not exist in the published version.
        await session.execute(
            update(CatalogName).where(CatalogName.id == primary_id).values(name="new")
        )
        await session.execute(delete(RouterFallback))
        await session.execute(delete(CatalogName).where(CatalogName.id == backup_id))
        await session.execute(delete(CatalogName).where(CatalogName.name == "backup-model"))
        upstream = Upstream(name="new-upstream", kind=UpstreamKind.OPENAI, base_url="http://new/v1")
        session.add(upstream)
        await session.flush()
        await session.execute(
            update(Model).where(Model.id == model_id).values(upstream_id=upstream.id)
        )
        # Current policy is not restored from the routing sidecar.
        await session.execute(update(Team).values(rpm_limit=23))
        await session.execute(
            update(ApiKey)
            .where(ApiKey.id == key_id)
            .values(lifecycle=KeyLifecycle.REVOKED, revoked_at=datetime.now(UTC))
        )
    async with factory() as session, session.begin():
        version = await rollback_routing(session, SCOPE, 1, require_local_membership=True)
        assert await capture_catalog_state(session) == expected
        assert await session.get(Router, backup_id) is not None
        assert await session.scalar(select(Team.rpm_limit)) == 23
    restored = await load_version(factory, version)
    assert restored.models == before.models
    assert restored.routers == before.routers
    assert not restored.api_keys


async def test_restore_handles_reused_and_swapped_names(factory):
    await seed_scope(factory)
    async with factory() as session, session.begin():
        await add_router(session, "one", [("a", None)])
        await add_router(session, "two", [("b", None)])
        expected = await capture_catalog_state(session)
        a = await session.scalar(select(CatalogName).where(CatalogName.name == "a"))
        b = await session.scalar(select(CatalogName).where(CatalogName.name == "b"))
        a_id = a.id
        a.name = "temporary"
        await session.flush()
        b.name = "a"
        await session.flush()
        a.name = "b"
        await session.flush()
        await restore_catalog_state(session, expected)
        assert await capture_catalog_state(session) == expected
        assert await session.scalar(select(Model.name).where(Model.name_id == a_id)) == "a"


@pytest.mark.parametrize("corruption", ["version", "table", "column", "uuid"])
async def test_bad_sidecar_fails_before_any_mutation(factory, corruption):
    await seed_scope(factory)
    async with factory() as session, session.begin():
        await add_router(session, "public", [("a", None)])
        expected = await capture_catalog_state(session)
        bad = copy.deepcopy(expected)
        if corruption == "version":
            bad["format_version"] = 999
        elif corruption == "table":
            bad["tables"]["api_keys"] = []
        elif corruption == "column":
            bad["tables"]["models"][0]["secret"] = "not accepted"
        else:
            bad["tables"]["models"][0]["name_id"] = "not-a-uuid"
        with pytest.raises(ValueError):
            await restore_catalog_state(session, bad)
        assert await capture_catalog_state(session) == expected


async def test_sidecar_is_immutable_except_joint_compaction(factory):
    await seed_scope(factory)
    await touch(factory)
    assert await publish_pending(factory, SCOPE, require_local_membership=True) == 1
    async with factory() as session, session.begin():
        for changes in ({"catalog_state": {}}, {"catalog_state": None}, {"payload": None}):
            with pytest.raises(IntegrityError):
                async with session.begin_nested():
                    await session.execute(update(ConfigVersion).values(**changes))
        row = await session.get(ConfigVersion, (SCOPE, 1))
        assert row.catalog_state == await capture_catalog_state(session)


async def test_same_wire_names_with_new_ids_publish_a_new_sidecar(factory):
    await seed_scope(factory)
    async with factory() as session, session.begin():
        await add_router(session, "public", [("a", None)])
    await touch(factory)
    assert await publish_pending(factory, SCOPE, require_local_membership=True) == 1
    async with factory() as session, session.begin():
        old_id = await session.scalar(select(Router.id))
        await session.execute(delete(CatalogName).where(CatalogName.name == "public"))
        await add_router(session, "public", [("a", None)])
        new_id = await session.scalar(select(Router.id))
        assert isinstance(new_id, uuid.UUID) and new_id != old_id
    await touch(factory)
    assert await publish_pending(factory, SCOPE, require_local_membership=True) == 2
    async with factory() as session, session.begin():
        await rollback_routing(session, SCOPE, 1, require_local_membership=True)
        assert await session.scalar(select(Router.id)) == old_id


async def _publish(factory) -> int:
    await touch(factory)
    return await publish_pending(factory, SCOPE, require_local_membership=True)


async def test_rollback_before_rename_keeps_grants_and_references(factory):
    await seed_scope(factory)
    async with factory() as session, session.begin():
        model = await factories.make_model(session, "m")
        router = await factories.make_router(session, "r", [model])
        alias = await factories.make_alias(session, "al", router)
        team = await factories.make_team(
            session, model_access=ModelAccess.LIST, allowed_models=["m", "r", "al"]
        )
        account = await factories.make_service_account(
            session, team, model_access=ModelAccess.LIST, allowed_models=["m"]
        )
        await add_key(session, team, account)
        ids = (model.id, router.id, alias.id)
    assert await _publish(factory) == 1
    async with factory() as session, session.begin():
        for name_id, new_name in zip(ids, ("m2", "r2", "al2"), strict=True):
            await session.execute(
                update(CatalogName).where(CatalogName.id == name_id).values(name=new_name)
            )
    assert await _publish(factory) == 2
    assert list((await load_version(factory, 2)).api_keys[0].policy.allowed_models) == ["m2"]

    async with factory() as session, session.begin():
        assert await rollback_routing(session, SCOPE, 1, require_local_membership=True) == 3
    restored = await load_version(factory, 3)
    assert list(restored.api_keys[0].policy.allowed_models) == ["m"]
    assert [m.name for m in restored.models] == ["m"]
    assert [r.name for r in restored.routers] == ["r"]
    assert [(a.name, a.target) for a in restored.aliases] == [("al", "r")]
    async with factory() as session:
        assert await session.get(Model, ids[0]) is not None
        assert await session.scalar(select(Alias.target_name_id)) == ids[1]
        grants = await session.scalars(select(PolicyGrant.name_id))
        assert set(grants) == set(ids)
        loaded_team = await session.get(Team, team.id)
        assert loaded_team.allowed_models == ["al", "m", "r"]
        history = await session.execute(
            select(CatalogNameHistory.name_id, CatalogNameHistory.old_name)
        )
        assert set(history.all()) == {(ids[0], "m2"), (ids[1], "r2"), (ids[2], "al2")}


async def test_rollback_recreates_deleted_item_without_its_grants(factory):
    await seed_scope(factory)
    async with factory() as session, session.begin():
        model = await factories.make_model(session, "m")
        team = await factories.make_team(
            session, model_access=ModelAccess.LIST, allowed_models=["m"]
        )
        model_id, team_id = model.id, team.id
    assert await _publish(factory) == 1
    async with factory() as session, session.begin():
        await session.execute(delete(CatalogName).where(CatalogName.id == model_id))
    assert await _publish(factory) == 2
    async with factory() as session, session.begin():
        await rollback_routing(session, SCOPE, 1, require_local_membership=True)
    async with factory() as session:
        assert (await session.get(Model, model_id)).name == "m"
        assert (await session.get(Team, team_id)).allowed_models == []


async def test_rollback_removing_a_name_audits_its_grants(factory):
    await seed_scope(factory)
    async with factory() as session, session.begin():
        await factories.make_upstream(session, "u")
    assert await _publish(factory) == 1
    async with factory() as session, session.begin():
        await factories.make_model(session, "late")
        team = await factories.make_team(
            session, model_access=ModelAccess.LIST, allowed_models=["late"]
        )
        team_id = team.id
    assert await _publish(factory) == 2
    async with factory() as session, session.begin():
        await rollback_routing(session, SCOPE, 1, require_local_membership=True)
    async with factory() as session:
        assert await session.scalar(select(CatalogName.id)) is None
        assert (await session.get(Team, team_id)).allowed_models == []
        row = await session.scalar(select(AuditLog).where(AuditLog.action == "grant.delete"))
        assert row.team_id == team_id and row.details["name"] == "late"
