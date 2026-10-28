"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-10-27 21:27:59.535164
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Published snapshots never change. The only allowed update is compaction: payload -> NULL
# together with compacted_at, everything else identical.
CONFIG_VERSIONS_IMMUTABLE = """
CREATE FUNCTION lumewarp_config_versions_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.scope IS DISTINCT FROM OLD.scope
        OR NEW.version IS DISTINCT FROM OLD.version
        OR NEW.digest IS DISTINCT FROM OLD.digest
        OR NEW.size_bytes IS DISTINCT FROM OLD.size_bytes
        OR NEW.key_count IS DISTINCT FROM OLD.key_count
        OR NEW.source_revision IS DISTINCT FROM OLD.source_revision
        OR NEW.kind IS DISTINCT FROM OLD.kind
        OR NEW.rollback_of_version IS DISTINCT FROM OLD.rollback_of_version
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
        OR (NEW.payload IS DISTINCT FROM OLD.payload AND NEW.payload IS NOT NULL)
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

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


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
                "superuser_token",
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
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )
    op.create_index("ix_audit_log_team_ts", "audit_log", ["team_id", "ts"], unique=False)
    op.create_index(op.f("ix_audit_log_ts"), "audit_log", ["ts"], unique=False)
    op.create_table(
        "routes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("model", sa.String(length=400), nullable=False),
        sa.Column(
            "strategy",
            sa.Enum(
                "weighted",
                "least_in_flight",
                "lowest_latency",
                name="routing_strategy",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="weighted",
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_routes")),
        sa.UniqueConstraint("model", name=op.f("uq_routes_model")),
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
        "teams",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column(
            "allowed_models",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("rpm_limit", sa.Integer(), nullable=True),
        sa.Column("tpm_limit", sa.BigInteger(), nullable=True),
        sa.Column("max_parallel", sa.Integer(), nullable=True),
        sa.Column("budget_nano_usd", sa.BigInteger(), nullable=True),
        sa.Column("soft_budget_nano_usd", sa.BigInteger(), nullable=True),
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
            "model_access = 'list' OR cardinality(allowed_models) = 0",
            name=op.f("ck_teams_allowlist_mode"),
        ),
        sa.CheckConstraint(
            "budget_nano_usd IS NULL OR budget_nano_usd >= 0", name=op.f("ck_teams_budget_nonneg")
        ),
        sa.CheckConstraint(
            "max_parallel IS NULL OR max_parallel > 0", name=op.f("ck_teams_parallel_positive")
        ),
        sa.CheckConstraint(
            "rpm_limit IS NULL OR rpm_limit > 0", name=op.f("ck_teams_rpm_positive")
        ),
        sa.CheckConstraint(
            "soft_budget_nano_usd IS NULL OR budget_nano_usd IS NULL OR soft_budget_nano_usd <= budget_nano_usd",
            name=op.f("ck_teams_soft_le_hard"),
        ),
        sa.CheckConstraint(
            "soft_budget_nano_usd IS NULL OR soft_budget_nano_usd >= 0",
            name=op.f("ck_teams_soft_nonneg"),
        ),
        sa.CheckConstraint(
            "tpm_limit IS NULL OR tpm_limit > 0", name=op.f("ck_teams_tpm_positive")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_teams")),
        sa.UniqueConstraint("name", name=op.f("uq_teams_name")),
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
        sa.Column("model", sa.String(length=400), nullable=False),
        sa.Column("deployment", sa.String(length=200), nullable=False),
        sa.Column("route", sa.String(length=64), nullable=False),
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
        sa.CheckConstraint(
            "prompt_tokens >= 0 AND completion_tokens >= 0 AND cost_nano_usd >= 0",
            name=op.f("ck_usage_events_nonneg"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_usage_events")),
        sa.UniqueConstraint("request_id", name=op.f("uq_usage_events_request_id")),
    )
    op.create_index("ix_usage_events_key_ts", "usage_events", ["key_id", "ts"], unique=False)
    op.create_index("ix_usage_events_owner_ts", "usage_events", ["owner_id", "ts"], unique=False)
    op.create_index("ix_usage_events_team_ts", "usage_events", ["team_id", "ts"], unique=False)
    op.create_index(op.f("ix_usage_events_ts"), "usage_events", ["ts"], unique=False)
    op.create_table(
        "usage_totals",
        sa.Column(
            "scope_type",
            sa.Enum(
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
        sa.Column("soft_notified_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.Column("is_superuser", sa.Boolean(), server_default="false", nullable=False),
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
        "deployments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("upstream_id", sa.Uuid(), nullable=False),
        sa.Column("upstream_model", sa.String(length=400), nullable=False),
        sa.Column("weight", sa.Integer(), server_default="1", nullable=False),
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
        sa.CheckConstraint(
            "(price_input_nano_usd_per_mtok IS NULL) = (price_output_nano_usd_per_mtok IS NULL)",
            name=op.f("ck_deployments_price_both_or_neither"),
        ),
        sa.CheckConstraint(
            "price_input_nano_usd_per_mtok IS NULL OR (price_input_nano_usd_per_mtok >= 0 AND price_output_nano_usd_per_mtok >= 0)",
            name=op.f("ck_deployments_price_nonneg"),
        ),
        sa.CheckConstraint("weight >= 0", name=op.f("ck_deployments_weight_nonneg")),
        sa.ForeignKeyConstraint(
            ["upstream_id"],
            ["upstreams.id"],
            name=op.f("fk_deployments_upstream_id_upstreams"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deployments")),
        sa.UniqueConstraint("name", name=op.f("uq_deployments_name")),
    )
    op.create_index(
        op.f("ix_deployments_upstream_id"), "deployments", ["upstream_id"], unique=False
    )
    op.create_table(
        "gateway_sessions",
        sa.Column("scope", sa.String(length=128), nullable=False),
        sa.Column("gateway_id", sa.String(length=200), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=True),
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
        sa.PrimaryKeyConstraint("scope", "gateway_id", name=op.f("pk_gateway_sessions")),
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
        "notifier_channels",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=100), nullable=False),
        sa.Column("team_id", sa.Uuid(), nullable=True),
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), server_default="true", nullable=False),
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
            name=op.f("fk_notifier_channels_team_id_teams"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notifier_channels")),
        sa.UniqueConstraint("name", name=op.f("uq_notifier_channels_name")),
    )
    op.create_index(
        op.f("ix_notifier_channels_team_id"), "notifier_channels", ["team_id"], unique=False
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
        "service_accounts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "allowed_models",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("rpm_limit", sa.Integer(), nullable=True),
        sa.Column("tpm_limit", sa.BigInteger(), nullable=True),
        sa.Column("max_parallel", sa.Integer(), nullable=True),
        sa.Column("budget_nano_usd", sa.BigInteger(), nullable=True),
        sa.Column("soft_budget_nano_usd", sa.BigInteger(), nullable=True),
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
            "model_access = 'list' OR cardinality(allowed_models) = 0",
            name=op.f("ck_service_accounts_allowlist_mode"),
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
            "soft_budget_nano_usd IS NULL OR budget_nano_usd IS NULL OR soft_budget_nano_usd <= budget_nano_usd",
            name=op.f("ck_service_accounts_soft_le_hard"),
        ),
        sa.CheckConstraint(
            "soft_budget_nano_usd IS NULL OR soft_budget_nano_usd >= 0",
            name=op.f("ck_service_accounts_soft_nonneg"),
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
            "allowed_models",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("rpm_limit", sa.Integer(), nullable=True),
        sa.Column("tpm_limit", sa.BigInteger(), nullable=True),
        sa.Column("max_parallel", sa.Integer(), nullable=True),
        sa.Column("budget_nano_usd", sa.BigInteger(), nullable=True),
        sa.Column("soft_budget_nano_usd", sa.BigInteger(), nullable=True),
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
            "model_access = 'list' OR cardinality(allowed_models) = 0",
            name=op.f("ck_team_members_allowlist_mode"),
        ),
        sa.CheckConstraint(
            "budget_nano_usd IS NULL OR budget_nano_usd >= 0",
            name=op.f("ck_team_members_budget_nonneg"),
        ),
        sa.CheckConstraint(
            "max_parallel IS NULL OR max_parallel > 0",
            name=op.f("ck_team_members_parallel_positive"),
        ),
        sa.CheckConstraint(
            "rpm_limit IS NULL OR rpm_limit > 0", name=op.f("ck_team_members_rpm_positive")
        ),
        sa.CheckConstraint(
            "soft_budget_nano_usd IS NULL OR budget_nano_usd IS NULL OR soft_budget_nano_usd <= budget_nano_usd",
            name=op.f("ck_team_members_soft_le_hard"),
        ),
        sa.CheckConstraint(
            "soft_budget_nano_usd IS NULL OR soft_budget_nano_usd >= 0",
            name=op.f("ck_team_members_soft_nonneg"),
        ),
        sa.CheckConstraint(
            "tpm_limit IS NULL OR tpm_limit > 0", name=op.f("ck_team_members_tpm_positive")
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
        sa.CheckConstraint("octet_length(key_hash) = 32", name=op.f("ck_api_keys_key_hash_len")),
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
    op.create_table(
        "notifier_outbox",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("dedupe_key", sa.String(length=400), nullable=False),
        sa.Column("channel_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
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
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_notifier_outbox_attempts_nonneg")),
        sa.ForeignKeyConstraint(
            ["channel_id"],
            ["notifier_channels.id"],
            name=op.f("fk_notifier_outbox_channel_id_notifier_channels"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notifier_outbox")),
        sa.UniqueConstraint("dedupe_key", name=op.f("uq_notifier_outbox_dedupe_key")),
    )
    op.create_index(
        "ix_notifier_outbox_due",
        "notifier_outbox",
        ["next_attempt_at"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_table(
        "route_deployments",
        sa.Column("route_id", sa.Uuid(), nullable=False),
        sa.Column("deployment_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["deployment_id"],
            ["deployments.id"],
            name=op.f("fk_route_deployments_deployment_id_deployments"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["route_id"],
            ["routes.id"],
            name=op.f("fk_route_deployments_route_id_routes"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("route_id", "deployment_id", name=op.f("pk_route_deployments")),
        sa.UniqueConstraint(
            "route_id", "position", name=op.f("uq_route_deployments_route_id_position")
        ),
    )
    op.create_index(
        op.f("ix_route_deployments_deployment_id"),
        "route_deployments",
        ["deployment_id"],
        unique=False,
    )
    op.execute(CONFIG_VERSIONS_IMMUTABLE)
    op.execute(
        "CREATE TRIGGER config_versions_immutable BEFORE UPDATE ON config_versions"
        " FOR EACH ROW EXECUTE FUNCTION lumewarp_config_versions_immutable()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS config_versions_immutable ON config_versions")
    op.execute("DROP FUNCTION IF EXISTS lumewarp_config_versions_immutable()")
    op.drop_index(op.f("ix_route_deployments_deployment_id"), table_name="route_deployments")
    op.drop_table("route_deployments")
    op.drop_index(
        "ix_notifier_outbox_due",
        table_name="notifier_outbox",
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_table("notifier_outbox")
    op.drop_index(op.f("ix_api_keys_user_id"), table_name="api_keys")
    op.drop_index(op.f("ix_api_keys_team_id"), table_name="api_keys")
    op.drop_index(op.f("ix_api_keys_service_account_id"), table_name="api_keys")
    op.drop_table("api_keys")
    op.drop_index(op.f("ix_team_members_user_id"), table_name="team_members")
    op.drop_table("team_members")
    op.drop_table("service_accounts")
    op.drop_index(op.f("ix_personal_access_tokens_user_id"), table_name="personal_access_tokens")
    op.drop_table("personal_access_tokens")
    op.drop_index(op.f("ix_notifier_channels_team_id"), table_name="notifier_channels")
    op.drop_table("notifier_channels")
    op.drop_index(op.f("ix_gateway_tokens_scope"), table_name="gateway_tokens")
    op.drop_table("gateway_tokens")
    op.drop_table("gateway_sessions")
    op.drop_index(op.f("ix_deployments_upstream_id"), table_name="deployments")
    op.drop_table("deployments")
    op.drop_index(op.f("ix_config_versions_scope"), table_name="config_versions")
    op.drop_table("config_versions")
    op.drop_index(
        "ix_config_outbox_pending",
        table_name="config_outbox",
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_table("config_outbox")
    op.drop_index("uq_users_email_lower", table_name="users")
    op.drop_table("users")
    op.drop_table("usage_totals")
    op.drop_index(op.f("ix_usage_events_ts"), table_name="usage_events")
    op.drop_index("ix_usage_events_team_ts", table_name="usage_events")
    op.drop_index("ix_usage_events_owner_ts", table_name="usage_events")
    op.drop_index("ix_usage_events_key_ts", table_name="usage_events")
    op.drop_table("usage_events")
    op.drop_table("upstreams")
    op.drop_table("teams")
    op.drop_table("scopes")
    op.drop_table("routes")
    op.drop_index(op.f("ix_audit_log_ts"), table_name="audit_log")
    op.drop_index("ix_audit_log_team_ts", table_name="audit_log")
    op.drop_table("audit_log")
