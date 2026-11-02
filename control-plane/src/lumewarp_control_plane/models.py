"""Database schema (source of truth for the control plane).

Conventions:
- Primary keys of entities are UUIDv7 (`ids.uuid7`); high-volume logs use BIGINT identity.
- Money is integer nano-USD (1e-9 USD) in BIGINT, matching `Price` / `UsageEvent` in the protos.
- Enums are stored as VARCHAR with a CHECK constraint (easy to extend in a migration).
- Secrets are never stored: API keys, PATs and gateway tokens are kept as sha256 digests
  (32 bytes) plus a short non-secret display prefix. See security.py for why sha256.
- The installation serves one config scope (`scopes` row); source tables (teams, routing,
  users, ...) are installation-wide and compile into that scope's snapshots.
- Hierarchy: team -> members (users) and service accounts -> keys. Every key belongs to
  one team and is owned by a member of it or by one of its service accounts.
"""

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    cast,
    func,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, aggregate_order_by
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    column_property,
    declared_attr,
    mapped_column,
    synonym,
)

from lumewarp_control_plane.ids import uuid7

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _enum(cls: type[enum.StrEnum], name: str) -> Enum:
    return Enum(
        cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=32,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
    )


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _hash32(name: str) -> CheckConstraint:
    return CheckConstraint(f"octet_length({name}) = 32", name=f"{name}_len")


class TimestampMixin:
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# --------------------------------------------------------------------------- enums


class ModelAccess(enum.StrEnum):
    """Per-level model allowlist mode (O1 M1).

    INHERIT: take the team's effective set unchanged (not allowed on teams).
    ALL: no restriction at this level (effective set = the team's; universal on a team).
    LIST: exactly the `policy_grants` of this level, intersected with the parent; an empty
    list is deny-all.
    Resolve INHERIT first; a key whose effective set is still universal gets
    allow_all_models (see compiler.compile_scope).
    """

    INHERIT = "inherit"
    ALL = "all"
    LIST = "list"


class KeyLifecycle(enum.StrEnum):
    """Key lifecycle, set by people. Budget blocking is derived separately (O1 M5) and never
    written here; the compiler maps lifecycle + budget state + expiry to proto KeyStatus.
    """

    ACTIVE = "active"
    SUSPENDED = "suspended"
    REVOKED = "revoked"


class TeamRole(enum.StrEnum):
    ADMIN = "admin"
    MEMBER = "member"


class KeyOwnerKind(enum.StrEnum):
    USER = "user"
    SERVICE_ACCOUNT = "service_account"


class ScopeType(enum.StrEnum):
    """Level a budget/total/limit belongs to: the team, or the owner of a key (a team
    member, see `member_scope_id`, or a service account).
    """

    TEAM = "team"
    MEMBER = "member"
    SERVICE_ACCOUNT = "service_account"


class UpstreamKind(enum.StrEnum):
    OPENAI = "openai"
    VLLM = "vllm"
    OPENAI_COMPATIBLE = "openai_compatible"


class StreamUsageMode(enum.StrEnum):
    INCLUDE_USAGE = "include_usage"
    PASSTHROUGH = "passthrough"


class CatalogKind(enum.StrEnum):
    """What a public name refers to. Model, alias and router share one namespace."""

    MODEL = "model"
    ALIAS = "alias"
    ROUTER = "router"


class StickyBy(enum.StrEnum):
    """What a traffic split hashes to keep a caller on one split (proto StickyBy)."""

    NONE = "none"
    HEADER = "header"
    TEAM = "team"
    KEY = "key"


class ConfigVersionKind(enum.StrEnum):
    COMPILE = "compile"
    ROLLBACK = "rollback"


class OutboxStatus(enum.StrEnum):
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"


class UsageState(enum.StrEnum):
    UNSPECIFIED = "unspecified"
    REPORTED = "reported"
    MISSING = "missing"
    PARTIAL = "partial"
    INVALID = "invalid"


class RequestOutcome(enum.StrEnum):
    UNSPECIFIED = "unspecified"
    COMPLETED = "completed"
    CLIENT_CANCELLED = "client_cancelled"
    UPSTREAM_ERROR = "upstream_error"
    REJECTED = "rejected"


class ActorType(enum.StrEnum):
    BREAK_GLASS = "break_glass"
    USER = "user"
    GATEWAY = "gateway"
    SYSTEM = "system"


# --------------------------------------------------------------------------- policy


