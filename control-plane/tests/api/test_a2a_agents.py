"""A2A agents: CRUD, the middleware plugin, pasted cards and the sealed token."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import delete, select

from lumewarp_control_plane import sealing
from lumewarp_control_plane.compiler.plugins import BUILTIN_PLUGINS, CAPABILITY_PREFIX
from lumewarp_control_plane.models import (
    A2aAgent,
    AuditLog,
    GatewaySession,
    GatewayToken,
)
from tests.api.helpers import ADMIN_AUTH, report_plugins, suffix

H = ADMIN_AUTH
PREFIX = "a2a-test-"
PLAINTEXT = "user-owned-agent-token-4c1d"
MIDDLEWARE_SCHEMA = {
    "type": "object",
    "properties": {"mime": {"type": "string"}},
    "required": ["mime"],
    "additionalProperties": False,
}
BUILTIN_CAPS = [
    f"{CAPABILITY_PREFIX[kind]}:{name}"
    for kind, plugins in BUILTIN_PLUGINS.items()
    for name in plugins
]


def _create(client, **body):
    body = {"name": f"agent-{suffix()}", "url": "http://agent.test/a2a", **body}
    return client.post("/api/v1/a2a-agents", json=body, headers=H)


async def _agent(committed, agent_id: str) -> A2aAgent:
    committed.expire_all()
    return await committed.get(A2aAgent, uuid.UUID(agent_id))


@pytest.fixture
async def gateway(committed, api_scope: str):
    """A connected gateway offering an a2a-middleware plugin; gone after the test."""

    async def report(gateway: str = "one", plugins=None, **kwargs):
        return await report_plugins(
            committed,
            api_scope,
            f"{PREFIX}{gateway}",
            plugins or {("a2a-middleware", "strip_artifacts"): (MIDDLEWARE_SCHEMA, None)},
            connected=True,
            **kwargs,
        )

    yield report
    await committed.rollback()
    await committed.execute(delete(GatewayToken).where(GatewayToken.gateway_id.like(f"{PREFIX}%")))
    await committed.execute(
        delete(GatewaySession).where(GatewaySession.gateway_id.like(f"{PREFIX}%"))
    )
    await committed.commit()


def test_crud_and_the_pasted_card(client, committed) -> None:
    card = json.dumps(
        {
            "name": "agent",
            "description": "test",
            "url": "http://agent.test/a2a",
            "capabilities": {"pushNotifications": True},
        }
    )
    response = _create(
        client,
        card_url="http://agent.test/.well-known/agent-card.json",
        card_json=card,
        timeout_ms=15000,
        auth={"plugin": "passthrough"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["card_url"] == "http://agent.test/.well-known/agent-card.json"
    assert json.loads(body["card_json"])["name"] == "agent"
    assert body["auth"] == {"plugin": "passthrough", "params": {}, "timeout_ms": 0}
    assert body["middleware"] is None
    assert body["auth"]["timeout_ms"] == 0
    assert body["last_card_json"] == "" and body["last_card_error"] == ""
    assert body["config_invalid"] is False

    response = client.patch(
        f"/api/v1/a2a-agents/{body['id']}",
        json={"card_url": None, "card_json": None, "timeout_ms": 0},
        headers=H,
    )
    assert response.status_code == 200, response.text
    assert response.json()["card_url"] == "" and response.json()["card_json"] == ""

    assert client.delete(f"/api/v1/a2a-agents/{body['id']}", headers=H).status_code == 204
    assert client.get(f"/api/v1/a2a-agents/{body['id']}", headers=H).status_code == 404


def test_input_rules(client) -> None:
    for name in ("Bad_Name".lower().upper(), "a__b", ""):
        body = {"name": name, "url": "http://agent.test/a2a"}
        assert client.post("/api/v1/a2a-agents", json=body, headers=H).status_code == 422
    assert _create(client, url="ftp://agent.test/a2a").status_code == 422
    assert _create(client, card_url="mailto:x@y").status_code == 422
    assert _create(client, card_json="not json").status_code == 422
    assert _create(client, card_json="[]").status_code == 422
    assert _create(client, card_json='{"a": "' + "x" * 70_000 + '"}').status_code == 422
    first = _create(client)
    assert _create(client, name=first.json()["name"]).status_code == 409


async def test_the_static_bearer_is_sealed_and_write_only(client, committed, api_settings) -> None:
    response = _create(client, auth={"plugin": "static_bearer", "secret": PLAINTEXT})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["secret_set"] is True
    assert PLAINTEXT not in response.text
    row = await _agent(committed, body["id"])
    assert PLAINTEXT.encode() not in row.secret_ciphertext
    assert (
        sealing.unseal(api_settings, row.secret_ciphertext, info=sealing.AUTH_SECRET_INFO)
        == PLAINTEXT
    )
    for entry in await committed.scalars(
        select(AuditLog).where(AuditLog.resource_id == body["id"])
    ):
        assert PLAINTEXT not in json.dumps(entry.details)


async def test_the_middleware_plugin_needs_force_when_a_gateway_lacks_it(
    client, committed, gateway
) -> None:
    assert (
        _create(
            client, middleware={"plugin": "strip_artifacts", "params": {"mime": "x/y"}}
        ).status_code
        == 422
    )
    await gateway("one")
    response = _create(client, middleware={"plugin": "strip_artifacts", "params": {"mime": "x/y"}})
    assert response.status_code == 201, response.text
    assert response.json()["middleware"] == {
        "plugin": "strip_artifacts",
        "params": {"mime": "x/y"},
        "timeout_ms": 0,
    }
    # A middleware call's timeout round-trips, and only a WASM plugin uses it.
    timed = _create(
        client,
        middleware={"plugin": "strip_artifacts", "params": {"mime": "a/b"}, "timeout_ms": 900},
        auth={"plugin": "passthrough", "timeout_ms": 700},
    )
    assert timed.status_code == 201, timed.text
    assert timed.json()["middleware"]["timeout_ms"] == 900
    assert timed.json()["auth"]["timeout_ms"] == 700
    assert (
        _create(
            client, middleware={"plugin": "strip_artifacts", "params": {}, "timeout_ms": -1}
        ).status_code
        == 422
    )

    # A second connected gateway without it: force is required and audited.
    await gateway("two", capabilities=BUILTIN_CAPS)
    response = _create(client, middleware={"plugin": "strip_artifacts", "params": {"mime": "a/b"}})
    assert response.status_code == 422
    response = _create(
        client,
        middleware={"plugin": "strip_artifacts", "params": {"mime": "a/b"}},
        force=True,
    )
    assert response.status_code == 201, response.text
    actions = [
        entry.action
        for entry in await committed.scalars(
            select(AuditLog).where(AuditLog.resource_id == response.json()["id"])
        )
    ]
    assert "a2a_agent.force_save" in actions

    # A setting the schema rejects is refused even with force; null clears the middleware.
    bad = _create(
        client, middleware={"plugin": "strip_artifacts", "params": {"mime": 7}}, force=True
    )
    assert bad.status_code == 422
    cleared = client.patch(
        f"/api/v1/a2a-agents/{response.json()['id']}",
        json={"middleware": None, "name": f"agent-{suffix()}"},
        headers=H,
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["middleware"] is None


async def test_config_invalid_reports_a_vanished_middleware(client, committed) -> None:
    body = _create(client).json()
    row = await _agent(committed, body["id"])
    row.middleware_plugin = "ghost"
    committed.add(row)
    await committed.commit()
    listed = client.get(f"/api/v1/a2a-agents/{body['id']}", headers=H).json()
    assert listed["config_invalid"] is True
    assert "no gateway reports" in listed["config_problems"][0]
