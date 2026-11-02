"""Strategy descriptors reported by gateways in Hello.

Revision ID: 0012
Revises: 0011

The descriptor is untrusted data from the gateway, kept so the UI and write validation work
while it is offline. Whether a row still counts (30 days, its reporting token revoked) is
decided on read, so each row and each session records the token of the Hello behind it.
Downgrade drops both; gateways report again when they next connect.
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
    op.add_column("gateway_sessions", sa.Column("token_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_gateway_sessions_token_id_gateway_tokens"),
        "gateway_sessions",
        "gateway_tokens",
        ["token_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_table(
        "gateway_strategies",
        sa.Column("scope", sa.String(128), nullable=False),
        sa.Column("gateway_id", sa.String(200), nullable=False),
        sa.Column("name", sa.String(63), nullable=False),
        sa.Column("digest", sa.String(128), nullable=False),
        sa.Column("descriptor", postgresql.JSONB(), nullable=False),
        sa.Column("token_id", sa.Uuid(), nullable=False),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["scope", "gateway_id"],
            ["gateway_sessions.scope", "gateway_sessions.gateway_id"],
            name=op.f("fk_gateway_strategies_scope_gateway_sessions"),
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


def downgrade() -> None:
    op.drop_table("gateway_strategies")
    op.drop_constraint(
        op.f("fk_gateway_sessions_token_id_gateway_tokens"), "gateway_sessions", type_="foreignkey"
    )
    op.drop_column("gateway_sessions", "token_id")
