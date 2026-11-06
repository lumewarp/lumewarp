"""baseline

Revision ID: 0001
Revises:

The one and only schema migration. Lumewarp has never been deployed, so the history was
squashed into a baseline generated from `models.py`: a fresh database runs this file and
nothing else, and there is no upgrade path from an earlier revision. When the schema
changes, edit `models.py` and regenerate this file (the migration test compares the two).

Org data is not seeded here. The `orgs` table starts empty; `orgs.default_org` creates the
`default` org on the first team creation, so there is no backfill and no migration-time
data. `member_policies` rows are not required for membership either: the authz backend
owns memberships, while per-member quotas stay here.

The only object the models cannot express is the `config_versions` immutability trigger;
it lives at the end of `upgrade()`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Published snapshots never change. The only allowed update is compaction: payload and
# the catalog sidecar -> NULL together with compacted_at, everything else identical.
CONFIG_VERSIONS_IMMUTABLE = """
CREATE FUNCTION lumewarp_config_versions_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (to_jsonb(NEW) - ARRAY['payload', 'catalog_state', 'compacted_at'])
        IS DISTINCT FROM
        (to_jsonb(OLD) - ARRAY['payload', 'catalog_state', 'compacted_at'])
        OR (NEW.payload IS DISTINCT FROM OLD.payload AND NEW.payload IS NOT NULL)
        OR (NEW.catalog_state IS DISTINCT FROM OLD.catalog_state
            AND NOT (NEW.catalog_state IS NULL AND NEW.payload IS NULL
                     AND NEW.compacted_at IS NOT NULL))
        OR (NEW.payload IS NULL AND NEW.catalog_state IS NOT NULL)
        OR (OLD.compacted_at IS NOT NULL
            AND NEW.compacted_at IS DISTINCT FROM OLD.compacted_at)
    THEN
        RAISE EXCEPTION 'config_versions rows are immutable (only compaction is allowed)'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END
