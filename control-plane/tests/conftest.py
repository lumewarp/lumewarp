"""Fixtures on a real Postgres.

Each test session creates a uniquely named database `lumewarp_test_<hex>` on the server
from LUMEWARP_TEST_DATABASE_URL (default: the dev compose DB), migrates it to head and
drops it afterwards. Only databases with that prefix are ever dropped.
"""

import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from lumewarp_control_plane import migrate
from lumewarp_control_plane.settings import DEV_DATABASE_URL, Settings

# Tests that start background services bind the metrics listener to an ephemeral port
# instead of the fixed default.
os.environ.setdefault("LUMEWARP_METRICS_BIND", "127.0.0.1:0")

TEST_DB_PREFIX = "lumewarp_test_"
SERVER_URL = os.environ.get("LUMEWARP_TEST_DATABASE_URL", DEV_DATABASE_URL)


async def create_database() -> str:
    """Create an empty test database; return its URL."""
    name = f"{TEST_DB_PREFIX}{uuid.uuid4().hex[:12]}"
    admin = create_async_engine(SERVER_URL, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        await admin.dispose()
    return make_url(SERVER_URL).set(database=name).render_as_string(hide_password=False)


async def drop_database(url: str) -> None:
    name = make_url(url).database
    if not name or not name.startswith(TEST_DB_PREFIX):
        raise RuntimeError(f"refusing to drop non-test database {name!r}")
    admin = create_async_engine(SERVER_URL, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        await admin.dispose()


@asynccontextmanager
async def migrated_database() -> AsyncIterator[str]:
    """A fresh database at the migration head, dropped on exit even if setup fails."""
    url = await create_database()
    try:
        engine = create_async_engine(url)
        try:
            await migrate.upgrade(engine)
        finally:
            await engine.dispose()
        yield url
    finally:
        await drop_database(url)


@pytest.fixture(scope="session")
async def database_url() -> AsyncIterator[str]:
    async with migrated_database() as url:
        yield url


@pytest.fixture(scope="session")
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(database_url)
    yield engine
    await engine.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session inside an outer transaction that is rolled back after the test; commits
    inside the test become savepoints.
    """
    async with engine.connect() as conn:
        trans = await conn.begin()
        async with AsyncSession(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        ) as s:
            yield s
        await trans.rollback()


@pytest.fixture
def settings(database_url: str) -> Settings:
    return Settings(
        database_url=database_url,
        background_enabled=False,
        bootstrap_token="test-bootstrap-token",
    )
