"""The control plane's MCP server over the real stack: a member's PAT journey.

A member calls `POST /mcp` on the control plane with a personal access token, lists what
they can call, creates and rotates their own key, makes a real gateway request with the
revealed secret, reads usage and requests, and revokes the key. A team admin holds
another user's key, lists the read-only tenancy tools and cannot touch another member's
key. Every key write is audited `via=mcp`; a revealed secret is recorded for the run's
plaintext scan.
"""

import json
from typing import Self

import httpx
from stack import record_secret, wait_for

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


class McpClient:
    """The stateless Streamable HTTP endpoint, spoken as JSON-RPC over httpx.

    The control plane's `/mcp` answers every POST as one JSON body and keeps no session,
    so this is the whole protocol a client needs for initialize/list/call.
    """

    def __init__(self, stack, token: str) -> None:
        self.url = f"http://127.0.0.1:{stack.http_port}/mcp"
        self.token = token
        self.http = httpx.Client(timeout=20)
        self.sequence = 0

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        self.http.close()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": PROTOCOL,
        }

    def rpc(self, method, params=None, *, notification=False):
        self.sequence += 1
        body = {"jsonrpc": "2.0", "method": method}
        if not notification:
            body["id"] = self.sequence
        if params is not None:
            body["params"] = params
        return self.http.post(self.url, headers=self._headers(), json=body)

    def initialize(self) -> Self:
        response = self.rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "cp-mcp-e2e", "version": "1"},
            },
        )
        assert response.status_code == 200, response.text
        assert "result" in response.json(), response.text
        assert response.headers["content-type"].startswith("application/json")
        assert "mcp-session-id" not in response.headers
        initialized = self.rpc("notifications/initialized", notification=True)
        assert initialized.status_code == 202, initialized.text
        return self

    def tools(self) -> list[str]:
        names: list[str] = []
        cursor = None
        for _ in range(30):
            response = self.rpc(
                "tools/list", {"cursor": cursor} if cursor is not None else {}
            )
            assert response.status_code == 200, response.text
            result = response.json()["result"]
            names.extend(tool["name"] for tool in result["tools"])
            cursor = result.get("nextCursor")
            if cursor is None:
                return names
        raise AssertionError("MCP pagination did not terminate")

    def call(self, name: str, arguments=None) -> dict:
        response = self.rpc("tools/call", {"name": name, "arguments": arguments or {}})
        assert response.status_code == 200, response.text
        return response.json()["result"]

    def result(self, name: str, arguments=None) -> dict:
        result = self.call(name, arguments)
        assert result["isError"] is False, result
        return result["structuredContent"]

    def error(self, name: str, arguments=None) -> dict:
        result = self.call(name, arguments)
        assert result["isError"] is True, result
        return json.loads(result["content"][-1]["text"])


def wait_chat(gateway, secret: str, status: int, *, what: str, timeout: float = 20):
    wait_for(
        lambda: gateway.chat(secret).status_code == status,
        timeout,
        what,
        interval=0.05,
    )


