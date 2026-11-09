from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class TrafficKind(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    TRAFFIC_KIND_UNSPECIFIED: _ClassVar[TrafficKind]
    TRAFFIC_KIND_LLM: _ClassVar[TrafficKind]
    TRAFFIC_KIND_MCP: _ClassVar[TrafficKind]
    TRAFFIC_KIND_A2A: _ClassVar[TrafficKind]

class UsageState(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    USAGE_STATE_UNSPECIFIED: _ClassVar[UsageState]
    USAGE_STATE_REPORTED: _ClassVar[UsageState]
    USAGE_STATE_MISSING: _ClassVar[UsageState]
    USAGE_STATE_PARTIAL: _ClassVar[UsageState]
    USAGE_STATE_INVALID: _ClassVar[UsageState]

class RequestOutcome(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    REQUEST_OUTCOME_UNSPECIFIED: _ClassVar[RequestOutcome]
    REQUEST_OUTCOME_COMPLETED: _ClassVar[RequestOutcome]
    REQUEST_OUTCOME_CLIENT_CANCELLED: _ClassVar[RequestOutcome]
    REQUEST_OUTCOME_UPSTREAM_ERROR: _ClassVar[RequestOutcome]
    REQUEST_OUTCOME_REJECTED: _ClassVar[RequestOutcome]
TRAFFIC_KIND_UNSPECIFIED: TrafficKind
TRAFFIC_KIND_LLM: TrafficKind
TRAFFIC_KIND_MCP: TrafficKind
TRAFFIC_KIND_A2A: TrafficKind
USAGE_STATE_UNSPECIFIED: UsageState
USAGE_STATE_REPORTED: UsageState
USAGE_STATE_MISSING: UsageState
USAGE_STATE_PARTIAL: UsageState
USAGE_STATE_INVALID: UsageState
REQUEST_OUTCOME_UNSPECIFIED: RequestOutcome
REQUEST_OUTCOME_COMPLETED: RequestOutcome
REQUEST_OUTCOME_CLIENT_CANCELLED: RequestOutcome
REQUEST_OUTCOME_UPSTREAM_ERROR: RequestOutcome
REQUEST_OUTCOME_REJECTED: RequestOutcome

class ReportRequest(_message.Message):
    __slots__ = ("gateway_id", "events")
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    EVENTS_FIELD_NUMBER: _ClassVar[int]
    gateway_id: str
    events: _containers.RepeatedCompositeFieldContainer[UsageEvent]
    def __init__(self, gateway_id: _Optional[str] = ..., events: _Optional[_Iterable[_Union[UsageEvent, _Mapping]]] = ...) -> None: ...

class ReportResponse(_message.Message):
    __slots__ = ("accepted",)
    ACCEPTED_FIELD_NUMBER: _ClassVar[int]
    accepted: int
    def __init__(self, accepted: _Optional[int] = ...) -> None: ...

class UsageEvent(_message.Message):
    __slots__ = ("request_id", "timestamp_unix_ms", "key_id", "team_id", "requested", "model", "endpoint", "status", "stream", "prompt_tokens", "completion_tokens", "duration_ms", "config_version", "usage_state", "outcome", "started_at_unix_ms", "cost_nano_usd", "cost_known", "alias", "router", "upstream", "upstream_model", "fallback", "attempts", "kind", "target", "method", "tool")
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    KEY_ID_FIELD_NUMBER: _ClassVar[int]
    TEAM_ID_FIELD_NUMBER: _ClassVar[int]
    REQUESTED_FIELD_NUMBER: _ClassVar[int]
    MODEL_FIELD_NUMBER: _ClassVar[int]
    ENDPOINT_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    STREAM_FIELD_NUMBER: _ClassVar[int]
    PROMPT_TOKENS_FIELD_NUMBER: _ClassVar[int]
    COMPLETION_TOKENS_FIELD_NUMBER: _ClassVar[int]
    DURATION_MS_FIELD_NUMBER: _ClassVar[int]
    CONFIG_VERSION_FIELD_NUMBER: _ClassVar[int]
    USAGE_STATE_FIELD_NUMBER: _ClassVar[int]
    OUTCOME_FIELD_NUMBER: _ClassVar[int]
    STARTED_AT_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    COST_NANO_USD_FIELD_NUMBER: _ClassVar[int]
    COST_KNOWN_FIELD_NUMBER: _ClassVar[int]
    ALIAS_FIELD_NUMBER: _ClassVar[int]
    ROUTER_FIELD_NUMBER: _ClassVar[int]
    UPSTREAM_FIELD_NUMBER: _ClassVar[int]
    UPSTREAM_MODEL_FIELD_NUMBER: _ClassVar[int]
    FALLBACK_FIELD_NUMBER: _ClassVar[int]
    ATTEMPTS_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    TARGET_FIELD_NUMBER: _ClassVar[int]
    METHOD_FIELD_NUMBER: _ClassVar[int]
    TOOL_FIELD_NUMBER: _ClassVar[int]
    request_id: str
    timestamp_unix_ms: int
    key_id: str
    team_id: str
    requested: str
    model: str
    endpoint: str
    status: int
    stream: bool
    prompt_tokens: int
    completion_tokens: int
    duration_ms: int
    config_version: int
    usage_state: UsageState
    outcome: RequestOutcome
    started_at_unix_ms: int
    cost_nano_usd: int
    cost_known: bool
    alias: str
    router: str
    upstream: str
    upstream_model: str
    fallback: bool
    attempts: int
    kind: TrafficKind
    target: str
    method: str
    tool: str
    def __init__(self, request_id: _Optional[str] = ..., timestamp_unix_ms: _Optional[int] = ..., key_id: _Optional[str] = ..., team_id: _Optional[str] = ..., requested: _Optional[str] = ..., model: _Optional[str] = ..., endpoint: _Optional[str] = ..., status: _Optional[int] = ..., stream: _Optional[bool] = ..., prompt_tokens: _Optional[int] = ..., completion_tokens: _Optional[int] = ..., duration_ms: _Optional[int] = ..., config_version: _Optional[int] = ..., usage_state: _Optional[_Union[UsageState, str]] = ..., outcome: _Optional[_Union[RequestOutcome, str]] = ..., started_at_unix_ms: _Optional[int] = ..., cost_nano_usd: _Optional[int] = ..., cost_known: _Optional[bool] = ..., alias: _Optional[str] = ..., router: _Optional[str] = ..., upstream: _Optional[str] = ..., upstream_model: _Optional[str] = ..., fallback: _Optional[bool] = ..., attempts: _Optional[int] = ..., kind: _Optional[_Union[TrafficKind, str]] = ..., target: _Optional[str] = ..., method: _Optional[str] = ..., tool: _Optional[str] = ...) -> None: ...
