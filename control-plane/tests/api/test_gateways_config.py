"""Gateway fleet/token management and config versions/rollback."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from lumewarp_control_plane import compiler
from lumewarp_control_plane.models import (
    AuditLog,
    ConfigVersion,
    ConfigVersionKind,
    GatewaySession,
)
from tests.api.helpers import BOOTSTRAP_AUTH, auth, suffix, user_with_token


def _version(scope: str, version: int, *, kind=ConfigVersionKind.COMPILE, rollback_of=None):
    return ConfigVersion(
        scope=scope,
        version=version,
        payload=b"{}",
        digest=b"x" * 32,
        size_bytes=2,
        key_count=0,
        source_revision=version,
        kind=kind,
        rollback_of_version=rollback_of,
    )


async def test_gateway_tokens(client, committed, api_scope) -> None:
    gateway_id = f"gw-{suffix()}"
    resp = client.post(
        "/api/v1/gateways",
        json={"gateway_id": gateway_id, "name": "primary"},
        headers=BOOTSTRAP_AUTH,
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()
    token = created["token"]
    assert created["secret"].startswith("lwg-")
    assert "hash" not in token
    assert token["gateway_id"] == gateway_id

    listed = client.get("/api/v1/gateways", headers=BOOTSTRAP_AUTH)
    assert listed.status_code == 200
    row = next(item for item in listed.json()["items"] if item["gateway_id"] == gateway_id)
    assert row["connected"] is False
    assert row["tokens"][0]["display_prefix"] == token["display_prefix"]

    # A session row is reflected in the fleet view.
    committed.add(
        GatewaySession(
            scope=api_scope,
            gateway_id=gateway_id,
            connected=True,
            gateway_version="0.1",
            acked_version=3,
            last_seen_at=datetime.now(UTC),
        )
    )
    await committed.commit()
    listed = client.get("/api/v1/gateways?limit=500", headers=BOOTSTRAP_AUTH)
    row = next(item for item in listed.json()["items"] if item["gateway_id"] == gateway_id)
    assert row["connected"] is True
    assert row["acked_version"] == 3

    revoked = client.post(
        f"/api/v1/gateways/{gateway_id}/tokens/{token['id']}/revoke",
        headers=BOOTSTRAP_AUTH,
    )
    assert revoked.status_code == 200
    assert revoked.json()["revoked_at"] is not None
    # Wrong gateway id does not match the token.
    assert (
        client.post(
            f"/api/v1/gateways/other/tokens/{token['id']}/revoke",
            headers=BOOTSTRAP_AUTH,
        ).status_code
        == 404
    )

    user, secret = await user_with_token(committed)
    await committed.commit()
    assert client.get("/api/v1/gateways", headers=auth(secret)).status_code == 403


async def test_config_versions_and_rollback(client, committed, api_scope, monkeypatch) -> None:
    committed.add_all([_version(api_scope, 1), _version(api_scope, 2)])
    await committed.commit()

    listed = client.get("/api/v1/config/versions?limit=1", headers=BOOTSTRAP_AUTH)
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["items"][0]["version"] == 2
    assert body["next_cursor"] == "2"
    second = client.get(
        f"/api/v1/config/versions?limit=1&cursor={body['next_cursor']}",
        headers=BOOTSTRAP_AUTH,
    )
    assert second.status_code == 200
    assert second.json()["items"][0]["version"] == 1

    user, secret = await user_with_token(committed)
    await committed.commit()
    assert client.get("/api/v1/config/versions", headers=auth(secret)).status_code == 403

    async def fake_rollback(session, scope, version):
        if version == 404:
            raise LookupError("missing")
        session.add(
            _version(
                scope,
                999,
                kind=ConfigVersionKind.ROLLBACK,
                rollback_of=version,
            )
        )
        await session.flush()
        return 999

    monkeypatch.setattr(compiler, "rollback_routing", fake_rollback)
    resp = client.post("/api/v1/config/rollback", json={"version": 1}, headers=BOOTSTRAP_AUTH)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"version": 999}
    assert (
        client.post(
            "/api/v1/config/rollback", json={"version": 404}, headers=BOOTSTRAP_AUTH
        ).status_code
        == 404
    )

    async with type(committed)(committed.bind) as check:
        actions = (
            await check.scalars(select(AuditLog.action).where(AuditLog.action == "config.rollback"))
        ).all()
        assert actions
