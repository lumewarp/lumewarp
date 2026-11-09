"""Catalog writers and rollback serialize on one lock order; malformed sidecars are inert.

The interleaving tests use two real connections. The first operation takes the scope row
lock and is held there until the second has started and is waiting on a lock; then the
first continues. A lock-order inversion shows up as a Postgres deadlock (DBAPIError), a
correct order as two operations that run one after the other.
"""

import asyncio
import copy
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from lumewarp_control_plane.api import config, routing
from lumewarp_control_plane.api.schemas import (
    ModelCreate,
    ModelUpdate,
    RollbackCreate,
)
from lumewarp_control_plane.compiler import publish_pending
from lumewarp_control_plane.compiler.catalog_state import capture_catalog_state
from lumewarp_control_plane.compiler.delta import diff
from lumewarp_control_plane.errors import ApiError
from lumewarp_control_plane.models import (
    Alias,
    AuditLog,
    CatalogName,
    CatalogNameHistory,
    ConfigOutbox,
    ConfigVersion,
    Model,
    ModelAccess,
    PolicyGrant,
    Router,
    Scope,
    Team,
    Upstream,
)
from lumewarp_control_plane.security import Principal, PrincipalKind
from lumewarp_control_plane.settings import Settings
from tests import factories
from tests.compiler.helpers import SCOPE, apply_delta, load_version, seed_scope, touch

SETTINGS = Settings(scope=SCOPE, dev_mode=True, break_glass_password="x", background_enabled=False)


def _authz():
    async def require(action):
        return None

    return SimpleNamespace(
        principal=Principal(kind=PrincipalKind.BREAK_GLASS),
        authorizer=SimpleNamespace(manages_grants=False),
        require=require,
    )


async def _publish(factory) -> int:
    await touch(factory)
    version = await publish_pending(factory, SCOPE, require_local_membership=True)
    assert version is not None
    return version


async def _wait_for_blocked_backend(factory) -> None:
    """Return once some other backend of this database waits on a lock (or after 3 s)."""
    async with factory() as session:
        for _ in range(60):
            blocked = await session.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
                    " AND wait_event_type = 'Lock'"
                )
            )
            if blocked:
                return
            await asyncio.sleep(0.05)


async def _interleave(factory, monkeypatch, first, second) -> list:
    """Run `first(session)` and `second(session)` on two connections.

    `first` is paused right after it takes the scope lock in routing.lock_scope. `second`
    only starts then. Returns both outcomes (results or raised exceptions), never raising.
    """
    locked, release = asyncio.Event(), asyncio.Event()
    original = routing.lock_scope
    sessions = {}

    async def lock(session, settings):
        await original(session, settings)
        if session is sessions["first"]:
            locked.set()
            await release.wait()

    monkeypatch.setattr(routing, "lock_scope", lock)

    async with factory() as one, factory() as two:
        sessions.update(first=one, second=two)

        async def run_first():
            return await first(one)

        async def run_second():
            await locked.wait()
            return await second(two)

        async with asyncio.timeout(20):
            t1 = asyncio.create_task(run_first())
            t2 = asyncio.create_task(run_second())
            await locked.wait()
            await asyncio.sleep(0.1)
            await _wait_for_blocked_backend(factory)
            release.set()
            outcomes = await asyncio.gather(t1, t2, return_exceptions=True)
    for outcome in outcomes:
        assert not isinstance(outcome, DBAPIError), f"database error (deadlock?): {outcome}"
        if isinstance(outcome, BaseException) and not isinstance(outcome, ApiError):
            raise outcome
    return outcomes


async def _seed_catalog(factory):
    await seed_scope(factory)
    async with factory() as session, session.begin():
        upstream = await factories.make_upstream(session, "u")
        model = await factories.make_model(session, "m", upstream)
        router = await factories.make_router(session, "r", [model])
        team = await factories.make_team(
            session, model_access=ModelAccess.LIST, allowed_models=["m", "r"]
        )
        ids = SimpleNamespace(model=model.id, router=router.id, team=team.id, upstream=upstream.id)
    return ids


