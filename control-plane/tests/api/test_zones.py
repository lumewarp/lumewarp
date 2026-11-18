"""Zones API: CRUD, validation, permissions and archive rules.

Self-contained: its own migrated database and app, so it does not share the admin API
fixtures (and their rows) with other test modules.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from lumewarp_control_plane.app import create_app, ensure_zone
from lumewarp_control_plane.db import create_session_factory
from lumewarp_control_plane.models import (
    ConfigOutbox,
    GatewaySession,
    GatewayToken,
    PersonalAccessToken,
    Zone,
)
from lumewarp_control_plane.security import SecretKind, generate_secret
from lumewarp_control_plane.settings import Settings
from tests.conftest import migrated_database
from tests.factories import make_user

DEFAULT_ZONE = "zone-api-test"
ADMIN_PASSWORD = "test-zones-password"


def _name() -> str:
    return f"zone-{uuid.uuid4().hex[:10]}"


@pytest.fixture(scope="module")
async def zones_url() -> AsyncIterator[str]:
    async with migrated_database() as url:
        yield url


@pytest.fixture(scope="module")
async def zones_engine(zones_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(zones_url)
    yield engine
    await engine.dispose()


@pytest.fixture(scope="module")
async def zones_scope(zones_engine: AsyncEngine) -> None:
    async with AsyncSession(zones_engine) as session, session.begin():
        session.add(Zone(name=DEFAULT_ZONE))


@pytest.fixture(scope="module")
def zones_settings(zones_url: str, zones_scope: None) -> Settings:
    return Settings(
        database_url=zones_url,
        background_enabled=False,
        break_glass_password=ADMIN_PASSWORD,
        dev_mode=True,
        session_cookie_secure=False,
        zone=DEFAULT_ZONE,
    )


@pytest.fixture(scope="module")
def zones_app(zones_settings: Settings):
    app = create_app(zones_settings)
    yield app
    asyncio.run(app.state.engine.dispose())


@pytest.fixture
def client(zones_app):
    with TestClient(zones_app) as test_client:
        yield test_client


@pytest.fixture
async def session(zones_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with AsyncSession(zones_engine, expire_on_commit=False) as current:
        yield current


@pytest.fixture
def admin(client) -> dict[str, str]:
    """A break-glass admin session (cookie plus its CSRF header)."""
    response = client.post("/api/v1/session/break-glass", json={"password": ADMIN_PASSWORD})
    assert response.status_code == 204, response.text
    return {"X-Lumewarp-CSRF": client.get("/api/v1/session").json()["csrf_token"]}


async def test_default_zone_is_created_at_startup(zones_engine: AsyncEngine) -> None:
    factory = create_session_factory(zones_engine)
    settings = Settings(
        zone="bootstrap-zone",
        playground_gateway_url="http://gateway.internal:8080",
        background_enabled=False,
    )
    await ensure_zone(factory, settings)
    async with factory() as session:
        row = await session.get(Zone, "bootstrap-zone")
        assert row is not None
        assert row.display_name == "bootstrap-zone"
        assert row.playground_gateway_url == "http://gateway.internal:8080"
        assert row.key_min_creator_role.value == "member"
        assert row.key_expiry_required is False
        assert row.service_account_keys_allowed is True
        assert row.playground_allowed is True
    # Idempotent: an existing row is left alone (its URL is not reset).
    async with factory() as session, session.begin():
        (await session.get(Zone, "bootstrap-zone")).playground_gateway_url = "http://other:9000"
    await ensure_zone(factory, settings)
    async with factory() as session:
        assert (await session.get(Zone, "bootstrap-zone")).playground_gateway_url == (
            "http://other:9000"
        )


async def test_any_signed_in_user_lists_zones_but_only_admins_write(
    client, admin: dict[str, str], session: AsyncSession
) -> None:
    user = await make_user(session, email=f"member-{uuid.uuid4().hex[:8]}@example.com")
    generated = generate_secret(SecretKind.PAT)
    session.add(
        PersonalAccessToken(
            user_id=user.id,
            name="member token",
            token_hash=generated.hash,
            display_prefix=generated.display_prefix,
        )
    )
    await session.commit()
    member = {"Authorization": f"Bearer {generated.secret}"}
    listed = client.get("/api/v1/zones", headers=member)
    assert listed.status_code == 200, listed.text
    assert DEFAULT_ZONE in {zone["name"] for zone in listed.json()["items"]}

    name = _name()
    body = {"name": name, "display_name": "Nope"}
    assert client.post("/api/v1/zones", json=body, headers=member).status_code == 403
    patch = {"display_name": "Nope"}
    assert (
        client.patch(f"/api/v1/zones/{DEFAULT_ZONE}", json=patch, headers=member).status_code == 403
    )
    assert client.delete(f"/api/v1/zones/{name}", headers=member).status_code == 403
    # Archived zones are for installation admins only.
    listed = client.get("/api/v1/zones?include_archived=true", headers=member)
    assert listed.status_code == 200
    assert all(zone["archived_at"] is None for zone in listed.json()["items"])
    # The admin route works with the break-glass session.
    assert client.post("/api/v1/zones", json=body, headers=admin).status_code == 201


async def test_zone_crud(client, admin: dict[str, str]) -> None:
    name = _name()
    created = client.post(
        "/api/v1/zones",
        json={
            "name": name,
            "display_name": "Production",
            "color": "blue",
            "playground_gateway_url": "http://prod-gw:8080",
            "key_min_creator_role": "team_admin",
            "key_max_lifetime_seconds": 86400,
            "key_expiry_required": True,
            "key_max_per_member": 3,
            "service_account_keys_allowed": False,
            "playground_allowed": False,
        },
        headers=admin,
    )
    assert created.status_code == 201, created.text
    zone = created.json()
    assert zone["name"] == name
    assert zone["display_name"] == "Production"
    assert zone["color"] == "blue"
    assert zone["key_min_creator_role"] == "team_admin"
    assert zone["key_max_lifetime_seconds"] == 86400
    assert zone["key_expiry_required"] is True
    assert zone["key_max_per_member"] == 3
    assert zone["service_account_keys_allowed"] is False
    assert zone["playground_allowed"] is False
    assert zone["archived_at"] is None

    duplicate = client.post("/api/v1/zones", json={"name": name}, headers=admin)
    assert duplicate.status_code == 409

    patched = client.patch(
        f"/api/v1/zones/{name}",
        json={
            "display_name": "Prod",
            "color": "amber",
            "playground_gateway_url": None,
            "key_max_per_member": None,
            "key_min_creator_role": "org_admin",
        },
        headers=admin,
    )
    assert patched.status_code == 200, patched.text
    zone = patched.json()
    assert zone["display_name"] == "Prod"
    assert zone["color"] == "amber"
    assert zone["playground_gateway_url"] is None
    assert zone["key_max_per_member"] is None
    assert zone["key_min_creator_role"] == "org_admin"

    listed = client.get("/api/v1/zones", headers=admin)
    assert name in {item["name"] for item in listed.json()["items"]}
    assert client.patch("/api/v1/zones/no-such-zone", json={}, headers=admin).status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {"name": "UPPER", "display_name": "x"},
        {"name": "has_underscore"},
        {"name": "with space"},
        {"name": _name(), "color": "pink"},
        {"name": _name(), "playground_gateway_url": "ftp://gateway"},
        {"name": _name(), "playground_gateway_url": "http://user:pass@gateway"},
        {"name": _name(), "key_max_lifetime_seconds": 0},
        {"name": _name(), "key_max_per_member": 0},
        {"name": _name(), "key_min_creator_role": "owner"},
    ],
)
def test_zone_validation(client, admin: dict[str, str], body: dict) -> None:
    response = client.post("/api/v1/zones", json=body, headers=admin)
    assert response.status_code == 422, response.text


def test_patch_rejects_null_for_a_required_field(client, admin: dict[str, str]) -> None:
    name = _name()
    assert client.post("/api/v1/zones", json={"name": name}, headers=admin).status_code == 201
    response = client.patch(f"/api/v1/zones/{name}", json={"display_name": None}, headers=admin)
    assert response.status_code == 422
    assert "display_name" in response.text


def test_default_zone_cannot_be_archived(client, admin: dict[str, str]) -> None:
    response = client.delete(f"/api/v1/zones/{DEFAULT_ZONE}", headers=admin)
    assert response.status_code == 409
    assert "default" in response.text


async def test_archive_is_refused_while_a_gateway_is_connected_then_revokes_tokens(
    client, admin: dict[str, str], session: AsyncSession
) -> None:
    name = _name()
    assert client.post("/api/v1/zones", json={"name": name}, headers=admin).status_code == 201
    created = client.post(
        "/api/v1/gateway-tokens",
        json={"name": "prod", "zone": name},
        headers=admin,
    )
    assert created.status_code == 201, created.text
    token = created.json()["token"]
    assert token["zone"] == name

    session.add(
        GatewaySession(
            zone=name, gateway_id="gw-1", token_id=uuid.UUID(token["id"]), connected=True
        )
    )
    await session.commit()

    refused = client.delete(f"/api/v1/zones/{name}", headers=admin)
    assert refused.status_code == 409
    assert "connected" in refused.text

    async with AsyncSession(session.bind, expire_on_commit=False) as other:
        async with other.begin():
            (await other.get(GatewaySession, (name, "gw-1"))).connected = False
    archived = client.delete(f"/api/v1/zones/{name}", headers=admin)
    assert archived.status_code == 200, archived.text
    assert archived.json()["archived_at"] is not None

    listed = client.get("/api/v1/zones", headers=admin)
    assert name not in {item["name"] for item in listed.json()["items"]}
    listed = client.get("/api/v1/zones?include_archived=true", headers=admin)
    assert name in {item["name"] for item in listed.json()["items"]}

    async with AsyncSession(session.bind, expire_on_commit=False) as check:
        rows = (await check.scalars(select(GatewayToken).where(GatewayToken.zone == name))).all()
    assert rows and all(row.revoked_at is not None for row in rows)


async def test_creating_a_gateway_token_recompiles_only_that_zone(
    client, admin: dict[str, str], session: AsyncSession
) -> None:
    name, other = _name(), _name()
    for zone in (name, other):
        assert client.post("/api/v1/zones", json={"name": zone}, headers=admin).status_code == 201

    response = client.post(
        "/api/v1/gateway-tokens", json={"name": "prod-token", "zone": name}, headers=admin
    )
    assert response.status_code == 201, response.text
    assert response.json()["token"]["zone"] == name

    async def pending() -> set[str]:
        async with AsyncSession(session.bind) as check:
            return set(
                (
                    await check.scalars(
                        select(ConfigOutbox.zone).where(
                            ConfigOutbox.status == "pending",
                            ConfigOutbox.zone.in_([name, other]),
                        )
                    )
                ).all()
            )

    assert await pending() == {name}

    revoke = client.post(
        f"/api/v1/gateway-tokens/{response.json()['token']['id']}/revoke", headers=admin
    )
    assert revoke.status_code == 200, revoke.text
    assert await pending() == {name}
