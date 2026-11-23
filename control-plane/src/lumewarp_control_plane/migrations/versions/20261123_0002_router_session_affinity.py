"""router session affinity

Revision ID: 0002
Revises: 0001

Adds the router's session-affinity switch and its optional custom session header. The
switch defaults to false so every existing router keeps its current behaviour; the header
is nullable and only valid while the switch is on, as a lower-case `x-<name>-id`.

Downgrades are not supported (restore a database backup instead).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "routers",
        sa.Column(
            "session_affinity", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
    )
    op.add_column(
        "routers", sa.Column("session_affinity_header", sa.String(length=200), nullable=True)
    )
    op.create_check_constraint(
        op.f("ck_routers_session_affinity_header_iff"),
        "routers",
        "session_affinity OR session_affinity_header IS NULL",
    )
    op.create_check_constraint(
        op.f("ck_routers_session_affinity_header_format"),
        "routers",
        "session_affinity_header IS NULL OR session_affinity_header ~ '^x-[a-z0-9-]+-id$'",
    )


def downgrade() -> None:
    """Downgrades are not supported: restore a database backup instead."""
    raise NotImplementedError("restore a database backup")
