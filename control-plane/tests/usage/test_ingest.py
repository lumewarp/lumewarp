from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lumewarp_control_plane import outbox
from lumewarp_control_plane._proto.lumewarp.usage.v1 import usage_pb2
from lumewarp_control_plane.db import SessionFactory, create_session_factory
from lumewarp_control_plane.ids import uuid7
from lumewarp_control_plane.models import (
    ApiKey,
    ConfigOutbox,
    KeyOwnerKind,
    NotifierChannel,
    NotifierOutbox,
    Scope,
    ScopeType,
    Team,
    UsageTotal,
)
from lumewarp_control_plane.models import (
    UsageEvent as UsageEventRow,
)
from lumewarp_control_plane.usage import InvalidUsageBatch, ingest, rollover_scheduler
from lumewarp_control_plane.windows import month_window
from tests import factories


def _event(
    key: ApiKey,
    *,
    request_id: uuid.UUID | None = None,
    timestamp: datetime | None = None,
    cost: int = 1,
    model: str = "test-model",
) -> usage_pb2.UsageEvent:
    timestamp = timestamp or datetime.now(UTC)
    return usage_pb2.UsageEvent(
        request_id=str(request_id or uuid7()),
        timestamp_unix_ms=int(timestamp.timestamp() * 1000),
        key_id=str(key.id),
        team_id=str(key.team_id),
        model=model,
        deployment="test-deployment",
        route="chat_completions",
        status=200,
        prompt_tokens=10,
        completion_tokens=5,
        cost_nano_usd=cost,
        cost_known=True,
        duration_ms=10,
        config_version=1,
        usage_state=usage_pb2.USAGE_STATE_REPORTED,
        outcome=usage_pb2.REQUEST_OUTCOME_COMPLETED,
    )


async def _seed(
    factory: SessionFactory,
    *,
    hard_budget: int | None = None,
    soft_budget: int | None = None,
    webhook_url: str | None = None,
    owner_kind: KeyOwnerKind = KeyOwnerKind.USER,
) -> tuple[str, ApiKey]:
    scope_name = f"usage-{uuid7().hex}"
    suffix = uuid7().hex[-12:]
    async with factory() as session, session.begin():
        session.add(Scope(name=scope_name))
        team = await factories.make_team(
            session,
            name=f"usage-team-{suffix}",
            budget_nano_usd=hard_budget,
            soft_budget_nano_usd=soft_budget,
        )
        if owner_kind is KeyOwnerKind.USER:
            owner = await factories.make_user(session)
            await factories.make_member(session, team, owner)
        else:
            owner = await factories.make_service_account(session, team)
        key = await factories.make_key(session, team, owner)
        key_id = key.id
        if webhook_url is not None:
            session.add(
                NotifierChannel(
                    name=f"usage-webhook-{suffix}",
                    kind="webhook",
                    team_id=team.id,
                    config={"url": webhook_url},
                )
            )
    async with factory() as session:
        key = await session.get(ApiKey, key_id)
        assert key is not None
        return scope_name, key


@pytest.fixture(scope="module")
async def c4_engine():
    from tests.conftest import migrated_database

    async with migrated_database() as url:
        engine = create_async_engine(url)
        try:
            yield engine
        finally:
            await engine.dispose()


@pytest.fixture(scope="module")
async def session_factory(c4_engine: AsyncEngine) -> SessionFactory:
    return create_session_factory(c4_engine)


@pytest.mark.asyncio
async def test_duplicate_batch_replay_counts_once(session_factory: SessionFactory):
    scope_name, key = await _seed(session_factory)
    event = _event(key, request_id=uuid7(), cost=123)

    for _ in range(2):
        async with session_factory() as session, session.begin():
            assert await ingest(session, "gateway-a", [event], scope_name=scope_name) == 1

    async with session_factory() as session:
        total = await session.scalar(
            select(UsageTotal).where(
                UsageTotal.scope_type == key.owner_scope[0],
                UsageTotal.scope_id == key.owner_scope[1],
            )
        )
        assert total is not None
        assert (total.cost_nano_usd, total.request_count) == (123, 1)
        assert (
            await session.scalar(
                select(func.count())
                .select_from(UsageEventRow)
                .where(UsageEventRow.request_id == uuid.UUID(event.request_id))
            )
            == 1
        )

    changed = usage_pb2.UsageEvent.FromString(event.SerializeToString())
    changed.cost_nano_usd += 1
    async with session_factory() as session, session.begin():
        assert await ingest(session, "gateway-a", [changed], scope_name=scope_name) == 0
    async with session_factory() as session:
        stored = await session.scalar(
            select(UsageEventRow).where(UsageEventRow.request_id == uuid.UUID(event.request_id))
        )
        assert stored is not None and stored.cost_nano_usd == event.cost_nano_usd


