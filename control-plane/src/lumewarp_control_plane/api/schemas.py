"""Pydantic request/response schemas for the admin API.

Inputs reject unknown fields. Outputs are built from ORM rows (`from_attributes=True`) and
never contain hashes or raw secrets.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from datetime import date as date_type
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

from lumewarp_control_plane.compiler.validate import (
    DEFAULT_BODY_RESERVED_KEYS,
    body_object_problem,
    health_url_problem,
    metrics_url_problem,
    reasoning_problem,
    upstream_url_problem,
)
from lumewarp_control_plane.models import (
    CatalogKind,
    InstallationRole,
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


class TeamKeyLimits(ApiModel):
    """Self-service limits for user-owned (member) keys. NULL = unlimited.

    They are source-only: the API enforces the count on create and the lifetime on
    create, rotate and expiry updates. They never enter a snapshot. Service-account keys
    are not member keys and are exempt.
    """

    max_keys_per_member: int | None = Field(default=None, gt=0)
    max_member_key_lifetime_seconds: int | None = Field(default=None, gt=0)


class TeamMemberDefaults(ApiModel):
    """Per-member default limits: an exception overrides them, the team clamps them.

    Source-only like `TeamKeyLimits`; they compile into each member's effective policy
    rather than into a separate level. A team admin may set them; `TEAM_QUOTA_SET` is only
    needed for the team's own totals and model list.
    """

    member_rpm_default: int | None = Field(default=None, gt=0)
    member_tpm_default: int | None = Field(default=None, gt=0)
    member_max_parallel_default: int | None = Field(default=None, gt=0)


class OrgCreate(PolicyFields):
    name: Name


class OrgUpdate(PolicyFields):
    name: Name | None = None


class OrgOut(PolicyOut):
    id: uuid.UUID
    name: str
    created_at: datetime
    updated_at: datetime
    # Present for an installation admin reading an archived org; the list endpoint only
    # returns live orgs.
    deleted_at: datetime | None = None


class OrgAdminOut(ReadModel):
    org_id: uuid.UUID
    user_id: uuid.UUID
    email: str
    display_name: str
    is_active: bool
    assigned_by: uuid.UUID | None
    assigned_at: datetime


class OrgTeamUsageOut(ApiModel):
    """One team's usage inside an org, aggregated from `usage_events` at query time."""

    team_id: uuid.UUID
    team_name: str
    archived: bool
    requests: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_nano_usd: int
    unknown_cost_count: int


class OrgUsageOut(ApiModel):
    org_id: uuid.UUID
    org_name: str
    teams: list[OrgTeamUsageOut]
    requests: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_nano_usd: int
    unknown_cost_count: int


class TeamCreate(PolicyFields, TeamKeyLimits, TeamMemberDefaults):
    name: Name
    org_id: uuid.UUID


class TeamUpdate(PolicyFields, TeamKeyLimits, TeamMemberDefaults):
    name: Name | None = None


class TeamBasicOut(ReadModel, TeamKeyLimits):
    """What any member of a team may read: no team policy.

    A member's effective policy (team narrowed by their own) comes from `/users/me`; a
    member must not see the team's own allowlist, limits or budget. `org_id` is here so a
    client can tell whether the team belongs to an org the caller administers.
    """

    id: uuid.UUID
    org_id: uuid.UUID
    name: str
    created_at: datetime
    updated_at: datetime


class TeamOut(PolicyOut, TeamBasicOut, TeamMemberDefaults):
    """The whole team, including its policy: team admins and installation admins only."""


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
    # False deactivates the account (its keys leave the snapshot); True reactivates it.
    is_active: bool | None = None


