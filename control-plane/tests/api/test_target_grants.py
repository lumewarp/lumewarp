"""MCP/A2A grants: stored like model grants, intersected org -> team -> owner -> key."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane.compiler.compile import compile_scope
from lumewarp_control_plane.mcp import hashing
from lumewarp_control_plane.models import McpDefinitionKind, MemberPolicy
from tests.api.helpers import ADMIN_AUTH, auth, suffix, user_with_token
from tests.factories import (
    agent_grant,
    make_agent,
    make_key,
    make_mcp_server,
    make_member,
    make_org,
    make_team,
    make_user,
    mcp_grant,
    new_definition,
)


def unique(prefix: str) -> str:
    """A server/agent name unique in the shared API test database."""
    return f"{prefix}-{suffix()}"


def observed(name: str, kind: McpDefinitionKind = McpDefinitionKind.TOOL) -> hashing.Observed:
    value = hashing.observe(kind, {"name": name, "description": f"{name} tool"})
    assert value is not None
    return value


async def server_with_tools(session, name: str, *tools: str):
    definitions = [new_definition(McpDefinitionKind.TOOL, observed(tool)) for tool in tools]
    return await make_mcp_server(session, name, definitions=definitions)


async def compiled(api_engine, api_scope: str):
    async with AsyncSession(api_engine) as session:
        return await compile_scope(session, api_scope, require_local_membership=False)


def key_policy(compiled_snapshot, key) -> object:
    """This key's resolved policy in a compile of the whole shared test scope."""
    return next(
        item.policy for item in compiled_snapshot.snapshot.api_keys if item.key_id == str(key.id)
    )


async def test_grants_intersect_down_the_hierarchy(committed, api_engine, api_scope) -> None:
    s1, a1 = unique("s1"), unique("a1")
    server = await server_with_tools(committed, s1, "echo", "add")
    agent = await make_agent(committed, a1)
    org = await make_org(committed, mcp_access="list", a2a_access="list")
    await mcp_grant(committed, org, server, all_tools=True)
    await agent_grant(committed, org, agent)
    team = await make_team(committed, org=org, mcp_access="list", a2a_access="list")
    await mcp_grant(committed, team, server, all_tools=False, tools=["echo"])
    await agent_grant(committed, team, agent)
    user = await make_user(committed)
    await make_member(committed, team, user, mcp_access="list", a2a_access="list")
    member_policy = await committed.scalar(
        select(MemberPolicy).where(MemberPolicy.team_id == team.id, MemberPolicy.user_id == user.id)
    )
    # The member also names "add"; the team's narrower list wins.
    await mcp_grant(committed, member_policy, server, all_tools=False, tools=["echo", "add"])
    await agent_grant(committed, member_policy, agent)
    key = await make_key(committed, team, user)
    await committed.commit()

    snap = await compiled(api_engine, api_scope)
    policy = key_policy(snap, key)
    assert policy.allow_all_mcp is False
    assert [(g.server, g.all_tools, list(g.tools)) for g in policy.mcp_grants] == [
        (s1, False, ["echo"])
    ]
    assert policy.allow_all_agents is False
    assert list(policy.allowed_agents) == [a1]
    # `auth:<plugin>` is emitted for every server and agent, built-in `none` included.
    assert {"a2a", "auth:none", "mcp"} <= set(snap.snapshot.required_capabilities)


async def test_a_universal_level_grants_everything_it_contains(
    committed, api_engine, api_scope
) -> None:
    s1, s2, a1 = unique("s1"), unique("s2"), unique("a1")
    first = await server_with_tools(committed, s1, "echo")
    second = await server_with_tools(committed, s2, "add")
    agent = await make_agent(committed, a1)
    org = await make_org(committed, mcp_access="list", a2a_access="all")
    await mcp_grant(committed, org, first, all_tools=True)
    await mcp_grant(committed, org, second, all_tools=False, tools=["add"])
    team = await make_team(committed, org=org)  # ALL: keeps the org's set
    user = await make_user(committed)
    await make_member(committed, team, user)  # INHERIT: keeps the team's set
    key = await make_key(committed, team, user)
    await committed.commit()

    snap = await compiled(api_engine, api_scope)
    policy = key_policy(snap, key)
    assert policy.allow_all_mcp is False
    assert [(g.server, g.all_tools, list(g.tools)) for g in policy.mcp_grants] == [
        (s1, True, []),
        (s2, False, ["add"]),
    ]
    assert policy.allow_all_agents is True and list(policy.allowed_agents) == []
    # The agent exists but no level restricts it: universal.
    assert a1 in [a.name for a in snap.snapshot.a2a_agents]
    del agent


