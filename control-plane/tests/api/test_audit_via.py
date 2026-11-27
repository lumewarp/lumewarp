"""Audit rows record how the caller authenticated (`via`), and secret responses are not cached."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from lumewarp_control_plane import migrate
from lumewarp_control_plane.app import create_app
from lumewarp_control_plane.auth import sessions
from lumewarp_control_plane.models import AuditLog, TeamRole
from lumewarp_control_plane.security import VIA_SCOPE_EXTENSION
from lumewarp_control_plane.settings import Settings
from tests.api.helpers import ADMIN_AUTH, auth, suffix, user_with_token
from tests.conftest import create_database, drop_database
from tests.factories import make_member, make_team


def _with_mcp_extension(app):
    """The MCP server's in-process call: the same app, with the ASGI scope extension set."""

    async def wrapped(scope, receive, send):
        if scope["type"] == "http":
            scope = {
                **scope,
                "extensions": {**scope.get("extensions", {}), VIA_SCOPE_EXTENSION: "mcp"},
            }
        await app(scope, receive, send)

    return wrapped


@pytest.fixture
def mcp_client(api_settings: Settings) -> Iterator[TestClient]:
    with TestClient(_with_mcp_extension(create_app(api_settings))) as test_client:
        yield test_client


async def _team_admin(committed: AsyncSession):
    team = await make_team(committed)
    user, token = await user_with_token(committed)
    await make_member(committed, team, user, role=TeamRole.ADMIN)
    cookie = await sessions.create_session(committed, user.id, 3600)
    await committed.commit()
    return team, user, token, "session:" + cookie


async def _via(committed: AsyncSession, action: str, resource_id: str) -> str | None:
    rows = (
        await committed.execute(
            select(AuditLog.via).where(
                AuditLog.action == action, AuditLog.resource_id == resource_id
            )
        )
    ).all()
    assert len(rows) == 1
    return rows[0][0]


async def test_pat_session_and_mcp_writes_record_via(client, mcp_client, committed) -> None:
    team, user, token, cookie = await _team_admin(committed)
    by_pat = client.post(
        f"/api/v1/teams/{team.id}/keys", json={"name": f"pat-{suffix()}"}, headers=auth(token)
    )
    by_session = client.post(
        f"/api/v1/teams/{team.id}/keys", json={"name": f"ses-{suffix()}"}, headers=auth(cookie)
    )
    by_mcp = mcp_client.post(
        f"/api/v1/teams/{team.id}/keys", json={"name": f"mcp-{suffix()}"}, headers=auth(token)
    )
    for response in (by_pat, by_session, by_mcp):
        assert response.status_code == 201, response.text
    ids = [r.json()["key"]["id"] for r in (by_pat, by_session, by_mcp)]
    assert [await _via(committed, "key.create", i) for i in ids] == ["pat", "session", "mcp"]

    listed = client.get(
        "/api/v1/audit",
        params={"team_id": str(team.id), "action": "key.create"},
        headers=auth(token),
    )
    assert listed.status_code == 200, listed.text
    assert {r["resource_id"]: r["via"] for r in listed.json()["items"]} == dict(
        zip(ids, ["pat", "session", "mcp"], strict=True)
    )
    only_mcp = client.get(
        "/api/v1/audit", params={"team_id": str(team.id), "via": "mcp"}, headers=auth(token)
    )
    assert [r["resource_id"] for r in only_mcp.json()["items"]] == [ids[2]]


async def test_mcp_extension_does_not_mark_a_cookie_session(mcp_client, committed) -> None:
    team, _, _, cookie = await _team_admin(committed)
    response = mcp_client.post(
        f"/api/v1/teams/{team.id}/keys", json={"name": f"k-{suffix()}"}, headers=auth(cookie)
    )
    assert response.status_code == 201, response.text
    assert await _via(committed, "key.create", response.json()["key"]["id"]) == "session"


async def test_break_glass_rows_are_session(client, committed) -> None:
    response = client.post(
        "/api/v1/users", json={"email": f"via-{suffix()}@example.com"}, headers=ADMIN_AUTH
    )
    assert response.status_code == 201, response.text
    assert await _via(committed, "user.create", response.json()["id"]) == "session"
    request_rows = (
        await committed.scalars(
            select(AuditLog.via).where(AuditLog.action == "session.break_glass.request")
        )
    ).all()
    assert request_rows and set(request_rows) == {"session"}


async def test_secret_responses_are_not_cached(client, committed) -> None:
    team, _, token, _ = await _team_admin(committed)
    created = client.post(
        f"/api/v1/teams/{team.id}/keys", json={"name": f"k-{suffix()}"}, headers=auth(token)
    )
    assert created.status_code == 201, created.text
    assert created.headers["cache-control"] == "no-store"
    rotated = client.post(
        f"/api/v1/keys/{created.json()['key']['id']}/rotate", json={}, headers=auth(token)
    )
    assert rotated.status_code == 200, rotated.text
    assert rotated.headers["cache-control"] == "no-store"
    own = client.post("/api/v1/users/me/tokens", json={"name": "t"}, headers=auth(token))
    assert own.status_code == 201, own.text
    assert own.headers["cache-control"] == "no-store"
    assert await _via(committed, "pat.create", own.json()["token"]["id"]) == "pat"


async def test_migration_0004_keeps_existing_audit_rows() -> None:
    """Rows written before `audit_log.via` existed survive the upgrade with `via` NULL."""
    url = await create_database()
    engine = create_async_engine(url)
    try:
        await migrate.upgrade(engine, "0003")
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO audit_log (actor_type, actor_id, action, resource_type,"
                    " resource_id, details) VALUES ('user', 'u1', 'team.create', 'team',"
                    " 't1', '{\"name\": \"kept\"}'::jsonb), ('system', 'budget',"
                    " 'budget.monthly_rollover', 'budget', NULL, '{}'::jsonb)"
                )
            )
        await migrate.upgrade(engine)
        async with engine.connect() as conn:
            assert await migrate.current_revision(conn) == "0004"
            rows = (
                await conn.execute(
                    text("SELECT action, actor_id, via, details FROM audit_log ORDER BY id")
                )
            ).all()
            assert [tuple(r) for r in rows] == [
                ("team.create", "u1", None, {"name": "kept"}),
                ("budget.monthly_rollover", "budget", None, {}),
            ]
            constraint = await conn.scalar(
                text("SELECT 1 FROM pg_constraint WHERE conname = 'ck_audit_log_audit_via'")
            )
            assert constraint == 1
        with pytest.raises(Exception, match="check"):
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO audit_log (actor_type, action, resource_type, via)"
                        " VALUES ('user', 'x', 'x', 'carrier-pigeon')"
                    )
                )
    finally:
        await engine.dispose()
        await drop_database(url)
