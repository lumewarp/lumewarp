"""Pydantic request/response schemas for the admin API.

Inputs reject unknown fields. Outputs are built from ORM rows (`from_attributes=True`) and
never contain hashes or raw secrets.
"""

from __future__ import annotations

import json
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

from lumewarp_control_plane.api import field_docs
from lumewarp_control_plane.compiler.validate import (
    DEFAULT_BODY_RESERVED_KEYS,
    body_object_problem,
    card_url_problem,
    health_url_problem,
    metrics_url_problem,
    reasoning_problem,
    target_url_problem,
    upstream_url_problem,
)
from lumewarp_control_plane.models import (
    CatalogKind,
    InheritedFrom,
    InstallationRole,
    KeyLifecycle,
    KeyOwnerKind,
    McpDefinitionKind,
    McpDefinitionState,
    McpReviewMode,
    ModelAccess,
    ModelType,
    PluginKind,
    RequestOutcome,
    StickyBy,
    StreamUsageMode,
    TeamRole,
    TrafficKind,
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
    model_config = ConfigDict(
        extra="forbid", protected_namespaces=(), json_schema_extra=field_docs.describe
    )


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


# `^[a-z][a-z0-9_-]{0,62}$`: MCP server and A2A agent names (snapshot.proto, L4).
_TARGET_NAME = re.compile(r"[a-z][a-z0-9_-]{0,62}")


def _target_name(value: str) -> str:
    """A server or agent name: the gateway's own rule, so a grant always resolves."""
    if not _TARGET_NAME.fullmatch(value):
        raise ValueError("must match ^[a-z][a-z0-9_-]{0,62}$")
    if "__" in value:
        raise ValueError("must not contain '__' (the tool name separator)")
    return value


def _tool_name(value: str) -> str:
    """An upstream tool name, exactly as reported: no control characters, 1..=256 bytes."""
    if not value or value != value.strip():
        raise ValueError("tool names must be non-empty and contain no surrounding whitespace")
    if any(char < " " or char == "\x7f" for char in value):
        raise ValueError("tool names must contain no control characters")
    if len(value.encode("utf-8")) > 256:
        raise ValueError("tool names are at most 256 bytes")
    return value


class McpGrantIn(ApiModel):
    """One MCP server grant: every approved tool, or exactly `tools`."""

    server: Annotated[str, AfterValidator(_target_name)]
    all_tools: bool = False
    # Ignored when all_tools; upstream tool names, without the `<server>__` prefix.
    tools: list[Annotated[str, AfterValidator(_tool_name)]] = Field(default_factory=list)


class McpGrantOut(ReadModel):
    server: str
    all_tools: bool
    tools: list[str]


class PolicyFields(ApiModel):
    """Writable policy columns shared by teams, team members and service accounts.

    `model_access`/`allowed_models`, `mcp_access`/`mcp_servers` and
    `a2a_access`/`a2a_agents` cannot be null; limits and budgets can be set to null to
    clear them. On update, omitted fields keep their current value. MCP and A2A access
    mirror model access: `inherit` keeps the parent's set, `all` grants everything the
    parent allows, `list` grants exactly these rows (empty = deny all).
    """

    model_access: ModelAccess | None = None
    allowed_models: list[str] | None = None
    mcp_access: ModelAccess | None = None
    mcp_servers: list[McpGrantIn] | None = None
    a2a_access: ModelAccess | None = None
    a2a_agents: list[Annotated[str, AfterValidator(_target_name)]] | None = None
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

    @field_validator("mcp_servers")
    @classmethod
    def _merge_servers(cls, value: list[McpGrantIn] | None) -> list[McpGrantIn] | None:
        """A repeated server merges (`all_tools` ORs, tools union), as the gateway reads it."""
        if value is None:
            return None
        merged: dict[str, McpGrantIn] = {}
        for grant in value:
            current = merged.get(grant.server)
            if current is None:
                merged[grant.server] = grant
                continue
            current.all_tools = current.all_tools or grant.all_tools
            if not current.all_tools:
                current.tools = list(dict.fromkeys([*current.tools, *grant.tools]))
        return list(merged.values())

    @field_validator("a2a_agents")
    @classmethod
    def _clean_agents(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else list(dict.fromkeys(value))

    @model_validator(mode="after")
    def _non_nullable_fields(self) -> PolicyFields:
        for name in ("model_access", "mcp_access", "a2a_access"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        for name in ("allowed_models", "mcp_servers", "a2a_agents"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null; send [] to deny all")
        return self


class PolicyOut(ReadModel):
    model_access: ModelAccess
    allowed_models: list[str]
    mcp_access: ModelAccess
    mcp_servers: list[McpGrantOut]
    a2a_access: ModelAccess
    a2a_agents: list[str]
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
    """An org admin: a local `org_admins` row, or an installation admin listed by inheritance.

    `inherited_from` is `installation` for an installation admin (who administers every
    org). Without a local row (`direct` false) it is read-only here, and `assigned_by`/
    `assigned_at` are those of the installation role.
    """

    org_id: uuid.UUID
    user_id: uuid.UUID
    email: str
    display_name: str
    is_active: bool
    assigned_by: uuid.UUID | None
    assigned_at: datetime
    inherited_from: InheritedFrom | None = None
    # False when the user has no local `org_admins` row: nothing to remove here.
    direct: bool = True


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
    """A direct member, or an admin of the team by inheritance (`inherited_from`).

    An inherited-only row has role `admin`, no membership to edit or remove (it changes at
    the org or installation level), and its timestamps are those of the inherited role.
    A direct member who also inherits the role keeps their direct row with `inherited_from`
    set; removing the membership then leaves their keys in place.
    """

    team_id: uuid.UUID
    user_id: uuid.UUID
    email: str
    display_name: str
    user_active: bool
    role: TeamRole
    member_scope_id: uuid.UUID
    created_at: datetime
    updated_at: datetime
    inherited_from: InheritedFrom | None = None
    # False for an inherited-only row: there is no membership to edit or remove.
    direct: bool = True


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

    interval_ms: int = Field(ge=1000, le=INT32_MAX)
    timeout_ms: int = Field(ge=1, le=INT32_MAX, description="Must be below interval_ms.")
    unhealthy_threshold: int | None = Field(
        default=None, ge=1, le=1000, description="Consecutive failures; null = 3."
    )
    healthy_threshold: int | None = Field(
        default=None, ge=1, le=1000, description="Consecutive successes; null = 1."
    )

    @model_validator(mode="after")
    def _timeout_below_interval(self) -> HealthCheckIn:
        if self.timeout_ms >= self.interval_ms:
            raise ValueError("timeout_ms must be below interval_ms")
        return self


class HealthCheckOut(ReadModel):
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


def _health_url(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    value = value.strip()
    if problem := health_url_problem(value):
        raise ValueError(f"health_url {problem}")
    return value


class UpstreamCreate(ApiModel):
    health_url: str | None = Field(default=None, max_length=2000)

    _check_health_url = field_validator("health_url")(_health_url)
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

    health_url: str | None = Field(default=None, max_length=2000)

    _check_health_url = field_validator("health_url")(_health_url)

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


class ProbeResultOut(ReadModel):
    """The last control-plane Test now of the saved settings (not a gateway check)."""

    at: datetime
    ok: bool
    ok_at: datetime | None = Field(description="The last successful one, if any.")


def _probe(row: Any) -> ProbeResultOut | None:
    if row.probe_at is None or row.probe_ok is None:
        return None
    return ProbeResultOut(at=row.probe_at, ok=row.probe_ok, ok_at=row.probe_ok_at)


class UpstreamOut(ReadModel):
    health_url: str | None
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
    probe: ProbeResultOut | None = Field(
        default=None,
        description="The last Test now of the saved settings from the control plane, if any.",
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
            "health_url": data.health_url,
            "health_check": health,
            "circuit_breaker": breaker,
            "metrics_url": data.metrics_url,
            "probe": _probe(data),
            "created_at": data.created_at,
            "updated_at": data.updated_at,
        }


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
    fragments: dict[str, dict[str, Any]] | None = Field(
        default=None,
        description=(
            "mapped only: 1–16 levels mapped to request objects. Level names match"
            " ^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$."
        ),
    )
    default_level: str | None = Field(
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


class ChatCheckIn(ApiModel):
    interval_ms: int = Field(ge=1000, le=INT32_MAX)
    timeout_ms: int = Field(ge=1, le=INT32_MAX)
    unhealthy_threshold: int | None = Field(
        default=None, ge=1, le=1000, description="Consecutive failures; null = 3."
    )
    healthy_threshold: int | None = Field(
        default=None, ge=1, le=1000, description="Consecutive successes; null = 1."
    )

    @model_validator(mode="after")
    def _timeout_below_interval(self) -> ChatCheckIn:
        if self.timeout_ms >= self.interval_ms:
            raise ValueError("timeout_ms must be below interval_ms")
        return self


class ChatCheckOut(ReadModel):
    interval_ms: int
    timeout_ms: int
    unhealthy_threshold: int | None
    healthy_threshold: int | None


HealthState = Literal["healthy", "unhealthy", "unknown", "not_probed"]


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
    chat_check: ChatCheckIn | None = Field(
        default=None,
        description=(
            "Periodic type-specific probe sent by every gateway through this model;"
            " while it fails the model gets no traffic. null = not probed (Test now still"
            " works). Probes may incur upstream charges."
        ),
    )

    @field_validator("default_body")
    @classmethod
    def _check_default_body(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return _body_object(value, "default_body") or None


class ModelCreate(_ModelSettings):
    model_type: ModelType
    price_nano_usd_per_audio_second: int | None = Field(default=None, ge=0)
    name: ModelName
    upstream_id: uuid.UUID
    upstream_model: str = Field(min_length=1, max_length=400)
    price_input_nano_usd_per_mtok: int | None = Field(default=None, ge=0)
    price_output_nano_usd_per_mtok: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _price_pair(self) -> ModelCreate:
        validate_model_settings(self)
        return self


def validate_model_settings(value: Any) -> None:
    """Validate the complete desired state, including merged PATCH values.

    Unknown pricing is all NULL. Transcription allows a token pair, audio pricing,
    both, or neither; embedding allows input alone (or an explicit zero output).
    """
    kind = value.model_type
    pi = value.price_input_nano_usd_per_mtok
    po = value.price_output_nano_usd_per_mtok
    if kind == "embedding":
        if po not in (None, 0) or (pi is None and po is not None):
            raise ValueError("embedding pricing needs input and output must be null or zero")
    elif (pi is None) != (po is None):
        raise ValueError("price input and output must both be set or both be null")
    if kind != "transcription" and value.price_nano_usd_per_audio_second is not None:
        raise ValueError("audio price is only allowed on transcription models")
    if kind == "transcription" and value.default_body is not None:
        raise ValueError("default_body is not allowed on transcription models")
    reasoning = getattr(value, "reasoning", None)
    mode = reasoning.mode if reasoning else getattr(value, "reasoning_mode", "passthrough")
    fragments = reasoning.fragments if reasoning else getattr(value, "reasoning_fragments", None)
    level = (
        reasoning.default_level if reasoning else getattr(value, "reasoning_default_level", None)
    )
    if kind != "llm" and (
        value.max_output_tokens is not None or mode != "passthrough" or fragments or level
    ):
        raise ValueError("reasoning and max_output_tokens are only allowed on llm models")


class ModelUpdate(_ModelSettings):
    """PATCH: omitted fields are unchanged; the limits, default_body and reasoning accept
    null to remove the setting.
    """

    model_type: ModelType | None = None
    price_nano_usd_per_audio_second: int | None = Field(default=None, ge=0)
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
    model_type: ModelType
    price_nano_usd_per_audio_second: int | None
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
    chat_check: ChatCheckOut | None
    health: HealthState = Field(
        default="not_probed",
        description=(
            "Chat check as connected gateways report it: unhealthy if any says so, unknown"
            " when none has reported it yet."
        ),
    )
    probe: ProbeResultOut | None = Field(
        default=None,
        description="The last Test now of the saved settings from the control plane, if any.",
    )
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
            "model_type": data.model_type,
            "price_nano_usd_per_audio_second": data.price_nano_usd_per_audio_second,
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
            "chat_check": None
            if data.chat_check_interval_ms is None
            else ChatCheckOut(
                interval_ms=data.chat_check_interval_ms,
                timeout_ms=data.chat_check_timeout_ms,
                unhealthy_threshold=data.chat_check_unhealthy_threshold,
                healthy_threshold=data.chat_check_healthy_threshold,
            ),
            "probe": _probe(data),
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
    model_type: ModelType | None = None
    id: uuid.UUID
    name: str
    target: uuid.UUID = Field(validation_alias="target_name_id")
    target_kind: CatalogKind
    target_name: str
    created_at: datetime
    updated_at: datetime


class CatalogEntryOut(ReadModel):
    model_type: ModelType | None = None
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
        description="Save although connected gateways lack the plugin; they will NACK.",
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
    # WASM strategy only: how long one `pick` may run; 0 = the gateway default (60 s).
    plugin_timeout_ms: int = Field(default=0, ge=0, le=INT32_MAX)
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
    plugin_timeout_ms: int | None = Field(default=None, ge=0, le=INT32_MAX)
    force: bool = _force_field()

    @field_validator("sticky_header")
    @classmethod
    def _header(cls, value: str | None) -> str | None:
        return _sticky_header(value)


class RouterOut(ReadModel):
    model_type: ModelType | None = None
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
    plugin_timeout_ms: int = Field(
        description="One strategy call's timeout in ms; 0 = the gateway's 60 s default."
    )
    config_invalid: bool = Field(
        description="Stored settings fail the strategy's current schema, or nothing reports it."
    )
    config_problems: list[str]
    created_at: datetime
    updated_at: datetime


class PluginGatewayOut(ReadModel):
    gateway_id: str
    connected: bool
    digest: str | None = Field(description="Null when known only from its capabilities.")
    seen_at: datetime | None


class PluginOut(ReadModel):
    """A gateway plugin of any kind (docs/plugins.md).

    Everything but `kind`, `name` and `builtin` is data from a gateway: render it as plain
    text. `uses_weight`, `needs` and `member_schema` are for router strategies only.
    """

    kind: PluginKind
    name: str
    title: str
    description: str
    builtin: bool
    uses_weight: bool
    needs: list[str]
    params_schema: dict[str, Any] | None = Field(
        description="Null when unusable or not reported: settings then cannot be validated here."
    )
    member_schema: dict[str, Any] | None
    schema_problems: list[str] = Field(description="Why a schema reported is not usable.")
    wasm_sha256: str = Field(description="Hex sha256 of the loaded file; empty for built-ins.")
    gateways: list[PluginGatewayOut] = Field(description="Gateways that have it, newest first.")
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


class MeMcpGrantOut(ApiModel):
    """One MCP server the caller's key in this team could reach, and the tools on it."""

    server: str
    all_tools: bool
    tools: list[str] = Field(
        description="Upstream tool names (without the prefix); empty when all_tools."
    )


class EffectiveModelOut(ApiModel):
    name: str
    model_type: ModelType | None


class MeTeamOut(ApiModel):
    typed_models: list[EffectiveModelOut] = Field(default_factory=list)
    team_id: uuid.UUID
    team_name: str
    # The role the caller acts with: `admin` for a direct admin and for an inherited one.
    role: TeamRole
    # Set when the caller administers the team from above (an installation admin, or an
    # admin of the team's org), whether or not they are also a direct member.
    inherited_from: InheritedFrom | None = None
    # Whether the caller holds a direct membership; an inherited-only team is false.
    direct: bool = True
    # Effective names the caller may call, already narrowed by their member policy. For a
    # universal grant this lists the current catalog; `all_models` also covers names added
    # later. Never the team's full list when the member is narrower.
    models: list[str]
    all_models: bool
    # MCP and A2A access, resolved by the same one resolver as `models`: universal means
    # every live server/agent and is also true for ones added later.
    mcp_servers: list[MeMcpGrantOut]
    all_mcp: bool
    agents: list[str]
    all_agents: bool
    limits: MeLimitsOut
    key_limits: MeKeyLimitsOut
    budget: MeBudgetOut


class MeOrgOut(ApiModel):
    """An org the caller administers directly (`admin_orgs`).

    Its teams are administered by inheritance and appear in `MeOut.teams` with
    `inherited_from` set.
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
    # Every team the caller may own keys in: direct memberships, plus every team an
    # installation admin or org admin administers by inheritance (`inherited_from`).
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

    `error_count` and `total_duration_ms` describe how the group's requests ended
    (an error outcome, or a 4xx/5xx status that is not a client cancellation;
    summed duration, so the mean is the sum over `requests`). The LLM tabs
    ignore them; the MCP and A2A tabs read them in place of tokens and cost. The
    monthly totals fast path (LLM requests only) reports both as 0.
    """

    endpoint: str | None = None
    model_type: ModelType | None = None
    audio_ms: int = 0
    date: date_type | None = None
    kind: TrafficKind | None = None
    target: str | None = None
    method: str | None = None
    tool: str | None = None
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
    error_count: int = 0
    total_duration_ms: int = 0


class UsageEventOut(ReadModel):
    """Optional recent-event view (`include_events=1`), never the default list."""

    audio_ms: int

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
        description=(
            "A saved upstream: its stored key is used when no other is given, and the"
            " result is kept on it when the form matches it."
        ),
    )
    health_url: str | None = Field(default=None, max_length=2000)

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

    _check_health_url = field_validator("health_url")(_health_url)

    @model_validator(mode="after")
    def _one_key_source(self) -> UpstreamTestIn:
        if self.api_key is not None and self.api_key_ref is not None:
            raise ValueError("set api_key or api_key_ref, not both")
        return self


class ModelTestIn(ApiModel):
    """The model form as it stands, saved or not."""

    model_type: ModelType

    upstream_id: uuid.UUID
    upstream_model: str = Field(min_length=1, max_length=400)
    default_body: dict[str, Any] | None = None
    model_id: uuid.UUID | None = Field(
        default=None,
        description="A saved model; the result is kept on it when the form matches it.",
    )

    @field_validator("default_body")
    @classmethod
    def _check_default_body(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return _body_object(value, "default_body") or None

    @model_validator(mode="after")
    def _type_settings(self) -> ModelTestIn:
        if self.model_type == ModelType.TRANSCRIPTION and self.default_body is not None:
            raise ValueError("default_body is not allowed on transcription models")
        return self


class ModelTestOut(ReadModel):
    credential: Literal["stored", "env", "env_unset", "none"]
    probe: ProbeOut
    reply: str | None = Field(
        description="The start of the model's answer (at most 200 characters), on success."
    )


class UpstreamTestOut(ReadModel):
    credential: Literal["entered", "stored", "env", "env_unset", "none"] = Field(
        description=(
            "The bearer token used: typed in the form, the saved upstream's stored key, the"
            " control plane's own env var, that env var missing here, or none."
        )
    )
    probe: ProbeOut


# --------------------------------------------------------------------------- mcp / a2a


def _no_separator(value: str) -> str:
    """L4: the aggregated endpoint splits a tool name at the first "__", so a server or
    agent name may not contain one (the database enforces the same rule).
    """
    if "__" in value:
        raise ValueError('must not contain "__" (the tool name separator)')
    return value


# `^[a-z][a-z0-9_-]{0,62}$` without "__": the gateway's rule for a server or agent name.
TargetName = Annotated[
    str, Field(pattern=r"^[a-z][a-z0-9_-]{0,62}$"), AfterValidator(_no_separator)
]
AuthPluginName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")]
MAX_CARD_BYTES = 64 * 1024
MAX_TIMEOUT_MS = 10 * 60 * 1000


def _target_url(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if problem := target_url_problem(value):
        raise ValueError(f"url {problem}")
    return value


def _card_url(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    value = value.strip()
    if problem := card_url_problem(value):
        raise ValueError(f"card_url {problem}")
    return value


def _card_json(value: str | None) -> str | None:
    """A pasted Agent Card: a JSON object of at most 64 KiB, stored as text."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return ""
    if len(value.encode("utf-8")) > MAX_CARD_BYTES:
        raise ValueError("card_json exceeds 64 KiB")
    try:
        card = json.loads(value)
    except (ValueError, RecursionError) as exc:
        raise ValueError("card_json must be a JSON object") from exc
    if not isinstance(card, dict):
        raise ValueError("card_json must be a JSON object")
    return value


_SECRET_DOC = (
    "Bearer token for static_bearer, stored sealed with LUMEWARP_ENCRYPTION_KEY under the"
    " upstream-auth info and never returned. Mutually exclusive with secret_ref."
)


class TargetAuthIn(ApiModel):
    """The outbound auth of an MCP server or A2A agent (proto `UpstreamAuth`).

    `plugin` is a built-in (`none`, `static_bearer`, `passthrough`) or a WASM upstream-auth
    plugin a gateway reports, with its `params`. Only `static_bearer` carries a credential:
    the name of an env var on the gateway (`secret_ref`) or a token sealed here (`secret`).
    A WASM plugin reads its own secrets from the gateway environment, so it takes neither.
    """

    plugin: AuthPluginName = "none"
    params: Params = Field(default_factory=dict)
    secret_ref: str | None = Field(default=None, max_length=200)
    secret: SecretStr | None = Field(default=None, min_length=1, max_length=4096)
    # WASM plugin only: how long one call may run; 0 = the gateway default (60 s).
    timeout_ms: int = Field(default=0, ge=0, le=INT32_MAX)

    _check_secret_ref = field_validator("secret_ref")(_env_name)
    _check_secret = field_validator("secret")(_api_key)

    @model_validator(mode="after")
    def _credential_source(self) -> TargetAuthIn:
        if self.plugin == "static_bearer":
            if (self.secret_ref is None) == (self.secret is None):
                raise ValueError("static_bearer needs exactly one of secret_ref or secret")
        elif self.secret_ref is not None or self.secret is not None:
            raise ValueError(f"{self.plugin} takes no credential; only static_bearer does")
        return self


class PluginRefIn(ApiModel):
    plugin: AuthPluginName
    params: Params = Field(default_factory=dict)
    # WASM plugin only: how long one call may run; 0 = the gateway default (60 s).
    timeout_ms: int = Field(default=0, ge=0, le=INT32_MAX)


class PluginRefOut(ReadModel):
    plugin: str
    params: dict[str, Any]
    timeout_ms: int = Field(
        description="One plugin call's timeout in ms; 0 = the gateway's 60 s default."
    )


class McpServerCreate(ApiModel):
    name: TargetName
    url: str = Field(min_length=1, max_length=2000)
    auth: TargetAuthIn = Field(default_factory=TargetAuthIn)
    # Per upstream request; 0 = the gateway default (30 s).
    timeout_ms: int = Field(default=0, ge=0, le=INT32_MAX)
    review_mode: McpReviewMode = McpReviewMode.TOFU
    force: bool = _force_field()

    _check_url = field_validator("url")(_target_url)


class McpServerUpdate(ApiModel):
    """PATCH: omitted fields are unchanged. `auth` replaces the whole outbound auth."""

    name: TargetName | None = None
    url: str | None = Field(default=None, min_length=1, max_length=2000)
    auth: TargetAuthIn | None = None
    timeout_ms: int | None = Field(default=None, ge=0, le=INT32_MAX)
    review_mode: McpReviewMode | None = None
    force: bool = _force_field()

    _check_url = field_validator("url")(_target_url)


class McpServerOut(ReadModel):
    id: uuid.UUID
    name: str
    url: str
    auth: PluginRefOut
    secret_ref: str | None = Field(
        description="Name of the gateway's env var holding the token; never the value."
    )
    secret_set: bool = Field(description="A sealed token is stored (its value is never returned).")
    timeout_ms: int
    review_mode: McpReviewMode
    definitions: dict[str, int] = Field(
        description="Definition counts by review state: approved, pending, rejected."
    )
    config_invalid: bool = Field(
        description="The auth plugin or its settings cannot be served by the gateways today."
    )
    config_problems: list[str]
    created_at: datetime
    updated_at: datetime


class McpDefinitionOut(ApiModel):
    id: uuid.UUID
    server_id: uuid.UUID
    kind: McpDefinitionKind
    name: str
    hash: str = Field(description="sha256 of the canonical definition, hex.")
    definition_json: str = Field(description="Canonical projection the hash covers; plain text.")
    state: McpDefinitionState
    first_seen_at: datetime
    last_seen_at: datetime
    decided_by_user_id: uuid.UUID | None
    decided_at: datetime | None


class McpDiscoverIn(ApiModel):
    """One `tools/list`/`prompts/list` from the control plane, with a token used once.

    Give `server_id` to record the result as observed definitions of that server (the same
    rules as a gateway report). Without it nothing is stored, and nothing is stored either
    for a URL other than the saved one or when the server changed while the call ran. A
    server whose auth is a WASM plugin cannot be discovered here.
    """

    url: str = Field(min_length=1, max_length=2000)
    server_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "Record the result against this saved server; its stored credential is used when"
            " no token is given."
        ),
    )
    token: SecretStr | None = Field(
        default=None,
        min_length=1,
        max_length=4096,
        description="A token typed in the form: used once, never stored, never audited.",
    )
    timeout_ms: int = Field(default=10000, ge=1000, le=120_000)

    _check_url = field_validator("url")(_target_url)
    _check_token = field_validator("token")(_api_key)


class McpDiscoveredOut(ApiModel):
    kind: McpDefinitionKind
    name: str
    hash: str
    definition_json: str
    state: Literal["approved", "pending", "rejected", "unrecorded"] = Field(
        description=(
            "The state after recording; 'unrecorded' when nothing was recorded (no server_id,"
            " a URL other than the saved one, a configuration that changed meanwhile, or an"
            " item the database cannot store)."
        )
    )


class McpDiscoverOut(ApiModel):
    credential: Literal["entered", "stored", "env", "env_unset", "none"] = Field(
        description=(
            "Where the token came from: typed in the form, the saved server's sealed token,"
            " the control plane's own env var, that env var missing here, or none."
        )
    )
    server_id: uuid.UUID | None
    server: str | None
    protocol_version: str
    tools: list[McpDiscoveredOut]
    prompts: list[McpDiscoveredOut]
    truncated: bool = Field(description="The plan's per-server bound stopped the listing.")
    recorded: bool = Field(
        description=(
            "Whether the result was recorded against the saved server. False means the"
            " states above are 'unrecorded'."
        )
    )


class A2aAgentCreate(ApiModel):
    name: TargetName
    url: str = Field(min_length=1, max_length=2000)
    card_url: str | None = Field(default=None, max_length=2000)
    auth: TargetAuthIn = Field(default_factory=TargetAuthIn)
    middleware: PluginRefIn | None = None
    timeout_ms: int = Field(default=0, ge=0, le=INT32_MAX)
    card_json: str | None = Field(
        default=None, description="A pasted Agent Card (JSON object, at most 64 KiB)."
    )
    force: bool = _force_field()

    _check_url = field_validator("url")(_target_url)
    _check_card_url = field_validator("card_url")(_card_url)
    _check_card_json = field_validator("card_json")(_card_json)


class A2aAgentUpdate(ApiModel):
    """PATCH: omitted fields are unchanged; null clears `card_url`, `middleware` or the
    pasted `card_json`. `auth` replaces the whole outbound auth."""

    name: TargetName | None = None
    url: str | None = Field(default=None, min_length=1, max_length=2000)
    card_url: str | None = Field(default=None, max_length=2000)
    auth: TargetAuthIn | None = None
    middleware: PluginRefIn | None = None
    timeout_ms: int | None = Field(default=None, ge=0, le=INT32_MAX)
    card_json: str | None = Field(default=None)
    force: bool = _force_field()

    _check_url = field_validator("url")(_target_url)
    _check_card_url = field_validator("card_url")(_card_url)
    _check_card_json = field_validator("card_json")(_card_json)


class A2aAgentOut(ReadModel):
    id: uuid.UUID
    name: str
    url: str
    card_url: str = Field(description="Empty = the gateway derives it from `url`.")
    auth: PluginRefOut
    secret_ref: str | None
    secret_set: bool
    middleware: PluginRefOut | None
    timeout_ms: int
    card_json: str = Field(description="Pasted card served until the gateway fetched one.")
    last_card_json: str = Field(description="Newest card a gateway fetched, as text.")
    last_card_error: str = Field(description="Newest fetch error, empty on success.")
    last_card_at: datetime | None
    config_invalid: bool
    config_problems: list[str]
    created_at: datetime
    updated_at: datetime
