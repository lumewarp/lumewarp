"""Installation roles in Postgres.

Revision ID: 0016
Revises: 0015

Replaces the IAM-group based installation admins with `installation_admins`: a role row per
user (`proxy_admin` or `admin`), with a partial unique index allowing one `proxy_admin`.
There is no backfill: an upgraded installation starts with no rows and uses break-glass (or
a fresh first login with `LUMEWARP_INITIAL_ADMIN_EMAIL`) to create the first one.

`assigned_by` is the assigning user (NULL for break-glass and the first-login bootstrap).
`ON DELETE CASCADE` removes the role with the account; the API protects the proxy_admin
account itself. `user_sessions.admin_group` goes away: sessions no longer carry IAM groups.

Downgrade recreates the empty `admin_group` column (its values are gone) and drops the table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
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
    op.drop_column("user_sessions", "admin_group")


def downgrade() -> None:
    op.add_column("user_sessions", sa.Column("admin_group", sa.Text(), nullable=True))
    op.drop_index("uq_installation_admins_proxy_admin", table_name="installation_admins")
    op.drop_table("installation_admins")
