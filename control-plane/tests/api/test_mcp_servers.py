"""MCP servers: CRUD, secrets, `force`, definitions and Discover now."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane import sealing
from lumewarp_control_plane.compiler.plugins import (
    BUILTIN_PLUGINS,
    CAPABILITY_PREFIX,
)
from lumewarp_control_plane.mcp import discover, hashing
from lumewarp_control_plane.models import (
    AuditLog,
    GatewaySession,
    GatewayToken,
    McpDefinition,
    McpDefinitionKind,
    McpDefinitionState,
    McpReviewMode,
    McpServer,
)
from tests.api.helpers import ADMIN_AUTH, report_plugins, suffix

H = ADMIN_AUTH
PREFIX = "mcp-test-"
PLAINTEXT = "sk-mcp-private-token-91b2"
AUTH_SCHEMA = {
    "type": "object",
    "properties": {"ttl_ms": {"type": "integer", "minimum": 1}},
    "required": ["ttl_ms"],
    "additionalProperties": False,
}


def _create(client, **body):
    body = {"name": f"srv-{suffix()}", "url": "http://mcp.test/mcp", **body}
    return client.post("/api/v1/mcp-servers", json=body, headers=H)


async def _row(committed: AsyncSession, server_id: str) -> McpServer:
    committed.expire_all()
    return await committed.get(McpServer, uuid.UUID(server_id))


async def _audit(committed: AsyncSession, resource_id: str) -> list[AuditLog]:
    committed.expire_all()
    return list(
        (
            await committed.scalars(
                select(AuditLog).where(AuditLog.resource_id == resource_id).order_by(AuditLog.id)
            )
        ).all()
    )


BUILTIN_CAPS = [
    f"{CAPABILITY_PREFIX[kind]}:{name}"
    for kind, plugins in BUILTIN_PLUGINS.items()
    for name in plugins
]


@pytest.fixture
async def gateway(committed: AsyncSession, api_scope: str) -> AsyncIterator:
    """A connected gateway reporting a WASM auth plugin; gone after the test."""

    async def report(gateway: str = "one", plugins=None, **kwargs):
        return await report_plugins(
            committed,
            api_scope,
            f"{PREFIX}{gateway}",
            plugins or {("upstream-auth", "hmac_signature"): (AUTH_SCHEMA, None)},
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


def test_create_read_update_and_delete(client, committed) -> None:
    response = _create(
        client,
        url="https://mcp.example.test/mcp?tenant=1",
        timeout_ms=5000,
        review_mode="manual",
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["auth"] == {"plugin": "none", "params": {}, "timeout_ms": 0}
    assert body["secret_set"] is False and body["secret_ref"] is None
    assert body["review_mode"] == "manual"
    assert body["definitions"] == {"approved": 0, "pending": 0, "rejected": 0}
    assert body["config_invalid"] is False

    listed = client.get("/api/v1/mcp-servers", headers=H).json()
    assert [item["id"] for item in listed["items"]] == [body["id"]]

    response = client.patch(
        f"/api/v1/mcp-servers/{body['id']}",
        json={"name": f"renamed-{suffix()}", "timeout_ms": 0, "review_mode": "off"},
        headers=H,
    )
    assert response.status_code == 200, response.text
    assert response.json()["timeout_ms"] == 0 and response.json()["review_mode"] == "off"

    # Archive: gone from reads, the name free again, the row kept.
    assert client.delete(f"/api/v1/mcp-servers/{body['id']}", headers=H).status_code == 204
    assert client.get(f"/api/v1/mcp-servers/{body['id']}", headers=H).status_code == 404
    replaced = _create(client, name=response.json()["name"])
    assert replaced.status_code == 201, replaced.text
    assert client.get("/api/v1/mcp-servers", headers=H).json()["items"] != []


def test_the_auth_plugin_timeout_round_trips(client) -> None:
    """A WASM auth call's timeout: 0 means the gateway default (60 s)."""
    response = _create(
        client, auth={"plugin": "static_bearer", "secret_ref": "MCP_TOKEN", "timeout_ms": 1500}
    )
    assert response.status_code == 201, response.text
    assert response.json()["auth"] == {
        "plugin": "static_bearer",
        "params": {},
        "timeout_ms": 1500,
    }
    server_id = response.json()["id"]
    changed = client.patch(
        f"/api/v1/mcp-servers/{server_id}",
        json={"auth": {"plugin": "none", "timeout_ms": 0}},
        headers=H,
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["auth"]["timeout_ms"] == 0
    for bad in (-1, 2**31):
        request = _create(client, auth={"plugin": "none", "timeout_ms": bad})
        assert request.status_code == 422, bad


def test_name_and_url_rules(client) -> None:
    for name in ("Uppercase", "has__separator", "1starts-with-digit", "with space", ""):
        response = _create(client, name=name or "x")
        if name == "":
            response = client.post(
                "/api/v1/mcp-servers",
                json={"name": "", "url": "http://mcp.test/mcp"},
                headers=H,
            )
        assert response.status_code == 422, (name, response.text)
    for url in ("ftp://mcp.test/mcp", "http://user:pw@mcp.test/mcp", "mcp.test/mcp", "/mcp"):
        assert _create(client, url=url).status_code == 422, url
    first = _create(client)
    duplicate = _create(client, name=first.json()["name"])
    assert duplicate.status_code == 409


async def test_a_static_bearer_is_sealed_never_returned_and_never_audited(
    client, committed, api_settings
) -> None:
    response = _create(
        client,
        auth={"plugin": "static_bearer", "params": {}, "secret": PLAINTEXT},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["secret_set"] is True
    assert PLAINTEXT not in response.text
    assert PLAINTEXT not in client.get("/api/v1/mcp-servers", headers=H).text
    row = await _row(committed, body["id"])
    assert row.secret_ciphertext is not None
    assert PLAINTEXT.encode() not in row.secret_ciphertext
    assert (
        sealing.unseal(api_settings, row.secret_ciphertext, info=sealing.AUTH_SECRET_INFO)
        == PLAINTEXT
    )
    # A routing API key sealed under the other HKDF label does not open this one.
    with pytest.raises(sealing.SealedInvalid):
        sealing.unseal(api_settings, row.secret_ciphertext)

    for entry in await _audit(committed, body["id"]):
        assert PLAINTEXT not in json.dumps(entry.details)

    # Switching to an env reference drops the stored secret; null removes it.
    response = client.patch(
        f"/api/v1/mcp-servers/{body['id']}",
        json={"auth": {"plugin": "static_bearer", "secret_ref": "MCP_TOKEN"}},
        headers=H,
    )
    assert response.status_code == 200, response.text
    assert response.json()["secret_set"] is False
    assert response.json()["secret_ref"] == "MCP_TOKEN"
    assert (await _row(committed, body["id"])).secret_ciphertext is None


def test_the_auth_credential_rules(client) -> None:
    # static_bearer needs exactly one source; only static_bearer takes one.
    assert _create(client, auth={"plugin": "static_bearer"}).status_code == 422
    both = _create(
        client, auth={"plugin": "static_bearer", "secret": "x", "secret_ref": "MCP_TOKEN"}
    )
    assert both.status_code == 422
    assert _create(client, auth={"plugin": "none", "secret": "x"}).status_code == 422
    assert _create(client, auth={"plugin": "passthrough", "secret_ref": "T"}).status_code == 422
    # Built-ins take no params.
    assert _create(client, auth={"plugin": "none", "params": {"x": 1}}).status_code == 422


async def test_an_unknown_plugin_needs_force_and_is_audited(client, committed, gateway) -> None:
    # No gateway reports it yet.
    response = _create(client, auth={"plugin": "hmac_signature", "params": {"ttl_ms": 1000}})
    assert response.status_code == 422
    assert "no gateway reports" in response.json()["error"]["message"]

    await gateway("one")
    # The only connected gateway has it, so it saves without force.
    response = _create(client, auth={"plugin": "hmac_signature", "params": {"ttl_ms": 1000}})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["config_invalid"] is False

    # A second connected gateway without it: force is required and audited.
    await gateway("two", capabilities=BUILTIN_CAPS)
    response = _create(client, auth={"plugin": "hmac_signature", "params": {"ttl_ms": 1}})
    assert response.status_code == 422
    assert "connected gateways lack" in response.json()["error"]["message"]
    response = _create(
        client, auth={"plugin": "hmac_signature", "params": {"ttl_ms": 1}}, force=True
    )
    assert response.status_code == 201, response.text
    actions = [entry.action for entry in await _audit(committed, response.json()["id"])]
    assert "mcp_server.force_save" in actions

    # A bad setting is refused even with force.
    response = _create(
        client, auth={"plugin": "hmac_signature", "params": {"ttl_ms": 0}}, force=True
    )
    assert response.status_code == 422


async def test_a_platform_reserved_param_is_accepted_for_its_kind(client, gateway) -> None:
    """Review-w1 M3: `allowed_origins` belongs to upstream-auth, so a schema that does not
    declare it must not reject it (the gateway removes it before `configure`)."""
    await gateway(
        "one",
        plugins={
            ("upstream-auth", "oidc_fetch"): (
                {
                    "type": "object",
                    "properties": {"audience": {"type": "string"}},
                    "required": ["audience"],
                    "additionalProperties": False,
                },
                None,
            )
        },
    )
    response = _create(
        client,
        auth={
            "plugin": "oidc_fetch",
            "params": {"audience": "lumewarp", "allowed_origins": ["https://idp.example"]},
        },
    )
    assert response.status_code == 201, response.text
    # A malformed origin is refused before anything is stored.
    bad = _create(
        client,
        auth={
            "plugin": "oidc_fetch",
            "params": {"audience": "a", "allowed_origins": ["https://idp.example/token"]},
        },
    )
    assert bad.status_code == 422
    assert "allowed_origins" in bad.json()["error"]["message"]


async def test_config_invalid_is_computed_on_read(client, committed) -> None:
    body = _create(client).json()
    row = await _row(committed, body["id"])
    row.auth_plugin = "vanished-plugin"
    committed.add(row)
    await committed.commit()
    listed = client.get(f"/api/v1/mcp-servers/{body['id']}", headers=H).json()
    assert listed["config_invalid"] is True
    assert "no gateway reports" in listed["config_problems"][0]


def _tool(committed: AsyncSession, observed: hashing.Observed, state, server_id) -> McpDefinition:
    return McpDefinition(
        server_id=server_id,
        kind=McpDefinitionKind.TOOL,
        name=observed.name,
        hash=observed.hash,
        definition_json=observed.canonical_json,
        state=state,
    )


def observe(name: str, description: str = "Echoes text") -> hashing.Observed:
    observed = hashing.observe(McpDefinitionKind.TOOL, {"name": name, "description": description})
    assert observed is not None
    return observed


async def test_definitions_are_listed_and_reviewed(client, committed) -> None:
    body = _create(client).json()
    server_id = uuid.UUID(body["id"])
    old = observe("echo", "old")
    pending = observe("echo", "new")
    superseded = observe("echo", "older")
    rejected = observe("gone")
    committed.add_all(
        [
            _tool(committed, old, McpDefinitionState.APPROVED, server_id),
            _tool(committed, pending, McpDefinitionState.PENDING, server_id),
            _tool(committed, superseded, McpDefinitionState.PENDING, server_id),
            _tool(committed, rejected, McpDefinitionState.PENDING, server_id),
        ]
    )
    await committed.commit()

    listed = client.get(
        f"/api/v1/mcp-servers/{body['id']}/definitions?state=pending", headers=H
    ).json()
    assert len(listed["items"]) == 3
    assert all(item["state"] == "pending" for item in listed["items"])
    # The list is newest first and carries the canonical JSON the hash covers.
    ids = [uuid.UUID(item["id"]) for item in listed["items"]]
    assert ids == sorted(ids, reverse=True)
    chosen = next(item for item in listed["items"] if item["hash"] == pending.hash.hex())
    assert json.loads(chosen["definition_json"])["description"] == "new"

    # Approving one hash supersedes the other pending hash of the same name only.
    response = client.post(
        f"/api/v1/mcp-servers/{body['id']}/definitions/{chosen['id']}/approve", headers=H
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "approved"
    # The test admin is break-glass (no user row), so the decision records a time only.
    assert response.json()["decided_at"] is not None
    committed.expire_all()
    rows = (
        await committed.scalars(select(McpDefinition).where(McpDefinition.server_id == server_id))
    ).all()
    states = sorted((row.name, row.state.value) for row in rows)
    assert states == [("echo", "approved"), ("echo", "approved"), ("gone", "pending")]

    # Rejecting the remaining one keeps it, and repeating either call is idempotent.
    remaining = next(item for item in listed["items"] if item["name"] == "gone")
    response = client.post(
        f"/api/v1/mcp-servers/{body['id']}/definitions/{remaining['id']}/reject", headers=H
    )
    assert response.json()["state"] == "rejected"
    again = client.post(
        f"/api/v1/mcp-servers/{body['id']}/definitions/{remaining['id']}/reject", headers=H
    )
    assert again.status_code == 200 and again.json()["state"] == "rejected"
    actions = {
        entry.action
        for entry in await committed.scalars(
            select(AuditLog).where(AuditLog.resource_id == str(remaining["id"]))
        )
    }
    assert actions == {"mcp_server.definition_reject"}
    approved_actions = {
        entry.action
        for entry in await committed.scalars(
            select(AuditLog).where(AuditLog.resource_id == chosen["id"])
        )
    }
    assert approved_actions == {"mcp_server.definition_approve"}

    # A definition of another server is a 404.
    other = _create(client).json()
    assert (
        client.post(
            f"/api/v1/mcp-servers/{other['id']}/definitions/{remaining['id']}/approve",
            headers=H,
        ).status_code
        == 404
    )


# --------------------------------------------------------------------------- discover


def fake_server(*, sse: bool = True, seen: list | None = None) -> httpx.MockTransport:
    """One MCP server: initialize (SSE or plain JSON), notification, two list pages."""
    tools = [
        {"name": "echo", "description": "Echoes text", "inputSchema": {"type": "object"}},
        {"name": "add", "description": "Adds", "inputSchema": {"type": "object"}},
    ]
    prompts = [{"name": "greet", "description": "Greets"}]

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if seen is not None:
            seen.append((request.headers.get("authorization"), payload))
        method = payload.get("method")
        if method == "initialize":
            body = {
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {"tools": {}, "prompts": {}},
                    "serverInfo": {"name": "mock"},
                },
            }
            headers = {"mcp-session-id": "session-1"}
            if not sse:
                return httpx.Response(200, json=body, headers=headers)
            data = f"event: message\ndata: {json.dumps(body)}\n\n"
            return httpx.Response(
                200,
                text=data,
                headers={**headers, "content-type": "text/event-stream"},
            )
        if method == "notifications/initialized":
            assert request.headers.get("mcp-session-id") == "session-1"
            return httpx.Response(202)
        cursor = (payload.get("params") or {}).get("cursor")
        if method == "tools/list":
            result = (
                {"tools": tools[1:], "nextCursor": "page-2"}
                if cursor is None
                else {"tools": tools[:1]}
            )
        elif method == "prompts/list":
            result = {"prompts": prompts}
        else:  # pragma: no cover - the client never sends another method
            raise AssertionError(method)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result})

    return httpx.MockTransport(handler)


