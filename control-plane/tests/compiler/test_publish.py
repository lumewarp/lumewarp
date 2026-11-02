import asyncio
import hashlib
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, update

from lumewarp_control_plane._proto.lumewarp.config.v1 import snapshot_pb2 as pb
from lumewarp_control_plane.compiler import (
    compact_versions,
    diff,
    publish_pending,
    rollback_routing,
    run_compiler_loop,
    store_version,
)
from lumewarp_control_plane.models import (
    ApiKey,
    ConfigOutbox,
    ConfigVersion,
    ConfigVersionKind,
    GatewaySession,
    KeyLifecycle,
    Model,
    OutboxStatus,
    Router,
    Team,
)
from tests.compiler.helpers import (
    SCOPE,
    add_hierarchy,
    add_key,
    add_router,
    apply_delta,
    load_version,
    seed_scope,
    touch,
)
from tests.factories import make_key, make_team, make_user


async def publish(factory, **kw):
    kw.setdefault("require_local_membership", True)
    return await publish_pending(factory, SCOPE, **kw)


async def outbox_rows(factory) -> list[tuple[int, str, int | None]]:
    async with factory() as s:
        rows = await s.execute(
            select(
                ConfigOutbox.source_revision, ConfigOutbox.status, ConfigOutbox.published_version
            ).order_by(ConfigOutbox.id)
        )
        return [(r[0], r[1].value, r[2]) for r in rows]


async def versions(factory) -> list[ConfigVersion]:
    async with factory() as s:
        return list(await s.scalars(select(ConfigVersion).order_by(ConfigVersion.version)))


async def test_publish_basic(factory) -> None:
    await seed_scope(factory)
    assert await publish(factory) is None  # nothing pending

    async with factory() as s, s.begin():
        t, u = await add_hierarchy(s)
        await add_key(s, t, u)
    r1 = await touch(factory, "key.create")
    assert await publish(factory) == 1
    assert await publish(factory) is None
    assert await outbox_rows(factory) == [(r1, "done", 1)]

    [v1] = await versions(factory)
    assert (v1.source_revision, v1.kind, v1.key_count) == (r1, ConfigVersionKind.COMPILE, 1)
    assert v1.digest == hashlib.sha256(v1.payload).digest() and v1.size_bytes == len(v1.payload)
    snap = pb.Snapshot.FromString(v1.payload)
    assert (snap.version, snap.scope) == (1, SCOPE) and snap.created_at_unix_ms > 0

    r2 = await touch(factory)
    async with factory() as s, s.begin():
        await s.execute(update(Team).values(rpm_limit=17))
    r3 = await touch(factory)
    assert await publish(factory) == 2
    assert [r[:2] for r in await outbox_rows(factory)] == [
        (r1, "done"),
        (r2, "done"),
        (r3, "done"),
    ]
    assert (await versions(factory))[-1].source_revision == r3


async def test_concurrent_publishers_are_serialized(factory) -> None:
    await seed_scope(factory)
    published = []
    for i in range(6):
        async with factory() as s, s.begin():
            await add_router(s, f"m{i}", [(f"d{i}", None)])
        await touch(factory)
        results = await asyncio.gather(*(publish(factory) for _ in range(3)))
        new = [v for v in results if v is not None]
        assert len(new) == 1  # exactly one publisher wins per revision
        published += new
    assert published == list(range(1, 7))
    vs = await versions(factory)
    revisions = [v.source_revision for v in vs]
    assert revisions == sorted(revisions) and len(set(revisions)) == len(revisions)


