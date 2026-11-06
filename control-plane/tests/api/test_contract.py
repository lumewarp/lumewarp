"""OpenAPI contract, operation ids, and transaction atomicity."""

from __future__ import annotations

from sqlalchemy import func, select

from lumewarp_control_plane.app import create_app
from lumewarp_control_plane.models import AuditLog, ConfigOutbox
from lumewarp_control_plane.settings import Settings
from tests.api.helpers import ADMIN_AUTH, create_org, suffix

EXPECTED_OPERATION_IDS = {
    "createAlias",
    "deleteAlias",
    "getAlias",
    "listAliases",
    "listCatalog",
    "updateAlias",
    "completeOidcLogin",
    "createModel",
    "createGatewayToken",
    "createOrg",
    "createKey",
    "createMember",
    "createOwnToken",
    "createRouter",
    "createServiceAccount",
    "createTeam",
    "createUpstream",
    "createUser",
    "createUserToken",
    "deleteModel",
    "deleteMember",
    "deleteOrg",
    "deleteOrgAdmin",
    "deleteRouter",
    "deleteServiceAccount",
    "deleteTeam",
    "deleteInstallationAdmin",
    "deleteUpstream",
    "deleteUser",
    "getCurrentUser",
    "getModel",
    "getKey",
    "getOrg",
    "getOrgUsage",
    "getRouter",
    "getServiceAccount",
    "getSession",
    "getTeam",
    "getUpstream",
    "getUsage",
    "getUser",
    "listAudit",
    "listConfigVersions",
    "listModels",
    "listGateways",
    "listOrgs",
    "listOrgAdmins",
    "listInstallationAdmins",
    "listMembers",
    "listOwnKeys",
    "listOwnTokens",
    "listRouters",
    "listServiceAccounts",
    "listTeamKeys",
    "listTeams",
    "listUpstreams",
    "listStrategies",
    "listUsers",
    "logoutSession",
    "loginBreakGlass",
    "putInstallationAdmin",
    "putMember",
    "putOrgAdmin",
    "revokeGatewayToken",
    "revokeKey",
    "revokeOwnToken",
    "rollbackConfig",
    "rotateKey",
    "startOidcLogin",
    "transferProxyAdmin",
    "updateModel",
    "updateKey",
    "updateOrg",
    "updateMember",
    "updateRouter",
    "updateServiceAccount",
    "updateTeam",
    "updateUpstream",
    "updateUser",
}


def test_openapi_operation_ids_are_stable_and_unique() -> None:
    spec = create_app(
        Settings(
            database_url="postgresql+asyncpg://nobody:nothing@127.0.0.1:1/none",
            background_enabled=False,
        )
    ).openapi()
    ids: list[str] = []
    for path, operations in spec["paths"].items():
        if not path.startswith("/api/v1"):
            continue
        for operation in operations.values():
            ids.append(operation["operationId"])
    assert len(ids) == len(set(ids))
    assert set(ids) == EXPECTED_OPERATION_IDS


async def test_mutation_writes_audit_and_outbox_in_one_transaction(
    client, committed, api_scope
) -> None:
    org = create_org(client)
    async with type(committed)(committed.bind) as session:
        audit_before = await session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action != "session.break_glass.request")
        )
        outbox_before = await session.scalar(select(func.count()).select_from(ConfigOutbox))

    name = f"txn-{suffix()}"
    resp = client.post(
        "/api/v1/teams", json={"name": name, "org_id": org["id"]}, headers=ADMIN_AUTH
    )
    assert resp.status_code == 201, resp.text
    team_id = resp.json()["id"]

    async with type(committed)(committed.bind) as session:
        audit_after = await session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action != "session.break_glass.request")
        )
        outbox_after = await session.scalar(select(func.count()).select_from(ConfigOutbox))
        row = await session.scalar(
            select(AuditLog).where(
                AuditLog.action == "team.create", AuditLog.resource_id == team_id
            )
        )
        assert row is not None
        assert str(row.team_id) == team_id
    assert audit_after == audit_before + 1
    assert outbox_after == outbox_before + 1

    # A rejected mutation rolls back the row, audit and outbox together.
    duplicate = client.post(
        "/api/v1/teams", json={"name": name, "org_id": org["id"]}, headers=ADMIN_AUTH
    )
    assert duplicate.status_code == 409
    async with type(committed)(committed.bind) as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action != "session.break_glass.request")
            )
            == audit_after
        )
        assert await session.scalar(select(func.count()).select_from(ConfigOutbox)) == outbox_after


async def test_routing_mutations_are_outboxed(client, committed, api_scope) -> None:
    async with type(committed)(committed.bind) as session:
        before = await session.scalar(select(func.count()).select_from(ConfigOutbox))
    name = f"outbox-up-{suffix()}"
    created = client.post(
        "/api/v1/upstreams",
        json={
            "name": name,
            "kind": "vllm",
            "base_url": "http://127.0.0.1:9000/v1",
        },
        headers=ADMIN_AUTH,
    )
    assert created.status_code == 201, created.text
    upstream_id = created.json()["id"]
    updated = client.patch(
        f"/api/v1/upstreams/{upstream_id}",
        json={"timeout_ms": 1000},
        headers=ADMIN_AUTH,
    )
    assert updated.status_code == 200, updated.text
    deleted = client.delete(f"/api/v1/upstreams/{upstream_id}", headers=ADMIN_AUTH)
    assert deleted.status_code == 204
    async with type(committed)(committed.bind) as session:
        after = await session.scalar(select(func.count()).select_from(ConfigOutbox))
        audit_rows = (
            await session.scalars(
                select(AuditLog.action).where(
                    AuditLog.resource_id == upstream_id,
                )
            )
        ).all()
    assert after == before + 3
    assert set(audit_rows) == {"upstream.create", "upstream.update", "upstream.delete"}
