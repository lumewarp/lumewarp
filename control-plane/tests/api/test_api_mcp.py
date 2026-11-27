"""The user-facing MCP server (`/mcp`, api_mcp/): auth, tool listing by role, the `*_my_key`
wrappers, secret masking, the confirm guard and the REST coverage table."""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import select

from lumewarp_control_plane.api_mcp.catalog import (
    EXPOSED_OPERATIONS,
    NOT_EXPOSED,
    TOOLS,
    Kind,
    MinRole,
)
from lumewarp_control_plane.api_mcp.server import VIA_EXTENSION, _ViaMcp
from lumewarp_control_plane.app import create_app
from lumewarp_control_plane.models import (
    ApiKey,
    AuditLog,
    AuditVia,
    InstallationAdmin,
    InstallationRole,
    KeyLifecycle,
    PersonalAccessToken,
    TeamRole,
)
from lumewarp_control_plane.security import SecretKind, generate_secret, hash_secret
from tests.api.helpers import auth, user_with_token
from tests.factories import (
    make_key,
    make_member,
    make_org,
    make_org_admin,
    make_service_account,
    make_team,
    make_user,
)

PROTOCOL = "2025-11-25"
USER_TOOLS = {
    "get_current_user",
    "list_zones",
    "list_catalog",
    "list_teams",
    "get_team",
    "list_own_keys",
    "get_key",
    "create_my_key",
    "update_my_key",
    "revoke_my_key",
    "rotate_my_key",
    "get_usage",
    "get_usage_leaderboard",
    "list_requests",
    "get_status",
}
TEAM_ADMIN_TOOLS = USER_TOOLS | {
    "list_team_keys",
    "list_members",
    "list_service_accounts",
    "get_service_account",
}
ORG_ADMIN_TOOLS = TEAM_ADMIN_TOOLS | {"list_orgs", "get_org", "get_org_usage"}
INSTALLATION_ADMIN_TOOLS = ORG_ADMIN_TOOLS | {
    "list_org_admins",
    "list_users",
    "get_user",
    "list_incidents",
    "get_incident",
}
MY_KEY_WRITES = {"create_my_key", "update_my_key", "revoke_my_key", "rotate_my_key"}


async def _pat(session, user) -> str:
    new = generate_secret(SecretKind.PAT)
    session.add(
        PersonalAccessToken(
            user_id=user.id,
            name="mcp test token",
            token_hash=new.hash,
            display_prefix=new.display_prefix,
        )
    )
    await session.flush()
    return new.secret


def _headers(token: str | None = None, **extra: str) -> dict[str, str]:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": PROTOCOL,
        **extra,
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def rpc(client, token, method, params=None, *, url="/mcp", headers=None):
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    return client.post(url, json=body, headers={**_headers(token), **(headers or {})})


def list_tools(client, token, **kw) -> dict[str, dict]:
    response = rpc(client, token, "tools/list", **kw)
    assert response.status_code == 200, response.text
    return {tool["name"]: tool for tool in response.json()["result"]["tools"]}


def call(client, token, name, arguments=None) -> dict:
    response = rpc(client, token, "tools/call", {"name": name, "arguments": arguments or {}})
    assert response.status_code == 200, response.text
    return response.json()["result"]


def error_of(result: dict) -> dict:
    assert result["isError"] is True, result
    return json.loads(result["content"][-1]["text"])


# --- coverage ---------------------------------------------------------------------------


def test_every_rest_operation_is_classified(client) -> None:
    spec = client.app.openapi()
    operation_ids = {
        op["operationId"]
        for path, item in spec["paths"].items()
        if path.startswith("/api/")
        for op in item.values()
    }
    assert not EXPOSED_OPERATIONS & set(NOT_EXPOSED)
    assert EXPOSED_OPERATIONS | set(NOT_EXPOSED) == operation_ids
    assert (len(TOOLS), len(EXPOSED_OPERATIONS), len(NOT_EXPOSED)) == (27, 27, 91)
    assert len(operation_ids) == 118
    # The only writes are the caller's own keys.
    assert {tool.name for tool in TOOLS if tool.kind is Kind.WRITE} == MY_KEY_WRITES
    assert all(tool.min_role is MinRole.USER for tool in TOOLS if tool.kind is Kind.WRITE)
    assert "/mcp" not in spec["paths"]


