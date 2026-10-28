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
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from lumewarp_control_plane.models import (
    KeyLifecycle,
    KeyOwnerKind,
    ModelAccess,
    RequestOutcome,
    RoutingStrategy,
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
    model_config = ConfigDict(extra="forbid")


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorBody(BaseModel):
    """The `{error: {code, message}}` shape every non-2xx response uses."""

    error: ErrorDetail


class ReadModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


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


class UpstreamCreate(ApiModel):
    name: Name
    kind: UpstreamKind
    base_url: str = Field(min_length=1)
    api_key_ref: str | None = Field(default=None, max_length=200)
    timeout_ms: int = Field(default=0, ge=0)
    stream_usage_mode: StreamUsageMode = StreamUsageMode.INCLUDE_USAGE

    @field_validator("base_url")
    @classmethod
    def _http_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError("base_url must be an absolute http(s) URL")
        if parts.query or parts.fragment or parts.username or parts.password:
            raise ValueError("base_url must not contain userinfo, query or fragment")
        return value.rstrip("/")

    @field_validator("api_key_ref")
    @classmethod
    def _check_api_key_ref(cls, value: str | None) -> str | None:
        return _env_name(value)


class UpstreamUpdate(ApiModel):
    name: Name | None = None
    kind: UpstreamKind | None = None
    base_url: str | None = Field(default=None, min_length=1)
    api_key_ref: str | None = Field(default=None, max_length=200)
    timeout_ms: int | None = Field(default=None, ge=0)
    stream_usage_mode: StreamUsageMode | None = None

    @field_validator("base_url")
    @classmethod
    def _http_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError("base_url must be an absolute http(s) URL")
        if parts.query or parts.fragment or parts.username or parts.password:
            raise ValueError("base_url must not contain userinfo, query or fragment")
        return value.rstrip("/")

    @field_validator("api_key_ref")
    @classmethod
    def _check_api_key_ref(cls, value: str | None) -> str | None:
        return _env_name(value)


class UpstreamOut(ReadModel):
    id: uuid.UUID
    name: str
    kind: UpstreamKind
    base_url: str
    api_key_ref: str | None
    timeout_ms: int
    stream_usage_mode: StreamUsageMode
    created_at: datetime
    updated_at: datetime


class DeploymentCreate(ApiModel):
    name: Name
    upstream_id: uuid.UUID
    upstream_model: str = Field(min_length=1, max_length=400)
    weight: int = Field(default=1, ge=0)
    price_input_nano_usd_per_mtok: int | None = Field(default=None, ge=0)
    price_output_nano_usd_per_mtok: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _price_pair(self) -> DeploymentCreate:
        if (self.price_input_nano_usd_per_mtok is None) != (
            self.price_output_nano_usd_per_mtok is None
        ):
            raise ValueError("price input and output must both be set or both be null")
        return self


class DeploymentUpdate(ApiModel):
    name: Name | None = None
    upstream_id: uuid.UUID | None = None
    upstream_model: str | None = Field(default=None, min_length=1, max_length=400)
    weight: int | None = Field(default=None, ge=0)
    price_input_nano_usd_per_mtok: int | None = Field(default=None, ge=0)
    price_output_nano_usd_per_mtok: int | None = Field(default=None, ge=0)


class DeploymentOut(ReadModel):
    id: uuid.UUID
    name: str
    upstream_id: uuid.UUID
    upstream_model: str
    weight: int
    price_input_nano_usd_per_mtok: int | None
    price_output_nano_usd_per_mtok: int | None
    created_at: datetime
    updated_at: datetime


class RouteCreate(ApiModel):
    model: ModelName
    strategy: RoutingStrategy = RoutingStrategy.WEIGHTED
    deployment_ids: list[uuid.UUID] = Field(min_length=1)


class RouteUpdate(ApiModel):
    model: ModelName | None = None
    strategy: RoutingStrategy | None = None
    deployment_ids: list[uuid.UUID] | None = Field(default=None, min_length=1)


class RouteDeploymentOut(ReadModel):
    deployment_id: uuid.UUID
    position: int


class RouteOut(ReadModel):
    id: uuid.UUID
    model: str
    strategy: RoutingStrategy
    deployments: list[RouteDeploymentOut]
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------------- users


class UserCreate(ApiModel):
    email: str = Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+$")
    display_name: str = Field(default="", max_length=200)
    is_superuser: bool = False
    is_active: bool = True


class UserUpdate(ApiModel):
    email: str | None = Field(
        default=None, min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+$"
    )
    display_name: str | None = Field(default=None, max_length=200)
    is_superuser: bool | None = None
    is_active: bool | None = None


class UserOut(ReadModel):
    id: uuid.UUID
    email: str
    display_name: str
    is_superuser: bool
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
    is_superuser: bool
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
    model: str | None = None
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
    model: str
    deployment: str
    route: str
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
