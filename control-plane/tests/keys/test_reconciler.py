import asyncio
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from lumewarp_control_plane import compiler, outbox
from lumewarp_control_plane._proto.lumewarp.config.v1 import snapshot_pb2 as pb
from lumewarp_control_plane.api import keys
from lumewarp_control_plane.authz import (
    Authorizer,
    ChangeBatch,
    Decision,
    Relation,
    RequestAuthz,
)
from lumewarp_control_plane.authz.builtin import BuiltinBackend
from lumewarp_control_plane.keys import reconciler
from lumewarp_control_plane.models import (
    ActorType,
    ApiKey,
    AuditLog,
    ConfigOutbox,
    ConfigVersion,
    KeyLifecycle,
)
from lumewarp_control_plane.security import Principal, PrincipalKind
from lumewarp_control_plane.settings import Settings
from tests.conftest import migrated_database
from tests.factories import make_key, make_member, make_service_account, make_team, make_user, scope


@pytest.fixture
async def store():
    async with migrated_database() as url:
        engine = create_async_engine(url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session, session.begin():
                await scope(session)
                team = await make_team(session)
                other_team = await make_team(session)
                owner = await make_user(session)
                other = await make_user(session)
                account = await make_service_account(session, team)
                owned = await make_key(session, team, owner)
                second = await make_key(session, team, owner)
                elsewhere = await make_key(session, other_team, owner)
                unrelated = await make_key(session, team, other)
                service = await make_key(session, team, account)
                manual = await make_key(
                    session,
                    team,
                    owner,
                    lifecycle=KeyLifecycle.SUSPENDED,
                    suspended_reason="admin",
                )
                removed = await make_key(
                    session,
                    team,
                    owner,
                    lifecycle=KeyLifecycle.SUSPENDED,
                    suspended_reason="owner_removed",
                )
                revoked = await make_key(
                    session,
                    team,
                    owner,
                    lifecycle=KeyLifecycle.REVOKED,
                    revoked_at=datetime.now(UTC),
                )
            yield (
                factory,
                Settings(database_url=url, background_enabled=False),
                {
                    "team": team,
                    "other_team": other_team,
                    "owner": owner,
                    "other": other,
                    "owned": owned,
                    "second": second,
                    "elsewhere": elsewhere,
                    "unrelated": unrelated,
                    "service": service,
                    "manual": manual,
                    "removed": removed,
                    "revoked": revoked,
                },
            )
        finally:
            await engine.dispose()


class Backend:
    def __init__(self, decision=Decision.ALLOW):
        self.decision = decision
        self.overrides = {}
        self.calls = []

    async def check(self, user, relation, resource):
        self.calls.append((user, relation, resource))
        return self.overrides.get((user, resource.id), self.decision)


class ChangesBackend(Backend):
    def __init__(self, batches, decision=Decision.ALLOW):
        super().__init__(decision)
        self.batches = iter(batches)
        self.cursors = []

    async def changes(self, cursor):
        self.cursors.append(cursor)
        result = next(self.batches)
        if isinstance(result, Exception):
            raise result
        return result


async def states(factory):
    async with factory() as session:
        return {
            k.id: (k.lifecycle, k.suspended_reason) for k in await session.scalars(select(ApiKey))
        }


async def run_ticks(
    monkeypatch, factory, settings, backend, ticks, on_tick=None, full=300, metrics=None
):
    """Advance the loop's monotonic clock at its wait boundary, without wall-clock sleeps."""
    clock = 100.0
    count = 0
    delays = []
    stop = asyncio.Event()

    async def wait(event, seconds):
        nonlocal clock, count
        count += 1
        delays.append(seconds)
        if on_tick is not None:
            await on_tick(count)
        clock += seconds
        if count == ticks:
            event.set()

    monkeypatch.setattr(reconciler, "monotonic", lambda: clock)
    monkeypatch.setattr(reconciler, "_wait", wait)
    await reconciler.run_key_reconciler(
        factory,
        Authorizer(backend),
        stop,
        poll_seconds=2,
        full_seconds=full,
        settings=settings,
        **({"metrics": metrics} if metrics is not None else {}),
    )
    return delays


async def test_deny_suspends_only_matching_pair_and_publishes(store):
    factory, settings, rows = store
    backend = Backend()
    backend.overrides[(str(rows["owner"].id), str(rows["team"].id))] = Decision.DENY
    async with factory() as session, session.begin():
        await outbox.enqueue_recompile(session, settings.scope, "initial")
    assert (
        await compiler.publish_pending(
            factory,
            settings.scope,
            require_local_membership=False,
        )
        == 1
    )

    assert await reconciler.reconcile_keys(factory, Authorizer(backend), settings) == 2
    current = await states(factory)
    for name in ("owned", "second"):
        assert current[rows[name].id] == (KeyLifecycle.SUSPENDED, "owner_removed")
    for name in ("elsewhere", "unrelated", "service"):
        assert current[rows[name].id] == (KeyLifecycle.ACTIVE, None)
    assert current[rows["manual"].id] == (KeyLifecycle.SUSPENDED, "admin")
    assert current[rows["revoked"].id] == (KeyLifecycle.REVOKED, None)
    assert len(backend.calls) == 3  # Distinct (user, team), not one check per key.
    assert all(relation is Relation.MEMBER for _, relation, _ in backend.calls)
    async with factory() as session:
        audits = (await session.scalars(select(AuditLog))).all()
        assert len(audits) == 2
        assert {a.resource_id for a in audits} == {
            str(rows["owned"].id),
            str(rows["second"].id),
        }
        assert all(
            a.actor_type is ActorType.SYSTEM and a.actor_id == "key_reconciler" for a in audits
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ConfigOutbox)
                .where(ConfigOutbox.reason == "key.reconcile")
            )
            == 1
        )
    assert (
        await compiler.publish_pending(
            factory,
            settings.scope,
            require_local_membership=False,
        )
        == 2
    )
    async with factory() as session:
        version = await session.get(ConfigVersion, (settings.scope, 2))
        snapshot = pb.Snapshot.FromString(version.payload)
    published = {k.key_id: k.status for k in snapshot.api_keys}
    assert published[str(rows["owned"].id)] == pb.KEY_STATUS_SUSPENDED
    assert published[str(rows["service"].id)] == pb.KEY_STATUS_ACTIVE
    backend.overrides.clear()
    assert await reconciler.reconcile_keys(factory, Authorizer(backend), settings) == 0
    assert await states(factory) == current  # Membership recovery never auto-resumes.


