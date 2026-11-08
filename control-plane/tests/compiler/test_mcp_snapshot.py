"""The MCP and A2A halves of a snapshot: entities, grants, capabilities, delta, rollback."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from lumewarp_control_plane._proto.lumewarp.config.v1 import snapshot_pb2 as pb
from lumewarp_control_plane.compiler import publish_pending, rollback_routing
from lumewarp_control_plane.compiler.compile import CompileError, compile_scope
from lumewarp_control_plane.compiler.delta import diff
from lumewarp_control_plane.compiler.plugins import PluginRejected
from lumewarp_control_plane.db import SessionFactory
from lumewarp_control_plane.mcp import hashing
from lumewarp_control_plane.models import (
    ApiKey,
    AuditLog,
    McpDefinition,
    McpDefinitionKind,
    McpDefinitionState,
    McpReviewMode,
    McpServer,
    Router,
    Team,
)
from tests import factories
from tests.api.helpers import report_plugins
from tests.compiler.helpers import SCOPE, load_version, seed_scope, touch

AUTH_SCHEMA = {
    "type": "object",
    "properties": {"ttl_ms": {"type": "integer", "minimum": 1}},
    "required": ["ttl_ms"],
    "additionalProperties": False,
}


def observe(name: str, kind: McpDefinitionKind = McpDefinitionKind.TOOL) -> hashing.Observed:
    value = hashing.observe(kind, {"name": name, "description": f"{name}"})
    assert value is not None
    return value


def definition(name: str, state: McpDefinitionState) -> McpDefinition:
    return factories.new_definition(McpDefinitionKind.TOOL, observe(name), state=state)


async def compile_(factory: SessionFactory, **kwargs):
    async with factory() as s:
        return await compile_scope(s, SCOPE, require_local_membership=True, **kwargs)


async def seed_key(factory: SessionFactory, **policy) -> tuple[Team, ApiKey]:
    async with factory() as s, s.begin():
        org = await factories.make_org(s)
        team = await factories.make_team(s, org=org, **policy)
        user = await factories.make_user(s)
        await factories.make_member(s, team, user)
        key = await factories.make_key(s, team, user)
    return team, key


async def test_entities_definitions_and_capabilities(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        approved = await factories.make_mcp_server(
            s,
            "s1",
            review_mode=McpReviewMode.MANUAL,
            definitions=[
                definition("echo", McpDefinitionState.APPROVED),
                definition("add", McpDefinitionState.PENDING),
                definition("gone", McpDefinitionState.REJECTED),
            ],
        )
        await factories.make_agent(
            s,
            "a1",
            middleware_plugin="strip_artifacts",
            middleware_params={"mime": "x/y"},
            card_json='{"name": "agent"}',
        )
        approved_id = str(approved.id)
    snap = (await compile_(factory)).snapshot
    server = snap.mcp_servers[0]
    assert (server.id, server.name, server.url) == (approved_id, "s1", "http://s1.test/mcp")
    assert server.review_mode == pb.MCP_REVIEW_MODE_MANUAL
    assert server.auth.plugin.plugin == "none"
    assert [(d.name, d.state) for d in server.definitions] == [
        ("add", pb.MCP_DEFINITION_STATE_PENDING),
        ("echo", pb.MCP_DEFINITION_STATE_APPROVED),
        ("gone", pb.MCP_DEFINITION_STATE_REJECTED),
    ]
    # A state is never UNSPECIFIED in a snapshot: the gateway treats it as "approved", which
    # would serve a pending or rejected definition on that pod.
    assert all(d.state != pb.MCP_DEFINITION_STATE_UNSPECIFIED for d in server.definitions)
    agent = snap.a2a_agents[0]
    assert agent.name == "a1" and agent.card_url == ""
    assert agent.middleware.plugin == "strip_artifacts"
    assert agent.middleware.params_json == '{"mime":"x/y"}'
    assert agent.card_json == '{"name": "agent"}'
    assert list(snap.required_capabilities) == [
        "a2a",
        "a2a_middleware:strip_artifacts",
        "auth:none",
        "mcp",
    ]


async def test_no_mcp_entity_means_no_mcp_capability(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        model = await factories.make_model(s, "a")
        await factories.make_router(s, "r", [(model, 1)])
    caps = list((await compile_(factory)).snapshot.required_capabilities)
    assert caps == ["strategy:weighted"]


async def test_a_wasm_auth_plugin_becomes_its_own_capability(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        await factories.make_mcp_server(
            s, "s1", auth_plugin="hmac_signature", auth_params={"ttl_ms": 1000}
        )
        await factories.make_agent(s, "a1", auth_plugin="static_bearer", secret_ref="A2A_TOKEN")
    snap = (await compile_(factory)).snapshot
    assert snap.mcp_servers[0].auth.plugin.params_json == '{"ttl_ms":1000}'
    assert snap.a2a_agents[0].auth.plugin.plugin == "static_bearer"
    assert list(snap.required_capabilities) == [
        "a2a",
        "auth:hmac_signature",
        "auth:static_bearer",
        "mcp",
    ]


async def test_archived_targets_are_left_out(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        server = await factories.make_mcp_server(s, "s1")
        server.deleted_at = datetime.now(UTC)
        agent = await factories.make_agent(s, "a1")
        agent.deleted_at = datetime.now(UTC)
    snap = (await compile_(factory)).snapshot
    assert list(snap.mcp_servers) == [] and list(snap.a2a_agents) == []


async def test_grants_are_emitted_per_key(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        server = await factories.make_mcp_server(
            s, "s1", definitions=[definition("echo", McpDefinitionState.APPROVED)]
        )
        agent = await factories.make_agent(s, "a1")
        org = await factories.make_org(s, mcp_access="list", a2a_access="list")
        await factories.mcp_grant(s, org, server, all_tools=True)
        await factories.agent_grant(s, org, agent)
        team = await factories.make_team(s, org=org, mcp_access="list", a2a_access="list")
        await factories.mcp_grant(s, team, server, all_tools=False, tools=["echo"])
        await factories.agent_grant(s, team, agent)
        user = await factories.make_user(s)
        await factories.make_member(s, team, user)
        await factories.make_key(s, team, user)
    policy = (await compile_(factory)).snapshot.api_keys[0].policy
    assert policy.allow_all_mcp is False
    assert [(g.server, g.all_tools, list(g.tools)) for g in policy.mcp_grants] == [
        ("s1", False, ["echo"])
    ]
    assert policy.allow_all_agents is False and list(policy.allowed_agents) == ["a1"]


async def test_the_default_is_a_universal_mcp_grant(factory) -> None:
    await seed_scope(factory)
    await seed_key(factory)
    async with factory() as s, s.begin():
        await factories.make_mcp_server(s, "s1")
        await factories.make_agent(s, "a1")
    policy = (await compile_(factory)).snapshot.api_keys[0].policy
    assert policy.allow_all_mcp is True and list(policy.mcp_grants) == []
    assert policy.allow_all_agents is True and list(policy.allowed_agents) == []


def test_delta_carries_mcp_servers_and_agents_by_name() -> None:
    old = pb.Snapshot(
        mcp_servers=[pb.McpServer(name="s1", url="http://s1.test/mcp")],
        a2a_agents=[pb.A2aAgent(name="a1", url="http://a1.test/a2a")],
    )
    new = pb.Snapshot(
        mcp_servers=[
            pb.McpServer(name="s1", url="http://s1.test/mcp", review_mode=pb.MCP_REVIEW_MODE_OFF),
            pb.McpServer(name="s2", url="http://s2.test/mcp"),
        ],
        a2a_agents=[pb.A2aAgent(name="a2", url="http://a2.test/a2a")],
    )
    delta = diff(old, new)
    assert [server.name for server in delta.upsert_mcp_servers] == ["s1", "s2"]
    assert list(delta.remove_mcp_servers) == []
    assert [agent.name for agent in delta.upsert_a2a_agents] == ["a2"]
    assert list(delta.remove_a2a_agents) == ["a1"]
    old.mcp_servers.append(pb.McpServer(name="s2", url="http://s2.test/mcp"))
    delta = diff(old, new)
    assert list(delta.remove_a2a_agents) == ["a1"]
    assert [server.name for server in delta.upsert_mcp_servers] == ["s1"]


async def test_a_non_live_server_is_archived_by_a_rollback_and_definitions_stay(
    factory,
) -> None:
    """Rollback restores MCP servers by id; a server created later is archived, and the
    reviewed definition states are current state, never versioned."""
    await seed_scope(factory)
    async with factory() as s, s.begin():
        await factories.make_mcp_server(
            s, "s1", definitions=[definition("echo", McpDefinitionState.PENDING)]
        )
    await touch(factory)
    assert await publish_pending(factory, SCOPE, require_local_membership=True) == 1

    # v2: a new server, and the definition of s1 approved.
    async with factory() as s, s.begin():
        await factories.make_mcp_server(s, "s2")
        row = await s.scalar(select(McpDefinition))
        row.state = McpDefinitionState.REJECTED
    await touch(factory)
    assert await publish_pending(factory, SCOPE, require_local_membership=True) == 2

    async with factory() as s, s.begin():
        await rollback_routing(s, SCOPE, 1, require_local_membership=True, force=True)
    snap = await load_version(factory, 1)
    assert [server.name for server in snap.mcp_servers] == ["s1"]

    async with factory() as s:
        live = list(
            (
                await s.scalars(
                    select(McpServer).where(McpServer.deleted_at.is_(None)).order_by(McpServer.name)
                )
            ).all()
        )
        assert [server.name for server in live] == ["s1"]
        rows = (await s.scalars(select(McpDefinition))).all()
        assert [(row.name, row.state) for row in rows] == [("echo", McpDefinitionState.REJECTED)]
    # The rolled-back version is what a gateway got, so its definitions are current state.
    rolled = await load_version(factory, 3)
    assert [(d.name, d.state) for d in rolled.mcp_servers[0].definitions] == [
        ("echo", pb.MCP_DEFINITION_STATE_REJECTED)
    ]


async def test_a_rollback_refuses_a_vanished_auth_plugin(factory) -> None:
    """The rollback restores a server naming a plugin no gateway has any more: refused
    unless forced, and the forced save is audited like a router's."""
    await seed_scope(factory)
    async with factory() as s:
        await report_plugins(
            s,
            SCOPE,
            "gw-1",
            {("upstream-auth", "custom"): (AUTH_SCHEMA, None)},
            connected=True,
        )
        await report_plugins(s, SCOPE, "gw-2", {}, connected=True)
    async with factory() as s, s.begin():
        await factories.make_mcp_server(s, "s1", auth_plugin="custom", auth_params={"ttl_ms": 1})
    await touch(factory)
    assert await publish_pending(factory, SCOPE, require_local_membership=True) == 1
    async with factory() as s, s.begin():
        server = await s.scalar(select(McpServer))
        server.auth_plugin, server.auth_params = "none", {}
    await touch(factory)
    assert await publish_pending(factory, SCOPE, require_local_membership=True) == 2
    # A connected gateway that does not have it: refused unless forced.
    with pytest.raises(PluginRejected):
        async with factory() as s, s.begin():
            await rollback_routing(s, SCOPE, 1, require_local_membership=True)
    async with factory() as s, s.begin():
        await rollback_routing(s, SCOPE, 1, require_local_membership=True, force=True)
    async with factory() as s:
        actions = set((await s.scalars(select(AuditLog.action))).all())
    assert "mcp_server.force_save" in actions


