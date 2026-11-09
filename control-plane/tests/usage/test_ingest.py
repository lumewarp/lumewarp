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
    Scope,
    ScopeType,
    Team,
    TrafficKind,
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
    requested: str = "test-requested",
) -> usage_pb2.UsageEvent:
    timestamp = timestamp or datetime.now(UTC)
    return usage_pb2.UsageEvent(
        request_id=str(request_id or uuid7()),
        timestamp_unix_ms=int(timestamp.timestamp() * 1000),
        key_id=str(key.id),
        team_id=str(key.team_id),
        requested=requested,
        model="test-model",
        endpoint="chat_completions",
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
        )
        if owner_kind is KeyOwnerKind.USER:
            owner = await factories.make_user(session)
            await factories.make_member(session, team, owner)
        else:
            owner = await factories.make_service_account(session, team)
        key = await factories.make_key(session, team, owner)
        key_id = key.id
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
    event.endpoint = "audio_transcriptions"
    event.audio_ms = 1250

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
        assert (total.cost_nano_usd, total.request_count, total.audio_ms) == (123, 1, 1250)
        team_total = await session.scalar(
            select(UsageTotal).where(
                UsageTotal.scope_type == ScopeType.TEAM,
                UsageTotal.scope_id == key.team_id,
            )
        )
        assert team_total.audio_ms == 1250
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
    changed.requested = "different-requested"
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
        assert stored is not None and stored.requested == existing.requested
        total = await session.scalar(
            select(UsageTotal).where(
                UsageTotal.scope_type == key.owner_scope[0],
                UsageTotal.scope_id == key.owner_scope[1],
            )
        )
        assert total is not None and total.cost_nano_usd == 30


@pytest.mark.asyncio
async def test_concurrent_reports_flip_budget_once(session_factory: SessionFactory):
    scope_name, key = await _seed(session_factory, hard_budget=100)
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
    scope_name, key = await _seed(session_factory, hard_budget=1)
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
    bad_field.endpoint = ""
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
async def test_rejected_events_may_omit_requested_and_model(session_factory: SessionFactory):
    scope_name, key = await _seed(session_factory)
    completed = _event(key, cost=5)
    # Authentication rejects before the body is read: no requested, no model.
    unauthenticated = _event(key, cost=0, requested="")
    unauthenticated.model = ""
    # Policy rejections (403/429) know the requested but never chose a model.
    rate_limited = _event(key, cost=0)
    rate_limited.model = ""
    for rejected in (unauthenticated, rate_limited):
        rejected.status = 429
        rejected.prompt_tokens = rejected.completion_tokens = 0
        rejected.cost_known = False
        rejected.usage_state = usage_pb2.USAGE_STATE_MISSING
        rejected.outcome = usage_pb2.REQUEST_OUTCOME_REJECTED
    # Anything that reached an upstream must still name both.
    completed_without_model = _event(key, requested="")

    batch = [completed, unauthenticated, rate_limited, completed_without_model]
    async with session_factory() as session, session.begin():
        assert await ingest(session, "gateway-a", batch, scope_name=scope_name) == 3

    async with session_factory() as session:
        stored = {
            row.request_id: (row.requested, row.model)
            for row in (
                await session.execute(select(UsageEventRow).where(UsageEventRow.key_id == key.id))
            ).scalars()
        }
    assert stored == {
        uuid.UUID(completed.request_id): ("test-requested", "test-model"),
        uuid.UUID(unauthenticated.request_id): ("", ""),
        uuid.UUID(rate_limited.request_id): ("test-requested", ""),
    }


def _mcp_event(key: ApiKey, **values) -> usage_pb2.UsageEvent:
    """An MCP request: no model, requested, tokens or cost (usage.proto, TrafficKind)."""
    event = _event(key, cost=0, requested="")
    which = {
        "model": "",
        "alias": "",
        "router": "",
        "upstream": "",
        "upstream_model": "",
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cost_known": False,
        "kind": usage_pb2.TRAFFIC_KIND_MCP,
        "target": "s1",
        "method": "tools/call",
        "tool": "echo",
    }
    which.update(values)
    for field, value in which.items():
        setattr(event, field, value)
    return event