async def test_concurrent_writes_and_publishes(factory) -> None:
    await seed_scope(factory)

    async def writer() -> None:
        for i in range(10):
            async with factory() as s, s.begin():
                await add_router(s, f"m{i}", [(f"d{i}", None)])
                from lumewarp_control_plane import outbox

                await outbox.enqueue_recompile(s, SCOPE, "router.create")
            await asyncio.sleep(0)

    async def publisher() -> None:
        for _ in range(15):
            await publish(factory)
            await asyncio.sleep(0)

    await asyncio.gather(writer(), publisher(), publisher())
    await publish(factory)
    vs = await versions(factory)
    assert [v.version for v in vs] == list(range(1, len(vs) + 1))
    revisions = [v.source_revision for v in vs]
    assert revisions == sorted(set(revisions))
    async with factory() as s:
        final = await s.scalar(
            select(ConfigOutbox.source_revision).order_by(ConfigOutbox.id.desc())
        )
    assert revisions[-1] == final
    assert all(status == "done" for _, status, _ in await outbox_rows(factory))


async def test_stale_compile_is_not_published(factory) -> None:
    await seed_scope(factory)
    rev = await touch(factory)
    # A newer publisher (e.g. a rollback) already published this source revision.
    async with factory() as s, s.begin():
        snap = pb.Snapshot(scope=SCOPE)
        await store_version(s, SCOPE, snap, version=1, source_revision=rev)
    assert await publish(factory) is None
    assert await outbox_rows(factory) == [(rev, "done", 1)]
    assert len(await versions(factory)) == 1


async def test_compile_failure_keeps_previous_version(factory, monkeypatch) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        t, u = await add_hierarchy(s)
        await add_key(s, t, u, name="k1")
    await touch(factory)
    assert await publish(factory) == 1

    from lumewarp_control_plane.settings import get_settings

    monkeypatch.setattr(get_settings(), "max_snapshot_keys", 1)
    async with factory() as s, s.begin():
        t, u = await add_hierarchy(s, name="second")
        await add_key(s, t, u, name="k2")
    await touch(factory)
    assert await publish(factory) is None
    rows = await outbox_rows(factory)
    assert rows[-1][1] == "failed"
    async with factory() as s:
        err = await s.scalar(select(ConfigOutbox.last_error).order_by(ConfigOutbox.id.desc()))
    # Only the exception class is kept; the message may quote configuration.
    assert err == "CompileError"
    assert len(await versions(factory)) == 1

    # Fixing the source (any later mutation) publishes again.
    monkeypatch.setattr(get_settings(), "max_snapshot_keys", 10)
    await touch(factory)
    assert await publish(factory) == 2


# --------------------------------------------------------------------------- rollback


