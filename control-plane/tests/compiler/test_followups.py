"""Compiler regressions from the P1 end-to-end run."""

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError

from lumewarp_control_plane.compiler import publish as publisher
from lumewarp_control_plane.models import ConfigOutbox, ConfigVersion, Deployment, OutboxStatus
from lumewarp_control_plane.settings import Settings
from lumewarp_control_plane.usage import _rollover_once
from tests.compiler.helpers import SCOPE, add_route, seed_scope, touch


async def test_restart_and_noop_recompile_keep_version(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        await add_route(s, "m", [("d", None)])
    await touch(factory)
    now = datetime(2026, 10, 10, tzinfo=UTC)
    assert (
        await publisher.publish_pending(
            factory, SCOPE, now=now, router_plugins=[], require_local_membership=True
        )
        == 1
    )
    async with factory() as s:
        before = await s.get(ConfigVersion, (SCOPE, 1))
        original = (before.payload, before.digest, before.source_revision)

    # Startup's rollover scheduler enqueues work even in the same month. Publication
    # time and revision differ but the gateway-visible content has not changed.
    await _rollover_once(Settings(scope=SCOPE), factory)
    assert (
        await publisher.publish_pending(
            factory,
            SCOPE,
            now=now + timedelta(days=1),
            router_plugins=[],
            require_local_membership=True,
        )
        is None
    )
    assert (
        await publisher.publish_pending(
            factory, SCOPE, router_plugins=[], require_local_membership=True
        )
        is None
    )
    async with factory() as s:
        assert await s.scalar(select(func.count()).select_from(ConfigVersion)) == 1
        after = await s.get(ConfigVersion, (SCOPE, 1))
        assert (after.payload, after.digest, after.source_revision) == original
        assert after.digest == hashlib.sha256(after.payload).digest()
        rows = list(await s.scalars(select(ConfigOutbox)))
        assert all(r.status == OutboxStatus.DONE and r.published_version == 1 for r in rows)

    async with factory() as s, s.begin():
        await s.execute(update(Deployment).values(weight=3))
    await touch(factory)
    assert (
        await publisher.publish_pending(
            factory, SCOPE, router_plugins=[], require_local_membership=True
        )
        == 2
    )


async def test_config_insert_serialization_conflict_retries_fresh_snapshot(
    factory, monkeypatch, caplog
) -> None:
    await seed_scope(factory)
    await touch(factory)
    # Force the INSERT to lock the source row. A source transaction committed after
    # the compiler's REPEATABLE READ snapshot makes this fail with real SQLSTATE 40001.
    async with factory() as s, s.begin():
        await s.execute(
            text("""
            CREATE FUNCTION conflict_on_config_insert() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                PERFORM 1 FROM scopes WHERE name = NEW.scope FOR UPDATE;
                RETURN NEW;
            END $$
        """)
        )
        await s.execute(
            text("""
            CREATE TRIGGER conflict_on_config_insert BEFORE INSERT ON config_versions
            FOR EACH ROW EXECUTE FUNCTION conflict_on_config_insert()
        """)
        )
    original = publisher.store_version
    attempts = 0
    conflicts = []

    async def conflicting_insert(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            async with factory() as s, s.begin():
                await add_route(s, "new", [("new-d", None)])
            await touch(factory)
        try:
            return await original(*args, **kwargs)
        except DBAPIError as exc:
            conflicts.append(exc.orig.sqlstate)
            raise

    monkeypatch.setattr(publisher, "store_version", conflicting_insert)
    assert (
        await publisher.publish_pending(
            factory, SCOPE, router_plugins=[], require_local_membership=True
        )
        == 1
    )
    assert attempts == 2
    assert conflicts == ["40001"]
    assert not any(r.exc_info for r in caplog.records)
    async with factory() as s:
        [version] = list(await s.scalars(select(ConfigVersion)))
        assert version.source_revision == 2
        from lumewarp_control_plane._proto.lumewarp.config.v1 import snapshot_pb2 as pb

        assert [r.model for r in pb.Snapshot.FromString(version.payload).routes] == ["new"]
        assert all(r.status == OutboxStatus.DONE for r in await s.scalars(select(ConfigOutbox)))
    # The session advisory lock was released, and no work is left.
    assert (
        await publisher.publish_pending(
            factory, SCOPE, router_plugins=[], require_local_membership=True
        )
        is None
    )


@pytest.mark.parametrize("sqlstate, attempts", [("40001", 5), ("23514", 1)])
async def test_retry_is_bounded_and_only_for_serialization(
    factory, monkeypatch, sqlstate, attempts
):
    await seed_scope(factory)
    await touch(factory)
    calls = 0

    class Failure(Exception):
        pass

    failure = Failure("forced failure")
    failure.sqlstate = sqlstate

    async def fail(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise DBAPIError("INSERT INTO config_versions", {}, failure)

    monkeypatch.setattr(publisher, "store_version", fail)
    with pytest.raises(DBAPIError):
        await publisher.publish_pending(
            factory, SCOPE, router_plugins=[], require_local_membership=True
        )
    assert calls == attempts
    async with factory() as s:
        assert await s.scalar(select(func.count()).select_from(ConfigVersion)) == 0
        [row] = list(await s.scalars(select(ConfigOutbox)))
        assert row.status == OutboxStatus.PENDING
        assert row.attempts == 0
