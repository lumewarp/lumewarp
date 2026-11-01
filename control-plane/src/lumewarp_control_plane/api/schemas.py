"""Pydantic request/response schemas for the admin API.

Inputs reject unknown fields. Outputs are built from ORM rows (`from_attributes=True`) and
never contain hashes or raw secrets.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from datetime import date as date_type
from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from lumewarp_control_plane.compiler.validate import (
    health_path_problem,
    metrics_url_problem,
    upstream_url_problem,
)
from lumewarp_control_plane.models import (
    CatalogKind,
    KeyLifecycle,
    KeyOwnerKind,
    ModelAccess,
    RequestOutcome,
    StickyBy,
    StreamUsageMode,
    TeamRole,
    UpstreamKind,
    UsageState,
)

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_MODEL_NAME = re.compile(r"[^\s\x00-\x1f\x7f]+")


def _not_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


def _model_name(value: str) -> str:
    """Strip, then require a non-empty alias without whitespace or control characters."""
    name = value.strip()
    if not _MODEL_NAME.fullmatch(name):
        raise ValueError(
            "model names must be non-empty and contain no whitespace or control characters"
        )
    return name


def _env_name(value: str | None) -> str | None:
    if value is None:
        return None
    if not _ENV_NAME.fullmatch(value):
        raise ValueError("api_key_ref must be an environment variable name")
    return value


def _future(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is not None and value <= datetime.now(UTC):
        raise ValueError("expires_at must be in the future")
    return value


Name = Annotated[str, Field(min_length=1, max_length=200), AfterValidator(_not_blank)]
ModelName = Annotated[str, Field(min_length=1, max_length=400), AfterValidator(_model_name)]


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorBody(BaseModel):
    """The `{error: {code, message}}` shape every non-2xx response uses."""

    error: ErrorDetail


class ReadModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, protected_namespaces=())


class Page[T](ReadModel):
    items: list[T]
    next_cursor: str | None = None


# --------------------------------------------------------------------------- policy


class PolicyFields(ApiModel):
    """Writable policy columns shared by teams, team members and service accounts.

    `model_access`/`allowed_models` cannot be null; limits and budgets can be set to null
    to clear them. On update, omitted fields keep their current value.
    """

    model_access: ModelAccess | None = None
    allowed_models: list[str] | None = None
    rpm_limit: int | None = Field(default=None, gt=0)
    tpm_limit: int | None = Field(default=None, gt=0)
    max_parallel: int | None = Field(default=None, gt=0)
    budget_nano_usd: int | None = Field(default=None, ge=0)
    soft_budget_nano_usd: int | None = Field(default=None, ge=0)

    @field_validator("allowed_models")
    @classmethod
    def _clean_models(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned: list[str] = []
        for item in value:
            name = _model_name(item)
            if name not in cleaned:
                cleaned.append(name)
        return cleaned

    @model_validator(mode="after")
    def _non_nullable_fields(self) -> PolicyFields:
        if "model_access" in self.model_fields_set and self.model_access is None:
            raise ValueError("model_access cannot be null")
        if "allowed_models" in self.model_fields_set and self.allowed_models is None:
            raise ValueError("allowed_models cannot be null; send [] to deny all")
        return self


class PolicyOut(ReadModel):
    model_access: ModelAccess
    allowed_models: list[str]
    rpm_limit: int | None
    tpm_limit: int | None
    max_parallel: int | None
    budget_nano_usd: int | None
    soft_budget_nano_usd: int | None


class TeamCreate(PolicyFields):
    name: Name


class TeamUpdate(PolicyFields):
    name: Name | None = None


class TeamOut(PolicyOut):
    id: uuid.UUID
    name: str
    created_at: datetime
    updated_at: datetime


class MemberUpsert(PolicyFields):
    role: TeamRole


class MemberCreate(MemberUpsert):
    user_id: uuid.UUID | None = None
    email: str | None = Field(default=None, min_length=1, max_length=320)

    @model_validator(mode="after")
    def _one_user(self) -> MemberCreate:
        if (self.user_id is None) == (self.email is None):
            raise ValueError("provide exactly one of user_id or email")
        if self.email is not None and not self.email.strip():
            raise ValueError("email must not be blank")
        return self


class MemberUpdate(PolicyFields):
    role: TeamRole | None = None


class MemberOut(PolicyOut):
    team_id: uuid.UUID
    user_id: uuid.UUID
    email: str
    display_name: str
    user_active: bool
    role: TeamRole
    member_scope_id: uuid.UUID
    created_at: datetime
    updated_at: datetime


class ServiceAccountCreate(PolicyFields):
    name: Name
    description: str = Field(default="", max_length=2000)


class ServiceAccountUpdate(PolicyFields):
    name: Name | None = None
    description: str | None = Field(default=None, max_length=2000)


class ServiceAccountOut(PolicyOut):
    id: uuid.UUID
    team_id: uuid.UUID
    name: str
    description: str
    created_by_user_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class KeyCreate(ApiModel):
    name: Name
    expires_at: datetime | None = None
    # Omit to create a key owned by the caller.
    service_account_id: uuid.UUID | None = None

    @field_validator("expires_at")
    @classmethod
    def _aware_expiry(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("expires_at must include a timezone")
        return _future(value)


class KeyUpdate(ApiModel):
    name: Name | None = None
    expires_at: datetime | None = None
    lifecycle: KeyLifecycle | None = None

    @field_validator("expires_at")
    @classmethod
    def _aware_expiry(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("expires_at must include a timezone")
        return value


class KeyOut(ReadModel):
    id: uuid.UUID
    team_id: uuid.UUID
    owner_kind: KeyOwnerKind
    user_id: uuid.UUID | None
    service_account_id: uuid.UUID | None
    name: str
    display_prefix: str
    lifecycle: KeyLifecycle
    suspended_reason: str | None
    expires_at: datetime | None
    revoked_at: datetime | None
    created_by_user_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class KeyCreated(ApiModel):
    key: KeyOut
    secret: str


# --------------------------------------------------------------------------- routing


INT32_MAX = 2**31 - 1
# RFC 9110 token: what the gateway accepts as a header name (hyper HeaderName).
_HEADER_TOKEN = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
# Split names are metric labels on the gateway (snapshot.rs::validate_split).
_SPLIT_NAME = re.compile(r"[A-Za-z0-9_.-]{1,32}")
MAX_SPLITS = 16
MAX_RETRY_ATTEMPTS = 5


def _base_url(value: str | None) -> str | None:
    if value is None:
        return None
    if problem := upstream_url_problem(value):
        raise ValueError(f"base_url {problem}")
    return value.rstrip("/")


def _metrics_url(value: str | None) -> str | None:
    """Gateway rule (router.rs::metrics_uri): explicit URL is plain http(s), no userinfo."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if problem := metrics_url_problem(value):
        raise ValueError(f"metrics_url {problem}")
    return value


