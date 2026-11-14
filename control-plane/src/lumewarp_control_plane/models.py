"""Database schema (source of truth for the control plane).

Conventions:
- Primary keys of entities are UUIDv7 (`ids.uuid7`); high-volume logs use BIGINT identity.
- Money is integer nano-USD (1e-9 USD) in BIGINT, matching `Price` / `UsageEvent` in the protos.
- Enums are stored as VARCHAR with a CHECK constraint (easy to extend in a migration).
- Secrets are never stored: API keys, PATs and gateway tokens are kept as sha256 digests
  (32 bytes) plus a short non-secret display prefix. See security.py for why sha256. The OIDC
  login nonce and PKCE verifier live only in the AES-GCM sealed login cookie
  (auth/oidc.py).
- The installation serves one config scope (`scopes` row); source tables (teams, routing,
  users, ...) are installation-wide and compile into that scope's snapshots.
- Hierarchy: org -> teams -> members (users) and service accounts -> keys. Every key
  belongs to one team and is owned by a member of it or by one of its service accounts.
  Org administration is inherited by the org's teams in `authz.RequestAuthz`; the backend
  only ever answers direct relations.
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


class ModelType(enum.StrEnum):
    LLM = "llm"
    EMBEDDING = "embedding"
    TRANSCRIPTION = "transcription"
    DECISION = "decision"


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


class InheritedFrom(enum.StrEnum):
    """Where an inherited admin role comes from (not stored); the higher source wins.

    An installation admin administers every org and team, an org admin its org's teams;
    either may also own keys there (`keys.owners`).
    """

    INSTALLATION = "installation"
    ORG = "org"


class InstallationRole(enum.StrEnum):
    """Installation-level role. `proxy_admin` is unique; `admin` may be assigned to many."""

    PROXY_ADMIN = "proxy_admin"
    ADMIN = "admin"


class KeyOwnerKind(enum.StrEnum):
    USER = "user"
    SERVICE_ACCOUNT = "service_account"


class ScopeType(enum.StrEnum):
    """Level a budget/total/limit belongs to: the org, the team, or the owner of a key (a
    team member, see `member_scope_id`, or a service account).

    `usage_totals` never holds an ORG row: org spend is derived by summing its teams'
    totals (see `usage.org_budget`). The value exists for audit resource types and for
    the rate-limit scope prefix.
    """

    ORG = "org"
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


class PluginKind(enum.StrEnum):
    """What a gateway plugin changes (proto PluginKind). The value is the manifest's `kind`,
    and the capability it advertises is `<prefix>:<name>` (see compiler/plugins.py).
    """

    ROUTER_STRATEGY = "router-strategy"
    UPSTREAM_AUTH = "upstream-auth"
    A2A_MIDDLEWARE = "a2a-middleware"


class TrafficKind(enum.StrEnum):
    """Which gateway protocol a usage event belongs to (proto TrafficKind). Only `llm`
    requests carry a model, tokens or cost: MCP and A2A have no budgets or limits.
    """

    LLM = "llm"
    MCP = "mcp"
    A2A = "a2a"


class McpReviewMode(enum.StrEnum):
    """How a gateway treats a definition it observes upstream (proto McpReviewMode).

    TOFU: a name seen for the first time is served and reported as auto-approved; a
    different definition of a known name is withheld until an administrator approves the
    hash. MANUAL: every definition not in the approved set is withheld. OFF: no check.
    """

    TOFU = "tofu"
    MANUAL = "manual"
    OFF = "off"


class McpDefinitionKind(enum.StrEnum):
    """Which kind of MCP definition a reviewed hash belongs to (proto McpDefinitionKind)."""

    TOOL = "tool"
    PROMPT = "prompt"


class McpDefinitionState(enum.StrEnum):
    """Where a reported definition stands. A definition the gateway serves is APPROVED;
    PENDING and REJECTED are never served, and a name that has any row is no longer
    "seen for the first time" (proto McpDefinitionState).
    """

    APPROVED = "approved"
    PENDING = "pending"
    REJECTED = "rejected"


# --------------------------------------------------------------------------- policy


def _access_default(cls: type) -> ModelAccess:
    """The root and the team level default to ALL (no restriction); a key owner defaults
    to INHERIT, exactly like `model_access`.
    """
    return ModelAccess.ALL if cls.__name__ in ("Org", "Team") else ModelAccess.INHERIT


class PolicyMixin:
    """Policy carried by orgs, teams and by key owners (members, service accounts).

    Four levels: org -> team -> owner -> key. The resolver in `quota.py` is the single
    place that computes the effective values: limits are the minimum down the chain,
    models the intersection, and an owner's limits fall back to the team's member
    defaults before being clamped. The API is the enforcement point that keeps source
    state narrowing its parent (only changed values are rechecked).

    - Model access: see `ModelAccess`. The list lives in `policy_grants` (by name id, so a
      rename keeps it); `allowed_models` is its read-only expansion to current names.
    - MCP and A2A access mirror model access at the same four levels, with the same mode
      enum and defaults: `mcp_server_grants` (one row per server, optionally narrowed to a
      tool list) and `a2a_agent_grants` (one row per agent).
    - Rate limits: NULL = no limit at this level. Each set level becomes one shared
      `RateLimitScope` ("org:<id>", "team:<id>", "member:<scope id>", "sa:<id>",
      "key:<id>").
    - Budget: monthly (UTC calendar) hard limit in nano-USD, NULL = none. Spend lives in
      `usage_totals` (the org level derives it from its teams), never on the entity row.
    """

    @declared_attr
    @classmethod
    def model_access(cls) -> Mapped[ModelAccess]:
        return mapped_column(
            _enum(ModelAccess, "model_access"),
            nullable=False,
            default=_access_default(cls),
            server_default=_access_default(cls).value,
        )

    @declared_attr
    @classmethod
    def mcp_access(cls) -> Mapped[ModelAccess]:
        """Model access's mirror for MCP servers and tools: ALL means every server with
        every approved tool, LIST exactly this level's `mcp_server_grants` intersected
        with the parent, INHERIT the parent's set unchanged.
        """
        return mapped_column(
            _enum(ModelAccess, "mcp_access"),
            nullable=False,
            default=_access_default(cls),
            server_default=_access_default(cls).value,
        )

    @declared_attr
    @classmethod
    def a2a_access(cls) -> Mapped[ModelAccess]:
        """Model access's mirror for A2A agents: ALL/LIST/INHERIT over
        `a2a_agent_grants`.
        """
        return mapped_column(
            _enum(ModelAccess, "a2a_access"),
            nullable=False,
            default=_access_default(cls),
            server_default=_access_default(cls).value,
        )

    rpm_limit: Mapped[int | None] = mapped_column(Integer)
    tpm_limit: Mapped[int | None] = mapped_column(BigInteger)
    max_parallel: Mapped[int | None] = mapped_column(Integer)
    budget_nano_usd: Mapped[int | None] = mapped_column(BigInteger)

    @staticmethod
    def policy_constraints() -> tuple[CheckConstraint, ...]:
        return (
            CheckConstraint("rpm_limit IS NULL OR rpm_limit > 0", name="rpm_positive"),
            CheckConstraint("tpm_limit IS NULL OR tpm_limit > 0", name="tpm_positive"),
            CheckConstraint("max_parallel IS NULL OR max_parallel > 0", name="parallel_positive"),
            CheckConstraint(
                "budget_nano_usd IS NULL OR budget_nano_usd >= 0", name="budget_nonneg"
            ),
        )


# --------------------------------------------------------------------------- installation


class Scope(Base):
    """A config scope (one per installation).

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


