"""router reasoning default level

Revision ID: 0003
Revises: 0002

`routers.reasoning_default_level`, the level a router injects when a request sends no
`reasoning_effort` (proto `Router.reasoning_default_level`). Nullable: every existing
router keeps no default. Additive and lossless; the API only accepts a level every
member, split member and fallback accepts.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("routers", sa.Column("reasoning_default_level", sa.String(length=32)))


def downgrade() -> None:
    raise NotImplementedError(
        "Lumewarp does not support schema downgrades: restore the database backup taken"
        " before the upgrade."
    )