class ServiceAccountOut(PolicyOut):
    id: uuid.UUID
    team_id: uuid.UUID
    name: str
    description: str
    is_active: bool
    created_by_user_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class KeyCreate(ApiModel):
    name: Name
    expires_at: datetime | None = None
    # Omit to create a key owned by the caller.
    service_account_id: uuid.UUID | None = None
    # The key level of the hierarchy: no value here means no extra limiter scope. The
    # effective value is capped at the owner's (422 above it); this is enforced by keys.py.
    rpm_limit: int | None = Field(default=None, gt=0)
    tpm_limit: int | None = Field(default=None, gt=0)
    max_parallel: int | None = Field(default=None, gt=0)

    @field_validator("expires_at")
    @classmethod
    def _aware_expiry(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("expires_at must include a timezone")
        return _future(value)


class KeyUpdate(ApiModel):
    """PATCH: omitted fields are unchanged; an explicit null clears a key limit."""

    name: Name | None = None
    expires_at: datetime | None = None
    lifecycle: KeyLifecycle | None = None
    rpm_limit: int | None = Field(default=None, gt=0)
    tpm_limit: int | None = Field(default=None, gt=0)
    max_parallel: int | None = Field(default=None, gt=0)

    @field_validator("expires_at")
    @classmethod
    def _aware_expiry(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("expires_at must include a timezone")
        return value


MAX_ROTATION_GRACE_SECONDS = 24 * 3600


class KeyRotate(ApiModel):
    """Optional body of `rotateKey`. Grace 0 replaces the secret immediately."""

    grace_seconds: int = Field(default=0, ge=0, le=MAX_ROTATION_GRACE_SECONDS)


class KeyOut(ReadModel):
    id: uuid.UUID
    team_id: uuid.UUID
    rpm_limit: int | None
    tpm_limit: int | None
    max_parallel: int | None
    owner_kind: KeyOwnerKind
    user_id: uuid.UUID | None
    service_account_id: uuid.UUID | None
    name: str
    display_prefix: str
    lifecycle: KeyLifecycle
    suspended_reason: str | None
    expires_at: datetime | None
    # End of the rotation grace period for the previous secret; NULL when there is none.
    # The previous hash itself is never exposed.
    previous_key_expires_at: datetime | None
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
    """Active probe: GET <url> every interval; 2xx within timeout = success."""

    url: str | None = Field(
        default=None,
        max_length=2000,
        description=(
            "Absolute http(s) URL; null = vLLM: base_url without /v1 + /health,"
            " others: base_url + /models."
        ),
    )
    interval_ms: int = Field(ge=1000, le=INT32_MAX)
    timeout_ms: int = Field(ge=1, le=INT32_MAX, description="Must be below interval_ms.")
    unhealthy_threshold: int | None = Field(
        default=None, ge=1, le=1000, description="Consecutive failures; null = 3."
    )
    healthy_threshold: int | None = Field(
        default=None, ge=1, le=1000, description="Consecutive successes; null = 1."
    )

    @field_validator("url")
    @classmethod
    def _url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip()
        if problem := health_url_problem(value):
            raise ValueError(f"url {problem}")
        return value

    @model_validator(mode="after")
    def _timeout_below_interval(self) -> HealthCheckIn:
        if self.timeout_ms >= self.interval_ms:
            raise ValueError("timeout_ms must be below interval_ms")
        return self


class HealthCheckOut(ReadModel):
    url: str | None
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


_API_KEY_DOC = (
    "Bearer token, stored sealed with LUMEWARP_ENCRYPTION_KEY and never returned. "
    "Mutually exclusive with api_key_ref."
)


def _api_key(value: SecretStr | None) -> SecretStr | None:
    if value is None:
        return None
    raw = value.get_secret_value()
    # It becomes an HTTP header value on every gateway: visible ASCII only.
    if not all("!" <= c <= "~" for c in raw):
        raise ValueError("api_key must be printable ASCII without spaces")
    return value


class UpstreamCreate(ApiModel):
    name: Name
    kind: UpstreamKind
    base_url: str = Field(min_length=1)
    api_key_ref: str | None = Field(default=None, max_length=200)
    api_key: SecretStr | None = Field(
        default=None, min_length=1, max_length=4096, description=_API_KEY_DOC
    )
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

    @field_validator("api_key")
    @classmethod
    def _check_api_key(cls, value: SecretStr | None) -> SecretStr | None:
        return _api_key(value)

    @field_validator("metrics_url")
    @classmethod
    def _check_metrics_url(cls, value: str | None) -> str | None:
        return _metrics_url(value)

    @model_validator(mode="after")
    def _one_key_source(self) -> UpstreamCreate:
        if self.api_key is not None and self.api_key_ref is not None:
            raise ValueError("set api_key or api_key_ref, not both")
        return self


class UpstreamUpdate(ApiModel):
    """PATCH: omitted fields are unchanged; `health_check`, `circuit_breaker` and
    `metrics_url` accept null to remove the setting. `api_key` replaces the stored key
    (and clears `api_key_ref`); null removes it. Setting `api_key_ref` removes a stored key.
    """

    name: Name | None = None
    kind: UpstreamKind | None = None
    base_url: str | None = Field(default=None, min_length=1)
    api_key_ref: str | None = Field(default=None, max_length=200)
    api_key: SecretStr | None = Field(
        default=None, min_length=1, max_length=4096, description=_API_KEY_DOC
    )
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

    @field_validator("api_key")
    @classmethod
    def _check_api_key(cls, value: SecretStr | None) -> SecretStr | None:
        return _api_key(value)

    @field_validator("metrics_url")
    @classmethod
    def _check_metrics_url(cls, value: str | None) -> str | None:
        return _metrics_url(value)

    @model_validator(mode="after")
    def _one_key_source(self) -> UpstreamUpdate:
        if self.api_key is not None and self.api_key_ref is not None:
            raise ValueError("set api_key or api_key_ref, not both")
        return self


class UpstreamOut(ReadModel):
    id: uuid.UUID
    name: str
    kind: UpstreamKind
    base_url: str
    api_key_ref: str | None
    api_key_set: bool = Field(
        description="A sealed api_key is stored (its value is never returned)."
    )
    timeout_ms: int
    stream_usage_mode: StreamUsageMode
    health_check: HealthCheckOut | None
    circuit_breaker: CircuitBreakerOut | None
    metrics_url: str | None
    health: Literal["healthy", "unhealthy", "unknown", "not_probed"] = Field(
        default="not_probed",
        description=(
            "Active health check as connected gateways report it: unhealthy if any says so,"
            " unknown when no connected gateway has reported it yet."
        ),
    )
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
                url=data.health_check_url,
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
            "api_key_set": data.api_key_ciphertext is not None,
            "timeout_ms": data.timeout_ms,
            "stream_usage_mode": data.stream_usage_mode,
            "health_check": health,
            "circuit_breaker": breaker,
            "metrics_url": data.metrics_url,
            "created_at": data.created_at,
            "updated_at": data.updated_at,
        }


ReasoningLevel = Literal["none", "minimal", "low", "medium", "high", "xhigh"]


def _body_object(value: dict[str, Any] | None, what: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if problem := body_object_problem(value, what, DEFAULT_BODY_RESERVED_KEYS):
        raise ValueError(problem)
    return value


class ReasoningIn(ApiModel):
    """How the OpenAI `reasoning_effort` field is handled for this model."""

    mode: Literal["passthrough", "strip", "mapped"] = Field(
        default="passthrough",
        description=(
            "passthrough: forward it; strip: remove it; mapped: replace it with the"
            " JSON object mapped to its level (e.g. chat_template_kwargs for vLLM)."
        ),
    )
    fragments: dict[ReasoningLevel, dict[str, Any]] | None = Field(
        default=None, description="mapped only: level -> object merged into the request."
    )
    default_level: ReasoningLevel | None = Field(
        default=None, description="Used when the client sends no reasoning_effort."
    )

    @model_validator(mode="after")
    def _shape(self) -> ReasoningIn:
        fragments = self.fragments or None
        if fragments is not None:
            for level, fragment in fragments.items():
                if problem := body_object_problem(fragment, f"fragment {level!r}"):
                    raise ValueError(problem)
        if problem := reasoning_problem(self.mode, fragments, self.default_level):
            raise ValueError(problem)
        self.fragments = fragments if self.mode == "mapped" else None
        return self


class ReasoningOut(ReadModel):
    mode: Literal["passthrough", "strip", "mapped"]
    fragments: dict[str, dict[str, Any]] | None
    default_level: str | None


class _ModelSettings(ApiModel):
    max_input_tokens: int | None = Field(
        default=None, ge=1, le=INT32_MAX, description="Context window; informational."
    )
    max_output_tokens: int | None = Field(
        default=None,
        ge=1,
        le=INT32_MAX,
        description="Requests asking for more max_tokens / max_completion_tokens get a 400.",
    )
    default_body: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Merged under each request body: the client's fields win, objects on both"
            " sides merge. Must not set model, stream or stream_options."
        ),
    )
    reasoning: ReasoningIn | None = Field(default=None, description="null = passthrough.")

    @field_validator("default_body")
    @classmethod
    def _check_default_body(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return _body_object(value, "default_body") or None


class ModelCreate(_ModelSettings):
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


class ModelUpdate(_ModelSettings):
    """PATCH: omitted fields are unchanged; the limits, default_body and reasoning accept
    null to remove the setting.
    """

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
    max_input_tokens: int | None
    max_output_tokens: int | None
    default_body: dict[str, Any] | None
    reasoning: ReasoningOut
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="before")
    @classmethod
    def _from_row(cls, data: Any) -> Any:
        if isinstance(data, dict) or not hasattr(data, "reasoning_mode"):
            return data
        return {
            "id": data.id,
            "name": data.name,
            "upstream_id": data.upstream_id,
            "upstream_model": data.upstream_model,
            "price_input_nano_usd_per_mtok": data.price_input_nano_usd_per_mtok,
            "price_output_nano_usd_per_mtok": data.price_output_nano_usd_per_mtok,
            "max_input_tokens": data.max_input_tokens,
            "max_output_tokens": data.max_output_tokens,
            "default_body": data.default_body,
            "reasoning": ReasoningOut(
                mode=data.reasoning_mode,
                fragments=data.reasoning_fragments,
                default_level=data.reasoning_default_level,
            ),
            "created_at": data.created_at,
            "updated_at": data.updated_at,
        }


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