async def _assert_rolled_back_state(factory, version_before: int, new_version: int) -> None:
    async with factory() as session:
        stored = await session.get(ConfigVersion, (SCOPE, version_before))
        assert await capture_catalog_state(session) == stored.catalog_state
    old, new = await load_version(factory, version_before), await load_version(factory, new_version)
    assert list(new.models) == list(old.models)
    assert list(new.routers) == list(old.routers)
    assert list(new.aliases) == list(old.aliases)
    assert list(new.upstreams) == list(old.upstreams)


async def test_rollback_and_rename_serialize(factory, monkeypatch):
    ids = await _seed_catalog(factory)
    v1 = await _publish(factory)
    authz = _authz()

    outcomes = await _interleave(
        factory,
        monkeypatch,
        lambda s: routing.update_model(ids.model, ModelUpdate(name="m2"), authz, s, SETTINGS),
        lambda s: config.rollback_config(RollbackCreate(version=v1), authz, s, SETTINGS),
    )
    renamed, rolled_back = outcomes
    assert not isinstance(renamed, BaseException) and renamed.name == "m2"
    assert rolled_back.version == v1 + 1

    # The rename ran first, rollback second: the name, grants and references are back.
    await _assert_rolled_back_state(factory, v1, rolled_back.version)
    async with factory() as session:
        assert await session.scalar(select(Model.name).where(Model.id == ids.model)) == "m"
        assert set(await session.scalars(select(PolicyGrant.name_id))) == {ids.model, ids.router}
        assert (await session.get(Team, ids.team)).allowed_models == ["m", "r"]
        history = await session.scalars(
            select(CatalogNameHistory.old_name).where(CatalogNameHistory.name_id == ids.model)
        )
        assert set(history) == {"m", "m2"}
        assert await session.scalar(select(func.count()).select_from(Model)) == 1


async def test_rollback_and_delete_serialize(factory, monkeypatch):
    ids = await _seed_catalog(factory)
    v1 = await _publish(factory)
    authz = _authz()
    async with factory() as session, session.begin():
        # Free the model for deletion: the router that references it goes first.
        await session.delete(await session.get(CatalogName, ids.router))

    outcomes = await _interleave(
        factory,
        monkeypatch,
        lambda s: routing.delete_model(ids.model, authz, s, SETTINGS),
        lambda s: config.rollback_config(RollbackCreate(version=v1), authz, s, SETTINGS),
    )
    deleted, rolled_back = outcomes
    assert not isinstance(deleted, BaseException) and deleted.status_code == 204
    assert rolled_back.version == v1 + 1

    # The delete ran first (its grant removal is audited); rollback recreated both items
    # under their old ids. Grants of recreated names do not come back.
    await _assert_rolled_back_state(factory, v1, rolled_back.version)
    async with factory() as session:
        assert await session.get(Model, ids.model) is not None
        assert await session.get(Router, ids.router) is not None
        assert await session.scalar(select(func.count()).select_from(PolicyGrant)) == 0
        assert (await session.get(Team, ids.team)).allowed_models == []
        audited = await session.scalars(
            select(AuditLog.details["name"].astext).where(AuditLog.action == "grant.delete")
        )
        assert list(audited) == ["m"]  # r was removed by setup, bypassing the API


async def test_rollback_first_blocks_a_rename(factory):
    """The mirror order: rollback holds the scope lock, a rename waits and then wins."""
    ids = await _seed_catalog(factory)
    v1 = await _publish(factory)
    authz = _authz()
    async with factory() as rolling, factory() as renaming:
        rollback_started = asyncio.Event()
        original = routing.lock_scope

        async def run_rollback():
            from lumewarp_control_plane import compiler

            async with rolling.begin():
                await compiler.rollback_routing(rolling, SCOPE, v1, require_local_membership=False)
                rollback_started.set()
                # Still inside the transaction, scope lock held.
                await asyncio.sleep(0.5)

        async def run_rename():
            await rollback_started.wait()
            return await routing.update_model(
                ids.model, ModelUpdate(name="m3"), authz, renaming, SETTINGS
            )

        assert original is routing.lock_scope
        async with asyncio.timeout(20):
            _, renamed = await asyncio.gather(run_rollback(), run_rename())
    assert renamed.name == "m3"
    async with factory() as session:
        assert await session.scalar(select(Model.name).where(Model.id == ids.model)) == "m3"


