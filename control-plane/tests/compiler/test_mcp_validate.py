"""`validate_snapshot` mirrors targets.rs::validate for MCP servers and A2A agents.

A published snapshot must never be NACKed INVALID_CONFIG, so every rule the gateway applies
to these entities and to a key's MCP/A2A grants has a check here.
"""

import json

import pytest

from lumewarp_control_plane._proto.lumewarp.config.v1 import snapshot_pb2 as pb
from lumewarp_control_plane.compiler.validate import (
    MAX_DEFINITION_NAME,
    SnapshotInvalid,
    capabilities_used,
    validate_snapshot,
)


def _check(snap: pb.Snapshot) -> None:
    del snap.required_capabilities[:]
    snap.required_capabilities.extend(capabilities_used(snap))
    validate_snapshot(snap, max_bytes=1 << 26, max_keys=1000)


def _auth(plugin: str = "none", params: str = "", **kwargs) -> pb.UpstreamAuth:
    return pb.UpstreamAuth(plugin=pb.PluginRef(plugin=plugin, params_json=params), **kwargs)


def _server(name: str = "s1", **kwargs) -> pb.McpServer:
    return pb.McpServer(
        id=f"id-{name}",
        name=name,
        url=kwargs.pop("url", "http://s1.test/mcp"),
        auth=kwargs.pop("auth", _auth()),
        **kwargs,
    )


def _agent(name: str = "a1", **kwargs) -> pb.A2aAgent:
    return pb.A2aAgent(
        id=f"id-{name}",
        name=name,
        url=kwargs.pop("url", "http://a1.test/a2a"),
        auth=kwargs.pop("auth", _auth()),
        **kwargs,
    )


def _definition(
    name: str = "echo",
    *,
    kind: int = pb.MCP_DEFINITION_KIND_TOOL,
    state: int = pb.MCP_DEFINITION_STATE_APPROVED,
    digest: bytes = b"0" * 32,
) -> pb.McpDefinition:
    return pb.McpDefinition(kind=kind, name=name, hash=digest, state=state)


def _key(policy: pb.EffectivePolicy | None = None) -> pb.ApiKey:
    return pb.ApiKey(key_id="k1", key_hash=b"1" * 32, status=pb.KEY_STATUS_ACTIVE, policy=policy)


def _base(*, servers=(), agents=(), keys=()) -> pb.Snapshot:
    return pb.Snapshot(mcp_servers=list(servers), a2a_agents=list(agents), api_keys=list(keys))


def test_a_minimal_snapshot_with_targets_is_valid() -> None:
    snap = _base(
        servers=[_server(definitions=[_definition()])],
        agents=[_agent(middleware=pb.PluginRef(plugin="strip_artifacts", params_json=""))],
        keys=[_key(pb.EffectivePolicy(allow_all_mcp=True, allow_all_agents=True))],
    )
    _check(snap)
    assert list(snap.required_capabilities) == [
        "a2a",
        "a2a_middleware:strip_artifacts",
        "auth:none",
        "mcp",
    ]


@pytest.mark.parametrize(
    "server",
    [
        _server(name="Uppercase"),
        _server(name="has__separator"),
        _server(url="ftp://s1.test/mcp"),
        _server(url="http://user:pw@s1.test/mcp"),
        _server(auth=_auth("none", '{"a":1}')),
        _server(auth=_auth("static_bearer")),
        _server(auth=_auth("none", secret_ref="TOKEN")),
        _server(auth=_auth("hmac_signature", secret_ref="TOKEN")),
        _server(definitions=[_definition(kind=pb.MCP_DEFINITION_KIND_UNSPECIFIED)]),
        _server(definitions=[_definition(state=99)]),
        _server(definitions=[_definition(name="")]),
        _server(definitions=[_definition(name="x" * (MAX_DEFINITION_NAME + 1))]),
        _server(definitions=[_definition(digest=b"0" * 31)]),
        _server(definitions=[_definition(), _definition()]),
        _server(review_mode=99),
    ],
)
def test_an_invalid_mcp_server_is_rejected(server: pb.McpServer) -> None:
    with pytest.raises(SnapshotInvalid):
        _check(_base(servers=[server]))