def _force_field() -> Any:
    # default_factory: an OpenAPI `default` would make it required in the generated UI client.
    return Field(
        default_factory=lambda: False,
        description="Save although connected gateways lack the strategy; they will NACK.",
    )


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
    force: bool = _force_field()

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
    force: bool = _force_field()

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
    config_invalid: bool = Field(
        description="Stored settings fail the strategy's current schema, or nothing reports it."
    )
    config_problems: list[str]
    created_at: datetime
    updated_at: datetime


class StrategyGatewayOut(ReadModel):
    gateway_id: str
    connected: bool
    digest: str | None = Field(description="Null when known only from its capabilities.")
    seen_at: datetime | None


class StrategyOut(ReadModel):
    """Everything but `name` and `builtin` is data from a gateway: render it as plain text."""

    name: str
    title: str
    description: str
    uses_weight: bool
    needs: list[str]
    params_schema: dict[str, Any] | None = Field(
        description="Null when unusable or not reported: settings then cannot be validated here."
    )
    member_schema: dict[str, Any] | None
    schema_problems: list[str] = Field(description="Why a schema reported is not usable.")
    builtin: bool
    gateways: list[StrategyGatewayOut] = Field(description="Gateways that have it, newest first.")
    missing_gateways: list[str] = Field(description="Connected gateways that do not have it.")


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