@pytest.mark.asyncio
async def test_conflicting_replay_skips_only_that_event(session_factory: SessionFactory):
    scope_name, key = await _seed(session_factory)
    existing = _event(key, cost=10)
    async with session_factory() as session, session.begin():
        await ingest(session, "gateway-a", [existing], scope_name=scope_name)

    changed = usage_pb2.UsageEvent.FromString(existing.SerializeToString())
    changed.model = "different-model"
    other = _event(key, cost=20)
    async with session_factory() as session, session.begin():
        assert await ingest(session, "gateway-a", [other, changed], scope_name=scope_name) == 1

    async with session_factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(UsageEventRow)
                .where(UsageEventRow.request_id == uuid.UUID(other.request_id))
            )
            == 1
        )
        stored = await session.scalar(
            select(UsageEventRow).where(UsageEventRow.request_id == uuid.UUID(existing.request_id))
        )
        assert stored is not None and stored.model == existing.model
        total = await session.scalar(
            select(UsageTotal).where(
                UsageTotal.scope_type == key.owner_scope[0],
                UsageTotal.scope_id == key.owner_scope[1],
            )
        )
        assert total is not None and total.cost_nano_usd == 30


@pytest.mark.asyncio
async def test_concurrent_reports_flip_budget_once(session_factory: SessionFactory):
    scope_name, key = await _seed(
        session_factory,
        hard_budget=100,
        soft_budget=50,
        webhook_url="https://notify.example.test/budget",
    )
    events = [_event(key, cost=60), _event(key, cost=60)]

    async def report(event: usage_pb2.UsageEvent) -> int:
        async with session_factory() as session, session.begin():
            return await ingest(session, "gateway-a", [event], scope_name=scope_name)

    assert await asyncio.gather(*(report(event) for event in events)) == [1, 1]
    async with session_factory() as session:
        total = await session.scalar(
            select(UsageTotal).where(
                UsageTotal.scope_type == ScopeType.TEAM,
                UsageTotal.scope_id == key.team_id,
            )
        )
        assert total is not None
        assert total.cost_nano_usd == 120
        assert total.exceeded_at is not None
        assert total.soft_notified_at is not None
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ConfigOutbox)
                .where(
                    ConfigOutbox.scope == scope_name,
                    ConfigOutbox.reason == "usage budget exceeded",
                )
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(NotifierOutbox)
                .where(
                    NotifierOutbox.event_type.in_(("budget.soft", "budget.hard")),
                    NotifierOutbox.payload["team_id"].astext == str(key.team_id),
                )
            )
            == 2
        )


@pytest.mark.asyncio
async def test_budget_raise_crossing_recompiles_again(session_factory: SessionFactory):
    scope_name, key = await _seed(session_factory, hard_budget=100)
    first = _event(key, cost=120)
    async with session_factory() as session, session.begin():
        await ingest(session, "gateway-a", [first], scope_name=scope_name)

    async with session_factory() as session, session.begin():
        team = await session.get(Team, key.team_id)
        assert team is not None
        team.budget_nano_usd = 200
        await outbox.enqueue_recompile(session, scope_name, "api.budget.update")

    second = _event(key, cost=100)
    async with session_factory() as session, session.begin():
        await ingest(session, "gateway-a", [second], scope_name=scope_name)

    async with session_factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ConfigOutbox)
                .where(
                    ConfigOutbox.scope == scope_name,
                    ConfigOutbox.reason.in_(("usage budget exceeded", "api.budget.update")),
                )
            )
            == 3
        )