async def test_rollback_restores_routing_not_security(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        t, u = await add_hierarchy(s)
        k1 = await add_key(s, t, u, name="k1")
        await add_router(s, "llama", [("llama-a", 1)])
    await touch(factory)
    assert await publish(factory) == 1

    # Change routing and revoke k1.
    async with factory() as s, s.begin():
        await s.execute(update(Model).values(upstream_model="llama-new"))
        await add_router(s, "qwen", [("qwen-a", None)])
        await s.execute(
            update(ApiKey)
            .where(ApiKey.id == k1.id)
            .values(lifecycle=KeyLifecycle.REVOKED, revoked_at=datetime.now(UTC))
        )
    await touch(factory)
    assert await publish(factory) == 2

    async with factory() as s, s.begin():
        v3 = await rollback_routing(s, SCOPE, 1, require_local_membership=True)
    assert v3 == 3
    v1, s3 = await load_version(factory, 1), await load_version(factory, 3)
    for field in ("upstreams", "models", "routers"):
        assert list(getattr(s3, field)) == list(getattr(v1, field))
    assert str(k1.id) not in {k.key_id for k in s3.api_keys}  # revoked stays revoked
    row = (await versions(factory))[-1]
    assert (row.kind, row.rollback_of_version) == (ConfigVersionKind.ROLLBACK, 1)
    assert await publish(factory) is None  # rollback's revision already published

    # Source routing now matches v1, so later mutations keep the rolled-back routing.
    async with factory() as s, s.begin():
        t2, u2 = await add_hierarchy(s, name="later")
        k2 = await add_key(s, t2, u2)
        assert await s.scalar(select(Router.name).where(Router.name == "qwen")) is None
    await touch(factory)
    assert await publish(factory) == 4
    s4 = await load_version(factory, 4)
    assert list(s4.models) == list(v1.models)
    assert list(s4.routers) == list(v1.routers)
    assert str(k2.id) in {k.key_id for k in s4.api_keys}
    assert str(k1.id) not in {k.key_id for k in s4.api_keys}


async def test_rollback_unknown_or_compacted(factory) -> None:
    await seed_scope(factory)
    await touch(factory)
    await publish(factory)
    async with factory() as s, s.begin():
        with pytest.raises(LookupError):
            await rollback_routing(s, SCOPE, 9, require_local_membership=True)
    async with factory() as s, s.begin():
        await s.execute(
            update(ConfigVersion).values(
                payload=None, catalog_state=None, compacted_at=datetime.now(UTC)
            )
        )
    async with factory() as s, s.begin():
        with pytest.raises(LookupError):
            await rollback_routing(s, SCOPE, 1, require_local_membership=True)


# --------------------------------------------------------------------------- diff


async def test_diff_roundtrip_over_published_versions(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        t, u = await add_hierarchy(s)
        k1 = await add_key(s, t, u, name="k1")
        k2 = await add_key(s, t, u, name="k2")
        await add_router(s, "llama", [("llama-a", 1), ("llama-b", 2)])
    await touch(factory)
    await publish(factory)
    async with factory() as s, s.begin():
        await s.execute(
            update(ApiKey)
            .where(ApiKey.id == k1.id)
            .values(lifecycle=KeyLifecycle.REVOKED, revoked_at=datetime.now(UTC))
        )
        await s.execute(update(Team).where(Team.id == t.id).values(rpm_limit=5))
        await add_router(s, "qwen", [("qwen-a", None)], upstream="openai")
        t2, u2 = await add_hierarchy(s, name="beta")
        await add_key(s, t2, u2)
    await touch(factory)
    await publish(factory)

    a, b = await load_version(factory, 1), await load_version(factory, 2)
    d = diff(a, b)
    assert (d.base_version, d.version, d.created_at_unix_ms) == (1, 2, b.created_at_unix_ms)
    assert list(d.remove_api_keys) == [str(k1.id)]
    assert {k.key_id for k in d.upsert_api_keys} >= {str(k2.id)}
    assert [r.name for r in d.upsert_routers] == ["qwen"]
    assert [m.name for m in d.upsert_models] == ["qwen-a"]
    assert apply_delta(a, d) == b
    # Reverse direction and identity.
    assert apply_delta(b, diff(b, a)) == a
    same = diff(b, b)
    assert not any(
        getattr(same, f) for f in ("upsert_api_keys", "remove_api_keys", "upsert_routers")
    )


def test_diff_unit_roundtrip() -> None:
    old = pb.Snapshot(
        version=4,
        scope="s",
        upstreams=[pb.Upstream(name="u1"), pb.Upstream(name="u2")],
        models=[pb.Model(name="d1", upstream="u1")],
        routers=[pb.Router(name="m1", members=[pb.RouterMember(model="d1", weight=1)])],
        aliases=[pb.Alias(name="a1", target="d1"), pb.Alias(name="a2", target="m1")],
        teams=[pb.Team(id="t1", name="a")],
        api_keys=[pb.ApiKey(key_id="k1"), pb.ApiKey(key_id="k2", status=1)],
        required_capabilities=["x"],
    )
    new = pb.Snapshot(
        version=7,
        scope="s",
        created_at_unix_ms=123,
        upstreams=[pb.Upstream(name="u2", base_url="changed")],
        models=[
            pb.Model(name="d1", upstream="u2"),
            pb.Model(name="d2", upstream="u2"),
        ],
        routers=[],
        aliases=[pb.Alias(name="a1", target="d2"), pb.Alias(name="a3", target="d1")],
        teams=[pb.Team(id="t1", name="a")],
        api_keys=[pb.ApiKey(key_id="k2", status=3), pb.ApiKey(key_id="k3")],
    )
    d = diff(old, new)
    assert list(d.remove_upstreams) == ["u1"] and list(d.remove_routers) == ["m1"]
    assert list(d.remove_api_keys) == ["k1"] and not d.upsert_teams
    assert [a.name for a in d.upsert_aliases] == ["a1", "a3"]
    assert list(d.remove_aliases) == ["a2"]
    assert list(d.required_capabilities) == []
    assert apply_delta(old, d) == new
    with pytest.raises(ValueError):
        diff(old, pb.Snapshot(scope="other"))


# --------------------------------------------------------------------------- compaction


async def test_compaction(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        for v in range(1, 61):
            await store_version(s, SCOPE, pb.Snapshot(scope=SCOPE), version=v, source_revision=v)
        s.add(GatewaySession(scope=SCOPE, gateway_id="a", connected=True,
                             acked_version=3, pending_version=5))  # fmt: skip
        s.add(GatewaySession(scope=SCOPE, gateway_id="b", connected=True, hello_have_version=7))
    async with factory() as s, s.begin():
        assert await compact_versions(s, SCOPE, retain=50) == 10 - 3
    vs = {v.version: v for v in await versions(factory)}
    kept = {v for v, row in vs.items() if row.payload is not None}
    assert kept == {3, 5, 7, *range(11, 61)}
    assert all(vs[v].compacted_at is not None for v in set(vs) - kept)
    assert all(vs[v].catalog_state is None for v in set(vs) - kept)
    assert all(vs[v].catalog_state is not None for v in kept)
    assert all(len(row.digest) == 32 for row in vs.values())
    async with factory() as s, s.begin():
        assert await compact_versions(s, SCOPE, retain=50) == 0
        # retain is at least 1: the latest version is never compacted.
        await s.execute(update(GatewaySession).values(acked_version=None, pending_version=None,
                                                      hello_have_version=None))  # fmt: skip
        assert await compact_versions(s, SCOPE, retain=0) == 52
    assert [v.version for v in await versions(factory) if v.payload is not None] == [60]


@pytest.mark.parametrize("removed", [False, True])
async def test_compaction_releases_disconnected_or_removed_gateway(factory, removed) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        for v in range(1, 5):
            await store_version(s, SCOPE, pb.Snapshot(scope=SCOPE), version=v, source_revision=v)
        s.add(
            GatewaySession(
                scope=SCOPE,
                gateway_id="gone",
                connected=True,
                hello_have_version=1,
                acked_version=2,
                pending_version=3,
            )
        )
    async with factory() as s, s.begin():
        assert await compact_versions(s, SCOPE, retain=1) == 0
        gateway = await s.get(GatewaySession, (SCOPE, "gone"))
        if removed:
            await s.delete(gateway)
        else:
            gateway.connected = False
        await s.flush()
        assert await compact_versions(s, SCOPE, retain=1) == 3
    assert [v.version for v in await versions(factory) if v.payload is not None] == [4]


# --------------------------------------------------------------------------- loop


@pytest.mark.parametrize("require_local_membership", [True, False])
async def test_compiler_loop(factory, require_local_membership) -> None:
    await seed_scope(factory)
    stop = asyncio.Event()
    seen: list[tuple[str, int]] = []
    published = asyncio.Queue()
    async with factory() as s, s.begin():
        team = await make_team(s)
        user = await make_user(s)
        key = await make_key(s, team, user)

    def on_published(scope, version):
        seen.append((scope, version))
        published.put_nowait((scope, version))

    task = asyncio.create_task(
        run_compiler_loop(
            factory,
            stop,
            scope=SCOPE,
            interval_seconds=0.05,
            on_published=on_published,
            require_local_membership=require_local_membership,
        )  # fmt: skip
    )
    try:
        await touch(factory)
        assert await asyncio.wait_for(published.get(), 5) == (SCOPE, 1)
        async with factory() as s, s.begin():
            await add_router(s, "m", [("d", None)])
        await touch(factory)
        assert await asyncio.wait_for(published.get(), 5) == (SCOPE, 2)
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)
    assert seen == [(SCOPE, 1), (SCOPE, 2)]
    assert all(status == OutboxStatus.DONE.value for _, status, _ in await outbox_rows(factory))
    wire = next(k for k in (await load_version(factory, 2)).api_keys if k.key_id == str(key.id))
    assert wire.status == (
        pb.KEY_STATUS_SUSPENDED if require_local_membership else pb.KEY_STATUS_ACTIVE
    )


# --------------------------------------------------------------------------- metrics


async def test_compile_metrics(factory, monkeypatch) -> None:
    from lumewarp_control_plane.metrics import Metrics, refresh_db_gauges
    from lumewarp_control_plane.settings import get_settings

    metrics = Metrics()
    await seed_scope(factory)
    assert await publish(factory, metrics=metrics) is None
    # Nothing pending: no compile happened.
    assert metrics.value("lumewarp_cp_compile_duration_seconds_count") == 0

    async with factory() as s, s.begin():
        t, u = await add_hierarchy(s)
        await add_key(s, t, u, name="k1")
    await touch(factory)
    await refresh_db_gauges(metrics, factory, SCOPE)
    assert metrics.value("lumewarp_cp_outbox_pending") == 1
    assert await publish(factory, metrics=metrics) == 1
    assert metrics.value("lumewarp_cp_compile_duration_seconds_count") == 1
    assert metrics.value("lumewarp_cp_compile_duration_seconds_sum") > 0
    assert metrics.value("lumewarp_cp_compile_failures_total") == 0
    assert metrics.value("lumewarp_cp_config_version") == 1

    # Unchanged content is still a compile, but publishes nothing new.
    await touch(factory)
    assert await publish(factory, metrics=metrics) is None
    assert metrics.value("lumewarp_cp_compile_duration_seconds_count") == 2
    assert metrics.value("lumewarp_cp_config_version") == 1

    monkeypatch.setattr(get_settings(), "max_snapshot_keys", 1)
    async with factory() as s, s.begin():
        t, u = await add_hierarchy(s, name="second")
        await add_key(s, t, u, name="k2")
    await touch(factory)
    assert await publish(factory, metrics=metrics) is None
    assert metrics.value("lumewarp_cp_compile_failures_total") == 1
    assert metrics.value("lumewarp_cp_compile_duration_seconds_count") == 3
    assert metrics.value("lumewarp_cp_config_version") == 1

    await refresh_db_gauges(metrics, factory, SCOPE)
    assert metrics.value("lumewarp_cp_outbox_pending") == 0
    # The refresher also tracks versions published elsewhere (rollback via the API).
    metrics.config_version.set(0)
    await refresh_db_gauges(metrics, factory, SCOPE)
    assert metrics.value("lumewarp_cp_config_version") == 1


async def test_compiler_loop_publishes_metrics(factory) -> None:
    from lumewarp_control_plane.metrics import Metrics

    metrics = Metrics()
    await seed_scope(factory)
    async with factory() as s, s.begin():
        t, u = await add_hierarchy(s)
        await add_key(s, t, u)
    await touch(factory)
    stop = asyncio.Event()
    task = asyncio.create_task(
        run_compiler_loop(
            factory,
            stop,
            scope=SCOPE,
            interval_seconds=0.01,
            require_local_membership=True,
            metrics=metrics,
        )
    )
    try:
        async with asyncio.timeout(5):
            while metrics.value("lumewarp_cp_config_version") != 1:
                await asyncio.sleep(0.01)
    finally:
        stop.set()
        await task
    assert metrics.value("lumewarp_cp_compile_duration_seconds_count") >= 1
