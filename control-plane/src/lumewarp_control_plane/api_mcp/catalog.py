"""The MCP tool table: which REST operations the `/mcp` endpoint exposes, and as what.

Every operation of the REST API (`/api/v1`) is either behind exactly one tool here or listed
in `NOT_EXPOSED` with the reason; `tests/api/test_api_mcp.py` fails when a new operation is
in neither. Tools are reads, plus four writes that only touch the caller's own keys
(`*_my_key`, wrappers with an owner check, see `server.py`). No administrator write is
reachable over MCP.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum


class MinRole(IntEnum):
    """The least role REST needs for a tool; a higher role implies the lower ones."""

    USER = 0
    TEAM_ADMIN = 1
    ORG_ADMIN = 2
    INSTALLATION_ADMIN = 3


class Kind(StrEnum):
    READ = "read"
    WRITE = "write"


class Wrapper(StrEnum):
    """How a `*_my_key` tool differs from the raw operation."""

    CREATE = "create"  # no service-account owner: always the caller's own key
    UPDATE = "update"  # getKey first; refuse unless the caller owns it
    REVOKE = "revoke"  # owner check + confirm
    ROTATE = "rotate"  # owner check + confirm


TOOLSETS = ("me", "keys", "usage", "incidents", "tenancy")


@dataclass(frozen=True)
class ToolSpec:
    name: str
    operation_id: str
    toolset: str
    min_role: MinRole = MinRole.USER
    kind: Kind = Kind.READ
    wrapper: Wrapper | None = None
    description: str | None = None

    @property
    def destructive(self) -> bool:
        return self.wrapper in (Wrapper.REVOKE, Wrapper.ROTATE)

    @property
    def secret_result(self) -> bool:
        """The result carries a one-time key secret (masked unless `reveal_secret`)."""
        return self.wrapper in (Wrapper.CREATE, Wrapper.ROTATE)


_U, _T, _O, _I = (
    MinRole.USER,
    MinRole.TEAM_ADMIN,
    MinRole.ORG_ADMIN,
    MinRole.INSTALLATION_ADMIN,
)

TOOLS: tuple[ToolSpec, ...] = (
    # me
    ToolSpec("get_current_user", "getCurrentUser", "me"),
    ToolSpec("list_zones", "listZones", "me"),
    ToolSpec("list_catalog", "listCatalog", "me"),
    ToolSpec("list_teams", "listTeams", "me"),
    ToolSpec("get_team", "getTeam", "me"),
    # keys
    ToolSpec("list_own_keys", "listOwnKeys", "keys"),
    ToolSpec("get_key", "getKey", "keys"),
    ToolSpec(
        "create_my_key",
        "createKey",
        "keys",
        kind=Kind.WRITE,
        wrapper=Wrapper.CREATE,
        description=(
            "Create a virtual API key (`lw-…`) owned by you in a team you belong to, under the "
            "team's member rules. The new secret is masked in the result unless "
            "`reveal_secret` is true; it is never shown again."
        ),
    ),
    ToolSpec(
        "update_my_key",
        "updateKey",
        "keys",
        kind=Kind.WRITE,
        wrapper=Wrapper.UPDATE,
        description=(
            "Change one of your own keys: name, expiry, limits, models, or suspend/resume it. "
            "Omitted fields are unchanged. Refused for a key you do not own."
        ),
    ),
    ToolSpec(
        "revoke_my_key",
        "revokeKey",
        "keys",
        kind=Kind.WRITE,
        wrapper=Wrapper.REVOKE,
        description=(
            "Revoke one of your own keys permanently, including a previous secret still in its "
            "rotation grace. Clients using it start failing at once. Requires `confirm: true`."
        ),
    ),
    ToolSpec(
        "rotate_my_key",
        "rotateKey",
        "keys",
        kind=Kind.WRITE,
        wrapper=Wrapper.ROTATE,
        description=(
            "Replace the secret of one of your own keys; the old secret stops working after "
            "`grace_seconds` (0 = at once). The new secret is masked unless `reveal_secret` is "
            "true. Requires `confirm: true`."
        ),
    ),
    # usage
    ToolSpec("get_usage", "getUsage", "usage"),
    ToolSpec("get_usage_leaderboard", "getUsageLeaderboard", "usage"),
    ToolSpec("list_requests", "listRequests", "usage"),
    # incidents
    ToolSpec("get_status", "getStatus", "incidents"),
    ToolSpec("list_incidents", "listIncidents", "incidents", _I),
    ToolSpec("get_incident", "getIncident", "incidents", _I),
    # tenancy (administrators, reads only)
    ToolSpec("list_team_keys", "listTeamKeys", "tenancy", _T),
    ToolSpec("list_members", "listMembers", "tenancy", _T),
    ToolSpec("list_service_accounts", "listServiceAccounts", "tenancy", _T),
    ToolSpec("get_service_account", "getServiceAccount", "tenancy", _T),
    ToolSpec("list_orgs", "listOrgs", "tenancy", _O),
    ToolSpec("get_org", "getOrg", "tenancy", _O),
    ToolSpec("get_org_usage", "getOrgUsage", "tenancy", _O),
    ToolSpec("list_org_admins", "listOrgAdmins", "tenancy", _I),
    ToolSpec("list_users", "listUsers", "tenancy", _I),
    ToolSpec("get_user", "getUser", "tenancy", _I),
)

TOOLS_BY_NAME = {tool.name: tool for tool in TOOLS}


class Reason(StrEnum):
    ADMIN_WRITE = "admin_write"  # every change an administrator makes stays in the UI/REST
    ADMIN_READ = "admin_read"  # admin reads outside tenancy and incidents
    PROBE = "probe"  # Test now, upstream model listing, MCP discovery: outbound calls
    TOKEN = "token"  # personal access tokens: an MCP client must not mint its own credential
    SESSION = "session"  # browser login/logout
    PLAYGROUND = "playground"  # browser Playground proxy
    WEBHOOK = "webhook"  # Alertmanager receiver, not a user operation


_NOT_EXPOSED_BY_REASON: dict[Reason, tuple[str, ...]] = {
    Reason.ADMIN_WRITE: (
        "rollbackConfig",
        "deleteGateway",
        "transferProxyAdmin",
        "putInstallationAdmin",
        "deleteInstallationAdmin",
        "createA2aAgent",
        "updateA2aAgent",
        "deleteA2aAgent",
        "createMcpServer",
        "updateMcpServer",
        "deleteMcpServer",
        "approveMcpDefinition",
        "rejectMcpDefinition",
        "createMember",
        "putMember",
        "updateMember",
        "deleteMember",
        "createModelGroup",
        "updateModelGroup",
        "deleteModelGroup",
        "createOrg",
        "updateOrg",
        "deleteOrg",
        "putOrgAdmin",
        "deleteOrgAdmin",
        "createAlias",
        "updateAlias",
        "deleteAlias",
        "createModel",
        "updateModel",
        "deleteModel",
        "putModelDeployment",
        "deleteModelDeployment",
        "createRouter",
        "updateRouter",
        "deleteRouter",
        "createUpstream",
        "updateUpstream",
        "deleteUpstream",
        "createServiceAccount",
        "updateServiceAccount",
        "deleteServiceAccount",
        "createTeam",
        "updateTeam",
        "deleteTeam",
        "createUser",
        "updateUser",
        "deleteUser",
        "updateZone",
        "archiveZone",
        "putZoneLimits",
        "putZoneMemberDefaults",
    ),
    Reason.PROBE: (
        "discoverMcp",
        "testModel",
        "testSavedModel",
        "testDeployment",
        "testUpstream",
        "testSavedUpstream",
        "listUpstreamModels",
    ),
    Reason.ADMIN_READ: (
        "listAudit",
        "listConfigVersions",
        "listGateways",
        "listInstallationAdmins",
        "listA2aAgents",
        "getA2aAgent",
        "listMcpServers",
        "getMcpServer",
        "listMcpDefinitions",
        "listModelGroups",
        "getModelGroup",
        "listAliases",
        "getAlias",
        "listModels",
        "getModel",
        "listRouters",
        "getRouter",
        "listUpstreams",
        "getUpstream",
        "listPlugins",
    ),
    Reason.TOKEN: ("listOwnTokens", "createOwnToken", "revokeOwnToken", "createUserToken"),
    Reason.SESSION: (
        "getSession",
        "loginBreakGlass",
        "logoutSession",
        "startOidcLogin",
        "completeOidcLogin",
    ),
    Reason.PLAYGROUND: ("getPlayground", "playgroundChatCompletions"),
    Reason.WEBHOOK: ("receiveAlertmanagerWebhook",),
}

NOT_EXPOSED: dict[str, Reason] = {
    operation_id: reason
    for reason, operation_ids in _NOT_EXPOSED_BY_REASON.items()
    for operation_id in operation_ids
}

EXPOSED_OPERATIONS = frozenset(tool.operation_id for tool in TOOLS)
