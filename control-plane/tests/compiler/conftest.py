"""Compiler tests each get a fresh database: the compiler reads all source tables and
publishes from its own connections, so rows must be committed and isolated per test.
Databases are cloned from one migrated template (CREATE DATABASE ... TEMPLATE), which is
much faster than migrating each one.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from lumewarp_control_plane.db import SessionFactory, create_session_factory
from tests.conftest import SERVER_URL, TEST_DB_PREFIX, drop_database, migrated_database


@pytest.fixture(scope="session")
async def template_db() -> AsyncIterator[str]:
    async with migrated_database() as url:
        yield make_url(url).database


@pytest.fixture
async def factory(template_db: str) -> AsyncIterator[SessionFactory]:
    name = f"{TEST_DB_PREFIX}{uuid.uuid4().hex[:12]}"
    admin = create_async_engine(SERVER_URL, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{name}" TEMPLATE "{template_db}"'))
    finally:
        await admin.dispose()
    url = make_url(SERVER_URL).set(database=name).render_as_string(hide_password=False)
    try:
        engine = create_async_engine(url)
        try:
            yield create_session_factory(engine)
        finally:
            await engine.dispose()
    finally:
        await drop_database(url)
