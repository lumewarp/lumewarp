"""Populated 0010 upgrade: interval SQL, merged identity, replay and explicit loss."""

import hashlib
import uuid
from collections import Counter
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from lumewarp_control_plane import migrate
from lumewarp_control_plane.usage import ingest
from tests.conftest import create_database, drop_database
from tests.usage.test_catalog_provenance import key_for, wire


async def test_populated_0010_usage_upgrade_and_downgrade():
    url = await create_database()
    engine = create_async_engine(url)
    ids = {
        key: uuid.uuid4()
        for key in ("upstream", "model", "router", "new_router", "late_router", "new_model")
    }
    try:
        await migrate.upgrade(engine, "0010")
        async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
            key = await key_for(session)
        ids.update(key=key.id, team=key.team_id, owner=key.owner_id)
        async with engine.begin() as conn:
            for statement in (
                "INSERT INTO upstreams (id, name, kind, base_url) "
                "VALUES (:upstream, 'u', 'openai', 'http://localhost/v1')",
                """INSERT INTO catalog_names (id, name, kind) VALUES
                  (:model, 'merged', 'model'), (:router, 'pool-final', 'router'),
                  (:new_router, 'pool', 'router'), (:late_router, 'later', 'router'),
                  (:new_model, 'previous-model', 'model')""",
                """INSERT INTO models (name_id, upstream_id, upstream_model, created_at) VALUES
                  (:model, :upstream, 'actual', '2026-01-01'),
                  (:new_model, :upstream, 'new-actual', '2026-02-01')""",
                """INSERT INTO routers (name_id, created_at) VALUES
                  (:router, '2026-01-01'), (:new_router, '2026-03-15'),
                  (:late_router, '2026-03-01')""",
                """INSERT INTO catalog_name_history (name_id, kind, old_name, renamed_at) VALUES
                  (:model, 'model', 'previous-model', '2026-02-01'),
                  (:router, 'router', 'pool', '2026-03-15'),
                  (:router, 'router', 'pool-new', '2026-04-01')""",
                """INSERT INTO usage_events (
                    request_id, gateway_id, ts, key_id, team_id, owner_kind, owner_id,
                    requested, model, endpoint, status, stream, prompt_tokens, completion_tokens,
                    cost_nano_usd, cost_known, duration_ms, config_version, usage_state, outcome)
                  SELECT md5(i::text)::uuid, 'g', '2026-03-01'::timestamptz,
                    :key, :team, 'service_account', :owner,
                    CASE WHEN i = 10001 THEN 'pool' WHEN i = 10002 THEN 'unknown' ELSE 'merged' END,
                    CASE WHEN i = 10001 THEN 'merged'
                      WHEN i = 10002 THEN '' ELSE 'old-deployment' END,
                    'chat_completions', CASE WHEN i = 10002 THEN 404 ELSE 200 END, false, 10, 5,
                    123, true, 10, 1, 'reported',
                    CASE WHEN i = 10002 THEN 'rejected' ELSE 'completed' END
                  FROM generate_series(1, 10002) i""",
                """INSERT INTO usage_totals (scope_type, scope_id, window_start, cost_nano_usd,
                    prompt_tokens, completion_tokens, request_count, unknown_cost_count)
                  VALUES ('team', :team, '2026-03-01', 1230246, 100020, 50010, 10002, 0)""",
                """INSERT INTO usage_events (
                    request_id, gateway_id, ts, started_at, key_id, team_id, owner_kind, owner_id,
                    requested, model, endpoint, status, stream, prompt_tokens, completion_tokens,
                    cost_nano_usd, cost_known, duration_ms, config_version, usage_state, outcome)
                  SELECT md5('extra-' || i::text)::uuid, 'g', ts, started, :key, :team,
                    'service_account', :owner, requested, model, 'chat_completions', 200,
                    false, 10, 5, 123, true, 10, 1, 'reported', 'completed'
                  FROM (VALUES
                    (1, '2025-12-01'::timestamptz, NULL::timestamptz, 'merged', 'old-deployment'),
                    (2, '2026-01-15'::timestamptz, NULL, 'pool', 'previous-model'),
                    (3, '2026-02-15'::timestamptz, NULL, 'pool', 'previous-model'),
                    (4, '2026-03-20'::timestamptz, NULL, 'pool', 'merged'),
                    (5, '2025-12-01'::timestamptz, NULL, 'pool', 'unknown-model'),
                    (6, '2026-02-01'::timestamptz, NULL, 'later', 'merged'),
                    (7, '2026-03-20'::timestamptz, NULL, 'pool-new', 'merged'),
                    (8, '2026-02-01'::timestamptz, NULL, 'pool-new', 'merged'),
                    (9, '2026-03-16'::timestamptz, '2026-03-14'::timestamptz, 'pool', 'merged')
                  ) v(i, ts, started, requested, model)""",
            ):
                await conn.execute(text(statement), ids)
        await migrate.upgrade(engine)
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT requested, model, router, model_id, router_id FROM usage_events "
                        "WHERE id >= 10001 ORDER BY id"
                    )
                )
            ).all()
            assert rows == [
                ("pool", "merged", "pool", ids["model"], ids["router"]),
                ("unknown", "", "", None, None),
                ("merged", "old-deployment", "", ids["model"], None),  # Predates deployment.
                ("pool", "previous-model", "pool", ids["model"], ids["router"]),
                ("pool", "previous-model", "pool", ids["new_model"], ids["router"]),
                ("pool", "merged", "pool", ids["model"], ids["new_router"]),
                ("pool", "unknown-model", "", None, None),  # Before router creation.
                ("later", "merged", "", ids["model"], None),
                ("pool-new", "merged", "pool-new", ids["model"], ids["router"]),
                ("pool-new", "merged", "", ids["model"], None),  # Before that rename interval.
                ("pool", "merged", "pool", ids["model"], ids["router"]),  # Request start.
            ]
            assert (
                await conn.execute(text("SELECT count(*) FROM usage_events"))
            ).scalar_one() == 10011
            assert (
                await conn.execute(text("SELECT model FROM usage_events WHERE id = 1"))
            ).scalar_one() == "old-deployment"
        # A pre-upgrade report whose ACK was lost: backfilled router is not a conflict.
        replay = wire(
            key,
            timestamp=datetime(2026, 3, 1, tzinfo=UTC),
            request_id=str(uuid.UUID(hashlib.md5(b"10001").hexdigest())),
            requested="pool",
            model="merged",
        )
        skipped = Counter()
        async with AsyncSession(engine) as session, session.begin():
            assert await ingest(session, "g", [replay], skipped=skipped) == 1
            assert not skipped
            assert (
                await session.execute(
                    text(
                        "SELECT request_count, cost_nano_usd FROM usage_totals "
                        "WHERE scope_type = 'team'"
                    )
                )
            ).one() == (10002, 1230246)
        async with engine.begin() as conn:
            await conn.execute(
                text("""
                UPDATE usage_events SET alias = 'wire-alias', upstream = 'wire-upstream',
                  upstream_model = 'wire-model', fallback = true, attempts = 3, alias_id = :alias
                WHERE id = 1
            """),
                {"alias": uuid.uuid4()},
            )
        # Each downgrade revision commits independently: failure at 0010 does not
        # roll back 0011's destructive drop. Explicitly demonstrate this data loss.
        with pytest.raises(NotImplementedError, match="irreversible"):
            await migrate.downgrade(engine, "0009")
        async with engine.connect() as conn:
            assert await migrate.current_revision(conn) == "0010"
        await migrate.upgrade(engine)
        async with engine.connect() as conn:
            assert (
                await conn.execute(text("SELECT sum(cost_nano_usd) FROM usage_events"))
            ).scalar_one() == 10011 * 123
            assert (
                await conn.execute(
                    text(
                        "SELECT alias, upstream, upstream_model, fallback, attempts, alias_id "
                        "FROM usage_events WHERE id = 1"
                    )
                )
            ).one() == ("", "", "", False, 0, None)
    finally:
        await engine.dispose()
        await drop_database(url)