async def test_a_deny_all_level_grants_no_server(committed, api_engine, api_scope) -> None:
    await server_with_tools(committed, unique("s1"), "echo")
    org = await make_org(committed)
    team = await make_team(committed, org=org, mcp_access="list", a2a_access="list")
    user = await make_user(committed)
    await make_member(committed, team, user)
    key = await make_key(committed, team, user)
    await committed.commit()
    policy = key_policy(await compiled(api_engine, api_scope), key)
    assert policy.allow_all_mcp is False and list(policy.mcp_grants) == []
    assert policy.allow_all_agents is False and list(policy.allowed_agents) == []


async def test_an_archived_server_is_left_out_and_its_grant_dropped(
    committed, api_engine, api_scope
) -> None:
    s1, s2 = unique("s1"), unique("s2")
    live = await server_with_tools(committed, s1, "echo")
    gone = await server_with_tools(committed, s2, "add")
    org = await make_org(committed, mcp_access="list")
    await mcp_grant(committed, org, live, all_tools=True)
    await mcp_grant(committed, org, gone, all_tools=True)
    team = await make_team(committed, org=org)
    user = await make_user(committed)
    await make_member(committed, team, user)
    key = await make_key(committed, team, user)
    gone.deleted_at = datetime.now(UTC)
    await committed.commit()

    compiled_snapshot = await compiled(api_engine, api_scope)
    snap = compiled_snapshot.snapshot
    assert s1 in [s.name for s in snap.mcp_servers]
    assert s2 not in [s.name for s in snap.mcp_servers]
    assert [g.server for g in key_policy(compiled_snapshot, key).mcp_grants] == [s1]


