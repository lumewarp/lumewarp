"""API → Postgres → config stream → real gateway → real MCP server acceptance.

Run with scripts/e2e-mcp.sh (or its --cp-only mode). Stack's pg_dump scan records
and checks every issued key plus the explicit upstream credentials below.
"""

import base64
import os
import shutil
import time
import uuid

import httpx
import pytest
from stack import ROOT, Proc, free_port, record_secret, wait_for

CLIENT_TOKEN = "mcp-client-bearer-" + uuid.uuid4().hex
STATIC_TOKEN = "mcp-sealed-bearer-" + uuid.uuid4().hex
HMAC_SECRET = "mcp-hmac-secret-" + uuid.uuid4().hex
ID_KEY = base64.b64encode(uuid.uuid4().bytes + uuid.uuid4().bytes).decode()


def start_mcp(stack, name, *args):
    port = free_port()
    binary = ROOT / "bench/mock-upstream/target/debug/mcp-a2a-mock"
    assert binary.exists(), "build mcp-a2a-mock before running MCP acceptance"
    proc = Proc(
        name,
        [str(binary), "mcp", "--listen", f"127.0.0.1:{port}", *args],
        stack.base_env,
        stack.dir,
    )
    stack.extra_mocks.append(proc)
    proc.start()
    url = f"http://127.0.0.1:{port}"
    wait_for(
        lambda: httpx.get(url + "/_mock/tools", timeout=1).status_code == 200, 10, name
    )
    return url


def create_key(stack, name, **policy):
    team = stack.ok(
        "POST", "/teams", json={"name": name, "org_id": stack.org_id, **policy}
    )
    owner = stack.ok(
        "POST", f"/teams/{team['id']}/service-accounts", json={"name": name}
    )
    issued = stack.ok(
        "POST",
        f"/teams/{team['id']}/keys",
        json={"name": name, "service_account_id": owner["id"]},
    )
    # Stack.api already records response secrets; explicit here for this acceptance's inventory.
    record_secret(issued["secret"], "MCP client API key")
    return issued


class McpClient:
    def __init__(self, gateway, key, route="/mcp"):
        self.gateway = gateway
        self.key = key
        self.route = route
        self.session = None
        self.sequence = 0

    def rpc(self, method, params=None, *, notification=False):
        self.sequence += 1
        body = {"jsonrpc": "2.0", "method": method}
        if not notification:
            body["id"] = self.sequence
        if params is not None:
            body["params"] = params
        headers = {
            "X-Lumewarp-Key": self.key,
            "Authorization": f"Bearer {CLIENT_TOKEN}",
            "Accept": "application/json, text/event-stream",
        }
        if self.session:
            headers.update(
                {"MCP-Session-Id": self.session, "MCP-Protocol-Version": "2025-11-25"}
            )
        return self.gateway.http.post(
            self.gateway.url(self.route), headers=headers, json=body
        )

    def initialize(self):
        def initialized():
            response = self.rpc(
                "initialize",
                {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "cp-mcp-acceptance", "version": "1"},
                },
            )
            if response.status_code != 200:
                return False
            assert "result" in response.json(), response.text
            self.session = response.headers["MCP-Session-Id"]
            response = self.rpc("notifications/initialized", notification=True)
            assert response.status_code == 202, response.text
            return True

        wait_for(initialized, 15, "MCP initialized after config ACK")

    def tools(self):
        tools = []
        cursor = None
        for _ in range(30):
            response = self.rpc(
                "tools/list", {"cursor": cursor} if cursor is not None else {}
            )
            assert response.status_code == 200, response.text
            result = response.json()["result"]
            tools.extend(item["name"] for item in result["tools"])
            cursor = result.get("nextCursor")
            if cursor is None:
                return tools
        raise AssertionError("MCP pagination did not terminate")

    def call(self, name, arguments=None):
        return self.rpc("tools/call", {"name": name, "arguments": arguments or {}})


def metric_count(text, name, **labels):
    return sum(
        float(line.split()[-1])
        for line in text.splitlines()
        if line.startswith(name + "{")
        and all(f'{key}="{value}"' in line for key, value in labels.items())
    )


