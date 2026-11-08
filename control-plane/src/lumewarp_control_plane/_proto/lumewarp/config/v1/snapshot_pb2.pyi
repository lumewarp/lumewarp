from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class StreamUsageMode(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    STREAM_USAGE_MODE_UNSPECIFIED: _ClassVar[StreamUsageMode]
    STREAM_USAGE_MODE_INCLUDE_USAGE: _ClassVar[StreamUsageMode]
    STREAM_USAGE_MODE_PASSTHROUGH: _ClassVar[StreamUsageMode]

class UpstreamKind(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    UPSTREAM_KIND_UNSPECIFIED: _ClassVar[UpstreamKind]
    UPSTREAM_KIND_OPENAI: _ClassVar[UpstreamKind]
    UPSTREAM_KIND_VLLM: _ClassVar[UpstreamKind]
    UPSTREAM_KIND_OPENAI_COMPATIBLE: _ClassVar[UpstreamKind]

class ReasoningEffortMode(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    REASONING_EFFORT_MODE_UNSPECIFIED: _ClassVar[ReasoningEffortMode]
    REASONING_EFFORT_MODE_PASSTHROUGH: _ClassVar[ReasoningEffortMode]
    REASONING_EFFORT_MODE_STRIP: _ClassVar[ReasoningEffortMode]
    REASONING_EFFORT_MODE_MAPPED: _ClassVar[ReasoningEffortMode]

class StickyBy(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    STICKY_BY_NONE: _ClassVar[StickyBy]
    STICKY_BY_HEADER: _ClassVar[StickyBy]
    STICKY_BY_TEAM: _ClassVar[StickyBy]
    STICKY_BY_KEY: _ClassVar[StickyBy]

class KeyStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    KEY_STATUS_UNSPECIFIED: _ClassVar[KeyStatus]
    KEY_STATUS_ACTIVE: _ClassVar[KeyStatus]
    KEY_STATUS_REVOKED: _ClassVar[KeyStatus]
    KEY_STATUS_BUDGET_EXCEEDED: _ClassVar[KeyStatus]
    KEY_STATUS_SUSPENDED: _ClassVar[KeyStatus]

class McpReviewMode(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    MCP_REVIEW_MODE_UNSPECIFIED: _ClassVar[McpReviewMode]
    MCP_REVIEW_MODE_TOFU: _ClassVar[McpReviewMode]
    MCP_REVIEW_MODE_MANUAL: _ClassVar[McpReviewMode]
    MCP_REVIEW_MODE_OFF: _ClassVar[McpReviewMode]

class McpDefinitionKind(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    MCP_DEFINITION_KIND_UNSPECIFIED: _ClassVar[McpDefinitionKind]
    MCP_DEFINITION_KIND_TOOL: _ClassVar[McpDefinitionKind]
    MCP_DEFINITION_KIND_PROMPT: _ClassVar[McpDefinitionKind]

class McpDefinitionState(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    MCP_DEFINITION_STATE_UNSPECIFIED: _ClassVar[McpDefinitionState]
    MCP_DEFINITION_STATE_APPROVED: _ClassVar[McpDefinitionState]
    MCP_DEFINITION_STATE_PENDING: _ClassVar[McpDefinitionState]
    MCP_DEFINITION_STATE_REJECTED: _ClassVar[McpDefinitionState]
STREAM_USAGE_MODE_UNSPECIFIED: StreamUsageMode
STREAM_USAGE_MODE_INCLUDE_USAGE: StreamUsageMode
STREAM_USAGE_MODE_PASSTHROUGH: StreamUsageMode
UPSTREAM_KIND_UNSPECIFIED: UpstreamKind
UPSTREAM_KIND_OPENAI: UpstreamKind
UPSTREAM_KIND_VLLM: UpstreamKind
UPSTREAM_KIND_OPENAI_COMPATIBLE: UpstreamKind
REASONING_EFFORT_MODE_UNSPECIFIED: ReasoningEffortMode
REASONING_EFFORT_MODE_PASSTHROUGH: ReasoningEffortMode
REASONING_EFFORT_MODE_STRIP: ReasoningEffortMode
REASONING_EFFORT_MODE_MAPPED: ReasoningEffortMode
STICKY_BY_NONE: StickyBy
STICKY_BY_HEADER: StickyBy
STICKY_BY_TEAM: StickyBy
STICKY_BY_KEY: StickyBy
KEY_STATUS_UNSPECIFIED: KeyStatus
KEY_STATUS_ACTIVE: KeyStatus
KEY_STATUS_REVOKED: KeyStatus
KEY_STATUS_BUDGET_EXCEEDED: KeyStatus
KEY_STATUS_SUSPENDED: KeyStatus
MCP_REVIEW_MODE_UNSPECIFIED: McpReviewMode
MCP_REVIEW_MODE_TOFU: McpReviewMode
MCP_REVIEW_MODE_MANUAL: McpReviewMode
MCP_REVIEW_MODE_OFF: McpReviewMode
MCP_DEFINITION_KIND_UNSPECIFIED: McpDefinitionKind
MCP_DEFINITION_KIND_TOOL: McpDefinitionKind
MCP_DEFINITION_KIND_PROMPT: McpDefinitionKind
MCP_DEFINITION_STATE_UNSPECIFIED: McpDefinitionState
MCP_DEFINITION_STATE_APPROVED: McpDefinitionState
MCP_DEFINITION_STATE_PENDING: McpDefinitionState
MCP_DEFINITION_STATE_REJECTED: McpDefinitionState

class Snapshot(_message.Message):
    __slots__ = ("version", "upstreams", "models", "routers", "api_keys", "teams", "created_at_unix_ms", "required_capabilities", "scope", "aliases", "mcp_servers", "a2a_agents")
    VERSION_FIELD_NUMBER: _ClassVar[int]
    UPSTREAMS_FIELD_NUMBER: _ClassVar[int]
    MODELS_FIELD_NUMBER: _ClassVar[int]
    ROUTERS_FIELD_NUMBER: _ClassVar[int]
    API_KEYS_FIELD_NUMBER: _ClassVar[int]
    TEAMS_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    REQUIRED_CAPABILITIES_FIELD_NUMBER: _ClassVar[int]
    SCOPE_FIELD_NUMBER: _ClassVar[int]
    ALIASES_FIELD_NUMBER: _ClassVar[int]
    MCP_SERVERS_FIELD_NUMBER: _ClassVar[int]
    A2A_AGENTS_FIELD_NUMBER: _ClassVar[int]
    version: int
    upstreams: _containers.RepeatedCompositeFieldContainer[Upstream]
    models: _containers.RepeatedCompositeFieldContainer[Model]
    routers: _containers.RepeatedCompositeFieldContainer[Router]
    api_keys: _containers.RepeatedCompositeFieldContainer[ApiKey]
    teams: _containers.RepeatedCompositeFieldContainer[Team]
    created_at_unix_ms: int
    required_capabilities: _containers.RepeatedScalarFieldContainer[str]
    scope: str
    aliases: _containers.RepeatedCompositeFieldContainer[Alias]
    mcp_servers: _containers.RepeatedCompositeFieldContainer[McpServer]
    a2a_agents: _containers.RepeatedCompositeFieldContainer[A2aAgent]
    def __init__(self, version: _Optional[int] = ..., upstreams: _Optional[_Iterable[_Union[Upstream, _Mapping]]] = ..., models: _Optional[_Iterable[_Union[Model, _Mapping]]] = ..., routers: _Optional[_Iterable[_Union[Router, _Mapping]]] = ..., api_keys: _Optional[_Iterable[_Union[ApiKey, _Mapping]]] = ..., teams: _Optional[_Iterable[_Union[Team, _Mapping]]] = ..., created_at_unix_ms: _Optional[int] = ..., required_capabilities: _Optional[_Iterable[str]] = ..., scope: _Optional[str] = ..., aliases: _Optional[_Iterable[_Union[Alias, _Mapping]]] = ..., mcp_servers: _Optional[_Iterable[_Union[McpServer, _Mapping]]] = ..., a2a_agents: _Optional[_Iterable[_Union[A2aAgent, _Mapping]]] = ...) -> None: ...

class Upstream(_message.Message):
    __slots__ = ("name", "kind", "base_url", "api_key_ref", "timeout_ms", "stream_usage_mode", "health_check", "circuit_breaker", "metrics_url", "api_key_ciphertext")
    NAME_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    BASE_URL_FIELD_NUMBER: _ClassVar[int]
    API_KEY_REF_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_MS_FIELD_NUMBER: _ClassVar[int]
    STREAM_USAGE_MODE_FIELD_NUMBER: _ClassVar[int]
    HEALTH_CHECK_FIELD_NUMBER: _ClassVar[int]
    CIRCUIT_BREAKER_FIELD_NUMBER: _ClassVar[int]
    METRICS_URL_FIELD_NUMBER: _ClassVar[int]
    API_KEY_CIPHERTEXT_FIELD_NUMBER: _ClassVar[int]
    name: str
    kind: UpstreamKind
    base_url: str
    api_key_ref: str
    timeout_ms: int
    stream_usage_mode: StreamUsageMode
    health_check: HealthCheck
    circuit_breaker: CircuitBreaker
    metrics_url: str
    api_key_ciphertext: bytes
    def __init__(self, name: _Optional[str] = ..., kind: _Optional[_Union[UpstreamKind, str]] = ..., base_url: _Optional[str] = ..., api_key_ref: _Optional[str] = ..., timeout_ms: _Optional[int] = ..., stream_usage_mode: _Optional[_Union[StreamUsageMode, str]] = ..., health_check: _Optional[_Union[HealthCheck, _Mapping]] = ..., circuit_breaker: _Optional[_Union[CircuitBreaker, _Mapping]] = ..., metrics_url: _Optional[str] = ..., api_key_ciphertext: _Optional[bytes] = ...) -> None: ...

class HealthCheck(_message.Message):
    __slots__ = ("url", "interval_ms", "timeout_ms", "unhealthy_threshold", "healthy_threshold")
    URL_FIELD_NUMBER: _ClassVar[int]
    INTERVAL_MS_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_MS_FIELD_NUMBER: _ClassVar[int]
    UNHEALTHY_THRESHOLD_FIELD_NUMBER: _ClassVar[int]
    HEALTHY_THRESHOLD_FIELD_NUMBER: _ClassVar[int]
    url: str
    interval_ms: int
    timeout_ms: int
    unhealthy_threshold: int
    healthy_threshold: int
    def __init__(self, url: _Optional[str] = ..., interval_ms: _Optional[int] = ..., timeout_ms: _Optional[int] = ..., unhealthy_threshold: _Optional[int] = ..., healthy_threshold: _Optional[int] = ...) -> None: ...

class CircuitBreaker(_message.Message):
    __slots__ = ("failure_threshold", "open_ms")
    FAILURE_THRESHOLD_FIELD_NUMBER: _ClassVar[int]
    OPEN_MS_FIELD_NUMBER: _ClassVar[int]
    failure_threshold: int
    open_ms: int
    def __init__(self, failure_threshold: _Optional[int] = ..., open_ms: _Optional[int] = ...) -> None: ...

class Model(_message.Message):
    __slots__ = ("name", "upstream", "upstream_model", "price", "max_output_tokens", "default_body_json", "reasoning_effort", "chat_check")
    NAME_FIELD_NUMBER: _ClassVar[int]
    UPSTREAM_FIELD_NUMBER: _ClassVar[int]
    UPSTREAM_MODEL_FIELD_NUMBER: _ClassVar[int]
    PRICE_FIELD_NUMBER: _ClassVar[int]
    MAX_OUTPUT_TOKENS_FIELD_NUMBER: _ClassVar[int]
    DEFAULT_BODY_JSON_FIELD_NUMBER: _ClassVar[int]
    REASONING_EFFORT_FIELD_NUMBER: _ClassVar[int]
    CHAT_CHECK_FIELD_NUMBER: _ClassVar[int]
    name: str
    upstream: str
    upstream_model: str
    price: Price
    max_output_tokens: int
    default_body_json: str
    reasoning_effort: ReasoningEffort
    chat_check: ChatCheck
    def __init__(self, name: _Optional[str] = ..., upstream: _Optional[str] = ..., upstream_model: _Optional[str] = ..., price: _Optional[_Union[Price, _Mapping]] = ..., max_output_tokens: _Optional[int] = ..., default_body_json: _Optional[str] = ..., reasoning_effort: _Optional[_Union[ReasoningEffort, _Mapping]] = ..., chat_check: _Optional[_Union[ChatCheck, _Mapping]] = ...) -> None: ...

class ChatCheck(_message.Message):
    __slots__ = ("interval_ms", "timeout_ms", "unhealthy_threshold", "healthy_threshold")
    INTERVAL_MS_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_MS_FIELD_NUMBER: _ClassVar[int]
    UNHEALTHY_THRESHOLD_FIELD_NUMBER: _ClassVar[int]
    HEALTHY_THRESHOLD_FIELD_NUMBER: _ClassVar[int]
    interval_ms: int
    timeout_ms: int
    unhealthy_threshold: int
    healthy_threshold: int
    def __init__(self, interval_ms: _Optional[int] = ..., timeout_ms: _Optional[int] = ..., unhealthy_threshold: _Optional[int] = ..., healthy_threshold: _Optional[int] = ...) -> None: ...

class ReasoningEffort(_message.Message):
    __slots__ = ("mode", "fragments_json", "default_level")
    class FragmentsJsonEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    MODE_FIELD_NUMBER: _ClassVar[int]
    FRAGMENTS_JSON_FIELD_NUMBER: _ClassVar[int]
    DEFAULT_LEVEL_FIELD_NUMBER: _ClassVar[int]
    mode: ReasoningEffortMode
    fragments_json: _containers.ScalarMap[str, str]
    default_level: str
    def __init__(self, mode: _Optional[_Union[ReasoningEffortMode, str]] = ..., fragments_json: _Optional[_Mapping[str, str]] = ..., default_level: _Optional[str] = ...) -> None: ...

class Price(_message.Message):
    __slots__ = ("input_nano_usd_per_mtok", "output_nano_usd_per_mtok")
    INPUT_NANO_USD_PER_MTOK_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_NANO_USD_PER_MTOK_FIELD_NUMBER: _ClassVar[int]
    input_nano_usd_per_mtok: int
    output_nano_usd_per_mtok: int
    def __init__(self, input_nano_usd_per_mtok: _Optional[int] = ..., output_nano_usd_per_mtok: _Optional[int] = ...) -> None: ...

class Alias(_message.Message):
    __slots__ = ("name", "target")
    NAME_FIELD_NUMBER: _ClassVar[int]
    TARGET_FIELD_NUMBER: _ClassVar[int]
    name: str
    target: str
    def __init__(self, name: _Optional[str] = ..., target: _Optional[str] = ...) -> None: ...

class RouterMember(_message.Message):
    __slots__ = ("model", "weight", "params_json")
    MODEL_FIELD_NUMBER: _ClassVar[int]
    WEIGHT_FIELD_NUMBER: _ClassVar[int]
    PARAMS_JSON_FIELD_NUMBER: _ClassVar[int]
    model: str
    weight: int
    params_json: str
    def __init__(self, model: _Optional[str] = ..., weight: _Optional[int] = ..., params_json: _Optional[str] = ...) -> None: ...

class Router(_message.Message):
    __slots__ = ("name", "members", "strategy", "params_json", "retry", "fallbacks", "traffic_split", "sticky", "sticky_header", "plugin_timeout_ms")
    NAME_FIELD_NUMBER: _ClassVar[int]
    MEMBERS_FIELD_NUMBER: _ClassVar[int]
    STRATEGY_FIELD_NUMBER: _ClassVar[int]
    PARAMS_JSON_FIELD_NUMBER: _ClassVar[int]
    RETRY_FIELD_NUMBER: _ClassVar[int]
    FALLBACKS_FIELD_NUMBER: _ClassVar[int]
    TRAFFIC_SPLIT_FIELD_NUMBER: _ClassVar[int]
    STICKY_FIELD_NUMBER: _ClassVar[int]
    STICKY_HEADER_FIELD_NUMBER: _ClassVar[int]
    PLUGIN_TIMEOUT_MS_FIELD_NUMBER: _ClassVar[int]
    name: str
    members: _containers.RepeatedCompositeFieldContainer[RouterMember]
    strategy: str
    params_json: str
    retry: RetryPolicy
    fallbacks: _containers.RepeatedScalarFieldContainer[str]
    traffic_split: _containers.RepeatedCompositeFieldContainer[TrafficSplit]
    sticky: StickyBy
    sticky_header: str
    plugin_timeout_ms: int
    def __init__(self, name: _Optional[str] = ..., members: _Optional[_Iterable[_Union[RouterMember, _Mapping]]] = ..., strategy: _Optional[str] = ..., params_json: _Optional[str] = ..., retry: _Optional[_Union[RetryPolicy, _Mapping]] = ..., fallbacks: _Optional[_Iterable[str]] = ..., traffic_split: _Optional[_Iterable[_Union[TrafficSplit, _Mapping]]] = ..., sticky: _Optional[_Union[StickyBy, str]] = ..., sticky_header: _Optional[str] = ..., plugin_timeout_ms: _Optional[int] = ...) -> None: ...

class RetryPolicy(_message.Message):
    __slots__ = ("max_attempts", "retry_on_status", "total_timeout_ms")
    MAX_ATTEMPTS_FIELD_NUMBER: _ClassVar[int]
    RETRY_ON_STATUS_FIELD_NUMBER: _ClassVar[int]
    TOTAL_TIMEOUT_MS_FIELD_NUMBER: _ClassVar[int]
    max_attempts: int
    retry_on_status: _containers.RepeatedScalarFieldContainer[int]
    total_timeout_ms: int
    def __init__(self, max_attempts: _Optional[int] = ..., retry_on_status: _Optional[_Iterable[int]] = ..., total_timeout_ms: _Optional[int] = ...) -> None: ...

class TrafficSplit(_message.Message):
    __slots__ = ("name", "weight", "members")
    NAME_FIELD_NUMBER: _ClassVar[int]
    WEIGHT_FIELD_NUMBER: _ClassVar[int]
    MEMBERS_FIELD_NUMBER: _ClassVar[int]
    name: str
    weight: int
    members: _containers.RepeatedCompositeFieldContainer[RouterMember]
    def __init__(self, name: _Optional[str] = ..., weight: _Optional[int] = ..., members: _Optional[_Iterable[_Union[RouterMember, _Mapping]]] = ...) -> None: ...

class Team(_message.Message):
    __slots__ = ("id", "name")
    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    id: str
    name: str
    def __init__(self, id: _Optional[str] = ..., name: _Optional[str] = ...) -> None: ...

class ApiKey(_message.Message):
    __slots__ = ("key_id", "key_hash", "team_id", "status", "expires_at_unix_ms", "policy", "previous_key_hash", "previous_key_expires_at_unix_ms")
    KEY_ID_FIELD_NUMBER: _ClassVar[int]
    KEY_HASH_FIELD_NUMBER: _ClassVar[int]
    TEAM_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    EXPIRES_AT_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    POLICY_FIELD_NUMBER: _ClassVar[int]
    PREVIOUS_KEY_HASH_FIELD_NUMBER: _ClassVar[int]
    PREVIOUS_KEY_EXPIRES_AT_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    key_id: str
    key_hash: bytes
    team_id: str
    status: KeyStatus
    expires_at_unix_ms: int
    policy: EffectivePolicy
    previous_key_hash: bytes
    previous_key_expires_at_unix_ms: int
    def __init__(self, key_id: _Optional[str] = ..., key_hash: _Optional[bytes] = ..., team_id: _Optional[str] = ..., status: _Optional[_Union[KeyStatus, str]] = ..., expires_at_unix_ms: _Optional[int] = ..., policy: _Optional[_Union[EffectivePolicy, _Mapping]] = ..., previous_key_hash: _Optional[bytes] = ..., previous_key_expires_at_unix_ms: _Optional[int] = ...) -> None: ...

class EffectivePolicy(_message.Message):
    __slots__ = ("allow_all_models", "allowed_models", "rate_limit_scopes", "budget_window_end_unix_ms", "allow_all_mcp", "mcp_grants", "allow_all_agents", "allowed_agents")
    ALLOW_ALL_MODELS_FIELD_NUMBER: _ClassVar[int]
    ALLOWED_MODELS_FIELD_NUMBER: _ClassVar[int]
    RATE_LIMIT_SCOPES_FIELD_NUMBER: _ClassVar[int]
    BUDGET_WINDOW_END_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    ALLOW_ALL_MCP_FIELD_NUMBER: _ClassVar[int]
    MCP_GRANTS_FIELD_NUMBER: _ClassVar[int]
    ALLOW_ALL_AGENTS_FIELD_NUMBER: _ClassVar[int]
    ALLOWED_AGENTS_FIELD_NUMBER: _ClassVar[int]
    allow_all_models: bool
    allowed_models: _containers.RepeatedScalarFieldContainer[str]
    rate_limit_scopes: _containers.RepeatedCompositeFieldContainer[RateLimitScope]
    budget_window_end_unix_ms: int
    allow_all_mcp: bool
    mcp_grants: _containers.RepeatedCompositeFieldContainer[McpGrant]
    allow_all_agents: bool
    allowed_agents: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, allow_all_models: _Optional[bool] = ..., allowed_models: _Optional[_Iterable[str]] = ..., rate_limit_scopes: _Optional[_Iterable[_Union[RateLimitScope, _Mapping]]] = ..., budget_window_end_unix_ms: _Optional[int] = ..., allow_all_mcp: _Optional[bool] = ..., mcp_grants: _Optional[_Iterable[_Union[McpGrant, _Mapping]]] = ..., allow_all_agents: _Optional[bool] = ..., allowed_agents: _Optional[_Iterable[str]] = ...) -> None: ...

class McpGrant(_message.Message):
    __slots__ = ("server", "all_tools", "tools")
    SERVER_FIELD_NUMBER: _ClassVar[int]
    ALL_TOOLS_FIELD_NUMBER: _ClassVar[int]
    TOOLS_FIELD_NUMBER: _ClassVar[int]
    server: str
    all_tools: bool
    tools: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, server: _Optional[str] = ..., all_tools: _Optional[bool] = ..., tools: _Optional[_Iterable[str]] = ...) -> None: ...

class PluginRef(_message.Message):
    __slots__ = ("plugin", "params_json", "timeout_ms")
    PLUGIN_FIELD_NUMBER: _ClassVar[int]
    PARAMS_JSON_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_MS_FIELD_NUMBER: _ClassVar[int]
    plugin: str
    params_json: str
    timeout_ms: int
    def __init__(self, plugin: _Optional[str] = ..., params_json: _Optional[str] = ..., timeout_ms: _Optional[int] = ...) -> None: ...

class UpstreamAuth(_message.Message):
    __slots__ = ("plugin", "secret_ref", "secret_ciphertext")
    PLUGIN_FIELD_NUMBER: _ClassVar[int]
    SECRET_REF_FIELD_NUMBER: _ClassVar[int]
    SECRET_CIPHERTEXT_FIELD_NUMBER: _ClassVar[int]
    plugin: PluginRef
    secret_ref: str
    secret_ciphertext: bytes
    def __init__(self, plugin: _Optional[_Union[PluginRef, _Mapping]] = ..., secret_ref: _Optional[str] = ..., secret_ciphertext: _Optional[bytes] = ...) -> None: ...

class McpDefinition(_message.Message):
    __slots__ = ("kind", "name", "hash", "state")
    KIND_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    HASH_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    kind: McpDefinitionKind
    name: str
    hash: bytes
    state: McpDefinitionState
    def __init__(self, kind: _Optional[_Union[McpDefinitionKind, str]] = ..., name: _Optional[str] = ..., hash: _Optional[bytes] = ..., state: _Optional[_Union[McpDefinitionState, str]] = ...) -> None: ...

class McpServer(_message.Message):
    __slots__ = ("id", "name", "url", "auth", "timeout_ms", "review_mode", "definitions")
    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    URL_FIELD_NUMBER: _ClassVar[int]
    AUTH_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_MS_FIELD_NUMBER: _ClassVar[int]
    REVIEW_MODE_FIELD_NUMBER: _ClassVar[int]
    DEFINITIONS_FIELD_NUMBER: _ClassVar[int]
    id: str
    name: str
    url: str
    auth: UpstreamAuth
    timeout_ms: int
    review_mode: McpReviewMode
    definitions: _containers.RepeatedCompositeFieldContainer[McpDefinition]
    def __init__(self, id: _Optional[str] = ..., name: _Optional[str] = ..., url: _Optional[str] = ..., auth: _Optional[_Union[UpstreamAuth, _Mapping]] = ..., timeout_ms: _Optional[int] = ..., review_mode: _Optional[_Union[McpReviewMode, str]] = ..., definitions: _Optional[_Iterable[_Union[McpDefinition, _Mapping]]] = ...) -> None: ...

class A2aAgent(_message.Message):
    __slots__ = ("id", "name", "url", "card_url", "auth", "middleware", "timeout_ms", "card_json")
    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    URL_FIELD_NUMBER: _ClassVar[int]
    CARD_URL_FIELD_NUMBER: _ClassVar[int]
    AUTH_FIELD_NUMBER: _ClassVar[int]
    MIDDLEWARE_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_MS_FIELD_NUMBER: _ClassVar[int]
    CARD_JSON_FIELD_NUMBER: _ClassVar[int]
    id: str
    name: str
    url: str
    card_url: str
    auth: UpstreamAuth
    middleware: PluginRef
    timeout_ms: int
    card_json: str
    def __init__(self, id: _Optional[str] = ..., name: _Optional[str] = ..., url: _Optional[str] = ..., card_url: _Optional[str] = ..., auth: _Optional[_Union[UpstreamAuth, _Mapping]] = ..., middleware: _Optional[_Union[PluginRef, _Mapping]] = ..., timeout_ms: _Optional[int] = ..., card_json: _Optional[str] = ...) -> None: ...

class RateLimitScope(_message.Message):
    __slots__ = ("id", "limit")
    ID_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    id: str
    limit: RateLimit
    def __init__(self, id: _Optional[str] = ..., limit: _Optional[_Union[RateLimit, _Mapping]] = ...) -> None: ...

class RateLimit(_message.Message):
    __slots__ = ("requests_per_minute", "tokens_per_minute", "max_parallel_requests")
    REQUESTS_PER_MINUTE_FIELD_NUMBER: _ClassVar[int]
    TOKENS_PER_MINUTE_FIELD_NUMBER: _ClassVar[int]
    MAX_PARALLEL_REQUESTS_FIELD_NUMBER: _ClassVar[int]
    requests_per_minute: int
    tokens_per_minute: int
    max_parallel_requests: int
    def __init__(self, requests_per_minute: _Optional[int] = ..., tokens_per_minute: _Optional[int] = ..., max_parallel_requests: _Optional[int] = ...) -> None: ...
