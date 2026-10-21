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
    __slots__ = ("hello", "ack", "nack")
    HELLO_FIELD_NUMBER: _ClassVar[int]
    ACK_FIELD_NUMBER: _ClassVar[int]
    NACK_FIELD_NUMBER: _ClassVar[int]
    hello: Hello
    ack: Ack
    nack: Nack
    def __init__(self, hello: _Optional[_Union[Hello, _Mapping]] = ..., ack: _Optional[_Union[Ack, _Mapping]] = ..., nack: _Optional[_Union[Nack, _Mapping]] = ...) -> None: ...

class Hello(_message.Message):
    __slots__ = ("gateway_id", "have_version", "gateway_version", "supported_capabilities", "scope")
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    HAVE_VERSION_FIELD_NUMBER: _ClassVar[int]
    GATEWAY_VERSION_FIELD_NUMBER: _ClassVar[int]
    SUPPORTED_CAPABILITIES_FIELD_NUMBER: _ClassVar[int]
    SCOPE_FIELD_NUMBER: _ClassVar[int]
    gateway_id: str
    have_version: int
    gateway_version: str
    supported_capabilities: _containers.RepeatedScalarFieldContainer[str]
    scope: str
    def __init__(self, gateway_id: _Optional[str] = ..., have_version: _Optional[int] = ..., gateway_version: _Optional[str] = ..., supported_capabilities: _Optional[_Iterable[str]] = ..., scope: _Optional[str] = ...) -> None: ...

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
    __slots__ = ("full", "delta")
    FULL_FIELD_NUMBER: _ClassVar[int]
    DELTA_FIELD_NUMBER: _ClassVar[int]
    full: _snapshot_pb2.Snapshot
    delta: Delta
    def __init__(self, full: _Optional[_Union[_snapshot_pb2.Snapshot, _Mapping]] = ..., delta: _Optional[_Union[Delta, _Mapping]] = ...) -> None: ...

class Delta(_message.Message):
    __slots__ = ("base_version", "version", "upsert_upstreams", "remove_upstreams", "upsert_deployments", "remove_deployments", "upsert_routes", "remove_routes", "upsert_api_keys", "remove_api_keys", "upsert_teams", "remove_teams", "created_at_unix_ms", "required_capabilities")
    BASE_VERSION_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    UPSERT_UPSTREAMS_FIELD_NUMBER: _ClassVar[int]
    REMOVE_UPSTREAMS_FIELD_NUMBER: _ClassVar[int]
    UPSERT_DEPLOYMENTS_FIELD_NUMBER: _ClassVar[int]
    REMOVE_DEPLOYMENTS_FIELD_NUMBER: _ClassVar[int]
    UPSERT_ROUTES_FIELD_NUMBER: _ClassVar[int]
    REMOVE_ROUTES_FIELD_NUMBER: _ClassVar[int]
    UPSERT_API_KEYS_FIELD_NUMBER: _ClassVar[int]
    REMOVE_API_KEYS_FIELD_NUMBER: _ClassVar[int]
    UPSERT_TEAMS_FIELD_NUMBER: _ClassVar[int]
    REMOVE_TEAMS_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    REQUIRED_CAPABILITIES_FIELD_NUMBER: _ClassVar[int]
    base_version: int
    version: int
    upsert_upstreams: _containers.RepeatedCompositeFieldContainer[_snapshot_pb2.Upstream]
    remove_upstreams: _containers.RepeatedScalarFieldContainer[str]
    upsert_deployments: _containers.RepeatedCompositeFieldContainer[_snapshot_pb2.Deployment]
    remove_deployments: _containers.RepeatedScalarFieldContainer[str]
    upsert_routes: _containers.RepeatedCompositeFieldContainer[_snapshot_pb2.ModelRoute]
    remove_routes: _containers.RepeatedScalarFieldContainer[str]
    upsert_api_keys: _containers.RepeatedCompositeFieldContainer[_snapshot_pb2.ApiKey]
    remove_api_keys: _containers.RepeatedScalarFieldContainer[str]
    upsert_teams: _containers.RepeatedCompositeFieldContainer[_snapshot_pb2.Team]
    remove_teams: _containers.RepeatedScalarFieldContainer[str]
    created_at_unix_ms: int
    required_capabilities: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, base_version: _Optional[int] = ..., version: _Optional[int] = ..., upsert_upstreams: _Optional[_Iterable[_Union[_snapshot_pb2.Upstream, _Mapping]]] = ..., remove_upstreams: _Optional[_Iterable[str]] = ..., upsert_deployments: _Optional[_Iterable[_Union[_snapshot_pb2.Deployment, _Mapping]]] = ..., remove_deployments: _Optional[_Iterable[str]] = ..., upsert_routes: _Optional[_Iterable[_Union[_snapshot_pb2.ModelRoute, _Mapping]]] = ..., remove_routes: _Optional[_Iterable[str]] = ..., upsert_api_keys: _Optional[_Iterable[_Union[_snapshot_pb2.ApiKey, _Mapping]]] = ..., remove_api_keys: _Optional[_Iterable[str]] = ..., upsert_teams: _Optional[_Iterable[_Union[_snapshot_pb2.Team, _Mapping]]] = ..., remove_teams: _Optional[_Iterable[str]] = ..., created_at_unix_ms: _Optional[int] = ..., required_capabilities: _Optional[_Iterable[str]] = ...) -> None: ...