class HealthCheckIn(ApiModel):
    """Active probe: GET <base_url><path> every interval; 2xx within timeout = success."""

    path: str | None = Field(
        default=None, max_length=400, description="Relative to base_url; null = /models."
    )
    interval_ms: int = Field(ge=1000, le=INT32_MAX)
    timeout_ms: int = Field(ge=1, le=INT32_MAX, description="Must be below interval_ms.")
    unhealthy_threshold: int | None = Field(
        default=None, ge=1, le=1000, description="Consecutive failures; null = 3."
    )
    healthy_threshold: int | None = Field(
        default=None, ge=1, le=1000, description="Consecutive successes; null = 1."
    )

    @field_validator("path")
    @classmethod
    def _path(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        if problem := health_path_problem(value):
            raise ValueError(problem)
        return value

    @model_validator(mode="after")
    def _timeout_below_interval(self) -> HealthCheckIn:
        if self.timeout_ms >= self.interval_ms:
            raise ValueError("timeout_ms must be below interval_ms")
        return self


class HealthCheckOut(ReadModel):
    path: str | None
    interval_ms: int
    timeout_ms: int
    unhealthy_threshold: int | None
    healthy_threshold: int | None


class CircuitBreakerIn(ApiModel):
    failure_threshold: int | None = Field(
        default=None, ge=1, le=1000, description="Consecutive failures to open; null = 5."
    )
    open_ms: int | None = Field(
        default=None, ge=1, le=INT32_MAX, description="Open time before a trial; null = 30000."
    )


class CircuitBreakerOut(ReadModel):
    failure_threshold: int | None
    open_ms: int | None


class UpstreamCreate(ApiModel):
    name: Name
    kind: UpstreamKind
    base_url: str = Field(min_length=1)
    api_key_ref: str | None = Field(default=None, max_length=200)
    timeout_ms: int = Field(default=0, ge=0)
    stream_usage_mode: StreamUsageMode = StreamUsageMode.INCLUDE_USAGE
    health_check: HealthCheckIn | None = None
    circuit_breaker: CircuitBreakerIn | None = None
    metrics_url: str | None = Field(default=None, max_length=2000)

    @field_validator("base_url")
    @classmethod
    def _http_url(cls, value: str) -> str:
        return _base_url(value)  # type: ignore[return-value]

    @field_validator("api_key_ref")
    @classmethod
    def _check_api_key_ref(cls, value: str | None) -> str | None:
        return _env_name(value)

    @field_validator("metrics_url")
    @classmethod
    def _check_metrics_url(cls, value: str | None) -> str | None:
        return _metrics_url(value)


class UpstreamUpdate(ApiModel):
    """PATCH: omitted fields are unchanged; `health_check`, `circuit_breaker` and
    `metrics_url` accept null to remove the setting.
    """

    name: Name | None = None
    kind: UpstreamKind | None = None
    base_url: str | None = Field(default=None, min_length=1)
    api_key_ref: str | None = Field(default=None, max_length=200)
    timeout_ms: int | None = Field(default=None, ge=0)
    stream_usage_mode: StreamUsageMode | None = None
    health_check: HealthCheckIn | None = None
    circuit_breaker: CircuitBreakerIn | None = None
    metrics_url: str | None = Field(default=None, max_length=2000)

    @field_validator("base_url")
    @classmethod
    def _http_url(cls, value: str | None) -> str | None:
        return _base_url(value)

    @field_validator("api_key_ref")
    @classmethod
    def _check_api_key_ref(cls, value: str | None) -> str | None:
        return _env_name(value)

    @field_validator("metrics_url")
    @classmethod
    def _check_metrics_url(cls, value: str | None) -> str | None:
        return _metrics_url(value)


class UpstreamOut(ReadModel):
    id: uuid.UUID
    name: str
    kind: UpstreamKind
    base_url: str
    api_key_ref: str | None
    timeout_ms: int
    stream_usage_mode: StreamUsageMode
    health_check: HealthCheckOut | None
    circuit_breaker: CircuitBreakerOut | None
    metrics_url: str | None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="before")
    @classmethod
    def _from_row(cls, data: Any) -> Any:
        if isinstance(data, dict) or not hasattr(data, "health_check_interval_ms"):
            return data
        health = None
        if data.health_check_interval_ms is not None:
            health = HealthCheckOut(
                path=data.health_check_path,
                interval_ms=data.health_check_interval_ms,
                timeout_ms=data.health_check_timeout_ms,
                unhealthy_threshold=data.health_check_unhealthy_threshold,
                healthy_threshold=data.health_check_healthy_threshold,
            )
        breaker = None
        if data.circuit_breaker_enabled:
            breaker = CircuitBreakerOut(
                failure_threshold=data.circuit_failure_threshold, open_ms=data.circuit_open_ms
            )
        return {
            "id": data.id,
            "name": data.name,
            "kind": data.kind,
            "base_url": data.base_url,
            "api_key_ref": data.api_key_ref,
            "timeout_ms": data.timeout_ms,
            "stream_usage_mode": data.stream_usage_mode,
            "health_check": health,
            "circuit_breaker": breaker,
            "metrics_url": data.metrics_url,
            "created_at": data.created_at,
            "updated_at": data.updated_at,
        }