async def test_compile_refuses_a_server_the_gateway_would_reject(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        server = await factories.make_mcp_server(s, "s1")
    async with factory() as s, s.begin():
        server = await s.get(McpServer, server.id)
        server.url = "ftp://s1.test/mcp"
    with pytest.raises(CompileError):
        await compile_(factory)


async def test_plugin_timeouts_reach_the_snapshot(factory) -> None:
    """Every WASM plugin use has a timeout: 0 = the gateway's 60 s default."""
    await seed_scope(factory)
    async with factory() as s, s.begin():
        model = await factories.make_model(s, "a")
        await factories.make_router(s, "r", [(model, 1)], plugin_timeout_ms=2500)
        await factories.make_mcp_server(s, "s1", auth_timeout_ms=1500)
        await factories.make_agent(
            s,
            "a1",
            secret_ref="A2A_TOKEN",
            auth_plugin="static_bearer",
            auth_timeout_ms=700,
            middleware_plugin="strip_artifacts",
            middleware_params={"mime": "x/y"},
            middleware_timeout_ms=900,
        )
    snap = (await compile_(factory)).snapshot
    assert snap.routers[0].plugin_timeout_ms == 2500
    assert snap.mcp_servers[0].auth.plugin.timeout_ms == 1500
    agent = snap.a2a_agents[0]
    assert agent.auth.plugin.timeout_ms == 700
    assert agent.middleware.timeout_ms == 900


async def test_a_rollback_restores_the_router_plugin_timeout(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        model = await factories.make_model(s, "a")
        router = await factories.make_router(
            s, "r", [(model, 1)], plugin_timeout_ms=2500, strategy="weighted"
        )
        router_id = router.id
    await touch(factory)
    assert await publish_pending(factory, SCOPE, require_local_membership=True) == 1
    async with factory() as s, s.begin():
        router = await s.get(Router, router_id)
        router.plugin_timeout_ms = 0
    await touch(factory)
    assert await publish_pending(factory, SCOPE, require_local_membership=True) == 2
    assert (await load_version(factory, 2)).routers[0].plugin_timeout_ms == 0
    async with factory() as s, s.begin():
        await rollback_routing(s, SCOPE, 1, require_local_membership=True)
    rolled = await load_version(factory, 3)
    assert rolled.routers[0].plugin_timeout_ms == 2500
    async with factory() as s:
        assert (await s.get(Router, router_id)).plugin_timeout_ms == 2500