@pytest.fixture
def fake(monkeypatch) -> list:
    seen: list = []
    monkeypatch.setattr(discover, "transport", fake_server(seen=seen))
    return seen


async def test_discover_without_a_server_records_nothing(client, committed, fake) -> None:
    before = len((await committed.scalars(select(McpDefinition.id))).all())
    response = client.post(
        "/api/v1/mcp-servers/discover",
        json={"url": "http://mcp.test/mcp", "token": "user-token"},
        headers=H,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["credential"] == "entered"
    assert body["recorded"] is False and body["server"] is None
    assert sorted(item["name"] for item in body["tools"]) == ["add", "echo"]
    assert [item["name"] for item in body["prompts"]] == ["greet"]
    assert {item["state"] for item in body["tools"]} == {"unrecorded"}
    assert body["protocol_version"] == "2025-11-25"
    assert body["truncated"] is False
    # The token reached the server, and was not stored.
    assert fake[0][0] == "Bearer user-token"
    # Nothing was recorded: no server was named, so there is nothing to record against.
    assert len((await committed.scalars(select(McpDefinition.id))).all()) == before


async def test_discover_records_against_a_server(client, committed, fake) -> None:
    created = _create(client)
    assert created.status_code == 201, created.text
    body = created.json()
    response = client.post(
        "/api/v1/mcp-servers/discover",
        json={"url": "http://mcp.test/mcp", "server_id": body["id"]},
        headers=H,
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["recorded"] is True and result["server"] == body["name"]
    # TOFU first sight: the control plane's own observation approves a new name.
    assert {item["state"] for item in result["tools"]} == {"approved"}
    committed.expire_all()
    rows = (
        await committed.scalars(
            select(McpDefinition).where(McpDefinition.server_id == uuid.UUID(body["id"]))
        )
    ).all()
    assert len(rows) == 3
    assert {row.state.value for row in rows} == {"approved"}
    # Reported to the console: the definitions are the canonical JSON.
    for item in result["tools"]:
        assert json.loads(item["definition_json"])["name"] == item["name"]
    actions = [entry.action for entry in await _audit(committed, body["id"])]
    assert "mcp_server.discover" in actions
    assert "user-token" not in json.dumps(
        [entry.details for entry in await _audit(committed, body["id"])]
    )


async def test_discover_uses_a_stored_secret_and_never_audits_it(
    client, committed, api_settings, monkeypatch
) -> None:
    seen: list = []
    monkeypatch.setattr(discover, "transport", fake_server(seen=seen))
    created = _create(client, auth={"plugin": "static_bearer", "secret": PLAINTEXT})
    assert created.status_code == 201, created.text
    body = created.json()
    response = client.post(
        "/api/v1/mcp-servers/discover",
        json={"url": "http://mcp.test/mcp", "server_id": body["id"]},
        headers=H,
    )
    assert response.status_code == 200, response.text
    assert response.json()["credential"] == "stored"
    assert seen[0][0] == f"Bearer {PLAINTEXT}"
    for entry in await _audit(committed, body["id"]):
        assert PLAINTEXT not in json.dumps(entry.details)


async def test_discover_refuses_a_wasm_auth_plugin(client, committed, gateway) -> None:
    await gateway()
    body = _create(
        client, auth={"plugin": "hmac_signature", "params": {"ttl_ms": 1000}}, force=True
    ).json()
    response = client.post(
        "/api/v1/mcp-servers/discover",
        json={"url": "http://mcp.test/mcp", "server_id": body["id"]},
        headers=H,
    )
    assert response.status_code == 422
    assert "traffic only" in response.json()["error"]["message"]


def test_discover_reports_an_unreachable_server_as_502(client, monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope", request=request)

    monkeypatch.setattr(discover, "transport", httpx.MockTransport(handler))
    response = client.post(
        "/api/v1/mcp-servers/discover", json={"url": "http://mcp.test/mcp"}, headers=H
    )
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_unavailable"


def test_discover_reports_a_jsonrpc_error(client, monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "error": {"code": -32601, "message": "No such method"},
            },
        )

    monkeypatch.setattr(discover, "transport", httpx.MockTransport(handler))
    response = client.post(
        "/api/v1/mcp-servers/discover", json={"url": "http://mcp.test/mcp"}, headers=H
    )
    assert response.status_code == 502
    assert "-32601" in response.json()["error"]["message"]


# --------------------------------------------------------------------------- protocol version


def protocol_version_server(version: str, seen: list | None = None) -> httpx.MockTransport:
    """A server that refuses every request after `initialize` without the negotiated version."""
    tools = [{"name": "echo", "description": "Echoes text", "inputSchema": {"type": "object"}}]

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        method = payload.get("method")
        if method == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": version,
                        "capabilities": {"tools": {}, "prompts": {}},
                        "serverInfo": {"name": "strict"},
                    },
                },
            )
        header = request.headers.get("mcp-protocol-version")
        if seen is not None:
            seen.append((method, header))
        if header != version:
            return httpx.Response(400, text="missing or wrong MCP-Protocol-Version")
        if method == "notifications/initialized":
            return httpx.Response(202)
        result = {"tools": tools} if method == "tools/list" else {"prompts": []}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result})

    return httpx.MockTransport(handler)


