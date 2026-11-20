"""Gateway fleet/token management and config versions/rollback."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import delete, select

from lumewarp_control_plane import compiler
from lumewarp_control_plane.models import (
    AuditLog,
    ConfigVersion,
    ConfigVersionKind,
    GatewayPlugin,
    GatewayQuarantine,
    GatewaySession,
    PluginKind,
)
from tests.api.helpers import ADMIN_AUTH, auth, suffix, user_with_token


def _version(scope: str, version: int, *, kind=ConfigVersionKind.COMPILE, rollback_of=None):
    return ConfigVersion(
        zone=scope,
        version=version,
        payload=b"{}",
        digest=b"x" * 32,
        size_bytes=2,
        key_count=0,
        source_revision=version,
        kind=kind,
        rollback_of_version=rollback_of,
    )


async def test_gateways_list_is_admin_only(client, committed) -> None:
    user, secret = await user_with_token(committed)
    await committed.commit()
    assert client.get("/api/v1/gateways", headers=auth(secret)).status_code == 403
    assert client.delete("/api/v1/gateways/default/nope", headers=auth(secret)).status_code == 403


async def test_delete_a_gateway(client, committed, api_scope) -> None:
    now = datetime.now(UTC)
    offline = f"gw-offline-{suffix()}"
    connected = f"gw-connected-{suffix()}"
    committed.add_all(
        [
            GatewaySession(
                zone=api_scope,
                gateway_id=offline,
                connected=False,
                connected_at=now,
                disconnected_at=now,
                last_seen_at=now,
            ),
            GatewaySession(
                zone=api_scope,
                gateway_id=connected,
                connected=True,
                connected_at=now,
                last_seen_at=now,
            ),
        ]
    )
    await committed.commit()
    committed.add(
        GatewayPlugin(
            zone=api_scope,
            gateway_id=offline,
            kind=PluginKind.ROUTER_STRATEGY,
            name="sticky",
            digest="aa",
            descriptor={},
            seen_at=now,
        )
    )
    await committed.commit()

    url = f"/api/v1/gateways/{api_scope}/{offline}"
    deleted = client.delete(url, headers=ADMIN_AUTH)
    assert deleted.status_code == 204, deleted.text
    async with type(committed)(committed.bind) as check:
        assert await check.get(GatewaySession, (api_scope, offline)) is None
        assert (
            await check.scalar(
                select(GatewayPlugin).where(
                    GatewayPlugin.zone == api_scope, GatewayPlugin.gateway_id == offline
                )
            )
            is None
        )
        action = await check.scalar(
            select(AuditLog.action).where(AuditLog.action == "gateway.delete")
        )
        assert action == "gateway.delete"

    # A connected gateway has to disconnect first; an unknown row is 404 either way.
    assert (
        client.delete(f"/api/v1/gateways/{api_scope}/{connected}", headers=ADMIN_AUTH).status_code
        == 409
    )
    assert client.delete(url, headers=ADMIN_AUTH).status_code == 404
    assert (
        client.delete(
            f"/api/v1/gateways/{api_scope}/no-such-gateway", headers=ADMIN_AUTH
        ).status_code
        == 404
    )


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
    connected_id = f"gw-connected-{suffix()}"
    offline_id = f"gw-offline-{suffix()}"
    committed.add_all(
        [
            GatewaySession(
                zone=api_scope,
                gateway_id=connected_id,
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
                zone=api_scope,
                gateway_id=offline_id,
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
    committed.add(GatewayQuarantine(zone=api_scope, quarantined_version=102))
    await committed.commit()
    try:
        listed = client.get("/api/v1/config/versions?limit=500", headers=ADMIN_AUTH).json()
        flags = {item["version"]: item["quarantined"] for item in listed["items"]}
        assert flags[101] is False
        assert flags[102] is True
    finally:
        await committed.execute(
            delete(GatewayQuarantine).where(GatewayQuarantine.zone == api_scope)
        )
        await committed.commit()
