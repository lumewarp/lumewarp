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
    ApiKey,
    ConfigVersion,
    KeyLifecycle,
    Model,
    Router,
    RouterFallback,
    RouterModel,
    RouterSplit,
    RouterSplitModel,
    Team,
    Upstream,
    UpstreamKind,
)
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
        primary.retry_enabled = True
        primary.retry_max_attempts = 3
        session.add(RouterFallback(router_id=primary.id, fallback_router_id=backup.id, position=0))
        split = RouterSplit(router_id=primary.id, name="stable", weight=100, position=0)
        session.add(split)
        await session.flush()
        model_id = await session.scalar(select(Model.id).where(Model.name == "model"))
        session.add(RouterSplitModel(split_id=split.id, model_id=model_id, position=0))
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
        await session.execute(update(Router).where(Router.id == primary_id).values(name="new"))
        await session.execute(delete(RouterFallback))
        await session.execute(delete(Router).where(Router.id == backup_id))
        await session.execute(delete(Model).where(Model.name == "backup-model"))
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
        a = await session.scalar(select(Model).where(Model.name == "a"))
        b = await session.scalar(select(Model).where(Model.name == "b"))
        a.name = "temporary"
        await session.flush()
        b.name = "a"
        await session.flush()
        a.name = "b"
        await session.flush()
        await restore_catalog_state(session, expected)
        assert await capture_catalog_state(session) == expected
        assert (await session.scalars(select(Model).order_by(Model.name))).all()[0].name == "a"


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
            bad["tables"]["models"][0]["id"] = "not-a-uuid"
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
        await session.execute(delete(RouterModel))
        await session.execute(delete(Router))
        await add_router(session, "public", [("a", None)])
        new_id = await session.scalar(select(Router.id))
        assert isinstance(new_id, uuid.UUID) and new_id != old_id
    await touch(factory)
    assert await publish_pending(factory, SCOPE, require_local_membership=True) == 2
    async with factory() as session, session.begin():
        await rollback_routing(session, SCOPE, 1, require_local_membership=True)
        assert await session.scalar(select(Router.id)) == old_id