@pytest.mark.parametrize("version", ["2025-11-25", "2025-06-18"])
async def test_discover_sends_the_negotiated_protocol_version(client, monkeypatch, version) -> None:
    """M6: after `initialize` every request carries the negotiated MCP-Protocol-Version."""
    seen: list = []
    monkeypatch.setattr(discover, "transport", protocol_version_server(version, seen))
    response = client.post(
        "/api/v1/mcp-servers/discover", json={"url": "http://mcp.test/mcp"}, headers=H
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["protocol_version"] == version
    assert [item["name"] for item in body["tools"]] == ["echo"]
    assert [method for method, _ in seen] == [
        "notifications/initialized",
        "tools/list",
        "prompts/list",
    ]
    assert {header for _, header in seen} == {version}


def test_discover_refuses_an_unsupported_protocol_version(client, monkeypatch) -> None:
    monkeypatch.setattr(discover, "transport", protocol_version_server("1999-01-01"))
    response = client.post(
        "/api/v1/mcp-servers/discover", json={"url": "http://mcp.test/mcp"}, headers=H
    )
    assert response.status_code == 502
    assert (
        response.json()["error"]["message"]
        == "the server negotiated an unsupported protocol version"
    )


# --------------------------------------------------------------------------- error text (M4)


def reflecting_error_server(seen: list | None = None) -> httpx.MockTransport:
    """A hostile MCP server: it reflects the Authorization header it received."""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        authorization = request.headers.get("authorization", "")
        if seen is not None:
            seen.append(authorization)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload.get("id"),
                "error": {
                    "code": -32000,
                    "message": f"invalid credential {authorization}",
                },
            },
        )

    return httpx.MockTransport(handler)


