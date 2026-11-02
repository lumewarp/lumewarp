"""Upgrade a populated previous head, including more than one backfill batch."""

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from lumewarp_control_plane import migrate
from tests.conftest import create_database, drop_database


async def test_populated_0010_usage_upgrade_and_downgrade():
    url = await create_database()
    engine = create_async_engine(url)
    ids = {key: uuid.uuid4() for key in ("upstream", "model", "router", "team", "owner", "key")}
    try:
        await migrate.upgrade(engine, "0010")
        async with engine.begin() as conn:
            for statement in (
                "INSERT INTO upstreams (id, name, kind, base_url) VALUES (:upstream, 'u', 'openai', 'http://localhost/v1')",
                "INSERT INTO catalog_names (id, name, kind) "
                "VALUES (:model, 'merged', 'model'), (:router, 'pool', 'router')",
                "INSERT INTO models (name_id, upstream_id, upstream_model, created_at) "
                "VALUES (:model, :upstream, 'actual', '2026-01-01')",
                "INSERT INTO routers (name_id, created_at) VALUES (:router, '2026-01-01')",
                """INSERT INTO usage_events (
                    request_id, gateway_id, ts, key_id, team_id, owner_kind, owner_id,
                    requested, model, endpoint, status, stream, prompt_tokens, completion_tokens,
                    cost_nano_usd, cost_known, duration_ms, config_version, usage_state, outcome)
                  SELECT md5(i::text)::uuid, 'g', '2026-03-01'::timestamptz,
                    :key, :team, 'user', :owner,
                    CASE WHEN i = 10001 THEN 'pool' WHEN i = 10002 THEN 'unknown' ELSE 'merged' END,
                    CASE WHEN i = 10001 THEN 'merged'
                      WHEN i = 10002 THEN '' ELSE 'old-deployment' END,
                    'chat_completions', CASE WHEN i = 10002 THEN 404 ELSE 200 END, false, 10, 5,
                    123, true, 10, 1, 'reported',
                    CASE WHEN i = 10002 THEN 'rejected' ELSE 'completed' END
                  FROM generate_series(1, 10002) i""",
            ):
                await conn.execute(text(statement), ids)
        await migrate.upgrade(engine)
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT requested, model, router, model_id, router_id, alias_id, "
                        "upstream_model, attempts, fallback, cost_nano_usd "
                        "FROM usage_events WHERE id IN (1, 10001, 10002) ORDER BY id"
                    )
                )
            ).all()
            assert rows == [
                ("merged", "old-deployment", "", ids["model"], None, None, "", 0, False, 123),
                ("pool", "merged", "pool", ids["model"], ids["router"], None, "", 0, False, 123),
                ("unknown", "", "", None, None, None, "", 0, False, 123),
            ]
            assert (
                await conn.execute(text("SELECT count(*) FROM usage_events"))
            ).scalar_one() == 10002
        await migrate.downgrade(engine, "0010")
        await migrate.upgrade(engine)
        async with engine.connect() as conn:
            assert (
                await conn.execute(text("SELECT sum(cost_nano_usd) FROM usage_events"))
            ).scalar_one() == 10002 * 123
    finally:
        await engine.dispose()
        await drop_database(url)
