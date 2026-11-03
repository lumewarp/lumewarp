"""The mock MCP upstream is the reference the gateway and control plane are tested against.

Everything the mock exposes is checked here against the protocol text: the modern
2026-07-28 JSON schema (pinned under fixtures/), the official Python SDK for both eras,
the header mirroring rules, MRTR, cancellation and every injected fault.
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import http.client
import json
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest
from jsonschema import Draft202012Validator
from mcp import Client
from mcp import types as mcp_types
from mcp_mock import ErrorSpec, Fault, MCPMock, Server, Tool

pytestmark = pytest.mark.stack_free

MODERN = "2026-07-28"
LEGACY = "2025-11-25"
SCHEMA_PATH = Path(__file__).parent / "fixtures/mcp-2026-07-28-schema.json"
SCHEMA = json.loads(SCHEMA_PATH.read_text())
RESPONSE_VALIDATOR = Draft202012Validator({**SCHEMA, "$ref": "#/$defs/JSONRPCResponse"})
MESSAGE_VALIDATOR = Draft202012Validator({**SCHEMA, "$ref": "#/$defs/JSONRPCMessage"})


@pytest.fixture(scope="module")
def mock() -> MCPMock:
    with MCPMock.start() as instance:
        yield instance


@pytest.fixture(autouse=True)
def clean_records(mock: MCPMock):
    mock.clear_records()
    mock.set_recording(True)
    yield
    mock.set_recording(True)


def meta(
    *, capabilities: dict[str, Any] | None = None, progress: str | None = None
) -> dict:
    value: dict[str, Any] = {
        "io.modelcontextprotocol/protocolVersion": MODERN,
        "io.modelcontextprotocol/clientInfo": {"name": "tests", "version": "1"},
        "io.modelcontextprotocol/clientCapabilities": capabilities or {},
    }
    if progress is not None:
        value["progressToken"] = progress
    return value


def modern_body(
    method: str,
    params: dict[str, Any] | None = None,
    *,
    request_id: int | str = 1,
    capabilities: dict[str, Any] | None = None,
    progress: str | None = None,
) -> dict:
    payload = dict(params or {})
    payload["_meta"] = meta(capabilities=capabilities, progress=progress)
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": payload}


def modern_headers(
    method: str,
    *,
    name: str | None = None,
    version: str = MODERN,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    headers = {"MCP-Protocol-Version": version, "Mcp-Method": method}
    if name is not None:
        headers["Mcp-Name"] = name
    if extra:
        headers.update(extra)
    return headers


def post_modern(
    mock: MCPMock,
    server: str,
    body: dict,
    headers: dict[str, str],
    *,
    stream: bool = False,
    timeout: float = 30,
):
    merged = {"content-type": "application/json", **headers}
    url = mock.url(server)
    if stream:
        return mock.client.stream(
            "POST", url, json=body, headers=merged, timeout=timeout
        )
    return mock.client.post(url, json=body, headers=merged, timeout=timeout)


def modern_json(response: httpx.Response, expected_status: int = 200) -> dict:
    assert response.status_code == expected_status, response.text
    body = response.json()
    RESPONSE_VALIDATOR.validate(body)
    return body


def sse_messages(response: httpx.Response) -> list[dict]:
    messages = []
    for line in response.iter_lines():
        if line.startswith("data:"):
            message = json.loads(line[len("data:") :].strip())
            MESSAGE_VALIDATOR.validate(message)
            messages.append(message)
    return messages


def base64_header(value: str) -> str:
    return "=?base64?" + base64.b64encode(value.encode()).decode() + "?="


def raw_post(
    mock: MCPMock, server: str, body: dict, headers: dict[str, str]
) -> tuple[int, dict[str, str], bytes]:
    """POST without httpx's automatic gzip decoding."""
    connection = http.client.HTTPConnection("127.0.0.1", mock.port, timeout=10)
    connection.request(
        "POST",
        mock.path(server),
        body=json.dumps(body).encode(),
        headers={"content-type": "application/json", **headers},
    )
    response = connection.getresponse()
    payload = response.read()
    headers_out = {name.lower(): value for name, value in response.getheaders()}
    connection.close()
    return response.status, headers_out, payload


def initialize(mock: MCPMock, server: str) -> str:
    response = mock.client.post(
        mock.url(server),
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": LEGACY,
                "capabilities": {},
                "clientInfo": {"name": "tests", "version": "1"},
            },
        },
    )
    assert response.status_code == 200, response.text
    session = response.headers.get("mcp-session-id")
    assert session, response.headers
    return session


