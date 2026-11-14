from lumewarp_control_plane._proto.lumewarp.config.v1 import snapshot_pb2 as _snapshot_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class PluginKind(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    PLUGIN_KIND_UNSPECIFIED: _ClassVar[PluginKind]
    PLUGIN_KIND_ROUTER_STRATEGY: _ClassVar[PluginKind]
    PLUGIN_KIND_UPSTREAM_AUTH: _ClassVar[PluginKind]
    PLUGIN_KIND_A2A_MIDDLEWARE: _ClassVar[PluginKind]

class NackCode(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    NACK_CODE_UNSPECIFIED: _ClassVar[NackCode]
    NACK_CODE_BASE_MISMATCH: _ClassVar[NackCode]
    NACK_CODE_INVALID_CONFIG: _ClassVar[NackCode]
    NACK_CODE_UNSUPPORTED_FEATURE: _ClassVar[NackCode]
    NACK_CODE_PERSIST_FAILED: _ClassVar[NackCode]
PLUGIN_KIND_UNSPECIFIED: PluginKind
PLUGIN_KIND_ROUTER_STRATEGY: PluginKind
PLUGIN_KIND_UPSTREAM_AUTH: PluginKind
PLUGIN_KIND_A2A_MIDDLEWARE: PluginKind
NACK_CODE_UNSPECIFIED: NackCode
NACK_CODE_BASE_MISMATCH: NackCode
NACK_CODE_INVALID_CONFIG: NackCode
NACK_CODE_UNSUPPORTED_FEATURE: NackCode
NACK_CODE_PERSIST_FAILED: NackCode

class GatewayMessage(_message.Message):
    __slots__ = ("hello", "ack", "nack", "health", "catalog")
    HELLO_FIELD_NUMBER: _ClassVar[int]
    ACK_FIELD_NUMBER: _ClassVar[int]
    NACK_FIELD_NUMBER: _ClassVar[int]
    HEALTH_FIELD_NUMBER: _ClassVar[int]
    CATALOG_FIELD_NUMBER: _ClassVar[int]
    hello: Hello
    ack: Ack
    nack: Nack
    health: HealthReport
    catalog: CatalogReport
    def __init__(self, hello: _Optional[_Union[Hello, _Mapping]] = ..., ack: _Optional[_Union[Ack, _Mapping]] = ..., nack: _Optional[_Union[Nack, _Mapping]] = ..., health: _Optional[_Union[HealthReport, _Mapping]] = ..., catalog: _Optional[_Union[CatalogReport, _Mapping]] = ...) -> None: ...

class HealthReport(_message.Message):
    __slots__ = ("upstreams", "models")
    UPSTREAMS_FIELD_NUMBER: _ClassVar[int]
    MODELS_FIELD_NUMBER: _ClassVar[int]
    upstreams: _containers.RepeatedCompositeFieldContainer[TargetHealth]
    models: _containers.RepeatedCompositeFieldContainer[TargetHealth]
    def __init__(self, upstreams: _Optional[_Iterable[_Union[TargetHealth, _Mapping]]] = ..., models: _Optional[_Iterable[_Union[TargetHealth, _Mapping]]] = ...) -> None: ...

class TargetHealth(_message.Message):
    __slots__ = ("name", "healthy")
    NAME_FIELD_NUMBER: _ClassVar[int]
    HEALTHY_FIELD_NUMBER: _ClassVar[int]
    name: str
    healthy: bool
    def __init__(self, name: _Optional[str] = ..., healthy: _Optional[bool] = ...) -> None: ...

class Hello(_message.Message):
    __slots__ = ("gateway_id", "have_version", "gateway_version", "supported_capabilities", "scope", "have_digest", "plugins", "plugin_abi", "shared_limits")
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    HAVE_VERSION_FIELD_NUMBER: _ClassVar[int]
    GATEWAY_VERSION_FIELD_NUMBER: _ClassVar[int]
    SUPPORTED_CAPABILITIES_FIELD_NUMBER: _ClassVar[int]
    SCOPE_FIELD_NUMBER: _ClassVar[int]
    HAVE_DIGEST_FIELD_NUMBER: _ClassVar[int]
    PLUGINS_FIELD_NUMBER: _ClassVar[int]
    PLUGIN_ABI_FIELD_NUMBER: _ClassVar[int]
    SHARED_LIMITS_FIELD_NUMBER: _ClassVar[int]
    gateway_id: str
    have_version: int
    gateway_version: str
    supported_capabilities: _containers.RepeatedScalarFieldContainer[str]
    scope: str
    have_digest: bytes
    plugins: _containers.RepeatedCompositeFieldContainer[PluginDescriptor]
    plugin_abi: int
    shared_limits: SharedLimits
    def __init__(self, gateway_id: _Optional[str] = ..., have_version: _Optional[int] = ..., gateway_version: _Optional[str] = ..., supported_capabilities: _Optional[_Iterable[str]] = ..., scope: _Optional[str] = ..., have_digest: _Optional[bytes] = ..., plugins: _Optional[_Iterable[_Union[PluginDescriptor, _Mapping]]] = ..., plugin_abi: _Optional[int] = ..., shared_limits: _Optional[_Union[SharedLimits, _Mapping]] = ...) -> None: ...

class SharedLimits(_message.Message):
    __slots__ = ("enabled", "namespace")
    ENABLED_FIELD_NUMBER: _ClassVar[int]
    NAMESPACE_FIELD_NUMBER: _ClassVar[int]
    enabled: bool
    namespace: str
    def __init__(self, enabled: _Optional[bool] = ..., namespace: _Optional[str] = ...) -> None: ...

class PluginDescriptor(_message.Message):
    __slots__ = ("kind", "name", "title", "description", "builtin", "params_schema_json", "router", "wasm_sha256", "digest")
    KIND_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    BUILTIN_FIELD_NUMBER: _ClassVar[int]
    PARAMS_SCHEMA_JSON_FIELD_NUMBER: _ClassVar[int]
    ROUTER_FIELD_NUMBER: _ClassVar[int]
    WASM_SHA256_FIELD_NUMBER: _ClassVar[int]
    DIGEST_FIELD_NUMBER: _ClassVar[int]
    kind: PluginKind
    name: str
    title: str
    description: str
    builtin: bool
    params_schema_json: str
    router: RouterStrategyInfo
    wasm_sha256: str
    digest: str
    def __init__(self, kind: _Optional[_Union[PluginKind, str]] = ..., name: _Optional[str] = ..., title: _Optional[str] = ..., description: _Optional[str] = ..., builtin: _Optional[bool] = ..., params_schema_json: _Optional[str] = ..., router: _Optional[_Union[RouterStrategyInfo, _Mapping]] = ..., wasm_sha256: _Optional[str] = ..., digest: _Optional[str] = ...) -> None: ...

class RouterStrategyInfo(_message.Message):
    __slots__ = ("uses_weight", "needs", "member_schema_json")
    USES_WEIGHT_FIELD_NUMBER: _ClassVar[int]
    NEEDS_FIELD_NUMBER: _ClassVar[int]
    MEMBER_SCHEMA_JSON_FIELD_NUMBER: _ClassVar[int]
    uses_weight: bool
    needs: _containers.RepeatedScalarFieldContainer[str]
    member_schema_json: str
    def __init__(self, uses_weight: _Optional[bool] = ..., needs: _Optional[_Iterable[str]] = ..., member_schema_json: _Optional[str] = ...) -> None: ...

class CatalogReport(_message.Message):
    __slots__ = ("definitions", "cards")
    DEFINITIONS_FIELD_NUMBER: _ClassVar[int]
    CARDS_FIELD_NUMBER: _ClassVar[int]
    definitions: _containers.RepeatedCompositeFieldContainer[ObservedDefinition]
    cards: _containers.RepeatedCompositeFieldContainer[AgentCardReport]
    def __init__(self, definitions: _Optional[_Iterable[_Union[ObservedDefinition, _Mapping]]] = ..., cards: _Optional[_Iterable[_Union[AgentCardReport, _Mapping]]] = ...) -> None: ...

class ObservedDefinition(_message.Message):
    __slots__ = ("server", "kind", "name", "hash", "definition_json", "auto_approved", "observed_at_unix_ms", "server_id", "config_version")
    SERVER_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    HASH_FIELD_NUMBER: _ClassVar[int]
    DEFINITION_JSON_FIELD_NUMBER: _ClassVar[int]
    AUTO_APPROVED_FIELD_NUMBER: _ClassVar[int]
    OBSERVED_AT_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    SERVER_ID_FIELD_NUMBER: _ClassVar[int]
    CONFIG_VERSION_FIELD_NUMBER: _ClassVar[int]
    server: str
    kind: _snapshot_pb2.McpDefinitionKind
    name: str
    hash: bytes
    definition_json: str
    auto_approved: bool
    observed_at_unix_ms: int
    server_id: str
    config_version: int
    def __init__(self, server: _Optional[str] = ..., kind: _Optional[_Union[_snapshot_pb2.McpDefinitionKind, str]] = ..., name: _Optional[str] = ..., hash: _Optional[bytes] = ..., definition_json: _Optional[str] = ..., auto_approved: _Optional[bool] = ..., observed_at_unix_ms: _Optional[int] = ..., server_id: _Optional[str] = ..., config_version: _Optional[int] = ...) -> None: ...

class AgentCardReport(_message.Message):
    __slots__ = ("agent", "card_json", "error", "fetched_at_unix_ms", "agent_id", "config_version")
    AGENT_FIELD_NUMBER: _ClassVar[int]
    CARD_JSON_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    FETCHED_AT_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    CONFIG_VERSION_FIELD_NUMBER: _ClassVar[int]
    agent: str
    card_json: str
    error: str
    fetched_at_unix_ms: int
    agent_id: str
    config_version: int
    def __init__(self, agent: _Optional[str] = ..., card_json: _Optional[str] = ..., error: _Optional[str] = ..., fetched_at_unix_ms: _Optional[int] = ..., agent_id: _Optional[str] = ..., config_version: _Optional[int] = ...) -> None: ...

class Ack(_message.Message):
    __slots__ = ("version",)
    VERSION_FIELD_NUMBER: _ClassVar[int]
    version: int
    def __init__(self, version: _Optional[int] = ...) -> None: ...

class Nack(_message.Message):
    __slots__ = ("version", "reason", "have_version", "code")
    VERSION_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    HAVE_VERSION_FIELD_NUMBER: _ClassVar[int]
    CODE_FIELD_NUMBER: _ClassVar[int]
    version: int
    reason: str
    have_version: int
    code: NackCode
    def __init__(self, version: _Optional[int] = ..., reason: _Optional[str] = ..., have_version: _Optional[int] = ..., code: _Optional[_Union[NackCode, str]] = ...) -> None: ...

class ConfigUpdate(_message.Message):
    __slots__ = ("full", "delta", "target_digest")
    FULL_FIELD_NUMBER: _ClassVar[int]
    DELTA_FIELD_NUMBER: _ClassVar[int]
    TARGET_DIGEST_FIELD_NUMBER: _ClassVar[int]
    full: _snapshot_pb2.Snapshot
    delta: Delta
    target_digest: bytes
    def __init__(self, full: _Optional[_Union[_snapshot_pb2.Snapshot, _Mapping]] = ..., delta: _Optional[_Union[Delta, _Mapping]] = ..., target_digest: _Optional[bytes] = ...) -> None: ...

class Delta(_message.Message):
    __slots__ = ("base_version", "version", "upsert_upstreams", "remove_upstreams", "upsert_models", "remove_models", "upsert_routers", "remove_routers", "upsert_api_keys", "remove_api_keys", "upsert_teams", "remove_teams", "created_at_unix_ms", "required_capabilities", "upsert_aliases", "remove_aliases", "upsert_mcp_servers", "remove_mcp_servers", "upsert_a2a_agents", "remove_a2a_agents")
    BASE_VERSION_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    UPSERT_UPSTREAMS_FIELD_NUMBER: _ClassVar[int]
    REMOVE_UPSTREAMS_FIELD_NUMBER: _ClassVar[int]
    UPSERT_MODELS_FIELD_NUMBER: _ClassVar[int]
    REMOVE_MODELS_FIELD_NUMBER: _ClassVar[int]
    UPSERT_ROUTERS_FIELD_NUMBER: _ClassVar[int]
    REMOVE_ROUTERS_FIELD_NUMBER: _ClassVar[int]
    UPSERT_API_KEYS_FIELD_NUMBER: _ClassVar[int]
    REMOVE_API_KEYS_FIELD_NUMBER: _ClassVar[int]
    UPSERT_TEAMS_FIELD_NUMBER: _ClassVar[int]
    REMOVE_TEAMS_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    REQUIRED_CAPABILITIES_FIELD_NUMBER: _ClassVar[int]
    UPSERT_ALIASES_FIELD_NUMBER: _ClassVar[int]
    REMOVE_ALIASES_FIELD_NUMBER: _ClassVar[int]
    UPSERT_MCP_SERVERS_FIELD_NUMBER: _ClassVar[int]
    REMOVE_MCP_SERVERS_FIELD_NUMBER: _ClassVar[int]
    UPSERT_A2A_AGENTS_FIELD_NUMBER: _ClassVar[int]
    REMOVE_A2A_AGENTS_FIELD_NUMBER: _ClassVar[int]
    base_version: int
    version: int
    upsert_upstreams: _containers.RepeatedCompositeFieldContainer[_snapshot_pb2.Upstream]
    remove_upstreams: _containers.RepeatedScalarFieldContainer[str]
    upsert_models: _containers.RepeatedCompositeFieldContainer[_snapshot_pb2.Model]
    remove_models: _containers.RepeatedScalarFieldContainer[str]
    upsert_routers: _containers.RepeatedCompositeFieldContainer[_snapshot_pb2.Router]
    remove_routers: _containers.RepeatedScalarFieldContainer[str]
    upsert_api_keys: _containers.RepeatedCompositeFieldContainer[_snapshot_pb2.ApiKey]
    remove_api_keys: _containers.RepeatedScalarFieldContainer[str]
    upsert_teams: _containers.RepeatedCompositeFieldContainer[_snapshot_pb2.Team]
    remove_teams: _containers.RepeatedScalarFieldContainer[str]
    created_at_unix_ms: int
    required_capabilities: _containers.RepeatedScalarFieldContainer[str]
    upsert_aliases: _containers.RepeatedCompositeFieldContainer[_snapshot_pb2.Alias]
    remove_aliases: _containers.RepeatedScalarFieldContainer[str]
    upsert_mcp_servers: _containers.RepeatedCompositeFieldContainer[_snapshot_pb2.McpServer]
    remove_mcp_servers: _containers.RepeatedScalarFieldContainer[str]
    upsert_a2a_agents: _containers.RepeatedCompositeFieldContainer[_snapshot_pb2.A2aAgent]
    remove_a2a_agents: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, base_version: _Optional[int] = ..., version: _Optional[int] = ..., upsert_upstreams: _Optional[_Iterable[_Union[_snapshot_pb2.Upstream, _Mapping]]] = ..., remove_upstreams: _Optional[_Iterable[str]] = ..., upsert_models: _Optional[_Iterable[_Union[_snapshot_pb2.Model, _Mapping]]] = ..., remove_models: _Optional[_Iterable[str]] = ..., upsert_routers: _Optional[_Iterable[_Union[_snapshot_pb2.Router, _Mapping]]] = ..., remove_routers: _Optional[_Iterable[str]] = ..., upsert_api_keys: _Optional[_Iterable[_Union[_snapshot_pb2.ApiKey, _Mapping]]] = ..., remove_api_keys: _Optional[_Iterable[str]] = ..., upsert_teams: _Optional[_Iterable[_Union[_snapshot_pb2.Team, _Mapping]]] = ..., remove_teams: _Optional[_Iterable[str]] = ..., created_at_unix_ms: _Optional[int] = ..., required_capabilities: _Optional[_Iterable[str]] = ..., upsert_aliases: _Optional[_Iterable[_Union[_snapshot_pb2.Alias, _Mapping]]] = ..., remove_aliases: _Optional[_Iterable[str]] = ..., upsert_mcp_servers: _Optional[_Iterable[_Union[_snapshot_pb2.McpServer, _Mapping]]] = ..., remove_mcp_servers: _Optional[_Iterable[str]] = ..., upsert_a2a_agents: _Optional[_Iterable[_Union[_snapshot_pb2.A2aAgent, _Mapping]]] = ..., remove_a2a_agents: _Optional[_Iterable[str]] = ...) -> None: ...