@pytest.mark.asyncio
async def test_an_mcp_event_is_stored_with_kind_target_method_and_tool(
    session_factory: SessionFactory,
):
    scope_name, key = await _seed(session_factory)
    event = _mcp_event(key)
    llm = _event(key, cost=5)
    async with session_factory() as session, session.begin():
        assert await ingest(session, "gateway-a", [event, llm], scope_name=scope_name) == 2

    async with session_factory() as session:
        stored = {
            row.request_id: row
            for row in (
                await session.execute(select(UsageEventRow).where(UsageEventRow.key_id == key.id))
            ).scalars()
        }
        totals = await session.scalar(
            select(UsageTotal).where(
                UsageTotal.scope_type == ScopeType.TEAM, UsageTotal.scope_id == key.team_id
            )
        )
    row = stored[uuid.UUID(event.request_id)]
    assert (row.kind, row.target, row.method, row.tool) == (
        TrafficKind.MCP,
        "s1",
        "tools/call",
        "echo",
    )
    assert (row.model, row.requested, row.cost_nano_usd) == ("", "", 0)
    assert stored[uuid.UUID(llm.request_id)].kind is TrafficKind.LLM
    # MCP is recorded but never aggregated: no budget, no cost, not a request total.
    assert totals is not None
    assert (totals.request_count, totals.cost_nano_usd) == (1, 5)


@pytest.mark.asyncio
async def test_a_non_llm_event_may_not_carry_llm_fields(session_factory: SessionFactory):
    scope_name, key = await _seed(session_factory)
    carried = [
        _mcp_event(key, model="gpt"),
        _mcp_event(key, requested="gpt"),
        _mcp_event(key, upstream="vllm"),
        _mcp_event(key, prompt_tokens=10),
        _mcp_event(key, cost_nano_usd=1, cost_known=True),
        _mcp_event(key, method="x" * 129),
        _mcp_event(key, tool="t" * 401),
    ]
    # An LLM event may not claim an MCP method, target or tool either.
    llm = _event(key)
    llm.method = "tools/call"
    carried.append(llm)
    async with session_factory() as session, session.begin():
        assert await ingest(session, "gateway-a", carried, scope_name=scope_name) == 0
    async with session_factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(UsageEventRow)
                .where(UsageEventRow.key_id == key.id)
            )
            == 0
        )


@pytest.mark.asyncio
async def test_an_unspecified_kind_is_an_llm_event(session_factory: SessionFactory):
    """An older gateway sends no kind at all: its events must stay LLM requests."""
    scope_name, key = await _seed(session_factory)
    event = _event(key)
    assert event.kind == usage_pb2.TRAFFIC_KIND_UNSPECIFIED
    async with session_factory() as session, session.begin():
        assert await ingest(session, "gateway-a", [event], scope_name=scope_name) == 1
    async with session_factory() as session:
        row = await session.scalar(
            select(UsageEventRow).where(UsageEventRow.request_id == uuid.UUID(event.request_id))
        )
    assert row is not None and row.kind is TrafficKind.LLM


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
async def test_late_usage_for_an_archived_team_still_attributes_owner_and_totals(
    session_factory: SessionFactory,
):
    """A report that lands after the team was archived still finds the team and its owner."""
    scope_name, key = await _seed(session_factory)
    async with session_factory() as session, session.begin():
        team = await session.get(Team, key.team_id)
        assert team is not None
        team.deleted_at = datetime.now(UTC)
    event = _event(key, cost=23)
    async with session_factory() as session, session.begin():
        assert await ingest(session, "gateway-archive", [event], scope_name=scope_name) == 1
    async with session_factory() as session:
        stored = await session.scalar(
            select(UsageEventRow).where(UsageEventRow.request_id == uuid.UUID(event.request_id))
        )
        assert stored is not None
        assert (stored.team_id, stored.owner_id) == (key.team_id, key.owner_id)
        total = await session.scalar(
            select(UsageTotal).where(
                UsageTotal.scope_type == ScopeType.TEAM,
                UsageTotal.scope_id == key.team_id,
            )
        )
        assert total is not None and total.cost_nano_usd == 23


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


def _skip_batch(key: ApiKey) -> list[usage_pb2.UsageEvent]:
    valid = _event(key, cost=7)
    wrong_team = _event(key)
    wrong_team.team_id = str(uuid7())
    unknown_key = _event(key)
    unknown_key.key_id = str(uuid7())
    bad_field = _event(key)
    bad_field.endpoint = ""
    bad_status = _event(key)
    bad_status.status = 600
    reused = usage_pb2.UsageEvent.FromString(valid.SerializeToString())
    reused.cost_nano_usd = 100
    duplicate = usage_pb2.UsageEvent.FromString(valid.SerializeToString())
    return [valid, wrong_team, unknown_key, bad_field, bad_status, reused, duplicate]