@pytest.mark.parametrize("decision", [Decision.ALLOW, Decision.ERROR])
async def test_allow_and_repeated_errors_change_nothing(store, monkeypatch, decision):
    factory, settings, _ = store
    before = await states(factory)
    backend = Backend(decision)
    await run_ticks(monkeypatch, factory, settings, backend, 8, full=3)
    assert len(backend.calls) == 24
    assert await states(factory) == before
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(AuditLog)) == 0
        assert await session.scalar(select(func.count()).select_from(ConfigOutbox)) == 0


async def test_full_scan_slices_isolate_slow_backend(store):
    factory, settings, _ = store
    async with factory() as session, session.begin():
        team = await make_team(session)
        for _ in range(498):
            owner = await make_user(session)
            await make_key(session, team, owner)

    class SlicedBackend(Backend):
        async def batch_check(self, items):
            self.calls.append(items)
            if len(self.calls) == 1:
                await asyncio.Event().wait()
            return [Decision.DENY] * len(items)

    backend = SlicedBackend()
    async with asyncio.timeout(5):
        suspended = await reconciler.reconcile_keys(
            factory, Authorizer(backend, timeout=0.01), settings
        )
    assert [len(items) for items in backend.calls] == [500, 1]
    user_id, _, resource = backend.calls[-1][0]
    async with factory() as session:
        expected = await session.scalar(
            select(func.count())
            .select_from(ApiKey)
            .where(
                ApiKey.user_id == uuid.UUID(user_id),
                ApiKey.team_id == uuid.UUID(resource.id),
                ApiKey.suspended_reason == "owner_removed",
                ApiKey.id != store[2]["removed"].id,
            )
        )
        remaining = await session.scalar(
            select(func.count()).select_from(ApiKey).where(ApiKey.lifecycle == KeyLifecycle.ACTIVE)
        )
    assert suspended == expected
    assert suspended > 0
    assert remaining == 503 - suspended