$$;
"""


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column(
            "ts", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "actor_type",
            sa.Enum(
                "break_glass",
                "user",
                "gateway",
                "system",
                name="actor_type",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("actor_id", sa.String(length=200), nullable=True),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=False),
        sa.Column("resource_id", sa.String(length=200), nullable=True),
        sa.Column("team_id", sa.Uuid(), nullable=True),
        sa.Column("org_id", sa.Uuid(), nullable=True),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )
    op.create_index("ix_audit_log_org_ts", "audit_log", ["org_id", "ts"], unique=False)
    op.create_index("ix_audit_log_team_ts", "audit_log", ["team_id", "ts"], unique=False)
    op.create_index(op.f("ix_audit_log_ts"), "audit_log", ["ts"], unique=False)
    op.create_table(
        "catalog_name_history",
        sa.Column("name_id", sa.Uuid(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "model",
                "alias",
                "router",
                name="catalog_kind",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("old_name", sa.String(length=400), nullable=False),
        sa.Column(
            "renamed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "name_id", "old_name", "renamed_at", name=op.f("pk_catalog_name_history")
        ),
    )
    op.create_index(
        op.f("ix_catalog_name_history_old_name"),
        "catalog_name_history",
        ["old_name", "kind"],
        unique=False,
    )
    op.create_table(
        "catalog_names",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=400), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "model",
                "alias",
                "router",
                name="catalog_kind",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_catalog_names")),
        sa.UniqueConstraint("id", "kind", name=op.f("uq_catalog_names_id_kind")),
        sa.UniqueConstraint("name", name=op.f("uq_catalog_names_name")),
    )
    op.create_table(
        "oidc_login_states",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("state_hash", sa.LargeBinary(), nullable=False),
        sa.Column("return_path", sa.Text(), nullable=False),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "octet_length(state_hash) = 32", name=op.f("ck_oidc_login_states_state_hash_len")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_oidc_login_states")),
        sa.UniqueConstraint("state_hash", name=op.f("uq_oidc_login_states_state_hash")),
    )
    op.create_table(
        "orgs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rpm_limit", sa.Integer(), nullable=True),
        sa.Column("tpm_limit", sa.BigInteger(), nullable=True),
        sa.Column("max_parallel", sa.Integer(), nullable=True),
        sa.Column("budget_nano_usd", sa.BigInteger(), nullable=True),
        sa.Column(
            "model_access",
            sa.Enum(
                "inherit",
                "all",
                "list",
                name="model_access",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="all",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("model_access <> 'inherit'", name=op.f("ck_orgs_no_inherit")),
        sa.CheckConstraint(
            "budget_nano_usd IS NULL OR budget_nano_usd >= 0", name=op.f("ck_orgs_budget_nonneg")
        ),
        sa.CheckConstraint(
            "max_parallel IS NULL OR max_parallel > 0", name=op.f("ck_orgs_parallel_positive")
        ),
        sa.CheckConstraint("rpm_limit IS NULL OR rpm_limit > 0", name=op.f("ck_orgs_rpm_positive")),
        sa.CheckConstraint("tpm_limit IS NULL OR tpm_limit > 0", name=op.f("ck_orgs_tpm_positive")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_orgs")),
        sa.UniqueConstraint("name", name=op.f("uq_orgs_name")),
    )
    op.create_table(
        "scopes",
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("source_revision", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("name", name=op.f("pk_scopes")),
    )
    op.create_table(
        "upstreams",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "openai",
                "vllm",
                "openai_compatible",
                name="upstream_kind",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("api_key_ref", sa.String(length=200), nullable=True),
        sa.Column("timeout_ms", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "stream_usage_mode",
            sa.Enum(
                "include_usage",
                "passthrough",
                name="stream_usage_mode",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="include_usage",
            nullable=False,
        ),
        sa.Column("health_check_path", sa.String(length=400), nullable=True),
        sa.Column("health_check_interval_ms", sa.Integer(), nullable=True),
        sa.Column("health_check_timeout_ms", sa.Integer(), nullable=True),
        sa.Column("health_check_unhealthy_threshold", sa.Integer(), nullable=True),
        sa.Column("health_check_healthy_threshold", sa.Integer(), nullable=True),
        sa.Column(
            "circuit_breaker_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("circuit_failure_threshold", sa.Integer(), nullable=True),
        sa.Column("circuit_open_ms", sa.Integer(), nullable=True),
        sa.Column("metrics_url", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "api_key_ref IS NULL OR api_key_ref ~ '^[A-Za-z_][A-Za-z0-9_]*$'",
            name=op.f("ck_upstreams_api_key_ref_env_name"),
        ),
        sa.CheckConstraint(
            "(circuit_failure_threshold IS NULL OR circuit_failure_threshold > 0) AND (circuit_open_ms IS NULL OR circuit_open_ms > 0)",
            name=op.f("ck_upstreams_circuit_breaker_positive"),
        ),
        sa.CheckConstraint(
            "(health_check_interval_ms IS NULL) = (health_check_timeout_ms IS NULL) AND (health_check_interval_ms IS NOT NULL OR (health_check_path IS NULL AND health_check_unhealthy_threshold IS NULL AND health_check_healthy_threshold IS NULL))",
            name=op.f("ck_upstreams_health_check_shape"),
        ),
        sa.CheckConstraint(
            "(health_check_unhealthy_threshold IS NULL OR health_check_unhealthy_threshold > 0) AND (health_check_healthy_threshold IS NULL OR health_check_healthy_threshold > 0)",
            name=op.f("ck_upstreams_health_check_thresholds"),
        ),
        sa.CheckConstraint(
            "circuit_breaker_enabled OR (circuit_failure_threshold IS NULL AND circuit_open_ms IS NULL)",
            name=op.f("ck_upstreams_circuit_breaker_shape"),
        ),
        sa.CheckConstraint(
            "health_check_interval_ms IS NULL OR (health_check_interval_ms >= 1000 AND health_check_timeout_ms > 0 AND health_check_timeout_ms < health_check_interval_ms)",
            name=op.f("ck_upstreams_health_check_bounds"),
        ),
        sa.CheckConstraint("timeout_ms >= 0", name=op.f("ck_upstreams_timeout_nonneg")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_upstreams")),
        sa.UniqueConstraint("name", name=op.f("uq_upstreams_name")),
    )
    op.create_table(
        "usage_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("gateway_id", sa.String(length=200), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("key_id", sa.Uuid(), nullable=False),
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.Column(
            "owner_kind",
            sa.Enum(
                "user",
                "service_account",
                name="key_owner_kind",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("requested", sa.String(length=400), nullable=False),
        sa.Column("model", sa.String(length=400), nullable=False),
        sa.Column("alias", sa.String(length=400), server_default="", nullable=False),
        sa.Column("router", sa.String(length=400), server_default="", nullable=False),
        sa.Column("upstream", sa.String(length=200), server_default="", nullable=False),
        sa.Column("upstream_model", sa.String(length=400), server_default="", nullable=False),
        sa.Column("fallback", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("attempts", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("model_id", sa.Uuid(), nullable=True),
        sa.Column("alias_id", sa.Uuid(), nullable=True),
        sa.Column("router_id", sa.Uuid(), nullable=True),
        sa.Column("endpoint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.SmallInteger(), nullable=False),
        sa.Column("stream", sa.Boolean(), nullable=False),
        sa.Column("prompt_tokens", sa.BigInteger(), nullable=False),
        sa.Column("completion_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cost_nano_usd", sa.BigInteger(), nullable=False),
        sa.Column("cost_known", sa.Boolean(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("config_version", sa.BigInteger(), nullable=False),
        sa.Column(
            "usage_state",
            sa.Enum(
                "unspecified",
                "reported",
                "missing",
                "partial",
                "invalid",
                name="usage_state",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "outcome",
            sa.Enum(
                "unspecified",
                "completed",
                "client_cancelled",
                "upstream_error",
                "rejected",
                name="request_outcome",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_usage_events_attempts_nonneg")),
        sa.CheckConstraint(
            "prompt_tokens >= 0 AND completion_tokens >= 0 AND cost_nano_usd >= 0",
            name=op.f("ck_usage_events_nonneg"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_usage_events")),
        sa.UniqueConstraint("request_id", name=op.f("uq_usage_events_request_id")),
    )
    op.create_index(
        "ix_usage_events_alias_id_ts",
        "usage_events",
        ["alias_id", "ts"],
        unique=False,
        postgresql_where=sa.text("alias_id IS NOT NULL"),
    )
    op.create_index("ix_usage_events_key_ts", "usage_events", ["key_id", "ts"], unique=False)
    op.create_index("ix_usage_events_model_id_ts", "usage_events", ["model_id", "ts"], unique=False)
    op.create_index("ix_usage_events_model_ts", "usage_events", ["model", "ts"], unique=False)
    op.create_index("ix_usage_events_owner_ts", "usage_events", ["owner_id", "ts"], unique=False)
    op.create_index(
        "ix_usage_events_router_id_ts",
        "usage_events",
        ["router_id", "ts"],
        unique=False,
        postgresql_where=sa.text("router_id IS NOT NULL"),
    )
    op.create_index(
        "ix_usage_events_team_model_id_ts",
        "usage_events",
        ["team_id", "model_id", "ts"],
        unique=False,
    )
    op.create_index("ix_usage_events_team_ts", "usage_events", ["team_id", "ts"], unique=False)
    op.create_index(op.f("ix_usage_events_ts"), "usage_events", ["ts"], unique=False)
    op.create_index(
        "ix_usage_events_upstream_model_ts", "usage_events", ["upstream_model", "ts"], unique=False
    )
    op.create_table(
        "usage_totals",
        sa.Column(
            "scope_type",
            sa.Enum(
                "org",
                "team",
                "member",
                "service_account",
                name="scope_type",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("scope_id", sa.Uuid(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cost_nano_usd", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("prompt_tokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("completion_tokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("request_count", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("unknown_cost_count", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("exceeded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "cost_nano_usd >= 0 AND prompt_tokens >= 0 AND completion_tokens >= 0 AND request_count >= 0 AND unknown_cost_count >= 0",
            name=op.f("ck_usage_totals_nonneg"),
        ),
        sa.PrimaryKeyConstraint(
            "scope_type", "scope_id", "window_start", name=op.f("pk_usage_totals")
        ),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("display_name", sa.String(length=200), server_default="", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
    )
    op.create_index(
        "uq_users_email_lower", "users", [sa.literal_column("lower(email)")], unique=True
    )
    op.create_table(
        "aliases",
        sa.Column("name_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), server_default="alias", nullable=False),
        sa.Column("target_name_id", sa.Uuid(), nullable=False),
        sa.Column("target_kind", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("kind = 'alias'", name=op.f("ck_aliases_kind_alias")),
        sa.CheckConstraint(
            "target_kind IN ('model', 'router')", name=op.f("ck_aliases_target_not_alias")
        ),
        sa.ForeignKeyConstraint(
            ["name_id", "kind"],
            ["catalog_names.id", "catalog_names.kind"],
            name=op.f("fk_aliases_name_id_kind_catalog_names"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["target_name_id", "target_kind"],
            ["catalog_names.id", "catalog_names.kind"],
            name=op.f("fk_aliases_target_name_id_target_kind_catalog_names"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("name_id", name=op.f("pk_aliases")),
    )
    op.create_index(op.f("ix_aliases_target_name_id"), "aliases", ["target_name_id"], unique=False)
    op.create_table(
        "config_outbox",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("scope", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("source_revision", sa.BigInteger(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "done",
                "failed",
                name="outbox_status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("published_version", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_config_outbox_attempts_nonneg")),
        sa.ForeignKeyConstraint(
            ["scope"],
            ["scopes.name"],
            name=op.f("fk_config_outbox_scope_scopes"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_config_outbox")),
    )
    op.create_index(
        "ix_config_outbox_pending",
        "config_outbox",
        ["scope", "id"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_table(
        "config_versions",
        sa.Column("scope", sa.String(length=128), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("payload", sa.LargeBinary(), nullable=True),
        sa.Column(
            "catalog_state",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("digest", sa.LargeBinary(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("key_count", sa.Integer(), nullable=False),
        sa.Column("source_revision", sa.BigInteger(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "compile",
                "rollback",
                name="config_version_kind",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("rollback_of_version", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("compacted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(kind = 'rollback') = (rollback_of_version IS NOT NULL)",
            name=op.f("ck_config_versions_rollback_ref"),
        ),
        sa.CheckConstraint(
            "(payload IS NULL) = (compacted_at IS NOT NULL)",
            name=op.f("ck_config_versions_compaction"),
        ),
        sa.CheckConstraint("octet_length(digest) = 32", name=op.f("ck_config_versions_digest_len")),
        sa.CheckConstraint(
            "payload IS NOT NULL OR catalog_state IS NULL",
            name=op.f("ck_config_versions_catalog_state_compaction"),
        ),
        sa.CheckConstraint("version > 0", name=op.f("ck_config_versions_version_positive")),
        sa.ForeignKeyConstraint(
            ["scope"],
            ["scopes.name"],
            name=op.f("fk_config_versions_scope_scopes"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("scope", "version", name=op.f("pk_config_versions")),
    )
    op.create_index(
        op.f("ix_config_versions_scope"),
        "config_versions",
        ["scope", "source_revision"],
        unique=False,
    )
    op.create_table(
        "gateway_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("scope", sa.String(length=128), nullable=False),
        sa.Column("gateway_id", sa.String(length=200), nullable=False),
        sa.Column("name", sa.String(length=200), server_default="", nullable=False),
        sa.Column("token_hash", sa.LargeBinary(), nullable=False),
        sa.Column("display_prefix", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "octet_length(token_hash) = 32", name=op.f("ck_gateway_tokens_token_hash_len")
        ),
        sa.ForeignKeyConstraint(
            ["scope"],
            ["scopes.name"],
            name=op.f("fk_gateway_tokens_scope_scopes"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_gateway_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_gateway_tokens_token_hash")),
    )
    op.create_index(
        op.f("ix_gateway_tokens_scope"), "gateway_tokens", ["scope", "gateway_id"], unique=False
    )
    op.create_table(
        "installation_admins",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "role",
            sa.Enum(
                "proxy_admin",
                "admin",
                name="installation_role",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("assigned_by", sa.Uuid(), nullable=True),
        sa.Column(
            "assigned_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["assigned_by"],
            ["users.id"],
            name=op.f("fk_installation_admins_assigned_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_installation_admins_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_installation_admins")),
    )
    op.create_index(
        "uq_installation_admins_proxy_admin",
        "installation_admins",
        ["role"],
        unique=True,
        postgresql_where=sa.text("role = 'proxy_admin'"),
    )
    op.create_table(
        "models",
        sa.Column("name_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), server_default="model", nullable=False),
        sa.Column("upstream_id", sa.Uuid(), nullable=False),
        sa.Column("upstream_model", sa.String(length=400), nullable=False),
        sa.Column("price_input_nano_usd_per_mtok", sa.BigInteger(), nullable=True),
        sa.Column("price_output_nano_usd_per_mtok", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("kind = 'model'", name=op.f("ck_models_kind_model")),
        sa.CheckConstraint(
            "(price_input_nano_usd_per_mtok IS NULL) = (price_output_nano_usd_per_mtok IS NULL)",
            name=op.f("ck_models_price_both_or_neither"),
        ),
        sa.CheckConstraint(
            "price_input_nano_usd_per_mtok IS NULL OR (price_input_nano_usd_per_mtok >= 0 AND price_output_nano_usd_per_mtok >= 0)",
            name=op.f("ck_models_price_nonneg"),
        ),
        sa.ForeignKeyConstraint(
            ["name_id", "kind"],
            ["catalog_names.id", "catalog_names.kind"],
            name=op.f("fk_models_name_id_kind_catalog_names"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["upstream_id"],
            ["upstreams.id"],
            name=op.f("fk_models_upstream_id_upstreams"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("name_id", name=op.f("pk_models")),
    )
    op.create_index(op.f("ix_models_upstream_id"), "models", ["upstream_id"], unique=False)
    op.create_table(
        "org_admins",
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("assigned_by", sa.Uuid(), nullable=True),
        sa.Column(
            "assigned_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["assigned_by"],
            ["users.id"],
            name=op.f("fk_org_admins_assigned_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_org_admins_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_org_admins_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("org_id", "user_id", name=op.f("pk_org_admins")),
    )
    op.create_index(op.f("ix_org_admins_user_id"), "org_admins", ["user_id"], unique=False)
    op.create_table(
        "org_budget_windows",
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "exceeded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["orgs.id"],
            name=op.f("fk_org_budget_windows_org_id_orgs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("org_id", "window_start", name=op.f("pk_org_budget_windows")),
    )
    op.create_table(
        "personal_access_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(), nullable=False),
        sa.Column("display_prefix", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "octet_length(token_hash) = 32", name=op.f("ck_personal_access_tokens_token_hash_len")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_personal_access_tokens_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_personal_access_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_personal_access_tokens_token_hash")),
    )
    op.create_index(
        op.f("ix_personal_access_tokens_user_id"),
        "personal_access_tokens",
        ["user_id"],
        unique=False,
    )
    op.create_table(
        "routers",
        sa.Column("name_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), server_default="router", nullable=False),
        sa.Column("strategy", sa.String(length=63), server_default="weighted", nullable=False),
        sa.Column(
            "params",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("retry_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("retry_max_attempts", sa.SmallInteger(), nullable=True),
        sa.Column(
            "retry_on_status",
            postgresql.ARRAY(sa.SmallInteger()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("retry_total_timeout_ms", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "sticky",
            sa.Enum(
                "none",
                "header",
                "team",
                "key",
                name="sticky_by",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="none",
            nullable=False,
        ),
        sa.Column("sticky_header", sa.String(length=200), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(sticky = 'header') = (sticky_header IS NOT NULL)",
            name=op.f("ck_routers_sticky_header_iff"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(params) = 'object'", name=op.f("ck_routers_params_object")
        ),
        sa.CheckConstraint("kind = 'router'", name=op.f("ck_routers_kind_router")),
        sa.CheckConstraint(
            "strategy ~ '^[a-z][a-z0-9_]{0,62}$'", name=op.f("ck_routers_strategy_name")
        ),
        sa.CheckConstraint(
            "400 <= ALL (retry_on_status) AND 599 >= ALL (retry_on_status)",
            name=op.f("ck_routers_retry_status_range"),
        ),
        sa.CheckConstraint(
            "retry_enabled OR (retry_max_attempts IS NULL AND cardinality(retry_on_status) = 0 AND retry_total_timeout_ms = 0)",
            name=op.f("ck_routers_retry_shape"),
        ),
        sa.CheckConstraint(
            "retry_max_attempts IS NULL OR retry_max_attempts BETWEEN 1 AND 5",
            name=op.f("ck_routers_retry_max_attempts_range"),
        ),
        sa.CheckConstraint(
            "retry_total_timeout_ms >= 0", name=op.f("ck_routers_retry_timeout_nonneg")
        ),
        sa.ForeignKeyConstraint(
            ["name_id", "kind"],
            ["catalog_names.id", "catalog_names.kind"],
            name=op.f("fk_routers_name_id_kind_catalog_names"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("name_id", name=op.f("pk_routers")),
    )
    op.create_table(
        "teams",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("max_keys_per_member", sa.Integer(), nullable=True),
        sa.Column("max_member_key_lifetime_seconds", sa.Integer(), nullable=True),
        sa.Column("member_rpm_default", sa.Integer(), nullable=True),
        sa.Column("member_tpm_default", sa.BigInteger(), nullable=True),
        sa.Column("member_max_parallel_default", sa.Integer(), nullable=True),
        sa.Column("rpm_limit", sa.Integer(), nullable=True),
        sa.Column("tpm_limit", sa.BigInteger(), nullable=True),
        sa.Column("max_parallel", sa.Integer(), nullable=True),
        sa.Column("budget_nano_usd", sa.BigInteger(), nullable=True),
        sa.Column(
            "model_access",
            sa.Enum(
                "inherit",
                "all",
                "list",
                name="model_access",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="all",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("model_access <> 'inherit'", name=op.f("ck_teams_no_inherit")),
        sa.CheckConstraint(
            "budget_nano_usd IS NULL OR budget_nano_usd >= 0", name=op.f("ck_teams_budget_nonneg")
        ),
        sa.CheckConstraint(
            "max_keys_per_member IS NULL OR max_keys_per_member > 0",
            name=op.f("ck_teams_max_keys_per_member_positive"),
        ),
        sa.CheckConstraint(
            "max_member_key_lifetime_seconds IS NULL OR max_member_key_lifetime_seconds > 0",
            name=op.f("ck_teams_max_member_key_lifetime_positive"),
        ),
        sa.CheckConstraint(
            "max_parallel IS NULL OR max_parallel > 0", name=op.f("ck_teams_parallel_positive")
        ),
        sa.CheckConstraint(
            "member_max_parallel_default IS NULL OR member_max_parallel_default > 0",
            name=op.f("ck_teams_member_max_parallel_default_positive"),
        ),
        sa.CheckConstraint(
            "member_rpm_default IS NULL OR member_rpm_default > 0",
            name=op.f("ck_teams_member_rpm_default_positive"),
        ),
        sa.CheckConstraint(
            "member_tpm_default IS NULL OR member_tpm_default > 0",
            name=op.f("ck_teams_member_tpm_default_positive"),
        ),
        sa.CheckConstraint(
            "rpm_limit IS NULL OR rpm_limit > 0", name=op.f("ck_teams_rpm_positive")
        ),
        sa.CheckConstraint(
            "tpm_limit IS NULL OR tpm_limit > 0", name=op.f("ck_teams_tpm_positive")
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_teams_org_id_orgs"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_teams")),
        sa.UniqueConstraint("name", name=op.f("uq_teams_name")),
    )
    op.create_index(op.f("ix_teams_org_id"), "teams", ["org_id"], unique=False)
    op.create_table(
        "user_identities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("issuer", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_login_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_identities_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_identities")),
        sa.UniqueConstraint("issuer", "subject", name=op.f("uq_user_identities_issuer_subject")),
    )
    op.create_index(
        op.f("ix_user_identities_user_id"), "user_identities", ["user_id"], unique=False
    )
    op.create_table(
        "user_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.String(length=20), server_default="session", nullable=False),
        sa.Column("credential_fingerprint", sa.LargeBinary(), nullable=True),
        sa.Column("token_hash", sa.LargeBinary(), nullable=False),
        sa.Column("csrf_hash", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(kind = 'session' AND user_id IS NOT NULL) OR (kind = 'break_glass' AND user_id IS NULL)",
            name=op.f("ck_user_sessions_kind"),
        ),
        sa.CheckConstraint(
            "octet_length(csrf_hash) = 32", name=op.f("ck_user_sessions_csrf_hash_len")
        ),
        sa.CheckConstraint(
            "octet_length(token_hash) = 32", name=op.f("ck_user_sessions_token_hash_len")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_sessions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_sessions")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_user_sessions_token_hash")),
    )
    op.create_index(op.f("ix_user_sessions_user_id"), "user_sessions", ["user_id"], unique=False)
    op.create_table(
        "gateway_sessions",
        sa.Column("scope", sa.String(length=128), nullable=False),
        sa.Column("gateway_id", sa.String(length=200), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=True),
        sa.Column("token_id", sa.Uuid(), nullable=True),
        sa.Column("connected", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("gateway_version", sa.String(length=100), server_default="", nullable=False),
        sa.Column(
            "capabilities",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("peer", sa.String(length=200), nullable=True),
        sa.Column("hello_have_version", sa.BigInteger(), nullable=True),
        sa.Column("acked_version", sa.BigInteger(), nullable=True),
        sa.Column("pending_version", sa.BigInteger(), nullable=True),
        sa.Column("last_nack_version", sa.BigInteger(), nullable=True),
        sa.Column("last_nack_code", sa.String(length=64), nullable=True),
        sa.Column("last_nack_reason", sa.Text(), nullable=True),
        sa.Column("quarantined_version", sa.BigInteger(), nullable=True),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disconnected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["scope"],
            ["scopes.name"],
            name=op.f("fk_gateway_sessions_scope_scopes"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["token_id"],
            ["gateway_tokens.id"],
            name=op.f("fk_gateway_sessions_token_id_gateway_tokens"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("scope", "gateway_id", name=op.f("pk_gateway_sessions")),
    )
    op.create_table(
        "member_policies",
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("rpm_limit", sa.Integer(), nullable=True),
        sa.Column("tpm_limit", sa.BigInteger(), nullable=True),
        sa.Column("max_parallel", sa.Integer(), nullable=True),
        sa.Column("budget_nano_usd", sa.BigInteger(), nullable=True),
        sa.Column(
            "model_access",
            sa.Enum(
                "inherit",
                "all",
                "list",
                name="model_access",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="inherit",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "budget_nano_usd IS NULL OR budget_nano_usd >= 0",
            name=op.f("ck_member_policies_budget_nonneg"),
        ),
        sa.CheckConstraint(
            "max_parallel IS NULL OR max_parallel > 0",
            name=op.f("ck_member_policies_parallel_positive"),
        ),
        sa.CheckConstraint(
            "rpm_limit IS NULL OR rpm_limit > 0", name=op.f("ck_member_policies_rpm_positive")
        ),
        sa.CheckConstraint(
            "tpm_limit IS NULL OR tpm_limit > 0", name=op.f("ck_member_policies_tpm_positive")
        ),
        sa.ForeignKeyConstraint(
            ["team_id"],
            ["teams.id"],
            name=op.f("fk_member_policies_team_id_teams"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_member_policies_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("team_id", "user_id", name=op.f("pk_member_policies")),
    )
    op.create_index(
        op.f("ix_member_policies_user_id"), "member_policies", ["user_id"], unique=False
    )
    op.create_table(
        "router_fallbacks",
        sa.Column("router_id", sa.Uuid(), nullable=False),
        sa.Column("model_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["model_id"],
            ["models.name_id"],
            name=op.f("fk_router_fallbacks_model_id_models"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["router_id"],
            ["routers.name_id"],
            name=op.f("fk_router_fallbacks_router_id_routers"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("router_id", "model_id", name=op.f("pk_router_fallbacks")),
        sa.UniqueConstraint(
            "router_id", "position", name=op.f("uq_router_fallbacks_router_id_position")
        ),
    )
    op.create_index(
        op.f("ix_router_fallbacks_model_id"), "router_fallbacks", ["model_id"], unique=False
    )
    op.create_table(
        "router_members",
        sa.Column("router_id", sa.Uuid(), nullable=False),
        sa.Column("model_id", sa.Uuid(), nullable=False),
        sa.Column("weight", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "params",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "jsonb_typeof(params) = 'object'", name=op.f("ck_router_members_params_object")
        ),
        sa.CheckConstraint("weight >= 0", name=op.f("ck_router_members_weight_nonneg")),
        sa.ForeignKeyConstraint(
            ["model_id"],
            ["models.name_id"],
            name=op.f("fk_router_members_model_id_models"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["router_id"],
            ["routers.name_id"],
            name=op.f("fk_router_members_router_id_routers"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("router_id", "model_id", name=op.f("pk_router_members")),
        sa.UniqueConstraint(
            "router_id", "position", name=op.f("uq_router_members_router_id_position")
        ),
    )
    op.create_index(
        op.f("ix_router_members_model_id"), "router_members", ["model_id"], unique=False
    )
    op.create_table(
        "router_splits",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("router_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=32), nullable=False),
        sa.Column("weight", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "name ~ '^[A-Za-z0-9_.-]{1,32}$'", name=op.f("ck_router_splits_name_charset")
        ),
        sa.CheckConstraint("weight >= 0", name=op.f("ck_router_splits_weight_nonneg")),
        sa.ForeignKeyConstraint(
            ["router_id"],
            ["routers.name_id"],
            name=op.f("fk_router_splits_router_id_routers"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_router_splits")),
        sa.UniqueConstraint("router_id", "name", name=op.f("uq_router_splits_router_id_name")),
        sa.UniqueConstraint(
            "router_id", "position", name=op.f("uq_router_splits_router_id_position")
        ),
    )
    op.create_table(
        "service_accounts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("rpm_limit", sa.Integer(), nullable=True),
        sa.Column("tpm_limit", sa.BigInteger(), nullable=True),
        sa.Column("max_parallel", sa.Integer(), nullable=True),
        sa.Column("budget_nano_usd", sa.BigInteger(), nullable=True),
        sa.Column(
            "model_access",
            sa.Enum(
                "inherit",
                "all",
                "list",
                name="model_access",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="inherit",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "budget_nano_usd IS NULL OR budget_nano_usd >= 0",
            name=op.f("ck_service_accounts_budget_nonneg"),
        ),
        sa.CheckConstraint(
            "max_parallel IS NULL OR max_parallel > 0",
            name=op.f("ck_service_accounts_parallel_positive"),
        ),
        sa.CheckConstraint(
            "rpm_limit IS NULL OR rpm_limit > 0", name=op.f("ck_service_accounts_rpm_positive")
        ),
        sa.CheckConstraint(
            "tpm_limit IS NULL OR tpm_limit > 0", name=op.f("ck_service_accounts_tpm_positive")
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f("fk_service_accounts_created_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["team_id"],
            ["teams.id"],
            name=op.f("fk_service_accounts_team_id_teams"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_service_accounts")),
        sa.UniqueConstraint("id", "team_id", name=op.f("uq_service_accounts_id_team_id")),
        sa.UniqueConstraint("team_id", "name", name=op.f("uq_service_accounts_team_id_name")),
    )
    op.create_table(
        "team_members",
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "role",
            sa.Enum(
                "admin",
                "member",
                name="team_role",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["team_id"],
            ["teams.id"],
            name=op.f("fk_team_members_team_id_teams"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_team_members_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("team_id", "user_id", name=op.f("pk_team_members")),
    )
    op.create_index(op.f("ix_team_members_user_id"), "team_members", ["user_id"], unique=False)
    op.create_table(
        "api_keys",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.Column(
            "owner_kind",
            sa.Enum(
                "user",
                "service_account",
                name="key_owner_kind",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("service_account_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("key_hash", sa.LargeBinary(), nullable=False),
        sa.Column("previous_key_hash", sa.LargeBinary(), nullable=True),
        sa.Column("previous_key_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("display_prefix", sa.String(length=32), nullable=False),
        sa.Column(
            "lifecycle",
            sa.Enum(
                "active",
                "suspended",
                "revoked",
                name="key_lifecycle",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="active",
            nullable=False,
        ),
        sa.Column("suspended_reason", sa.String(length=64), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("rpm_limit", sa.Integer(), nullable=True),
        sa.Column("tpm_limit", sa.BigInteger(), nullable=True),
        sa.Column("max_parallel", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(lifecycle = 'revoked') = (revoked_at IS NOT NULL)",
            name=op.f("ck_api_keys_revoked_at_matches"),
        ),
        sa.CheckConstraint(
            "(owner_kind = 'user' AND user_id IS NOT NULL AND service_account_id IS NULL) OR (owner_kind = 'service_account' AND service_account_id IS NOT NULL AND user_id IS NULL)",
            name=op.f("ck_api_keys_owner_matches_kind"),
        ),
        sa.CheckConstraint(
            "suspended_reason IS NULL OR lifecycle = 'suspended'",
            name=op.f("ck_api_keys_reason_when_suspended"),
        ),
        sa.CheckConstraint(
            "(previous_key_hash IS NULL) = (previous_key_expires_at IS NULL)",
            name=op.f("ck_api_keys_previous_key_pair"),
        ),
        sa.CheckConstraint(
            "max_parallel IS NULL OR max_parallel > 0", name=op.f("ck_api_keys_parallel_positive")
        ),
        sa.CheckConstraint("octet_length(key_hash) = 32", name=op.f("ck_api_keys_key_hash_len")),
        sa.CheckConstraint(
            "previous_key_hash IS NULL OR octet_length(previous_key_hash) = 32",
            name=op.f("ck_api_keys_previous_key_hash_len"),
        ),
        sa.CheckConstraint(
            "rpm_limit IS NULL OR rpm_limit > 0", name=op.f("ck_api_keys_rpm_positive")
        ),
        sa.CheckConstraint(
            "tpm_limit IS NULL OR tpm_limit > 0", name=op.f("ck_api_keys_tpm_positive")
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f("fk_api_keys_created_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["service_account_id", "team_id"],
            ["service_accounts.id", "service_accounts.team_id"],
            name=op.f("fk_api_keys_service_account_id_team_id_service_accounts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["team_id"], ["teams.id"], name=op.f("fk_api_keys_team_id_teams"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_api_keys_user_id_users"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_keys")),
        sa.UniqueConstraint("key_hash", name=op.f("uq_api_keys_key_hash")),
    )
    op.create_index(
        op.f("ix_api_keys_service_account_id"), "api_keys", ["service_account_id"], unique=False
    )
    op.create_index(op.f("ix_api_keys_team_id"), "api_keys", ["team_id"], unique=False)
    op.create_index(op.f("ix_api_keys_user_id"), "api_keys", ["user_id"], unique=False)
    op.create_index(
        "uq_api_keys_previous_key_hash",
        "api_keys",
        ["previous_key_hash"],
        unique=True,
        postgresql_where=sa.text("previous_key_hash IS NOT NULL"),
    )
    op.create_table(
        "gateway_strategies",
        sa.Column("scope", sa.String(length=128), nullable=False),
        sa.Column("gateway_id", sa.String(length=200), nullable=False),
        sa.Column("name", sa.String(length=63), nullable=False),
        sa.Column("digest", sa.String(length=128), nullable=False),
        sa.Column("descriptor", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("token_id", sa.Uuid(), nullable=False),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["scope", "gateway_id"],
            ["gateway_sessions.scope", "gateway_sessions.gateway_id"],
            name=op.f("fk_gateway_strategies_scope_gateway_id_gateway_sessions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["token_id"],
            ["gateway_tokens.id"],
            name=op.f("fk_gateway_strategies_token_id_gateway_tokens"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("scope", "gateway_id", "name", name=op.f("pk_gateway_strategies")),
    )
    op.create_table(
        "policy_grants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name_id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=True),
        sa.Column("team_id", sa.Uuid(), nullable=True),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("service_account_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "(org_id IS NOT NULL AND team_id IS NULL AND user_id IS NULL"
            " AND service_account_id IS NULL)"
            " OR (org_id IS NULL AND team_id IS NOT NULL"
            " AND NOT (user_id IS NOT NULL AND service_account_id IS NOT NULL))",
            name=op.f("ck_policy_grants_owner_shape"),
        ),
        sa.ForeignKeyConstraint(
            ["name_id"],
            ["catalog_names.id"],
            name=op.f("fk_policy_grants_name_id_catalog_names"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_policy_grants_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["service_account_id", "team_id"],
            ["service_accounts.id", "service_accounts.team_id"],
            name=op.f("fk_policy_grants_service_account_id_team_id_service_accounts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["team_id", "user_id"],
            ["member_policies.team_id", "member_policies.user_id"],
            name=op.f("fk_policy_grants_team_id_user_id_member_policies"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["team_id"],
            ["teams.id"],
            name=op.f("fk_policy_grants_team_id_teams"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_policy_grants")),
    )
    op.create_index(op.f("ix_policy_grants_name_id"), "policy_grants", ["name_id"], unique=False)
    op.create_index(
        "uq_policy_grants_member",
        "policy_grants",
        ["team_id", "user_id", "name_id"],
        unique=True,
        postgresql_where=sa.text("user_id IS NOT NULL"),
    )
    op.create_index(
        "uq_policy_grants_org",
        "policy_grants",
        ["org_id", "name_id"],
        unique=True,
        postgresql_where=sa.text("org_id IS NOT NULL"),
    )
    op.create_index(
        "uq_policy_grants_service_account",
        "policy_grants",
        ["service_account_id", "name_id"],
        unique=True,
        postgresql_where=sa.text("service_account_id IS NOT NULL"),
    )
    op.create_index(
        "uq_policy_grants_team",
        "policy_grants",
        ["team_id", "name_id"],
        unique=True,
        postgresql_where=sa.text(
            "user_id IS NULL AND service_account_id IS NULL AND org_id IS NULL"
        ),
    )
    op.create_table(
        "router_split_members",
        sa.Column("split_id", sa.Uuid(), nullable=False),
        sa.Column("model_id", sa.Uuid(), nullable=False),
        sa.Column("weight", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "params",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "jsonb_typeof(params) = 'object'", name=op.f("ck_router_split_members_params_object")
        ),
        sa.CheckConstraint("weight >= 0", name=op.f("ck_router_split_members_weight_nonneg")),
        sa.ForeignKeyConstraint(
            ["model_id"],
            ["models.name_id"],
            name=op.f("fk_router_split_members_model_id_models"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["split_id"],
            ["router_splits.id"],
            name=op.f("fk_router_split_members_split_id_router_splits"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("split_id", "model_id", name=op.f("pk_router_split_members")),
        sa.UniqueConstraint(
            "split_id", "position", name=op.f("uq_router_split_members_split_id_position")
        ),
    )
    op.create_index(
        op.f("ix_router_split_members_model_id"), "router_split_members", ["model_id"], unique=False
    )
    op.execute(CONFIG_VERSIONS_IMMUTABLE)
    op.execute(
        "CREATE TRIGGER config_versions_immutable BEFORE UPDATE ON config_versions"
        " FOR EACH ROW EXECUTE FUNCTION lumewarp_config_versions_immutable()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS config_versions_immutable ON config_versions")
    op.execute("DROP FUNCTION IF EXISTS lumewarp_config_versions_immutable()")
    op.drop_index(op.f("ix_router_split_members_model_id"), table_name="router_split_members")
    op.drop_table("router_split_members")
    op.drop_index(
        "uq_policy_grants_team",
        table_name="policy_grants",
        postgresql_where=sa.text(
            "user_id IS NULL AND service_account_id IS NULL AND org_id IS NULL"
        ),
    )
    op.drop_index(
        "uq_policy_grants_service_account",
        table_name="policy_grants",
        postgresql_where=sa.text("service_account_id IS NOT NULL"),
    )
    op.drop_index(
        "uq_policy_grants_org",
        table_name="policy_grants",
        postgresql_where=sa.text("org_id IS NOT NULL"),
    )
    op.drop_index(
        "uq_policy_grants_member",
        table_name="policy_grants",
        postgresql_where=sa.text("user_id IS NOT NULL"),
    )
    op.drop_index(op.f("ix_policy_grants_name_id"), table_name="policy_grants")
    op.drop_table("policy_grants")
    op.drop_table("gateway_strategies")
    op.drop_index(
        "uq_api_keys_previous_key_hash",
        table_name="api_keys",
        postgresql_where=sa.text("previous_key_hash IS NOT NULL"),
    )
    op.drop_index(op.f("ix_api_keys_user_id"), table_name="api_keys")
    op.drop_index(op.f("ix_api_keys_team_id"), table_name="api_keys")
    op.drop_index(op.f("ix_api_keys_service_account_id"), table_name="api_keys")
    op.drop_table("api_keys")
    op.drop_index(op.f("ix_team_members_user_id"), table_name="team_members")
    op.drop_table("team_members")
    op.drop_table("service_accounts")
    op.drop_table("router_splits")
    op.drop_index(op.f("ix_router_members_model_id"), table_name="router_members")
    op.drop_table("router_members")
    op.drop_index(op.f("ix_router_fallbacks_model_id"), table_name="router_fallbacks")
    op.drop_table("router_fallbacks")
    op.drop_index(op.f("ix_member_policies_user_id"), table_name="member_policies")
    op.drop_table("member_policies")
    op.drop_table("gateway_sessions")
    op.drop_index(op.f("ix_user_sessions_user_id"), table_name="user_sessions")
    op.drop_table("user_sessions")
    op.drop_index(op.f("ix_user_identities_user_id"), table_name="user_identities")
    op.drop_table("user_identities")
    op.drop_index(op.f("ix_teams_org_id"), table_name="teams")
    op.drop_table("teams")
    op.drop_table("routers")
    op.drop_index(op.f("ix_personal_access_tokens_user_id"), table_name="personal_access_tokens")
    op.drop_table("personal_access_tokens")
    op.drop_table("org_budget_windows")
    op.drop_index(op.f("ix_org_admins_user_id"), table_name="org_admins")
    op.drop_table("org_admins")
    op.drop_index(op.f("ix_models_upstream_id"), table_name="models")
    op.drop_table("models")
    op.drop_index(
        "uq_installation_admins_proxy_admin",
        table_name="installation_admins",
        postgresql_where=sa.text("role = 'proxy_admin'"),
    )
    op.drop_table("installation_admins")
    op.drop_index(op.f("ix_gateway_tokens_scope"), table_name="gateway_tokens")
    op.drop_table("gateway_tokens")
    op.drop_index(op.f("ix_config_versions_scope"), table_name="config_versions")
    op.drop_table("config_versions")
    op.drop_index(
        "ix_config_outbox_pending",
        table_name="config_outbox",
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_table("config_outbox")
    op.drop_index(op.f("ix_aliases_target_name_id"), table_name="aliases")
    op.drop_table("aliases")
    op.drop_index("uq_users_email_lower", table_name="users")
    op.drop_table("users")
    op.drop_table("usage_totals")
    op.drop_index("ix_usage_events_upstream_model_ts", table_name="usage_events")
    op.drop_index(op.f("ix_usage_events_ts"), table_name="usage_events")
    op.drop_index("ix_usage_events_team_ts", table_name="usage_events")
    op.drop_index("ix_usage_events_team_model_id_ts", table_name="usage_events")
    op.drop_index(
        "ix_usage_events_router_id_ts",
        table_name="usage_events",
        postgresql_where=sa.text("router_id IS NOT NULL"),
    )
    op.drop_index("ix_usage_events_owner_ts", table_name="usage_events")
    op.drop_index("ix_usage_events_model_ts", table_name="usage_events")
    op.drop_index("ix_usage_events_model_id_ts", table_name="usage_events")
    op.drop_index("ix_usage_events_key_ts", table_name="usage_events")
    op.drop_index(
        "ix_usage_events_alias_id_ts",
        table_name="usage_events",
        postgresql_where=sa.text("alias_id IS NOT NULL"),
    )
    op.drop_table("usage_events")
    op.drop_table("upstreams")
    op.drop_table("scopes")
    op.drop_table("orgs")
    op.drop_table("oidc_login_states")
    op.drop_table("catalog_names")
    op.drop_index(op.f("ix_catalog_name_history_old_name"), table_name="catalog_name_history")
    op.drop_table("catalog_name_history")
    op.drop_index(op.f("ix_audit_log_ts"), table_name="audit_log")
    op.drop_index("ix_audit_log_team_ts", table_name="audit_log")
    op.drop_index("ix_audit_log_org_ts", table_name="audit_log")
    op.drop_table("audit_log")
