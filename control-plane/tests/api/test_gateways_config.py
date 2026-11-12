"""Gateway fleet/token management and config versions/rollback."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select, update

from lumewarp_control_plane import compiler
from lumewarp_control_plane.models import (
    AuditLog,
    ConfigVersion,
    ConfigVersionKind,
    GatewayQuarantine,
    GatewaySession,
)
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


async def test_gateway_tokens(client, committed, api_scope) -> None:
    gateway_id = f"gw-{suffix()}"
    resp = client.post(
        "/api/v1/gateways",
        json={"gateway_id": gateway_id, "name": "primary"},
        headers=ADMIN_AUTH,
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()
    token = created["token"]
    assert created["secret"].startswith("lwg-")
    assert "hash" not in token
    assert token["gateway_id"] == gateway_id

    listed = client.get("/api/v1/gateways", headers=ADMIN_AUTH)
    assert listed.status_code == 200
    row = next(item for item in listed.json()["items"] if item["gateway_id"] == gateway_id)
    assert row["connected"] is False
    assert row["tokens"][0]["display_prefix"] == token["display_prefix"]

    # A session row is reflected in the fleet view.
    committed.add(
        GatewaySession(
            scope=api_scope,
            gateway_id=gateway_id,
            instance_id="i1",
            connected=True,
            gateway_version="0.1",
            acked_version=3,
            last_seen_at=datetime.now(UTC),
        )
    )
    await committed.commit()
    listed = client.get("/api/v1/gateways?limit=500", headers=ADMIN_AUTH)
    row = next(item for item in listed.json()["items"] if item["gateway_id"] == gateway_id)
    assert row["connected"] is True
    assert row["acked_version"] == 3

    revoked = client.post(
        f"/api/v1/gateways/{gateway_id}/tokens/{token['id']}/revoke",
        headers=ADMIN_AUTH,
    )
    assert revoked.status_code == 200
    assert revoked.json()["revoked_at"] is not None
    # Wrong gateway id does not match the token.
    assert (
        client.post(
            f"/api/v1/gateways/other/tokens/{token['id']}/revoke",
            headers=ADMIN_AUTH,
        ).status_code
        == 404
    )

    user, secret = await user_with_token(committed)
    await committed.commit()
    assert client.get("/api/v1/gateways", headers=auth(secret)).status_code == 403


async def test_config_versions_and_rollback(client, committed, api_scope, monkeypatch) -> None:
    committed.add_all([_version(api_scope, 1), _version(api_scope, 2)])
    await committed.commit()

    listed = client.get("/api/v1/config/versions?limit=1", headers=ADMIN_AUTH)
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["items"][0]["version"] == 2
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


async def test_gateway_state_is_derived_from_its_instances(client, committed, api_scope) -> None:
    """One gateway id, three processes: the row is their aggregate (docs/config-protocol.md)."""
    gateway_id = f"gw-{suffix()}"
    now = datetime.now(UTC)
    committed.add_all(
        [
            GatewaySession(
                scope=api_scope,
                gateway_id=gateway_id,
                instance_id="pod-1-aa",
                connected=True,
                gateway_version="0.2.0",
                capabilities=["retries", "strategy:weighted"],
                acked_version=4,
                connected_at=now,
                last_seen_at=now,
                shared_limits_enabled=True,
                shared_limits_namespace="team-ns",
            ),
            GatewaySession(
                scope=api_scope,
                gateway_id=gateway_id,
                instance_id="pod-2-bb",
                connected=True,
                gateway_version="0.3.0",
                capabilities=["retries"],
                acked_version=3,
                pending_version=5,
                last_nack_version=4,
                last_nack_code="NACK_CODE_INVALID_CONFIG",
                last_nack_reason="invalid config",
                connected_at=now,
                last_seen_at=now,
            ),
            GatewaySession(
                scope=api_scope,
                gateway_id=gateway_id,
                instance_id="pod-0-zz",
                connected=False,
                gateway_version="0.1.0",
                capabilities=["retries", "strategy:weighted"],
                acked_version=2,
                connected_at=now,
                disconnected_at=now,
                last_seen_at=now,
            ),
        ]
    )
    committed.add(GatewayQuarantine(scope=api_scope, gateway_id=gateway_id, quarantined_version=4))
    await committed.commit()

    listed = client.get("/api/v1/gateways?limit=500", headers=ADMIN_AUTH)
    row = next(item for item in listed.json()["items"] if item["gateway_id"] == gateway_id)
    assert row["connected"] is True
    # The lowest ACK of the connected instances; the pending version of any; the newest NACK.
    assert (row["acked_version"], row["pending_version"]) == (3, 5)
    assert (row["last_nack_version"], row["last_nack_code"]) == (4, "NACK_CODE_INVALID_CONFIG")
    assert row["last_nack_reason"] == "invalid config"
    assert row["quarantined_version"] == 4
    # Every connected instance must report it, and the versions are listed when they differ.
    assert row["capabilities"] == ["retries"]
    assert row["gateway_version"] == "0.2.0, 0.3.0"
    assert row["disconnected_at"] is None
    assert [item["instance_id"] for item in row["instances"]] == [
        "pod-0-zz",
        "pod-1-aa",
        "pod-2-bb",
    ]
    shared = {
        item["instance_id"]: (item["shared_limits_enabled"], item["shared_limits_namespace"])
        for item in row["instances"]
    }
    assert shared == {
        "pod-0-zz": (False, ""),
        "pod-1-aa": (True, "team-ns"),
        "pod-2-bb": (False, ""),
    }

    # With nothing connected the row falls back to the instances that are left.
    async with type(committed)(committed.bind) as session, session.begin():
        await session.execute(
            update(GatewaySession)
            .where(GatewaySession.scope == api_scope, GatewaySession.gateway_id == gateway_id)
            .values(connected=False, disconnected_at=now)
        )
    listed = client.get("/api/v1/gateways?limit=500", headers=ADMIN_AUTH)
    row = next(item for item in listed.json()["items"] if item["gateway_id"] == gateway_id)
    assert row["connected"] is False
    assert row["pending_version"] is None
    assert row["disconnected_at"] is not None
    assert row["acked_version"] == 2


async def test_a_nack_is_only_shown_while_it_is_still_true(client, committed, api_scope) -> None:
    """A recovered gateway shows no NACK; a live instance's stale one does not linger for 24 h."""
    gateway_id = f"gw-{suffix()}"
    now = datetime.now(UTC)
    committed.add_all(
        [
            GatewaySession(
                scope=api_scope,
                gateway_id=gateway_id,
                instance_id="pod-1-aa",
                connected=True,
                acked_version=6,
                connected_at=now,
                last_seen_at=now,
            ),
            GatewaySession(
                scope=api_scope,
                gateway_id=gateway_id,
                instance_id="pod-2-bb",
                connected=True,
                acked_version=6,
                last_nack_version=5,
                last_nack_code="NACK_CODE_INVALID_CONFIG",
                last_nack_reason="invalid config",
                connected_at=now,
                last_seen_at=now,
            ),
        ]
    )
    await committed.commit()

    def status() -> dict:
        listed = client.get("/api/v1/gateways?limit=500", headers=ADMIN_AUTH)
        return next(item for item in listed.json()["items"] if item["gateway_id"] == gateway_id)

    async def set_instances(**values) -> None:
        async with type(committed)(committed.bind) as session, session.begin():
            await session.execute(
                update(GatewaySession)
                .where(GatewaySession.scope == api_scope, GatewaySession.gateway_id == gateway_id)
                .values(**values)
            )

    # Every instance is at 6: the NACK of 5 no longer applies, even though its row lives on.
    row = status()
    assert (row["acked_version"], row["last_nack_version"]) == (6, None)
    assert (row["last_nack_code"], row["last_nack_reason"]) == (None, None)

    # One instance is still on 4: the NACK of 5 is exactly why.
    async with type(committed)(committed.bind) as session, session.begin():
        await session.execute(
            update(GatewaySession)
            .where(
                GatewaySession.scope == api_scope,
                GatewaySession.gateway_id == gateway_id,
                GatewaySession.instance_id == "pod-2-bb",
            )
            .values(acked_version=4)
        )
    row = status()
    assert (row["acked_version"], row["last_nack_version"]) == (4, 5)
    assert row["last_nack_reason"] == "invalid config"

    # A disconnected instance's NACK does not count while another one is connected and current.
    async with type(committed)(committed.bind) as session, session.begin():
        await session.execute(
            update(GatewaySession)
            .where(
                GatewaySession.scope == api_scope,
                GatewaySession.gateway_id == gateway_id,
                GatewaySession.instance_id == "pod-2-bb",
            )
            .values(connected=False, disconnected_at=now)
        )
    row = status()
    assert (row["acked_version"], row["last_nack_version"]) == (6, None)

    # With nothing connected the remaining rows are all there is left to report.
    await set_instances(connected=False, disconnected_at=now)
    row = status()
    assert (row["acked_version"], row["last_nack_version"]) == (4, 5)
