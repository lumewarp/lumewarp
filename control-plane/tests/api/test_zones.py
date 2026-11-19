"""Zones: env-declared startup rows, update/archive rules and permissions.

Self-contained: its own migrated database and app, so it does not share the admin API
fixtures (and their rows) with other test modules.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from lumewarp_control_plane.app import create_app
from lumewarp_control_plane.models import (
    ConfigOutbox,
    GatewaySession,
    PersonalAccessToken,
    Zone,
)
from lumewarp_control_plane.security import SecretKind, generate_secret
from lumewarp_control_plane.settings import Settings, gateway_zone_tokens
from lumewarp_control_plane.zones import ensure_zones
from tests.conftest import migrated_database
from tests.factories import make_user

DEFAULT_ZONE = "zone-api-test"
ADMIN_PASSWORD = "test-zones-password"
DEFAULT_TOKEN = "z" * 32


def _name() -> str:
    return f"zone-{uuid.uuid4().hex[:10]}"


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


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
        session.add(Zone(id=DEFAULT_ZONE, name=DEFAULT_ZONE))


@pytest.fixture(scope="module")
def zones_settings(zones_url: str, zones_scope: None) -> Settings:
    return Settings(
        database_url=zones_url,
        background_enabled=False,
        break_glass_password=ADMIN_PASSWORD,
        dev_mode=True,
        session_cookie_secure=False,
        zone=DEFAULT_ZONE,
        gateway_zone_tokens={DEFAULT_ZONE: _digest(DEFAULT_TOKEN)},
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


def test_gateway_zone_tokens_are_parsed_and_validated() -> None:
    parsed = gateway_zone_tokens(
        {
            "LUMEWARP_GATEWAY_ZONE_PROD_EU_TOKEN": "p" * 32,
            "LUMEWARP_GATEWAY_ZONE_DEFAULT_TOKEN": "d" * 32,
            "UNRELATED": "ignored",
        }
    )
    assert parsed == {"prod-eu": _digest("p" * 32), "default": _digest("d" * 32)}
    assert all(isinstance(value, bytes) and len(value) == 32 for value in parsed.values())

    with pytest.raises(ValueError, match="zone id"):
        gateway_zone_tokens({"LUMEWARP_GATEWAY_ZONE_1BAD_TOKEN": "x" * 32})
    with pytest.raises(ValueError, match="at least 32"):
        gateway_zone_tokens({"LUMEWARP_GATEWAY_ZONE_SHORT_TOKEN": "x" * 31})
    with pytest.raises(ValueError, match="both declare"):
        gateway_zone_tokens(
            {
                "LUMEWARP_GATEWAY_ZONE_PROD_TOKEN": "x" * 32,
                "LUMEWARP_GATEWAY_ZONE_Prod_TOKEN": "y" * 32,
            }
        )


async def test_env_zones_are_created_at_startup_and_never_unarchived(
    zones_engine: AsyncEngine,
) -> None:
    from lumewarp_control_plane.db import create_session_factory

    session_factory = create_session_factory(zones_engine)
    settings = Settings(
        zone="bootstrap-zone",
        playground_gateway_url="http://gateway.internal:8080",
        background_enabled=False,
        gateway_zone_tokens={
            "prod": _digest("p" * 32),
            "edge-zone": _digest("e" * 32),
        },
    )
    await ensure_zones(session_factory, settings)
    async with session_factory() as session:
        rows = {row.id: row for row in (await session.scalars(select(Zone))).all()}
    assert {"bootstrap-zone", "prod", "edge-zone"} <= set(rows)
    assert rows["bootstrap-zone"].playground_gateway_url == "http://gateway.internal:8080"
    assert rows["prod"].playground_gateway_url is None
    assert rows["prod"].key_min_creator_role.value == "member"
    assert rows["prod"].archived_at is None

    # Idempotent: an existing row is left alone (its URL is not reset), and an archived zone
    # is never un-archived by a token in the environment.
    async with session_factory() as session, session.begin():
        (await session.get(Zone, "bootstrap-zone")).playground_gateway_url = "http://other:9000"
        (await session.get(Zone, "prod")).archived_at = rows["prod"].created_at
    await ensure_zones(session_factory, settings)
    async with session_factory() as session:
        assert (await session.get(Zone, "bootstrap-zone")).playground_gateway_url == (
            "http://other:9000"
        )
        assert (await session.get(Zone, "prod")).archived_at is not None


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
    zones = {zone["id"]: zone for zone in listed.json()["items"]}
    assert DEFAULT_ZONE in zones
    assert zones[DEFAULT_ZONE]["token_configured"] is True

    name = _name()
    session.add(Zone(id=name, name="Nope"))
    await session.commit()
    patch = {"name": "Nope"}
    assert client.post("/api/v1/zones", json={"id": _name()}, headers=admin).status_code in (
        404,
        405,
    )
    assert (
        client.patch(f"/api/v1/zones/{DEFAULT_ZONE}", json=patch, headers=member).status_code == 403
    )
    assert client.delete(f"/api/v1/zones/{name}", headers=member).status_code == 403
    # Archived zones are for installation admins only.
    listed = client.get("/api/v1/zones?include_archived=true", headers=member)
    assert listed.status_code == 200
    assert all(zone["archived_at"] is None for zone in listed.json()["items"])
    # The admin route works with the break-glass session.
    assert client.patch(f"/api/v1/zones/{name}", json=patch, headers=admin).status_code == 200


async def test_zone_update_and_listing(
    client, admin: dict[str, str], session: AsyncSession
) -> None:
    name = _name()
    async with AsyncSession(session.bind) as other, other.begin():
        other.add(Zone(id=name, name="Production"))

    patched = client.patch(
        f"/api/v1/zones/{name}",
        json={
            "name": "Prod",
            "color": "amber",
            "playground_gateway_url": "http://prod-gw:8080",
            "key_min_creator_role": "org_admin",
            "key_max_lifetime_seconds": 86400,
            "key_expiry_required": True,
            "key_max_per_member": 3,
            "service_account_keys_allowed": False,
            "playground_allowed": False,
        },
        headers=admin,
    )
    assert patched.status_code == 200, patched.text
    zone = patched.json()
    assert zone["name"] == "Prod"
    assert zone["color"] == "amber"
    assert zone["playground_gateway_url"] == "http://prod-gw:8080"
    assert zone["key_min_creator_role"] == "org_admin"
    assert zone["key_max_lifetime_seconds"] == 86400
    assert zone["key_expiry_required"] is True
    assert zone["key_max_per_member"] == 3
    assert zone["service_account_keys_allowed"] is False
    assert zone["playground_allowed"] is False
    assert zone["token_configured"] is False

    listed = client.get("/api/v1/zones", headers=admin)
    zones = {item["id"]: item for item in listed.json()["items"]}
    assert name in zones
    assert zones[name]["token_configured"] is False
    assert zones[DEFAULT_ZONE]["token_configured"] is True
    assert client.patch("/api/v1/zones/no-such-zone", json={}, headers=admin).status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {"name": "x", "color": "pink"},
        {"name": "x", "playground_gateway_url": "ftp://gateway"},
        {"name": "x", "playground_gateway_url": "http://user:pass@gateway"},
        {"name": "x", "key_max_lifetime_seconds": 0},
        {"name": "x", "key_max_per_member": 0},
        {"name": "x", "key_min_creator_role": "owner"},
        {"name": ""},
    ],
)
async def test_zone_update_validation(
    client, admin: dict[str, str], session: AsyncSession, body: dict
) -> None:
    name = _name()
    async with AsyncSession(session.bind) as other, other.begin():
        other.add(Zone(id=name, name=name))
    response = client.patch(f"/api/v1/zones/{name}", json=body, headers=admin)
    assert response.status_code == 422, response.text


async def test_patch_rejects_null_for_a_required_field(
    client, admin: dict[str, str], session: AsyncSession
) -> None:
    name = _name()
    async with AsyncSession(session.bind) as other, other.begin():
        other.add(Zone(id=name, name=name))
    response = client.patch(f"/api/v1/zones/{name}", json={"name": None}, headers=admin)
    assert response.status_code == 422
    assert "name" in response.text


async def test_only_the_default_zone_is_marked_default(
    client, admin: dict[str, str], session: AsyncSession
) -> None:
    name = _name()
    async with AsyncSession(session.bind) as other, other.begin():
        other.add(Zone(id=name, name=name))
    zones = {z["id"]: z for z in client.get("/api/v1/zones", headers=admin).json()["items"]}
    assert zones[DEFAULT_ZONE]["is_default"] is True
    assert zones[name]["is_default"] is False
    assert zones[name]["token_configured"] is False


def test_default_zone_cannot_be_archived(client, admin: dict[str, str]) -> None:
    response = client.delete(f"/api/v1/zones/{DEFAULT_ZONE}", headers=admin)
    assert response.status_code == 409
    assert "default" in response.text


async def test_archive_is_refused_while_a_gateway_is_connected(
    client, admin: dict[str, str], session: AsyncSession
) -> None:
    name = _name()
    async with AsyncSession(session.bind) as other, other.begin():
        other.add(Zone(id=name, name=name))
    async with AsyncSession(session.bind) as other, other.begin():
        other.add(GatewaySession(zone=name, gateway_id="gw-1", connected=True))

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
    assert name not in {item["id"] for item in listed.json()["items"]}
    listed = client.get("/api/v1/zones?include_archived=true", headers=admin)
    assert name in {item["id"] for item in listed.json()["items"]}


async def test_archiving_a_zone_drops_its_pending_outbox(
    client, admin: dict[str, str], session: AsyncSession
) -> None:
    name = _name()
    async with AsyncSession(session.bind) as other, other.begin():
        other.add(Zone(id=name, name=name))
    async with AsyncSession(session.bind) as other, other.begin():
        other.add(ConfigOutbox(zone=name, reason="test", source_revision=1))
    assert (
        await session.scalar(select(ConfigOutbox.id).where(ConfigOutbox.zone == name))
    ) is not None

    archived = client.delete(f"/api/v1/zones/{name}", headers=admin)
    assert archived.status_code == 200, archived.text
    async with AsyncSession(session.bind) as check:
        assert (
            await check.scalar(select(ConfigOutbox.id).where(ConfigOutbox.zone == name))
        ) is None
