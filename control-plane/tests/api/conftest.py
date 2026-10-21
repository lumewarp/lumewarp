"""Fixtures for the admin API tests.

The API tests commit rows, so they use their own migrated database (created and dropped by
the shared `tests.conftest.migrated_database` helper) instead of the suite-wide database.
That keeps committed audit/outbox rows out of tests that assert on a pristine database.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from lumewarp_control_plane.app import create_app
from lumewarp_control_plane.models import Scope
from lumewarp_control_plane.settings import Settings
from tests.conftest import migrated_database

API_SCOPE = "api-test"
BOOTSTRAP_TOKEN = "test-bootstrap-token"


@pytest.fixture(scope="session")
async def api_database_url() -> AsyncIterator[str]:
    async with migrated_database() as url:
        yield url


@pytest.fixture(scope="session")
async def api_engine(api_database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(api_database_url)
    yield engine
    await engine.dispose()


@pytest.fixture(scope="session")
async def api_scope(api_engine: AsyncEngine) -> str:
    async with AsyncSession(api_engine, expire_on_commit=False) as session:
        if await session.get(Scope, API_SCOPE) is None:
            session.add(Scope(name=API_SCOPE))
            await session.commit()
    return API_SCOPE


@pytest.fixture
def api_settings(api_database_url: str, api_scope: str) -> Settings:
    return Settings(
        database_url=api_database_url,
        background_enabled=False,
        bootstrap_token=BOOTSTRAP_TOKEN,
        scope=api_scope,
    )


@pytest.fixture
async def committed(api_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session whose commits are visible to the HTTP app."""
    async with AsyncSession(api_engine, expire_on_commit=False) as session:
        yield session


@pytest.fixture
def client(api_settings: Settings) -> AsyncIterator[TestClient]:
    with TestClient(create_app(api_settings)) as test_client:
        yield test_client