async def test_keys_created_at_or_after_decision_are_not_suspended(store, monkeypatch):
    factory, settings, rows = store
    cutoff = datetime.now(UTC)

    class Clock:
        @staticmethod
        def now(tz):
            return cutoff

    original = reconciler.lock_scope
    fresh = []

    async def create_after_decisions(session, config):
        async with factory() as writer, writer.begin():
            for created_at in (cutoff, cutoff + timedelta(seconds=1)):
                fresh.append(
                    await make_key(writer, rows["team"], rows["owner"], created_at=created_at)
                )
        await original(session, config)

    monkeypatch.setattr(reconciler, "datetime", Clock)
    monkeypatch.setattr(reconciler, "lock_scope", create_after_decisions)
    assert (
        await reconciler.reconcile_keys(
            factory,
            Authorizer(Backend(Decision.DENY)),
            settings,
            user_ids={str(rows["owner"].id)},
        )
        == 3
    )
    current = await states(factory)
    assert all(current[key.id] == (KeyLifecycle.ACTIVE, None) for key in fresh)


async def test_inactive_local_owner_suspended_even_when_iam_allows(store):
    factory, settings, rows = store
    async with factory() as session, session.begin():
        user = await session.get(type(rows["owner"]), rows["owner"].id)
        user.is_active = False
    assert await reconciler.reconcile_keys(factory, Authorizer(Backend()), settings) == 3
    current = await states(factory)
    assert current[rows["elsewhere"].id] == (KeyLifecycle.SUSPENDED, "owner_removed")
    assert current[rows["service"].id] == (KeyLifecycle.ACTIVE, None)


async def test_changes_only_rechecks_listed_users_next_poll(store, monkeypatch):
    factory, settings, rows = store
    owner = str(rows["owner"].id)
    backend = ChangesBackend(
        [
            ChangeBatch(frozenset(), "first"),
            ChangeBatch(frozenset({owner}), "second"),
        ]
    )

    async def tick(number):
        if number == 1:
            backend.calls.clear()
            backend.decision = Decision.DENY
        else:
            assert {u for u, _, _ in backend.calls} == {owner}
            current = await states(factory)
            assert current[rows["owned"].id][0] is KeyLifecycle.SUSPENDED
            assert current[rows["unrelated"].id][0] is KeyLifecycle.ACTIVE

    delays = await run_ticks(monkeypatch, factory, settings, backend, 2, tick)
    assert delays == [2, 2]
    assert backend.cursors == [None, "first"]


async def test_invalid_changed_user_ids_do_not_abort_valid_reconciliation(store, caplog):
    factory, settings, rows = store
    with caplog.at_level("DEBUG", logger="lumewarp_control_plane.keys.reconciler"):
        suspended = await reconciler.reconcile_keys(
            factory,
            Authorizer(Backend(Decision.DENY)),
            settings,
            user_ids={str(rows["owner"].id), "not-a-uuid", "user:*", None},
        )
    assert suspended == 3
    assert len([r for r in caplog.records if r.levelname == "DEBUG"]) == 3
    assert (
        await reconciler.reconcile_keys(
            factory, Authorizer(Backend(Decision.DENY)), settings, user_ids={"invalid"}
        )
        == 0
    )


