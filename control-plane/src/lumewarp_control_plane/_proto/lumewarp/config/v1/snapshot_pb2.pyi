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

class RoutingStrategy(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ROUTING_STRATEGY_UNSPECIFIED: _ClassVar[RoutingStrategy]
    ROUTING_STRATEGY_WEIGHTED: _ClassVar[RoutingStrategy]
    ROUTING_STRATEGY_LEAST_IN_FLIGHT: _ClassVar[RoutingStrategy]
    ROUTING_STRATEGY_LOWEST_LATENCY: _ClassVar[RoutingStrategy]

class KeyStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    KEY_STATUS_UNSPECIFIED: _ClassVar[KeyStatus]
    KEY_STATUS_ACTIVE: _ClassVar[KeyStatus]
    KEY_STATUS_REVOKED: _ClassVar[KeyStatus]
    KEY_STATUS_BUDGET_EXCEEDED: _ClassVar[KeyStatus]
    KEY_STATUS_SUSPENDED: _ClassVar[KeyStatus]
STREAM_USAGE_MODE_UNSPECIFIED: StreamUsageMode
STREAM_USAGE_MODE_INCLUDE_USAGE: StreamUsageMode
STREAM_USAGE_MODE_PASSTHROUGH: StreamUsageMode
UPSTREAM_KIND_UNSPECIFIED: UpstreamKind
UPSTREAM_KIND_OPENAI: UpstreamKind
UPSTREAM_KIND_VLLM: UpstreamKind
UPSTREAM_KIND_OPENAI_COMPATIBLE: UpstreamKind
ROUTING_STRATEGY_UNSPECIFIED: RoutingStrategy
ROUTING_STRATEGY_WEIGHTED: RoutingStrategy
ROUTING_STRATEGY_LEAST_IN_FLIGHT: RoutingStrategy
ROUTING_STRATEGY_LOWEST_LATENCY: RoutingStrategy
KEY_STATUS_UNSPECIFIED: KeyStatus
KEY_STATUS_ACTIVE: KeyStatus
KEY_STATUS_REVOKED: KeyStatus
KEY_STATUS_BUDGET_EXCEEDED: KeyStatus
KEY_STATUS_SUSPENDED: KeyStatus

class Snapshot(_message.Message):
    __slots__ = ("version", "upstreams", "deployments", "routes", "api_keys", "teams", "created_at_unix_ms", "required_capabilities", "scope")
    VERSION_FIELD_NUMBER: _ClassVar[int]
    UPSTREAMS_FIELD_NUMBER: _ClassVar[int]
    DEPLOYMENTS_FIELD_NUMBER: _ClassVar[int]
    ROUTES_FIELD_NUMBER: _ClassVar[int]
    API_KEYS_FIELD_NUMBER: _ClassVar[int]
    TEAMS_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    REQUIRED_CAPABILITIES_FIELD_NUMBER: _ClassVar[int]
    SCOPE_FIELD_NUMBER: _ClassVar[int]
    version: int
    upstreams: _containers.RepeatedCompositeFieldContainer[Upstream]
    deployments: _containers.RepeatedCompositeFieldContainer[Deployment]
    routes: _containers.RepeatedCompositeFieldContainer[ModelRoute]
    api_keys: _containers.RepeatedCompositeFieldContainer[ApiKey]
    teams: _containers.RepeatedCompositeFieldContainer[Team]
    created_at_unix_ms: int
    required_capabilities: _containers.RepeatedScalarFieldContainer[str]
    scope: str
    def __init__(self, version: _Optional[int] = ..., upstreams: _Optional[_Iterable[_Union[Upstream, _Mapping]]] = ..., deployments: _Optional[_Iterable[_Union[Deployment, _Mapping]]] = ..., routes: _Optional[_Iterable[_Union[ModelRoute, _Mapping]]] = ..., api_keys: _Optional[_Iterable[_Union[ApiKey, _Mapping]]] = ..., teams: _Optional[_Iterable[_Union[Team, _Mapping]]] = ..., created_at_unix_ms: _Optional[int] = ..., required_capabilities: _Optional[_Iterable[str]] = ..., scope: _Optional[str] = ...) -> None: ...

class Upstream(_message.Message):
    __slots__ = ("name", "kind", "base_url", "api_key_ref", "timeout_ms", "stream_usage_mode")
    NAME_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    BASE_URL_FIELD_NUMBER: _ClassVar[int]
    API_KEY_REF_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_MS_FIELD_NUMBER: _ClassVar[int]
    STREAM_USAGE_MODE_FIELD_NUMBER: _ClassVar[int]
    name: str
    kind: UpstreamKind
    base_url: str
    api_key_ref: str
    timeout_ms: int
    stream_usage_mode: StreamUsageMode
    def __init__(self, name: _Optional[str] = ..., kind: _Optional[_Union[UpstreamKind, str]] = ..., base_url: _Optional[str] = ..., api_key_ref: _Optional[str] = ..., timeout_ms: _Optional[int] = ..., stream_usage_mode: _Optional[_Union[StreamUsageMode, str]] = ...) -> None: ...

class Deployment(_message.Message):
    __slots__ = ("name", "upstream", "upstream_model", "weight", "price")
    NAME_FIELD_NUMBER: _ClassVar[int]
    UPSTREAM_FIELD_NUMBER: _ClassVar[int]
    UPSTREAM_MODEL_FIELD_NUMBER: _ClassVar[int]
    WEIGHT_FIELD_NUMBER: _ClassVar[int]
    PRICE_FIELD_NUMBER: _ClassVar[int]
    name: str
    upstream: str
    upstream_model: str
    weight: int
    price: Price
    def __init__(self, name: _Optional[str] = ..., upstream: _Optional[str] = ..., upstream_model: _Optional[str] = ..., weight: _Optional[int] = ..., price: _Optional[_Union[Price, _Mapping]] = ...) -> None: ...

class Price(_message.Message):
    __slots__ = ("input_nano_usd_per_mtok", "output_nano_usd_per_mtok")
    INPUT_NANO_USD_PER_MTOK_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_NANO_USD_PER_MTOK_FIELD_NUMBER: _ClassVar[int]
    input_nano_usd_per_mtok: int
    output_nano_usd_per_mtok: int
    def __init__(self, input_nano_usd_per_mtok: _Optional[int] = ..., output_nano_usd_per_mtok: _Optional[int] = ...) -> None: ...

class ModelRoute(_message.Message):
    __slots__ = ("model", "deployments", "strategy")
    MODEL_FIELD_NUMBER: _ClassVar[int]
    DEPLOYMENTS_FIELD_NUMBER: _ClassVar[int]
    STRATEGY_FIELD_NUMBER: _ClassVar[int]
    model: str
    deployments: _containers.RepeatedScalarFieldContainer[str]
    strategy: RoutingStrategy
    def __init__(self, model: _Optional[str] = ..., deployments: _Optional[_Iterable[str]] = ..., strategy: _Optional[_Union[RoutingStrategy, str]] = ...) -> None: ...

class Team(_message.Message):
    __slots__ = ("id", "name")
    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    id: str
    name: str
    def __init__(self, id: _Optional[str] = ..., name: _Optional[str] = ...) -> None: ...

class ApiKey(_message.Message):
    __slots__ = ("key_id", "key_hash", "team_id", "status", "expires_at_unix_ms", "policy")
    KEY_ID_FIELD_NUMBER: _ClassVar[int]
    KEY_HASH_FIELD_NUMBER: _ClassVar[int]
    TEAM_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    EXPIRES_AT_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    POLICY_FIELD_NUMBER: _ClassVar[int]
    key_id: str
    key_hash: bytes
    team_id: str
    status: KeyStatus
    expires_at_unix_ms: int
    policy: EffectivePolicy
    def __init__(self, key_id: _Optional[str] = ..., key_hash: _Optional[bytes] = ..., team_id: _Optional[str] = ..., status: _Optional[_Union[KeyStatus, str]] = ..., expires_at_unix_ms: _Optional[int] = ..., policy: _Optional[_Union[EffectivePolicy, _Mapping]] = ...) -> None: ...

class EffectivePolicy(_message.Message):
    __slots__ = ("allow_all_models", "allowed_models", "rate_limit_scopes", "budget_window_end_unix_ms")
    ALLOW_ALL_MODELS_FIELD_NUMBER: _ClassVar[int]
    ALLOWED_MODELS_FIELD_NUMBER: _ClassVar[int]
    RATE_LIMIT_SCOPES_FIELD_NUMBER: _ClassVar[int]
    BUDGET_WINDOW_END_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    allow_all_models: bool
    allowed_models: _containers.RepeatedScalarFieldContainer[str]
    rate_limit_scopes: _containers.RepeatedCompositeFieldContainer[RateLimitScope]
    budget_window_end_unix_ms: int
    def __init__(self, allow_all_models: _Optional[bool] = ..., allowed_models: _Optional[_Iterable[str]] = ..., rate_limit_scopes: _Optional[_Iterable[_Union[RateLimitScope, _Mapping]]] = ..., budget_window_end_unix_ms: _Optional[int] = ...) -> None: ...

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
