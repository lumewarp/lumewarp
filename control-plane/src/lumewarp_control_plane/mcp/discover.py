"""Discover now (plan P6): fetch one MCP server's tools and prompts once, from the control
plane, with a token an administrator pasted for that call.

Only for a server whose auth the control plane can reproduce — `none`, `static_bearer` or
`passthrough` (the pasted token is then the user's). A WASM upstream-auth plugin cannot be
run here (no WASM, no plugin secrets), so such a server is observable from traffic only.

Stateless: nothing is stored by this module. The caller records the results as observed
definitions (`catalog_ingest`) when it has a server to record them against. Errors never
contain the token and never echo a response body: an upstream can reflect credentials.

Wire: Streamable HTTP, MCP revision 2025-11-25 — `initialize` (JSON-RPC over POST, an SSE
stream or a JSON body in the answer), `notifications/initialized`, then paged `tools/list`
and `prompts/list`. A session id the server returns (`Mcp-Session-Id`) is echoed on every
later request of this discovery.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from lumewarp_control_plane.mcp import hashing
from lumewarp_control_plane.models import McpDefinitionKind

PROTOCOL_VERSION = "2025-11-25"
CLIENT_NAME = "lumewarp-control-plane"
CLIENT_VERSION = "0.1.0"
DEFAULT_TIMEOUT_MS = 10_000
MAX_TIMEOUT_MS = 120_000
# L10's non-streaming response bound; a discovery answer is one such response per page.
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_EVENT_BYTES = 1024 * 1024
MAX_PAGES = 20
MAX_PAGE_ITEMS = 500
# Plan P5: at most this many definitions are worth recording for one server.
MAX_DEFINITIONS = 200

# Tests replace this with an httpx.MockTransport.
transport: httpx.AsyncBaseTransport | None = None


class DiscoverFailed(Exception):
    """The server could not be reached or did not answer as an MCP server."""


@dataclass
class Discovered:
    """What one discovery call found, already projected and hashed."""

    tools: list[hashing.Observed] = field(default_factory=list)
    prompts: list[hashing.Observed] = field(default_factory=list)
    protocol_version: str = PROTOCOL_VERSION
    truncated: bool = False

    def observations(self) -> list[tuple[McpDefinitionKind, hashing.Observed]]:
        return [
            *((McpDefinitionKind.TOOL, item) for item in self.tools),
            *((McpDefinitionKind.PROMPT, item) for item in self.prompts),
        ]


def _client(timeout_ms: int) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=timeout_ms / 1000,
        follow_redirects=False,
        transport=transport,
        headers={"accept": "application/json, text/event-stream"},
    )


def _headers(token: str | None, session_id: str | None) -> dict[str, str]:
    headers = {"content-type": "application/json"}
    if token:
        headers["authorization"] = f"Bearer {token}"
    if session_id:
        headers["mcp-session-id"] = session_id
    return headers


async def _read_body(response: httpx.Response) -> bytes:
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body += chunk
        if len(body) > MAX_RESPONSE_BYTES:
            raise DiscoverFailed("response larger than 16 MiB")
    return bytes(body)


def _json_or_sse(body: bytes, content_type: str, request_id: Any) -> dict[str, Any]:
    """The JSON-RPC response carried by a plain JSON body or by one SSE event."""
    text = body.decode("utf-8", "replace")
    is_sse = "text/event-stream" in content_type or text.lstrip().startswith(("event:", "data:"))
    if not is_sse:
        return _message(text)
    # SSE: events are blank-line separated; `data:` lines concatenate into one JSON value.
    data: list[str] = []
    for line in text.splitlines():
        if line.startswith("data:"):
            data.append(line[5:].lstrip())
        elif not line.strip() and data:
            message = _message("\n".join(data))
            if message.get("id") == request_id:
                return message
            data = []
    if data:
        message = _message("\n".join(data))
        if message.get("id") == request_id:
            return message
    raise DiscoverFailed("the server sent no JSON-RPC response for the request")


def _message(text: str) -> dict[str, Any]:
    if len(text.encode("utf-8")) > MAX_EVENT_BYTES:
        raise DiscoverFailed("a single event is larger than 1 MiB")
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        raise DiscoverFailed("the server answered with invalid JSON") from None
    if not isinstance(value, dict):
        raise DiscoverFailed("the server answered with a non-object JSON-RPC message")
    return value


def _error(message: dict[str, Any], what: str) -> str:
    error = message.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        text = error.get("message")
        detail = f"{code}: {text}" if code is not None else str(text)
        return f"{what} failed: {detail[:300]}"
    return f"{what} failed: the server sent no result"


async def _call(
    client: httpx.AsyncClient,
    url: str,
    *,
    token: str | None,
    session_id: str | None,
    request_id: int,
    method: str,
    params: dict[str, Any] | None,
) -> tuple[dict[str, Any], str | None]:
    """One JSON-RPC request; returns (result, session id the server may have set)."""
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        payload["params"] = params
    try:
        async with client.stream(
            "POST", url, headers=_headers(token, session_id), json=payload
        ) as response:
            body = await _read_body(response)
            new_session = response.headers.get("mcp-session-id") or session_id
            status = response.status_code
            content_type = response.headers.get("content-type", "")
    except httpx.HTTPError as exc:
        raise DiscoverFailed(_describe(exc)) from None
    if status == 401 or status == 403:
        raise DiscoverFailed(f"the server refused the credential ({status})")
    if status >= 400:
        raise DiscoverFailed(f"the server answered HTTP {status}")
    message = _json_or_sse(body, content_type, request_id)
    if "error" in message:
        raise DiscoverFailed(_error(message, method))
    result = message.get("result")
    if not isinstance(result, dict):
        raise DiscoverFailed(f"{method} answered without a result object")
    return result, new_session


async def _notify(
    client: httpx.AsyncClient, url: str, *, token: str | None, session_id: str | None
) -> str | None:
    """`notifications/initialized`: a notification, so any 2xx (or 202) ends it."""
    try:
        async with client.stream(
            "POST",
            url,
            headers=_headers(token, session_id),
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        ) as response:
            await _read_body(response)
            new_session = response.headers.get("mcp-session-id") or session_id
            status = response.status_code
    except httpx.HTTPError as exc:
        raise DiscoverFailed(_describe(exc)) from None
    if status >= 400:
        raise DiscoverFailed(f"the server refused notifications/initialized ({status})")
    return new_session


def _describe(exc: httpx.HTTPError) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "timed out"
    if isinstance(exc, httpx.ConnectError):
        return "could not connect"
    return "the request failed"


async def _list(
    client: httpx.AsyncClient,
    url: str,
    *,
    token: str | None,
    session: list[str | None],
    method: str,
    key: str,
    next_request_id: list[int],
    kind: McpDefinitionKind,
    discover: Discovered,
) -> None:
    """One paged list call, bounded in pages, items and total definitions (P5's cap)."""
    out = discover.tools if kind is McpDefinitionKind.TOOL else discover.prompts
    cursor: str | None = None
    for _ in range(MAX_PAGES):
        next_request_id[0] += 1
        params = {"cursor": cursor} if cursor else {}
        result, session[0] = await _call(
            client,
            url,
            token=token,
            session_id=session[0],
            request_id=next_request_id[0],
            method=method,
            params=params,
        )
        items = result.get(key, [])
        if not isinstance(items, list):
            raise DiscoverFailed(f"{method} answered with no {key} list")
        for item in items[:MAX_PAGE_ITEMS]:
            if len(discover.tools) + len(discover.prompts) >= MAX_DEFINITIONS:
                discover.truncated = True
                return
            observed = hashing.observe(kind, item)
            if observed is None:
                continue
            out.append(observed)
        cursor = result.get("nextCursor")
        if not isinstance(cursor, str) or not cursor:
            return
    discover.truncated = True


async def discover(
    url: str,
    *,
    token: str | None = None,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> Discovered:
    """Initialize a session and list tools and prompts, bounded by the plan's limits."""
    timeout_ms = min(max(timeout_ms, 1000), MAX_TIMEOUT_MS)
    discovered = Discovered()
    async with _client(timeout_ms) as client:
        result, session_id = await _call(
            client,
            url,
            token=token,
            session_id=None,
            request_id=1,
            method="initialize",
            params={
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
            },
        )
        version = result.get("protocolVersion")
        if isinstance(version, str) and version:
            discovered.protocol_version = version
        session = [await _notify(client, url, token=token, session_id=session_id)]
        counter = [1]
        # A capability object may be empty: its presence under its name is what counts.
        capabilities = result.get("capabilities")
        known = capabilities if isinstance(capabilities, dict) else {}
        tools = "tools" in known if known else True
        prompts = "prompts" in known if known else True
        if tools:
            await _list(
                client,
                url,
                token=token,
                session=session,
                method="tools/list",
                key="tools",
                next_request_id=counter,
                kind=McpDefinitionKind.TOOL,
                discover=discovered,
            )
        if prompts:
            await _list(
                client,
                url,
                token=token,
                session=session,
                method="prompts/list",
                key="prompts",
                next_request_id=counter,
                kind=McpDefinitionKind.PROMPT,
                discover=discovered,
            )
    return discovered