@pytest.mark.parametrize(
    "agent",
    [
        _agent(name="a__b"),
        _agent(url="not a url"),
        _agent(card_url="ftp://a.test/card"),
        _agent(middleware=pb.PluginRef(plugin="Bad-Name")),
        _agent(middleware=pb.PluginRef(plugin="x", params_json="[]")),
        _agent(card_json="not json"),
        _agent(card_json="[]"),
        _agent(card_json='{"a": "' + "x" * (64 * 1024) + '"}'),
    ],
)
def test_an_invalid_a2a_agent_is_rejected(agent: pb.A2aAgent) -> None:
    with pytest.raises(SnapshotInvalid):
        _check(_base(agents=[agent]))


def test_duplicate_names_are_rejected() -> None:
    with pytest.raises(SnapshotInvalid):
        _check(_base(servers=[_server("s1"), _server("s1")]))
    with pytest.raises(SnapshotInvalid):
        _check(_base(agents=[_agent("a1"), _agent("a1")]))


def test_a_key_may_only_grant_servers_and_agents_of_the_snapshot() -> None:
    policy = pb.EffectivePolicy(
        mcp_grants=[pb.McpGrant(server="ghost", all_tools=True)], allow_all_agents=True
    )
    with pytest.raises(SnapshotInvalid):
        _check(_base(servers=[_server()], keys=[_key(policy)]))
    policy = pb.EffectivePolicy(allow_all_mcp=True, allowed_agents=["ghost"])
    with pytest.raises(SnapshotInvalid):
        _check(_base(agents=[_agent()], keys=[_key(policy)]))
    policy = pb.EffectivePolicy(
        mcp_grants=[pb.McpGrant(server="s1", all_tools=True, tools=["echo"])],
        allow_all_agents=True,
    )
    with pytest.raises(SnapshotInvalid):
        _check(_base(servers=[_server()], keys=[_key(policy)]))
    policy = pb.EffectivePolicy(
        mcp_grants=[pb.McpGrant(server="s1"), pb.McpGrant(server="s1")],
        allow_all_agents=True,
    )
    with pytest.raises(SnapshotInvalid):
        _check(_base(servers=[_server()], keys=[_key(policy)]))


def test_a_granted_server_with_tools_is_valid() -> None:
    policy = pb.EffectivePolicy(
        mcp_grants=[pb.McpGrant(server="s1", tools=["echo"])],
        allowed_agents=["a1"],
    )
    _check(_base(servers=[_server()], agents=[_agent()], keys=[_key(policy)]))


def test_capabilities_are_only_listed_for_entities_that_exist() -> None:
    assert capabilities_used(pb.Snapshot()) == []
    snap = pb.Snapshot(mcp_servers=[_server(auth=_auth("passthrough"))])
    assert capabilities_used(snap) == ["auth:passthrough", "mcp"]
    snap = pb.Snapshot(a2a_agents=[_agent(auth=_auth("static_bearer", secret_ref="T"))])
    assert capabilities_used(snap) == ["a2a", "auth:static_bearer"]


def test_a_plugin_capability_must_name_a_valid_plugin() -> None:
    snap = _base(servers=[_server()])
    snap.required_capabilities.extend(["mcp", "auth:none", "auth:Bad-Name"])
    with pytest.raises(SnapshotInvalid):
        validate_snapshot(snap, max_bytes=1 << 26, max_keys=1000)


def test_a_card_is_a_json_object_at_most_64_kib() -> None:
    card = json.dumps({"name": "agent", "capabilities": {"streaming": True}})
    _check(_base(agents=[_agent(card_json=card)]))