class InstallationAdmin(Base):
    """An installation-level role, read from Postgres on every request.

    The authorization backend is never asked about this: it only answers team relations.
    A partial unique index keeps at most one `proxy_admin`.

    `ON DELETE CASCADE`: the role is an attribute of the account, so deleting the account
    removes it. The API protects the proxy_admin account separately (only the account
    itself and break-glass may change or delete it).
    """

    __tablename__ = "installation_admins"
    __table_args__ = (
        Index(
            "uq_installation_admins_proxy_admin",
            "role",
            unique=True,
            postgresql_where=text("role = 'proxy_admin'"),
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[InstallationRole] = mapped_column(
        _enum(InstallationRole, "installation_role"), nullable=False
    )
    # The user who made the assignment; NULL for break-glass and the first-login bootstrap.
    assigned_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    assigned_at: Mapped[datetime] = _now()


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
    credential_fingerprint: Mapped[bytes | None] = mapped_column(LargeBinary)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True)
    csrf_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = _now()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = _now()
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OIDCLoginState(Base):
    """One pending browser login. The OIDC nonce and PKCE verifier are not stored: they are
    sealed into the encrypted login-state cookie (auth/oidc.py).
    """

    __tablename__ = "oidc_login_states"
    __table_args__ = (_hash32("state_hash"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    state_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True)
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


class Org(PolicyMixin, TimestampMixin, Base):
    """An organization: the grouping above teams and the root of the quota hierarchy.

    Policy (models, limits, budget) lives here and only an installation admin may set it
    (see `api/orgs.py`). An org can only be archived (`deleted_at`) when it has no live
    team; archiving is irreversible and no team may be created in it afterwards.

    The org has no `usage_totals` row: its spend is the sum of its teams' rows for the
    month (archived teams included), so an org total always stays consistent with the
    teams it contains.
    """

    __tablename__ = "orgs"
    __table_args__ = (
        *PolicyMixin.policy_constraints(),
        CheckConstraint(
            "model_access <> 'inherit' AND mcp_access <> 'inherit' AND a2a_access <> 'inherit'",
            name="no_inherit",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OrgAdmin(Base):
    """A user who administers one org.

    This is a direct relation (`admin` on `org:<id>`); its teams inherit the capability in
    `RequestAuthz`, never in the authorization backend. Under an external backend the rows
    are read-only like team memberships. `ON DELETE CASCADE` on both sides: the grant is an
    attribute of the org and of the account; `assigned_by` is kept as NULL if the assigner
    is deleted.
    """

    __tablename__ = "org_admins"
    __table_args__ = (PrimaryKeyConstraint("org_id", "user_id"), Index(None, "user_id"))

    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    assigned_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    assigned_at: Mapped[datetime] = _now()


class Team(PolicyMixin, TimestampMixin, Base):
    __tablename__ = "teams"
    __table_args__ = (
        *PolicyMixin.policy_constraints(),
        CheckConstraint(
            "model_access <> 'inherit' AND mcp_access <> 'inherit' AND a2a_access <> 'inherit'",
            name="no_inherit",
        ),
        CheckConstraint(
            "max_keys_per_member IS NULL OR max_keys_per_member > 0",
            name="max_keys_per_member_positive",
        ),
        CheckConstraint(
            "max_member_key_lifetime_seconds IS NULL OR max_member_key_lifetime_seconds > 0",
            name="max_member_key_lifetime_positive",
        ),
        CheckConstraint(
            "member_rpm_default IS NULL OR member_rpm_default > 0",
            name="member_rpm_default_positive",
        ),
        CheckConstraint(
            "member_tpm_default IS NULL OR member_tpm_default > 0",
            name="member_tpm_default_positive",
        ),
        CheckConstraint(
            "member_max_parallel_default IS NULL OR member_max_parallel_default > 0",
            name="member_max_parallel_default_positive",
        ),
        # A name is freed by archiving: unique only among live teams (moving a team
        # between orgs is still not supported).
        Index(
            "uq_teams_name",
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    # RESTRICT keeps the owning org from disappearing under its teams. Archiving a team
    # revokes its keys, deactivates its service accounts and leaves the row for history.
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("orgs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # Set once; an archived team is invisible to the API and to the compiler, and its
    # name is free for a new live team.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Per-member self-service limits. NULL = unlimited. They are source-only (not compiled
    # into snapshots): the API enforces them on create/rotate. Revoking a key frees a slot.
    max_keys_per_member: Mapped[int | None] = mapped_column(Integer)
    max_member_key_lifetime_seconds: Mapped[int | None] = mapped_column(Integer)
    # The member level's default limits: a member exception overrides them, and the
    # effective value is clamped by the team's own limit. Source-only, like the key
    # limits above; no model or budget default (those need an explicit policy row).
    member_rpm_default: Mapped[int | None] = mapped_column(Integer)
    member_tpm_default: Mapped[int | None] = mapped_column(BigInteger)
    member_max_parallel_default: Mapped[int | None] = mapped_column(Integer)


class MemberPolicy(PolicyMixin, TimestampMixin, Base):
    """One member's exception policy and grants, in a team.

    Deliberately independent of `team_members`: with an external authorization backend
    there are no membership rows, yet per-member quotas still live in Postgres and must
    apply. The absence of a row means "no exception": the team's member defaults and
    model set apply (see `quota.resolve_owner`).
    """

    __tablename__ = "member_policies"
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


class TeamMember(TimestampMixin, Base):
    """A user in a team, with a role. Policy lives in `member_policies`."""

    __tablename__ = "team_members"
    __table_args__ = (
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
    # A deactivated account keeps its rows (usage history) but the compiler leaves its
    # keys out of the snapshot. Reversible through PATCH.
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    @property
    def is_active(self) -> bool:
        return self.deactivated_at is None


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
        CheckConstraint(
            "previous_key_hash IS NULL OR octet_length(previous_key_hash) = 32",
            name="previous_key_hash_len",
        ),
        CheckConstraint(
            "(previous_key_hash IS NULL) = (previous_key_expires_at IS NULL)",
            name="previous_key_pair",
        ),
        Index(None, "team_id"),
        Index(None, "user_id"),
        Index(None, "service_account_id"),
        Index(
            "uq_api_keys_previous_key_hash",
            "previous_key_hash",
            unique=True,
            postgresql_where=text("previous_key_hash IS NOT NULL"),
        ),
        CheckConstraint("rpm_limit IS NULL OR rpm_limit > 0", name="rpm_positive"),
        CheckConstraint("tpm_limit IS NULL OR tpm_limit > 0", name="tpm_positive"),
        CheckConstraint("max_parallel IS NULL OR max_parallel > 0", name="parallel_positive"),
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
    # Rotation grace: the secret being replaced stays valid until its own expiry, so a
    # gateway that boots from its persisted snapshot enforces it without the control
    # plane. NULL = no grace. A second rotation drops the older of the two old hashes.
    previous_key_hash: Mapped[bytes | None] = mapped_column(LargeBinary)
    previous_key_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
    # The key level of the hierarchy. NULL = the key adds no limiter scope, so it costs
    # nothing at request time. A key may not exceed its owner's effective limits (API).
    rpm_limit: Mapped[int | None] = mapped_column(Integer)
    tpm_limit: Mapped[int | None] = mapped_column(BigInteger)
    max_parallel: Mapped[int | None] = mapped_column(Integer)

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
        CheckConstraint(
            "api_key_ref IS NULL OR api_key_ciphertext IS NULL", name="one_api_key_source"
        ),
        CheckConstraint("timeout_ms >= 0", name="timeout_nonneg"),
        # Health check: interval/timeout NULL = no active probing; the other settings
        # only exist together with them.
        CheckConstraint(
            "(health_check_interval_ms IS NULL) = (health_check_timeout_ms IS NULL)"
            " AND (health_check_interval_ms IS NOT NULL OR ("
            "health_check_unhealthy_threshold IS NULL"
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
    # The bearer token itself, sealed with LUMEWARP_ENCRYPTION_KEY (sealing.py); never
    # returned by the API. At most one of this and api_key_ref.
    api_key_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    # 0 = gateway default.
    timeout_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    stream_usage_mode: Mapped[StreamUsageMode] = mapped_column(
        _enum(StreamUsageMode, "stream_usage_mode"),
        nullable=False,
        default=StreamUsageMode.INCLUDE_USAGE,
        server_default=StreamUsageMode.INCLUDE_USAGE.value,
    )
    # Endpoint health URL, independent of active probing; NULL = <base>/models.
    # Active health probe (proto HealthCheck): NULL interval = not probed;
    # thresholds NULL = gateway defaults (3 / 1).
    health_url: Mapped[str | None] = mapped_column(Text)
    health_check_interval_ms: Mapped[int | None] = mapped_column(Integer)
    health_check_timeout_ms: Mapped[int | None] = mapped_column(Integer)
    health_check_unhealthy_threshold: Mapped[int | None] = mapped_column(Integer)
    health_check_healthy_threshold: Mapped[int | None] = mapped_column(Integer)
    # Last control-plane Test now of the saved settings (not a gateway check);
    # cleared when the settings it probed change.
    probe_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    probe_ok: Mapped[bool | None] = mapped_column(Boolean)
    probe_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Per-model circuit breaker (proto CircuitBreaker); NULL settings = gateway
    # defaults (5 failures / 30000 ms).
    circuit_breaker_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    circuit_failure_threshold: Mapped[int | None] = mapped_column(Integer)
    circuit_open_ms: Mapped[int | None] = mapped_column(Integer)
    # Prometheus endpoint for ROUTING_STRATEGY_VLLM_LOAD; NULL = derived from base_url.
    metrics_url: Mapped[str | None] = mapped_column(Text)


# --------------------------------------------------------------------------- mcp / a2a

# `^[a-z][a-z0-9_-]{0,62}$`: the name of an MCP server or A2A agent (snapshot.proto). The
# gateway splits aggregated tool names at the first "__", so a name may not contain it
# (L4): the split must be unambiguous.
TARGET_NAME_SQL = r"^[a-z][a-z0-9_-]{0,62}$"


def target_name_constraints() -> tuple[CheckConstraint, ...]:
    return (
        CheckConstraint(f"name ~ '{TARGET_NAME_SQL}'", name="name_charset"),
        CheckConstraint("position('__' in name) = 0", name="name_no_separator"),
    )


def _live_name_index(table: str) -> Index:
    """Unique among live rows: archiving (`deleted_at`) frees the name for reuse."""
    return Index(
        f"uq_{table}_name", "name", unique=True, postgresql_where=text("deleted_at IS NULL")
    )


class McpServer(TimestampMixin, Base):
    """An upstream MCP server (Streamable HTTP) whose tools and prompts the gateway proxies.

    `auth_plugin`/`auth_params` name an upstream-auth plugin: the built-ins
    `none`/`static_bearer`/`passthrough` or a WASM plugin a gateway reports. Only
    `static_bearer` takes a credential, stored exactly like `Upstream`: the name of an env
    var (`secret_ref`) or the token sealed with LUMEWARP_ENCRYPTION_KEY under HKDF info
    `lumewarp/upstream-auth-secret/v1`. The value is never returned by the API.

    `review_mode` decides how the gateway treats definitions it observes (McpReviewMode).
    The definitions themselves live in `mcp_definitions`, one row per observed hash.
    Deleting archives (`deleted_at`): the name becomes free, definitions and grants stay
    for history, and the compiler leaves the server out.
    """

    __tablename__ = "mcp_servers"
    __table_args__ = (
        *target_name_constraints(),
        CheckConstraint("timeout_ms >= 0", name="timeout_nonneg"),
        CheckConstraint(
            "secret_ref IS NULL OR secret_ref ~ '^[A-Za-z_][A-Za-z0-9_]*$'",
            name="secret_ref_env_name",
        ),
        CheckConstraint(
            "secret_ref IS NULL OR secret_ciphertext IS NULL", name="one_secret_source"
        ),
        CheckConstraint("auth_timeout_ms >= 0", name="auth_timeout_nonneg"),
        _live_name_index("mcp_servers"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    name: Mapped[str] = mapped_column(String(63), nullable=False)
    # The Streamable HTTP endpoint; absolute http(s) URL without userinfo (validated in
    # compiler/validate.py, mirrored by the gateway).
    url: Mapped[str] = mapped_column(Text, nullable=False)
    # Built-in name or a WASM manifest name (PluginKind.UPSTREAM_AUTH).
    auth_plugin: Mapped[str] = mapped_column(
        String(63), nullable=False, default="none", server_default="none"
    )
    # Plugin settings: a JSON object, at most 16 KiB. Never a credential.
    auth_params: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    # static_bearer only: name of the env var on the gateway holding the token; never the value.
    secret_ref: Mapped[str | None] = mapped_column(String(200))
    # static_bearer only: the token sealed with LUMEWARP_ENCRYPTION_KEY (sealing.py). At most
    # one of this and secret_ref.
    secret_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    # How long one call to a WASM auth plugin may run before the gateway cancels it
    # (proto PluginRef.timeout_ms). 0 = 60 s.
    auth_timeout_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # Per upstream request; 0 = the gateway default.
    timeout_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # The scope's config revision (`scopes.source_revision`) of the last write to this row.
    # Provenance for a CatalogReport: the gateway reports the config version it served, and
    # that version is trusted only when it was compiled from a revision at least this new
    # (otherwise the report may describe a different upstream, L3). Observed definition and
    # card data never move it; only an API write (or a rollback restoring this row) does.
    config_revision: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    review_mode: Mapped[McpReviewMode] = mapped_column(
        _enum(McpReviewMode, "mcp_review_mode"),
        nullable=False,
        default=McpReviewMode.TOFU,
        server_default=McpReviewMode.TOFU.value,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class McpDefinition(TimestampMixin, Base):
    """One definition hash observed at a server, with the review decision on it.

    `(server_id, kind, name, hash)` is unique: a changed definition is a new row, and the
    approved row it replaces stays until the administrator cleans it up. `definition_json`
    is the canonical projection the hash was computed over (mcp/hashing.py), stored as
    text because it is shown as text. A definition deleted with its server is gone
    (CASCADE); archiving the server keeps the rows so a rejected definition never comes
    back as "first sight".
    """

    __tablename__ = "mcp_definitions"
    __table_args__ = (
        UniqueConstraint("server_id", "kind", "name", "hash"),
        CheckConstraint("octet_length(hash) = 32", name="hash_len"),
        Index(None, "server_id", "kind", "name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    server_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("mcp_servers.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[McpDefinitionKind] = mapped_column(
        _enum(McpDefinitionKind, "mcp_definition_kind"), nullable=False
    )
    # Upstream name, without the `<server>__` prefix.
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # Canonical JSON of the projected definition (what `hash` covers).
    definition_json: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[McpDefinitionState] = mapped_column(
        _enum(McpDefinitionState, "mcp_definition_state"), nullable=False
    )
    first_seen_at: Mapped[datetime] = _now()
    last_seen_at: Mapped[datetime] = _now()
    # Who approved or rejected it; NULL while pending and for a TOFU approval.
    decided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class A2aAgent(TimestampMixin, Base):
    """An upstream A2A agent (JSON-RPC binding) the gateway proxies.

    Auth is `McpServer`'s (`auth_plugin`/`auth_params`/`secret_ref`/`secret_ciphertext`,
    same sealing info). `middleware_plugin`/`middleware_params` name at most one
    a2a-middleware plugin. `card_json` is the card an administrator pasted and that the
    gateway serves until it fetched one itself; `last_card_json`/`last_card_error` are
    what the newest gateway fetch reported (display only). Deleting archives, like a
    server.
    """

    __tablename__ = "a2a_agents"
    __table_args__ = (
        *target_name_constraints(),
        CheckConstraint("timeout_ms >= 0", name="timeout_nonneg"),
        CheckConstraint(
            "secret_ref IS NULL OR secret_ref ~ '^[A-Za-z_][A-Za-z0-9_]*$'",
            name="secret_ref_env_name",
        ),
        CheckConstraint(
            "secret_ref IS NULL OR secret_ciphertext IS NULL", name="one_secret_source"
        ),
        CheckConstraint("auth_timeout_ms >= 0", name="auth_timeout_nonneg"),
        CheckConstraint("middleware_timeout_ms >= 0", name="middleware_timeout_nonneg"),
        _live_name_index("a2a_agents"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    name: Mapped[str] = mapped_column(String(63), nullable=False)
    # The agent's JSON-RPC endpoint; absolute http(s) URL without userinfo.
    url: Mapped[str] = mapped_column(Text, nullable=False)
    # Empty = the origin of `url` plus "/.well-known/agent-card.json" (the gateway derives it).
    card_url: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    auth_plugin: Mapped[str] = mapped_column(
        String(63), nullable=False, default="none", server_default="none"
    )
    auth_params: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    secret_ref: Mapped[str | None] = mapped_column(String(200))
    secret_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    # At most one middleware; empty = none.
    middleware_plugin: Mapped[str] = mapped_column(
        String(63), nullable=False, default="", server_default=""
    )
    middleware_params: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    # WASM plugin timeouts (proto PluginRef.timeout_ms): 0 = 60 s. The auth one applies to
    # the agent's outbound auth, the middleware one to each request/response hook call.
    auth_timeout_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    middleware_timeout_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    timeout_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # The scope's config revision of the last write to this row; a fetched card never moves
    # it (see McpServer.config_revision).
    config_revision: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    # Card an administrator pasted (JSON object, at most 64 KiB); served until the gateway
    # has fetched one. Never trusted: shown as text and rewritten by the gateway.
    card_json: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    last_card_json: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    last_card_error: Mapped[str] = mapped_column(
        String(1000), nullable=False, default="", server_default=""
    )
    last_card_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


def _owner_shape() -> CheckConstraint:
    """`PolicyGrant`'s four owner shapes, shared by the MCP and A2A grant tables."""
    return CheckConstraint(
        "(org_id IS NOT NULL AND team_id IS NULL AND user_id IS NULL"
        " AND service_account_id IS NULL)"
        " OR (org_id IS NULL AND team_id IS NOT NULL"
        " AND NOT (user_id IS NOT NULL AND service_account_id IS NOT NULL))",
        name="owner_shape",
    )


def _owner_indexes(table: str, target_column: str) -> tuple[Index, ...]:
    """One row per owner and target, like `PolicyGrant`'s four partial unique indexes."""
    return (
        Index(
            f"uq_{table}_org",
            "org_id",
            target_column,
            unique=True,
            postgresql_where=text("org_id IS NOT NULL"),
        ),
        Index(
            f"uq_{table}_team",
            "team_id",
            target_column,
            unique=True,
            postgresql_where=text(
                "user_id IS NULL AND service_account_id IS NULL AND org_id IS NULL"
            ),
        ),
        Index(
            f"uq_{table}_member",
            "team_id",
            "user_id",
            target_column,
            unique=True,
            postgresql_where=text("user_id IS NOT NULL"),
        ),
        Index(
            f"uq_{table}_service_account",
            "service_account_id",
            target_column,
            unique=True,
            postgresql_where=text("service_account_id IS NOT NULL"),
        ),
        Index(None, target_column),
    )


class McpServerGrant(Base):
    """An org, team, member or service account's grant of one MCP server.

    Stored exactly like `PolicyGrant` (one row per owner and target, four owner shapes).
    `all_tools` grants every approved tool; otherwise `tools` names the allowed ones
    (prompts follow the server). A level with `mcp_access=LIST` contributes exactly its
    rows; the compiler intersects them along org -> team -> owner, and a key inherits its
    owner's set unchanged (there is no key-level list, as for models).
    """

    __tablename__ = "mcp_server_grants"
    __table_args__ = (
        _owner_shape(),
        *_owner_indexes("mcp_server_grants", "server_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    server_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("mcp_servers.id", ondelete="CASCADE"), nullable=False
    )
    org_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"))
    team_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID | None] = mapped_column()
    service_account_id: Mapped[uuid.UUID | None] = mapped_column()
    # Every approved tool of the server (the wire McpGrant.all_tools).
    all_tools: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # Upstream tool names (without the `<server>__` prefix); ignored when all_tools.
    tools: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list, server_default=text("'{}'::text[]")
    )


class A2aAgentGrant(Base):
    """An org, team, member or service account's grant of one A2A agent."""

    __tablename__ = "a2a_agent_grants"
    __table_args__ = (
        _owner_shape(),
        *_owner_indexes("a2a_agent_grants", "agent_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("a2a_agents.id", ondelete="CASCADE"), nullable=False
    )
    org_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"))
    team_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID | None] = mapped_column()
    service_account_id: Mapped[uuid.UUID | None] = mapped_column()


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
    """One typed public name -> one upstream model. All prices NULL = unknown, not free.
    Token pairs are complete except for embedding (input only); transcription can add
    audio pricing. Calling directly is a single attempt; weights live on router members.
    """

    __tablename__ = "models"
    __table_args__ = (
        _name_fk(),
        CheckConstraint("kind = 'model'", name="kind_model"),
        CheckConstraint(
            "(model_type = 'embedding' AND (price_output_nano_usd_per_mtok IS NULL"
            " OR price_output_nano_usd_per_mtok = 0) AND (price_output_nano_usd_per_mtok IS NULL"
            " OR price_input_nano_usd_per_mtok IS NOT NULL)) OR (model_type <> 'embedding' AND"
            " (price_input_nano_usd_per_mtok IS NULL) = (price_output_nano_usd_per_mtok IS NULL))",
            name="price_both_or_neither",
        ),
        CheckConstraint(
            "(price_input_nano_usd_per_mtok IS NULL OR price_input_nano_usd_per_mtok >= 0)"
            " AND (price_output_nano_usd_per_mtok IS NULL OR price_output_nano_usd_per_mtok >= 0)"
            " AND (price_nano_usd_per_audio_second IS NULL"
            " OR price_nano_usd_per_audio_second >= 0)",
            name="price_nonneg",
        ),
        CheckConstraint(
            "model_type = 'transcription' OR price_nano_usd_per_audio_second IS NULL",
            name="audio_price_type",
        ),
        CheckConstraint(
            "model_type <> 'transcription' OR default_body IS NULL", name="default_body_type"
        ),
        CheckConstraint(
            "model_type = 'llm' OR (max_output_tokens IS NULL AND reasoning_mode = 'passthrough'"
            " AND reasoning_fragments IS NULL AND reasoning_default_level IS NULL)",
            name="llm_settings_type",
        ),
        CheckConstraint(
            "(max_input_tokens IS NULL OR max_input_tokens > 0)"
            " AND (max_output_tokens IS NULL OR max_output_tokens > 0)",
            name="token_limits_positive",
        ),
        CheckConstraint(
            "reasoning_mode IN ('passthrough', 'strip', 'mapped')", name="reasoning_mode"
        ),
        CheckConstraint(
            "(reasoning_mode = 'mapped') = (reasoning_fragments IS NOT NULL)"
            " AND (reasoning_mode <> 'strip' OR reasoning_default_level IS NULL)",
            name="reasoning_shape",
        ),
        CheckConstraint(
            "(chat_check_interval_ms IS NULL) = (chat_check_timeout_ms IS NULL)"
            " AND (chat_check_interval_ms IS NOT NULL OR (chat_check_unhealthy_threshold IS NULL"
            " AND chat_check_healthy_threshold IS NULL))",
            name="chat_check_shape",
        ),
        CheckConstraint(
            "chat_check_interval_ms IS NULL OR (chat_check_interval_ms >= 1000"
            " AND chat_check_timeout_ms > 0"
            " AND chat_check_timeout_ms < chat_check_interval_ms)",
            name="chat_check_bounds",
        ),
        CheckConstraint(
            "(chat_check_unhealthy_threshold IS NULL OR chat_check_unhealthy_threshold > 0)"
            " AND (chat_check_healthy_threshold IS NULL OR chat_check_healthy_threshold > 0)",
            name="chat_check_thresholds",
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
    model_type: Mapped[ModelType] = mapped_column(_enum(ModelType, "model_type"), nullable=False)
    price_nano_usd_per_audio_second: Mapped[int | None] = mapped_column(BigInteger)
    price_input_nano_usd_per_mtok: Mapped[int | None] = mapped_column(BigInteger)
    price_output_nano_usd_per_mtok: Mapped[int | None] = mapped_column(BigInteger)
    # Context window, informational (the gateway does not tokenize). NULL = unknown.
    max_input_tokens: Mapped[int | None] = mapped_column(Integer)
    # Enforced by the gateway on max_tokens / max_completion_tokens. NULL = none.
    max_output_tokens: Mapped[int | None] = mapped_column(Integer)
    # Merged under every request body (the client's fields win). NULL = none.
    default_body: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    # reasoning_effort handling: passthrough | strip | mapped (level -> fragment object).
    reasoning_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="passthrough", server_default="passthrough"
    )
    reasoning_fragments: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    reasoning_default_level: Mapped[str | None] = mapped_column(String(32))
    # Periodic type-specific probe by the gateway; interval NULL = not probed.
    chat_check_interval_ms: Mapped[int | None] = mapped_column(Integer)
    chat_check_timeout_ms: Mapped[int | None] = mapped_column(Integer)
    chat_check_unhealthy_threshold: Mapped[int | None] = mapped_column(Integer)
    chat_check_healthy_threshold: Mapped[int | None] = mapped_column(Integer)
    # Last control-plane Test now of the saved settings (not a gateway check);
    # cleared when the settings it probed change.
    probe_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    probe_ok: Mapped[bool | None] = mapped_column(Boolean)
    probe_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

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

    Retry, fallback, traffic split and strategy exist only here. Resilience: an
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
        CheckConstraint("plugin_timeout_ms >= 0", name="plugin_timeout_nonneg"),
    )

    name_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(
        String(32), nullable=False, default="router", server_default="router"
    )
    # A router-strategy plugin's name: a built-in or one a gateway reports (compiler/plugins.py).
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
    # How long one strategy `pick` (a WASM plugin) may run before the gateway cancels it
    # (proto Router.plugin_timeout_ms). 0 = the gateway default, 60 s.
    plugin_timeout_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

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
    """A name in the `allowed_models` of an org (only `org_id` set), a team (only
    `team_id` set), a member (team and user) or a service account. By name id, so a
    rename keeps it.
    """

    __tablename__ = "policy_grants"
    __table_args__ = (
        ForeignKeyConstraint(
            ["team_id", "user_id"],
            ["member_policies.team_id", "member_policies.user_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["service_account_id", "team_id"],
            ["service_accounts.id", "service_accounts.team_id"],
            ondelete="CASCADE",
        ),
        # Exactly four shapes: org only, team only, team + member, team + service account.
        CheckConstraint(
            "(org_id IS NOT NULL AND team_id IS NULL AND user_id IS NULL"
            " AND service_account_id IS NULL)"
            " OR (org_id IS NULL AND team_id IS NOT NULL"
            " AND NOT (user_id IS NOT NULL AND service_account_id IS NOT NULL))",
            name="owner_shape",
        ),
        Index(
            "uq_policy_grants_org",
            "org_id",
            "name_id",
            unique=True,
            postgresql_where=text("org_id IS NOT NULL"),
        ),
        Index(
            "uq_policy_grants_team",
            "team_id",
            "name_id",
            unique=True,
            postgresql_where=text(
                "user_id IS NULL AND service_account_id IS NULL AND org_id IS NULL"
            ),
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
    org_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"))
    team_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
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
Org.allowed_models = column_property(_granted_names(PolicyGrant.org_id == Org.id))
MemberPolicy.allowed_models = column_property(
    _granted_names(
        PolicyGrant.team_id == MemberPolicy.team_id, PolicyGrant.user_id == MemberPolicy.user_id
    )
)
ServiceAccount.allowed_models = column_property(
    _granted_names(PolicyGrant.service_account_id == ServiceAccount.id)
)


def _granted_mcp_servers(*owner) -> Any:
    """The owner's MCP grants as `[{server, all_tools, tools}]`, server-ordered.

    Empty array, not NULL, when the level has none; an archived server is left out. This
    is the read-only mirror of `allowed_models` for the policy responses.
    """
    return (
        select(
            func.coalesce(
                func.jsonb_agg(
                    aggregate_order_by(
                        func.jsonb_build_object(
                            "server",
                            McpServer.name,
                            "all_tools",
                            McpServerGrant.all_tools,
                            "tools",
                            McpServerGrant.tools,
                        ),
                        McpServer.name,
                    )
                ),
                cast(text("'[]'"), JSONB),
            )
        )
        .select_from(McpServerGrant)
        .join(McpServer, McpServer.id == McpServerGrant.server_id)
        .where(*owner, McpServer.deleted_at.is_(None))
        .correlate_except(McpServerGrant, McpServer)
        .scalar_subquery()
    )


def _granted_agents(*owner) -> Any:
    """The owner's A2A grants as current agent names, sorted (empty array when none)."""
    return (
        select(
            func.coalesce(
                func.array_agg(aggregate_order_by(cast(A2aAgent.name, Text), A2aAgent.name)),
                cast(text("'{}'"), ARRAY(Text)),
            )
        )
        .select_from(A2aAgentGrant)
        .join(A2aAgent, A2aAgent.id == A2aAgentGrant.agent_id)
        .where(*owner, A2aAgent.deleted_at.is_(None))
        .correlate_except(A2aAgentGrant, A2aAgent)
        .scalar_subquery()
    )


def _mcp_owner_columns(row: type) -> tuple:
    """Owner predicates for a policy row, matching `PolicyGrant`'s four shapes."""
    if row is Org:
        return (McpServerGrant.org_id == Org.id,)
    if row is Team:
        return (
            McpServerGrant.team_id == Team.id,
            McpServerGrant.org_id.is_(None),
            McpServerGrant.user_id.is_(None),
            McpServerGrant.service_account_id.is_(None),
        )
    if row is MemberPolicy:
        return (
            McpServerGrant.team_id == MemberPolicy.team_id,
            McpServerGrant.user_id == MemberPolicy.user_id,
        )
    return (
        McpServerGrant.team_id == ServiceAccount.team_id,
        McpServerGrant.service_account_id == ServiceAccount.id,
    )


def _agent_owner_columns(row: type) -> tuple:
    """`_mcp_owner_columns` for the A2A grant table."""
    if row is Org:
        return (A2aAgentGrant.org_id == Org.id,)
    if row is Team:
        return (
            A2aAgentGrant.team_id == Team.id,
            A2aAgentGrant.org_id.is_(None),
            A2aAgentGrant.user_id.is_(None),
            A2aAgentGrant.service_account_id.is_(None),
        )
    if row is MemberPolicy:
        return (
            A2aAgentGrant.team_id == MemberPolicy.team_id,
            A2aAgentGrant.user_id == MemberPolicy.user_id,
        )
    return (
        A2aAgentGrant.team_id == ServiceAccount.team_id,
        A2aAgentGrant.service_account_id == ServiceAccount.id,
    )


for _policy_row in (Org, Team, MemberPolicy, ServiceAccount):
    _policy_row.mcp_servers = column_property(
        _granted_mcp_servers(*_mcp_owner_columns(_policy_row))
    )
    _policy_row.a2a_agents = column_property(_granted_agents(*_agent_owner_columns(_policy_row)))


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
    """Gateway credential (`lwg-...`): a scope credential with a label, not bound to a
    gateway id. Any number of gateways may authenticate with one token. Hash only.
    """

    __tablename__ = "gateway_tokens"
    __table_args__ = (_hash32("token_hash"), Index(None, "scope"))

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    scope: Mapped[str] = mapped_column(
        ForeignKey("scopes.name", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False, server_default="")
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True)
    display_prefix: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = _now()
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GatewaySession(Base):
    """Last known stream state per gateway process, for status/UI. A gateway is one
    process: a restart is a new gateway id and a new row. Authoritative ACK/pending state
    is in memory (`grpc.SessionRegistry`); this row mirrors it. A row outlives its stream
    for `GATEWAY_SESSION_RETENTION`, so an offline gateway stays visible for a day.
    """

    __tablename__ = "gateway_sessions"
    __table_args__ = (PrimaryKeyConstraint("scope", "gateway_id"),)

    scope: Mapped[str] = mapped_column(
        ForeignKey("scopes.name", ondelete="RESTRICT"), nullable=False
    )
    # Hello.gateway_id: this process. `[A-Za-z0-9._-]`, at most 100 characters.
    gateway_id: Mapped[str] = mapped_column(String(100), nullable=False)
    session_id: Mapped[uuid.UUID | None] = mapped_column()
    # The token of the latest Hello; revoking it closes this gateway's stream.
    token_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("gateway_tokens.id", ondelete="CASCADE"), nullable=False
    )
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
    # Hello.shared_limits: display only. Quarantine is scope-wide, in GatewayQuarantine.
    shared_limits_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    shared_limits_namespace: Mapped[str] = mapped_column(
        String(200), nullable=False, server_default=""
    )
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disconnected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Latest HealthReport: name -> healthy, probed upstreams / models only.
    upstream_health: Mapped[dict[str, bool]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    model_health: Mapped[dict[str, bool]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )


class GatewayQuarantine(Base):
    """A config version a gateway rejected as INVALID_CONFIG: no gateway in the scope is
    sent it (docs/config-protocol.md). Every gateway gets the same snapshot, so the version is
    withheld scope-wide; the row outlives sessions, and a newer published version is tried
    again from scratch.
    """

    __tablename__ = "gateway_quarantines"
    __table_args__ = (PrimaryKeyConstraint("scope"),)

    scope: Mapped[str] = mapped_column(
        ForeignKey("scopes.name", ondelete="RESTRICT"), nullable=False
    )
    quarantined_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[datetime] = _now()


class GatewayPlugin(Base):
    """A plugin a gateway reported in Hello. Untrusted data, kept so forms and validation
    work while it is offline; `compiler.plugins.load_plugins` decides what still counts.
    Keyed per gateway: a plugin is available only if every connected gateway reports it.
    """

    __tablename__ = "gateway_plugins"
    __table_args__ = (
        PrimaryKeyConstraint("scope", "gateway_id", "kind", "name"),
        ForeignKeyConstraint(
            ["scope", "gateway_id"],
            ["gateway_sessions.scope", "gateway_sessions.gateway_id"],
            ondelete="CASCADE",
        ),
    )

    scope: Mapped[str] = mapped_column(String(128), nullable=False)
    gateway_id: Mapped[str] = mapped_column(String(100), nullable=False)
    kind: Mapped[PluginKind] = mapped_column(_enum(PluginKind, "plugin_kind"), nullable=False)
    name: Mapped[str] = mapped_column(String(63), nullable=False)
    digest: Mapped[str] = mapped_column(String(128), nullable=False)
    descriptor: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # The report stops counting when this token is revoked.
    token_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("gateway_tokens.id", ondelete="CASCADE"), nullable=False
    )
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# --------------------------------------------------------------------------- usage


class UsageEvent(Base):
    """One gateway request. Deduped on `request_id` (ON CONFLICT DO NOTHING); only newly
    inserted LLM events add to `usage_totals` (MCP and A2A have no budgets). No FKs: a
    high-volume append-only log.
    """

    __tablename__ = "usage_events"
    __table_args__ = (
        CheckConstraint(
            "prompt_tokens >= 0 AND completion_tokens >= 0"
            " AND audio_ms >= 0 AND cost_nano_usd >= 0",
            name="nonneg",
        ),
        Index(None, "ts"),
        Index("ix_usage_events_team_ts", "team_id", "ts"),
        Index("ix_usage_events_owner_ts", "owner_id", "ts"),
        Index("ix_usage_events_key_ts", "key_id", "ts"),
        Index("ix_usage_events_model_id_ts", "model_id", "ts"),
        Index("ix_usage_events_team_model_id_ts", "team_id", "model_id", "ts"),
        Index(
            "ix_usage_events_alias_id_ts",
            "alias_id",
            "ts",
            postgresql_where=text("alias_id IS NOT NULL"),
        ),
        Index(
            "ix_usage_events_router_id_ts",
            "router_id",
            "ts",
            postgresql_where=text("router_id IS NOT NULL"),
        ),
        Index("ix_usage_events_model_ts", "model", "ts"),
        Index("ix_usage_events_upstream_model_ts", "upstream_model", "ts"),
        Index("ix_usage_events_kind_ts", "kind", "ts"),
        CheckConstraint("attempts >= 0", name="attempts_nonneg"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    request_id: Mapped[uuid.UUID] = mapped_column(nullable=False, unique=True)
    # The reporting process (ReportRequest.gateway_id), never derived from the token.
    gateway_id: Mapped[str] = mapped_column(String(100), nullable=False)
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
    kind: Mapped[TrafficKind] = mapped_column(
        _enum(TrafficKind, "traffic_kind"),
        nullable=False,
        default=TrafficKind.LLM,
        server_default="llm",
    )
    # The McpServer or A2aAgent called; empty on an LLM request or one that named none the
    # key may see.
    target: Mapped[str] = mapped_column(String(200), nullable=False, default="", server_default="")
    # The JSON-RPC method ("tools/call", "message/stream", ...); empty on an LLM request.
    method: Mapped[str] = mapped_column(String(128), nullable=False, default="", server_default="")
    # The MCP tool or prompt an MCP request named; empty otherwise.
    tool: Mapped[str] = mapped_column(String(400), nullable=False, default="", server_default="")
    model_id: Mapped[uuid.UUID | None] = mapped_column()
    alias_id: Mapped[uuid.UUID | None] = mapped_column()
    router_id: Mapped[uuid.UUID | None] = mapped_column()
    audio_ms: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
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


class OrgBudgetWindow(Base):
    """The first time an org's derived spend reached its budget in one UTC month.

    There is no org row in `usage_totals`: org spend is summed from its teams' rows. This
    table only marks "already reported", so the budget audit row and the recompile are
    written exactly once. `INSERT ... ON CONFLICT DO NOTHING` makes that race-free.
    """

    __tablename__ = "org_budget_windows"
    __table_args__ = (PrimaryKeyConstraint("org_id", "window_start"),)

    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    exceeded_at: Mapped[datetime] = _now()


class UsageTotal(Base):
    """Aggregated spend per level per UTC month (`windows.month_window`).

    Each new event adds its cost once to the team row and once to the key owner's row
    (member or service account). Blocking is
    derived (cost_nano_usd >= the level's current budget_nano_usd); `exceeded_at` records
    when that was first seen, for audit.
    """

    __tablename__ = "usage_totals"
    __table_args__ = (
        PrimaryKeyConstraint("scope_type", "scope_id", "window_start"),
        CheckConstraint(
            "cost_nano_usd >= 0 AND prompt_tokens >= 0 AND completion_tokens >= 0"
            " AND audio_ms >= 0 AND request_count >= 0 AND unknown_cost_count >= 0",
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
    audio_ms: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    # Events whose cost is unknown (no price or usage not REPORTED).
    unknown_cost_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    exceeded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = _now()


# --------------------------------------------------------------------------- audit


class AuditLog(Base):
    """Who did what. `details` must never contain secrets, Authorization headers or
    prompts; write rows through `audit.record`, which redacts sensitive keys.
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        Index(None, "ts"),
        Index("ix_audit_log_team_ts", "team_id", "ts"),
        Index("ix_audit_log_org_ts", "org_id", "ts"),
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
    # The team's org at write time, so an org admin reads its teams' rows and the row
    # survives the team. No FK: audit outlives the entities it mentions.
    org_id: Mapped[uuid.UUID | None] = mapped_column()
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