@pytest.mark.skipif(
    os.environ.get("E2E_MCP") != "1",
    reason="run scripts/e2e-mcp.sh for MCP binaries/fixtures",
)
def test_cp_backed_mcp(stack):
    for secret, kind in [
        (CLIENT_TOKEN, "MCP passthrough client bearer"),
        (STATIC_TOKEN, "MCP sealed static bearer"),
        (HMAC_SECRET, "MCP HMAC plugin secret"),
        (ID_KEY, "MCP session id key"),
    ]:
        record_secret(secret, kind)
    plugins = stack.dir / "mcp-auth-plugins"
    plugins.mkdir()
    for source in [
        ROOT / "gateway/target/plugins/auth/hmac-signature.wasm",
        ROOT / "gateway/target/plugins-test/auth/fixture.wasm",
    ]:
        assert source.exists(), (
            "build WASM examples and fixtures with plugins/build.sh --tests"
        )
        shutil.copy2(source, plugins / source.name)
    gateway = stack.start_gateway(
        extra_env={
            "LUMEWARP_GATEWAY_ID_KEY": ID_KEY,
            "LUMEWARP_AUTH_PLUGIN_DIR": str(plugins),
            "LUMEWARP_PLUGIN_SECRET_MCP_ACCEPTANCE": HMAC_SECRET,
        }
    )

    def reported_plugins():
        rows = stack.ok("GET", "/plugins?kind=upstream-auth")["items"]
        selected = {
            row["name"]: row
            for row in rows
            if row["name"] in {"hmac_signature", "auth_fixture"}
        }
        return (
            selected
            if len(selected) == 2
            and all(
                not row["builtin"]
                and row["wasm_sha256"]
                and any(g["connected"] and g["digest"] for g in row["gateways"])
                for row in selected.values()
            )
            else None
        )

    wait_for(reported_plugins, 10, "WASM auth descriptors reported by Hello")

    mock = start_mcp(stack, "mcp-s1")
    s1 = stack.ok(
        "POST",
        "/mcp-servers",
        json={
            "name": "s1",
            "url": mock + "/mcp",
            "review_mode": "tofu",
            "auth": {"plugin": "none"},
        },
    )
    # Tool-level grants must name a definition already known to CP. Discover through
    # its admin API, not direct SQL or YAML; subsequent changed definitions come from traffic.
    discovered = stack.ok(
        "POST",
        "/mcp-servers/discover",
        json={"server_id": s1["id"], "url": mock + "/mcp"},
    )
    assert discovered["recorded"] is True
    assert any(item["name"] == "echo" for item in discovered["tools"])
    key_a = create_key(
        stack,
        "mcp-limited",
        mcp_access="list",
        mcp_servers=[{"server": "s1", "all_tools": False, "tools": ["echo"]}],
    )
    key_b = create_key(stack, "mcp-auth")
    limited = McpClient(gateway, key_a["secret"])
    limited.initialize()
    assert limited.tools() == ["s1__echo"]
    refused = limited.call("s1__add", {"a": 1, "b": 2})
    assert refused.status_code == 403, refused.text
    before = metric_count(
        gateway.metrics(),
        "lumewarp_mcp_tool_calls_total",
        server="s1",
        tool="echo",
        status="200",
    )
    response = limited.call("s1__echo", {"message": "MCP acceptance"})
    assert (
        response.status_code == 200 and response.json()["result"]["isError"] is False
    ), response.text
    assert (
        metric_count(
            gateway.metrics(),
            "lumewarp_mcp_tool_calls_total",
            server="s1",
            tool="echo",
            status="200",
        )
        > before
    )

    changed = httpx.post(
        mock + "/_mock/tool/description",
        json={"name": "echo", "description": "Changed MCP acceptance definition"},
    )
    assert changed.status_code == 200
    assert limited.tools() == []
    blocked = limited.call("s1__echo", {"message": "must not execute"})
    assert blocked.status_code == 403 or "error" in blocked.json(), blocked.text

    def pending_definition():
        rows = stack.ok(
            "GET",
            f"/mcp-servers/{s1['id']}/definitions",
            params={"state": "pending", "kind": "tool"},
        )["items"]
        return next((row for row in rows if row["name"] == "echo"), None)

    try:
        pending = wait_for(
            pending_definition, 10, "changed echo reported as pending via config stream"
        )
    except AssertionError:
        print(
            "Catalog diagnostic:",
            httpx.get(f"http://127.0.0.1:{stack.metrics_port}/metrics").text,
        )
        print("Versions:", stack.ok("GET", "/config/versions", params={"limit": 5}))
        print("Server:", stack.ok("GET", f"/mcp-servers/{s1['id']}"))
        raise
    assert "Changed MCP acceptance definition" in pending["definition_json"]
    started = time.monotonic()
    stack.ok("POST", f"/mcp-servers/{s1['id']}/definitions/{pending['id']}/approve")

    def restored():
        if limited.tools() != ["s1__echo"]:
            return False
        response = limited.call("s1__echo", {"message": "approved"})
        return response.status_code == 200 and "result" in response.json()

    wait_for(
        restored,
        2,
        "approved definition restored for discovery and calls",
        interval=0.02,
    )
    elapsed = time.monotonic() - started
    assert elapsed <= 2.0, f"approval-to-visible took {elapsed:.3f}s (bound 2s)"
    print(f"MCP API approval restored discovery/calls in {elapsed:.3f}s")

    def create_server(name, url, auth):
        return stack.ok(
            "POST",
            "/mcp-servers",
            json={
                "name": name,
                "url": url + "/mcp",
                "review_mode": "off",
                "auth": auth,
            },
        )

    passthrough_mock = start_mcp(stack, "mcp-passthrough", "--auth", "any-bearer")
    create_server("passthrough", passthrough_mock, {"plugin": "passthrough"})
    static_mock = start_mcp(
        stack, "mcp-static", "--auth", "bearer", "--bearer", STATIC_TOKEN
    )
    sealed = create_server(
        "sealed", static_mock, {"plugin": "static_bearer", "secret": STATIC_TOKEN}
    )
    assert sealed["secret_set"] is True and STATIC_TOKEN not in str(sealed)
    assert STATIC_TOKEN not in stack.api("GET", f"/mcp-servers/{sealed['id']}").text
    hmac_mock = start_mcp(
        stack, "mcp-hmac", "--auth", "hmac", "--hmac-secret", HMAC_SECRET
    )
    create_server(
        "signed",
        hmac_mock,
        {"plugin": "hmac_signature", "params": {"secret_name": "MCP_ACCEPTANCE"}},
    )
    for server, expected in [
        ("passthrough", f"Bearer {CLIENT_TOKEN}"),
        ("sealed", f"Bearer {STATIC_TOKEN}"),
        ("signed", None),
    ]:
        client = McpClient(gateway, key_b["secret"], "/mcp/servers/" + server)
        client.initialize()
        response = client.call("whoami")
        assert response.status_code == 200, response.text
        summary = response.json()["result"]["structuredContent"]
        assert summary["authorization"] == expected
        assert summary["saw_lumewarp_key"] is False
        if server == "signed":
            assert summary["headers"]["x-hmac-signature"].startswith("t=")

    timeout_server = create_server(
        "timeout",
        mock,
        {
            "plugin": "auth_fixture",
            "params": {"mode": "loop"},
            "timeout_ms": 100,
        },
    )
    assert timeout_server["auth"]["timeout_ms"] == 100
    timeout_client = McpClient(gateway, key_b["secret"], "/mcp/servers/timeout")
    wait_for(
        lambda: (
            timeout_client.rpc(
                "initialize",
                {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "timeout-acceptance", "version": "1"},
                },
            ).status_code
            == 504
        ),
        10,
        "API-configured plugin timeout gives 504",
    )
    assert (
        metric_count(
            gateway.metrics(),
            "lumewarp_plugin_calls_total",
            plugin="auth_fixture",
            result="timeout",
        )
        >= 1
    )

    # The usage API reads Postgres, not an in-process gateway counter. Drain the reporter
    # before reconciling, so the row and the subsequent dump scan include every request.
    gateway.drain_usage()
    rows = stack.ok(
        "GET",
        "/usage",
        params={
            "kind": "mcp",
            "key_id": key_a["key"]["id"],
            "group_by": "kind,target,method,tool",
        },
    )["items"]
    assert any(
        row["kind"] == "mcp"
        and row["target"] == "s1"
        and row["method"] == "tools/call"
        and row["tool"] == "echo"
        and row["requests"] >= 2
        for row in rows
    ), rows
    # Explicit scan here makes this acceptance assert it, in addition to the normal
    # Stack.teardown scan and core-run finalizer that cover failures and hard kills.
    assert stack._scan_secrets() is None
    print("MCP usage reached Postgres; plaintext-credential pg_dump scan passed")
