"""Fixtures for the admin API tests.

The API tests commit rows, so they use their own migrated database (created and dropped by
the shared `tests.conftest.migrated_database` helper) instead of the suite-wide database.
That keeps committed audit/outbox rows out of tests that assert on a pristine database.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from lumewarp_control_plane.app import create_app
from lumewarp_control_plane.auth.break_glass import fingerprint
from lumewarp_control_plane.auth.sessions import csrf_token
from lumewarp_control_plane.models import Scope, UserSession
from lumewarp_control_plane.security import PrincipalKind, hash_secret
from lumewarp_control_plane.settings import Settings
from tests.conftest import migrated_database

API_SCOPE = "api-test"
ADMIN_PASSWORD = "test-break-glass-password"


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
        credential = await session.scalar(
            select(UserSession).where(UserSession.token_hash == hash_secret("test-admin-session"))
        )
        if credential is None:
            session.add(
                UserSession(
                    user_id=None,
                    kind=PrincipalKind.BREAK_GLASS.value,
                    credential_fingerprint=fingerprint(
                        Settings(break_glass_password=ADMIN_PASSWORD, dev_mode=True)
                    ),
                    token_hash=hash_secret("test-admin-session"),
                    csrf_hash=hash_secret(csrf_token("test-admin-session")),
                    expires_at=datetime.now(UTC) + timedelta(days=1),
                )
            )
            await session.commit()
    return API_SCOPE


@pytest.fixture
async def api_settings(api_database_url: str, api_scope: str, api_engine: AsyncEngine) -> Settings:
    async with AsyncSession(api_engine) as session, session.begin():
        await session.execute(
            update(UserSession)
            .where(UserSession.token_hash == hash_secret("test-admin-session"))
            .values(revoked_at=None)
        )
    return Settings(
        database_url=api_database_url,
        background_enabled=False,
        break_glass_password=ADMIN_PASSWORD,
        dev_mode=True,
        session_cookie_secure=False,
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
