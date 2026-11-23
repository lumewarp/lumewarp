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
