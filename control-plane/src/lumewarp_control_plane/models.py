"""Database schema (source of truth for the control plane).

Conventions:
- Primary keys of entities are UUIDv7 (`ids.uuid7`); high-volume logs use BIGINT identity.
- Money is integer nano-USD (1e-9 USD) in BIGINT, matching `Price` / `UsageEvent` in the protos.
- Enums are stored as VARCHAR with a CHECK constraint (easy to extend in a migration).
- Secrets are never stored: API keys, PATs and gateway tokens are kept as sha256 digests
  (32 bytes) plus a short non-secret display prefix. See security.py for why sha256.
- The installation serves one config scope (`scopes` row); source tables (orgs, routing,
  users, ...) are installation-wide and compile into that scope's snapshots.
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
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column

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

    INHERIT: take the parent's effective set unchanged (not allowed on orgs).
    ALL: no restriction at this level (effective set = parent's; universal on an org).
    LIST: exactly `allowed_models`, intersected with the parent; an empty list is deny-all.
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


class OrgRole(enum.StrEnum):
    OWNER = "owner"
    ADMIN = "admin"


class TeamRole(enum.StrEnum):
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


class ScopeType(enum.StrEnum):
    """Hierarchy level a budget/total/limit belongs to."""

    ORG = "org"
    TEAM = "team"
    PROJECT = "project"
    KEY = "key"


class UpstreamKind(enum.StrEnum):
    OPENAI = "openai"
    VLLM = "vllm"
    OPENAI_COMPATIBLE = "openai_compatible"


class StreamUsageMode(enum.StrEnum):
    INCLUDE_USAGE = "include_usage"
    PASSTHROUGH = "passthrough"


class RoutingStrategy(enum.StrEnum):
    WEIGHTED = "weighted"
    LEAST_IN_FLIGHT = "least_in_flight"
    LOWEST_LATENCY = "lowest_latency"


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
    SUPERUSER_TOKEN = "superuser_token"
    USER = "user"
    GATEWAY = "gateway"
    SYSTEM = "system"


# --------------------------------------------------------------------------- policy


class PolicyMixin:
    """Per-level policy carried by orgs, teams, projects and keys.

    Children may only narrow parents (enforced by the API, 422 otherwise):
    allowlist subset of parent, limits <= parent, budgets <= parent.

    - Model access: see `ModelAccess`; `allowed_models` must be empty unless mode is LIST.
    - Rate limits: NULL = no limit at this level. Each set level becomes one shared
      `RateLimitScope` ("<level>:<id>") in the compiled snapshot.
    - Budget: monthly (UTC calendar) hard limit and soft (notify) threshold in nano-USD,
      NULL = none. Spend lives in `usage_totals`, never on the entity row.
    """

    @declared_attr
    @classmethod
    def model_access(cls) -> Mapped[ModelAccess]:
        default = ModelAccess.ALL if cls.__name__ == "Org" else ModelAccess.INHERIT
        return mapped_column(
            _enum(ModelAccess, "model_access"),
            nullable=False,
            default=default,
            server_default=default.value,
        )

    allowed_models: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list, server_default=text("'{}'")
    )
    rpm_limit: Mapped[int | None] = mapped_column(Integer)
    tpm_limit: Mapped[int | None] = mapped_column(BigInteger)
    max_parallel: Mapped[int | None] = mapped_column(Integer)
    budget_nano_usd: Mapped[int | None] = mapped_column(BigInteger)
    soft_budget_nano_usd: Mapped[int | None] = mapped_column(BigInteger)

    @staticmethod
    def policy_constraints() -> tuple[CheckConstraint, ...]:
        return (
            CheckConstraint(
                "model_access = 'list' OR cardinality(allowed_models) = 0", name="allowlist_mode"
            ),
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
    is_superuser: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")


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


class Org(PolicyMixin, TimestampMixin, Base):
    __tablename__ = "orgs"
    __table_args__ = (
        *PolicyMixin.policy_constraints(),
        CheckConstraint("model_access <> 'inherit'", name="no_inherit"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)


class Team(PolicyMixin, TimestampMixin, Base):
    __tablename__ = "teams"
    __table_args__ = (
        *PolicyMixin.policy_constraints(),
        UniqueConstraint("org_id", "name"),
        # Target for composite FKs that pin children to the same org.
        UniqueConstraint("id", "org_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("orgs.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)


class Project(PolicyMixin, TimestampMixin, Base):
    __tablename__ = "projects"
    __table_args__ = (
        *PolicyMixin.policy_constraints(),
        ForeignKeyConstraint(
            ["team_id", "org_id"], ["teams.id", "teams.org_id"], ondelete="RESTRICT"
        ),
        UniqueConstraint("team_id", "name"),
        UniqueConstraint("id", "team_id", "org_id"),
        Index(None, "org_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    org_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    team_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)


class ApiKey(PolicyMixin, TimestampMixin, Base):
    """Virtual API key (`lw-...`). `id` is the public key_id used in snapshots and usage.

    Effective gateway status = REVOKED/SUSPENDED from `lifecycle`, else expired, else
    BUDGET_EXCEEDED if any of key/project/team/org is over budget in the current window,
    else ACTIVE. Keys are revoked, not deleted, so usage history keeps resolving.
    """

    __tablename__ = "api_keys"
    __table_args__ = (
        *PolicyMixin.policy_constraints(),
        _hash32("key_hash"),
        ForeignKeyConstraint(
            ["project_id", "team_id", "org_id"],
            ["projects.id", "projects.team_id", "projects.org_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "(lifecycle = 'revoked') = (revoked_at IS NOT NULL)", name="revoked_at_matches"
        ),
        Index(None, "project_id"),
        Index(None, "team_id"),
        Index(None, "org_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    org_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    team_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    key_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True)
    display_prefix: Mapped[str] = mapped_column(String(32), nullable=False)
    lifecycle: Mapped[KeyLifecycle] = mapped_column(
        _enum(KeyLifecycle, "key_lifecycle"),
        nullable=False,
        default=KeyLifecycle.ACTIVE,
        server_default=KeyLifecycle.ACTIVE.value,
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )


class Membership(TimestampMixin, Base):
    """Role grant. Org-level (team_id NULL): owner/admin. Team-level: admin/member/viewer.

    The superuser flag lives on `users`, not here (installation-level, not org-level).
    """

    __tablename__ = "memberships"
    __table_args__ = (
        ForeignKeyConstraint(
            ["team_id", "org_id"], ["teams.id", "teams.org_id"], ondelete="CASCADE"
        ),
        CheckConstraint(
            "(team_id IS NULL AND role IN ('owner', 'admin'))"
            " OR (team_id IS NOT NULL AND role IN ('admin', 'member', 'viewer'))",
            name="role_level",
        ),
        Index(
            "uq_memberships_org_role",
            "user_id",
            "org_id",
            unique=True,
            postgresql_where=text("team_id IS NULL"),
        ),
        Index(
            "uq_memberships_team_role",
            "user_id",
            "team_id",
            unique=True,
            postgresql_where=text("team_id IS NOT NULL"),
        ),
        Index(None, "org_id"),
        Index(None, "team_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    team_id: Mapped[uuid.UUID | None] = mapped_column()
    # OrgRole or TeamRole value; the CHECK above ties it to the level.
    role: Mapped[str] = mapped_column(String(16), nullable=False)


# --------------------------------------------------------------------------- routing


class Upstream(TimestampMixin, Base):
    __tablename__ = "upstreams"
    __table_args__ = (
        CheckConstraint(
            "api_key_ref IS NULL OR api_key_ref ~ '^[A-Za-z_][A-Za-z0-9_]*$'",
            name="api_key_ref_env_name",
        ),
        CheckConstraint("timeout_ms >= 0", name="timeout_nonneg"),
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


class Deployment(TimestampMixin, Base):
    """A concrete model on one upstream. Price NULL = unknown (not free); both or neither."""

    __tablename__ = "deployments"
    __table_args__ = (
        CheckConstraint("weight >= 0", name="weight_nonneg"),
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

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    upstream_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("upstreams.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    upstream_model: Mapped[str] = mapped_column(String(400), nullable=False)
    weight: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    price_input_nano_usd_per_mtok: Mapped[int | None] = mapped_column(BigInteger)
    price_output_nano_usd_per_mtok: Mapped[int | None] = mapped_column(BigInteger)


class Route(TimestampMixin, Base):
    """Public model alias -> ordered deployments (`route_deployments`)."""

    __tablename__ = "routes"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    model: Mapped[str] = mapped_column(String(400), nullable=False, unique=True)
    strategy: Mapped[RoutingStrategy] = mapped_column(
        _enum(RoutingStrategy, "routing_strategy"),
        nullable=False,
        default=RoutingStrategy.WEIGHTED,
        server_default=RoutingStrategy.WEIGHTED.value,
    )


class RouteDeployment(Base):
    __tablename__ = "route_deployments"
    __table_args__ = (
        PrimaryKeyConstraint("route_id", "deployment_id"),
        UniqueConstraint("route_id", "position"),
        Index(None, "deployment_id"),
    )

    route_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("routes.id", ondelete="CASCADE"), nullable=False
    )
    deployment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("deployments.id", ondelete="RESTRICT"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)


# --------------------------------------------------------------------------- config stream


class ConfigVersion(Base):
    """A published snapshot. Immutable: a DB trigger rejects any UPDATE except compaction
    (payload -> NULL with compacted_at set). Rollbacks are new rows of kind ROLLBACK.
    """

    __tablename__ = "config_versions"
    __table_args__ = (
        PrimaryKeyConstraint("scope", "version"),
        _hash32("digest"),
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint("(payload IS NULL) = (compacted_at IS NOT NULL)", name="compaction"),
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
        Index("ix_usage_events_org_ts", "org_id", "ts"),
        Index("ix_usage_events_team_ts", "team_id", "ts"),
        Index("ix_usage_events_project_ts", "project_id", "ts"),
        Index("ix_usage_events_key_ts", "key_id", "ts"),
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
    org_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    team_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    model: Mapped[str] = mapped_column(String(400), nullable=False)
    deployment: Mapped[str] = mapped_column(String(200), nullable=False)
    route: Mapped[str] = mapped_column(String(64), nullable=False)
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
    """Aggregated spend per hierarchy level per UTC month (`windows.month_window`).

    Each new event adds its cost once to the key, project, team and org rows. Blocking is
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
        Index("ix_audit_log_org_ts", "org_id", "ts"),
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
    # Hierarchy the resource belongs to, for scoped reads.
    org_id: Mapped[uuid.UUID | None] = mapped_column()
    team_id: Mapped[uuid.UUID | None] = mapped_column()
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )


class NotifierChannel(TimestampMixin, Base):
    """A configured notification target. `kind` is "webhook" (built-in) or the entry-point
    name of a `lumewarp.notifier` plugin. org_id NULL = installation-wide.
    """

    __tablename__ = "notifier_channels"
    __table_args__ = (Index(None, "org_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(String(100), nullable=False)
    org_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"))
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