class ModelCreate(ApiModel):
    name: ModelName
    upstream_id: uuid.UUID
    upstream_model: str = Field(min_length=1, max_length=400)
    price_input_nano_usd_per_mtok: int | None = Field(default=None, ge=0)
    price_output_nano_usd_per_mtok: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _price_pair(self) -> ModelCreate:
        if (self.price_input_nano_usd_per_mtok is None) != (
            self.price_output_nano_usd_per_mtok is None
        ):
            raise ValueError("price input and output must both be set or both be null")
        return self


class ModelUpdate(ApiModel):
    name: ModelName | None = None
    upstream_id: uuid.UUID | None = None
    upstream_model: str | None = Field(default=None, min_length=1, max_length=400)
    price_input_nano_usd_per_mtok: int | None = Field(default=None, ge=0)
    price_output_nano_usd_per_mtok: int | None = Field(default=None, ge=0)
    keep_old_name_as_alias: bool = Field(
        default=False,
        description="With a new name: add an alias under the old name, with its grants.",
    )


class ModelOut(ReadModel):
    id: uuid.UUID
    name: str
    upstream_id: uuid.UUID
    upstream_model: str
    price_input_nano_usd_per_mtok: int | None
    price_output_nano_usd_per_mtok: int | None
    created_at: datetime
    updated_at: datetime


class AliasCreate(ApiModel):
    name: ModelName
    target: uuid.UUID = Field(description="Id of a model or a router, not another alias.")