def test_discover_never_returns_a_reflected_entered_token(client, monkeypatch) -> None:
    sentinel = "REVIEW_SENTINEL_TOKEN"
    monkeypatch.setattr(discover, "transport", reflecting_error_server())
    response = client.post(
        "/api/v1/mcp-servers/discover",
        json={"url": "http://mcp.test/mcp", "token": sentinel},
        headers=H,
    )
    assert response.status_code == 502
    assert sentinel not in response.text
    assert response.json()["error"]["message"] == "initialize failed: JSON-RPC error -32000"


def test_discover_never_returns_a_reflected_stored_token(client, monkeypatch) -> None:
    """The stored credential is write-only in the API; a reflected 502 must not undo that."""
    created = _create(client, auth={"plugin": "static_bearer", "secret": PLAINTEXT})
    assert created.status_code == 201, created.text
    body = created.json()
    monkeypatch.setattr(discover, "transport", reflecting_error_server())
    response = client.post(
        "/api/v1/mcp-servers/discover",
        json={"url": body["url"], "server_id": body["id"]},
        headers=H,
    )
    assert response.status_code == 502
    assert PLAINTEXT not in response.text
    assert response.json()["error"]["message"] == "initialize failed: JSON-RPC error -32000"


@pytest.mark.parametrize("code", ["-32000", 2**40, None])
def test_discover_only_reports_a_validated_numeric_error_code(client, monkeypatch, code) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        error = {"message": "boom"}
        if code is not None:
            error["code"] = code
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "error": error})

    monkeypatch.setattr(discover, "transport", httpx.MockTransport(handler))
    response = client.post(
        "/api/v1/mcp-servers/discover", json={"url": "http://mcp.test/mcp"}, headers=H
    )
    assert response.status_code == 502
    body = response.json()
    message = body["error"]["message"]
    assert message in {
        "initialize failed",
        "initialize failed: JSON-RPC error -32000",
    }
    assert "boom" not in message
    if code == 2**40:
        assert str(code) not in message


