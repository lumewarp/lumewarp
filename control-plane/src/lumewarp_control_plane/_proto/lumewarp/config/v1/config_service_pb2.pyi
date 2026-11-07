from lumewarp_control_plane._proto.lumewarp.config.v1 import snapshot_pb2 as _snapshot_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class NackCode(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    NACK_CODE_UNSPECIFIED: _ClassVar[NackCode]
    NACK_CODE_BASE_MISMATCH: _ClassVar[NackCode]
    NACK_CODE_INVALID_CONFIG: _ClassVar[NackCode]
    NACK_CODE_UNSUPPORTED_FEATURE: _ClassVar[NackCode]
    NACK_CODE_PERSIST_FAILED: _ClassVar[NackCode]
NACK_CODE_UNSPECIFIED: NackCode
NACK_CODE_BASE_MISMATCH: NackCode
NACK_CODE_INVALID_CONFIG: NackCode
NACK_CODE_UNSUPPORTED_FEATURE: NackCode
NACK_CODE_PERSIST_FAILED: NackCode

class GatewayMessage(_message.Message):
    __slots__ = ("hello", "ack", "nack", "health")
    HELLO_FIELD_NUMBER: _ClassVar[int]
    ACK_FIELD_NUMBER: _ClassVar[int]
    NACK_FIELD_NUMBER: _ClassVar[int]
    HEALTH_FIELD_NUMBER: _ClassVar[int]
    hello: Hello
    ack: Ack
    nack: Nack
    health: HealthReport
    def __init__(self, hello: _Optional[_Union[Hello, _Mapping]] = ..., ack: _Optional[_Union[Ack, _Mapping]] = ..., nack: _Optional[_Union[Nack, _Mapping]] = ..., health: _Optional[_Union[HealthReport, _Mapping]] = ...) -> None: ...

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
    __slots__ = ("gateway_id", "have_version", "gateway_version", "supported_capabilities", "scope", "have_digest", "strategies")
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    HAVE_VERSION_FIELD_NUMBER: _ClassVar[int]
    GATEWAY_VERSION_FIELD_NUMBER: _ClassVar[int]
    SUPPORTED_CAPABILITIES_FIELD_NUMBER: _ClassVar[int]
    SCOPE_FIELD_NUMBER: _ClassVar[int]
    HAVE_DIGEST_FIELD_NUMBER: _ClassVar[int]
    STRATEGIES_FIELD_NUMBER: _ClassVar[int]
    gateway_id: str
    have_version: int
    gateway_version: str
    supported_capabilities: _containers.RepeatedScalarFieldContainer[str]
    scope: str
    have_digest: bytes
    strategies: _containers.RepeatedCompositeFieldContainer[StrategyDescriptor]
    def __init__(self, gateway_id: _Optional[str] = ..., have_version: _Optional[int] = ..., gateway_version: _Optional[str] = ..., supported_capabilities: _Optional[_Iterable[str]] = ..., scope: _Optional[str] = ..., have_digest: _Optional[bytes] = ..., strategies: _Optional[_Iterable[_Union[StrategyDescriptor, _Mapping]]] = ...) -> None: ...

class StrategyDescriptor(_message.Message):
    __slots__ = ("name", "title", "description", "uses_weight", "needs", "params_schema_json", "member_schema_json", "digest")
    NAME_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    USES_WEIGHT_FIELD_NUMBER: _ClassVar[int]
    NEEDS_FIELD_NUMBER: _ClassVar[int]
    PARAMS_SCHEMA_JSON_FIELD_NUMBER: _ClassVar[int]
    MEMBER_SCHEMA_JSON_FIELD_NUMBER: _ClassVar[int]
    DIGEST_FIELD_NUMBER: _ClassVar[int]
    name: str
    title: str
    description: str
    uses_weight: bool
    needs: _containers.RepeatedScalarFieldContainer[str]
    params_schema_json: str
    member_schema_json: str
    digest: str
    def __init__(self, name: _Optional[str] = ..., title: _Optional[str] = ..., description: _Optional[str] = ..., uses_weight: _Optional[bool] = ..., needs: _Optional[_Iterable[str]] = ..., params_schema_json: _Optional[str] = ..., member_schema_json: _Optional[str] = ..., digest: _Optional[str] = ...) -> None: ...

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
    __slots__ = ("base_version", "version", "upsert_upstreams", "remove_upstreams", "upsert_models", "remove_models", "upsert_routers", "remove_routers", "upsert_api_keys", "remove_api_keys", "upsert_teams", "remove_teams", "created_at_unix_ms", "required_capabilities", "upsert_aliases", "remove_aliases")
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
    def __init__(self, base_version: _Optional[int] = ..., version: _Optional[int] = ..., upsert_upstreams: _Optional[_Iterable[_Union[_snapshot_pb2.Upstream, _Mapping]]] = ..., remove_upstreams: _Optional[_Iterable[str]] = ..., upsert_models: _Optional[_Iterable[_Union[_snapshot_pb2.Model, _Mapping]]] = ..., remove_models: _Optional[_Iterable[str]] = ..., upsert_routers: _Optional[_Iterable[_Union[_snapshot_pb2.Router, _Mapping]]] = ..., remove_routers: _Optional[_Iterable[str]] = ..., upsert_api_keys: _Optional[_Iterable[_Union[_snapshot_pb2.ApiKey, _Mapping]]] = ..., remove_api_keys: _Optional[_Iterable[str]] = ..., upsert_teams: _Optional[_Iterable[_Union[_snapshot_pb2.Team, _Mapping]]] = ..., remove_teams: _Optional[_Iterable[str]] = ..., created_at_unix_ms: _Optional[int] = ..., required_capabilities: _Optional[_Iterable[str]] = ..., upsert_aliases: _Optional[_Iterable[_Union[_snapshot_pb2.Alias, _Mapping]]] = ..., remove_aliases: _Optional[_Iterable[str]] = ...) -> None: ...