class AliasUpdate(ApiModel):
    name: ModelName | None = None
    target: uuid.UUID | None = None


class AliasOut(ReadModel):
    id: uuid.UUID
    name: str
    target: uuid.UUID = Field(validation_alias="target_name_id")
    target_kind: CatalogKind
    target_name: str
    created_at: datetime
    updated_at: datetime


class CatalogEntryOut(ReadModel):
    id: uuid.UUID
    name: str
    kind: CatalogKind


class RetryPolicyIn(ApiModel):
    """Retries happen only before any response byte reached the client."""

    max_attempts: int | None = Field(
        default=None,
        ge=1,
        le=MAX_RETRY_ATTEMPTS,
        description="Total attempts including the first; null = gateway default (2).",
    )
    retry_on_status: list[int] = Field(
        default_factory=list,
        max_length=200,
        description="Upstream statuses that are retried; empty = 429, 502, 503, 504.",
    )
    total_timeout_ms: int = Field(
        default=0, ge=0, le=INT32_MAX, description="Bound for all attempts; 0 = none."
    )

    @field_validator("retry_on_status")
    @classmethod
    def _statuses(cls, value: list[int]) -> list[int]:
        if any(not 400 <= code <= 599 for code in value):
            raise ValueError("retry_on_status entries must be in 400..599")
        if len(set(value)) != len(value):
            raise ValueError("retry_on_status contains duplicates")
        return sorted(value)


class RetryPolicyOut(ReadModel):
    max_attempts: int | None
    retry_on_status: list[int]
    total_timeout_ms: int


Params = Annotated[dict[str, Any], Field(description="Strategy-defined settings.")]


class RouterMemberIn(ApiModel):
    model_id: uuid.UUID
    weight: int = Field(default=1, ge=0, le=INT32_MAX, description="Share under `weighted`.")
    params: Params = Field(default_factory=dict)


class RouterMemberOut(ReadModel):
    model_id: uuid.UUID
    weight: int
    params: dict[str, Any]


class TrafficSplitIn(ApiModel):
    name: str = Field(description="1..32 of [A-Za-z0-9_.-]; a metric label.")
    weight: int = Field(ge=0, le=INT32_MAX, description="Share = weight / sum(weights).")
    members: list[RouterMemberIn] = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not _SPLIT_NAME.fullmatch(value):
            raise ValueError("split name must be 1..32 characters of [A-Za-z0-9_.-]")
        return value

    @field_validator("members")
    @classmethod
    def _unique(cls, value: list[RouterMemberIn]) -> list[RouterMemberIn]:
        ids = [member.model_id for member in value]
        if len(set(ids)) != len(ids):
            raise ValueError("a split lists a model twice")
        return value


class TrafficSplitOut(ReadModel):
    name: str
    weight: int
    members: list[RouterMemberOut]


def _check_splits(value: list[TrafficSplitIn] | None) -> list[TrafficSplitIn] | None:
    if not value:
        return value
    if len(value) > MAX_SPLITS:
        raise ValueError(f"at most {MAX_SPLITS} traffic splits")
    names = [s.name for s in value]
    if len(set(names)) != len(names):
        raise ValueError("split names must be unique")
    if sum(s.weight for s in value) <= 0:
        raise ValueError("split weights must sum to more than 0")
    return value


def _sticky_header(value: str | None) -> str | None:
    if value is None:
        return None
    if not _HEADER_TOKEN.fullmatch(value):
        raise ValueError("sticky_header must be a valid HTTP header name")
    return value.lower()


StickyHeader = Annotated[str, Field(max_length=200)]
Splits = Annotated[list[TrafficSplitIn], AfterValidator(_check_splits)]


StrategyName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")]


class RouterCreate(ApiModel):
    name: ModelName
    strategy: StrategyName = "weighted"
    params: Params = Field(default_factory=dict)
    members: list[RouterMemberIn] = Field(
        default_factory=list,
        description="Ordered models; empty exactly when traffic_split is set.",
    )
    retry: RetryPolicyIn | None = None
    fallback_model_ids: list[uuid.UUID] = Field(
        default_factory=list, description="Models tried in order once the members failed."
    )
    traffic_split: Splits = Field(default_factory=list)
    sticky: StickyBy = StickyBy.NONE
    sticky_header: StickyHeader | None = None

    @field_validator("sticky_header")
    @classmethod
    def _header(cls, value: str | None) -> str | None:
        return _sticky_header(value)