# --------------------------------------------------------------------------- recording (M5)


async def test_discover_records_nothing_for_an_unsaved_url(client, committed, fake) -> None:
    """A preview of a URL that is not the saved one must not decide a review for the server."""
    created = _create(client)
    assert created.status_code == 201, created.text
    body = created.json()
    response = client.post(
        "/api/v1/mcp-servers/discover",
        json={"url": "http://other.test/mcp", "server_id": body["id"]},
        headers=H,
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["recorded"] is False
    assert {item["state"] for item in result["tools"]} == {"unrecorded"}
    assert [item["name"] for item in result["tools"]] == ["add", "echo"]
    committed.expire_all()
    stored = (
        await committed.scalars(
            select(McpDefinition).where(McpDefinition.server_id == uuid.UUID(body["id"]))
        )
    ).all()
    assert stored == []
    assert "mcp_server.discover" not in [
        entry.action for entry in await _audit(committed, body["id"])
    ]


@pytest.mark.parametrize(
    "change",
    [{"url": "http://changed.test/mcp"}, {"review_mode": McpReviewMode.MANUAL}],
    ids=["url", "review-mode"],
)
async def test_discover_records_nothing_when_the_config_changed_in_flight(
    client, api_engine, api_settings, monkeypatch, change
) -> None:
    """M5: an edit while the network call runs makes the answer describe an old upstream.

    The URL case is the review's repro; the review-mode case changes nothing the URL check
    can see, so only the captured config revision can catch it.
    """
    from lumewarp_control_plane import outbox
    from lumewarp_control_plane.api import targets
    from lumewarp_control_plane.api.schemas import McpDiscoverIn
    from lumewarp_control_plane.authz import RequestAuthz
    from lumewarp_control_plane.metrics import DISCARD
    from lumewarp_control_plane.security import Principal, PrincipalKind

    created = _create(client)
    assert created.status_code == 201, created.text
    body = created.json()
    server_id = uuid.UUID(body["id"])
    base = fake_server()

    class Handler(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            # Another administrator saves the server while this request is in flight.
            async with (
                AsyncSession(api_engine, expire_on_commit=False) as other,
                other.begin(),
            ):
                row = await other.get(McpServer, server_id, with_for_update=True)
                for name, value in change.items():
                    setattr(row, name, value)
                row.config_revision = await outbox.enqueue_recompile(
                    other, api_settings.scope, "test.in_flight_edit"
                )
            return await base.handle_async_request(request)

    monkeypatch.setattr(discover, "transport", Handler())
    authz = RequestAuthz(Principal(kind=PrincipalKind.BREAK_GLASS), client.app.state.authorizer)
    async with AsyncSession(api_engine, expire_on_commit=False) as session:
        result = await targets.discover_mcp_server(
            McpDiscoverIn(url=body["url"], server_id=server_id),
            authz,
            session,
            api_settings,
            DISCARD,
        )
    assert result.recorded is False
    assert {item.state for item in result.tools} == {"unrecorded"}
    async with AsyncSession(api_engine) as check:
        stored = (
            await check.scalars(select(McpDefinition).where(McpDefinition.server_id == server_id))
        ).all()
        assert stored == []
        assert "mcp_server.discover" not in [
            entry.action for entry in await _audit(check, body["id"])
        ]


# --------------------------------------------------------------------------- pagination (m1)


async def test_pagination_walks_every_server_once(client) -> None:
    """The list is id-ordered and the cursor is an id: names that sort against creation
    order must not skip or repeat an item."""
    names = []
    for name in ("zeta", "alpha", "race"):
        response = _create(client, name=name)
        assert response.status_code == 201, response.text
        names.append(name)
    seen: list[str] = []
    cursor = None
    for _ in range(10):
        params: dict[str, object] = {"limit": 1}
        if cursor is not None:
            params["cursor"] = cursor
        page = client.get("/api/v1/mcp-servers", params=params, headers=H)
        assert page.status_code == 200, page.text
        body = page.json()
        seen.extend(item["name"] for item in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    ours = [name for name in seen if name in set(names)]
    assert ours == names
    assert len(seen) == len(set(seen))


async def test_discover_respects_manual_review(client, committed, fake) -> None:
    """M1: the control plane's own observation follows the saved review mode too."""
    created = _create(client, review_mode="manual")
    assert created.status_code == 201, created.text
    body = created.json()
    response = client.post(
        "/api/v1/mcp-servers/discover",
        json={"url": body["url"], "server_id": body["id"]},
        headers=H,
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["recorded"] is True
    # MANUAL withholds every name the control plane has not approved, first sight included.
    assert {item["state"] for item in result["tools"]} == {"pending"}
    assert {item["state"] for item in result["prompts"]} == {"pending"}


def unsafe_definition_server() -> httpx.MockTransport:
    """An upstream whose names/definitions cannot all be stored (N3)."""
    tools = [
        {"name": "bad\x00tool", "description": "nul", "inputSchema": {"type": "object"}},
        {
            "name": "huge",
            "description": "a" * (64 * 1024 + 1),
            "inputSchema": {"type": "object"},
        },
        {"name": "echo", "description": "fine", "inputSchema": {"type": "object"}},
    ]
    prompts = [{"name": "bad\x00prompt", "description": "nul"}]

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        method = payload.get("method")
        if method == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}, "prompts": {}},
                        "serverInfo": {"name": "unsafe"},
                    },
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        result = {"tools": tools} if method == "tools/list" else {"prompts": prompts}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result})

    return httpx.MockTransport(handler)