def legacy_headers(session: str | None = None) -> dict[str, str]:
    headers = {"MCP-Protocol-Version": LEGACY}
    if session:
        headers["Mcp-Session-Id"] = session
    return headers


# Admin and catalog -------------------------------------------------------------


def test_admin_lifecycle_and_rug_pull(mock: MCPMock):
    name = "admin"
    created = mock.create_server(Server(name, tools=(Tool("echo"),)))
    assert created.name == name
    assert created.era == "modern"
    assert created.tool("echo") is not None
    assert [server.name for server in mock.list_servers()] == [name]
    assert mock.get_server(name).cache_scope == "public"

    listed = modern_json(
        post_modern(
            mock,
            name,
            modern_body("tools/list", request_id=1),
            modern_headers("tools/list"),
        )
    )
    assert listed["result"]["tools"][0]["name"] == "echo"
    assert "description" not in listed["result"]["tools"][0]

    changed = Tool(
        "echo",
        definition={
            "name": "echo",
            "description": "changed definition",
            "inputSchema": {"type": "object", "properties": {}},
        },
    )
    mock.upsert_tool(name, changed)
    listed = modern_json(
        post_modern(
            mock,
            name,
            modern_body("tools/list", request_id=2),
            modern_headers("tools/list"),
        )
    )
    assert listed["result"]["tools"][0]["description"] == "changed definition"

    mock.delete_tool(name, "echo")
    listed = modern_json(
        post_modern(
            mock,
            name,
            modern_body("tools/list", request_id=3),
            modern_headers("tools/list"),
        )
    )
    assert listed["result"]["tools"] == []

    mock.delete_server(name)
    assert all(server.name != name for server in mock.list_servers())
    with pytest.raises(AssertionError):
        mock.get_server(name)


def test_modern_discover_list_and_tool_results(mock: MCPMock):
    name = "modern_tools"
    mock.create_server(
        Server(
            name,
            ttl_ms=1234,
            cache_scope="private",
            tools=(
                Tool("echo"),
                Tool(
                    "fixed",
                    behaviour="fixed",
                    result={"content": [{"type": "text", "text": "the fixed result"}]},
                ),
                Tool(
                    "tool_error",
                    behaviour="tool_error",
                    error=ErrorSpec(message="bad tool"),
                ),
                Tool(
                    "protocol_error",
                    behaviour="protocol_error",
                    error=ErrorSpec(code=-32000, message="bad protocol"),
                ),
            ),
        )
    )

    discover = modern_json(
        post_modern(
            mock,
            name,
            modern_body("server/discover", request_id=1),
            modern_headers("server/discover"),
        )
    )["result"]
    assert discover["resultType"] == "complete"
    assert discover["supportedVersions"] == [MODERN]
    assert discover["capabilities"] == {"tools": {}}
    assert discover["ttlMs"] == 1234
    assert discover["cacheScope"] == "private"
    assert discover["_meta"]["io.modelcontextprotocol/serverInfo"]["name"].startswith(
        "mock-mcp/"
    )

    listed = modern_json(
        post_modern(
            mock,
            name,
            modern_body("tools/list", request_id=2),
            modern_headers("tools/list"),
        )
    )["result"]
    assert [tool["name"] for tool in listed["tools"]] == [
        "echo",
        "fixed",
        "tool_error",
        "protocol_error",
    ]
    assert listed["ttlMs"] == 1234
    assert listed["cacheScope"] == "private"

    echo = modern_json(
        post_modern(
            mock,
            name,
            modern_body(
                "tools/call",
                {"name": "echo", "arguments": {"message": "hi", "n": 3}},
                request_id=3,
            ),
            modern_headers("tools/call", name="echo"),
        )
    )["result"]
    assert echo["resultType"] == "complete"
    assert echo["content"][0]["text"] == '{"message":"hi","n":3}'
    assert echo["structuredContent"] == {"message": "hi", "n": 3}
    assert "ttlMs" not in echo and "cacheScope" not in echo

    fixed = modern_json(
        post_modern(
            mock,
            name,
            modern_body("tools/call", {"name": "fixed", "arguments": {}}, request_id=4),
            modern_headers("tools/call", name="fixed"),
        )
    )["result"]
    assert fixed["content"][0]["text"] == "the fixed result"

    tool_error = modern_json(
        post_modern(
            mock,
            name,
            modern_body(
                "tools/call", {"name": "tool_error", "arguments": {}}, request_id=5
            ),
            modern_headers("tools/call", name="tool_error"),
        )
    )["result"]
    assert tool_error["isError"] is True
    assert tool_error["content"][0]["text"] == "bad tool"

    protocol_error = modern_json(
        post_modern(
            mock,
            name,
            modern_body(
                "tools/call", {"name": "protocol_error", "arguments": {}}, request_id=6
            ),
            modern_headers("tools/call", name="protocol_error"),
        )
    )
    assert protocol_error["error"] == {"code": -32000, "message": "bad protocol"}

    unknown = modern_json(
        post_modern(
            mock,
            name,
            modern_body(
                "tools/call", {"name": "missing", "arguments": {}}, request_id=7
            ),
            modern_headers("tools/call", name="missing"),
        )
    )
    assert unknown["error"]["code"] == -32602
    assert "Unknown tool" in unknown["error"]["message"]