class InstallationAdminOut(ReadModel):
    user_id: uuid.UUID
    email: str
    display_name: str
    is_active: bool
    role: InstallationRole
    assigned_by: uuid.UUID | None
    assigned_at: datetime


class InstallationAdminAssign(ApiModel):
    """Only `admin` can be assigned here; proxy_admin moves through transfer."""

    role: InstallationRole = InstallationRole.ADMIN


class InstallationAdminTransfer(ApiModel):
    user_id: uuid.UUID


class MeLimitsOut(ApiModel):
    """Effective rate limits for a key the caller could create in this team."""

    rpm_limit: int | None
    tpm_limit: int | None
    max_parallel: int | None


class MeKeyLimitsOut(ApiModel):
    """The team's member-key limits and the caller's current usage of the count."""

    max_keys_per_member: int | None
    max_member_key_lifetime_seconds: int | None
    # Non-revoked user-owned keys the caller has in this team.
    key_count: int


class MeBudgetOut(ApiModel):
    """Per-scope monthly budgets and spend for the caller's key in one team.

    The team scope (`team_budget_nano_usd`, `team_spent_nano_usd`) is the team-admin view:
    it is NULL for a plain member and filled for a team admin or an installation admin.
    `remaining_nano_usd` is still the minimum across every scope that sets a budget, so a
    member sees the effective limit without learning the team's own numbers.
    """

    org_budget_nano_usd: int | None = Field(
        default=None,
        description=(
            "The org's monthly budget; NULL when the caller is not a team admin "
            "(or installation admin) or the org sets none."
        ),
    )
    org_spent_nano_usd: int | None = Field(
        default=None,
        description=(
            "The org's spend this month (sum of its teams); NULL when the caller is not a "
            "team admin (or installation admin)."
        ),
    )
    team_budget_nano_usd: int | None = Field(
        description=(
            "The team's monthly budget; NULL when the caller is not a team admin "
            "(or installation admin) or the team sets none."
        ),
    )
    team_spent_nano_usd: int | None = Field(
        description=(
            "The team's spend this month; NULL when the caller is not a team admin "
            "(or installation admin)."
        ),
    )
    member_budget_nano_usd: int | None = Field(
        description="The caller's own member budget in this team; NULL when none is set.",
    )
    member_spent_nano_usd: int = Field(
        description="The caller's spend this month in this team.",
    )
    remaining_nano_usd: int | None = Field(
        description=(
            "MIN over the scopes that set a budget; NULL when none does. Computed "
            "server-side even when a team value is not disclosed."
        ),
    )
    window_start: datetime
    window_end: datetime