async def test_discover_marks_an_unstorable_definition_unrecorded(
    client, committed, monkeypatch
) -> None:
    """N3: a saved-server Discover meets the same DB-safe boundary as a gateway report.

    A NUL name or an over-bound definition is dropped by itself (state `unrecorded`), the
    rest is recorded, and the route answers 200 rather than failing on the first query.
    """
    created = _create(client)
    assert created.status_code == 201, created.text
    body = created.json()
    monkeypatch.setattr(discover, "transport", unsafe_definition_server())
    response = client.post(
        "/api/v1/mcp-servers/discover",
        json={"url": body["url"], "server_id": body["id"]},
        headers=H,
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["recorded"] is True
    assert {item["name"]: item["state"] for item in result["tools"]} == {
        "bad\x00tool": "unrecorded",
        "huge": "unrecorded",
        "echo": "approved",
    }
    assert {item["name"]: item["state"] for item in result["prompts"]} == {
        "bad\x00prompt": "unrecorded"
    }
    committed.expire_all()
    stored = (
        await committed.scalars(
            select(McpDefinition).where(McpDefinition.server_id == uuid.UUID(body["id"]))
        )
    ).all()
    assert [(row.kind, row.name) for row in stored] == [(McpDefinitionKind.TOOL, "echo")]