def test_modern_header_validation(mock: MCPMock):
    name = "headers"
    mock.create_server(Server(name, tools=(Tool("echo"), Tool("écho"))))

    def error(body: dict, headers: dict[str, str], status: int = 400) -> dict:
        return modern_json(post_modern(mock, name, body, headers), status)

    body = modern_body("tools/call", {"name": "echo", "arguments": {}})
    missing_version = modern_headers("tools/call", name="echo")
    del missing_version["MCP-Protocol-Version"]
    assert error(body, missing_version)["error"]["code"] == -32020

    assert (
        error(body, modern_headers("tools/call", name="echo", version=LEGACY))["error"][
            "code"
        ]
        == -32020
    )
    assert (
        error(
            body,
            modern_headers("tools/call", name="echo") | {"Mcp-Method": "tools/list"},
        )["error"]["code"]
        == -32020
    )
    del body["params"]["_meta"]
    assert (
        error(body, modern_headers("tools/call", name="echo"))["error"]["code"]
        == -32602
    )

    body = modern_body("tools/call", {"name": "echo", "arguments": {}})
    body["params"]["_meta"].pop("io.modelcontextprotocol/clientCapabilities")
    assert (
        error(body, modern_headers("tools/call", name="echo"))["error"]["code"]
        == -32602
    )

    body = modern_body("tools/call", {"name": "echo", "arguments": {}})
    headers = modern_headers("tools/call", name="echo")
    del headers["Mcp-Name"]
    assert error(body, headers)["error"]["code"] == -32020

    body = modern_body("tools/call", {"name": "echo", "arguments": {}})
    assert (
        error(body, modern_headers("tools/call", name="other"))["error"]["code"]
        == -32020
    )

    body = modern_body("tools/call", {"name": "écho", "arguments": {}})
    unicode_name = modern_json(
        post_modern(
            mock, name, body, modern_headers("tools/call", name=base64_header("écho"))
        )
    )
    assert unicode_name["result"]["resultType"] == "complete"

    body = modern_body(
        "tools/list",
    )
    body["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"] = "1900-01-01"
    unsupported = modern_json(
        post_modern(
            mock,
            name,
            body,
            modern_headers("tools/list", version="1900-01-01"),
        ),
        400,
    )
    assert unsupported["error"]["code"] == -32022
    assert unsupported["error"]["data"]["supported"] == [MODERN]

    unknown = modern_json(
        post_modern(
            mock,
            name,
            modern_body("no/such/method", request_id=9),
            modern_headers("no/such/method"),
        ),
        404,
    )
    assert unknown["error"]["code"] == -32601

    assert mock.client.get(mock.url(name)).status_code == 405
    assert mock.client.delete(mock.url(name)).status_code == 405