async def test_team_grants_are_written_and_narrowed_through_the_api(client, committed) -> None:
    s1, s2, a1 = unique("s1"), unique("s2"), unique("a1")
    server = await server_with_tools(committed, s1, "echo", "add")
    other = await server_with_tools(committed, s2, "sum")
    agent = await make_agent(committed, a1)
    org = await make_org(committed, mcp_access="list", a2a_access="list")
    await mcp_grant(committed, org, server, all_tools=False, tools=["echo"])
    await agent_grant(committed, org, agent)
    team = await make_team(committed, org=org)
    await committed.commit()

    response = client.patch(
        f"/api/v1/teams/{team.id}",
        json={
            "mcp_access": "list",
            "mcp_servers": [{"server": s1, "tools": ["echo"]}],
            "a2a_access": "list",
            "a2a_agents": [a1],
        },
        headers=ADMIN_AUTH,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mcp_servers"] == [{"server": s1, "all_tools": False, "tools": ["echo"]}]
    assert body["mcp_access"] == "list"
    assert body["a2a_agents"] == [a1] and body["a2a_access"] == "list"

    # Widening past the org: another tool of the same server, and another server.
    response = client.patch(
        f"/api/v1/teams/{team.id}",
        json={"mcp_servers": [{"server": s1, "tools": ["echo", "add"]}]},
        headers=ADMIN_AUTH,
    )
    assert response.status_code == 422
    assert "mcp_servers" in response.json()["error"]["message"]
    response = client.patch(
        f"/api/v1/teams/{team.id}",
        json={"mcp_servers": [{"server": s2, "all_tools": True}]},
        headers=ADMIN_AUTH,
    )
    assert response.status_code == 422
    assert s2 in response.json()["error"]["message"]
    # An all_tools grant where the parent restricts the tools must name them.
    await make_team(committed, org=org)
    response = client.patch(
        f"/api/v1/teams/{team.id}",
        json={"mcp_servers": [{"server": s1, "all_tools": True}]},
        headers=ADMIN_AUTH,
    )
    assert response.status_code == 422
    assert "restricted" in response.json()["error"]["message"]

    # Unknown servers, unknown tools and unknown agents are 422s (in an unrestricted team,
    # so the name check, not the narrowing check, is what answers).
    loose = await make_team(committed, org=await make_org(committed))
    await committed.commit()
    unknown = client.patch(
        f"/api/v1/teams/{loose.id}",
        json={"mcp_access": "list", "mcp_servers": [{"server": "ghost", "all_tools": True}]},
        headers=ADMIN_AUTH,
    )
    assert unknown.status_code == 422 and "unknown servers" in unknown.json()["error"]["message"]
    unknown = client.patch(
        f"/api/v1/teams/{loose.id}",
        json={"mcp_access": "list", "mcp_servers": [{"server": s1, "tools": ["nope"]}]},
        headers=ADMIN_AUTH,
    )
    assert unknown.status_code == 422 and "unknown tools" in unknown.json()["error"]["message"]
    unknown = client.patch(
        f"/api/v1/teams/{loose.id}",
        json={"a2a_access": "list", "a2a_agents": ["ghost"]},
        headers=ADMIN_AUTH,
    )
    assert unknown.status_code == 422 and "unknown agents" in unknown.json()["error"]["message"]
    del other


async def test_a_service_account_and_member_read_their_grants(client, committed) -> None:
    s1, a1 = unique("s1"), unique("a1")
    server = await server_with_tools(committed, s1, "echo")
    agent = await make_agent(committed, a1)
    org = await make_org(committed, mcp_access="list", a2a_access="list")
    await mcp_grant(committed, org, server, all_tools=True)
    await agent_grant(committed, org, agent)
    team = await make_team(committed, org=org, mcp_access="list", a2a_access="list")
    await mcp_grant(committed, team, server, all_tools=False, tools=["echo"])
    await agent_grant(committed, team, agent)
    await committed.commit()
    created = client.post(
        f"/api/v1/teams/{team.id}/service-accounts",
        json={"name": f"sa-{suffix()}", "mcp_access": "list", "a2a_access": "list"},
        headers=ADMIN_AUTH,
    )
    assert created.status_code == 201, created.text
    assert created.json()["mcp_servers"] == []
    # The service account may not name a tool the team lacks.
    bad = client.patch(
        f"/api/v1/service-accounts/{created.json()['id']}",
        json={"mcp_servers": [{"server": s1, "tools": ["nope"]}]},
        headers=ADMIN_AUTH,
    )
    assert bad.status_code == 422
    ok = client.patch(
        f"/api/v1/service-accounts/{created.json()['id']}",
        json={"mcp_servers": [{"server": s1, "tools": ["echo"]}]},
        headers=ADMIN_AUTH,
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["mcp_servers"][0]["tools"] == ["echo"]

    # /users/me resolves a member's effective MCP and A2A access.
    user, token = await user_with_token(committed, email=f"me-{suffix()}@example.com")
    await make_member(committed, team, user, mcp_access="list", a2a_access="list")
    member_policy = await committed.scalar(
        select(MemberPolicy).where(MemberPolicy.team_id == team.id, MemberPolicy.user_id == user.id)
    )
    await mcp_grant(committed, member_policy, server, all_tools=False, tools=["echo"])
    await agent_grant(committed, member_policy, agent)
    await committed.commit()

    me = client.get("/api/v1/users/me", headers=auth(token))
    assert me.status_code == 200, me.text
    entry = next(item for item in me.json()["teams"] if item["team_name"] == team.name)
    assert entry["all_mcp"] is False
    assert entry["mcp_servers"] == [{"server": s1, "all_tools": False, "tools": ["echo"]}]
    assert entry["all_agents"] is False and entry["agents"] == [a1]
    del agent
