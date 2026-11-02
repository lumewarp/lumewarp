"""Strategy descriptors reported by gateways in Hello.

Revision ID: 0012
Revises: 0011

The descriptor is untrusted data from the gateway, kept so the UI and write validation work
while it is offline. Whether a row still counts (30 days, revoked token) is decided on read.
Downgrade drops the table; gateways report again when they next connect.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "gateway_strategies",
        sa.Column("scope", sa.String(128), nullable=False),
        sa.Column("gateway_id", sa.String(200), nullable=False),
        sa.Column("name", sa.String(63), nullable=False),
        sa.Column("digest", sa.String(128), nullable=False),
        sa.Column("descriptor", postgresql.JSONB(), nullable=False),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["scope", "gateway_id"],
            ["gateway_sessions.scope", "gateway_sessions.gateway_id"],
            name=op.f("fk_gateway_strategies_scope_gateway_sessions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("scope", "gateway_id", "name", name=op.f("pk_gateway_strategies")),
    )


def downgrade() -> None:
    op.drop_table("gateway_strategies")
