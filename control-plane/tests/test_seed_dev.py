"""The dev seed: deterministic events, the dev/loopback guard and the totals rebuild."""

import uuid
from collections import Counter
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane import usage
from lumewarp_control_plane._proto.lumewarp.usage.v1 import usage_pb2
from lumewarp_control_plane.models import (
    ScopeType,
    TrafficKind,
    UsageEvent,
    UsageTotal,
    User,
    member_scope_id,
)
from lumewarp_control_plane.seed_dev import (
    SeedKey,
    SeedRefused,
    _guard,
    _rebuild_totals,
    generate_events,
)
from lumewarp_control_plane.settings import DEV_DATABASE_URL, Settings
from tests import factories

END = datetime(2026, 11, 23, 15, 30, tzinfo=UTC)


def _settings(**overrides) -> Settings:
    values = {
        "database_url": DEV_DATABASE_URL,
        "dev_mode": True,
        "break_glass_password": "test-dev-password",
        "oidc_issuer": None,
    }
    values.update(overrides)
    return Settings(**values)


def _keys() -> list[SeedKey]:
    return [
        SeedKey(
            "default",
            "search",
            uuid.UUID(int=1),
            uuid.UUID(int=2),
            uuid.UUID(int=3),
            "search-chat",
        ),
        SeedKey(
            "prod",
            "ml-research",
            uuid.UUID(int=4),
            uuid.UUID(int=5),
            uuid.UUID(int=6),
            "research-batch",
        ),
    ]


def test_guard_refuses_without_dev_mode() -> None:
    with pytest.raises(SeedRefused):
        _guard(_settings(dev_mode=False))


def test_guard_refuses_a_non_loopback_database() -> None:
    with pytest.raises(SeedRefused):
        _guard(_settings(database_url="postgresql+asyncpg://u:p@10.1.2.3:5432/dev"))


def test_guard_accepts_the_dev_database() -> None:
    _guard(_settings())


def test_same_seed_produces_identical_events() -> None:
    first = generate_events(_keys(), seed=42, days=5, end=END)
    second = generate_events(_keys(), seed=42, days=5, end=END)
    assert [e.SerializeToString() for e in first["default"]] == [
        e.SerializeToString() for e in second["default"]
    ]
    assert [e.SerializeToString() for e in first["prod"]] == [
        e.SerializeToString() for e in second["prod"]
    ]
    # The other seed must differ, or determinism is trivially true.
    other = generate_events(_keys(), seed=7, days=5, end=END)
    assert [e.SerializeToString() for e in other["default"]] != [
        e.SerializeToString() for e in first["default"]
    ]


def test_generated_events_pass_ingest_validation() -> None:
    events = generate_events(_keys(), seed=42, days=5, end=END)
    seen = Counter()
    ids: set[str] = set()
    for zone, batch in events.items():
        for event in batch:
            # This is the exact per-event validation a gateway report runs.
            usage._prepare_event(f"seed-dev-{zone}", zone, event)
            assert event.request_id not in ids, "request ids must be unique"
            ids.add(event.request_id)
            seen[event.endpoint] += 1
            if event.kind == usage_pb2.TRAFFIC_KIND_LLM:
                if event.model:
                    seen["model:" + event.model] += 1
                if event.alias:
                    seen["alias"] += 1
                if event.router:
                    seen["router"] += 1
                if event.attempts > 1:
                    seen["retried"] += 1
                if event.fallback:
                    seen["fallback"] += 1
                if not event.cost_known:
                    seen["cost_unknown"] += 1
                if event.status == 429:
                    seen["429"] += 1
                elif event.status >= 500:
                    seen["5xx"] += 1
            else:
                seen["kind:" + ("mcp" if event.kind == usage_pb2.TRAFFIC_KIND_MCP else "a2a")] += 1

    assert seen["embeddings"] > 0
    assert seen["audio_transcriptions"] > 0
    assert seen["chat_completions"] > 0
    assert seen["model:llama-3.1-8b"] > 0
    assert seen["alias"] > 0
    assert seen["router"] > 0
    assert seen["fallback"] > 0
    assert seen["retried"] > 0
    assert seen["cost_unknown"] > 0
    assert seen["429"] > 0
    assert seen["5xx"] > 0
    assert seen["kind:mcp"] > 0
    assert seen["kind:a2a"] > 0
    # Some calls went through an alias or router, so the called name is not the served model.
    assert any(
        event.requested != event.model
        for batch in events.values()
        for event in batch
        if event.kind == usage_pb2.TRAFFIC_KIND_LLM and event.status < 400
    )


