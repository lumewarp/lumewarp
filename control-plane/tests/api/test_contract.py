"""OpenAPI contract, operation ids, and transaction atomicity."""

from __future__ import annotations

from sqlalchemy import func, select

from lumewarp_control_plane.app import create_app
from lumewarp_control_plane.models import AuditLog, ConfigOutbox
from lumewarp_control_plane.settings import Settings
from tests.api.helpers import BOOTSTRAP_AUTH, suffix

EXPECTED_OPERATION_IDS = {
    "createDeployment",
    "createGatewayToken",
    "createKey",
    "createMembership",
    "createOrg",
    "createOwnToken",
    "createProject",
    "createRoute",
    "createTeam",
    "createUpstream",
    "createUser",
    "createUserToken",
    "deleteDeployment",
    "deleteMembership",
    "deleteOrg",
    "deleteProject",
    "deleteRoute",
    "deleteTeam",
    "deleteUpstream",
    "deleteUser",
    "getCurrentUser",
    "getDeployment",
    "getKey",
    "getOrg",
    "getProject",
    "getRoute",
    "getTeam",
    "getUpstream",
    "getUsage",
    "getUser",
    "listAudit",
    "listConfigVersions",
    "listDeployments",
    "listGateways",
    "listKeys",
    "listMemberships",
    "listOrgs",
    "listOwnTokens",
    "listProjects",
    "listRoutes",
    "listTeams",
    "listUpstreams",
    "listUsers",
    "revokeGatewayToken",
    "revokeKey",
    "revokeOwnToken",
    "rollbackConfig",
    "updateDeployment",
    "updateKey",
    "updateMembership",
    "updateOrg",
    "updateProject",
    "updateRoute",
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
    assert len(ids) == 56


async def test_mutation_writes_audit_and_outbox_in_one_transaction(
    client, committed, api_scope
) -> None:
    async with type(committed)(committed.bind) as session:
        audit_before = await session.scalar(select(func.count()).select_from(AuditLog))
        outbox_before = await session.scalar(select(func.count()).select_from(ConfigOutbox))

    name = f"txn-{suffix()}"
    resp = client.post("/api/v1/orgs", json={"name": name}, headers=BOOTSTRAP_AUTH)
    assert resp.status_code == 201, resp.text
    org_id = resp.json()["id"]

    async with type(committed)(committed.bind) as session:
        audit_after = await session.scalar(select(func.count()).select_from(AuditLog))
        outbox_after = await session.scalar(select(func.count()).select_from(ConfigOutbox))
        row = await session.scalar(
            select(AuditLog).where(AuditLog.action == "org.create", AuditLog.resource_id == org_id)
        )
        assert row is not None
        assert row.org_id is not None and str(row.org_id) == org_id
    assert audit_after == audit_before + 1
    assert outbox_after == outbox_before + 1

    # A rejected mutation rolls back the row, audit and outbox together.
    duplicate = client.post("/api/v1/orgs", json={"name": name}, headers=BOOTSTRAP_AUTH)
    assert duplicate.status_code == 409
    async with type(committed)(committed.bind) as session:
        assert await session.scalar(select(func.count()).select_from(AuditLog)) == audit_after
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
        headers=BOOTSTRAP_AUTH,
    )
    assert created.status_code == 201, created.text
    upstream_id = created.json()["id"]
    updated = client.patch(
        f"/api/v1/upstreams/{upstream_id}",
        json={"timeout_ms": 1000},
        headers=BOOTSTRAP_AUTH,
    )
    assert updated.status_code == 200, updated.text
    deleted = client.delete(f"/api/v1/upstreams/{upstream_id}", headers=BOOTSTRAP_AUTH)
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