def test_api_mcp(stack):
    # --- the stack's admin sets up a team, a callable model and two PATs ---------------
    team = stack.ok(
        "POST",
        "/teams",
        json={"name": "mcp-e2e", "org_id": stack.org_id, "model_access": "all"},
    )
    upstream = stack.ok(
        "POST",
        "/upstreams",
        json={
            "name": "mock",
            "kind": "openai_compatible",
            "base_url": f"http://127.0.0.1:{stack.mock_port}/v1",
        },
    )
    stack.create_model(
        {
            "name": "mock",
            "model_type": "llm",
            "upstream_id": upstream["id"],
            "upstream_model": "mock",
            "price_input_nano_usd_per_mtok": 1_000_000_000,
            "price_output_nano_usd_per_mtok": 2_000_000_000,
        }
    )
    member_pat = stack.create_pat("mcp-member@e2e.test", team["id"], "member")
    admin_pat = stack.create_pat("mcp-team-admin@e2e.test", team["id"], "admin")
    member = stack.ok("GET", "/users/me", member_pat)
    member_id = member["user_id"]
    admin_user_id = stack.ok("GET", "/users/me", admin_pat)["user_id"]

    # --- a member works with their own keys over MCP -----------------------------------
    with McpClient(stack, member_pat) as mcp:
        mcp.initialize()

        tools = mcp.tools()
        assert len(tools) == 15, tools
        assert set(tools) == USER_TOOLS, tools

        me = mcp.result("get_current_user")
        assert me["kind"] == "user"
        assert me["user_id"] == member_id
        member_team = next(
            item for item in me["teams"] if item["team_id"] == team["id"]
        )
        assert "mock" in member_team["models"]

        catalog = mcp.result("list_catalog")
        assert any(
            item["name"] == "mock" and item["kind"] == "model"
            for item in catalog["items"]
        ), catalog

        created = mcp.result(
            "create_my_key", {"team_id": team["id"], "name": "mcp-key"}
        )
        key = created["key"]
        key_id = key["id"]
        assert key["owner_kind"] == "user"
        assert key["user_id"] == member_id
        # The secret is masked by default: it must not be usable as returned.
        assert created["secret"].endswith("(masked)"), created["secret"]
        assert "masked" in created["secret"]

        rotated = mcp.result(
            "rotate_my_key",
            {"key_id": key_id, "confirm": True, "reveal_secret": True},
        )
        assert rotated["key"]["id"] == key_id
        secret = rotated["secret"]
        assert secret.startswith("lw-") and "masked" not in secret
        record_secret(secret, "MCP-revealed rotated key secret")

        # The control plane compiles the new key, then the gateway serves it.
        stack.wait_key_count(1)
        gateway = stack.start_gateway()
        wait_for(
            lambda: (
                (stack.gateway_status(gateway) or {}).get("acked_version")
                == stack.current_version()
            ),
            20,
            "gateway ACKed the MCP key config",
        )
        wait_chat(gateway, secret, 200, what="gateway serves the MCP-revealed key")
        assert gateway.chat(secret).json()["model"] == "mock"

        wait_for(
            lambda: (
                sum(row["requests"] for row in mcp.result("get_usage")["items"]) >= 1
            ),
            20,
            "MCP get_usage shows the gateway request",
        )
        wait_for(
            lambda: any(
                item["key_id"] == key_id
                for item in mcp.result("list_requests")["items"]
            ),
            20,
            "MCP list_requests shows the gateway request",
        )

        refused = mcp.error("revoke_my_key", {"key_id": key_id})
        assert refused["error"]["code"] == "confirmation_required", refused
        revoked = mcp.result("revoke_my_key", {"key_id": key_id, "confirm": True})
        assert revoked["lifecycle"] == "revoked"
        wait_chat(gateway, secret, 401, what="gateway rejects the revoked MCP key")

    # --- a team admin gets the tenancy reads, never another member's key --------------
    with McpClient(stack, admin_pat) as mcp:
        mcp.initialize()
        tools = mcp.tools()
        assert len(tools) == 19, tools
        assert set(tools) == TEAM_ADMIN_TOOLS, tools

        members = mcp.result("list_members", {"team_id": team["id"]})
        assert {item["user_id"] for item in members["items"]} >= {
            member_id,
            admin_user_id,
        }

        refused = mcp.error(
            "update_my_key", {"key_id": key_id, "name": "hijacked-by-team-admin"}
        )
        assert refused["error"]["code"] == "not_your_key", refused

    # --- every key write the member made over MCP is audited as MCP -------------------
    rows = stack.ok(
        "GET",
        "/audit",
        params={"via": "mcp", "resource_id": key_id},
    )["items"]
    by_action = {row["action"]: row for row in rows}
    for action in ("key.create", "key.rotate", "key.revoke"):
        assert action in by_action, (action, by_action)
        row = by_action[action]
        assert row["via"] == "mcp"
        assert row["resource_id"] == key_id
        assert row["actor_id"] == member_id

    # --- no token: 401 that points an MCP client at the protected-resource metadata ---
    response = stack.admin.post(
        f"http://127.0.0.1:{stack.http_port}/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 401, response.text
    challenge = response.headers["www-authenticate"]
    assert challenge.startswith("Bearer "), challenge
    assert "resource_metadata=" in challenge, challenge
    assert "/.well-known/oauth-protected-resource/mcp" in challenge, challenge