async def test_changes_reset_and_periodic_safety_scan(store, monkeypatch):
    factory, settings, _ = store
    backend = ChangesBackend(
        [
            ChangeBatch(frozenset(), "a"),
            ChangeBatch(frozenset(), "b", reset=True),
            ChangeBatch(frozenset(), "c"),
            ChangeBatch(frozenset(), "d"),
        ]
    )
    counts = []

    async def tick(_):
        counts.append(len(backend.calls))

    await run_ticks(monkeypatch, factory, settings, backend, 4, tick, full=3)
    assert counts == [3, 6, 6, 9]


async def test_changes_error_keeps_cursor_and_full_scan_still_runs(store, monkeypatch):
    factory, settings, _ = store
    backend = ChangesBackend(
        [
            ChangeBatch(frozenset(), "saved"),
            RuntimeError("IAM unavailable"),
            RuntimeError("IAM unavailable"),
            ChangeBatch(frozenset(), "recovered"),
        ]
    )
    await run_ticks(monkeypatch, factory, settings, backend, 4, full=3)
    assert backend.cursors == [None, "saved", "saved", "saved"]
    assert len(backend.calls) == 6


async def test_unexpected_failure_retries_without_advancing_cursor(store, monkeypatch):
    factory, settings, rows = store
    backend = ChangesBackend(
        [
            ChangeBatch(frozenset(), "a"),
            ChangeBatch(frozenset({str(rows["owner"].id)}), "b"),
            ChangeBatch(frozenset({str(rows["owner"].id)}), "b"),
        ]
    )
    original = reconciler._reconcile
    calls = 0

    async def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("temporary database failure")
        return await original(*args, **kwargs)

    monkeypatch.setattr(reconciler, "_reconcile", fail_once)
    delays = await run_ticks(monkeypatch, factory, settings, backend, 3)
    assert backend.cursors == [None, "a", "a"]
    assert delays == [2, 4, 2]
    assert calls == 3


async def test_builtin_backend_runs_safety_scan(store):
    factory, settings, rows = store
    async with factory() as session, session.begin():
        await make_member(session, rows["team"], rows["owner"])
    assert (
        await reconciler.reconcile_keys(
            factory,
            Authorizer(BuiltinBackend(factory)),
            settings,
        )
        == 2
    )
    current = await states(factory)
    assert current[rows["owned"].id][0] is KeyLifecycle.ACTIVE
    assert current[rows["elsewhere"].id][0] is KeyLifecycle.SUSPENDED


@pytest.mark.parametrize("failure", ["exception", "timeout"])
async def test_backend_failure_is_never_a_deny(store, failure):
    factory, settings, _ = store
    before = await states(factory)

    async def check(*args):
        if failure == "exception":
            raise RuntimeError("IAM unavailable")
        await asyncio.Event().wait()

    assert (
        await reconciler.reconcile_keys(
            factory,
            Authorizer(check, timeout=0.01),
            settings,
        )
        == 0
    )
    assert await states(factory) == before