class PolicyMixin:
    """Policy carried by teams and by key owners (team members, service accounts).

    Two levels only; an owner may only narrow its team (enforced by the API, 422
    otherwise): allowlist subset of the team's, limits <= the team's, budget <= the team's.
    Keys carry no policy of their own.

    - Model access: see `ModelAccess`. The list lives in `policy_grants` (by name id, so a
      rename keeps it); `allowed_models` is its read-only expansion to current names.
    - Rate limits: NULL = no limit at this level. Each set level becomes one shared
      `RateLimitScope` ("team:<id>", "member:<scope id>", "sa:<id>") in the snapshot.
    - Budget: monthly (UTC calendar) hard limit and soft (notify) threshold in nano-USD,
      NULL = none. Spend lives in `usage_totals`, never on the entity row.
    """

    @declared_attr
    @classmethod
    def model_access(cls) -> Mapped[ModelAccess]:
        default = ModelAccess.ALL if cls.__name__ == "Team" else ModelAccess.INHERIT
        return mapped_column(
            _enum(ModelAccess, "model_access"),
            nullable=False,
            default=default,
            server_default=default.value,
        )

    rpm_limit: Mapped[int | None] = mapped_column(Integer)
    tpm_limit: Mapped[int | None] = mapped_column(BigInteger)
    max_parallel: Mapped[int | None] = mapped_column(Integer)
    budget_nano_usd: Mapped[int | None] = mapped_column(BigInteger)
    soft_budget_nano_usd: Mapped[int | None] = mapped_column(BigInteger)

    @staticmethod
    def policy_constraints() -> tuple[CheckConstraint, ...]:
        return (
            CheckConstraint("rpm_limit IS NULL OR rpm_limit > 0", name="rpm_positive"),
            CheckConstraint("tpm_limit IS NULL OR tpm_limit > 0", name="tpm_positive"),
            CheckConstraint("max_parallel IS NULL OR max_parallel > 0", name="parallel_positive"),
            CheckConstraint(
                "budget_nano_usd IS NULL OR budget_nano_usd >= 0", name="budget_nonneg"
            ),
            CheckConstraint(
                "soft_budget_nano_usd IS NULL OR soft_budget_nano_usd >= 0", name="soft_nonneg"
            ),
            CheckConstraint(
                "soft_budget_nano_usd IS NULL OR budget_nano_usd IS NULL"
                " OR soft_budget_nano_usd <= budget_nano_usd",
                name="soft_le_hard",
            ),
        )


# --------------------------------------------------------------------------- installation


class Scope(Base):
    """A config scope (one per installation in P1).

    `source_revision` is bumped by `outbox.enqueue_recompile` in every mutating transaction;
    the row lock it takes also serializes writers. Compiles record the revision they saw.
    """

    __tablename__ = "scopes"

    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    source_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    created_at: Mapped[datetime] = _now()