@pytest.mark.parametrize("create_first", [True, False])
async def test_create_model_and_delete_upstream_serialize(factory, monkeypatch, create_first):
    await seed_scope(factory)
    async with factory() as session, session.begin():
        upstream = await factories.make_upstream(session, "lonely")
        upstream_id = upstream.id
    authz = _authz()

    def create(session):
        body = ModelCreate(
            name="fresh", model_type="llm", upstream_id=upstream_id, upstream_model="x"
        )
        return routing.create_model(body, authz, session, SETTINGS)

    def delete(session):
        return routing.delete_upstream(upstream_id, authz, session, SETTINGS)

    first, second = (create, delete) if create_first else (delete, create)
    outcomes = await _interleave(factory, monkeypatch, first, second)
    created, deleted = outcomes if create_first else outcomes[::-1]

    async with factory() as session:
        names = await session.scalars(select(CatalogName.name))
        if create_first:
            # Created first, so the delete sees a referencing model.
            assert created.name == "fresh"
            assert isinstance(deleted, ApiError) and deleted.status == 409
            assert await session.get(Upstream, upstream_id) is not None
            assert list(names) == ["fresh"]
            assert await session.scalar(select(Model.upstream_id)) == upstream_id
        else:
            assert deleted.status_code == 204
            assert isinstance(created, ApiError) and created.status == 422
            assert await session.get(Upstream, upstream_id) is None
            assert list(names) == []
            assert await session.scalar(select(func.count()).select_from(Model)) == 0


# --------------------------------------------------------------------- malformed sidecars


def _bad_root_array(state):
    return []


def _bad_root_scalar(state):
    return 7


def _bad_numeric_uuid(state):
    state["tables"]["models"][0]["name_id"] = 12345
    return state


def _bad_numeric_datetime(state):
    state["tables"]["models"][0]["created_at"] = 1730000000
    return state


@pytest.mark.parametrize(
    "corrupt",
    [_bad_root_array, _bad_root_scalar, _bad_numeric_uuid, _bad_numeric_datetime],
)
async def test_malformed_sidecar_is_a_controlled_error_and_changes_nothing(factory, corrupt):
    ids = await _seed_catalog(factory)
    v1 = await _publish(factory)
    # A retained version whose sidecar is valid JSON of the wrong shape. The immutability
    # trigger forbids updating one, so insert a copy.
    async with factory() as session, session.begin():
        good = await session.get(ConfigVersion, (SCOPE, v1))
        bad_state = corrupt(copy.deepcopy(good.catalog_state))
        session.add(
            ConfigVersion(
                scope=SCOPE,
                version=v1 + 1,
                payload=good.payload,
                catalog_state=bad_state,
                digest=good.digest,
                size_bytes=good.size_bytes,
                key_count=good.key_count,
                source_revision=good.source_revision,
                kind=good.kind,
            )
        )
        # Change something so a half-applied restore would be visible.
        await session.delete(await session.get(CatalogName, ids.router))

    async def snapshot_of_everything():
        async with factory() as session:
            return {
                "catalog": await capture_catalog_state(session),
                "grants": sorted(
                    (str(g.name_id), str(g.team_id), str(g.user_id), str(g.service_account_id))
                    for g in await session.scalars(select(PolicyGrant))
                ),
                "audit": await session.scalar(select(func.count()).select_from(AuditLog)),
                "outbox": await session.scalar(select(func.count()).select_from(ConfigOutbox)),
                "revision": await session.scalar(select(Scope.source_revision)),
                "versions": await session.scalar(select(func.count()).select_from(ConfigVersion)),
                "history": await session.scalar(
                    select(func.count()).select_from(CatalogNameHistory)
                ),
                "aliases": await session.scalar(select(func.count()).select_from(Alias)),
            }

    before = await snapshot_of_everything()
    async with factory() as session:
        with pytest.raises(ApiError) as caught:
            await config.rollback_config(
                RollbackCreate(version=v1 + 1), _authz(), session, SETTINGS
            )
    assert caught.value.status == 409
    message = caught.value.message
    assert "12345" not in message and "1730000000" not in message
    assert await snapshot_of_everything() == before


