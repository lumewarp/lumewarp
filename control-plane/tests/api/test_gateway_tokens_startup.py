"""Zones declared by `LUMEWARP_GATEWAY_ZONE_<ID>_TOKEN` are created by `serve` startup.

The admin API fixtures insert the scope themselves, so this test uses its own migrated
database and creates the app directly: only the lifespan may create these rows. A zone is
never un-archived by an env token, and a restart leaves the rows and revisions alone.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from lumewarp_control_plane.app import create_app
from lumewarp_control_plane.models import Zone
from lumewarp_control_plane.settings import Settings
from tests.conftest import migrated_database

ADMIN_PASSWORD = "test-startup-password"
EDGE_TOKEN = "e" * 48


def _settings(database_url: str) -> Settings:
    return Settings(
        database_url=database_url,
        # `serve` mode: this is what creates the zones. The port binds are ephemeral so the
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


async def _zones(database_url: str) -> dict[str, Zone]:
    engine = create_async_engine(database_url)
    try:
        async with AsyncSession(engine) as session:
            return {row.id: row for row in (await session.scalars(select(Zone))).all()}
    finally:
        await engine.dispose()


async def test_startup_creates_env_zones_and_a_restart_keeps_them(
    monkeypatch,
) -> None:
    monkeypatch.setenv("LUMEWARP_GATEWAY_ZONE_EDGE_TOKEN", EDGE_TOKEN)
    async with migrated_database() as database_url:
        # Only the lifespan wrote these rows: no bootstrap and no test fixture ran.
        with TestClient(create_app(_settings(database_url))) as client:
            zones = await _zones(database_url)
            assert set(zones) >= {"default", "edge"}
            assert (zones["edge"].name, zones["edge"].archived_at) == ("edge", None)
            listed = client.get("/api/v1/zones", headers=_sign_in(client))
            assert listed.status_code == 200, listed.text
            by_id = {item["id"]: item for item in listed.json()["items"]}
            assert by_id["edge"]["token_configured"] is True
            assert by_id["default"]["token_configured"] is False
            # Zones are declared by the environment, not created through the API.
            assert client.post(
                "/api/v1/zones", json={"id": "manual"}, headers=_sign_in(client)
            ).status_code in (404, 405)

        revision = (await _zones(database_url))["default"].source_revision
        with TestClient(create_app(_settings(database_url))) as client:
            assert (await _zones(database_url))["default"].source_revision == revision
            # The token still exists but the zone is archived: it stays archived.
            archived = client.delete("/api/v1/zones/edge", headers=_sign_in(client))
            assert archived.status_code == 200, archived.text
        with TestClient(create_app(_settings(database_url))):
            zone = (await _zones(database_url))["edge"]
            assert zone.archived_at is not None

        assert set(await _zones(database_url)) >= {"default", "edge"}