class MeTeamOut(ApiModel):
    team_id: uuid.UUID
    team_name: str
    role: TeamRole
    # Effective names the caller may call, already narrowed by their member policy. For a
    # universal grant this lists the current catalog; `all_models` also covers names added
    # later. Never the team's full list when the member is narrower.
    models: list[str]
    all_models: bool
    limits: MeLimitsOut
    key_limits: MeKeyLimitsOut
    budget: MeBudgetOut


class MeOrgOut(ApiModel):
    """An org the caller administers directly (`admin_orgs`).

    Its teams are administered by inheritance and are not mixed into `MeOut.teams`,
    which lists direct memberships only.
    """

    id: uuid.UUID
    name: str


class MeOut(ApiModel):
    kind: str
    user_id: uuid.UUID | None
    email: str | None
    display_name: str
    # Compatibility flag: true for proxy_admin, admin and break-glass.
    is_admin: bool
    installation_role: InstallationRole | None = None
    authz_unavailable: bool = False
    manages_grants: bool
    # Direct memberships only; an org admin's other teams come through `admin_orgs`.
    teams: list[MeTeamOut]
    # Orgs the caller administers. An installation admin administers every org and does
    # not need this list; break-glass has no user and gets an empty one.
    admin_orgs: list[MeOrgOut] = Field(default_factory=list)


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


class UsageNameOut(ApiModel):
    """Current catalog name, or the recorded spelling if the item no longer exists."""

    id: uuid.UUID | None = None
    name: str


