"""A fresh database gets its config scope from `serve` startup, without `bootstrap`.

The admin API fixtures insert the scope themselves, so this test uses its own migrated
database and creates the app directly: only the lifespan may create the row.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from lumewarp_control_plane.app import create_app
from lumewarp_control_plane.models import ConfigVersion, Zone
from lumewarp_control_plane.settings import Settings
from tests.conftest import migrated_database

ADMIN_PASSWORD = "test-startup-password"


def _settings(database_url: str) -> Settings:
    return Settings(
        database_url=database_url,
        # `serve` mode: this is what creates the scope. The port binds are ephemeral so the
        # test never collides with another control plane on the host.
        background_enabled=True,
        grpc_bind="127.0.0.1:0",
        metrics_bind="127.0.0.1:0",
        key_reconcile_enabled=False,
        dev_mode=True,
        break_glass_password=ADMIN_PASSWORD,
        session_cookie_secure=False,
    )


def _sign_in(client: TestClient) -> dict[str, str]:
    response = client.post("/api/v1/session/break-glass", json={"password": ADMIN_PASSWORD})
    assert response.status_code == 204, response.text
    csrf = client.get("/api/v1/session").json()["csrf_token"]
    return {"X-Lumewarp-CSRF": csrf}


async def _scope_rows(database_url: str) -> list[tuple[str, int]]:
    engine = create_async_engine(database_url)
    try:
        async with AsyncSession(engine) as session:
            rows = (await session.scalars(select(Zone))).all()
            return [(row.name, row.source_revision) for row in rows]
    finally:
        await engine.dispose()


async def _config_versions(database_url: str) -> int:
    engine = create_async_engine(database_url)
    try:
        async with AsyncSession(engine) as session:
            return (await session.scalar(select(func.count()).select_from(ConfigVersion))) or 0
    finally:
        await engine.dispose()


async def test_startup_creates_the_default_scope_and_a_token_can_be_created() -> None:
    async with migrated_database() as database_url:
        settings = _settings(database_url)
        with TestClient(create_app(settings)) as client:
            # Only the lifespan wrote this row: no bootstrap and no test fixture ran.
            assert await _scope_rows(database_url) == [("default", 0)]
            assert await _config_versions(database_url) == 0
            created = client.post(
                "/api/v1/gateway-tokens",
                json={"name": "startup"},
                headers=_sign_in(client),
            )
            assert created.status_code == 201, created.text
            assert created.json()["secret"].startswith("lwg-")

        # A restart leaves the one row and its revision alone.
        revision = (await _scope_rows(database_url))[0][1]
        with TestClient(create_app(settings)) as client:
            assert await _scope_rows(database_url) == [("default", revision)]
            created = client.post(
                "/api/v1/gateway-tokens",
                json={"name": "after-restart"},
                headers=_sign_in(client),
            )
            assert created.status_code == 201, created.text

        assert [name for name, _ in await _scope_rows(database_url)] == ["default"]
