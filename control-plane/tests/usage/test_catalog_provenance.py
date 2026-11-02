"""Real ingest coverage for catalog provenance, delayed delivery and rename intervals."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event as sa_event
from sqlalchemy import select

from lumewarp_control_plane._proto.lumewarp.usage.v1 import usage_pb2
from lumewarp_control_plane.models import (
    CatalogName,
    CatalogNameHistory,
    ScopeType,
    UsageEvent,
    UsageTotal,
)
from lumewarp_control_plane.usage import ingest
from tests import factories

CREATED = datetime(2026, 3, 1, tzinfo=UTC)
TS = CREATED + timedelta(days=1)
RENAMED = CREATED + timedelta(days=2)


def wire(key, *, timestamp=TS, **fields):
    values = dict(
        request_id=str(uuid.uuid4()),
        timestamp_unix_ms=int(timestamp.timestamp() * 1000),
        key_id=str(key.id),
        team_id=str(key.team_id),
        requested="public",
        model="served",
        endpoint="chat_completions",
        status=200,
        prompt_tokens=10,
        completion_tokens=5,
        cost_nano_usd=123,
        cost_known=True,
        duration_ms=10,
        config_version=1,
        usage_state=usage_pb2.USAGE_STATE_REPORTED,
        outcome=usage_pb2.REQUEST_OUTCOME_COMPLETED,
    )
    values.update(fields)
    return usage_pb2.UsageEvent(**values)


async def key_for(session):
    team = await factories.make_team(session)
    owner = await factories.make_service_account(session, team)
    return await factories.make_key(session, team, owner)


async def rename_at(session, item, new_name, at=RENAMED):
    name = await session.get(CatalogName, item.id)
    session.add(
        CatalogNameHistory(name_id=name.id, kind=name.kind, old_name=name.name, renamed_at=at)
    )
    name.name = new_name
    await session.flush()


@pytest.mark.parametrize("path", ["direct", "alias", "router", "alias_router", "fallback"])
async def test_provenance_table_and_budget_by_served_cost(session, path):
    key = await key_for(session)
    model = await factories.make_model(
        session,
        "served",
        created_at=CREATED,
        price_input_nano_usd_per_mtok=1000000,
        price_output_nano_usd_per_mtok=2000000,
    )
    pool = await factories.make_router(session, "pool", members=[model], created_at=CREATED)
    alias = await factories.make_alias(
        session, "nick", pool if "router" in path or path == "fallback" else model
    )
    alias.created_at = CREATED
    await session.flush()
    via_alias = path in {"alias", "alias_router", "fallback"}
    via_router = path in {"router", "alias_router", "fallback"}
    event = wire(
        key,
        requested="nick" if via_alias else "pool" if via_router else "served",
        alias="nick" if via_alias else "",
        router="pool" if via_router else "",
        upstream="original-upstream",
        upstream_model="original-upstream-model",
        fallback=path == "fallback",
        attempts=3 if path == "fallback" else 1,
        # CP verifies preservation/accounting of gateway-reported cost only.
        # Snapshot/model pricing itself is a gateway (c3) responsibility.
        cost_nano_usd=20,
    )
    model.upstream_model = "changed-before-delivery"
    await session.flush()
    assert await ingest(session, "gateway", [event]) == 1
    row = await session.scalar(
        select(UsageEvent).where(UsageEvent.request_id == uuid.UUID(event.request_id))
    )
    assert (row.model, row.alias, row.router) == (event.model, event.alias, event.router)
    assert (row.model_id, row.alias_id, row.router_id) == (
        model.id,
        alias.id if via_alias else None,
        pool.id if via_router else None,
    )
    assert (row.upstream, row.upstream_model, row.fallback, row.attempts) == (
        "original-upstream",
        "original-upstream-model",
        path == "fallback",
        event.attempts,
    )
    total = await session.scalar(
        select(UsageTotal).where(
            UsageTotal.scope_type == ScopeType.TEAM, UsageTotal.scope_id == key.team_id
        )
    )
    assert (row.cost_nano_usd, total.cost_nano_usd) == (20, 20)


async def test_keep_old_alias_and_reused_model_names_resolve_by_kind_and_time(session):
    key = await key_for(session)
    original = await factories.make_model(session, "old", created_at=CREATED)
    await rename_at(session, original, "new")
    alias = await factories.make_alias(session, "old", original)
    alias.created_at = RENAMED
    await session.flush()
    before = wire(key, model="old", timestamp=TS)
    after = wire(key, model="new", alias="old", timestamp=RENAMED + timedelta(hours=1))
    assert await ingest(session, "gateway", [before, after]) == 2
    rows = list(await session.scalars(select(UsageEvent).order_by(UsageEvent.ts)))
    assert [(row.model_id, row.alias_id) for row in rows] == [
        (original.id, None),
        (original.id, alias.id),
    ]
    # Replay must preserve the stored identities AND leave accounting unchanged.
    total = await session.scalar(
        select(UsageTotal).where(
            UsageTotal.scope_type == ScopeType.TEAM, UsageTotal.scope_id == key.team_id
        )
    )
    assert (total.request_count, total.cost_nano_usd) == (2, 246)
    assert await ingest(session, "gateway", [before]) == 1
    await session.refresh(rows[0])
    await session.refresh(total)
    assert (rows[0].model_id, rows[0].alias_id) == (original.id, None)
    assert (total.request_count, total.cost_nano_usd) == (2, 246)
    await rename_at(session, alias, "alias-new", RENAMED + timedelta(hours=2))
    reused = await factories.make_model(session, "old", created_at=RENAMED + timedelta(hours=2))
    later = wire(key, model="old", timestamp=RENAMED + timedelta(hours=3))
    delayed = wire(key, model="old", timestamp=TS)
    assert await ingest(session, "gateway", [later, delayed]) == 2
    for event, expected in ((later, reused.id), (delayed, original.id)):
        row = await session.scalar(
            select(UsageEvent).where(UsageEvent.request_id == uuid.UUID(event.request_id))
        )
        assert row.model_id == expected


@pytest.mark.parametrize("kind", ["model", "alias", "router"])
async def test_repeated_rename_intervals_and_reuse_within_kind(session, kind):
    key = await key_for(session)
    served = await factories.make_model(session, "served", created_at=CREATED)

    async def make(name, created):
        if kind == "model":
            return await factories.make_model(session, name, created_at=created)
        if kind == "router":
            return await factories.make_router(session, name, created_at=created)
        alias = await factories.make_alias(session, name, served)
        alias.created_at = created
        await session.flush()
        return alias

    a = await make("old", CREATED)
    await rename_at(session, a, "new")
    b = await make("old", RENAMED)
    second = RENAMED + timedelta(days=2)
    await rename_at(session, b, "newer", second)
    await rename_at(session, a, "old", second + timedelta(days=1))
    events = [
        wire(key, timestamp=ts, **{kind: "old"})
        for ts in (
            TS,
            RENAMED + timedelta(days=1),
            second + timedelta(hours=1),
            second + timedelta(days=2),
        )
    ]
    assert await ingest(session, "gateway", events) == 4
    rows = [
        await session.scalar(
            select(UsageEvent).where(UsageEvent.request_id == uuid.UUID(e.request_id))
        )
        for e in events
    ]
    assert [getattr(row, f"{kind}_id") for row in rows] == [a.id, b.id, None, a.id]


async def test_deleted_unknown_rejected_and_legacy_events(session):
    key = await key_for(session)
    gone = await factories.make_model(session, "gone", created_at=CREATED)
    await rename_at(session, gone, "gone-new")
    await session.delete(await session.get(CatalogName, gone.id))
    await session.flush()
    unknown = wire(key, model="unknown")
    deleted = wire(key, model="gone")
    legacy = wire(key)  # No new protobuf fields: default strings/false/zero.
    rejected = wire(
        key,
        requested="not-found",
        model="",
        outcome=usage_pb2.REQUEST_OUTCOME_REJECTED,
        status=404,
        cost_known=False,
        cost_nano_usd=0,
        usage_state=usage_pb2.USAGE_STATE_MISSING,
    )
    assert await ingest(session, "gateway", [unknown, deleted, legacy, rejected]) == 4
    rows = list(await session.scalars(select(UsageEvent)))
    assert all((row.model_id, row.alias_id, row.router_id) == (None, None, None) for row in rows)
    row = next(row for row in rows if row.requested == "not-found")
    assert row.model == ""
    assert next(row for row in rows if row.model == "gone").upstream_model == ""
    assert next(row for row in rows if row.request_id == uuid.UUID(legacy.request_id)).attempts == 0


async def test_catalog_resolution_is_two_queries_for_a_batch(session):
    key = await key_for(session)
    await factories.make_model(session, "served", created_at=CREATED)
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith(("SELECT catalog_names.", "SELECT catalog_name_history.")):
            statements.append(statement)

    engine = session.bind.sync_engine
    sa_event.listen(engine, "before_cursor_execute", capture)
    try:
        assert await ingest(session, "gateway", [wire(key) for _ in range(100)]) == 100
    finally:
        sa_event.remove(engine, "before_cursor_execute", capture)
    assert len(statements) == 2


async def test_full_batch_of_distinct_provenance_names_stays_under_bind_limit(session):
    from lumewarp_control_plane.settings import Settings

    key = await key_for(session)
    count = Settings().max_usage_batch_events
    events = [
        wire(key, model=f"model-{i}", alias=f"alias-{i}", router=f"router-{i}")
        for i in range(count)
    ]
    assert await ingest(session, "gateway", events) == count


@pytest.mark.parametrize(
    "field, value",
    [
        ("model", "m" * 401),
        ("alias", "a" * 401),
        ("router", "r" * 401),
        ("upstream", "u" * 201),
        ("upstream_model", "u" * 401),
        ("alias", "bad\x00name"),
    ],
)
async def test_invalid_provenance_is_skipped_without_losing_the_batch(session, field, value):
    key = await key_for(session)
    assert await ingest(session, "gateway", [wire(key), wire(key, **{field: value})]) == 1