# --- transport and auth -----------------------------------------------------------------


async def test_official_client_initializes_lists_and_calls(client, committed) -> None:
    team = await make_team(committed)
    user, token = await user_with_token(committed)
    await make_member(committed, team, user)
    await committed.commit()

    async def session_flow():
        http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=client.app),
            headers={"Authorization": f"Bearer {token}"},
        )
        async with (
            http,
            streamable_http_client("http://testserver/mcp", http_client=http) as streams,
            ClientSession(streams[0], streams[1]) as session,
        ):
            await session.initialize()
            tools = await session.list_tools()
            result = await session.call_tool("list_teams", {})
            return tools, result

    tools, result = client.portal.call(session_flow)
    assert {tool.name for tool in tools.tools} == USER_TOOLS
    assert result.isError is False
    assert [item["id"] for item in result.structuredContent["items"]] == [str(team.id)]


def test_initialize_over_raw_json_rpc_is_stateless(client, committed_user_token) -> None:
    response = rpc(
        client,
        committed_user_token,
        "initialize",
        {
            "protocolVersion": PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/json")
    assert "mcp-session-id" not in response.headers
    assert response.json()["result"]["serverInfo"]["name"] == "lumewarp"


def test_missing_token_gets_401_with_resource_metadata(client) -> None:
    response = rpc(client, None, "tools/list")
    assert response.status_code == 401
    challenge = response.headers["www-authenticate"]
    assert challenge.startswith("Bearer ")
    assert 'resource_metadata="http://testserver/.well-known/oauth-protected-resource/mcp"' in (
        challenge
    )
    metadata = client.get("/.well-known/oauth-protected-resource/mcp")
    assert metadata.status_code == 200
    assert metadata.json()["resource"] == "http://testserver/mcp"


def test_cookie_gateway_key_and_bad_tokens_are_refused(client) -> None:
    gateway_key = "lw-" + "a" * 43

    cookie_only = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={**_headers(), **auth("session:test-admin-session")},
    )
    assert cookie_only.status_code == 401
    for token in (gateway_key, "lwp-" + "b" * 43, "garbage"):
        response = rpc(client, token, "tools/list")
        assert response.status_code == 401, token
        assert 'error="invalid_token"' in response.headers["www-authenticate"]


def test_foreign_origin_is_refused(client, committed_user_token) -> None:
    response = rpc(
        client, committed_user_token, "tools/list", headers={"Origin": "https://evil.example"}
    )
    assert response.status_code == 403


def test_disabled_endpoint_is_404(api_settings) -> None:
    settings = api_settings.model_copy(update={"api_mcp_enabled": False})
    with TestClient(create_app(settings)) as disabled:
        response = disabled.post("/mcp", json={}, headers=_headers("lwp-x"))
        assert response.status_code == 404
        assert disabled.get("/.well-known/oauth-protected-resource/mcp").status_code == 404


@pytest.fixture
async def committed_user_token(committed) -> str:
    _, token = await user_with_token(committed)
    await committed.commit()
    return token


# --- listing ----------------------------------------------------------------------------


async def test_tools_listed_by_role(client, committed) -> None:
    org = await make_org(committed)
    team = await make_team(committed, org=org)
    member, member_token = await user_with_token(committed)
    await make_member(committed, team, member)
    team_admin, team_admin_token = await user_with_token(committed)
    await make_member(committed, team, team_admin, role=TeamRole.ADMIN)
    org_admin, org_admin_token = await user_with_token(committed)
    await make_org_admin(committed, org, org_admin)
    installation_admin = await make_user(committed)
    committed.add(InstallationAdmin(user_id=installation_admin.id, role=InstallationRole.ADMIN))
    installation_token = await _pat(committed, installation_admin)
    await committed.commit()

    assert set(list_tools(client, member_token)) == USER_TOOLS
    assert set(list_tools(client, team_admin_token)) == TEAM_ADMIN_TOOLS
    assert set(list_tools(client, org_admin_token)) == ORG_ADMIN_TOOLS
    everything = list_tools(client, installation_token)
    assert set(everything) == INSTALLATION_ADMIN_TOOLS
    assert (len(USER_TOOLS), len(TEAM_ADMIN_TOOLS), len(ORG_ADMIN_TOOLS), len(everything)) == (
        15,
        19,
        22,
        27,
    )

    for name, tool in everything.items():
        annotations = tool["annotations"]
        if name in MY_KEY_WRITES:
            assert annotations["readOnlyHint"] is False
            assert annotations["destructiveHint"] is (name in {"revoke_my_key", "rotate_my_key"})
        else:
            assert annotations["readOnlyHint"] is True
        assert tool["inputSchema"]["type"] == "object"
        assert "$ref" not in json.dumps(tool["inputSchema"])
    assert "service_account_id" not in everything["create_my_key"]["inputSchema"]["properties"]
    assert "confirm" in everything["revoke_my_key"]["inputSchema"]["properties"]

    # A hidden admin tool called anyway is still REST's decision: 403, not data.
    result = call(client, member_token, "list_users")
    assert error_of(result)["status"] == 403


async def test_toolsets_narrow_the_list(client, committed_user_token) -> None:
    token = committed_user_token
    assert set(list_tools(client, token, url="/mcp?toolsets=usage")) == {
        "get_usage",
        "get_usage_leaderboard",
        "list_requests",
    }
    by_header = list_tools(client, token, headers={"X-Lumewarp-MCP-Toolsets": "me,incidents"})
    assert set(by_header) == {
        "get_current_user",
        "list_zones",
        "list_catalog",
        "list_teams",
        "get_team",
        "get_status",
    }
    # A tool outside the selected toolsets is not callable either.
    response = rpc(
        client, token, "tools/call", {"name": "get_current_user"}, url="/mcp?toolsets=usage"
    )
    assert error_of(response.json()["result"])["error"]["code"] == "unknown_tool"
    assert rpc(client, token, "tools/list", url="/mcp?toolsets=admin").status_code == 400


# --- own keys -----------------------------------------------------------------------------


async def test_create_my_key_masks_the_secret_unless_revealed(client, committed) -> None:
    team = await make_team(committed)
    user, token = await user_with_token(committed)
    await make_member(committed, team, user)
    sa = await make_service_account(committed, team)
    await committed.commit()

    result = call(client, token, "create_my_key", {"team_id": str(team.id), "name": "masked"})
    assert result["isError"] is False, result
    created = result["structuredContent"]
    assert created["key"]["owner_kind"] == "user"
    assert created["key"]["user_id"] == str(user.id)
    assert created["secret"].endswith("(masked)")
    assert "masked" in result["content"][-1]["text"]
    texts = " ".join(item["text"] for item in result["content"])
    assert created["key"]["display_prefix"] in texts

    stringly = call(
        client,
        token,
        "create_my_key",
        {"team_id": str(team.id), "name": "stringly", "reveal_secret": "true"},
    )
    assert stringly["structuredContent"]["secret"].endswith("(masked)")

    response = rpc(
        client,
        token,
        "tools/call",
        {
            "name": "create_my_key",
            "arguments": {"team_id": str(team.id), "name": "revealed", "reveal_secret": True},
        },
    )
    assert response.headers["cache-control"] == "no-store"
    revealed = response.json()["result"]
    secret = revealed["structuredContent"]["secret"]
    assert secret.startswith("lw-") and "masked" not in secret
    assert revealed["content"][0]["text"].startswith("WARNING")
    stored = await committed.scalar(select(ApiKey).where(ApiKey.key_hash == hash_secret(secret)))
    assert stored is not None and stored.user_id == user.id

    # A service-account owner cannot be smuggled in.
    smuggled = call(
        client,
        token,
        "create_my_key",
        {"team_id": str(team.id), "name": "sa", "service_account_id": str(sa.id)},
    )
    assert error_of(smuggled)["error"]["code"] == "invalid_arguments"


async def test_revoke_and_rotate_need_confirm(client, committed) -> None:
    team = await make_team(committed)
    user, token = await user_with_token(committed)
    await make_member(committed, team, user)
    key = await make_key(committed, team, user)
    await committed.commit()

    for name in ("revoke_my_key", "rotate_my_key"):
        refused = call(client, token, name, {"key_id": str(key.id)})
        assert error_of(refused)["error"]["code"] == "confirmation_required"
        refused = call(client, token, name, {"key_id": str(key.id), "confirm": "yes"})
        assert error_of(refused)["error"]["code"] == "confirmation_required"
    await committed.refresh(key)
    assert key.lifecycle is KeyLifecycle.ACTIVE

    rotated = call(client, token, "rotate_my_key", {"key_id": str(key.id), "confirm": True})
    assert rotated["isError"] is False, rotated
    assert rotated["structuredContent"]["secret"].endswith("(masked)")
    revoked = call(client, token, "revoke_my_key", {"key_id": str(key.id), "confirm": True})
    assert revoked["isError"] is False, revoked
    assert revoked["structuredContent"]["lifecycle"] == "revoked"
    # The in-process REST call is attributed to MCP in the audit log (T2's `via`).
    vias = (
        await committed.scalars(select(AuditLog.via).where(AuditLog.resource_id == str(key.id)))
    ).all()
    assert vias and set(vias) == {AuditVia.MCP}


async def test_my_key_tools_refuse_keys_the_caller_does_not_own(client, committed) -> None:
    team = await make_team(committed)
    admin, admin_token = await user_with_token(committed)
    await make_member(committed, team, admin, role=TeamRole.ADMIN)
    other = await make_user(committed)
    await make_member(committed, team, other)
    sa = await make_service_account(committed, team)
    others_key = await make_key(committed, team, other)
    sa_key = await make_key(committed, team, sa)
    own_key = await make_key(committed, team, admin)
    await committed.commit()

    for key in (others_key, sa_key):
        for name, extra in (
            ("update_my_key", {"name": "taken"}),
            ("revoke_my_key", {"confirm": True}),
            ("rotate_my_key", {"confirm": True}),
        ):
            result = call(client, admin_token, name, {"key_id": str(key.id), **extra})
            assert error_of(result)["error"]["code"] == "not_your_key", (name, key.id)
    # REST itself would have let the team admin rename it: the refusal is the MCP wrapper's.
    response = client.patch(
        f"/api/v1/keys/{others_key.id}", json={"name": "rest"}, headers=auth(admin_token)
    )
    assert response.status_code == 200, response.text

    updated = call(client, admin_token, "update_my_key", {"key_id": str(own_key.id), "name": "n"})
    assert updated["isError"] is False, updated
    assert updated["structuredContent"]["name"] == "n"
    missing = call(client, admin_token, "update_my_key", {"key_id": str(uuid.uuid4())})
    assert error_of(missing)["status"] == 404


# --- dispatch and metrics -------------------------------------------------------------------


async def test_tool_calls_carry_the_mcp_scope_extension() -> None:
    seen: list[dict] = []

    async def app(scope, receive, send):
        seen.append(scope)

    await _ViaMcp(app)({"type": "http", "extensions": {"other": 1}}, None, None)
    assert seen[0]["extensions"] == {"other": 1, VIA_EXTENSION: "mcp"}


def test_metrics_count_requests_and_tool_calls(client, committed_user_token) -> None:
    metrics = client.app.state.metrics
    before = metrics.value(
        "lumewarp_cp_api_mcp_tool_calls_total",
        {"toolset": "me", "kind": "read", "outcome": "ok"},
    )
    call(client, committed_user_token, "get_current_user")
    assert (
        metrics.value(
            "lumewarp_cp_api_mcp_tool_calls_total",
            {"toolset": "me", "kind": "read", "outcome": "ok"},
        )
        == before + 1
    )
    assert metrics.value(
        "lumewarp_cp_api_mcp_requests_total", {"method": "tools/call", "outcome": "ok"}
    )
    rpc(client, None, "tools/list")
    assert metrics.value(
        "lumewarp_cp_api_mcp_requests_total", {"method": "other", "outcome": "unauthorized"}
    )


def test_only_post_is_served(client, committed_user_token) -> None:
    """No standalone SSE stream: a GET answers 405 at once instead of holding a stream open."""
    headers = _headers(committed_user_token, Accept="text/event-stream")
    response = client.get("/mcp", headers=headers)
    assert response.status_code == 405
    assert response.headers["allow"] == "POST"
    assert response.headers["cache-control"] == "no-store"
    # Without a token the challenge still comes first.
    assert client.get("/mcp").status_code == 401


@pytest.mark.parametrize("value", [".", ".."])
def test_dot_segment_path_values_are_refused(client, committed_user_token, value) -> None:
    """`..` would be normalised into another path (`/orgs/../usage` is getUsage)."""
    result = call(client, committed_user_token, "get_org_usage", {"org_id": value})
    assert error_of(result)["error"]["code"] == "invalid_arguments"