@pytest.mark.asyncio
async def test_late_previous_month_event_uses_its_event_window(session_factory: SessionFactory):
    scope_name, key = await _seed(
        session_factory,
        hard_budget=1,
        soft_budget=1,
        webhook_url="https://notify.example.test/budget",
    )
    current_start, _ = month_window(datetime.now(UTC))
    previous = current_start - timedelta(milliseconds=1)
    event = _event(key, timestamp=previous, cost=2)

    async with session_factory() as session, session.begin():
        assert await ingest(session, "gateway-a", [event], scope_name=scope_name) == 1

    previous_window = month_window(previous)[0]
    async with session_factory() as session:
        total = await session.scalar(
            select(UsageTotal).where(
                UsageTotal.scope_type == ScopeType.TEAM,
                UsageTotal.scope_id == key.team_id,
                UsageTotal.window_start == previous_window,
            )
        )
        assert total is not None and total.cost_nano_usd == 2
        assert total.exceeded_at is not None
        assert (
            await session.scalar(
                select(UsageTotal).where(
                    UsageTotal.scope_type == ScopeType.TEAM,
                    UsageTotal.scope_id == key.team_id,
                    UsageTotal.window_start == current_start,
                )
            )
            is None
        )
        # An over-budget late event for an old window must not block the current month.
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ConfigOutbox)
                .where(
                    ConfigOutbox.scope == scope_name,
                    ConfigOutbox.reason == "usage budget exceeded",
                )
            )
            == 0
        )
        # The old window is marked and audited, but no "exceeded" notification goes out.
        assert (
            await session.scalar(
                select(func.count())
                .select_from(NotifierOutbox)
                .where(NotifierOutbox.payload["team_id"].astext == str(key.team_id))
            )
            == 0
        )


@pytest.mark.asyncio
async def test_invalid_events_do_not_cost_the_rest_of_the_batch(
    session_factory: SessionFactory,
):
    scope_name, key = await _seed(session_factory)
    valid = _event(key, cost=7)
    wrong_team = _event(key, cost=100)
    wrong_team.team_id = str(uuid7())
    unknown_key = _event(key, cost=100)
    unknown_key.key_id = str(uuid7())
    bad_field = _event(key, cost=100)
    bad_field.route = ""
    reused = usage_pb2.UsageEvent.FromString(valid.SerializeToString())
    reused.cost_nano_usd = 100
    duplicate = usage_pb2.UsageEvent.FromString(valid.SerializeToString())
    batch = [valid, wrong_team, unknown_key, bad_field, reused, duplicate]

    async with session_factory() as session, session.begin():
        # valid and its identical duplicate count; the four invalid events are skipped.
        assert await ingest(session, "gateway-a", batch, scope_name=scope_name) == 2

    async with session_factory() as session:
        rows = (
            (await session.execute(select(UsageEventRow).where(UsageEventRow.key_id == key.id)))
            .scalars()
            .all()
        )
        assert [row.request_id for row in rows] == [uuid.UUID(valid.request_id)]
        assert rows[0].cost_nano_usd == 7
        totals = (
            await session.execute(
                select(UsageTotal.scope_type, UsageTotal.cost_nano_usd).where(
                    UsageTotal.scope_id.in_((key.owner_scope[1], key.team_id))
                )
            )
        ).all()
        assert sorted(cost for _, cost in totals) == [7, 7]


@pytest.mark.asyncio
async def test_rejected_events_may_omit_model_and_deployment(session_factory: SessionFactory):
    scope_name, key = await _seed(session_factory)
    completed = _event(key, cost=5)
    # Authentication rejects before the body is read: no model, no deployment.
    unauthenticated = _event(key, cost=0, model="")
    unauthenticated.deployment = ""
    # Policy rejections (403/429) know the model but never chose a deployment.
    rate_limited = _event(key, cost=0)
    rate_limited.deployment = ""
    for rejected in (unauthenticated, rate_limited):
        rejected.status = 429
        rejected.prompt_tokens = rejected.completion_tokens = 0
        rejected.cost_known = False
        rejected.usage_state = usage_pb2.USAGE_STATE_MISSING
        rejected.outcome = usage_pb2.REQUEST_OUTCOME_REJECTED
    # Anything that reached an upstream must still name both.
    completed_without_model = _event(key, model="")

    batch = [completed, unauthenticated, rate_limited, completed_without_model]
    async with session_factory() as session, session.begin():
        assert await ingest(session, "gateway-a", batch, scope_name=scope_name) == 3

    async with session_factory() as session:
        stored = {
            row.request_id: (row.model, row.deployment)
            for row in (
                await session.execute(select(UsageEventRow).where(UsageEventRow.key_id == key.id))
            ).scalars()
        }
    assert stored == {
        uuid.UUID(completed.request_id): ("test-model", "test-deployment"),
        uuid.UUID(unauthenticated.request_id): ("", ""),
        uuid.UUID(rate_limited.request_id): ("test-model", ""),
    }


