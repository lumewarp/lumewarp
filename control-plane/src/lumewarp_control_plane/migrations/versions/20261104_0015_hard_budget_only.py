"""Remove the notifier feature and the soft budget.

Revision ID: 0015
Revises: 0014

Budgets are hard only: a scope is blocked once spend reaches `budget_nano_usd`. The
per-level `soft_budget_nano_usd` columns, `usage_totals.soft_notified_at` and the
`notifier_channels`/`notifier_outbox` tables are dropped.

Downgrade recreates the columns and empty tables; it cannot restore the notification
history they held.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POLICY_TABLES = ("teams", "team_members", "service_accounts")

# The webhook config contract added by 0013; a downgrade has to restore it with the table.
WEBHOOK_CONFIG_CHECK = (
    "kind <> 'webhook' OR coalesce("
    " jsonb_typeof(config) = 'object'"
    " AND config ? 'url_ref'"
    " AND jsonb_typeof(config->'url_ref') = 'string'"
    " AND (config->>'url_ref') ~ '^[A-Za-z_][A-Za-z0-9_]*$'"
    " AND octet_length(config->>'url_ref') <= 200"
    " AND (NOT (config ? 'timeout_seconds')"
    " OR (CASE WHEN jsonb_typeof(config->'timeout_seconds') <> 'number' THEN false"
    " ELSE (config->>'timeout_seconds')::numeric >= 0.001"
    " AND (config->>'timeout_seconds')::numeric <= 30 END))"
    " AND config - 'url_ref' - 'timeout_seconds' = '{}'::jsonb,"
    " false)"
)


def upgrade() -> None:
    op.drop_index(op.f("ix_notifier_outbox_due"), table_name="notifier_outbox")
    op.drop_table("notifier_outbox")
    op.drop_index(op.f("ix_notifier_channels_team_id"), table_name="notifier_channels")
    op.drop_table("notifier_channels")
    op.drop_column("usage_totals", "soft_notified_at")
    for table in POLICY_TABLES:
        op.drop_constraint(op.f(f"ck_{table}_soft_le_hard"), table, type_="check")
        op.drop_constraint(op.f(f"ck_{table}_soft_nonneg"), table, type_="check")
        op.drop_column(table, "soft_budget_nano_usd")


def downgrade() -> None:
    for table in POLICY_TABLES:
        op.add_column(table, sa.Column("soft_budget_nano_usd", sa.BigInteger(), nullable=True))
        op.create_check_constraint(
            "soft_nonneg", table, "soft_budget_nano_usd IS NULL OR soft_budget_nano_usd >= 0"
        )
        op.create_check_constraint(
            "soft_le_hard",
            table,
            "soft_budget_nano_usd IS NULL OR budget_nano_usd IS NULL"
            " OR soft_budget_nano_usd <= budget_nano_usd",
        )
    op.add_column(
        "usage_totals", sa.Column("soft_notified_at", sa.DateTime(timezone=True), nullable=True)
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
    op.create_check_constraint("webhook_config_url_ref", "notifier_channels", WEBHOOK_CONFIG_CHECK)
    op.create_index(
        op.f("ix_notifier_channels_team_id"), "notifier_channels", ["team_id"], unique=False
    )
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
