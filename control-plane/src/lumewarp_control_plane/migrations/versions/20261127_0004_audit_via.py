"""audit via

Revision ID: 0004
Revises: 0003

Adds the nullable `audit_log.via` column: how an API caller reached the handler
(`session`, `pat`, or `mcp` for a PAT through the control plane's MCP server). Gateway and
system rows leave it NULL, and rows written before this revision keep NULL (unknown), so
no backfill is needed.

Downgrades are not supported (restore a database backup instead).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "audit_log",
        sa.Column(
            "via",
            sa.Enum(
                "session",
                "pat",
                "mcp",
                name="audit_via",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        op.f("ck_audit_log_audit_via"),
        "audit_log",
        "via IN ('session', 'pat', 'mcp')",
    )


def downgrade() -> None:
    """Downgrades are not supported: restore a database backup instead."""
    raise NotImplementedError("restore a database backup")