class RouterUpdate(ApiModel):
    """PATCH: omitted fields are unchanged; lists replace the stored list; `retry` and
    `sticky_header` accept null to remove the setting.
    """

    name: ModelName | None = None
    strategy: StrategyName | None = None
    params: Params | None = None
    members: list[RouterMemberIn] | None = None
    retry: RetryPolicyIn | None = None
    fallback_model_ids: list[uuid.UUID] | None = None
    traffic_split: Splits | None = None
    sticky: StickyBy | None = None
    sticky_header: StickyHeader | None = None

    @field_validator("sticky_header")
    @classmethod
    def _header(cls, value: str | None) -> str | None:
        return _sticky_header(value)


class RouterOut(ReadModel):
    id: uuid.UUID
    name: str
    strategy: str
    params: dict[str, Any]
    members: list[RouterMemberOut]
    retry: RetryPolicyOut | None
    fallback_model_ids: list[uuid.UUID]
    traffic_split: list[TrafficSplitOut]
    sticky: StickyBy
    sticky_header: str | None
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------------- users


class UserCreate(ApiModel):
    email: str = Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+$")
    display_name: str = Field(default="", max_length=200)
    is_active: bool = True


class UserUpdate(ApiModel):
    email: str | None = Field(
        default=None, min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+$"
    )
    display_name: str | None = Field(default=None, max_length=200)
    is_active: bool | None = None


class UserOut(ReadModel):
    id: uuid.UUID
    email: str
    display_name: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


class MeTeamOut(ApiModel):
    team_id: uuid.UUID
    team_name: str
    role: TeamRole


class MeOut(ApiModel):
    kind: str
    user_id: uuid.UUID | None
    email: str | None
    display_name: str
    is_admin: bool
    authz_unavailable: bool = False
    manages_grants: bool
    teams: list[MeTeamOut]


class TokenCreate(ApiModel):
    name: Name
    expires_at: datetime | None = None

    @field_validator("expires_at")
    @classmethod
    def _aware_expiry(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("expires_at must include a timezone")
        return _future(value)


class TokenOut(ReadModel):
    id: uuid.UUID
    name: str
    display_prefix: str
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None


class TokenCreated(ApiModel):
    token: TokenOut
    secret: str


# --------------------------------------------------------------------------- usage


class UsageRowOut(ApiModel):
    date: date_type | None = None
    requested: str | None = None
    team_id: uuid.UUID | None = None
    owner_kind: KeyOwnerKind | None = None
    owner_id: uuid.UUID | None = None
    key_id: uuid.UUID | None = None
    requests: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_nano_usd: int
    unknown_cost_count: int


class UsageEventOut(ReadModel):
    """Optional recent-event view (`include_events=1`), never the default list."""

    request_id: uuid.UUID
    ts: datetime
    key_id: uuid.UUID
    team_id: uuid.UUID
    owner_kind: KeyOwnerKind
    owner_id: uuid.UUID
    requested: str
    model: str
    endpoint: str
    status: int
    stream: bool
    prompt_tokens: int
    completion_tokens: int
    cost_nano_usd: int
    usage_state: UsageState
    outcome: RequestOutcome


# --------------------------------------------------------------------------- audit


class AuditOut(ReadModel):
    id: int
    ts: datetime
    actor_type: str
    actor_id: str | None
    action: str
    resource_type: str
    resource_id: str | None
    team_id: uuid.UUID | None
    details: dict[str, Any]


# --------------------------------------------------------------------------- gateways/config


class GatewayTokenCreate(ApiModel):
    gateway_id: str = Field(min_length=1, max_length=200)
    name: str = Field(default="", max_length=200)


class GatewayTokenOut(ReadModel):
    id: uuid.UUID
    gateway_id: str
    name: str
    display_prefix: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class GatewayTokenCreated(ApiModel):
    token: GatewayTokenOut
    secret: str


class GatewayOut(ApiModel):
    gateway_id: str
    connected: bool
    gateway_version: str
    capabilities: list[str]
    acked_version: int | None
    pending_version: int | None
    last_nack_version: int | None
    last_nack_code: str | None
    last_nack_reason: str | None
    quarantined_version: int | None
    connected_at: datetime | None
    disconnected_at: datetime | None
    last_seen_at: datetime | None
    tokens: list[GatewayTokenOut]


class ConfigVersionOut(ApiModel):
    scope: str
    version: int
    digest: str
    size_bytes: int
    key_count: int
    source_revision: int
    kind: str
    rollback_of_version: int | None
    created_at: datetime
    compacted_at: datetime | None


class RollbackCreate(ApiModel):
    version: int = Field(gt=0)


class RollbackOut(ApiModel):
    version: int