def test_modern_param_headers(mock: MCPMock):
    name = "params"
    definition = {
        "name": "with_headers",
        "inputSchema": {
            "type": "object",
            "properties": {
                "region": {"type": "string", "x-mcp-header": "Region"},
                "count": {"type": "integer", "x-mcp-header": "Count"},
                "flag": {"type": "boolean", "x-mcp-header": "Flag"},
                "nested": {
                    "type": "object",
                    "properties": {
                        "token": {"type": "string", "x-mcp-header": "Token"},
                    },
                },
            },
        },
    }
    mock.create_server(
        Server(
            name,
            tools=(
                Tool(
                    "with_headers",
                    definition=definition,
                    input_schema=definition["inputSchema"],
                ),
            ),
        )
    )

    def call(
        arguments: dict, extra: dict[str, str] | None = None, status: int = 200
    ) -> dict:
        body = modern_body(
            "tools/call", {"name": "with_headers", "arguments": arguments}, request_id=1
        )
        headers = modern_headers("tools/call", name="with_headers", extra=extra)
        return modern_json(post_modern(mock, name, body, headers), status)

    ok = call(
        {
            "region": "us-west1",
            "count": 42,
            "flag": True,
            "nested": {"token": "Hello, 世界"},
        },
        {
            "Mcp-Param-Region": "us-west1",
            "Mcp-Param-Count": "42.0",
            "Mcp-Param-Flag": "true",
            "Mcp-Param-Token": base64_header("Hello, 世界"),
        },
    )
    assert ok["result"]["resultType"] == "complete"

    assert call({"region": "us-west1"}, status=400)["error"]["code"] == -32020
    assert (
        call({"region": "us-west1"}, {"Mcp-Param-Region": "us-east1"}, status=400)[
            "error"
        ]["code"]
        == -32020
    )
    assert (
        call(
            {"region": "us-west1"},
            {"Mcp-Param-Region": "us-west1", "Mcp-Param-Flag": "true"},
            status=400,
        )["error"]["code"]
        == -32020
    )
    assert (
        call(
            {"region": "us-west1", "nested": None},
            {"Mcp-Param-Region": "us-west1", "Mcp-Param-Token": "x"},
            status=400,
        )["error"]["code"]
        == -32020
    )
    assert (
        call(
            {"region": "padded "},
            {"Mcp-Param-Region": base64_header("padded ")},
        )["result"]["resultType"]
        == "complete"
    )
    assert (
        call(
            {"region": "line1\nline2"},
            {"Mcp-Param-Region": base64_header("line1\nline2")},
        )["result"]["resultType"]
        == "complete"
    )
    # Unknown Mcp-Param headers are ignored, not rejected.
    assert (
        call(
            {"region": "us-west1"},
            {"Mcp-Param-Region": "us-west1", "Mcp-Param-Other": "x"},
        )["result"]["resultType"]
        == "complete"
    )

    # Duplicate recognized headers are a mismatch even when the values are equal.
    headers = [
        ("content-type", "application/json"),
        ("MCP-Protocol-Version", MODERN),
        ("Mcp-Method", "tools/call"),
        ("Mcp-Name", "with_headers"),
        ("Mcp-Param-Region", "us-west1"),
        ("Mcp-Param-Region", "us-west1"),
    ]
    body = modern_body(
        "tools/call", {"name": "with_headers", "arguments": {"region": "us-west1"}}
    )
    duplicate = mock.client.post(mock.url(name), json=body, headers=headers)
    assert modern_json(duplicate, 400)["error"]["code"] == -32020


def test_modern_progress_and_mrtr(mock: MCPMock):
    progress_name = "progress"
    mock.create_server(
        Server(
            progress_name,
            response="sse",
            tools=(
                Tool(
                    "progress",
                    behaviour="progress",
                    progress_events=3,
                    progress_interval_ms=1,
                ),
            ),
        )
    )
    body = modern_body(
        "tools/call", {"name": "progress", "arguments": {}}, progress="tok"
    )
    with post_modern(
        mock,
        progress_name,
        body,
        modern_headers("tools/call", name="progress"),
        stream=True,
    ) as response:
        assert response.status_code == 200
        messages = sse_messages(response)
    assert [message["method"] for message in messages[:-1]] == [
        "notifications/progress",
        "notifications/progress",
        "notifications/progress",
    ]
    assert [message["params"]["progress"] for message in messages[:3]] == [
        1.0,
        2.0,
        3.0,
    ]
    assert messages[0]["params"]["progressToken"] == "tok"
    assert messages[-1]["result"]["resultType"] == "complete"

    mrtr_name = "mrtr"
    mock.create_server(
        Server(
            mrtr_name,
            tools=(Tool("needs_input", behaviour="mrtr"),),
        )
    )
    no_capability = modern_json(
        post_modern(
            mock,
            mrtr_name,
            modern_body("tools/call", {"name": "needs_input", "arguments": {}}),
            modern_headers("tools/call", name="needs_input"),
        ),
        400,
    )
    assert no_capability["error"]["code"] == -32021
    assert no_capability["error"]["data"]["requiredCapabilities"] == ["elicitation"]

    first = modern_json(
        post_modern(
            mock,
            mrtr_name,
            modern_body(
                "tools/call",
                {"name": "needs_input", "arguments": {}},
                capabilities={"elicitation": {}},
            ),
            modern_headers("tools/call", name="needs_input"),
        )
    )["result"]
    assert first["resultType"] == "input_required"
    assert first["inputRequests"]["confirm"]["method"] == "elicitation/create"
    request_state = first["requestState"]
    assert request_state.startswith("mock-mrtr:")

    completed = modern_json(
        post_modern(
            mock,
            mrtr_name,
            modern_body(
                "tools/call",
                {
                    "name": "needs_input",
                    "arguments": {},
                    "inputResponses": {
                        "confirm": {"action": "accept", "content": {"answer": "yes"}}
                    },
                    "requestState": request_state,
                },
                request_id=2,
                capabilities={"elicitation": {}},
            ),
            modern_headers("tools/call", name="needs_input"),
        )
    )["result"]
    assert completed["resultType"] == "complete"
    assert "MRTR completed" in completed["content"][0]["text"]

    wrong_state = modern_json(
        post_modern(
            mock,
            mrtr_name,
            modern_body(
                "tools/call",
                {
                    "name": "needs_input",
                    "arguments": {},
                    "inputResponses": {},
                    "requestState": "wrong",
                },
                request_id=3,
                capabilities={"elicitation": {}},
            ),
            modern_headers("tools/call", name="needs_input"),
        )
    )
    assert wrong_state["error"]["code"] == -32602