@pytest.mark.asyncio
async def test_ingest_reports_bounded_skip_reasons(session_factory: SessionFactory):
    from collections import Counter

    from lumewarp_control_plane.usage import SKIP_REASONS

    scope_name, key = await _seed(session_factory)
    skipped: Counter[str] = Counter()
    async with session_factory() as session, session.begin():
        accepted = await ingest(
            session, "gateway-a", _skip_batch(key), scope_name=scope_name, skipped=skipped
        )
    assert accepted == 2
    assert skipped == {"key_mismatch": 2, "invalid_event": 2, "request_id_conflict": 1}
    assert set(skipped) <= set(SKIP_REASONS)

    # A different stored event under a known request id is a conflict, too.
    stored = _event(key)
    async with session_factory() as session, session.begin():
        await ingest(session, "gateway-a", [stored], scope_name=scope_name)
    changed = usage_pb2.UsageEvent.FromString(stored.SerializeToString())
    changed.cost_nano_usd = 99
    skipped.clear()
    async with session_factory() as session, session.begin():
        accepted = await ingest(
            session, "gateway-a", [changed], scope_name=scope_name, skipped=skipped
        )
    assert accepted == 0
    assert skipped == {"request_id_conflict": 1}


@pytest.mark.asyncio
async def test_usage_report_metrics_count_committed_events(
    session_factory: SessionFactory, monkeypatch
):
    from lumewarp_control_plane import grpc as grpc_module
    from lumewarp_control_plane.metrics import Metrics
    from lumewarp_control_plane.settings import Settings

    scope_name, key = await _seed(session_factory)
    metrics = Metrics()
    monkeypatch.setattr(
        grpc_module,
        "_identity",
        lambda: grpc_module.GatewayIdentity(uuid7(), "gateway-a", scope_name),
    )

    class Aborted(Exception):
        pass

    class Context:
        async def abort(self, code, details):
            raise Aborted(details)

    service = grpc_module._UsageService(Settings(scope=scope_name), session_factory, metrics)
    response = await service.Report(
        usage_pb2.ReportRequest(gateway_id="gateway-a", events=_skip_batch(key)), Context()
    )
    assert response.accepted == 2

    def skipped(reason):
        return metrics.value("lumewarp_cp_usage_events_skipped_total", {"reason": reason})

    assert metrics.value("lumewarp_cp_usage_events_ingested_total") == 2
    assert (skipped("key_mismatch"), skipped("invalid_event")) == (2, 2)
    assert skipped("request_id_conflict") == 1

    # A batch-level rejection commits nothing and counts nothing.
    too_many = [_event(key) for _ in range(3)]
    small = grpc_module._UsageService(
        Settings(scope=scope_name, max_usage_batch_events=2), session_factory, metrics
    )
    with pytest.raises(Aborted):
        await small.Report(
            usage_pb2.ReportRequest(gateway_id="gateway-a", events=too_many), Context()
        )
    assert metrics.value("lumewarp_cp_usage_events_ingested_total") == 2

    # A failed commit counts nothing either.
    async def failing_ingest(session, *args, skipped=None, **kwargs):
        skipped["invalid_event"] += 1
        raise RuntimeError("database down")

    monkeypatch.setattr(grpc_module, "ingest", failing_ingest)
    with pytest.raises(RuntimeError):
        await service.Report(
            usage_pb2.ReportRequest(gateway_id="gateway-a", events=[_event(key)]), Context()
        )
    assert metrics.value("lumewarp_cp_usage_events_ingested_total") == 2
    assert skipped("invalid_event") == 2


async def test_provenance_fields_are_stored_with_rejected_requests(
    session_factory: SessionFactory,
):
    scope_name, key = await _seed(session_factory)
    event = _event(key)
    event.alias = "nick"
    event.router = "pool"
    event.upstream = "up"
    event.upstream_model = "real"
    event.fallback = True
    event.attempts = 2
    # Rejected events name what the client sent, however odd, within bounds.
    rejected = _event(key, cost=0, requested="n" * 400)
    rejected.model = ""
    rejected.status = 404
    rejected.prompt_tokens = rejected.completion_tokens = 0
    rejected.cost_known = False
    rejected.usage_state = usage_pb2.USAGE_STATE_MISSING
    rejected.outcome = usage_pb2.REQUEST_OUTCOME_REJECTED
    batch = [event, rejected]
    async with session_factory() as session, session.begin():
        assert await ingest(session, "gateway-a", batch, scope_name=scope_name) == 2
        row = await session.scalar(
            select(UsageEventRow).where(UsageEventRow.request_id == uuid.UUID(event.request_id))
        )
        assert (row.alias, row.router, row.upstream, row.upstream_model) == (
            "nick",
            "pool",
            "up",
            "real",
        )
        assert (row.fallback, row.attempts) == (True, 2)
        rejected_row = await session.scalar(
            select(UsageEventRow).where(UsageEventRow.request_id == uuid.UUID(rejected.request_id))
        )
        assert (rejected_row.requested, rejected_row.model) == ("n" * 400, "")
        assert (rejected_row.model_id, rejected_row.alias_id, rejected_row.router_id) == (
            None,
            None,
            None,
        )