async def test_rebuild_totals_matches_the_remaining_events(session: AsyncSession) -> None:
    team = await factories.make_team(session, budget_nano_usd=10**10)
    account = await factories.make_service_account(session, team)
    key = await factories.make_key(session, team, account)
    keys = [SeedKey("default", team.name, team.id, key.id, account.id, account.name)]
    events = generate_events(keys, seed=7, days=4, end=END)["default"]

    for event in events:
        assert await usage.ingest(session, "seed-dev-default", [event], zone="default") == 1

    # A team outside the seed must keep its events and totals across the rebuild.
    other = await factories.make_team(session)
    other_account = await factories.make_service_account(session, other)
    other_key = await factories.make_key(session, other, other_account)
    other_events = generate_events(
        [
            SeedKey(
                "default",
                other.name,
                other.id,
                other_key.id,
                other_account.id,
                other_account.name,
            )
        ],
        seed=9,
        days=2,
        end=END,
    )["default"]
    for event in other_events:
        assert await usage.ingest(session, "seed-dev-default", [event], zone="default") == 1
    other_cost = sum(
        event.cost_nano_usd for event in other_events if event.kind == usage_pb2.TRAFFIC_KIND_LLM
    )

    assert await _rebuild_totals(session, {team.id}) > 0
    other_total = await session.scalar(
        select(UsageTotal).where(
            UsageTotal.zone == "default",
            UsageTotal.scope_type == ScopeType.TEAM,
            UsageTotal.scope_id == other.id,
        )
    )
    assert other_total is not None and other_total.cost_nano_usd == other_cost
    assert (
        await session.scalar(select(UsageEvent).where(UsageEvent.team_id == other.id).limit(1))
        is not None
    )
    remaining = (
        await session.scalars(
            select(UsageEvent).where(
                UsageEvent.team_id == team.id,
                UsageEvent.kind == TrafficKind.LLM,
            )
        )
    ).all()
    expected = {
        "cost_nano_usd": sum(e.cost_nano_usd for e in remaining),
        "prompt_tokens": sum(e.prompt_tokens for e in remaining),
        "completion_tokens": sum(e.completion_tokens for e in remaining),
        "request_count": len(remaining),
        "unknown_cost_count": sum(1 for e in remaining if not e.cost_known),
        "audio_ms": sum(e.audio_ms for e in remaining),
    }
    team_total = await session.scalar(
        select(UsageTotal).where(
            UsageTotal.zone == "default",
            UsageTotal.scope_type == ScopeType.TEAM,
            UsageTotal.scope_id == team.id,
        )
    )
    owner_total = await session.scalar(
        select(UsageTotal).where(
            UsageTotal.zone == "default",
            UsageTotal.scope_type == ScopeType.SERVICE_ACCOUNT,
            UsageTotal.scope_id == account.id,
        )
    )
    for total in (team_total, owner_total):
        assert total is not None
        for field, value in expected.items():
            assert getattr(total, field) == value


async def test_rebuild_totals_keys_member_rows_like_ingest(session: AsyncSession) -> None:
    """A user-owned key's budget row is per team (`member_scope_id`), as ingest writes it."""
    team = await factories.make_team(session, budget_nano_usd=10**10)
    member = await factories.make_member(session, team)
    user = await session.get(User, member.user_id)
    key = await factories.make_key(session, team, user)
    keys = [SeedKey("default", team.name, team.id, key.id, user.id, "member")]
    for event in generate_events(keys, seed=11, days=3, end=END)["default"]:
        assert await usage.ingest(session, "seed-dev-default", [event], zone="default") == 1

    def snapshot(rows):
        return sorted(
            (r.zone, r.scope_type, r.scope_id, r.window_start, r.cost_nano_usd, r.request_count)
            for r in rows
        )

    before = snapshot((await session.scalars(select(UsageTotal))).all())
    assert any(row[2] == member_scope_id(team.id, user.id) for row in before)
    await _rebuild_totals(session, {team.id})
    assert snapshot((await session.scalars(select(UsageTotal))).all()) == before