# --------------------------------------------------------------------- delta contents


def _changed(old, new, delta) -> None:
    """The delta carries exactly the entities that differ, and applying it is exact."""
    for field, upsert, remove, key in (
        ("upstreams", "upsert_upstreams", "remove_upstreams", "name"),
        ("models", "upsert_models", "remove_models", "name"),
        ("routers", "upsert_routers", "remove_routers", "name"),
    ):
        before = {getattr(e, key): e for e in getattr(old, field)}
        after = {getattr(e, key): e for e in getattr(new, field)}
        assert {getattr(e, key) for e in getattr(delta, upsert)} == {
            k for k, v in after.items() if before.get(k) != v
        }
        assert set(getattr(delta, remove)) == set(before) - set(after)
    assert apply_delta(old, delta) == new


async def test_diff_after_rename_keep_old_alias_reuse_and_rollback(factory):
    ids = await _seed_catalog(factory)
    v1 = await _publish(factory)
    authz = _authz()
    a, b = await load_version(factory, v1), None

    # 1. Plain rename.
    async with factory() as session:
        await routing.update_model(ids.model, ModelUpdate(name="m2"), authz, session, SETTINGS)
    v2 = await _publish(factory)
    b = await load_version(factory, v2)
    delta = diff(a, b)
    assert "m2" in {m.name for m in delta.upsert_models}
    assert "m" in delta.remove_models
    # The router that references the model is affected too.
    assert "r" in {r.name for r in delta.upsert_routers}
    _changed(a, b, delta)

    # 2. Rename back, keeping the old name as an alias.
    async with factory() as session:
        await routing.update_model(
            ids.model,
            ModelUpdate(name="m3", keep_old_name_as_alias=True),
            authz,
            session,
            SETTINGS,
        )
    v3 = await _publish(factory)
    c = await load_version(factory, v3)
    delta = diff(b, c)
    assert "m3" in {m.name for m in delta.upsert_models}
    assert "m2" in delta.remove_models
    assert ("m2", "m3") in {(a.name, a.target) for a in delta.upsert_aliases}  # keeps the old name
    _changed(b, c, delta)

    # 3. The old name m is free again: reuse it for a different model.
    async with factory() as session:
        await routing.create_model(
            ModelCreate(
                name="m", model_type="llm", upstream_id=ids.upstream, upstream_model="other"
            ),
            authz,
            session,
            SETTINGS,
        )
    v4 = await _publish(factory)
    d = await load_version(factory, v4)
    delta = diff(c, d)
    assert "m" in {m.name for m in delta.upsert_models}
    _changed(c, d, delta)
    # Against v1, "m" is the same wire name for a different model: it must be an upsert.
    against_v1 = diff(a, d)
    assert "m" in {m.name for m in against_v1.upsert_models}
    _changed(a, d, against_v1)

    # 4. Rollback to v1: m2/m3 vanish, the original m is back.
    async with factory() as session:
        result = await config.rollback_config(RollbackCreate(version=v1), authz, session, SETTINGS)
    e = await load_version(factory, result.version)
    delta = diff(d, e)
    assert "m" in {m.name for m in delta.upsert_models}
    assert {"m3"} <= set(delta.remove_models)
    _changed(d, e, delta)
    assert list(e.models) == list(a.models)
