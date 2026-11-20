"""Shared sessions for the config-stream tests: one migrated database per module."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lumewarp_control_plane.db import SessionFactory, create_session_factory


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
