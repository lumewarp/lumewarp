"""Replace superuser credentials with IAM groups and emergency cookie sessions.

Revision ID: 0004
Revises: 0003

Existing audit rows are migrated with the enum check constraint. Downgrade
revokes emergency sessions and discards captured groups; removed superuser grants
cannot be recovered and are restored as false.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("users", "is_superuser")
    op.drop_constraint(op.f("ck_audit_log_actor_type"), "audit_log", type_="check")
    op.execute(
        "UPDATE audit_log SET actor_type = 'break_glass' WHERE actor_type = 'superuser_token'"
    )
    op.create_check_constraint(
        op.f("ck_audit_log_actor_type"),
        "audit_log",
        "actor_type IN ('break_glass', 'user', 'gateway', 'system')",
    )
    op.alter_column("user_sessions", "user_id", nullable=True)
    op.add_column(
        "user_sessions",
        sa.Column("kind", sa.String(20), nullable=False, server_default="session"),
    )
    op.add_column(
        "user_sessions",
        sa.Column("groups", postgresql.JSONB(), nullable=False, server_default="[]"),
    )
    op.create_check_constraint(
        op.f("ck_user_sessions_kind"),
        "user_sessions",
        "(kind = 'session' AND user_id IS NOT NULL) OR (kind = 'break_glass' AND user_id IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_user_sessions_kind"), "user_sessions", type_="check")
    op.execute("DELETE FROM user_sessions WHERE kind = 'break_glass'")
    op.drop_column("user_sessions", "groups")
    op.drop_column("user_sessions", "kind")
    op.alter_column("user_sessions", "user_id", nullable=False)
    op.drop_constraint(op.f("ck_audit_log_actor_type"), "audit_log", type_="check")
    op.execute(
        "UPDATE audit_log SET actor_type = 'superuser_token' WHERE actor_type = 'break_glass'"
    )
    op.create_check_constraint(
        op.f("ck_audit_log_actor_type"),
        "audit_log",
        "actor_type IN ('superuser_token', 'user', 'gateway', 'system')",
    )
    op.add_column(
        "users", sa.Column("is_superuser", sa.Boolean(), nullable=False, server_default="false")
    )
