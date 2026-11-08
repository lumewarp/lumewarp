"""MCP servers and A2A agents: CRUD, definition review, and Discover now.

Both resources follow the Upstreams conventions: installation admins only
(`ROUTING_READ`/`ROUTING_MANAGE`), one transaction per mutation with an audit row and a
recompile request, `force` to save past a plugin a connected gateway lacks (audited as
`mcp_server.force_save` / `a2a_agent.force_save`), and `config_invalid`/`config_problems`
computed on read from what gateways report.

A write that names a plugin goes through `validate_plugin_params`, like a router. Secrets
never leave the database: `secret_ref` is an env var name (returned), `secret` is stored
sealed and only its presence is returned.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Sequence
from typing import Any, Literal

from fastapi import APIRouter, Response, status
from pydantic import SecretStr
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane import audit, sealing
from lumewarp_control_plane.api.common import (
    LimitDep,
    MetricsDep,
    SessionDep,
    SettingsDep,
    conflict,
    enqueue_recompile,
    lock_scope,
    mutation,
    parse_uuid_cursor,
    uuid_page,
    validation_error,
)
from lumewarp_control_plane.api.schemas import (
    A2aAgentCreate,
    A2aAgentOut,
    A2aAgentUpdate,
    McpDefinitionOut,
    McpDiscoveredOut,
    McpDiscoverIn,
    McpDiscoverOut,
    McpServerCreate,
    McpServerOut,
    McpServerUpdate,
    Page,
    PluginRefOut,
)
from lumewarp_control_plane.authz import Action, Authz
from lumewarp_control_plane.compiler.plugins import (
    PluginRegistry,
    PluginRejected,
    audit_target_forced,
    load_plugins,
    validate_plugin_params,
)
from lumewarp_control_plane.compiler.validate import BUILTIN_AUTH
from lumewarp_control_plane.errors import ApiError, not_found
from lumewarp_control_plane.mcp import catalog_ingest, discover, hashing
from lumewarp_control_plane.models import (
    A2aAgent,
    McpDefinition,
    McpDefinitionKind,
    McpDefinitionState,
    McpReviewMode,
    McpServer,
    PluginKind,
)
from lumewarp_control_plane.settings import Settings

router = APIRouter(tags=["mcp"])

# What a review of one definition may do, and the state it produces.
REVIEW_ACTIONS = {
    "approve": McpDefinitionState.APPROVED,
    "reject": McpDefinitionState.REJECTED,
}


# --------------------------------------------------------------------------- helpers


def _secret(settings: Settings, secret: SecretStr) -> bytes:
    try:
        return sealing.seal(settings, secret.get_secret_value(), info=sealing.AUTH_SECRET_INFO)
    except sealing.SealingUnavailable as exc:
        raise validation_error(
            "auth.secret", "LUMEWARP_ENCRYPTION_KEY is not configured on the control plane"
        ) from exc


async def _validate_auth(
    session: AsyncSession,
    settings: Settings,
    auth: Any,
    *,
    force: bool,
) -> list[str]:
    """422 unless the gateways can serve the auth plugin; returns what `force` saved past."""
    try:
        return await validate_plugin_params(
            session,
            PluginKind.UPSTREAM_AUTH,
            auth.plugin,
            auth.params,
            force=force,
            scope=settings.scope,
        )
    except PluginRejected as exc:
        raise validation_error(f"auth.{exc.field}", exc.message) from exc


async def _validate_middleware(
    session: AsyncSession,
    settings: Settings,
    middleware: Any,
    *,
    force: bool,
) -> list[str]:
    if middleware is None:
        return []
    try:
        return await validate_plugin_params(
            session,
            PluginKind.A2A_MIDDLEWARE,
            middleware.plugin,
            middleware.params,
            force=force,
            scope=settings.scope,
        )
    except PluginRejected as exc:
        raise validation_error(f"middleware.{exc.field}", exc.message) from exc


def _plugin_ref(row: Any, *, prefix: str = "") -> PluginRefOut:
    params = getattr(row, f"{prefix}params") if prefix else row.auth_params
    plugin = getattr(row, f"{prefix}plugin") if prefix else row.auth_plugin
    timeout = row.middleware_timeout_ms if prefix else row.auth_timeout_ms
    return PluginRefOut(plugin=plugin, params=params or {}, timeout_ms=timeout)


def _auth_columns(settings: Settings, auth: Any) -> dict[str, Any]:
    """The stored auth columns of a create or of a PATCH that replaces the auth."""
    return {
        "auth_plugin": auth.plugin,
        "auth_params": auth.params,
        "auth_timeout_ms": auth.timeout_ms,
        "secret_ref": auth.secret_ref,
        "secret_ciphertext": (_secret(settings, auth.secret) if auth.secret is not None else None),
    }


def _target_problems(
    registry: PluginRegistry, kind: PluginKind, plugin: str, params: dict[str, Any]
) -> list[str]:
    return registry.problems(kind, plugin, params)


async def _ensure_mcp_name_free(
    session: AsyncSession, name: str, *, exclude: uuid.UUID | None = None
) -> None:
    stmt = select(McpServer.id).where(McpServer.name == name, McpServer.deleted_at.is_(None))
    if exclude is not None:
        stmt = stmt.where(McpServer.id != exclude)
    if await session.scalar(stmt) is not None:
        raise conflict(f"MCP server name {name!r} is already in use")


async def _ensure_agent_name_free(
    session: AsyncSession, name: str, *, exclude: uuid.UUID | None = None
) -> None:
    stmt = select(A2aAgent.id).where(A2aAgent.name == name, A2aAgent.deleted_at.is_(None))
    if exclude is not None:
        stmt = stmt.where(A2aAgent.id != exclude)
    if await session.scalar(stmt) is not None:
        raise conflict(f"A2A agent name {name!r} is already in use")


async def _definition_counts(
    session: AsyncSession, server_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, dict[str, int]]:
    counts = {
        server_id: {state.value: 0 for state in McpDefinitionState} for server_id in server_ids
    }
    if not server_ids:
        return counts
    rows = await session.execute(
        select(McpDefinition.server_id, McpDefinition.state, func.count())
        .where(McpDefinition.server_id.in_(server_ids))
        .group_by(McpDefinition.server_id, McpDefinition.state)
    )
    for server_id, state, count in rows:
        counts[server_id][state.value] = count
    return counts


def _definition_out(row: McpDefinition) -> McpDefinitionOut:
    return McpDefinitionOut(
        id=row.id,
        server_id=row.server_id,
        kind=row.kind,
        name=row.name,
        hash=row.hash.hex(),
        definition_json=row.definition_json,
        state=row.state,
        first_seen_at=row.first_seen_at,
        last_seen_at=row.last_seen_at,
        decided_by_user_id=row.decided_by_user_id,
        decided_at=row.decided_at,
    )


def _definition_page_cursor(cursor: str | None) -> uuid.UUID | None:
    return parse_uuid_cursor(cursor) if cursor is not None else None


# --------------------------------------------------------------------------- mcp servers


async def _mcp_servers_out(
    session: AsyncSession, settings: Settings, rows: Sequence[McpServer]
) -> list[McpServerOut]:
    registry = await load_plugins(session, settings.scope)
    counts = await _definition_counts(session, [row.id for row in rows])
    problems = {
        row.id: _target_problems(
            registry, PluginKind.UPSTREAM_AUTH, row.auth_plugin, row.auth_params or {}
        )
        for row in rows
    }
    return [
        McpServerOut(
            id=row.id,
            name=row.name,
            url=row.url,
            auth=_plugin_ref(row),
            secret_ref=row.secret_ref,
            secret_set=row.secret_ciphertext is not None,
            timeout_ms=row.timeout_ms,
            review_mode=row.review_mode,
            definitions=counts[row.id],
            config_invalid=bool(problems[row.id]),
            config_problems=problems[row.id],
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
        for row in rows
    ]


async def _live_server(session: AsyncSession, server_id: uuid.UUID) -> McpServer:
    row = await session.get(McpServer, server_id)
    if row is None or row.deleted_at is not None:
        raise not_found("MCP server not found")
    return row


@router.get("/mcp-servers", response_model=Page[McpServerOut], operation_id="listMcpServers")
async def list_mcp_servers(
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
    limit: LimitDep = 50,
    cursor: str | None = None,
) -> Page[McpServerOut]:
    await authz.require(Action.ROUTING_READ)
    # One ordering and one cursor rule for every list (m1): the previous cursor is an id and
    # the next page is "id greater than it". Ordering by name made the two disagree.
    stmt = select(McpServer).where(McpServer.deleted_at.is_(None)).order_by(McpServer.id)
    rows, next_cursor = await uuid_page(session, stmt, McpServer.id, limit, cursor)
    return Page[McpServerOut](
        items=await _mcp_servers_out(session, settings, rows), next_cursor=next_cursor
    )


@router.post(
    "/mcp-servers",
    response_model=McpServerOut,
    status_code=status.HTTP_201_CREATED,
    operation_id="createMcpServer",
)
async def create_mcp_server(
    body: McpServerCreate,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> McpServerOut:
    await authz.require(Action.ROUTING_MANAGE)
    async with mutation(session):
        await lock_scope(session, settings)
        await _ensure_mcp_name_free(session, body.name)
        missing = await _validate_auth(session, settings, body.auth, force=body.force)
        row = McpServer(
            name=body.name,
            url=body.url,
            timeout_ms=body.timeout_ms,
            review_mode=body.review_mode,
            **_auth_columns(settings, body.auth),
        )
        session.add(row)
        await session.flush()
        # Provenance for a CatalogReport: only a version compiled at least this new can be
        # trusted about this row (catalog_ingest). Set before the refresh, so the refreshed
        # row (and its server-side `updated_at`) is what the response reads.
        row.config_revision = await enqueue_recompile(session, settings, "api.mcp_server.create")
        # refresh() does not autoflush, so flush the new revision first: otherwise the
        # reload discards the assignment and the row keeps the server default 0.
        await session.flush()
        await session.refresh(row)
        audit.record(
            session,
            action="mcp_server.create",
            resource_type="mcp_server",
            resource_id=row.id,
            principal=authz.principal,
            details={
                "name": row.name,
                "url": row.url,
                "review_mode": row.review_mode,
                "credential_stored": row.secret_ciphertext is not None,
            },
        )
        audit_target_forced(
            session,
            kind="mcp_server",
            resource_id=row.id,
            name=row.name,
            plugin=row.auth_plugin,
            missing=missing,
            principal=authz.principal,
        )
    return (await _mcp_servers_out(session, settings, [row]))[0]


@router.get("/mcp-servers/{server_id}", response_model=McpServerOut, operation_id="getMcpServer")
async def get_mcp_server(
    server_id: uuid.UUID, authz: Authz, session: SessionDep, settings: SettingsDep
) -> McpServerOut:
    await authz.require(Action.ROUTING_READ)
    row = await _live_server(session, server_id)
    return (await _mcp_servers_out(session, settings, [row]))[0]


@router.patch(
    "/mcp-servers/{server_id}", response_model=McpServerOut, operation_id="updateMcpServer"
)
async def update_mcp_server(
    server_id: uuid.UUID,
    body: McpServerUpdate,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> McpServerOut:
    await authz.require(Action.ROUTING_MANAGE)
    async with mutation(session):
        await lock_scope(session, settings)
        row = await _live_server(session, server_id)
        if "name" in body.model_fields_set:
            await _ensure_mcp_name_free(session, body.name, exclude=row.id)
        missing: list[str] = []
        if "auth" in body.model_fields_set:
            # A stale stored plugin never blocks a rename or a timeout change, only a write
            # that names one (the routers rule).
            missing = await _validate_auth(session, settings, body.auth, force=body.force)
            for name, value in _auth_columns(settings, body.auth).items():
                setattr(row, name, value)
        for name in ("name", "url", "timeout_ms", "review_mode"):
            if name in body.model_fields_set:
                setattr(row, name, getattr(body, name))
        row.config_revision = await enqueue_recompile(session, settings, "api.mcp_server.update")
        await session.flush()
        await session.refresh(row)
        audit.record(
            session,
            action="mcp_server.update",
            resource_type="mcp_server",
            resource_id=row.id,
            principal=authz.principal,
            details={
                "name": row.name,
                "credential_changed": "auth" in body.model_fields_set,
                "changes": body.model_dump(exclude_unset=True, exclude={"force"}, mode="json"),
            },
        )
        audit_target_forced(
            session,
            kind="mcp_server",
            resource_id=row.id,
            name=row.name,
            plugin=row.auth_plugin,
            missing=missing,
            principal=authz.principal,
        )
    return (await _mcp_servers_out(session, settings, [row]))[0]


@router.delete(
    "/mcp-servers/{server_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="deleteMcpServer",
)
async def delete_mcp_server(
    server_id: uuid.UUID, authz: Authz, session: SessionDep, settings: SettingsDep
) -> Response:
    """Archive the server: the name is free again, definitions and grants stay for history,
    and the compiler leaves it out of the snapshot."""
    await authz.require(Action.ROUTING_MANAGE)
    async with mutation(session):
        await lock_scope(session, settings)
        row = await session.get(McpServer, server_id)
        if row is None or row.deleted_at is not None:
            raise not_found("MCP server not found")
        row.deleted_at = func.now()
        await session.flush()
        audit.record(
            session,
            action="mcp_server.delete",
            resource_type="mcp_server",
            resource_id=row.id,
            principal=authz.principal,
            details={"name": row.name},
        )
        await enqueue_recompile(session, settings, "api.mcp_server.delete")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- definitions


@router.get(
    "/mcp-servers/{server_id}/definitions",
    response_model=Page[McpDefinitionOut],
    operation_id="listMcpDefinitions",
)
async def list_mcp_definitions(
    server_id: uuid.UUID,
    authz: Authz,
    session: SessionDep,
    limit: LimitDep = 50,
    cursor: str | None = None,
    state: McpDefinitionState | None = None,
    kind: McpDefinitionKind | None = None,
) -> Page[McpDefinitionOut]:
    await authz.require(Action.ROUTING_READ)
    await _live_server(session, server_id)
    stmt = select(McpDefinition).where(McpDefinition.server_id == server_id)
    if state is not None:
        stmt = stmt.where(McpDefinition.state == state)
    if kind is not None:
        stmt = stmt.where(McpDefinition.kind == kind)
    before = _definition_page_cursor(cursor)
    if before is not None:
        stmt = stmt.where(McpDefinition.id < before)
    # Newest first: a review queue is read from the top.
    rows = list(
        (await session.scalars(stmt.order_by(McpDefinition.id.desc()).limit(limit + 1))).all()
    )
    next_cursor = str(rows[limit - 1].id) if len(rows) > limit else None
    return Page[McpDefinitionOut](
        items=[_definition_out(row) for row in rows[:limit]], next_cursor=next_cursor
    )


async def _review(
    server_id: uuid.UUID,
    definition_id: uuid.UUID,
    *,
    action: Literal["approve", "reject"],
    authz: Authz,
    session: AsyncSession,
    settings: Settings,
) -> McpDefinition:
    state = REVIEW_ACTIONS[action]
    async with mutation(session):
        await lock_scope(session, settings)
        server = await _live_server(session, server_id)
        row = await session.get(McpDefinition, definition_id, populate_existing=True)
        if row is None or row.server_id != server.id:
            raise not_found("definition not found")
        if row.state is state:
            return row  # idempotent: no new audit row, no recompile
        superseded = 0
        if state is McpDefinitionState.APPROVED:
            # One approved hash per name is the point of a review: the other pending rows
            # of this name describe definitions the upstream no longer sends (or that the
            # administrator just judged), so they are superseded. Rejected rows stay.
            others = select(McpDefinition.id).where(
                McpDefinition.server_id == server.id,
                McpDefinition.kind == row.kind,
                McpDefinition.name == row.name,
                McpDefinition.state == McpDefinitionState.PENDING,
                McpDefinition.id != row.id,
            )
            superseded = len((await session.scalars(others)).all())
            await session.execute(delete(McpDefinition).where(McpDefinition.id.in_(others)))
        row.state = state
        row.decided_by_user_id = authz.user_id
        row.decided_at = func.now()
        await session.flush()
        await session.refresh(row)
        audit.record(
            session,
            action=(
                "mcp_server.definition_approve"
                if action == "approve"
                else "mcp_server.definition_reject"
            ),
            resource_type="mcp_definition",
            resource_id=row.id,
            principal=authz.principal,
            details={
                "server": server.name,
                "kind": row.kind.value,
                "name": row.name,
                "hash": row.hash.hex(),
                "superseded": superseded,
            },
        )
        await enqueue_recompile(session, settings, f"api.mcp_server.definition_{action}")
    return row


@router.post(
    "/mcp-servers/{server_id}/definitions/{definition_id}/approve",
    response_model=McpDefinitionOut,
    operation_id="approveMcpDefinition",
)
async def approve_mcp_definition(
    server_id: uuid.UUID,
    definition_id: uuid.UUID,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
    metrics: MetricsDep,
) -> McpDefinitionOut:
    """Approve one hash. Other pending hashes of the same name are superseded (deleted);
    rejected hashes are left alone. Idempotent on an already-approved row."""
    await authz.require(Action.ROUTING_MANAGE)
    row = await _review(
        server_id,
        definition_id,
        action="approve",
        authz=authz,
        session=session,
        settings=settings,
    )
    metrics.definition_reviews.labels("approve").inc()
    return _definition_out(row)


@router.post(
    "/mcp-servers/{server_id}/definitions/{definition_id}/reject",
    response_model=McpDefinitionOut,
    operation_id="rejectMcpDefinition",
)
async def reject_mcp_definition(
    server_id: uuid.UUID,
    definition_id: uuid.UUID,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
    metrics: MetricsDep,
) -> McpDefinitionOut:
    """Reject one hash: the gateway never serves it, and reporting it again leaves it
    rejected. Idempotent on an already-rejected row."""
    await authz.require(Action.ROUTING_MANAGE)
    row = await _review(
        server_id,
        definition_id,
        action="reject",
        authz=authz,
        session=session,
        settings=settings,
    )
    metrics.definition_reviews.labels("reject").inc()
    return _definition_out(row)


# --------------------------------------------------------------------------- discover now


def _stored_token(settings: Settings, plugin: str, secret_ref: str | None, sealed: bytes | None):
    """(token, credential label) the control plane can produce for an auth configuration."""
    if sealed is not None:
        try:
            return sealing.unseal(settings, sealed, info=sealing.AUTH_SECRET_INFO), "stored"
        except sealing.SealingUnavailable as exc:
            raise ApiError(
                502,
                "upstream_unavailable",
                "LUMEWARP_ENCRYPTION_KEY is not configured on the control plane",
            ) from exc
        except sealing.SealedInvalid as exc:
            raise ApiError(
                502,
                "upstream_unavailable",
                "the stored token does not open with LUMEWARP_ENCRYPTION_KEY",
            ) from exc
    if secret_ref:
        token = os.environ.get(secret_ref) or None
        return token, ("env" if token else "env_unset")
    return None, "none"


@router.post("/mcp-servers/discover", response_model=McpDiscoverOut, operation_id="discoverMcp")
async def discover_mcp_server(
    body: McpDiscoverIn,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
    metrics: MetricsDep,
) -> McpDiscoverOut:
    """Call `initialize`, `notifications/initialized`, `tools/list` and `prompts/list` once.

    The token is used once, never stored and never audited. With `server_id` the result is
    recorded as observed definitions of that server, under the same first-writer rules as a
    gateway's `CatalogReport`; without it nothing is written. Nothing is recorded for a URL
    other than the saved one, or when the server's configuration changed while the call was
    in flight (`recorded` is false): a preview must not decide a review for a configuration
    it did not observe. A server behind a WASM upstream-auth plugin cannot be discovered
    here (the control plane does not run WASM).
    """
    await authz.require(Action.ROUTING_MANAGE)
    server_id = body.server_id
    server_name: str | None = None
    # What the saved server looked like when the call went out: (config revision, URL).
    # Recording needs both unchanged afterwards (M5).
    captured: tuple[int, str] | None = None
    token: str | None = None
    credential: Literal["entered", "stored", "env", "env_unset", "none"] = "none"
    if body.token is not None:
        token, credential = body.token.get_secret_value(), "entered"
    if server_id is not None:
        # One short read transaction: the network call must not run inside a transaction.
        async with session.begin():
            server = await session.get(McpServer, server_id)
            if server is None or server.deleted_at is not None:
                raise not_found("MCP server not found")
            plugin, secret_ref, sealed = (
                server.auth_plugin,
                server.secret_ref,
                server.secret_ciphertext,
            )
            server_name = server.name
            # A preview of a URL that is not the saved one observes a different upstream:
            # it is shown, never recorded against this server.
            if body.url == server.url:
                captured = (server.config_revision, server.url)
        if plugin not in BUILTIN_AUTH:
            raise validation_error(
                "server_id",
                f"auth plugin {plugin!r} cannot run here; this server is observable from"
                " traffic only",
            )
        if token is None:
            token, credential = _stored_token(settings, plugin, secret_ref, sealed)
    try:
        discovered = await discover.discover(body.url, token=token, timeout_ms=body.timeout_ms)
    except discover.DiscoverFailed as exc:
        metrics.mcp_discover.labels("failed").inc()
        # The message is a fixed text, the method and a validated numeric code; upstream
        # text (which may reflect the token) never reaches the API (M4).
        raise ApiError(502, "upstream_unavailable", str(exc)) from None
    metrics.mcp_discover.labels("ok").inc()

    states: dict[tuple[McpDefinitionKind, str, bytes], McpDefinitionState] = {}
    result = catalog_ingest.IngestResult()
    recorded = captured is not None
    if recorded:
        async with mutation(session):
            await lock_scope(session, settings)
            server = await session.get(
                McpServer, server_id, populate_existing=True, with_for_update=True
            )
            live = server is not None and server.deleted_at is None
            # An edit while the call was in flight — URL, auth, review mode, a rename — means
            # the answer describes a configuration that is no longer saved. Record nothing
            # rather than auto-approve an old upstream under the new one.
            if not live or (server.config_revision, server.url) != captured:
                recorded = False
            else:
                observations = [
                    catalog_ingest.Observation(
                        kind=kind,
                        name=item.name,
                        canonical_json=item.canonical_json,
                        hash=item.hash,
                        # Decided by the locked row: a preview whose review mode changed is
                        # not recorded, and MANUAL never auto-approves anyway.
                        auto_approved=server.review_mode is not McpReviewMode.MANUAL,
                    )
                    for kind, item in discovered.observations()
                ]
                result = await catalog_ingest.record_observations(session, server, observations)
                if result.changed:
                    await enqueue_recompile(session, settings, "api.mcp_server.discover")
                audit.record(
                    session,
                    action="mcp_server.discover",
                    resource_type="mcp_server",
                    resource_id=server.id,
                    principal=authz.principal,
                    details={
                        "server": server.name,
                        "credential": credential,
                        "definitions": result.stored,
                        "approved": result.approved,
                        "pending": result.pending,
                    },
                )
                if observations:
                    rows = await session.execute(
                        select(
                            McpDefinition.kind,
                            McpDefinition.name,
                            McpDefinition.hash,
                            McpDefinition.state,
                        ).where(
                            McpDefinition.server_id == server.id,
                            McpDefinition.hash.in_([item.hash for item in observations]),
                        )
                    )
                    states = {(kind, name, digest): state for kind, name, digest, state in rows}
    metrics.record_catalog(result.counts(), result.discarded)

    def out(kind: McpDefinitionKind, item: hashing.Observed) -> McpDiscoveredOut:
        return McpDiscoveredOut(
            kind=kind,
            name=item.name,
            hash=item.hash.hex(),
            definition_json=item.canonical_json,
            state=(
                states[(kind, item.name, item.hash)].value
                if (kind, item.name, item.hash) in states
                else "unrecorded"
            ),
        )

    return McpDiscoverOut(
        credential=credential,
        server_id=server_id,
        server=server_name,
        protocol_version=discovered.protocol_version,
        tools=[out(McpDefinitionKind.TOOL, item) for item in discovered.tools],
        prompts=[out(McpDefinitionKind.PROMPT, item) for item in discovered.prompts],
        truncated=discovered.truncated,
        recorded=recorded,
    )


# --------------------------------------------------------------------------- a2a agents


async def _agents_out(
    session: AsyncSession, settings: Settings, rows: Sequence[A2aAgent]
) -> list[A2aAgentOut]:
    registry = await load_plugins(session, settings.scope)
    out: list[A2aAgentOut] = []
    for row in rows:
        problems = _target_problems(
            registry, PluginKind.UPSTREAM_AUTH, row.auth_plugin, row.auth_params or {}
        )
        if row.middleware_plugin:
            problems += _target_problems(
                registry,
                PluginKind.A2A_MIDDLEWARE,
                row.middleware_plugin,
                row.middleware_params or {},
            )
        out.append(
            A2aAgentOut(
                id=row.id,
                name=row.name,
                url=row.url,
                card_url=row.card_url,
                auth=_plugin_ref(row),
                secret_ref=row.secret_ref,
                secret_set=row.secret_ciphertext is not None,
                middleware=(
                    _plugin_ref(row, prefix="middleware_") if row.middleware_plugin else None
                ),
                timeout_ms=row.timeout_ms,
                card_json=row.card_json,
                last_card_json=row.last_card_json,
                last_card_error=row.last_card_error,
                last_card_at=row.last_card_at,
                config_invalid=bool(problems),
                config_problems=problems,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
        )
    return out


async def _live_agent(session: AsyncSession, agent_id: uuid.UUID) -> A2aAgent:
    row = await session.get(A2aAgent, agent_id)
    if row is None or row.deleted_at is not None:
        raise not_found("A2A agent not found")
    return row


@router.get("/a2a-agents", response_model=Page[A2aAgentOut], operation_id="listA2aAgents")
async def list_a2a_agents(
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
    limit: LimitDep = 50,
    cursor: str | None = None,
) -> Page[A2aAgentOut]:
    await authz.require(Action.ROUTING_READ)
    stmt = select(A2aAgent).where(A2aAgent.deleted_at.is_(None)).order_by(A2aAgent.id)
    rows, next_cursor = await uuid_page(session, stmt, A2aAgent.id, limit, cursor)
    return Page[A2aAgentOut](
        items=await _agents_out(session, settings, rows), next_cursor=next_cursor
    )


@router.post(
    "/a2a-agents",
    response_model=A2aAgentOut,
    status_code=status.HTTP_201_CREATED,
    operation_id="createA2aAgent",
)
async def create_a2a_agent(
    body: A2aAgentCreate,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> A2aAgentOut:
    await authz.require(Action.ROUTING_MANAGE)
    async with mutation(session):
        await lock_scope(session, settings)
        await _ensure_agent_name_free(session, body.name)
        missing = await _validate_auth(session, settings, body.auth, force=body.force)
        missing += await _validate_middleware(session, settings, body.middleware, force=body.force)
        row = A2aAgent(
            name=body.name,
            url=body.url,
            card_url=body.card_url or "",
            timeout_ms=body.timeout_ms,
            card_json=body.card_json or "",
            middleware_plugin=body.middleware.plugin if body.middleware else "",
            middleware_params=body.middleware.params if body.middleware else {},
            middleware_timeout_ms=body.middleware.timeout_ms if body.middleware else 0,
            **_auth_columns(settings, body.auth),
        )
        session.add(row)
        await session.flush()
        row.config_revision = await enqueue_recompile(session, settings, "api.a2a_agent.create")
        # refresh() does not autoflush; flush the revision or the reload keeps 0.
        await session.flush()
        await session.refresh(row)
        audit.record(
            session,
            action="a2a_agent.create",
            resource_type="a2a_agent",
            resource_id=row.id,
            principal=authz.principal,
            details={
                "name": row.name,
                "url": row.url,
                "card_url": row.card_url,
                "credential_stored": row.secret_ciphertext is not None,
                "middleware": row.middleware_plugin or None,
            },
        )
        audit_target_forced(
            session,
            kind="a2a_agent",
            resource_id=row.id,
            name=row.name,
            plugin=row.middleware_plugin or row.auth_plugin,
            missing=missing,
            principal=authz.principal,
        )
    return (await _agents_out(session, settings, [row]))[0]


@router.get("/a2a-agents/{agent_id}", response_model=A2aAgentOut, operation_id="getA2aAgent")
async def get_a2a_agent(
    agent_id: uuid.UUID, authz: Authz, session: SessionDep, settings: SettingsDep
) -> A2aAgentOut:
    await authz.require(Action.ROUTING_READ)
    row = await _live_agent(session, agent_id)
    return (await _agents_out(session, settings, [row]))[0]


@router.patch("/a2a-agents/{agent_id}", response_model=A2aAgentOut, operation_id="updateA2aAgent")
async def update_a2a_agent(
    agent_id: uuid.UUID,
    body: A2aAgentUpdate,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> A2aAgentOut:
    await authz.require(Action.ROUTING_MANAGE)
    async with mutation(session):
        await lock_scope(session, settings)
        row = await _live_agent(session, agent_id)
        if "name" in body.model_fields_set:
            await _ensure_agent_name_free(session, body.name, exclude=row.id)
        missing: list[str] = []
        if "auth" in body.model_fields_set:
            missing = await _validate_auth(session, settings, body.auth, force=body.force)
            for name, value in _auth_columns(settings, body.auth).items():
                setattr(row, name, value)
        if "middleware" in body.model_fields_set:
            missing += await _validate_middleware(
                session, settings, body.middleware, force=body.force
            )
            row.middleware_plugin = body.middleware.plugin if body.middleware else ""
            row.middleware_params = body.middleware.params if body.middleware else {}
            row.middleware_timeout_ms = body.middleware.timeout_ms if body.middleware else 0
        if "name" in body.model_fields_set:
            row.name = body.name
        if "url" in body.model_fields_set:
            row.url = body.url
        if "card_url" in body.model_fields_set:
            row.card_url = body.card_url or ""
        if "timeout_ms" in body.model_fields_set:
            row.timeout_ms = body.timeout_ms
        if "card_json" in body.model_fields_set:
            row.card_json = body.card_json or ""
        row.config_revision = await enqueue_recompile(session, settings, "api.a2a_agent.update")
        await session.flush()
        await session.refresh(row)
        audit.record(
            session,
            action="a2a_agent.update",
            resource_type="a2a_agent",
            resource_id=row.id,
            principal=authz.principal,
            details={
                "name": row.name,
                "credential_changed": "auth" in body.model_fields_set,
                "changes": body.model_dump(exclude_unset=True, exclude={"force"}, mode="json"),
            },
        )
        audit_target_forced(
            session,
            kind="a2a_agent",
            resource_id=row.id,
            name=row.name,
            plugin=row.middleware_plugin or row.auth_plugin,
            missing=missing,
            principal=authz.principal,
        )
    return (await _agents_out(session, settings, [row]))[0]


@router.delete(
    "/a2a-agents/{agent_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="deleteA2aAgent",
)
async def delete_a2a_agent(
    agent_id: uuid.UUID, authz: Authz, session: SessionDep, settings: SettingsDep
) -> Response:
    """Archive the agent: the name is free again and the compiler leaves it out."""
    await authz.require(Action.ROUTING_MANAGE)
    async with mutation(session):
        await lock_scope(session, settings)
        row = await session.get(A2aAgent, agent_id)
        if row is None or row.deleted_at is not None:
            raise not_found("A2A agent not found")
        row.deleted_at = func.now()
        await session.flush()
        audit.record(
            session,
            action="a2a_agent.delete",
            resource_type="a2a_agent",
            resource_id=row.id,
            principal=authz.principal,
            details={"name": row.name},
        )
        await enqueue_recompile(session, settings, "api.a2a_agent.delete")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
