"""Gateway fleet/token management and config versions/rollback."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import delete, select

from lumewarp_control_plane import compiler
from lumewarp_control_plane.models import (
    AuditLog,
    ConfigVersion,
    ConfigVersionKind,
    GatewayQuarantine,
    GatewaySession,
    GatewayToken,
)
from lumewarp_control_plane.security import SecretKind, generate_secret
from tests.api.helpers import ADMIN_AUTH, auth, suffix, user_with_token


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


def _token(scope: str, name: str) -> GatewayToken:
    secret = generate_secret(SecretKind.GATEWAY_TOKEN)
    return GatewayToken(
        scope=scope,
        name=name,
        token_hash=secret.hash,
        display_prefix=secret.display_prefix,
    )


async def test_gateway_tokens(client, committed, api_scope) -> None:
    created_resp = client.post(
        "/api/v1/gateway-tokens", json={"name": "primary"}, headers=ADMIN_AUTH
    )
    assert created_resp.status_code == 201, created_resp.text
    created = created_resp.json()
    token = created["token"]
    assert created["secret"].startswith("lwg-")
    assert "hash" not in token
    assert (token["name"], token["connected_gateways"]) == ("primary", 0)

    listed = client.get("/api/v1/gateway-tokens", headers=ADMIN_AUTH)
    assert listed.status_code == 200
    row = next(item for item in listed.json()["items"] if item["id"] == token["id"])
    assert row["display_prefix"] == token["display_prefix"]

    # A session row is one gateway, and it names the token it authenticated with.
    committed.add(
        GatewaySession(
            scope=api_scope,
            gateway_id=f"gw-{suffix()}",
            token_id=token["id"],
            connected=True,
            gateway_version="0.1",
            acked_version=3,
            last_seen_at=datetime.now(UTC),
        )
    )
    await committed.commit()
    listed = client.get("/api/v1/gateway-tokens", headers=ADMIN_AUTH)
    row = next(item for item in listed.json()["items"] if item["id"] == token["id"])
    assert row["connected_gateways"] == 1

    fleet = client.get("/api/v1/gateways?limit=500", headers=ADMIN_AUTH)
    assert fleet.status_code == 200
    gateway = next(item for item in fleet.json()["items"] if item["token"]["id"] == token["id"])
    assert gateway["connected"] is True
    assert gateway["acked_version"] == 3
    assert gateway["token"] == {
        "id": token["id"],
        "name": "primary",
        "display_prefix": token["display_prefix"],
    }

    revoked = client.post(f"/api/v1/gateway-tokens/{token['id']}/revoke", headers=ADMIN_AUTH)
    assert revoked.status_code == 200
    assert revoked.json()["revoked_at"] is not None
    assert (
        client.post(
            "/api/v1/gateway-tokens/00000000-0000-0000-0000-000000000000/revoke", headers=ADMIN_AUTH
        ).status_code
        == 404
    )

    user, secret = await user_with_token(committed)
    await committed.commit()
    assert client.get("/api/v1/gateways", headers=auth(secret)).status_code == 403
    assert client.get("/api/v1/gateway-tokens", headers=auth(secret)).status_code == 403


async def test_config_versions_and_rollback(client, committed, api_scope, monkeypatch) -> None:
    committed.add_all([_version(api_scope, 1), _version(api_scope, 2)])
    await committed.commit()

    listed = client.get("/api/v1/config/versions?limit=1", headers=ADMIN_AUTH)
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["items"][0]["version"] == 2
    assert body["items"][0]["quarantined"] is False
    assert body["next_cursor"] == "2"
    second = client.get(
        f"/api/v1/config/versions?limit=1&cursor={body['next_cursor']}",
        headers=ADMIN_AUTH,
    )
    assert second.status_code == 200
    assert second.json()["items"][0]["version"] == 1

    user, secret = await user_with_token(committed)
    await committed.commit()
    assert client.get("/api/v1/config/versions", headers=auth(secret)).status_code == 403

    async def fake_rollback(session, scope, version, *, require_local_membership, **_):
        assert require_local_membership is True
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
    resp = client.post("/api/v1/config/rollback", json={"version": 1}, headers=ADMIN_AUTH)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"version": 999}
    assert (
        client.post(
            "/api/v1/config/rollback", json={"version": 404}, headers=ADMIN_AUTH
        ).status_code
        == 404
    )

    async with type(committed)(committed.bind) as check:
        actions = (
            await check.scalars(select(AuditLog.action).where(AuditLog.action == "config.rollback"))
        ).all()
        assert actions


def test_rollback_rejected_by_strategy_rules_is_422_and_force_reaches_it(client, monkeypatch):
    from lumewarp_control_plane import compiler
    from lumewarp_control_plane.compiler.plugins import PluginRejected

    seen = []

    async def refuse(session, scope, version, *, require_local_membership, force, principal):
        seen.append((force, principal is not None))
        raise PluginRejected("strategy", "router 'r': no gateway reports strategy 's'")

    monkeypatch.setattr(compiler, "rollback_routing", refuse)
    for body, force in (({"version": 1}, False), ({"version": 1, "force": True}, True)):
        resp = client.post("/api/v1/config/rollback", json=body, headers=ADMIN_AUTH)
        assert resp.status_code == 422, resp.text
        assert "no gateway reports" in resp.json()["error"]["message"]
        assert seen.pop() == (force, True)


async def test_gateways_are_listed_one_per_process_connected_first(
    client, committed, api_scope
) -> None:
    now = datetime.now(UTC)
    token = _token(api_scope, "fleet")
    committed.add(token)
    await committed.flush()
    connected_id = f"gw-connected-{suffix()}"
    offline_id = f"gw-offline-{suffix()}"
    committed.add_all(
        [
            GatewaySession(
                scope=api_scope,
                gateway_id=connected_id,
                token_id=token.id,
                connected=True,
                peer="127.0.0.1:1",
                gateway_version="0.2.0",
                capabilities=["retries", "strategy:weighted"],
                acked_version=4,
                pending_version=5,
                last_nack_version=4,
                last_nack_code="NACK_CODE_INVALID_CONFIG",
                last_nack_reason="invalid config",
                connected_at=now,
                last_seen_at=now,
                shared_limits_enabled=True,
                shared_limits_namespace="team-ns",
            ),
            GatewaySession(
                scope=api_scope,
                gateway_id=offline_id,
                token_id=token.id,
                connected=False,
                gateway_version="0.1.0",
                capabilities=["retries"],
                acked_version=2,
                connected_at=now,
                disconnected_at=now,
                last_seen_at=now,
            ),
        ]
    )
    await committed.commit()

    listed = client.get("/api/v1/gateways?limit=500", headers=ADMIN_AUTH)
    items = listed.json()["items"]
    connected = next(item for item in items if item["gateway_id"] == connected_id)
    assert connected["capabilities"] == ["retries", "strategy:weighted"]
    assert (connected["acked_version"], connected["pending_version"]) == (4, 5)
    assert (connected["last_nack_version"], connected["last_nack_code"]) == (
        4,
        "NACK_CODE_INVALID_CONFIG",
    )
    assert connected["last_nack_reason"] == "invalid config"
    assert (connected["shared_limits_enabled"], connected["shared_limits_namespace"]) == (
        True,
        "team-ns",
    )
    offline = next(item for item in items if item["gateway_id"] == offline_id)
    assert offline["connected"] is False
    assert offline["disconnected_at"] is not None
    assert offline["token"]["name"] == "fleet"
    # Connected rows sort before disconnected ones; each group is ordered by gateway id.
    states = [item["connected"] for item in items]
    assert states == sorted(states, reverse=True)
    for group in (True, False):
        ids = [item["gateway_id"] for item in items if item["connected"] is group]
        assert ids == sorted(ids)


async def test_config_version_marks_the_scope_quarantined_version(
    client, committed, api_scope
) -> None:
    committed.add_all([_version(api_scope, 101), _version(api_scope, 102)])
    committed.add(GatewayQuarantine(scope=api_scope, quarantined_version=102))
    await committed.commit()
    try:
        listed = client.get("/api/v1/config/versions?limit=500", headers=ADMIN_AUTH).json()
        flags = {item["version"]: item["quarantined"] for item in listed["items"]}
        assert flags[101] is False
        assert flags[102] is True
    finally:
        await committed.execute(
            delete(GatewayQuarantine).where(GatewayQuarantine.scope == api_scope)
        )
        await committed.commit()