def test_modern_cancellation_by_closing_stream(mock: MCPMock):
    name = "modern_cancel"
    mock.create_server(
        Server(
            name,
            response="sse",
            tools=(Tool("slow", behaviour="sleep", sleep_ms=10_000),),
        )
    )
    body = modern_body("tools/call", {"name": "slow", "arguments": {}}, request_id=77)
    with (
        httpx.Client(timeout=10) as client,
        client.stream(
            "POST",
            mock.url(name),
            json=body,
            headers={
                **modern_headers("tools/call", name="slow"),
                "content-type": "application/json",
            },
        ) as response,
    ):
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        # Leaving the context closes the stream without a notifications/cancelled.
    record = mock.wait_for_record(
        lambda item: item.cancelled, server=name, tool="slow", timeout=5
    )
    assert record.cancelled_by == "disconnect"
    assert record.disconnected is True
    assert record.completed_after_disconnect is False


# Legacy -----------------------------------------------------------------------


def test_legacy_with_official_sdk(mock: MCPMock):
    name = "legacy_sdk"
    mock.create_server(
        Server(
            name,
            era="legacy",
            tools=(
                Tool("echo"),
                Tool(
                    "progress",
                    behaviour="progress",
                    progress_events=2,
                    progress_interval_ms=1,
                ),
                Tool("slow", behaviour="sleep", sleep_ms=5000),
            ),
        )
    )

    async def journey() -> tuple[
        list[str], list[tuple[float, float | None, str | None]], Any
    ]:
        async with Client(mock.url(name), mode="legacy") as client:
            assert client.protocol_version == LEGACY
            tools = await client.list_tools()
            progress: list[tuple[float, float | None, str | None]] = []

            async def on_progress(
                value: float, total: float | None, message: str | None
            ) -> None:
                progress.append((value, total, message))

            called = await client.call_tool("echo", {"a": 1})
            progressed = await client.call_tool(
                "progress", {}, progress_callback=on_progress
            )

            task = asyncio.create_task(client.call_tool("slow", {}))
            await asyncio.sleep(0.3)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return [tool.name for tool in tools.tools], progress, progressed, called

    tools, progress, progressed, called = asyncio.run(journey())
    assert tools == ["echo", "progress", "slow"]
    assert [value for value, _, _ in progress] == [1.0, 2.0]
    assert progressed.content[0].text == "emitted 2 progress events"
    assert called.content[0].text == '{"a":1}'

    record = mock.wait_for_record(
        lambda item: item.cancelled and item.tool == "slow",
        server=name,
        timeout=5,
    )
    assert record.cancelled_by == "notification"


