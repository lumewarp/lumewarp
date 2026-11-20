"""Per-zone catalog writes: upstream/model deployment/MCP/A2A zones and recompiles."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from lumewarp_control_plane.compiler import compile_zone
from lumewarp_control_plane.models import (
    A2aAgent,
    ConfigOutbox,
    ConfigVersion,
    GatewayPlugin,
    GatewayQuarantine,
    GatewaySession,
    McpServer,
    ModelDeployment,
    Upstream,
    Zone,
)
from tests.api.helpers import ADMIN_AUTH, deployment_of, suffix

API = "/api/v1"


@pytest.fixture
async def other_zone(api_engine: AsyncEngine, api_scope: str) -> AsyncIterator[str]:
    """A second live zone, and removal of everything this file puts in it.

    `clean_gateways` deletes extra zones after every test, and the FK would refuse while
    an upstream or deployment still points at one, so this fixture (explicit, torn down
    before the autouse ones) removes its own rows.
    """
    name = f"zone-{suffix()}"
    async with AsyncSession(api_engine) as session, session.begin():
        session.add(Zone(id=name, name=name))
    yield name
    async with AsyncSession(api_engine) as session, session.begin():
        await session.execute(delete(ModelDeployment).where(ModelDeployment.zone == name))
        await session.execute(delete(Upstream).where(Upstream.zone == name))
        await session.execute(delete(McpServer).where(McpServer.zone == name))
        await session.execute(delete(A2aAgent).where(A2aAgent.zone == name))
        for table in (GatewayPlugin, GatewaySession, GatewayQuarantine):
            await session.execute(delete(table).where(table.zone == name))
        await session.execute(delete(ConfigOutbox).where(ConfigOutbox.zone == name))
        await session.execute(delete(ConfigVersion).where(ConfigVersion.zone == name))
        await session.execute(delete(Zone).where(Zone.id == name))


def _upstream(client, zone: str | None = None, name: str | None = None):
    body = {
        "name": name or f"up-{suffix()}",
        "kind": "vllm",
        "base_url": "http://127.0.0.1:9/v1",
    }
    if zone is not None:
        body["zone"] = zone
    return client.post(f"{API}/upstreams", json=body, headers=ADMIN_AUTH)


def _model(client, name: str | None = None, **body):
    body = {"name": name or f"m-{suffix()}", "model_type": "llm", **body}
    return client.post(f"{API}/models", json=body, headers=ADMIN_AUTH)


def _deploy(client, model_id: str, zone: str, upstream_id: str, **body):
    return client.put(
        f"{API}/models/{model_id}/deployments/{zone}",
        json={"upstream_id": upstream_id, "upstream_model": "upstream-name", **body},
        headers=ADMIN_AUTH,
    )


async def _outbox(api_engine: AsyncEngine, zone: str) -> int:
    async with AsyncSession(api_engine) as session:
        return await session.scalar(
            select(func.count()).select_from(ConfigOutbox).where(ConfigOutbox.zone == zone)
        )


def test_the_same_upstream_name_may_exist_in_two_zones(client, other_zone) -> None:
    name = f"shared-{suffix()}"
    assert _upstream(client, name=name).status_code == 201
    other = _upstream(client, zone=other_zone, name=name)
    assert other.status_code == 201, other.text
    assert other.json()["zone"] == other_zone
    duplicate = _upstream(client, zone=other_zone, name=name)
    assert duplicate.status_code == 409


def test_a_deployment_must_use_an_upstream_of_its_zone(client, other_zone) -> None:
    model = _model(client).json()
    other_up = _upstream(client, zone=other_zone).json()
    wrong = _deploy(client, model["id"], client.app.state.settings.zone, other_up["id"])
    assert wrong.status_code == 422
    assert "belongs to zone" in wrong.json()["error"]["message"]

    own_up = _upstream(client).json()
    ok = _deploy(client, model["id"], client.app.state.settings.zone, own_up["id"])
    assert ok.status_code == 200, ok.text
    assert ok.json()["zone"] == client.app.state.settings.zone
    assert ok.json()["upstream_name"] == own_up["name"]
    assert ok.json()["upstream_model"] == "upstream-name"

    got = client.get(f"{API}/models/{model['id']}", headers=ADMIN_AUTH).json()
    deployment = deployment_of(got)
    assert (deployment["zone"], deployment["upstream_id"]) == (
        client.app.state.settings.zone,
        own_up["id"],
    )

    assert client.delete(f"{API}/upstreams/{own_up['id']}", headers=ADMIN_AUTH).status_code == 409
    assert (
        client.delete(
            f"{API}/models/{model['id']}/deployments/{client.app.state.settings.zone}",
            headers=ADMIN_AUTH,
        ).status_code
        == 204
    )
    got = client.get(f"{API}/models/{model['id']}", headers=ADMIN_AUTH).json()
    assert got["deployments"] == []
    assert client.delete(f"{API}/upstreams/{own_up['id']}", headers=ADMIN_AUTH).status_code == 204
    assert client.delete(f"{API}/models/{model['id']}", headers=ADMIN_AUTH).status_code == 204


async def test_a_deployment_write_recompiles_only_its_zone(
    client, api_engine: AsyncEngine, api_scope: str, other_zone: str
) -> None:
    model = _model(client).json()
    other_up = _upstream(client, zone=other_zone).json()
    before_other = await _outbox(api_engine, other_zone)
    before_local = await _outbox(api_engine, api_scope)

    assert _deploy(client, model["id"], other_zone, other_up["id"]).status_code == 200
    assert await _outbox(api_engine, other_zone) == before_other + 1
    assert await _outbox(api_engine, api_scope) == before_local

    # Deleting it is a zone write too.
    assert (
        client.delete(
            f"{API}/models/{model['id']}/deployments/{other_zone}", headers=ADMIN_AUTH
        ).status_code
        == 204
    )
    assert await _outbox(api_engine, other_zone) == before_other + 2
    assert await _outbox(api_engine, api_scope) == before_local
    assert client.delete(f"{API}/models/{model['id']}", headers=ADMIN_AUTH).status_code == 204


async def test_mcp_servers_and_agents_carry_and_filter_by_zone(
    client, committed, other_zone: str, api_scope: str
) -> None:
    name = f"shared-{suffix()}"
    local = client.post(
        f"{API}/mcp-servers",
        json={"name": name, "zone": api_scope, "url": "http://a.test/mcp"},
        headers=ADMIN_AUTH,
    )
    assert local.status_code == 201, local.text
    other = client.post(
        f"{API}/mcp-servers",
        json={"name": name, "zone": other_zone, "url": "http://b.test/mcp"},
        headers=ADMIN_AUTH,
    )
    assert other.status_code == 201, other.text
    assert (local.json()["zone"], other.json()["zone"]) == (api_scope, other_zone)

    agent = client.post(
        f"{API}/a2a-agents",
        json={"name": name, "zone": other_zone, "url": "http://c.test/a2a"},
        headers=ADMIN_AUTH,
    )
    assert agent.status_code == 201, agent.text
    assert agent.json()["zone"] == other_zone

    committed.expire_all()
    local_snapshot = await compile_zone(committed, api_scope, require_local_membership=False)
    other_snapshot = await compile_zone(committed, other_zone, require_local_membership=False)
    assert [(s.name, s.url) for s in local_snapshot.snapshot.mcp_servers if s.name == name] == [
        (name, "http://a.test/mcp")
    ]
    assert [(s.name, s.url) for s in other_snapshot.snapshot.mcp_servers if s.name == name] == [
        (name, "http://b.test/mcp")
    ]
    assert [a.name for a in local_snapshot.snapshot.a2a_agents if a.name == name] == []
    assert [a.name for a in other_snapshot.snapshot.a2a_agents if a.name == name] == [name]