@pytest.mark.asyncio
async def test_monthly_rollover_runs_on_startup(session_factory: SessionFactory, monkeypatch):
    from lumewarp_control_plane import usage

    scope_name = f"rollover-{uuid7().hex}"
    async with session_factory() as session, session.begin():
        session.add(Scope(name=scope_name))
    from lumewarp_control_plane.settings import Settings

    rolled_over = asyncio.Event()
    original = usage._rollover_once

    async def rollover(*args):
        await original(*args)
        rolled_over.set()

    monkeypatch.setattr(usage, "_rollover_once", rollover)
    task = asyncio.create_task(rollover_scheduler(Settings(scope=scope_name), session_factory))
    try:
        async with asyncio.timeout(2):
            await rolled_over.wait()
        async with session_factory() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(ConfigOutbox)
                .where(
                    ConfigOutbox.scope == scope_name,
                    ConfigOutbox.reason == "monthly budget rollover",
                )
            )
        assert count == 1
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.parametrize("owner_kind", list(KeyOwnerKind))
async def test_key_supplies_owner_and_exactly_two_totals(session_factory, owner_kind):
    scope_name, key = await _seed(session_factory, owner_kind=owner_kind)
    event = _event(key, cost=17)
    async with session_factory() as session, session.begin():
        assert await ingest(session, "gateway-owner", [event], scope_name=scope_name) == 1
    async with session_factory() as session:
        stored = await session.scalar(
            select(UsageEventRow).where(UsageEventRow.request_id == uuid.UUID(event.request_id))
        )
        assert (stored.owner_kind, stored.owner_id) == (key.owner_kind, key.owner_id)
        assert stored.gateway_id == "gateway-owner"
        totals = list(
            await session.scalars(
                select(UsageTotal).where(UsageTotal.scope_id.in_([key.team_id, key.owner_scope[1]]))
            )
        )
        assert {(t.scope_type, t.scope_id) for t in totals} == {
            (ScopeType.TEAM, key.team_id),
            key.owner_scope,
        }
        assert all(
            (
                t.cost_nano_usd,
                t.prompt_tokens,
                t.completion_tokens,
                t.request_count,
                t.unknown_cost_count,
            )
            == (17, 10, 5, 1, 0)
            for t in totals
        )


@pytest.mark.asyncio
async def test_batch_at_configured_maximum_is_ingested(session_factory: SessionFactory):
    from lumewarp_control_plane.settings import Settings

    maximum = Settings().max_usage_batch_events
    scope_name, key = await _seed(session_factory)
    events = [_event(key, cost=1) for _ in range(maximum)]
    # Reverse order: the insert sorts by request_id anyway.
    events.reverse()
    async with session_factory() as session, session.begin():
        assert (
            await ingest(
                session, "gateway-a", events, scope_name=scope_name, max_batch_events=maximum
            )
            == maximum
        )
    async with session_factory() as session:
        total = await session.scalar(
            select(UsageTotal).where(
                UsageTotal.scope_type == key.owner_scope[0],
                UsageTotal.scope_id == key.owner_scope[1],
            )
        )
        assert total is not None
        assert (total.cost_nano_usd, total.request_count) == (maximum, maximum)
    # Replaying the full batch is idempotent.
    async with session_factory() as session, session.begin():
        assert (
            await ingest(
                session, "gateway-a", events, scope_name=scope_name, max_batch_events=maximum
            )
            == maximum
        )
    with pytest.raises(InvalidUsageBatch, match="maximum"):
        async with session_factory() as session, session.begin():
            await ingest(
                session,
                "gateway-a",
                [*events, _event(key)],
                scope_name=scope_name,
                max_batch_events=maximum,
            )


def test_batch_cap_stays_under_driver_parameter_limit():
    from pydantic import ValidationError

    from lumewarp_control_plane.settings import Settings

    with pytest.raises(ValidationError):
        Settings(max_usage_batch_events=10_001)


@pytest.mark.asyncio
async def test_zero_budget_recompiles_once_for_zero_cost_batches(session_factory: SessionFactory):
    scope_name, key = await _seed(session_factory, hard_budget=0)
    for _ in range(3):
        event = _event(key, cost=0)
        event.cost_known = False
        event.usage_state = usage_pb2.USAGE_STATE_MISSING
        async with session_factory() as session, session.begin():
            await ingest(session, "gateway-a", [event], scope_name=scope_name)
    async with session_factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ConfigOutbox)
                .where(
                    ConfigOutbox.scope == scope_name,
                    ConfigOutbox.reason == "usage budget exceeded",
                )
            )
            == 1
        )
