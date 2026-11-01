"""Bind emergency sessions to credentials and retain only the matched admin group.

Revision ID: 0006
Revises: 0005

Existing sessions are revoked: old emergency sessions have no fingerprint and old
OIDC sessions must reauthenticate rather than retain unneeded group claims.
Downgrade also revokes sessions because the old schema cannot bind credentials.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE user_sessions SET revoked_at = now() WHERE revoked_at IS NULL")
    op.add_column("user_sessions", sa.Column("admin_group", sa.Text(), nullable=True))
    op.add_column(
        "user_sessions", sa.Column("credential_fingerprint", sa.LargeBinary(), nullable=True)
    )
    op.drop_column("user_sessions", "groups")


def downgrade() -> None:
    op.execute("UPDATE user_sessions SET revoked_at = now() WHERE revoked_at IS NULL")
    op.add_column(
        "user_sessions",
        sa.Column("groups", postgresql.JSONB(), nullable=False, server_default="[]"),
    )
    op.drop_column("user_sessions", "credential_fingerprint")
    op.drop_column("user_sessions", "admin_group")
