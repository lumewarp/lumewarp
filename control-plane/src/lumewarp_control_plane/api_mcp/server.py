"""The user-facing MCP server: `POST /mcp` on the control plane's HTTP listener.

Stateless Streamable HTTP with JSON responses (official `mcp` SDK 1.x, low-level `Server`), so
it needs no session affinity across replicas. (SDK 2.x pulls in `httpx2`, which authlib then
uses for OIDC instead of `httpx`; the 1.x line keeps one HTTP client library.)
Authentication is a personal access token (`Authorization: Bearer lwp-…`); a session cookie
or a gateway key (`lw-…`) is refused with a 401 that points at the RFC 9728 metadata
document, ready for OAuth later.

A tool call is an in-process request against the same FastAPI app (`httpx.ASGITransport`),
carrying the caller's `Authorization` header unchanged plus the ASGI scope extension
`lumewarp.via = "mcp"`, which no network client can set. Authorization, validation, rate
limits, error shapes and audit are therefore exactly REST's. The PAT was issued by this same
resource, so this is not token passthrough.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlsplit

import httpx
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import Message, Receive, Scope, Send

from lumewarp_control_plane import __version__
from lumewarp_control_plane.api_mcp.catalog import (
    TOOLS,
    TOOLS_BY_NAME,
    TOOLSETS,
    Kind,
    MinRole,
    ToolSpec,
    Wrapper,
)
from lumewarp_control_plane.api_mcp.schema import Operation, operations
from lumewarp_control_plane.errors import error_body
from lumewarp_control_plane.security import SecretKind, authenticate, parse_bearer, secret_kind

if TYPE_CHECKING:
    from fastapi import FastAPI

log = logging.getLogger(__name__)

MCP_PATH = "/mcp"
METADATA_PATH = "/.well-known/oauth-protected-resource/mcp"
# The ASGI scope extension a tool call's in-process request carries (read by
# `security.current_principal`). Only this module's dispatcher sets it.
VIA_EXTENSION = "lumewarp.via"
VIA_MCP = "mcp"
TOOLSETS_HEADER = "x-lumewarp-mcp-toolsets"
# The bounded `method` label of `lumewarp_cp_api_mcp_requests_total`.
API_MCP_METHODS = ("initialize", "tools/list", "tools/call", "ping", "notification", "other")
MAX_BODY_BYTES = 4 * 1024 * 1024
_SCOPE_KEY = "lumewarp.api_mcp.caller"
_INNER_BASE_URL = "http://lumewarp-api-mcp"

INSTRUCTIONS = (
    "Lumewarp is an LLM gateway. These tools read what you can call (models, limits, budget), "
    "manage your own virtual API keys (`lw-…`), and read usage, requests and incident status, "
    "with the same permissions as the REST API. Administrators also get read-only tenancy "
    "tools. Administrator changes are not available here; use the Admin UI."
)

SECRET_MASKED_NOTE = (
    "The new key secret is masked: it is shown only once and was not returned, so it stays out "
    "of this conversation, and the key cannot be used until it gets a secret someone has seen. "
    "Call rotate_my_key with reveal_secret: true to get a new secret, or create keys in the "
    "Admin UI (My keys) to copy the secret there."
)
SECRET_REVEALED_WARNING = (
    "WARNING: the result contains a plaintext key secret. It is now in this conversation and "
    "possibly in the client's logs; it is not shown again. Store it in a secret manager and "
    "rotate the key if the conversation is shared."
)


@dataclass(frozen=True)
class Caller:
    authorization: str
    user_id: uuid.UUID
    toolsets: frozenset[str]


class ToolError(Exception):
    """A refusal reported to the model as a tool error (`isError`)."""

    def __init__(self, outcome: str, body: dict[str, Any]) -> None:
        super().__init__(body)
        self.outcome = outcome
        self.body = body


def _error(code: str, message: str) -> dict[str, Any]:
    return error_body(code, message)


class ApiMcp:
    """The MCP server, its session manager and its two routes for one FastAPI app."""

    def __init__(self, app: FastAPI) -> None:
        self.app = app
        self._tools: dict[str, types.Tool] | None = None
        self._operations: dict[str, Operation] | None = None
        self.server: Server[Any, Request] = Server(
            "lumewarp", version=__version__, instructions=INSTRUCTIONS
        )
        # Registered directly, not through the `list_tools`/`call_tool` decorators: those keep
        # one tool cache for every caller and re-run the listing on a cache miss, while the
        # listing here depends on the caller and the arguments are checked by REST.
        self.server.request_handlers[types.ListToolsRequest] = self._list_tools
        self.server.request_handlers[types.CallToolRequest] = self._call_tool
        self.session_manager: StreamableHTTPSessionManager | None = None
        if not app.state.settings.public_url:
            log.warning(
                "LUMEWARP_PUBLIC_URL is unset: the /mcp 401 challenge and resource metadata "
                "are built from the request's Host header; set it when /mcp is exposed"
            )

    @asynccontextmanager
    async def run(self) -> AsyncIterator[None]:
        """Serve requests for one app lifespan.

        A session manager runs only once, so every lifespan (a test client may enter the
        app's twice) gets a new one. It runs in a task of its own: its anyio task group must
        be entered and exited by one task, which a lifespan driven by an async fixture
        (setup and teardown in different tasks) would not guarantee.
        """
        # Origin is checked in `__call__` (settings-driven); the SDK keeps checking the POST
        # Content-Type and the body size.
        manager = StreamableHTTPSessionManager(
            app=self.server,
            json_response=True,
            stateless=True,
            max_request_body_size=MAX_BODY_BYTES,
        )
        started = asyncio.Event()
        stop = asyncio.Event()

        async def serve() -> None:
            async with manager.run():
                started.set()
                await stop.wait()

        task = asyncio.create_task(serve(), name="api-mcp-session-manager")
        waiter = asyncio.create_task(started.wait())
        await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if task.done():
            waiter.cancel()
            task.result()  # raises the startup failure
            raise RuntimeError("api_mcp: the session manager stopped during startup")
        self.session_manager = manager
        try:
            yield
        finally:
            self.session_manager = None
            stop.set()
            await task

    # --- routes -----------------------------------------------------------------------

    def routes(self) -> list[Route]:
        return [
            Route(MCP_PATH, endpoint=self, include_in_schema=False),
            Route(METADATA_PATH, endpoint=self.metadata, methods=["GET"], include_in_schema=False),
        ]

    def _base_url(self, request: Request) -> str:
        public = self.app.state.settings.public_url
        if public:
            return public.rstrip("/")
        return str(request.base_url).rstrip("/")

    async def metadata(self, request: Request) -> Response:
        """RFC 9728 protected resource metadata. No `authorization_servers` yet (OPTIONAL in
        RFC 9728): the only credential is a personal access token from the Admin UI."""
        return JSONResponse(
            {
                "resource": self._base_url(request) + MCP_PATH,
                "bearer_methods_supported": ["header"],
                "resource_name": "Lumewarp",
            }
        )

    def _origin_allowed(self, origin: str) -> bool:
        settings = self.app.state.settings
        allowed = list(settings.api_mcp_allowed_origins)
        if not allowed and settings.public_url:
            parts = urlsplit(settings.public_url)
            allowed = [f"{parts.scheme}://{parts.netloc}"]
        return origin.rstrip("/") in allowed

    def _unauthorized(self, request: Request, presented: bool) -> Response:
        challenge = f'Bearer resource_metadata="{self._base_url(request)}{METADATA_PATH}"'
        if presented:
            challenge = 'Bearer error="invalid_token", ' + challenge.removeprefix("Bearer ")
        return JSONResponse(
            _error(
                "unauthorized",
                "a personal access token (Authorization: Bearer lwp-…) is required",
            ),
            status_code=401,
            headers={"WWW-Authenticate": challenge},
        )

    @staticmethod
    def _toolsets(request: Request) -> frozenset[str] | None:
        raw = request.query_params.get("toolsets") or request.headers.get(TOOLSETS_HEADER) or ""
        names = {item.strip() for item in raw.split(",") if item.strip()}
        if not names:
            return frozenset(TOOLSETS)
        if not names <= set(TOOLSETS):
            return None
        return frozenset(names)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        metrics = self.app.state.metrics
        status = 500
        method = "other"

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                # Tool results are per-user data and may carry a revealed key secret.
                headers = [
                    h for h in message.get("headers", []) if h[0].lower() != b"cache-control"
                ]
                message = {**message, "headers": [*headers, (b"cache-control", b"no-store")]}
            await send(message)

        try:
            request = Request(scope, receive)
            response = await self._precheck(request)
            if response is not None:
                await response(scope, receive, send_wrapper)
                return
            if request.method != "POST":
                # Stateless JSON only: no standalone SSE stream (GET) and no session to DELETE.
                await Response(status_code=405, headers={"Allow": "POST"})(
                    scope, receive, send_wrapper
                )
                return
            body = b""
            if request.method == "POST":
                body = await _read_body(receive)
                if body is None:
                    await Response("Request body too large", status_code=413)(
                        scope, receive, send_wrapper
                    )
                    return
                method = _method_label(body)
            manager = self.session_manager
            if manager is None:  # outside the app's lifespan
                await JSONResponse(
                    _error("unavailable", "the MCP endpoint is not running"), status_code=503
                )(scope, receive, send_wrapper)
                return
            await manager.handle_request(scope, _replay(body, receive), send_wrapper)
        finally:
            metrics.api_mcp_requests.labels(method, _outcome(status)).inc()

    async def _precheck(self, request: Request) -> Response | None:
        """Origin, toolsets and the token; on success the caller is put in the scope."""
        origin = request.headers.get("origin")
        if origin and not self._origin_allowed(origin):
            return JSONResponse(
                _error("forbidden_origin", "this Origin may not call the MCP endpoint"),
                status_code=403,
            )
        token = parse_bearer(request.headers.get("authorization"))
        if token is None or secret_kind(token) is not SecretKind.PAT:
            return self._unauthorized(request, presented=token is not None)
        state = self.app.state
        principal = await authenticate(state.session_factory, token, state.settings)
        if principal is None or principal.user_id is None:
            return self._unauthorized(request, presented=True)
        toolsets = self._toolsets(request)
        if toolsets is None:
            return JSONResponse(
                _error(
                    "invalid_toolsets",
                    "toolsets must be a comma-separated subset of " + ",".join(TOOLSETS),
                ),
                status_code=400,
            )
        request.scope[_SCOPE_KEY] = Caller(
            authorization=f"Bearer {token}", user_id=principal.user_id, toolsets=toolsets
        )
        return None

    # --- tools ------------------------------------------------------------------------

    def _ops(self) -> dict[str, Operation]:
        if self._operations is None:
            self._operations = operations(self.app.openapi())
        return self._operations

    def tools(self) -> dict[str, types.Tool]:
        """Every tool definition, built once from the app's OpenAPI document."""
        if self._tools is None:
            ops = self._ops()
            self._tools = {spec.name: _tool(spec, ops[spec.operation_id]) for spec in TOOLS}
        return self._tools

    async def _list_tools(self, request: types.ListToolsRequest) -> types.ServerResult:
        caller = _caller(self.server)
        role = await self._role(caller)
        tools = self.tools()
        listed = [
            tools[spec.name]
            for spec in TOOLS
            if spec.toolset in caller.toolsets and spec.min_role <= role
        ]
        return types.ServerResult(types.ListToolsResult(tools=listed))

    async def _role(self, caller: Caller) -> MinRole:
        """The caller's highest role, from `getCurrentUser` (as REST sees it)."""
        try:
            status, me = await self._request(caller, "GET", "/api/v1/users/me")
        except Exception:
            log.exception("api_mcp: getCurrentUser failed; listing user tools only")
            return MinRole.USER
        if status != 200 or not isinstance(me, dict) or me.get("kind") != "user":
            return MinRole.USER
        if me.get("installation_role"):
            return MinRole.INSTALLATION_ADMIN
        if me.get("admin_orgs"):
            return MinRole.ORG_ADMIN
        if any(team.get("role") == "admin" for team in me.get("teams") or []):
            return MinRole.TEAM_ADMIN
        return MinRole.USER

    async def _call_tool(self, request: types.CallToolRequest) -> types.ServerResult:
        return types.ServerResult(await self._call(request.params))

    async def _call(self, params: types.CallToolRequestParams) -> types.CallToolResult:
        caller = _caller(self.server)
        spec = TOOLS_BY_NAME.get(params.name)
        if spec is None or spec.toolset not in caller.toolsets:
            # An unknown name is not a toolset/kind we can label; count it as "other".
            self.app.state.metrics.api_mcp_tool_calls.labels("other", "other", "unknown").inc()
            return _error_result(_error("unknown_tool", f"no tool named {params.name!r}"))
        outcome = "error"
        try:
            result = await self._run(spec, caller, dict(params.arguments or {}))
            outcome = "ok"
            return result
        except ToolError as exc:
            outcome = exc.outcome
            return _error_result(exc.body)
        finally:
            self.app.state.metrics.api_mcp_tool_calls.labels(
                spec.toolset, spec.kind.value, outcome
            ).inc()

    async def _run(
        self, spec: ToolSpec, caller: Caller, args: dict[str, Any]
    ) -> types.CallToolResult:
        op = self._ops()[spec.operation_id]
        reveal = args.pop("reveal_secret", False) is True if spec.secret_result else False
        if spec.destructive:
            confirmed = args.pop("confirm", None) is True
            if not confirmed:
                raise ToolError(
                    "refused",
                    _error(
                        "confirmation_required",
                        f"{spec.name} {_EFFECT[spec.wrapper]} Call it again with "
                        '"confirm": true to go ahead.',
                    ),
                )
        if spec.wrapper in (Wrapper.UPDATE, Wrapper.REVOKE, Wrapper.ROTATE):
            await self._require_own_key(spec, caller, args.get("key_id"))
        body_fields = op.body_fields
        if spec.wrapper is Wrapper.CREATE and body_fields is not None:
            body_fields = tuple(name for name in body_fields if name != "service_account_id")

        status, body = await self._dispatch(caller, op, args, body_fields)
        if not 200 <= status < 300:
            raise ToolError("rest_error", _rest_error(status, body))

        notes: list[str] = []
        if spec.secret_result and isinstance(body, dict) and "secret" in body:
            if reveal:
                notes.append(SECRET_REVEALED_WARNING)
            else:
                prefix = (body.get("key") or {}).get("display_prefix") or "lw-"
                body = {**body, "secret": f"{prefix}… (masked)"}
                notes.append(SECRET_MASKED_NOTE)
        structured = {"result": body} if op.wraps_result else body
        content: list[types.ContentBlock] = [
            types.TextContent(type="text", text=note)
            for note in notes
            if note == SECRET_REVEALED_WARNING
        ]
        content.append(
            types.TextContent(type="text", text=json.dumps(structured, ensure_ascii=False))
        )
        content.extend(
            types.TextContent(type="text", text=note)
            for note in notes
            if note != SECRET_REVEALED_WARNING
        )
        return types.CallToolResult(content=content, structuredContent=structured)

    async def _require_own_key(self, spec: ToolSpec, caller: Caller, key_id: Any) -> None:
        """`*_my_key` only touch a user-owned key of the caller; REST authz still applies."""
        if not isinstance(key_id, str) or not key_id:
            raise ToolError("invalid", _error("invalid_arguments", "key_id is required"))
        status, key = await self._request(caller, "GET", f"/api/v1/keys/{quote(key_id, safe='')}")
        if status != 200:
            raise ToolError("rest_error", _rest_error(status, key))
        if key.get("owner_kind") != "user" or key.get("user_id") != str(caller.user_id):
            raise ToolError(
                "refused",
                _error(
                    "not_your_key",
                    f"{spec.name} only changes keys you own; this key belongs to "
                    + (
                        "a service account"
                        if key.get("owner_kind") == "service_account"
                        else "another user"
                    )
                    + ". Use the Admin UI or the REST API.",
                ),
            )

    async def _dispatch(
        self,
        caller: Caller,
        op: Operation,
        args: dict[str, Any],
        body_fields: tuple[str, ...] | None,
    ) -> tuple[int, Any]:
        path = op.path
        for name in op.path_params:
            value = args.pop(name, None)
            if value is None or value == "":
                raise ToolError("invalid", _error("invalid_arguments", f"{name} is required"))
            if str(value) in (".", ".."):
                # Dot segments would be normalised into another path.
                raise ToolError("invalid", _error("invalid_arguments", f"{name} is not valid"))
            path = path.replace("{" + name + "}", quote(str(value), safe=""))
        query: dict[str, Any] = {}
        for name in op.query_params:
            value = args.pop(name, None)
            if value is not None:
                query[name] = value
        payload: dict[str, Any] | None = None
        if body_fields is not None:
            payload = {name: args.pop(name) for name in body_fields if name in args}
            if not payload and not op.body_required:
                payload = None
        if args:
            raise ToolError(
                "invalid",
                _error("invalid_arguments", "unknown argument(s): " + ", ".join(sorted(args))),
            )
        return await self._request(caller, op.method, path, params=query, json_body=payload)

    async def _request(
        self,
        caller: Caller,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
    ) -> tuple[int, Any]:
        transport = httpx.ASGITransport(app=_ViaMcp(self.app))
        async with httpx.AsyncClient(transport=transport, base_url=_INNER_BASE_URL) as client:
            response = await client.request(
                method,
                path,
                params=params or None,
                json=json_body,
                headers={"Authorization": caller.authorization},
            )
        if not response.content:
            return response.status_code, {}
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, {"raw": response.text[:2000]}


_EFFECT = {
    Wrapper.REVOKE: "revokes the key permanently: every client using it fails from now on.",
    Wrapper.ROTATE: (
        "replaces the key's secret: clients using the old secret fail once the grace period "
        "(grace_seconds, default 0 = at once) ends."
    ),
}


class _ViaMcp:
    """Marks the in-process request as a tool call (`scope["extensions"]["lumewarp.via"]`)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        extensions = dict(scope.get("extensions") or {})
        extensions[VIA_EXTENSION] = VIA_MCP
        await self.app({**scope, "extensions": extensions}, receive, send)


def _caller(server: Server[Any, Request]) -> Caller:
    """The authenticated caller `__call__` put in the HTTP request's scope."""
    request = server.request_context.request
    caller = request.scope.get(_SCOPE_KEY) if request is not None else None
    if not isinstance(caller, Caller):  # only reachable by a transport bypassing __call__
        raise RuntimeError("api_mcp: request without an authenticated caller")
    return caller


def _tool(spec: ToolSpec, op: Operation) -> types.Tool:
    schema = json.loads(json.dumps(op.input_schema))
    properties: dict[str, Any] = schema["properties"]
    required: list[str] = schema.get("required", [])
    if spec.wrapper is Wrapper.CREATE:
        properties.pop("service_account_id", None)
        if "service_account_id" in required:
            required.remove("service_account_id")
    if spec.destructive:
        properties["confirm"] = {
            "type": "boolean",
            "description": "Must be true: confirms the change after the user agreed to it.",
        }
    if spec.secret_result:
        properties["reveal_secret"] = {
            "type": "boolean",
            "default": False,
            "description": (
                "Return the new secret in plaintext (it enters this conversation); false masks it."
            ),
        }
    if spec.kind is Kind.READ:
        annotations = types.ToolAnnotations(readOnlyHint=True, openWorldHint=False)
    else:
        annotations = types.ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=spec.destructive,
            idempotentHint=spec.wrapper is Wrapper.UPDATE,
            openWorldHint=False,
        )
    return types.Tool(
        name=spec.name,
        description=spec.description or op.description,
        inputSchema=schema,
        annotations=annotations,
    )


def _error_result(body: dict[str, Any]) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(body, ensure_ascii=False))],
        isError=True,
    )


def _rest_error(status: int, body: Any) -> dict[str, Any]:
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        error = {"code": "http_error", "message": f"HTTP {status}", "details": body}
    return {"status": status, "error": error}


def _method_label(body: bytes) -> str:
    try:
        message = json.loads(body)
    except ValueError:
        return "other"
    method = message.get("method") if isinstance(message, dict) else None
    if not isinstance(method, str):
        return "other"
    if method.startswith("notifications/"):
        return "notification"
    return method if method in API_MCP_METHODS else "other"


def _outcome(status: int) -> str:
    if 200 <= status < 300:
        return "ok"
    if status == 401:
        return "unauthorized"
    if status == 403:
        return "forbidden"
    if 400 <= status < 500:
        return "rejected"
    return "error"


async def _read_body(receive: Receive) -> bytes | None:
    """The whole request body, or None once it exceeds MAX_BODY_BYTES."""
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            return None
        chunks.append(chunk)
        if not message.get("more_body", False):
            break
    return b"".join(chunks)


def _replay(body: bytes, receive: Receive) -> Receive:
    sent = False

    async def replay() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return replay
