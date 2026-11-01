"""Compiled-in custom routing strategies.

Revision ID: 0005
Revises: 0003 (lead re-points to 0004 at merge).
Downgrade drops custom settings; routes resume their stored built-in default.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "route_custom_strategies",
        sa.Column("route_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(63), nullable=False),
        sa.Column(
            "params", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.CheckConstraint("name ~ '^[a-z][a-z0-9_]{0,62}$'", name="strategy_name"),
        sa.CheckConstraint("jsonb_typeof(params) = 'object'", name="params_object"),
        sa.ForeignKeyConstraint(["route_id"], ["routes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("route_id"),
    )


def downgrade() -> None:
    op.drop_table("route_custom_strategies")