@pytest.mark.parametrize("first", ["revoke", "reconcile"])
async def test_concurrent_revoke_does_not_deadlock_or_overwrite_state(store, monkeypatch, first):
    factory, settings, rows = store
    key_locked = asyncio.Event()
    second_attempted = asyncio.Event()
    original_key_scope = keys.lock_scope
    original_reconcile_scope = reconciler.lock_scope
    original_get_key = keys._get_key

    async def get_key(*args):
        row = await original_get_key(*args)
        if first == "revoke":
            key_locked.set()
            await second_attempted.wait()
        return row

    async def key_scope(session, config):
        if first == "reconcile":
            await key_locked.wait()
            second_attempted.set()
        await original_key_scope(session, config)

    async def reconcile_scope(session, config):
        if first == "revoke":
            await key_locked.wait()
            second_attempted.set()
        await original_reconcile_scope(session, config)
        if first == "reconcile":
            # Hold a key row as well, so both execution orders exercise contention.
            await session.get(ApiKey, rows["owned"].id, with_for_update=True)
            key_locked.set()
            await second_attempted.wait()

    monkeypatch.setattr(keys, "_get_key", get_key)
    monkeypatch.setattr(keys, "lock_scope", key_scope)
    monkeypatch.setattr(reconciler, "lock_scope", reconcile_scope)
    authorizer = Authorizer(BuiltinBackend(factory))
    authz = RequestAuthz(Principal(kind=PrincipalKind.BREAK_GLASS), authorizer)
    async with factory() as key_session, asyncio.timeout(10), asyncio.TaskGroup() as tasks:
        tasks.create_task(keys.revoke_key(rows["owned"].id, authz, key_session, settings))
        tasks.create_task(
            reconciler.reconcile_keys(factory, Authorizer(Backend(Decision.DENY)), settings)
        )
    current = await states(factory)
    assert current[rows["owned"].id] == (KeyLifecycle.REVOKED, None)
    async with factory() as session:
        audits = (
            await session.scalars(
                select(AuditLog).where(
                    AuditLog.action == "key.suspend",
                    AuditLog.resource_id == str(rows["owned"].id),
                )
            )
        ).all()
    assert len(audits) == (0 if first == "revoke" else 1)


async def test_stop_interrupts_wait():
    stop = asyncio.Event()
    task = asyncio.create_task(reconciler._wait(stop, 300))
    stop.set()
    await asyncio.wait_for(task, 1)


# --------------------------------------------------------------------------- metrics


async def test_suspension_metrics_count_committed_suspensions(store):
    from lumewarp_control_plane.metrics import Metrics

    factory, settings, rows = store
    metrics = Metrics()
    backend = Backend()
    backend.overrides[(str(rows["owner"].id), str(rows["team"].id))] = Decision.DENY
    assert await reconciler.reconcile_keys(factory, Authorizer(backend), settings, metrics=metrics)
    assert metrics.value("lumewarp_cp_keys_suspended_total", {"reason": "owner_removed"}) == 2
    # Nothing left to suspend: the counter does not move.
    await reconciler.reconcile_keys(factory, Authorizer(backend), settings, metrics=metrics)
    assert metrics.value("lumewarp_cp_keys_suspended_total", {"reason": "owner_removed"}) == 2


@pytest.mark.parametrize(
    ("decision", "succeeds"),
    [(Decision.ALLOW, True), (Decision.DENY, True), (Decision.ERROR, False)],
)
async def test_last_success_timestamp(store, monkeypatch, decision, succeeds):
    from lumewarp_control_plane.metrics import Metrics

    factory, settings, rows = store
    metrics = Metrics()
    before = time.time()
    await run_ticks(monkeypatch, factory, settings, Backend(decision), 2, metrics=metrics)
    stamp = metrics.value("lumewarp_cp_key_reconcile_last_success_timestamp_seconds")
    if succeeds:
        assert before <= stamp <= time.time()
    else:
        # Authorization unavailable for some owner: not a successful reconciliation.
        assert stamp == 0


async def test_last_success_requires_readable_change_stream(store, monkeypatch):
    from lumewarp_control_plane.metrics import Metrics

    factory, settings, rows = store
    metrics = Metrics()
    stamps = []

    async def tick(count):
        stamps.append(metrics.value("lumewarp_cp_key_reconcile_last_success_timestamp_seconds"))

    backend = ChangesBackend(
        [RuntimeError("changes down"), ChangeBatch(frozenset(), "a", reset=True)]
    )
    await run_ticks(monkeypatch, factory, settings, backend, 2, tick, metrics=metrics)
    # First step: changes() failed (the full scan still ran), so no success stamp.
    assert stamps[0] == 0
    assert stamps[1] > 0
