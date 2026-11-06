"""The org budget: derived spend, exactly-one audit row, keys blocked after compile."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lumewarp_control_plane._proto.lumewarp.usage.v1 import usage_pb2
from lumewarp_control_plane.compiler import compile_scope
from lumewarp_control_plane.db import SessionFactory, create_session_factory
from lumewarp_control_plane.ids import uuid7
from lumewarp_control_plane.models import (
    ApiKey,
    AuditLog,
    Org,
    OrgBudgetWindow,
    Scope,
    ScopeType,
    UsageTotal,
)
from lumewarp_control_plane.usage import ingest
from tests import factories


def _event(key: ApiKey, *, cost: int) -> usage_pb2.UsageEvent:
    return usage_pb2.UsageEvent(
        request_id=str(uuid7()),
        timestamp_unix_ms=int(datetime.now(UTC).timestamp() * 1000),
        key_id=str(key.id),
        team_id=str(key.team_id),
        requested="test-requested",
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
    factory: SessionFactory, *, teams: int = 1, budget: int | None = 100
) -> tuple[str, Org, list[ApiKey]]:
    scope_name = f"org-budget-{uuid7().hex}"
    async with factory() as session, session.begin():
        session.add(Scope(name=scope_name))
        org = await factories.make_org(session, f"org-{uuid7().hex[-10:]}", budget_nano_usd=budget)
        keys: list[ApiKey] = []
        for index in range(teams):
            team = await factories.make_team(session, f"team-{index}-{uuid7().hex[-8:]}", org=org)
            account = await factories.make_service_account(session, team)
            keys.append(await factories.make_key(session, team, account, name=f"key-{index}"))
        return scope_name, org, keys


@pytest.fixture(scope="module")
async def engine() -> AsyncEngine:
    from tests.conftest import migrated_database

    async with migrated_database() as url:
        engine = create_async_engine(url)
        try:
            yield engine
        finally:
            await engine.dispose()


@pytest.fixture(scope="module")
async def session_factory(engine: AsyncEngine) -> SessionFactory:
    return create_session_factory(engine)


async def _report(session_factory: SessionFactory, scope: str, key: ApiKey, cost: int) -> int:
    async with session_factory() as session, session.begin():
        return await ingest(session, "gateway-a", [_event(key, cost=cost)], scope_name=scope)


@pytest.mark.asyncio
async def test_concurrent_teams_cross_the_org_budget_exactly_once(
    session_factory: SessionFactory,
) -> None:
    scope_name, org, keys = await _seed(session_factory, teams=3, budget=100)
    # Each batch alone is enough to cross the org budget; only one may report it.
    accepted = await asyncio.gather(
        *(_report(session_factory, scope_name, key, 120) for key in keys)
    )
    assert accepted == [1, 1, 1]

    async with session_factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(OrgBudgetWindow)
                .where(OrgBudgetWindow.org_id == org.id)
            )
            == 1
        )
        window = await session.scalar(
            select(OrgBudgetWindow).where(OrgBudgetWindow.org_id == org.id)
        )
        assert window is not None and window.exceeded_at is not None
        audits = (
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.action == "budget.hard_exceeded",
                        AuditLog.resource_type == ScopeType.ORG.value,
                        AuditLog.resource_id == str(org.id),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(audits) == 1
        # The org event is not attached to the team whose batch happened to cross it.
        assert audits[0].team_id is None and audits[0].org_id == org.id
        # Org spend is the sum of the teams' month totals; there is no org total row.
        assert (
            await session.scalar(
                select(func.count())
                .select_from(UsageTotal)
                .where(UsageTotal.scope_type == ScopeType.ORG)
            )
            == 0
        )
        compiled = await compile_scope(session, scope_name, require_local_membership=True)
    by_id = {wire.key_id: wire for wire in compiled.snapshot.api_keys}
    for key in keys:
        # KEY_STATUS_BUDGET_EXCEEDED arrives with the next compile after ingest.
        assert by_id[str(key.id)].status == 3


@pytest.mark.asyncio
async def test_an_org_without_a_budget_is_not_checked(session_factory: SessionFactory) -> None:
    scope_name, org, keys = await _seed(session_factory, teams=1, budget=None)
    assert await _report(session_factory, scope_name, keys[0], 10_000) == 1
    async with session_factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(OrgBudgetWindow)
                .where(OrgBudgetWindow.org_id == org.id)
            )
            == 0
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.action == "budget.hard_exceeded",
                    AuditLog.resource_type == ScopeType.ORG.value,
                    AuditLog.resource_id == str(org.id),
                )
            )
            == 0
        )
        compiled = await compile_scope(session, scope_name, require_local_membership=True)
    wire = next(k for k in compiled.snapshot.api_keys if k.key_id == str(keys[0].id))
    assert wire.status == 1  # KEY_STATUS_ACTIVE


@pytest.mark.asyncio
async def test_zero_budget_reports_once_across_batches(session_factory: SessionFactory) -> None:
    scope_name, org, keys = await _seed(session_factory, teams=1, budget=0)
    for _ in range(3):
        assert await _report(session_factory, scope_name, keys[0], 0) == 1
    async with session_factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(OrgBudgetWindow)
                .where(OrgBudgetWindow.org_id == org.id)
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.action == "budget.hard_exceeded",
                    AuditLog.resource_type == ScopeType.ORG.value,
                    AuditLog.resource_id == str(org.id),
                )
            )
            == 1
        )
        compiled = await compile_scope(session, scope_name, require_local_membership=True)
    wire = next(k for k in compiled.snapshot.api_keys if k.key_id == str(keys[0].id))
    assert wire.status == 3