class UsageRowOut(ApiModel):
    """Aggregated usage with current catalog names and immutable upstream names.

    Singular model/alias/router/upstream_model fields are set for one distinct
    value; mixed groups use lists. Empty string means no provenance; null means
    mixed/not collected. Lists omit empty values. Model grouping collects all refs;
    alias/router grouping collects its refs and Served by; upstream-model grouping
    collects Served by. Default/scope-only grouping does not collect catalog refs.
    """

    date: date_type | None = None
    requested: str | None = None
    model: str | None = None
    model_id: uuid.UUID | None = None
    upstream_model: str | None = None
    alias: str | None = None
    alias_id: uuid.UUID | None = None
    router: str | None = None
    router_id: uuid.UUID | None = None
    upstream_models: list[str] = Field(default_factory=list)
    via_aliases: list[UsageNameOut] = Field(default_factory=list)
    via_routers: list[UsageNameOut] = Field(default_factory=list)
    served_by: list[UsageNameOut] = Field(default_factory=list)
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
    model_id: uuid.UUID | None = None
    alias: str
    alias_id: uuid.UUID | None = None
    router: str
    router_id: uuid.UUID | None = None
    upstream: str
    upstream_model: str
    fallback: bool
    attempts: int
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
    org_id: uuid.UUID | None
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
    force: bool = _force_field()


class RollbackOut(ApiModel):
    version: int


class UpstreamModelInfoOut(ReadModel):
    id: str
    max_input_tokens: int | None = Field(
        description=(
            "Context window the upstream reports (vLLM max_model_len, or context_length /"
            " context_window / max_input_tokens); null when it reports none."
        )
    )
    max_output_tokens: int | None = Field(
        description=(
            "Output limit the upstream reports (max_output_tokens / max_completion_tokens);"
            " otherwise the context window, which no completion can exceed; null when"
            " neither is reported."
        )
    )


class UpstreamModelsOut(ReadModel):
    models: list[UpstreamModelInfoOut]


class ProbeOut(ReadModel):
    url: str
    ok: bool = Field(description="2xx within the timeout.")
    status: int | None = Field(description="HTTP status; null when no response arrived.")
    latency_ms: int
    error: str | None = Field(description="Why no response arrived (never the response body).")


class UpstreamTestIn(ApiModel):
    """The upstream form as it stands, saved or not."""

    kind: UpstreamKind
    base_url: str = Field(min_length=1)
    api_key: SecretStr | None = Field(
        default=None,
        min_length=1,
        max_length=4096,
        description="A key typed in the form; used once, never stored.",
    )
    api_key_ref: str | None = Field(default=None, max_length=200)
    upstream_id: uuid.UUID | None = Field(
        default=None,
        description="A saved upstream whose stored key is used when api_key is not given.",
    )
    health_check_url: str | None = Field(default=None, max_length=2000)

    @field_validator("base_url")
    @classmethod
    def _http_url(cls, value: str) -> str:
        return _base_url(value)  # type: ignore[return-value]

    @field_validator("api_key_ref")
    @classmethod
    def _check_api_key_ref(cls, value: str | None) -> str | None:
        return _env_name(value)

    @field_validator("api_key")
    @classmethod
    def _check_api_key(cls, value: SecretStr | None) -> SecretStr | None:
        return _api_key(value)

    @field_validator("health_check_url")
    @classmethod
    def _check_health_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip()
        if problem := health_url_problem(value):
            raise ValueError(f"health_check_url {problem}")
        return value

    @model_validator(mode="after")
    def _one_key_source(self) -> UpstreamTestIn:
        if self.api_key is not None and self.api_key_ref is not None:
            raise ValueError("set api_key or api_key_ref, not both")
        return self


class UpstreamTestOut(ReadModel):
    credential: Literal["entered", "stored", "env", "env_unset", "none"] = Field(
        description=(
            "The bearer token used: typed in the form, the saved upstream's stored key, the"
            " control plane's own env var, that env var missing here, or none."
        )
    )
    health: ProbeOut
    models: ProbeOut
