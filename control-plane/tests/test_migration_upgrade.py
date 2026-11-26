"""Upgrades keep existing rows; the rebuilt baseline has no downgrade path.

The baseline shipped in v0.3.0 and is frozen; every schema change is a new revision. A
downgrade always asks for a database backup.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from lumewarp_control_plane import migrate
from tests.conftest import create_database, drop_database


async def test_downgrade_is_refused() -> None:
    url = await create_database()
    engine = create_async_engine(url)
    try:
        await migrate.upgrade(engine, "head")
        with pytest.raises(NotImplementedError, match="restore"):
            await migrate.downgrade(engine, "base")
    finally:
        await engine.dispose()
        await drop_database(url)


async def test_router_session_affinity_upgrade_keeps_existing_routers() -> None:
    """The new columns default off for rows written before the revision."""
    url = await create_database()
    engine = create_async_engine(url)
    name_id = uuid.uuid4()
    try:
        await migrate.upgrade(engine, "0001")
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO catalog_names (id, name, kind)"
                    " VALUES (:id, 'existing-router', 'router')"
                ),
                {"id": name_id},
            )
            await conn.execute(text("INSERT INTO routers (name_id) VALUES (:id)"), {"id": name_id})
        await migrate.upgrade(engine, "head")
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT session_affinity, session_affinity_header FROM routers"
                        " WHERE name_id = :id"
                    ),
                    {"id": name_id},
                )
            ).one()
        assert row == (False, None)
    finally:
        await engine.dispose()
        await drop_database(url)


async def test_alerts_upgrade_keeps_existing_usage_events() -> None:
    """The new diagnostics are NULL on rows written before the revision."""
    url = await create_database()
    engine = create_async_engine(url)
    request_id = uuid.uuid4()
    try:
        await migrate.upgrade(engine, "0002")
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO usage_events ("
                    " request_id, gateway_id, ts, key_id, team_id, zone, owner_kind,"
                    " owner_id, requested, model, endpoint, status, stream, prompt_tokens,"
                    " completion_tokens, cost_nano_usd, cost_known, duration_ms,"
                    " config_version, usage_state, outcome"
                    " ) VALUES ("
                    " :request_id, 'gw-1', now(), :key_id, :team_id, 'default', 'user',"
                    " :owner_id, 'existing-model', 'existing-model', 'chat_completions',"
                    " 200, false, 10, 5, 100, true, 12, 1, 'reported', 'completed'"
                    " )"
                ),
                {
                    "request_id": request_id,
                    "key_id": uuid.uuid4(),
                    "team_id": uuid.uuid4(),
                    "owner_id": uuid.uuid4(),
                },
            )
        await migrate.upgrade(engine, "head")
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT error_code, ttft_ms FROM usage_events"
                        " WHERE request_id = :request_id"
                    ),
                    {"request_id": request_id},
                )
            ).one()
            tables = set(
                (
                    await conn.execute(
                        text(
                            "SELECT table_name FROM information_schema.tables"
                            " WHERE table_schema = current_schema()"
                        )
                    )
                ).scalars()
            )
        assert row == (None, None)
        assert {"incidents", "alerts", "alert_targets"} <= tables
        assert "prometheus_panels" not in tables
    finally:
        await engine.dispose()
        await drop_database(url)