class User(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (Index("uq_users_email_lower", text("lower(email)"), unique=True),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False, server_default="")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")


class UserIdentity(Base):
    __tablename__ = "user_identities"
    __table_args__ = (UniqueConstraint("issuer", "subject"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    issuer: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str | None] = mapped_column(String(320))
    created_at: Mapped[datetime] = _now()
    last_login_at: Mapped[datetime] = _now()


class UserSession(Base):
    __tablename__ = "user_sessions"
    __table_args__ = (
        _hash32("token_hash"),
        _hash32("csrf_hash"),
        CheckConstraint(
            "(kind = 'session' AND user_id IS NOT NULL) OR "
            "(kind = 'break_glass' AND user_id IS NULL)",
            name="kind",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False, server_default="session")
    admin_group: Mapped[str | None] = mapped_column(Text)
    credential_fingerprint: Mapped[bytes | None] = mapped_column(LargeBinary)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True)
    csrf_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = _now()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = _now()
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OIDCLoginState(Base):
    __tablename__ = "oidc_login_states"
    __table_args__ = (_hash32("state_hash"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    state_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True)
    nonce: Mapped[str] = mapped_column(Text, nullable=False)
    code_verifier: Mapped[str] = mapped_column(Text, nullable=False)
    return_path: Mapped[str] = mapped_column(Text, nullable=False)
    redirect_uri: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PersonalAccessToken(Base):
    """Admin API credential (`lwp-...`) for one user. Only the sha256 is stored."""

    __tablename__ = "personal_access_tokens"
    __table_args__ = (_hash32("token_hash"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True)
    display_prefix: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = _now()
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# --------------------------------------------------------------------------- hierarchy


def member_scope_id(team_id: uuid.UUID, user_id: uuid.UUID) -> uuid.UUID:
    """Stable id of one user's membership in one team, for `usage_totals` and the
    "member:<id>" rate-limit scope. Derived rather than stored so that removing and
    re-adding a member keeps the same spend and limiter.
    """
    return uuid.uuid5(team_id, str(user_id))


class Team(PolicyMixin, TimestampMixin, Base):
    __tablename__ = "teams"
    __table_args__ = (
        *PolicyMixin.policy_constraints(),
        CheckConstraint("model_access <> 'inherit'", name="no_inherit"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)


class TeamMember(PolicyMixin, TimestampMixin, Base):
    """A user in a team, with a role and the member's own (narrowing) policy."""

    __tablename__ = "team_members"
    __table_args__ = (
        *PolicyMixin.policy_constraints(),
        PrimaryKeyConstraint("team_id", "user_id"),
        Index(None, "user_id"),
    )

    team_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("teams.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[TeamRole] = mapped_column(_enum(TeamRole, "team_role"), nullable=False)


class ServiceAccount(PolicyMixin, TimestampMixin, Base):
    """A non-human key owner that belongs to a team; managed by the team's admins and
    unaffected by people joining or leaving.
    """

    __tablename__ = "service_accounts"
    __table_args__ = (
        *PolicyMixin.policy_constraints(),
        UniqueConstraint("team_id", "name"),
        # Target for the composite FK that pins a key to its owner's team.
        UniqueConstraint("id", "team_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    team_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("teams.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )


class ApiKey(TimestampMixin, Base):
    """Virtual API key (`lw-...`). `id` is the public key_id used in snapshots and usage.

    Owned by a user (who must be a member of `team_id`) or by a service account of
    `team_id`. Effective gateway status = REVOKED/SUSPENDED from `lifecycle`, else
    expired, else BUDGET_EXCEEDED if the team or the owner is over budget in the current
    window, else ACTIVE. Keys are revoked, not deleted, so usage history keeps resolving.
    """

    __tablename__ = "api_keys"
    __table_args__ = (
        _hash32("key_hash"),
        ForeignKeyConstraint(
            ["service_account_id", "team_id"],
            ["service_accounts.id", "service_accounts.team_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "(owner_kind = 'user' AND user_id IS NOT NULL AND service_account_id IS NULL)"
            " OR (owner_kind = 'service_account' AND service_account_id IS NOT NULL"
            " AND user_id IS NULL)",
            name="owner_matches_kind",
        ),
        CheckConstraint(
            "(lifecycle = 'revoked') = (revoked_at IS NOT NULL)", name="revoked_at_matches"
        ),
        CheckConstraint(
            "suspended_reason IS NULL OR lifecycle = 'suspended'", name="reason_when_suspended"
        ),
        Index(None, "team_id"),
        Index(None, "user_id"),
        Index(None, "service_account_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    team_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("teams.id", ondelete="RESTRICT"), nullable=False
    )
    owner_kind: Mapped[KeyOwnerKind] = mapped_column(
        _enum(KeyOwnerKind, "key_owner_kind"), nullable=False
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    service_account_id: Mapped[uuid.UUID | None] = mapped_column()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    key_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True)
    display_prefix: Mapped[str] = mapped_column(String(32), nullable=False)
    lifecycle: Mapped[KeyLifecycle] = mapped_column(
        _enum(KeyLifecycle, "key_lifecycle"),
        nullable=False,
        default=KeyLifecycle.ACTIVE,
        server_default=KeyLifecycle.ACTIVE.value,
    )
    # Why a key was suspended automatically (e.g. "owner_removed"); NULL when a person
    # suspended it.
    suspended_reason: Mapped[str | None] = mapped_column(String(64))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    @property
    def owner_id(self) -> uuid.UUID:
        owner = self.user_id if self.owner_kind is KeyOwnerKind.USER else self.service_account_id
        assert owner is not None
        return owner

    @property
    def owner_scope(self) -> tuple[ScopeType, uuid.UUID]:
        """The owner's `usage_totals` / rate-limit level and id."""
        if self.owner_kind is KeyOwnerKind.USER:
            assert self.user_id is not None
            return ScopeType.MEMBER, member_scope_id(self.team_id, self.user_id)
        assert self.service_account_id is not None
        return ScopeType.SERVICE_ACCOUNT, self.service_account_id


# --------------------------------------------------------------------------- routing


class Upstream(TimestampMixin, Base):
    __tablename__ = "upstreams"
    __table_args__ = (
        CheckConstraint(
            "api_key_ref IS NULL OR api_key_ref ~ '^[A-Za-z_][A-Za-z0-9_]*$'",
            name="api_key_ref_env_name",
        ),
        CheckConstraint("timeout_ms >= 0", name="timeout_nonneg"),
        # Health check: interval/timeout NULL = no active probing; the other settings
        # only exist together with them.
        CheckConstraint(
            "(health_check_interval_ms IS NULL) = (health_check_timeout_ms IS NULL)"
            " AND (health_check_interval_ms IS NOT NULL OR (health_check_path IS NULL"
            " AND health_check_unhealthy_threshold IS NULL"
            " AND health_check_healthy_threshold IS NULL))",
            name="health_check_shape",
        ),
        CheckConstraint(
            "health_check_interval_ms IS NULL OR (health_check_interval_ms >= 1000"
            " AND health_check_timeout_ms > 0"
            " AND health_check_timeout_ms < health_check_interval_ms)",
            name="health_check_bounds",
        ),
        CheckConstraint(
            "(health_check_unhealthy_threshold IS NULL OR health_check_unhealthy_threshold > 0)"
            " AND (health_check_healthy_threshold IS NULL OR health_check_healthy_threshold > 0)",
            name="health_check_thresholds",
        ),
        CheckConstraint(
            "circuit_breaker_enabled OR (circuit_failure_threshold IS NULL"
            " AND circuit_open_ms IS NULL)",
            name="circuit_breaker_shape",
        ),
        CheckConstraint(
            "(circuit_failure_threshold IS NULL OR circuit_failure_threshold > 0)"
            " AND (circuit_open_ms IS NULL OR circuit_open_ms > 0)",
            name="circuit_breaker_positive",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    kind: Mapped[UpstreamKind] = mapped_column(_enum(UpstreamKind, "upstream_kind"), nullable=False)
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    # Name of the env var on the gateway holding the bearer token; never the value.
    api_key_ref: Mapped[str | None] = mapped_column(String(200))
    # 0 = gateway default.
    timeout_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    stream_usage_mode: Mapped[StreamUsageMode] = mapped_column(
        _enum(StreamUsageMode, "stream_usage_mode"),
        nullable=False,
        default=StreamUsageMode.INCLUDE_USAGE,
        server_default=StreamUsageMode.INCLUDE_USAGE.value,
    )
    # Active health probe (proto HealthCheck). NULL interval = not probed. Path NULL =
    # gateway default "/models"; thresholds NULL = gateway defaults (3 / 1).
    health_check_path: Mapped[str | None] = mapped_column(String(400))
    health_check_interval_ms: Mapped[int | None] = mapped_column(Integer)
    health_check_timeout_ms: Mapped[int | None] = mapped_column(Integer)
    health_check_unhealthy_threshold: Mapped[int | None] = mapped_column(Integer)
    health_check_healthy_threshold: Mapped[int | None] = mapped_column(Integer)
    # Per-model circuit breaker (proto CircuitBreaker); NULL settings = gateway
    # defaults (5 failures / 30000 ms).
    circuit_breaker_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    circuit_failure_threshold: Mapped[int | None] = mapped_column(Integer)
    circuit_open_ms: Mapped[int | None] = mapped_column(Integer)
    # Prometheus endpoint for ROUTING_STRATEGY_VLLM_LOAD; NULL = derived from base_url.
    metrics_url: Mapped[str | None] = mapped_column(Text)


class CatalogName(Base):
    """A public name. Everything else refers to the id, so renaming touches only this row."""

    __tablename__ = "catalog_names"
    __table_args__ = (
        # Target of the composite FKs that pin a row to the kind of its name.
        UniqueConstraint("id", "kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    name: Mapped[str] = mapped_column(String(400), nullable=False, unique=True)
    kind: Mapped[CatalogKind] = mapped_column(_enum(CatalogKind, "catalog_kind"), nullable=False)


class CatalogNameHistory(Base):
    """Names an id has given up. No FK: history outlives the item, and a name may be reused
    by another item, so lookups go by `kind` and time.
    """

    __tablename__ = "catalog_name_history"
    __table_args__ = (
        PrimaryKeyConstraint("name_id", "old_name", "renamed_at"),
        Index(None, "old_name", "kind"),
    )

    name_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    kind: Mapped[CatalogKind] = mapped_column(_enum(CatalogKind, "catalog_kind"), nullable=False)
    old_name: Mapped[str] = mapped_column(String(400), nullable=False)
    renamed_at: Mapped[datetime] = _now()


def _name_fk() -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["name_id", "kind"], ["catalog_names.id", "catalog_names.kind"], ondelete="CASCADE"
    )


class Model(TimestampMixin, Base):
    """One public name -> one model on one upstream. Price NULL = unknown (not free); both
    or neither. Calling it directly is a single attempt; weights live on router members.
    """

    __tablename__ = "models"
    __table_args__ = (
        _name_fk(),
        CheckConstraint("kind = 'model'", name="kind_model"),
        CheckConstraint(
            "(price_input_nano_usd_per_mtok IS NULL) = (price_output_nano_usd_per_mtok IS NULL)",
            name="price_both_or_neither",
        ),
        CheckConstraint(
            "price_input_nano_usd_per_mtok IS NULL OR (price_input_nano_usd_per_mtok >= 0"
            " AND price_output_nano_usd_per_mtok >= 0)",
            name="price_nonneg",
        ),
    )

    name_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(
        String(32), nullable=False, default="model", server_default="model"
    )
    upstream_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("upstreams.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    upstream_model: Mapped[str] = mapped_column(String(400), nullable=False)
    price_input_nano_usd_per_mtok: Mapped[int | None] = mapped_column(BigInteger)
    price_output_nano_usd_per_mtok: Mapped[int | None] = mapped_column(BigInteger)

    id = synonym("name_id")


class Alias(TimestampMixin, Base):
    """Another name for a model or a router: one level, authorized on its own."""

    __tablename__ = "aliases"
    __table_args__ = (
        _name_fk(),
        ForeignKeyConstraint(
            ["target_name_id", "target_kind"],
            ["catalog_names.id", "catalog_names.kind"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("kind = 'alias'", name="kind_alias"),
        CheckConstraint("target_kind IN ('model', 'router')", name="target_not_alias"),
        Index(None, "target_name_id"),
    )

    name_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(
        String(32), nullable=False, default="alias", server_default="alias"
    )
    target_name_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    target_kind: Mapped[str] = mapped_column(String(32), nullable=False)

    id = synonym("name_id")


class Router(TimestampMixin, Base):
    """A callable name that distributes requests over models (`router_members`).

    Retry, fallback, traffic split and strategy exist only here. Resilience (P2): an
    optional retry policy (scalar columns, `retry_enabled` = policy present), ordered
    fallback models (`router_fallbacks`) and an optional traffic split (`router_splits` +
    `router_split_members`, sticky by header/team/key). With a traffic split the members
    are listed under each split.
    """

    __tablename__ = "routers"
    __table_args__ = (
        _name_fk(),
        CheckConstraint("kind = 'router'", name="kind_router"),
        CheckConstraint("strategy ~ '^[a-z][a-z0-9_]{0,62}$'", name="strategy_name"),
        CheckConstraint("jsonb_typeof(params) = 'object'", name="params_object"),
        CheckConstraint(
            "retry_max_attempts IS NULL OR retry_max_attempts BETWEEN 1 AND 5",
            name="retry_max_attempts_range",
        ),
        CheckConstraint(
            "400 <= ALL (retry_on_status) AND 599 >= ALL (retry_on_status)",
            name="retry_status_range",
        ),
        CheckConstraint("retry_total_timeout_ms >= 0", name="retry_timeout_nonneg"),
        CheckConstraint(
            "retry_enabled OR (retry_max_attempts IS NULL"
            " AND cardinality(retry_on_status) = 0 AND retry_total_timeout_ms = 0)",
            name="retry_shape",
        ),
        CheckConstraint(
            "(sticky = 'header') = (sticky_header IS NOT NULL)", name="sticky_header_iff"
        ),
    )

    name_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(
        String(32), nullable=False, default="router", server_default="router"
    )
    # A built-in name or a compiled-in strategy's name (see compiler/strategy.py).
    strategy: Mapped[str] = mapped_column(
        String(63), nullable=False, default="weighted", server_default="weighted"
    )
    params: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    # Retry policy (proto RetryPolicy). max_attempts NULL = gateway default (2, emitted
    # as 0); empty retry_on_status = gateway default (429, 502, 503, 504); timeout 0 =
    # no bound beyond each upstream's timeout_ms.
    retry_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    retry_max_attempts: Mapped[int | None] = mapped_column(SmallInteger)
    retry_on_status: Mapped[list[int]] = mapped_column(
        ARRAY(SmallInteger), nullable=False, default=list, server_default=text("'{}'")
    )
    retry_total_timeout_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    sticky: Mapped[StickyBy] = mapped_column(
        _enum(StickyBy, "sticky_by"),
        nullable=False,
        default=StickyBy.NONE,
        server_default=StickyBy.NONE.value,
    )
    # Request header hashed when sticky = header (lower-case HTTP token).
    sticky_header: Mapped[str | None] = mapped_column(String(200))

    id = synonym("name_id")


class RouterMember(Base):
    """A model of a router, with this router's weight and strategy-specific settings."""

    __tablename__ = "router_members"
    __table_args__ = (
        PrimaryKeyConstraint("router_id", "model_id"),
        UniqueConstraint("router_id", "position"),
        Index(None, "model_id"),
        CheckConstraint("weight >= 0", name="weight_nonneg"),
        CheckConstraint("jsonb_typeof(params) = 'object'", name="params_object"),
    )

    router_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("routers.name_id", ondelete="CASCADE"), nullable=False
    )
    model_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("models.name_id", ondelete="RESTRICT"), nullable=False
    )
    weight: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    params: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)


class RouterFallback(Base):
    """Ordered fallback models of a router. A model referenced as a fallback cannot be
    deleted (RESTRICT; the API answers 409).
    """

    __tablename__ = "router_fallbacks"
    __table_args__ = (
        PrimaryKeyConstraint("router_id", "model_id"),
        UniqueConstraint("router_id", "position"),
        Index(None, "model_id"),
    )

    router_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("routers.name_id", ondelete="CASCADE"), nullable=False
    )
    model_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("models.name_id", ondelete="RESTRICT"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)


class RouterSplit(Base):
    """One traffic split (canary group) of a router; `name` is a bounded metric label."""

    __tablename__ = "router_splits"
    __table_args__ = (
        UniqueConstraint("router_id", "name"),
        UniqueConstraint("router_id", "position"),
        CheckConstraint("name ~ '^[A-Za-z0-9_.-]{1,32}$'", name="name_charset"),
        CheckConstraint("weight >= 0", name="weight_nonneg"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    router_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("routers.name_id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    weight: Mapped[int] = mapped_column(Integer, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)


class RouterSplitMember(Base):
    __tablename__ = "router_split_members"
    __table_args__ = (
        PrimaryKeyConstraint("split_id", "model_id"),
        UniqueConstraint("split_id", "position"),
        Index(None, "model_id"),
        CheckConstraint("weight >= 0", name="weight_nonneg"),
        CheckConstraint("jsonb_typeof(params) = 'object'", name="params_object"),
    )

    split_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("router_splits.id", ondelete="CASCADE"), nullable=False
    )
    model_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("models.name_id", ondelete="RESTRICT"), nullable=False
    )
    weight: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    params: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)


class PolicyGrant(Base):
    """A name in the `allowed_models` of a team (user and service account NULL), a member
    (user set) or a service account. By name id, so a rename keeps it.
    """

    __tablename__ = "policy_grants"
    __table_args__ = (
        ForeignKeyConstraint(
            ["team_id", "user_id"],
            ["team_members.team_id", "team_members.user_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["service_account_id", "team_id"],
            ["service_accounts.id", "service_accounts.team_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint("user_id IS NULL OR service_account_id IS NULL", name="one_owner"),
        Index(
            "uq_policy_grants_team",
            "team_id",
            "name_id",
            unique=True,
            postgresql_where=text("user_id IS NULL AND service_account_id IS NULL"),
        ),
        Index(
            "uq_policy_grants_member",
            "team_id",
            "user_id",
            "name_id",
            unique=True,
            postgresql_where=text("user_id IS NOT NULL"),
        ),
        Index(
            "uq_policy_grants_service_account",
            "service_account_id",
            "name_id",
            unique=True,
            postgresql_where=text("service_account_id IS NOT NULL"),
        ),
        Index(None, "name_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    name_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("catalog_names.id", ondelete="CASCADE"), nullable=False
    )
    team_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("teams.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column()
    service_account_id: Mapped[uuid.UUID | None] = mapped_column()


def _current_name(id_column) -> Any:
    return (
        select(CatalogName.name)
        .where(CatalogName.id == id_column)
        .correlate_except(CatalogName)
        .scalar_subquery()
    )


# Read-only: a rename writes `catalog_names`.
Model.name = column_property(_current_name(Model.name_id))
Alias.name = column_property(_current_name(Alias.name_id))
Alias.target_name = column_property(_current_name(Alias.target_name_id))
Router.name = column_property(_current_name(Router.name_id))


def _granted_names(*owner) -> Any:
    """The owner's grants as current names, sorted (empty array, not NULL, when none)."""
    return (
        select(
            func.coalesce(
                func.array_agg(aggregate_order_by(cast(CatalogName.name, Text), CatalogName.name)),
                cast(text("'{}'"), ARRAY(Text)),
            )
        )
        .select_from(PolicyGrant)
        .join(CatalogName, CatalogName.id == PolicyGrant.name_id)
        .where(*owner)
        .correlate_except(PolicyGrant, CatalogName)
        .scalar_subquery()
    )


Team.allowed_models = column_property(
    _granted_names(
        PolicyGrant.team_id == Team.id,
        PolicyGrant.user_id.is_(None),
        PolicyGrant.service_account_id.is_(None),
    )
)
TeamMember.allowed_models = column_property(
    _granted_names(
        PolicyGrant.team_id == TeamMember.team_id, PolicyGrant.user_id == TeamMember.user_id
    )
)
ServiceAccount.allowed_models = column_property(
    _granted_names(PolicyGrant.service_account_id == ServiceAccount.id)
)


# --------------------------------------------------------------------------- config stream


class ConfigVersion(Base):
    """A published snapshot. Immutable: a DB trigger rejects any UPDATE except compaction
    (payload and catalog_state -> NULL with compacted_at set). Rollbacks are new rows
    of kind ROLLBACK.
    """

    __tablename__ = "config_versions"
    __table_args__ = (
        PrimaryKeyConstraint("scope", "version"),
        _hash32("digest"),
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint("(payload IS NULL) = (compacted_at IS NOT NULL)", name="compaction"),
        CheckConstraint(
            "payload IS NOT NULL OR catalog_state IS NULL", name="catalog_state_compaction"
        ),
        CheckConstraint(
            "(kind = 'rollback') = (rollback_of_version IS NOT NULL)", name="rollback_ref"
        ),
        Index(None, "scope", "source_revision"),
    )

    scope: Mapped[str] = mapped_column(
        ForeignKey("scopes.name", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Serialized lumewarp.config.v1.Snapshot.
    payload: Mapped[bytes | None] = mapped_column(LargeBinary)
    # Catalog source rows at publish, including IDs (compiler/catalog_state.py). Compacted
    # together with payload. Policy and grants are never included.
    catalog_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    # sha256(payload), kept after compaction.
    digest: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    key_count: Mapped[int] = mapped_column(Integer, nullable=False)
    source_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[ConfigVersionKind] = mapped_column(
        _enum(ConfigVersionKind, "config_version_kind"), nullable=False
    )
    rollback_of_version: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = _now()
    compacted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ConfigOutbox(Base):
    """ "Recompile scope" requests, written in the same transaction as the source mutation
    (`outbox.enqueue_recompile`). The compiler loop drains rows with
    source_revision <= the revision it compiled.
    """

    __tablename__ = "config_outbox"
    __table_args__ = (
        CheckConstraint("attempts >= 0", name="attempts_nonneg"),
        Index(
            "ix_config_outbox_pending",
            "scope",
            "id",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    scope: Mapped[str] = mapped_column(
        ForeignKey("scopes.name", ondelete="RESTRICT"), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    source_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[OutboxStatus] = mapped_column(
        _enum(OutboxStatus, "outbox_status"),
        nullable=False,
        default=OutboxStatus.PENDING,
        server_default=OutboxStatus.PENDING.value,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    # Version that satisfied this request, once done.
    published_version: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = _now()
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GatewayToken(Base):
    """Gateway credential (`lwg-...`), bound to one gateway_id and scope. Hash only."""

    __tablename__ = "gateway_tokens"
    __table_args__ = (_hash32("token_hash"), Index(None, "scope", "gateway_id"))

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    scope: Mapped[str] = mapped_column(
        ForeignKey("scopes.name", ondelete="RESTRICT"), nullable=False
    )
    gateway_id: Mapped[str] = mapped_column(String(200), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False, server_default="")
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True)
    display_prefix: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = _now()
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GatewaySession(Base):
    """Last known stream state per gateway, for status/UI. Authoritative ACK/pending state
    is in memory (`grpc.SessionRegistry`); this row mirrors it.
    """

    __tablename__ = "gateway_sessions"
    __table_args__ = (PrimaryKeyConstraint("scope", "gateway_id"),)

    scope: Mapped[str] = mapped_column(
        ForeignKey("scopes.name", ondelete="RESTRICT"), nullable=False
    )
    gateway_id: Mapped[str] = mapped_column(String(200), nullable=False)
    session_id: Mapped[uuid.UUID | None] = mapped_column()
    connected: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    gateway_version: Mapped[str] = mapped_column(String(100), nullable=False, server_default="")
    capabilities: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list, server_default=text("'{}'")
    )
    peer: Mapped[str | None] = mapped_column(String(200))
    hello_have_version: Mapped[int | None] = mapped_column(BigInteger)
    acked_version: Mapped[int | None] = mapped_column(BigInteger)
    pending_version: Mapped[int | None] = mapped_column(BigInteger)
    last_nack_version: Mapped[int | None] = mapped_column(BigInteger)
    last_nack_code: Mapped[str | None] = mapped_column(String(64))
    last_nack_reason: Mapped[str | None] = mapped_column(Text)
    quarantined_version: Mapped[int | None] = mapped_column(BigInteger)
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disconnected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# --------------------------------------------------------------------------- usage


class UsageEvent(Base):
    """One gateway request. Deduped on `request_id` (ON CONFLICT DO NOTHING); only newly
    inserted events add to `usage_totals`. No FKs: a high-volume append-only log.
    """

    __tablename__ = "usage_events"
    __table_args__ = (
        CheckConstraint(
            "prompt_tokens >= 0 AND completion_tokens >= 0 AND cost_nano_usd >= 0",
            name="nonneg",
        ),
        Index(None, "ts"),
        Index("ix_usage_events_team_ts", "team_id", "ts"),
        Index("ix_usage_events_owner_ts", "owner_id", "ts"),
        Index("ix_usage_events_key_ts", "key_id", "ts"),
        *(
            Index(f"ix_usage_events_{field}_ts", field, "ts")
            for field in (
                "model_id",
                "alias_id",
                "router_id",
                "model",
                "alias",
                "router",
                "upstream_model",
            )
        ),
        CheckConstraint("attempts >= 0", name="attempts_nonneg"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    request_id: Mapped[uuid.UUID] = mapped_column(nullable=False, unique=True)
    # Identity from the gateway token, never from the payload.
    gateway_id: Mapped[str] = mapped_column(String(200), nullable=False)
    # Completion time (UsageEvent.timestamp_unix_ms); budget windows use it.
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = _now()
    key_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    team_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    # The key's owner at ingest time (user or service account id).
    owner_kind: Mapped[KeyOwnerKind] = mapped_column(
        _enum(KeyOwnerKind, "key_owner_kind"), nullable=False
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    requested: Mapped[str] = mapped_column(String(400), nullable=False)
    model: Mapped[str] = mapped_column(String(400), nullable=False)
    alias: Mapped[str] = mapped_column(String(400), nullable=False, default="", server_default="")
    router: Mapped[str] = mapped_column(String(400), nullable=False, default="", server_default="")
    upstream: Mapped[str] = mapped_column(
        String(200), nullable=False, default="", server_default=""
    )
    upstream_model: Mapped[str] = mapped_column(
        String(400), nullable=False, default="", server_default=""
    )
    fallback: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    attempts: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    model_id: Mapped[uuid.UUID | None] = mapped_column()
    alias_id: Mapped[uuid.UUID | None] = mapped_column()
    router_id: Mapped[uuid.UUID | None] = mapped_column()
    endpoint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    stream: Mapped[bool] = mapped_column(Boolean, nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cost_nano_usd: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cost_known: Mapped[bool] = mapped_column(Boolean, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    config_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    usage_state: Mapped[UsageState] = mapped_column(
        _enum(UsageState, "usage_state"), nullable=False
    )
    outcome: Mapped[RequestOutcome] = mapped_column(
        _enum(RequestOutcome, "request_outcome"), nullable=False
    )


class UsageTotal(Base):
    """Aggregated spend per level per UTC month (`windows.month_window`).

    Each new event adds its cost once to the team row and once to the key owner's row
    (member or service account). Blocking is
    derived (cost_nano_usd >= the level's current budget_nano_usd); `exceeded_at` and
    `soft_notified_at` record when that was first seen, for notifications and audit.
    """

    __tablename__ = "usage_totals"
    __table_args__ = (
        PrimaryKeyConstraint("scope_type", "scope_id", "window_start"),
        CheckConstraint(
            "cost_nano_usd >= 0 AND prompt_tokens >= 0 AND completion_tokens >= 0"
            " AND request_count >= 0 AND unknown_cost_count >= 0",
            name="nonneg",
        ),
    )

    scope_type: Mapped[ScopeType] = mapped_column(_enum(ScopeType, "scope_type"), nullable=False)
    scope_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cost_nano_usd: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    prompt_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    completion_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    request_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    # Events whose cost is unknown (no price or usage not REPORTED).
    unknown_cost_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    soft_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exceeded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = _now()


# --------------------------------------------------------------------------- audit, notify


class AuditLog(Base):
    """Who did what. `details` must never contain secrets, Authorization headers or
    prompts; write rows through `audit.record`, which redacts sensitive keys.
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        Index(None, "ts"),
        Index("ix_audit_log_team_ts", "team_id", "ts"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    ts: Mapped[datetime] = _now()
    actor_type: Mapped[ActorType] = mapped_column(_enum(ActorType, "actor_type"), nullable=False)
    # User id, gateway id, or a fixed name for system jobs.
    actor_id: Mapped[str | None] = mapped_column(String(200))
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(200))
    # Team the resource belongs to, for scoped reads (NULL = installation-wide).
    team_id: Mapped[uuid.UUID | None] = mapped_column()
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )


class NotifierChannel(TimestampMixin, Base):
    """A configured notification target. `kind` is "webhook" (built-in) or the entry-point
    name of a `lumewarp.notifier` plugin. team_id NULL = installation-wide.
    """

    __tablename__ = "notifier_channels"
    __table_args__ = (Index(None, "team_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(String(100), nullable=False)
    team_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")


class NotifierOutbox(Base):
    """Pending notifications, delivered by a background loop with retries. `dedupe_key`
    (e.g. "budget.soft:team:<id>:2026-10") makes enqueueing idempotent.
    """

    __tablename__ = "notifier_outbox"
    __table_args__ = (
        CheckConstraint("attempts >= 0", name="attempts_nonneg"),
        Index(
            "ix_notifier_outbox_due",
            "next_attempt_at",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String(400), nullable=False, unique=True)
    channel_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("notifier_channels.id", ondelete="CASCADE")
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[OutboxStatus] = mapped_column(
        _enum(OutboxStatus, "outbox_status"),
        nullable=False,
        default=OutboxStatus.PENDING,
        server_default=OutboxStatus.PENDING.value,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    next_attempt_at: Mapped[datetime] = _now()
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
