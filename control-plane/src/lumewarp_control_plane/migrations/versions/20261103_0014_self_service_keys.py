"""Self-service key limits and rotation grace.

Revision ID: 0014
Revises: 0012

0013 belongs to the secrets task; this revision is written on the current head and the
merge rechains it after 0013. It adds the team-level member key limits (source only) and
the previous-hash pair used by rotation grace.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("teams", sa.Column("max_keys_per_member", sa.Integer(), nullable=True))
    op.add_column(
        "teams", sa.Column("max_member_key_lifetime_seconds", sa.Integer(), nullable=True)
    )
    op.create_check_constraint(
        "max_keys_per_member_positive",
        "teams",
        "max_keys_per_member IS NULL OR max_keys_per_member > 0",
    )
    op.create_check_constraint(
        "max_member_key_lifetime_positive",
        "teams",
        "max_member_key_lifetime_seconds IS NULL OR max_member_key_lifetime_seconds > 0",
    )
    op.add_column("api_keys", sa.Column("previous_key_hash", sa.LargeBinary(), nullable=True))
    op.add_column(
        "api_keys",
        sa.Column("previous_key_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "previous_key_hash_len",
        "api_keys",
        "previous_key_hash IS NULL OR octet_length(previous_key_hash) = 32",
    )
    op.create_check_constraint(
        "previous_key_pair",
        "api_keys",
        "(previous_key_hash IS NULL) = (previous_key_expires_at IS NULL)",
    )
    op.create_index(
        "uq_api_keys_previous_key_hash",
        "api_keys",
        ["previous_key_hash"],
        unique=True,
        postgresql_where=sa.text("previous_key_hash IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(op.f("uq_api_keys_previous_key_hash"), table_name="api_keys")
    op.drop_constraint(op.f("ck_api_keys_previous_key_pair"), "api_keys", type_="check")
    op.drop_constraint(op.f("ck_api_keys_previous_key_hash_len"), "api_keys", type_="check")
    op.drop_column("api_keys", "previous_key_expires_at")
    op.drop_column("api_keys", "previous_key_hash")
    op.drop_constraint(op.f("ck_teams_max_member_key_lifetime_positive"), "teams", type_="check")
    op.drop_constraint(op.f("ck_teams_max_keys_per_member_positive"), "teams", type_="check")
    op.drop_column("teams", "max_member_key_lifetime_seconds")
    op.drop_column("teams", "max_keys_per_member")