def test_legacy_sessions_and_headers(mock: MCPMock):
    name = "legacy_sessions"
    mock.create_server(Server(name, era="legacy", tools=(Tool("echo"),)))
    session = initialize(mock, name)

    initialized = mock.client.post(
        mock.url(name),
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=legacy_headers(session),
    )
    assert initialized.status_code == 202
    assert initialized.content == b""

    listed = mock.client.post(
        mock.url(name),
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        headers=legacy_headers(session),
    )
    assert listed.status_code == 200
    result = listed.json()["result"]
    assert "resultType" not in result and "ttlMs" not in result
    assert result["tools"][0]["name"] == "echo"

    called = mock.client.post(
        mock.url(name),
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "echo", "arguments": {"x": 1}},
        },
        headers=legacy_headers(session),
    )
    assert called.status_code == 200
    assert called.json()["result"]["structuredContent"] == {"x": 1}

    unknown = mock.client.post(
        mock.url(name),
        json={"jsonrpc": "2.0", "id": 4, "method": "tools/list"},
        headers=legacy_headers("unknown-session"),
    )
    assert unknown.status_code == 404

    required = "legacy_required"
    mock.create_server(
        Server(required, era="legacy", session="required", tools=(Tool("echo"),))
    )
    required_session = initialize(mock, required)
    missing = mock.client.post(
        mock.url(required),
        json={"jsonrpc": "2.0", "id": 5, "method": "tools/list"},
        headers={"MCP-Protocol-Version": LEGACY},
    )
    assert missing.status_code == 400
    ok = mock.client.post(
        mock.url(required),
        json={"jsonrpc": "2.0", "id": 6, "method": "tools/list"},
        headers=legacy_headers(required_session),
    )
    assert ok.status_code == 200

    assert (
        mock.client.get(mock.url(name), headers=legacy_headers(session)).status_code
        == 405
    )

    streamed = "legacy_stream"
    mock.create_server(Server(streamed, era="legacy", get_stream=True, tools=()))
    stream_session = initialize(mock, streamed)
    with mock.client.stream(
        "GET", mock.url(streamed), headers=legacy_headers(stream_session)
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        first = next(response.iter_lines())
        assert first == ": mock-mcp stream open"


def test_legacy_disconnect_is_not_cancellation(mock: MCPMock):
    name = "legacy_disconnect"
    mock.create_server(
        Server(
            name,
            era="legacy",
            response="sse",
            tools=(Tool("slow", behaviour="sleep", sleep_ms=400),),
        )
    )
    session = initialize(mock, name)
    body = {
        "jsonrpc": "2.0",
        "id": 9,
        "method": "tools/call",
        "params": {"name": "slow", "arguments": {}},
    }
    with (
        httpx.Client(timeout=10) as client,
        client.stream(
            "POST",
            mock.url(name),
            json=body,
            headers={**legacy_headers(session), "content-type": "application/json"},
        ) as response,
    ):
        assert response.status_code == 200
        # The client leaves before the 400 ms tool completes.
    record = mock.wait_for_record(
        lambda item: item.disconnected and item.completed_at_ms is not None,
        server=name,
        tool="slow",
        timeout=5,
    )
    assert record.cancelled is False
    assert record.outcome == "result"
    assert record.completed_after_disconnect is True


def test_legacy_notification_cancels(mock: MCPMock):
    name = "legacy_cancel"
    mock.create_server(
        Server(
            name,
            era="legacy",
            tools=(Tool("slow", behaviour="sleep", sleep_ms=5000),),
        )
    )
    session = initialize(mock, name)
    body = {
        "jsonrpc": "2.0",
        "id": 12,
        "method": "tools/call",
        "params": {"name": "slow", "arguments": {}},
    }
    response_holder: list[httpx.Response] = []

    def call() -> None:
        response_holder.append(
            mock.client.post(
                mock.url(name),
                json=body,
                headers={**legacy_headers(session), "content-type": "application/json"},
                timeout=10,
            )
        )

    thread = threading.Thread(target=call)
    thread.start()
    mock.wait_for_record(
        lambda item: item.method == "tools/call" and not item.completed_at_ms,
        server=name,
        tool="slow",
        timeout=5,
    )
    cancelled = mock.client.post(
        mock.url(name),
        json={
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": 12, "reason": "test"},
        },
        headers=legacy_headers(session),
    )
    assert cancelled.status_code == 202
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert response_holder and response_holder[0].status_code == 202
    record = mock.wait_for_record(
        lambda item: item.cancelled, server=name, tool="slow", timeout=5
    )
    assert record.cancelled_by == "notification"


# Faults -----------------------------------------------------------------------


def test_http_faults(mock: MCPMock):
    name = "http_faults"
    mock.create_server(
        Server(name, bearer_token="upstream-token", tools=(Tool("echo"),))
    )
    body = modern_body("tools/list")
    headers = modern_headers("tools/list")

    unauthenticated = mock.client.post(mock.url(name), json=body, headers=headers)
    assert unauthenticated.status_code == 401
    assert "WWW-Authenticate" in unauthenticated.headers
    wrong = mock.client.post(
        mock.url(name),
        json=body,
        headers={**headers, "authorization": "Bearer wrong"},
    )
    assert wrong.status_code == 401
    ok = mock.client.post(
        mock.url(name),
        json=body,
        headers={**headers, "authorization": "Bearer upstream-token"},
    )
    modern_json(ok)

    records = mock.records(server=name)
    assert any(
        record.header("authorization") == "Bearer upstream-token" for record in records
    )

    origin = "origin_faults"
    mock.create_server(
        Server(
            origin, allowed_origins=("http://allowed.example",), tools=(Tool("echo"),)
        )
    )
    assert (
        mock.client.post(
            mock.url(origin),
            json=body,
            headers={**headers, "origin": "http://allowed.example"},
        ).status_code
        == 200
    )
    rejected = mock.client.post(
        mock.url(origin), json=body, headers={**headers, "origin": "http://bad.example"}
    )
    assert rejected.status_code == 403
    modern_json(rejected, 403)

    mock.set_fault(name, Fault("server_error", status=503))
    assert (
        mock.client.post(
            mock.url(name),
            json=body,
            headers={**headers, "authorization": "Bearer upstream-token"},
        ).status_code
        == 503
    )
    mock.set_fault(name, Fault("unauthorized", www_authenticate='Bearer realm="fault"'))
    faulted = mock.client.post(
        mock.url(name),
        json=body,
        headers={**headers, "authorization": "Bearer upstream-token"},
    )
    assert faulted.status_code == 401
    assert faulted.headers["www-authenticate"] == 'Bearer realm="fault"'
    mock.set_fault(name, Fault("wrong_content_type", content_type="text/plain"))
    wrong_type = mock.client.post(
        mock.url(name),
        json=body,
        headers={**headers, "authorization": "Bearer upstream-token"},
    )
    assert wrong_type.headers["content-type"].startswith("text/plain")
    RESPONSE_VALIDATOR.validate(wrong_type.json())
    mock.set_fault(name, Fault("compressed"))
    status, compressed_headers, compressed_body = raw_post(
        mock,
        name,
        body,
        {**headers, "authorization": "Bearer upstream-token"},
    )
    assert status == 200
    assert compressed_headers["content-encoding"] == "gzip"
    RESPONSE_VALIDATOR.validate(json.loads(gzip.decompress(compressed_body)))
    mock.set_fault(name, None)


def test_stream_faults(mock: MCPMock):
    name = "stream_faults"
    mock.create_server(Server(name, response="sse", tools=(Tool("echo"),)))
    body = modern_body("tools/list")
    headers = modern_headers("tools/list")

    def stream_fault(fault: Fault) -> None:
        mock.set_fault(name, fault)

    stream_fault(Fault("oversized_sse_event", bytes=4096))
    oversized = mock.client.post(mock.url(name), json=body, headers=headers)
    assert oversized.status_code == 200
    assert oversized.headers["content-type"].startswith("text/event-stream")
    assert len(oversized.content) > 4096
    assert b"xxxxxxxx" in oversized.content
    mock.set_fault(name, None)

    stream_fault(Fault("invalid_sse"))
    invalid = mock.client.post(mock.url(name), json=body, headers=headers)
    assert invalid.content.startswith(b'data: {"jsonrpc":"2.0","id":')
    assert b'"result"' not in invalid.content
    mock.set_fault(name, None)

    stream_fault(Fault("slow_reads", events=3, interval_ms=1))
    slow = mock.client.post(mock.url(name), json=body, headers=headers)
    assert slow.content.count(b": slow") == 3
    assert b'"result"' in slow.content
    mock.set_fault(name, None)

    stream_fault(Fault("reset_mid_stream", after_events=1))
    with pytest.raises(httpx.HTTPError):
        mock.client.post(mock.url(name), json=body, headers=headers)
    mock.set_fault(name, None)

    stream_fault(Fault("stalled_stream"))
    with (
        httpx.Client(timeout=0.3) as client,
        client.stream("POST", mock.url(name), json=body, headers=headers) as response,
    ):
        assert response.status_code == 200
        lines = response.iter_lines()
        assert next(lines) == ": stalled"
        assert next(lines) == ""
        with pytest.raises(httpx.TimeoutException):
            next(lines)
    mock.set_fault(name, None)


def test_session_404_once(mock: MCPMock):
    name = "expiry"
    mock.create_server(
        Server(
            name,
            era="legacy",
            fault=Fault("session_404_once"),
            tools=(Tool("echo"),),
        )
    )
    session = initialize(mock, name)
    expired = mock.client.post(
        mock.url(name),
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        headers=legacy_headers(session),
    )
    assert expired.status_code == 404
    new_session = initialize(mock, name)
    assert new_session != session
    recovered = mock.client.post(
        mock.url(name),
        json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
        headers=legacy_headers(new_session),
    )
    assert recovered.status_code == 200
    # The old session stays expired; the one-shot does not bring it back.
    again = mock.client.post(
        mock.url(name),
        json={"jsonrpc": "2.0", "id": 4, "method": "tools/list"},
        headers=legacy_headers(session),
    )
    assert again.status_code == 404


# Recording --------------------------------------------------------------------


def test_recording_filters_and_toggle(mock: MCPMock):
    name = "recording"
    mock.create_server(Server(name, tools=(Tool("echo"),)))
    mock.clear_records(server=name)
    body = modern_body("tools/call", {"name": "echo", "arguments": {"secret": "x"}})
    body["unknownField"] = "kept"
    response = mock.client.post(
        mock.url(name),
        json=body,
        headers={
            **modern_headers("tools/call", name="echo"),
            "authorization": "Bearer visible",
            "x-test": "one",
        },
    )
    modern_json(response)
    listing = mock.client.post(
        mock.url(name),
        json=modern_body("tools/list", request_id=2),
        headers=modern_headers("tools/list"),
    )
    modern_json(listing)

    records = mock.records(server=name)
    assert [record.method for record in records] == ["tools/call", "tools/list"]
    call = records[0]
    assert call.http_method == "POST"
    assert call.path == f"/s/{name}/mcp"
    assert call.header("authorization") == "Bearer visible"
    assert call.header("x-test") == "one"
    assert call.tool == "echo"
    assert call.request_id == 1
    assert call.era == "modern"
    assert call.result_type == "complete"
    assert json.loads(call.body)["unknownField"] == "kept"
    assert "secret" in call.body

    only_calls = mock.records(server=name, tool="echo")
    assert len(only_calls) == 1
    only_lists = mock.records(server=name, method="tools/list")
    assert len(only_lists) == 1
    after = mock.records(server=name, after=call.seq)
    assert all(record.seq > call.seq for record in after)

    cleared = mock.clear_records(server=name)
    assert cleared == 2
    assert mock.records(server=name) == []

    assert mock.set_recording(False) is False
    assert mock.recording() is False
    mock.client.post(
        mock.url(name),
        json=modern_body("tools/list", request_id=3),
        headers=modern_headers("tools/list"),
    )
    assert mock.records(server=name) == []
    assert mock.set_recording(True) is True


def test_recording_ring_is_bounded():
    with MCPMock.start(
        extra_args=["--record-capacity", "2", "--record-body-limit", "8"]
    ) as small:
        name = "ring"
        small.create_server(Server(name, tools=(Tool("echo"),)))
        for request_id in range(3):
            small.client.post(
                small.url(name),
                json=modern_body(
                    "tools/call",
                    {"name": "echo", "arguments": {"payload": "0123456789"}},
                    request_id=request_id,
                ),
                headers=modern_headers("tools/call", name="echo"),
            )
        records = small.records(server=name)
        assert len(records) == 2
        assert records[-1].seq == 2
        assert all(record.body_truncated for record in records)
        assert all(len(record.body.encode()) <= 8 for record in records)


# Official modern SDK ----------------------------------------------------------


def test_official_modern_sdk(mock: MCPMock):
    name = "modern_sdk"
    mock.create_server(
        Server(
            name,
            response="sse",
            tools=(
                Tool("echo"),
                Tool(
                    "progress",
                    behaviour="progress",
                    progress_events=2,
                    progress_interval_ms=1,
                ),
                Tool("needs_input", behaviour="mrtr"),
            ),
        )
    )

    async def journey() -> tuple[list[str], list[float], Any, Any]:
        elicited: list[Any] = []

        async def elicit(
            _context: Any, params: mcp_types.ElicitRequestParams
        ) -> mcp_types.ElicitResult:
            elicited.append(params)
            return mcp_types.ElicitResult(action="accept", content={"answer": "yes"})

        async with Client(
            mock.url(name), mode=MODERN, elicitation_callback=elicit
        ) as client:
            assert client.protocol_version == MODERN
            tools = await client.list_tools()
            progress: list[float] = []

            async def on_progress(
                value: float, _total: float | None, _message: str | None
            ) -> None:
                progress.append(value)

            echoed = await client.call_tool("echo", {"message": "hi"})
            progressed = await client.call_tool(
                "progress", {}, progress_callback=on_progress
            )
            mrtr = await client.call_tool("needs_input", {})
            return (
                [tool.name for tool in tools.tools],
                progress,
                echoed,
                (progressed, mrtr, elicited),
            )

    tools, progress, echoed, extra = asyncio.run(journey())
    progressed, mrtr, elicited = extra
    assert tools == ["echo", "progress", "needs_input"]
    assert echoed.content[0].text == '{"message":"hi"}'
    assert progress == [1.0, 2.0]
    assert progressed.content[0].text == "emitted 2 progress events"
    assert mrtr.content[0].text.startswith("MRTR completed")
    assert elicited and elicited[0].message.startswith("Confirm")
